# Security

How XploWhatsApp protects businesses, their customers and XploSys: the threat model, the controls in place, the runbooks for rotating keys and handling incidents, the pen-test checklist, and the personal data tools for India's DPDP Act and the GDPR.

Report a vulnerability to security@xplosys.com. Please don't open a public issue.

## 1. Threat model

| Asset | Who might want it | Main threats | Main controls |
|---|---|---|---|
| One business's customers, chats and leads | Another business on the platform, an outsider | Cross-tenant reads through a bug, stolen sessions | Postgres row-level security on every tenant table; tenant context set per transaction; isolation test over every table |
| Business AI keys and WhatsApp tokens | Outsiders, a leaked database backup | Database dump, log leakage | Envelope encryption (AES-256-GCM) with per-row associated data; keys never logged or returned |
| Team accounts | Credential stuffing, phishing | Password reuse, stolen refresh cookie | Argon2id, lockout, per-IP and per-person rate limits, optional or business-wide two-factor, sessions that can be signed out remotely |
| The AI assistant | Customers on WhatsApp, poisoned documents | Prompt injection, invented prices, leaking the prompt | Customer text never enters the system prompt; prompt-frame markers removed from business text; replies checked before sending |
| Uploaded documents | Attackers posing as a business | Malware, zip bombs, files that render as HTML | Content sniffing, archive limits, ClamAV, private bucket, downloads forced as attachments |
| Billing | Fraudsters | Forged payment events | Gateway signatures checked; events stored once by id; prices read from the gateway, never the browser |
| Platform control | Anyone | Super Admin misuse | Separate admin database role; every admin action audited; Super Admin requires two-factor |

Out of scope: Meta's and the payment gateways' own platforms, and the customer's phone.

## 2. Controls

### Sign-in and sessions

