from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.oauth2 import service_account
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz

# ==== CONFIG ====
SERVICE_ACCOUNT_FILE = "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets"
]
CALENDAR_ID = "c4a95f02ed44de459ce8ef700d858eed0d7755e5efc7ae4494ebd04f7b2f4084@group.calendar.google.com"
SPREADSHEET_ID = "1NshxCHnicdaOMNuavv9iJKXY8l0djtkqohQE3Bmcbz4"

# Auth
creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
calendar_service = build("calendar", "v3", credentials=creds)
sheets_service = build("sheets", "v4", credentials=creds)

app = FastAPI()

# ==== MODELS ====
class BookingRequest(BaseModel):
    appointment_type: str
    date: str  # format: YYYY-MM-DD HH:MM
    name: str
    contact: str

# ==== FUNCTIONS ====
def is_business_hours(dt):
    """Check if the datetime is within business hours (9 AM - 6 PM, Mon-Fri)"""
    # Check if it's a weekday (Monday = 0, Sunday = 6)
    if dt.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    # Check if it's within business hours (9 AM to 6 PM)
    hour = dt.hour
    minute = dt.minute
    
    # 9:00 AM to 5:30 PM (last slot starts at 5:30)
    if hour < 9 or hour >= 18:
        return False
    
    # Only allow 30-minute intervals (:00 and :30)
    if minute not in [0, 30]:
        return False
    
    return True

def is_slot_available(start_dt):
    # First check business hours
    if not is_business_hours(start_dt):
        return False
        
    end_dt = start_dt + timedelta(minutes=30)
    
    # Use FreeBusy API - more efficient than listing events
    freebusy_request = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "items": [{"id": CALENDAR_ID}]
    }
    
    freebusy_result = calendar_service.freebusy().query(body=freebusy_request).execute()
    busy_times = freebusy_result["calendars"][CALENDAR_ID].get("busy", [])
    
    # If there are any busy periods in our requested slot, it's not available
    print(f"Checking availability from {start_dt} to {end_dt}")
    print(f"Busy times: {busy_times}")

    return len(busy_times) == 0

def create_calendar_event(req: BookingRequest):
    start_dt = datetime.strptime(req.date, "%Y-%m-%d %H:%M")
    start_dt = pytz.timezone("America/Toronto").localize(start_dt)
    end_dt = start_dt + timedelta(minutes=30)

    event = {
        "summary": f"{req.appointment_type} - {req.name}",
        "description": f"Contact: {req.contact}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Toronto"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Toronto"},
    }
    created_event = calendar_service.events().insert(
        calendarId=CALENDAR_ID, body=event
    ).execute()
    print(f"Event created: {created_event.get('htmlLink')}")
    print(f"Event ID: {created_event.get('id')}")
    print(f"Event:", created_event)
    return created_event

def log_to_sheets(req: BookingRequest):
    values = [[req.name, req.contact, req.appointment_type, req.date]]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range="Sheet1!A:D",
        valueInputOption="RAW",
        body={"values": values}
    ).execute()

def find_alternative_slots(requested_dt, num_slots=3):
    """Find alternative available slots using FreeBusy API efficiently"""
    alternatives = []
    
    # Generate all potential slots to check
    potential_slots = []
    requested_date = requested_dt.date()
    
    # Same day slots (9 AM to 6 PM, 30-min intervals)
    for hour in range(9, 18):
        for minute in [0, 30]:
            slot_time = requested_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Skip the originally requested slot
            if slot_time != requested_dt:
                potential_slots.append(slot_time)
    
    # Next few days at the same time (up to 7 days)
    for day_offset in range(1, 8):
        next_date = requested_date + timedelta(days=day_offset)
        next_slot = requested_dt.replace(
            year=next_date.year, 
            month=next_date.month, 
            day=next_date.day
        )
        potential_slots.append(next_slot)
    
    # Use FreeBusy API to check all slots at once (in chunks if needed)
    # FreeBusy API can handle larger time ranges efficiently
    if potential_slots:
        # Find the earliest and latest times to create a single query
        earliest_time = min(potential_slots)
        latest_time = max(potential_slots) + timedelta(minutes=30)
        
        freebusy_request = {
            "timeMin": earliest_time.isoformat(),
            "timeMax": latest_time.isoformat(),
            "items": [{"id": CALENDAR_ID}]
        }
        
        freebusy_result = calendar_service.freebusy().query(body=freebusy_request).execute()
        busy_periods = freebusy_result["calendars"][CALENDAR_ID].get("busy", [])
        
        # Convert busy periods to datetime objects for comparison
        busy_times = []
        for busy in busy_periods:
            start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
            busy_times.append((start, end))
        
        # Check each potential slot against busy periods
        for slot_time in potential_slots:
            if len(alternatives) >= num_slots:
                break
                
            slot_end = slot_time + timedelta(minutes=30)
            
            # Check if this slot conflicts with any busy period
            is_available = True
            for busy_start, busy_end in busy_times:
                # Convert to same timezone for comparison
                busy_start = busy_start.astimezone(slot_time.tzinfo)
                busy_end = busy_end.astimezone(slot_time.tzinfo)
                
                # Check for overlap
                if slot_time < busy_end and slot_end > busy_start:
                    is_available = False
                    break
            
            if is_available:
                alternatives.append({
                    "date": slot_time.strftime("%Y-%m-%d %H:%M"),
                    "day": slot_time.strftime("%A"),
                    "formatted": slot_time.strftime("%B %d, %Y at %I:%M %p")
                })
    print(f"Found alternative slots: {alternatives}")
    return alternatives

# ==== ENDPOINTS ====
@app.post("/check-slot")
def check_slot(data: BookingRequest):
    start_dt = datetime.strptime(data.date, "%Y-%m-%d %H:%M")
    start_dt = pytz.timezone("America/Toronto").localize(start_dt)

    # Check business hours first
    if not is_business_hours(start_dt):
        return {
            "available": False,
            "reason": "outside_business_hours",
            "message": "Appointments are only available Monday-Friday, 9 AM to 5:30 PM"
        }
    
    # Check if slot is available
    available = is_slot_available(start_dt)
    if available:
        return {
            "available": True,
            "message": "Slot is available",
            "slot_details": {
                "date": data.date,
                "day": start_dt.strftime("%A"),
                "formatted": start_dt.strftime("%B %d, %Y at %I:%M %p")
            }
        }
    else:
        return {
            "available": False,
            "reason": "slot_busy",
            "message": "Slot is already booked"
        }
    
@app.post('/find-alternatives')
def check_alternatives(data: BookingRequest):
    start_dt = datetime.strptime(data.date, "%Y-%m-%d %H:%M")
    start_dt = pytz.timezone("America/Toronto").localize(start_dt)
    alternatives = find_alternative_slots(start_dt)    
    return {"alternative_slots": alternatives}

@app.post("/book-slot")
def book_slot(data: BookingRequest):
    # Check if slot is available first
    availability_check = check_slot(data)
    
    if availability_check["available"]:
        # Slot is available, create the actual calendar event
        event = create_calendar_event(data)
        log_to_sheets(data)
        return {"success": True, "eventLink": event.get("htmlLink")}
    else:
        # Slot not available - return different responses based on reason
        if availability_check["reason"] == "outside_business_hours":
            return {
                "success": False,
                "message": availability_check["message"],
                "reason": "outside_business_hours"
            }
        else:
            # Slot is busy, get alternatives
            alt = check_alternatives(data)
            return {
                "success": False,
                "message": "Slot not available",
                "reason": "slot_busy",
                "alternative_slots": alt["alternative_slots"]
            }
