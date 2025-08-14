from fastapi import FastAPI, Request
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from dateutil import parser as du
import pytz
import os, json
from typing import Optional
from pydantic import ValidationError
import logging, json
from fastapi import Request
from pydantic import ValidationError


SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Load environment variables and credentials
from dotenv import load_dotenv
load_dotenv()

SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

CALENDAR_ID = os.getenv("CALENDAR_ID")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
PHARM_TZ = "America/Toronto"

calendar_service = build("calendar", "v3", credentials=creds)
sheets_service = build("sheets", "v4", credentials=creds)
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Header

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- MODELS ----
class Slot(BaseModel):
    start: str
    end: str
    speakable: str

class FindSlotsReq(BaseModel):
    appointment_type: str
    preferred_datetime_text: str
    limit: int = 3

class FindSlotsRes(BaseModel):
    slots: list[Slot]
    reason: str | None = None

class Patient(BaseModel):
    name: str
    contact: str
    email: str | None = None

class CreateEventReq(BaseModel):
    appointment_type: str
    slot: Slot
    patient: Patient
    idempotency_key: Optional[str] = None
    notes: Optional[str] = None

class CreateEventRes(BaseModel):
    success: bool
    booking_ref: str | None = None
    event_id: str | None = None
    confirm_speakable: str | None = None
    error: str | None = None
    reason: str | None = None

class RescheduleReq(BaseModel):
    booking_ref: Optional[str] = None
    name: Optional[str] = None
    contact: Optional[str] = None
    new_preferred_datetime_text: str
    appointment_type: Optional[str] = None  
    confirm_reschedule: bool = False  
    notes: Optional[str] = None

class RescheduleRes(BaseModel):
    success: bool
    booking_ref: str | None = None
    old_slot: str | None = None
    new_slot: Slot | None = None
    available_slots: list[Slot] | None = None  # Alternative slots if preferred time not available
    confirm_speakable: str | None = None
    error: str | None = None
    reason: str | None = None

class CancelReq(BaseModel):
    booking_ref: Optional[str] = None
    name: Optional[str] = None
    contact: Optional[str] = None
    reason: Optional[str] = None

class CancelRes(BaseModel):
    success: bool
    booking_ref: str | None = None
    cancelled_slot: str | None = None
    error: str | None = None
    reason: str | None = None

# ---- HELPERS ----
def is_business_hours(dt: datetime) -> bool:
    if dt.weekday() >= 5:
        return False
    if dt.hour < 9 or dt.hour >= 18:
        return False
    if dt.minute not in (0, 30):
        return False
    return True

def freebusy_range(start_dt: datetime, end_dt: datetime):
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items": [{"id": CALENDAR_ID}],
    }
    fb = calendar_service.freebusy().query(body=body).execute()
    return fb["calendars"][CALENDAR_ID].get("busy", [])

def is_slot_available(start_dt: datetime) -> bool:
    if not is_business_hours(start_dt):
        return False
    end_dt = start_dt + timedelta(minutes=30)
    busy = freebusy_range(start_dt, end_dt)
    return len(busy) == 0