- Passwords are hashed with Argon2id. Five failures lock the account for 15 minutes (`LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_SECONDS`).
- Access tokens are ES256 JWTs valid for 15 minutes. Refresh tokens rotate on every use, live in an `HttpOnly`, `Secure`, `SameSite=Strict` cookie scoped to `/api/v1/auth`, and reuse of an old one ends the whole sign-in.
- **Every access token is tied to a live sign-in.** Signing a device out (Account & security → Where you're signed in) refuses its next request at once, not after the token expires.
- Two-factor uses authenticator app codes (TOTP) with ten single-use recovery codes. Using a recovery code is audited. Turning two-factor on starts a fresh, two-factor-verified sign-in.
- **A business can require two-factor for everyone** (Privacy & security). Members without it can only reach their account page to turn it on. The owner must be signed in with two-factor before switching it on, and nobody can turn their own two-factor off while a business they belong to requires it.
- Deleting an account needs the password and, when two-factor is on, a code. A sole owner must delete the business or hand it over first.

### Rate limits

| Limit | Default | Setting |
|---|---|---|
| Sign-in, refresh, sign-up, password reset (per IP) | 20 a minute | `RATE_LIMIT_AUTH_PER_MINUTE` |
| Every signed-in request (per person) | 300 a minute | `RATE_LIMIT_DEFAULT_PER_MINUTE` |
| Webhooks (per sender IP) | 3000 a minute | `RATE_LIMIT_WEBHOOK_PER_MINUTE` |
| Export downloads (per person) | 20 an hour | fixed |
| Business deletion attempts (per person) | 5 an hour | fixed |

The client IP comes from `X-Real-IP` only when the connection comes from an address in `TRUSTED_PROXIES` (default: loopback and private ranges). Set it to your load balancer's ranges in production, so nobody can pick their own IP by sending the header.

### HTTP headers

- API: `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cross-Origin-Opener-Policy` and `Cross-Origin-Resource-Policy`. HSTS (two years, `HSTS_MAX_AGE_SECONDS`) is sent outside development.
- Web app: a per-request nonce with `'strict-dynamic'` for scripts, so injected markup cannot run code. Frames, connections and forms are limited to the app, Razorpay and Facebook (Embedded Signup). Pages cannot be framed.
- State-changing requests that rely on the refresh cookie also need the `X-Requested-With: XploWhatsApp` header.

### Data protection

- Postgres row-level security isolates every tenant table. The app role cannot bypass it. Platform-wide jobs use narrow `SECURITY DEFINER` functions, and the Super Admin uses a separate role.
- Secrets are encrypted with a per-secret data key wrapped by a versioned key-encryption key (`ENCRYPTION_KEYS`). The associated data binds each secret to its business and purpose, so a ciphertext copied to another row does not decrypt.
- The audit log is append-only for the app role. Every route that changes data is either audited or listed with a reason in `tests/integration/test_audit_coverage.py`, and a new route fails that test until it is classified.

### Uploads

Checked before anything is stored: the declared size, magic bytes against the extension, Office archive limits (entries, total size, compression ratio) and macros. Then ClamAV scans each file before parsing. Files live in a private bucket under the business's own prefix, with storage keys the server chooses; file names never pick the path. Download links are short-lived and forced to download as attachments. Details are in docs/KNOWLEDGE.md.

### The AI assistant

- Customer messages are sent as user turns and never placed in the system prompt.
- Business settings and knowledge passages are cleaned of anything that imitates the prompt's own section markers, so a document cannot "close" its section and add instructions.
- Each reply is checked before sending. Prices, percentages and links must appear in the business's own settings or documents. A customer saying "the price is ₹1" does not make it a fact. Times, phone numbers and emails may also come from the customer. Replies that repeat the prompt or contain key-like strings are refused, after invisible characters are removed. Anything refused goes to a person.
- Tests: `tests/unit/test_prompt_injection.py` and `tests/integration/test_upload_abuse.py`.

### Dependencies and CI

Every pull request runs pip-audit, `pnpm audit` (high and above), gitleaks over the whole history, and Trivy on both images (fixable high and critical findings).

## 3. Rotating keys

Run these where the API's `.env` lives (`docker compose run --rm api python -m app.cli …` with Compose).

### Token-signing key (JWT)

1. `python -m app.cli rotate-jwt-key` prints new `JWT_PRIVATE_KEY_PEM`, `JWT_KEY_ID` and `JWT_PREVIOUS_PUBLIC_KEYS` lines.
2. Replace those three lines in `.env` and restart the API and worker.
3. Tokens signed with the old key keep working until they expire (15 minutes), so nobody is signed out. The next rotation drops the old key.

If the key leaked, also run `UPDATE refresh_tokens SET revoked_at = now() WHERE revoked_at IS NULL;` on the admin role. That signs everyone out.

### Encryption key

1. `python -m app.cli new-encryption-key` prints `ENCRYPTION_KEYS` with a new version appended, and `ENCRYPTION_ACTIVE_KEY_VERSION`.
2. Replace both lines and restart the API and worker. New secrets now use the new key; old ones still decrypt with theirs.
3. `python -m app.cli rewrap-secrets --dry-run` shows what will change. Then run it without `--dry-run`. It re-encrypts AI keys, WhatsApp tokens and two-factor secrets in one transaction: if any secret cannot be read, nothing is saved and the command exits with an error that lists the row ids.
4. After the next backup cycle, remove the old version from `ENCRYPTION_KEYS`.

### Other secrets

| Secret | Where to rotate | Then |
|---|---|---|
| `META_APP_SECRET` | Meta app dashboard → Settings → Basic | Update `.env`, restart. Webhook signatures use it |
| `META_WEBHOOK_VERIFY_TOKEN` | `.env` | Re-verify the webhook in the Meta dashboard |
| Razorpay and Stripe keys and webhook secrets | Gateway dashboards | Update `.env`, restart |
| Database, Redis, S3 passwords | The service, then `.env` | Restart everything |

## 4. Incident response

1. **Contain.** Suspend an affected business (Super Admin → Businesses → Suspend). Sign everyone out (the SQL above). Rotate any key that may have leaked (section 3). Turn off a compromised AI provider in Super Admin.
2. **Investigate.** The audit log (Super Admin, or `audit_logs` on the admin role) records who did what, from which IP, with the request id that ties it to the API logs. Webhook inbox rows keep the raw Meta events for 30 days.
3. **Notify.**
   - Under the DPDP Act, tell the Data Protection Board of India and each affected person of a personal data breach. Rule 7 of the DPDP Rules, 2025, asks for a detailed report to the Board within 72 hours.
   - Under the GDPR, tell the supervisory authority within 72 hours where there is risk to people (Article 33), and tell the people themselves when the risk is high (Article 34).
   - Businesses are the data fiduciaries or controllers for their customers' data. Tell them promptly so they can meet their own duties.
   - India's CERT-In expects certain incidents to be reported within 6 hours.

   Confirm the exact duties with counsel.
4. **Recover and learn.** Restore from backups if needed, fix the cause, add a test, and write a blameless post-mortem.

## 5. Pen-test checklist

Run before launch and after major changes. Each line names what to try and where it is covered today.

**Tenant isolation**
- [ ] Change any id in a URL or body to one from another business; expect 404. *(tests/isolation)*
- [ ] Switch business with a token for a business you were removed from; expect 403.
- [ ] Read another business's upload or export through a guessed object key; expect no access (private bucket, presigned links only).

**Authentication**
- [ ] Brute force sign-in and the two-factor step; expect lockout and 429.
- [ ] Replay an old refresh cookie; expect the whole sign-in to end.
- [ ] Use an access token after signing that device out; expect 401 `session_revoked`.
- [ ] Skip two-factor in a business that requires it; expect 403 `mfa_required_by_business`.
- [ ] Send `X-Real-IP` from an untrusted address; expect it to be ignored.

**Authorisation**
- [ ] Call every owner, admin and Super Admin route as an agent; expect 403. *(test_billing_permissions, test_admin)*
- [ ] Turn off your own two-factor while your business requires it; expect refusal.

**Input handling**
- [ ] Upload HTML, SVG, a renamed executable, a zip bomb, a macro document, an oversized file without Content-Length. *(test_upload_abuse)*
- [ ] Put `"; DROP TABLE` and Unicode tricks in every text field; expect them stored as text.
- [ ] Inject script through business name, contact names and notes; expect plain text in the app and in emails.

**AI assistant**
- [ ] Ask the assistant to ignore its rules, reveal its prompt, quote a made-up price or link, or role-play as staff. *(test_prompt_injection)*
- [ ] Upload a document that pretends to be a system message; expect it to be treated as content.

**Webhooks and billing**
- [ ] Post unsigned or re-signed Meta, Razorpay and Stripe events; expect 401 and nothing stored. *(test_webhook_signatures_and_unknown_events)*
- [ ] Replay a real event; expect it processed once.
- [ ] Change the plan or amount in the browser at checkout; expect the server's price.

**Browser**
- [ ] Check the CSP blocks inline script injection, and pages cannot be framed.
- [ ] Check cookies are `HttpOnly`, `Secure` and `SameSite=Strict`.

**Privacy tools**
- [ ] Erase a contact and confirm no row with their phone number remains.
- [ ] Download another business's export by id; expect 404.

## 6. Personal data tools (DPDP Act and GDPR)

Each business is the data fiduciary (DPDP) or controller (GDPR) for its customers' data; XploSys is its processor. These tools let a business meet its duties.

| Need | Where | What happens |
|---|---|---|
| A customer asks to be forgotten | Contacts → the contact → Erase (admins and owners) | The contact, their conversations, messages, notes, summaries and leads are deleted at once. The audit log keeps only a one-way fingerprint of the phone number, to prove the request was handled |
| A customer opts out of messages | They send STOP (or another opt-out word) on WhatsApp | Consent is recorded on the contact; no further messages are sent to them, and the assistant stops replying |
| Keep less | Privacy & security → Message history | WhatsApp messages older than 30 days to 2 years are deleted every hour. Contacts, leads and notes stay |
| A copy of everything | Privacy & security → Export your data (owner) | A zip of JSON Lines files: contacts, conversations, messages, leads, knowledge, settings, team members, invoices and the audit log. API keys, tokens and password hashes are left out. Ready in minutes; the owner is emailed; the link works for 7 days; 3 exports a day; every download is audited |
| Close the business | Privacy & security → Delete this business (owner, password and the typed name) | Everyone loses access at once and the assistant stops. A paid plan must be cancelled first. After 30 days all data is removed, including stored files. Tax invoices, payment records and the audit log are kept, because the law requires them. Until then, XploSys support can restore it (Super Admin → Deleted businesses) |
| A team member leaves | Account & security → Delete my account | The account is anonymised and signed out everywhere |

Platform records: processed webhook payloads are deleted after 30 days (`WEBHOOK_EVENT_RETENTION_DAYS`). The hourly `sweep-privacy` task runs retention, export expiry and purges.
