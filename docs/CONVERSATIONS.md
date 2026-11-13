# Conversations, contacts and leads

How a business's team works its WhatsApp inbox alongside the AI assistant. Code: `backend/app/modules/conversations/` (inbox, contacts, summaries, unanswered questions) and `backend/app/modules/leads/`.

## 1. Inbox

| Tab | Shows |
|---|---|
| Open | Every open conversation |
| Mine | Open conversations assigned to you |
| Unassigned | Open conversations nobody has picked up |
| Needs a person | The assistant handed over and nobody has taken it yet |
| Waiting for reply | The customer wrote last and nobody (person or assistant) has answered |
| Closed | Closed conversations; a new customer message reopens them |

Each tab shows its count (`GET /conversations/counts`). The search box matches a contact's name, WhatsApp name, phone or username, or any message text (the preview then shows the newest matching message). The tag filter limits the list to contacts with that tag. The list and the open conversation refresh every 15 and 5 seconds.

On a phone the list and the open conversation take turns, with a link back to the list.

## 2. Who answers: the assistant or a person

| Action | Who | Effect |
|---|---|---|
| Assistant hands over (asks for a person, no checked answer, or reply blocked) | Assistant | AI replies off, marked "Needs a person", handover session opened, question recorded, summary written (if enabled) |
| Take over from AI / I'm handling this | Anyone who can reply | AI replies off, "Needs a person" cleared, assigned to you if unassigned, you are recorded on the session |
| Hand back to AI | Anyone who can reply | Session closed, AI replies on; the assistant answers the customer's next message |
| Close / Reopen | Anyone who can reply | Status only; closing clears the unread count |

AI replies stay off until someone hands the conversation back; there is no timer. The "Handled by people" card lists past sessions (who, why, when it went back).

## 3. Assignment

| Role | Can |
|---|---|
| Agent | Assign an unassigned conversation to themselves, unassign themselves |
| Supervisor, admin, owner | Assign or unassign anyone who is an active member |

Removing a member from the business returns their conversations, open takeovers and leads to the shared queue. Every assignment change is in the audit log.

## 4. Notes, contacts and tags

- **Internal notes** are visible to the team only and never sent. The author can edit a note; the author or a supervisor and above can delete it (audited).
- **Contacts** keep the WhatsApp profile name and number as WhatsApp sends them. The business can set its own name for the contact, a language code (`en`, `hi-IN`) and up to 20 tags (lowercase; letters, numbers, spaces and `- _ . & + /`, up to 40 characters). Marketing consent is recorded with where it came from.
- The Contacts page lists contacts with search, tag and consent filters, and shows each contact's conversations (one per business number) and leads.

## 5. AI summaries

Written by the worker with the assistant's main model and fallbacks from the last 80 messages, in up to five short sentences: what the customer wants, what was already said or promised (prices and dates exactly as written), and what is still open. The transcript is passed as data, not instructions.

- Written automatically when the assistant hands over, unless "Write a short summary for your team when the assistant hands over" is off on the AI assistant page.
- "Summarise" or "Refresh" in the conversation asks for a new one. The card shows how many messages arrived since.
- Each summary is an AI call recorded in usage with purpose `summary` and its cost.
- If no assistant is set up, or every model fails, the card shows why. A job lost by the queue is sent again by a sweeper after 5 minutes.

## 6. Questions the assistant couldn't answer

Every handover by the assistant keeps the customer's question, the reason (`needs_human`, `all_failed` or `blocked:<code>`) and how many knowledge passages were found. They are listed on the Knowledge page. Owners and admins mark them resolved once the answer is in a document or note, or dismiss them. Everyone with knowledge access can read them.

## 7. Leads

A lead is a sales opportunity with a contact: title, status (new, contacted, qualified, won, lost), value and currency, owner, and the conversation it came from (source `whatsapp`) or none (source `manual`).

- Create one from a conversation's side panel or with "New lead" on the Leads page.
- The currency defaults to the business's country (for example INR for India, GBP for the UK, USD for the US); any three-letter code can be stored.
- Owners follow the assignment rule above: agents take unowned leads or let go of their own.
- Every change (status, value, title, owner) and note is written to the lead's history, which the app cannot edit or delete. Supervisors and above can delete a lead and its history; marking it lost keeps the record.
- The Leads page shows open leads, yours, unowned, and the open pipeline value per currency.

## 8. API

| Method and path | Permission |
|---|---|
| `GET /conversations?view=&q=&tag=&assigned_to=&number_id=`, `GET /conversations/counts`, `GET /conversations/{id}` | conversations:read |
| `PUT /conversations/{id}/assignee`, `POST .../takeover`, `POST .../release`, `PUT .../status`, `PUT .../ai` | conversations:reply (assigning others: conversations:assign) |
| `GET /conversations/{id}/handovers`, `GET .../notes`, `GET .../summary` | conversations:read |
| `POST .../notes`, `PATCH`/`DELETE .../notes/{note}`, `POST .../summary` | conversations:reply |
| `GET /contacts`, `GET /contacts/tags`, `GET /contacts/{id}` | conversations:read |
| `PATCH /contacts/{id}`, `PUT /contacts/{id}/consent` | contacts:manage |
| `GET /knowledge/unanswered`, `PATCH /knowledge/unanswered/{id}` | knowledge:read, knowledge:manage |
| `GET /leads`, `GET /leads/summary`, `POST /leads`, `GET`/`PATCH /leads/{id}`, `POST /leads/{id}/notes` | leads:manage |
| `DELETE /leads/{id}` | conversations:assign |

All tables are tenant-scoped with forced row-level security.
