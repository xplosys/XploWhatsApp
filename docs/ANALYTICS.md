# Overview and analytics

What the Overview and Analytics pages show and how each figure is worked out. Code: `backend/app/modules/analytics/` and `frontend/src/app/(dashboard)/{dashboard,analytics}/`, charts in `frontend/src/components/charts/Charts.tsx`.

## 1. Overview (everyone)

- **Inbox tiles:** needs a person, waiting for a reply, unassigned, assigned to you. Each opens the inbox on that tab (`/conversations?view=...`). Refreshes every 30 seconds.
- **Needs a person:** the five newest conversations the assistant handed over, with the reason.
- **Get set up:** connect a WhatsApp number, publish the assistant, add a knowledge document, invite a teammate, verify email, turn on two-factor. A step links to its page only for people allowed to do it. The card disappears when everything is done.
- **Last 7 days:** the four headline figures below, for supervisors and above.
- **Plan:** for people who can see billing.

## 2. Analytics (supervisors, admins, owners)

One request, `GET /analytics/overview?days=7|30|90`, fills the page. Periods are whole days in the business's time zone (Business settings), today included; the previous period is the same length just before it.

| Figure | How it is counted |
|---|---|
| Customer messages | Inbound messages, reactions excluded |
| Handled by the assistant alone | Of the conversations the assistant replied in during the period, the share it did not hand to a person during the period |
| Time to a person after handover | Median time from the assistant's handover to the first message from the team, for handovers in the period (unanswered ones are counted, not timed) |
| Leads won | Leads moved to won in the period, with their value per currency |
| Messages per day | Inbound, assistant and team messages per local day |
| When customers write | Inbound messages by local weekday and hour |
| Why the assistant handed over | Handovers started by the assistant, by reason (`blocked:*` grouped) |
| First reply to new conversations | For conversations started in the period, the median and 90th percentile from the customer's first message to the first reply, split by who replied first |
| Message delivery | Outbound messages by their latest WhatsApp status, the five most common error codes, and the messages Meta marked billable by category |
| AI usage | AI requests by purpose and their estimated cost per currency. Only for people who can configure AI or see billing |
| Team | Per person: messages sent, conversations replied in, handovers they handled (answered and median time), leads won |

Every chart has a table view. Hovering or focusing a chart shows the values at that point.

WhatsApp messaging charges are billed by Meta to each business directly. The page counts billable messages by category; it does not price them, because no rate card is stored (prices change and differ by country). See Phase 7 for billing.

## 3. Setup progress

`GET /analytics/setup` (any member) returns yes/no and counts only: a connected number, a published assistant and whether auto-reply is on, knowledge documents with an active version, active members.

## 4. Notes

- Figures are computed at request time from the operational tables, inside the business's row-level security. There are no stored aggregates yet; ADR-036 says when to add them.
- Chart colours are `--color-series-1..3` in `globals.css`, checked for colour-blind separation and contrast in light and dark mode.