def speakable_range(start_iso: str, end_iso: str, tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    s = du.isoparse(start_iso).astimezone(tz)
    e = du.isoparse(end_iso).astimezone(tz)
    left = s.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")  # Remove leading zeros
    right = e.strftime("%I:%M %p").replace(" 0", " ")
    return f"{left} – {right}"

def round_to_next_half_hour(dt: datetime) -> datetime:
    """Round to the nearest 30-minute boundary (00 or 30 minutes)"""
    # If already on a 30-minute boundary, return as-is
    if dt.minute in (0, 30) and dt.second == 0 and dt.microsecond == 0:
        return dt
    
    if dt.minute < 15:
        return dt.replace(minute=0, second=0, microsecond=0)
    elif dt.minute < 45:
        return dt.replace(minute=30, second=0, microsecond=0)
    else:
        return (dt + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    
def fmt_local(iso_str: str, tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    dt = du.isoparse(iso_str).astimezone(tz)
    return dt.strftime("%m/%d/%Y %I:%M %p") # google sheet format

def append_sheet_row(status: str, appt_type: str, start_iso: str, end_iso: str,
                     name: str, contact: str, ref: str, notes: str = ""):
    logged_utc = datetime.utcnow().strftime("%m/%d/%Y %H:%M:%S UTC")
    start_local = fmt_local(start_iso, PHARM_TZ)
    end_local   = fmt_local(end_iso,   PHARM_TZ)

    row = [[
        logged_utc, ref, "book", appt_type,
        start_local, end_local,
        name, contact,
        "retell-chat", notes, status
    ]]

    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A2:K",              
        valueInputOption="USER_ENTERED",  
        insertDataOption="INSERT_ROWS",
        body={"values": row, "majorDimension": "ROWS"},
    ).execute()

def booking_ref_key(name: str, contact: str, start_iso: str, appt_type: str) -> str:
    import hashlib
    h = hashlib.sha1(f"{name}|{contact}|{start_iso}|{appt_type}".encode()).hexdigest()[:6].upper()
    return f"{h[:3]}-{h[3:]}"

def find_booking_by_ref(booking_ref: str):
    """Find a booking by booking reference in calendar
    used https://developers.google.com/workspace/calendar/api/v3/reference/events/list
    to figure it out"""

    try:
        events = calendar_service.events().list(
            calendarId=CALENDAR_ID,
            privateExtendedProperty=f"bookingRef={booking_ref}",
            maxResults=1,
            singleEvents=True
        ).execute()
        
        if events.get("items"):
            return events["items"][0]
        return None
    except Exception:
        return None

def find_booking_by_patient(name: str, contact: str):
    try:
        events = calendar_service.events().list(
            calendarId=CALENDAR_ID,
            q=name,
            timeMin=datetime.utcnow().isoformat() + "Z",  
            maxResults=50,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        
        for event in events.get("items", []):
            description = event.get("description", "").lower()
            summary = event.get("summary", "").lower()
            if contact.lower() in description and name.lower() in summary:
                return event
        return None
    except Exception:
        return None

def update_sheet_status(booking_ref: str, new_status: str, notes: str = ""):
    """update the status of a booking in Google Sheets
    used: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get"""
    try:
        # Get all rows to find the booking
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:K"
        ).execute()
        
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if len(row) > 1 and row[1] == booking_ref:  # Find booking by reference
                # Allow updating status for confirmed or rescheduled appointments
                if len(row) > 10 and row[10] in ["confirmed", "rescheduled"]:
                    update_range = f"Sheet1!K{i+1}"
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=update_range,
                        valueInputOption="USER_ENTERED",
                        body={"values": [[new_status]]}
                    ).execute()

                    if notes:  # update notes if provided
                        existing_notes = row[9] if len(row) > 9 else ""
                        updated_notes = f"{existing_notes}. Cancelled: {notes}" if existing_notes else f"Cancelled: {notes}"
                        
                        notes_range = f"Sheet1!J{i+1}"  # Column J is notes (index 9)
                        sheets_service.spreadsheets().values().update(
                            spreadsheetId=SPREADSHEET_ID,
                            range=notes_range,
                            valueInputOption="USER_ENTERED",
                            body={"values": [[updated_notes]]}
                        ).execute()
                    
                    return True
        return False
    except Exception:
        return False

def update_sheet_reschedule(booking_ref: str, new_start_iso: str, new_end_iso: str, notes: str = ""):
    """Update the booking time and status for reschedule in Google Sheets"""
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Sheet1!A:K"
        ).execute()
        
        rows = result.get("values", [])
        for i, row in enumerate(rows):
            if len(row) > 1 and row[1] == booking_ref:  # Find booking by reference
                if len(row) > 10 and row[10] in ["confirmed", "rescheduled"]:
                    start_local = fmt_local(new_start_iso, PHARM_TZ)
                    end_local = fmt_local(new_end_iso, PHARM_TZ)
                    
                    existing_notes = row[9] if len(row) > 9 else ""
                    updated_notes = f"{existing_notes}. Rescheduled again: {notes}" if "Rescheduled" in existing_notes else f"Rescheduled. {notes}"
                    update_range = f"Sheet1!E{i+1}:K{i+1}"
                    sheets_service.spreadsheets().values().update(
                        spreadsheetId=SPREADSHEET_ID,
                        range=update_range,
                        valueInputOption="USER_ENTERED",
                        body={"values": [[start_local, end_local, row[6], row[7], row[8], updated_notes, "rescheduled"]]}
                    ).execute()
                    return True
                else:
                    print(f"Found booking {booking_ref} but status is {row[10] if len(row) > 10 else 'missing'}")
                    return False
        
        print(f"Booking {booking_ref} not found in sheets")
        return False
    except Exception as e:
        print(f"Error updating sheet for {booking_ref}: {str(e)}")
        return False

