# Meta WhatsApp integration: requirements and approval dependencies

**Last checked against Meta documentation:** 2026-11-12. Meta changes this area often. Every value below that comes from Meta (API version, Embedded Signup version, permission names, webhook fields) must be re-checked against the official docs at the start of Phase 2. Nothing here should be treated as permanent.

Primary sources:
- Embedded Signup overview and implementation: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation/
- Embedded Signup versions: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/versions/
- Onboarding customers as a Tech Provider: https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider
- WhatsApp Business Platform changelog: https://developers.facebook.com/documentation/business-messaging/whatsapp/changelog/

---

## 1. Current state of the platform (as of November 2026)

| Item | Current state | What we do |
|---|---|---|
| Graph API version | Meta examples use `v25.0`; some onboarding examples still show `v21.0` | `META_GRAPH_API_VERSION` env var, default to the latest stable at Phase 2 start |
| Embedded Signup | v2 and v3 deprecated 15 Oct 2026 (requests now served as v4). v4 is the migration target. v5 introduced 20 Oct 2026 | Build on v4; evaluate v5 at Phase 2 start |
| ESU v4 configuration | Products (Cloud API, Marketing Messages, Click-to-WhatsApp Ads, Conversions API) are selected in the Facebook Login for Business configuration. `extras` is `{ setup: {} }` | Products chosen in Meta App Dashboard; frontend passes only `config_id` |
| ESU completion | Users may finish with a verified number, an unverified number, or no number | Onboarding must handle a missing `phone_number_id` and let the tenant add a number later |
| Exchangeable code | Returned in `authResponse.code`, roughly 30 second lifetime | Frontend posts it to the backend immediately; backend exchanges at once |
| Pricing | Per-message pricing since 1 July 2025 (marketing, utility, authentication, service) | Versioned rate table, no hard-coded prices |
| AI chatbot policy | From 15 Jan 2026, general-purpose AI chatbots (where the AI is the product) are prohibited on the Business API. Business-specific assistants are allowed | Assistants are scoped to the tenant's business; see ARCHITECTURE.md §1.4 |

---

## 2. What Xplosys must set up with Meta (owner: Xplosys, not code)

These run in parallel with development and are the main schedule risk. Start them during Phase 1.

1. **Meta Business Portfolio** (business.facebook.com) for Xplosys, with admins who use 2FA.
2. **Business verification** of Xplosys in Security Center (legal name, address, registration documents, domain or phone verification). Required before advanced access and Tech Provider status.
3. **Meta Developer account** and a **Developer App of type Business**, owned by the Xplosys portfolio. Add the **WhatsApp** product.
4. **App basics:** app icon, privacy policy URL, terms of service URL, data deletion instructions or callback URL, category, contact email. These pages must be live on xplowhatsapp.com.
5. **Domains:** add `app.xplowhatsapp.com` (and the dev domain) to **Allowed Domains** for the JS SDK and to **Valid OAuth Redirect URIs**. HTTPS is mandatory, otherwise Embedded Signup does not return session data.
6. **Facebook Login for Business configuration** for WhatsApp Embedded Signup, which gives the `config_id`. Select products in the configuration (v4 behaviour).
7. **Webhooks:** callback `https://api.xplowhatsapp.com/api/v1/webhooks/whatsapp`, verify token from our env, subscribe to WhatsApp Business Account fields including `messages`, `account_update`, `message_template_status_update`, `phone_number_quality_update`, and review the list for others we need (for example `account_review_update`, `business_capability_update`, `template_category_update`).
8. **Tech Provider onboarding** (WhatsApp → become a Tech Provider in the App Dashboard). This includes:
   - **App Review** for `whatsapp_business_management` and `whatsapp_business_messaging` with **Advanced Access**. Meta needs a screencast of a real business connecting through Embedded Signup and a message being sent and received, so a working Phase 2 build is needed first.
   - **Access verification** for Tech Providers, if Meta still requires it at that time.
   - Any further permission (for example `business_management`) only if a feature truly needs it. We will not request permissions we do not use.
9. **System user** in the Xplosys portfolio for platform-level operations (template sync for our own test WABA, debugging). Customer tokens come from Embedded Signup, not from this system user.

## 3. Before and after approval

| Capability | Before App Review / Tech Provider approval | After approval |
|---|---|---|
| Cloud API send/receive | Yes, with Meta's **test WABA and test number** and up to a handful of registered recipient numbers | Yes, for onboarded customers |
| Webhooks | Yes, for our own WABA | Yes, for every customer WABA we subscribe to |
| Embedded Signup | Only for people with a role on the app (admins, developers, testers) | Any business |
| Production customers | No | Yes |

So Phase 2 is fully buildable and testable before approval. Going live with outside businesses depends on approval.

## 4. Server-side sequence after Embedded Signup (Tech Provider)

All calls are server to server. Version shown as `{v}` = `META_GRAPH_API_VERSION`.

1. **Exchange the code:** `GET https://graph.facebook.com/{v}/oauth/access_token?client_id={APP_ID}&client_secret={APP_SECRET}&code={CODE}`. Returns a business integration system user access token for the customer.
2. **Validate:** check token and granted scopes (`GET /debug_token`), confirm the WABA ID and phone number ID from the browser event are actually accessible with this token (`GET /{waba_id}`, `GET /{waba_id}/phone_numbers`). Never trust the browser's IDs alone.
3. **Subscribe our app to the WABA:** `POST /{v}/{waba_id}/subscribed_apps` with the business token.
4. **Register the number for Cloud API:** `POST /{v}/{phone_number_id}/register` with `{"messaging_product": "whatsapp", "pin": "<6 digits>"}`. We generate the PIN, store it encrypted, and show the owner how to see it (it becomes the number's two-step verification PIN).
5. **Persist** account, number, encrypted token, audit log entry. Mark health `connected`.
6. **Customer adds a payment method** in WhatsApp Manager (not an API call). Our UI shows this as a required step with a link, and the account health check reports it if messaging fails for billing reasons.
7. Reconcile with the `account_update` webhook that Meta sends on completion.

Tech Providers do not share a credit line. That is a Solution Partner step and is not part of our launch model.

## 5. Ongoing operations

- **Health checks** (Celery beat, daily and on demand): token still valid (`debug_token`), WABA still shared, number status, quality rating and messaging limit, payment method issues surfaced through errors.
- **Reconnect flow:** if a token is revoked or access is removed, mark the account `needs_reconnect`, pause automatic replies, notify the owner, and offer Embedded Signup again for the same WABA.
- **Disconnect:** `DELETE /whatsapp/accounts/{id}` unsubscribes the app from the WABA (`DELETE /{waba_id}/subscribed_apps`), revokes local credentials, keeps history per retention settings.
- **Templates:** sync from `GET /{waba_id}/message_templates`, track status changes through `message_template_status_update`, create/edit from the dashboard in Phase 5/6.
- **Webhook signature:** `X-Hub-Signature-256: sha256=<hex>` over the raw body using the app secret.

## 6. Policy constraints we enforce in code

- Customer-service window: free-form messages only within 24 hours of the customer's last message. Outside it, approved templates only.
- Opt-in for business-initiated (especially marketing) messages, and honour opt-out.
- Template category rules (marketing, utility, authentication) and Meta's recategorisation.
- Quality rating and messaging limits per number.
- Business-scoped AI only (no general-purpose assistant behaviour), with a path to a human.
- No unofficial APIs, WhatsApp Web automation or scraping.
