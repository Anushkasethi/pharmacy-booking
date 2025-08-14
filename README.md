# Community Pharmacy Booking Assistant

A voice-ready assistant built on **RetellAI + FastAPI**, integrated with **Google Calendar** (availability & events) and **Google Sheets** (audit log). It supports **booking**, **rescheduling**, and **cancellation** using natural-language dates/times.

---
**Repo:** <https://github.com/Anushkasethi/pharmacy-booking>  
---

## 1. Architecture & Key Components

1. **Retell Agent (chat)**
   - Drives the dialogue, stores dynamic variables, and calls custom functions.

2. **FastAPI service (custom functions)**
   - `POST /find-slots` — natural-language availability.
   - `POST /create-event` — create Calendar event + log to Sheets.
   - `POST /reschedule-booking` — move an existing booking.
   - `POST /cancel-booking` — cancel Calendar event, update Sheets.

3. **Google Calendar**
   - Free/busy checks and event lifecycle.
   - Idempotency via `extendedProperties.private.bookingRef`.

4. **Google Sheets**
   - Human-readable audit trail (status: `confirmed`, `rescheduled`, `cancelled`).

---

## 2. Data Flow (Sequence)

```mermaid
sequenceDiagram
  participant U as User
  participant R as Retell Agent
  participant F as FastAPI
  participant GC as Google Calendar
  participant GS as Google Sheets

  U->>R: "I’d like to book a flu shot tomorrow at 4pm"
  R->>F: find-slots {appointment_type, preferred_datetime_text}
  F->>GC: freeBusy query
  GC-->>F: availability
  F-->>R: {slots:[...], reason}
  R->>U: Offer top 3 slots, ask 1/2/3
  U->>R: "2"
  R->>F: create-event {slot, patient, appointment_type, idempotency_key}
  F->>GC: insert event (bookingRef in extendedProperties)
  F->>GS: append row (status=confirmed)
  F-->>R: {success, booking_ref, confirm_speakable}
  R-->>U: Confirmation (weekday + date + time)

---

## 3. Conversation Flow

```mermaid
flowchart TD
  A[Greeting] --> B[Collect appointment_type]
  B --> C[Collect preferred_datetime_text]
  C --> D{find-slots}
  D -->|slots > 0| E[Offer 3 options]
  D -->|none| C[Clarify or propose next windows]
  E --> F[Collect patient name + contact]
  F --> G{create-event}
  G -->|success| H[Confirm with booking_ref]
  G -->|error| I[Apologize + staff follow-up]
  H --> J{Reschedule or Cancel?}
  J -->|Reschedule| K[Verify + new time] --> L{reschedule-booking}
  J -->|Cancel| M[Verify identity] --> N{cancel-booking}

---

## 4. Tools & Libraries (Why)

- **RetellAI** — chat that can graduate to voice; function calling + dynamic variables.  
- **FastAPI + Pydantic** — fast iteration and strict payload validation.  
- **Google Calendar API** — real availability and robust event operations.  
- **Google Sheets API** — simple, transparent audit log for staff.  
- **python-dateutil, pytz** — reliable parsing and timezone handling.

---

## 5. Error-Handling Approach

- **Validation:** Pydantic returns
  ```json
  {"success": false, "error": "validation_error", "reason": "..."}
  ```
- **Calendar/Sheets exceptions:** caught and surfaced; agent replies with a short apology and offers staff follow-up.  
- **Busy slots:** up to three alternatives returned; agent asks the user to pick **1/2/3**.  
- **Idempotency:** `create-event` with the same details returns the existing event (no duplicates).  
- **Ref robustness:** `normalize_booking_ref` (trim, uppercase, normalize Unicode dashes) plus fallback to **name+contact** lookup.

---

## 6. Extras / Delighters

- Idempotent bookings via **bookingRef** stored on the Calendar event.  
- Clear **Sheet audit trail** with timestamps and statuses (`confirmed`, `rescheduled`, `cancelled`).  
- Robust **reschedule** via reference normalization and patient fallback.

---

## 7. If I Had More Time…

1. Clinic rules & closures (holiday calendar; age/consent; vaccine inventory). Give dropdown options for patients to select.
2. SMS/email confirmations and reminders.  
3. Staff dashboard (review queue, overrides).  
4. Duplicate booking detection and simple patient profiles.  
5. PHIPA/HIPAA hardening and data-retention policies.
6. Durable orchestration (Temporal / Cloud Tasks / Durable Functions):
  - Run booking/reschedule/cancel as **workflows with retries and compensation**.
  - If Google APIs are down, the agent still chats, **queues the job**, gives the user a ticket/ETA, and **auto-retries** until success. On success/failure, notify via SMS/email.

