# AI providers and the WhatsApp assistant

How XploWhatsApp talks to Google Gemini, OpenAI and DeepSeek, how a reply is produced and checked, and what XploSys and each business need to set up. Provider APIs were re-checked against their official documentation on 2026-11-12.

## 1. What the platform calls

| Provider | Generate | List models | Key sent as | Structured output |
|---|---|---|---|---|
| OpenAI | `POST /v1/responses` with `instructions`, `input`, `max_output_tokens`, `store: false` | `GET /v1/models` | `Authorization: Bearer` | `text.format` = `json_schema`, `strict: true` |
| Google Gemini | `POST /v1beta/models/{model}:generateContent` with `systemInstruction`, `contents`, `generationConfig` | `GET /v1beta/models` (paged; only models that support `generateContent` are kept) | `x-goog-api-key` header, never in the URL | `responseMimeType: application/json` with `responseSchema` |
| DeepSeek | `POST /chat/completions` (OpenAI-compatible) | `GET /models` | `Authorization: Bearer` | `response_format: json_object` (the prompt says "JSON", as DeepSeek requires) |

Base URLs come from `OPENAI_BASE_URL`, `GEMINI_BASE_URL` and `DEEPSEEK_BASE_URL` (defaults are the official hosts). Every call has a timeout (`AI_HTTP_TIMEOUT_SECONDS`, default 30) and an output limit (`AI_MAX_OUTPUT_TOKENS`, default 2048).

Errors are mapped to one taxonomy. Timeouts, 429 and 5xx are retryable (`AI_ATTEMPTS_PER_PROVIDER`, default 2, with a short backoff). A bad key (401/403, or Gemini's 400 "API key not valid"), unpaid balance (402), blocked content, a truncated answer and a bad request are not retried; the next model in the chain is tried instead.

Token usage is read from each response (input, cached input, output; Gemini's thinking tokens count as output) and stored per call.

## 2. Model catalogue and prices (Super Admin)

No model IDs or prices are in the code. The Super Admin console (Super Admin page, "AI model catalogue") manages them:

1. **Sync from provider** lists the provider's current models with XploSys's platform key. New models arrive **inactive**.
2. **Prices**: add input, output and optional cached-input prices per million tokens, with a currency and an effective date. Prices are never edited; a new entry takes over from its date, so earlier cost estimates stay reproducible.
3. **Activate** a model to let businesses choose it.

Calls to a model with no price are recorded with `cost_source = unpriced` and shown as such on the usage card. The catalogue tables are read-only for the application's database role; only Super Admin endpoints (audited) write them.

## 3. Keys

Each business chooses per provider:

- **Its own key.** Stored with envelope encryption, bound to the business and provider (AAD `ai_key:{tenant}:{provider}`). Only the last four characters are ever shown. "Check key" lists the provider's models to prove the key works.
- **XploSys's key** (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY` in the server environment), offered only when the key is set and the business's plan has `platform_ai_keys` > 0. The trial plan allows it.

## 4. How a reply is made

1. An inbound text, button, list, location, contact or media message schedules a reply job after a short debounce (`AI_REPLY_DEBOUNCE_SECONDS`, default 2). Reactions, unsupported and system messages, and STOP/START keywords do not.
2. The job stops if a newer message arrived (the newer job will answer both), if this message was already answered, or if AI is off for the business or the conversation, the customer opted out, the 24-hour window is closed, or the plan's `monthly_ai_messages` is used up. A Redis lock per conversation stops two jobs running at once.
3. The prompt holds the business's own information only (description, facts, opening hours, tone, language rule) and the recent conversation (`AI_HISTORY_MESSAGES`, `AI_HISTORY_MAX_CHARS`). The assistant is told to answer only about this business, which keeps it inside Meta's rule against general-purpose chatbots.
4. The main model is called, then up to three fallbacks. The database transaction is closed while providers think.
5. The answer must be JSON `{reply, needs_human, out_of_scope}` and pass the validator: length limit, no secrets or prompt text, and **grounding**: every price, percentage, time, phone number, email and link must appear in the business information or in the customer's own messages. An answer that fails goes to the next model.
6. If the model asks for a person, or no model gives a valid answer, the business's handover message is sent, AI is switched off for that conversation and it is marked "Needs a person". A team member turns AI back on from the conversation once it's handled.
7. Usage is recorded (`ai_call` per provider request with tokens and estimated cost, `ai_reply` per message sent), and the reply is queued through the normal WhatsApp outbox with its provenance (provider, model, reason, attempts), which shows on the message.

## 5. What a business does

On the **AI assistant** page (owners and admins):

1. Turn on a provider with its own key or XploSys's key, and check the key.
2. Fill in the assistant settings: about the business, the facts it may use, opening hours, tone, language, reply length, the handover message, the main model and fallbacks. Every save is a new version; any earlier version can be made active again.
3. Try it in the preview chat. Nothing is sent on WhatsApp, but the provider is called and the call is metered.
4. Turn on automatic replies.

## 6. Not in this phase

- Knowledge documents and retrieval (Phase 4). Until then the assistant only knows what is typed into its settings.
- Images, audio and video are not interpreted; the assistant sees a label such as "[image]" and the caption.
- Live inbox and assignment for handed-over conversations (Phase 5).