def top_slots(anchor: datetime, limit: int, tz: pytz.timezone) -> list[Slot]:
    out: list[Slot] = []
    now = datetime.now(tz)
    anchor = max(anchor, now + timedelta(minutes=60))
    probe = round_to_next_half_hour(anchor)
    days_checked = 0
    while len(out) < limit and days_checked < 8:
        day_end = probe.replace(hour=18, minute=0, second=0, microsecond=0)
        while probe <= day_end and len(out) < limit:
            if is_slot_available(probe):
                start_iso = probe.isoformat()
                end_iso = (probe + timedelta(minutes=30)).isoformat()
                out.append(Slot(
                    start=start_iso,
                    end=end_iso,
                    speakable=speakable_range(start_iso, end_iso, tz.zone),
                ))
            probe += timedelta(minutes=30)
        probe = (probe + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        while probe.weekday() >= 5:
            probe += timedelta(days=1)
        days_checked += 1
    return out

def find_available_slots(preferred_datetime_text: str, limit: int = 3) -> FindSlotsRes:
    tz = pytz.timezone("America/Toronto")
    anchor = du.parse(preferred_datetime_text, fuzzy=True)
    anchor = tz.localize(anchor) if anchor.tzinfo is None else anchor.astimezone(tz)

    rounded_anchor = round_to_next_half_hour(anchor)
    if is_slot_available(rounded_anchor):
        start_iso = rounded_anchor.isoformat()
        end_iso = (rounded_anchor + timedelta(minutes=30)).isoformat()
        preferred_slot = Slot(
            start=start_iso,
            end=end_iso,
            speakable=speakable_range(start_iso, end_iso, tz.zone)
        )
        return FindSlotsRes(slots=[preferred_slot], reason="preferred_time_available")
    else:
        slots = top_slots(anchor, limit, tz)
        return FindSlotsRes(slots=slots, reason="preferred_time_busy" if slots else "no_slots_available")

def _pick_args(body: dict) -> dict:
    """retell's way of extracting args from the request body."""
    if isinstance(body, dict) and "args" in body and isinstance(body["args"], dict):
        return body["args"]
    return body

@app.post("/find-slots", response_model=FindSlotsRes)
async def find_slots_endpoint(raw_req: Request):
    body = await raw_req.json()
    payload = _pick_args(body)

    try:
        req = FindSlotsReq(**payload)
    except ValidationError:
        # Your existing "ping" sample fallback
        tz = pytz.timezone(PHARM_TZ)
        s = round_to_next_half_hour(datetime.now(tz) + timedelta(hours=2))
        start_iso, end_iso = s.isoformat(), (s + timedelta(minutes=30)).isoformat()
        sample = Slot(start=start_iso, end=end_iso, speakable=speakable_range(start_iso, end_iso, PHARM_TZ))
        return {"slots": [sample.dict()], "reason": "bad_payload"}

    result = find_available_slots(req.preferred_datetime_text, req.limit)
    return {"slots": [s.dict() for s in result.slots], "reason": result.reason}

@app.post("/create-event", response_model=CreateEventRes)
async def create_event(raw_req: Request):
    body = await raw_req.json()
    payload = _pick_args(body)

    if isinstance(payload.get("slot"), str):
        try:
            payload["slot"] = json.loads(payload["slot"])
        except Exception:
            pass  

    try:
        req = CreateEventReq(**payload)
    except ValidationError as e:
        return CreateEventRes(success=False, error="validation_error", reason=str(e))

    if not req.idempotency_key:
        req.idempotency_key = booking_ref_key(
            req.patient.name, req.patient.contact, req.slot.start, req.appointment_type
        )

    slot = req.slot
    ref = booking_ref_key(req.patient.name, req.patient.contact, slot.start, req.appointment_type)

    existing = calendar_service.events().list(
        calendarId=CALENDAR_ID,
        privateExtendedProperty=f"bookingRef={ref}",
        maxResults=1, singleEvents=True
    ).execute().get("items", [])

    if existing:
        ev = existing[0]
        return CreateEventRes(
            success=True,
            booking_ref=ref,
            event_id=ev["id"],
            confirm_speakable=speakable_range(slot.start, slot.end, "America/Toronto")
        )

    s_dt = du.isoparse(slot.start)
    if not is_slot_available(s_dt):
        return CreateEventRes(success=False, error="slot_taken", reason="slot_no_longer_available")

    event = {
        "summary": f"{req.appointment_type} - {req.patient.name}",
        "description": f"Contact: {req.patient.contact}\nRef: {ref}",
        "start": {"dateTime": slot.start, "timeZone": "America/Toronto"},
        "end": {"dateTime": slot.end, "timeZone": "America/Toronto"},
        "extendedProperties": {"private": {"bookingRef": ref}},
    }
    created = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

    append_sheet_row("confirmed", req.appointment_type, slot.start, slot.end,
                     req.patient.name, req.patient.contact, ref, req.notes or "")

    return CreateEventRes(
        success=True,
        booking_ref=ref,
        event_id=created["id"],
        confirm_speakable=speakable_range(slot.start, slot.end, "America/Toronto")
    )

@app.post("/reschedule-booking", response_model=RescheduleRes)
async def reschedule_booking(raw_req: Request):
    body = await raw_req.json()
    payload = _pick_args(body)
    
    try:
        req = RescheduleReq(**payload)
    except ValidationError as e:
        return RescheduleRes(success=False, error="validation_error", reason=str(e))
    
    existing_event = None
    booking_ref = None
    
    if req.booking_ref:
        existing_event = find_booking_by_ref(req.booking_ref)
        booking_ref = req.booking_ref
    elif req.name and req.contact:
        existing_event = find_booking_by_patient(req.name, req.contact)
        if existing_event:
            booking_ref = existing_event.get("extendedProperties", {}).get("private", {}).get("bookingRef")
    
    if not existing_event:
        return RescheduleRes(success=False, error="booking_not_found", reason="No booking found with provided details")
    
    appointment_type = req.appointment_type
    if not appointment_type:
        appointment_type = existing_event["summary"].split(" - ")[0]
    
    old_start = existing_event["start"]["dateTime"]
    old_end = existing_event["end"]["dateTime"]
    old_slot_speakable = speakable_range(old_start, old_end, "America/Toronto")
    
    try:
        new_anchor = du.parse(req.new_preferred_datetime_text, fuzzy=True)
    except Exception:
        return RescheduleRes(success=False, error="invalid_datetime", reason="Could not parse new preferred datetime")
    
    slot_result = find_available_slots(req.new_preferred_datetime_text, 3)
    
    if slot_result.reason == "preferred_time_available":
        new_slot = slot_result.slots[0]
        if not req.confirm_reschedule:
            return RescheduleRes(
                success=True,
                booking_ref=booking_ref,
                old_slot=old_slot_speakable,
                new_slot=new_slot,
                reason="preferred_time_available"
            )
            
    elif slot_result.reason == "preferred_time_busy":
        if not req.confirm_reschedule:
            return RescheduleRes(
                success=False,
                booking_ref=booking_ref,
                old_slot=old_slot_speakable,
                available_slots=slot_result.slots,
                reason="preferred_time_busy"
            )
        
        new_slot = slot_result.slots[0]
        
    else:
        return RescheduleRes(success=False, error="no_slots_available", reason="No available slots found for the requested time period")
    
    if req.confirm_reschedule:
        try:
            updated_event = {
                "summary": existing_event["summary"], 
                "description": existing_event.get("description", ""),  
                "start": {"dateTime": new_slot.start, "timeZone": "America/Toronto"},
                "end": {"dateTime": new_slot.end, "timeZone": "America/Toronto"},
                "extendedProperties": existing_event.get("extendedProperties", {}),  # original properties
            }
            
            if req.notes:
                original_desc = existing_event.get("description", "")
                updated_event["description"] = f"{original_desc}\nRescheduled: {req.notes}"
            
            # Update calendar event
            calendar_service.events().update(
                calendarId=CALENDAR_ID,
                eventId=existing_event["id"],
                body=updated_event
            ).execute()
            
            # Update sheet with new times and status in the same  row
            sheet_update_success = update_sheet_reschedule(booking_ref, new_slot.start, new_slot.end, req.notes or f"Rescheduled from {old_slot_speakable} to {new_slot.speakable}")
            
            return RescheduleRes(
                success=True,
                booking_ref=booking_ref,
                old_slot=old_slot_speakable,
                new_slot=new_slot,
                confirm_speakable=new_slot.speakable,
                reason=f"Updated successfully. Sheet update: {sheet_update_success}"
            )
            
        except Exception as e:
            return RescheduleRes(success=False, error="update_failed", reason=f"Failed to update booking: {str(e)}")
    
    return RescheduleRes(success=False, error="unexpected_error", reason="Unexpected flow in reschedule logic")

@app.post("/cancel-booking", response_model=CancelRes)
async def cancel_booking(raw_req: Request):
    body = await raw_req.json()
    payload = _pick_args(body)
    
    try:
        req = CancelReq(**payload)
    except ValidationError as e:
        return CancelRes(success=False, error="validation_error", reason=str(e))
    
    existing_event = None
    booking_ref = None
    
    if req.booking_ref:
        existing_event = find_booking_by_ref(req.booking_ref)
        booking_ref = req.booking_ref
    elif req.name and req.contact:
        existing_event = find_booking_by_patient(req.name, req.contact)
        if existing_event:
            booking_ref = existing_event.get("extendedProperties", {}).get("private", {}).get("bookingRef")
    
    if not existing_event:
        return CancelRes(success=False, error="booking_not_found", reason="No booking found with provided details")
    
    start_time = existing_event["start"]["dateTime"]
    end_time = existing_event["end"]["dateTime"]
    cancelled_slot_speakable = speakable_range(start_time, end_time, "America/Toronto")
    
    try:
        calendar_service.events().delete(
            calendarId=CALENDAR_ID,
            eventId=existing_event["id"]
        ).execute()
        
        cancellation_reason = req.reason or "No reason provided"
        update_sheet_status(booking_ref, "cancelled", cancellation_reason)
        
        return CancelRes(
            success=True,
            booking_ref=booking_ref,
            cancelled_slot=cancelled_slot_speakable
        )
        
    except Exception as e:
        return CancelRes(success=False, error="cancellation_failed", reason=f"Failed to cancel booking: {str(e)}")
