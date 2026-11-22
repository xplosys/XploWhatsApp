# Appointments (Phase 11)

The WhatsApp assistant books, moves and cancels appointments by itself. Staff see and manage
everything on the **Appointments** page.

## Setting it up (business admin)

1. **Appointments → Doctors & services → Add doctor or service.** Enter the name, department,
   details the assistant may use, appointment length, how many patients per time (1 for a doctor)
   and the weekly hours, e.g. `10:00-13:00, 17:00-20:00` (24-hour clock, empty = closed).
2. **Holidays and leave** (same tab): whole days nobody, or one doctor, can be booked.
3. **Booking settings → The assistant books appointments.** Choose how far ahead (default 14 days),
   the earliest booking (minimum notice), and the limit of upcoming bookings per WhatsApp number.
   An optional note is added to every confirmation.
4. **Reminders** (optional): pick how many hours before, and an approved *Utility* template. Body
   variables are filled in order: `{{1}}` patient name, `{{2}}` doctor or service, `{{3}}` date,
   `{{4}}` time, `{{5}}` reference, `{{6}}` business name. Customers who wrote in the last 24 hours get
   a plain text reminder; others need the template.

The assistant's automatic replies must be on (AI assistant page).

## How the assistant books (ADR 064)

The model never touches the calendar. For each reply the backend builds a booking section in the
prompt: today's date and time in the business's time zone, the bookable doctors and services with
short codes (`R1`, `R2`…), their free start times for the booking window (compressed into ranges,
capped at 5,000 characters), and this customer's upcoming appointments with references.

The model answers with `booking.action` = `none | book | cancel | reschedule` plus code, date
(`YYYY-MM-DD`), time (`HH:MM`), patient name, reference and notes. It is told to repeat the details
and get a "yes" before acting. The reply validator also accepts the listed free times as grounding,
so the assistant can mention them but never invent one.

When an action comes back, `appointments.service.apply_ai_action` checks everything again in the
reply transaction: the doctor is active, the time is one of their slot starts, not a day off, not
too soon or too far, there is still room (the doctor's row is locked, so two customers can't take
the last place), the number is under its limit, and cancel/move only touch this number's own
appointments. On success the customer gets the model's one-line reply plus a confirmation block
written by the server (who, when, patient, **reference**, the business's note). On failure the
server writes the reason and offers free times that day or the next day with any. Booking and the
WhatsApp message share a savepoint: if the message can't be queued, nothing is booked.

## Staff

Agents and above can view the calendar, book (staff may book outside hours, and over a full slot
with "Book even if full"), move, mark completed or no-show, and cancel. Cancelling or moving tells the
customer on WhatsApp when they wrote within 24 hours; otherwise the page says to call them.
Admins and owners manage doctors, hours, leave and settings (`appointments:configure`).

## Reminders

Beat runs `app.workers.tasks.appointments.send_reminders` every 5 minutes. A definer function
finds confirmed appointments whose reminder window has started, in businesses with reminders on,
skipping appointments booked inside the window (they just got the confirmation). Each reminder is
sent once; the calendar shows sent, skipped (and why) or failed.

## Limits

- Confirmation and error texts written by the server are in English; the model's own sentence above
  them follows the customer's language.
- Time off is whole days. Weekly hours are the same every week.
- Free times are shown for up to 90 days; with many doctors the prompt shows the earliest dates
  that fit and tells the model later dates go to the team.
