# Knowledge base

How businesses give the AI assistant documents, how those are checked, read and searched, and what to configure. Code: `backend/app/modules/knowledge/` and `backend/app/modules/storage/`.

## 1. What a business can add

| Source | Formats | Notes |
|---|---|---|
| File upload | PDF with selectable text, Word `.docx`, Excel `.xlsx`, `.csv`, `.txt`, `.md` | Up to `KNOWLEDGE_MAX_FILE_MB` (20 MB default) |
| Web page | `text/html`, `text/plain`, `application/pdf` | Public `http`/`https` on ports 80 and 443 only, up to `KNOWLEDGE_URL_MAX_MB` (5 MB) |
| Note | Plain text or Markdown typed in the app | Edited in place; each save is a new version |

Not supported, with a clear message: scanned PDFs (no OCR yet), password-protected PDFs, macro-enabled or legacy binary Office files (`.xlsm`, `.doc`, `.xls`), images, and text files in legacy code pages (save as UTF-8).

Plan limits: `max_knowledge_documents` and `max_storage_mb` (trial: 20 documents, 200 MB). Old versions count toward storage until deleted.

## 2. Pipeline

1. **API request.** Multipart bodies must declare a `Content-Length` within the limit or are refused with 413 before they are read. The file's first bytes must match its extension; Office files are opened as zip archives with limits on entries (5,000), total uncompressed size (250 MB) and compression ratio (200), and macros are refused. The plan quota is checked. The file goes to the private bucket under `tenants/<tenant>/documents/<document>/`, and a version row is committed as `queued`. The job is dispatched after the commit.
2. **Worker, claim.** A short transaction marks the version `processing` and resolves the embedding model(s) and key.
3. **Worker, no transaction open.** Fetch (URL) or read (file) the bytes, scan with ClamAV, store a fetched page, extract sections, chunk, embed.
4. **Worker, store.** One transaction writes chunks and vectors, marks the version `ready` and makes it active; the previous active version becomes `replaced` and its chunks are deleted (its file stays downloadable until someone deletes that version).

Failures end as `failed` with an `error_code` and a readable message (retry from the app), or `rejected` when the scanner finds malware (the file is deleted). A beat task every minute re-dispatches versions stuck `queued` for 2 minutes or `processing` for 15, and re-index jobs that stopped.

### Extraction and chunks

- PDF: text per page; the page numbers are kept on each chunk.
- Word: headings (Title, Heading 1 to 9) become a heading path such as `Treatments > Teeth whitening`; tables become `cell | cell` lines.
- Excel and CSV: the first non-empty row is the header and each later row becomes `Header: value; Header: value`, so a price stays next to its item. 40 rows per section; the sheet name and row range are kept.
- HTML: navigation, footers, forms, scripts and styles are dropped; `h1` to `h6` become the heading path; the page `<title>` becomes the document title when none was given.
- Chunks are about 600 tokens with 80 tokens of overlap, split on paragraphs, then sentences. Small sections are merged; each keeps its own heading inline.
- Caps: 2,000 PDF pages, 50,000 rows per sheet, 2,000,000 characters of text and 3,000 chunks per document.

## 3. Embeddings

The business chooses one embedding model (from the models XploSys has activated with kind `embedding`). It uses the provider key from the AI assistant page (the business's own or, if the plan allows, XploSys's); the provider does not have to be switched on for replies.

| Provider | Endpoint | Notes |
|---|---|---|
| OpenAI | `POST /v1/embeddings` | `dimensions: 1536`, up to 2,048 inputs per call |
| Google Gemini | `POST /v1beta/models/{model}:batchEmbedContents` | `outputDimensionality: 1536`, task type `RETRIEVAL_DOCUMENT` for chunks and `RETRIEVAL_QUERY` for questions |
| DeepSeek | none | DeepSeek has no embeddings API |

Every vector is 1536 numbers; a model that returns another size fails the version. Every embedding call is recorded as an `ai_call` usage record (purposes `knowledge_index`, `knowledge_reindex`, `retrieval`, `preview`, `search_test`) with tokens and estimated cost from the catalogue price.

**Changing the model** with documents present sets it as pending. A re-index job embeds every chunk with the new model in batches of 500 chunks (sent to the provider 96 at a time) beside the old vectors, then switches in one update and deletes the old vectors. Answers keep using the old model until then. Documents added during the switch are embedded with both. If the new provider fails (no key, outage), the settings page shows the error and a "Try again" button.

## 4. Retrieval in answers

For each automatic reply and preview:

1. The customer's latest message, and their last three messages joined, are embedded in one call.
2. pgvector finds the nearest chunks of this business and model (`hnsw.iterative_scan = relaxed_order`, `ef_search = 100`); each chunk keeps its best score.
3. Chunks with a cosine similarity at or above the business's **minimum match score** (default 0.30) are kept, up to **passages per answer** (default 5), and trimmed to 9,000 characters.
4. They go into the system prompt as numbered excerpts, marked as reference material and not instructions. The validator accepts prices, times, phone numbers and links that appear in them.
5. The message's provenance records each passage's document, chunk, score and page or sheet. The conversation shows "Sources" under AI replies, and the preview lists them.

If the query embedding fails, the reply still runs without excerpts (and the failure is in the provenance). If nothing passes the threshold, nothing is added, so an answer that quotes a price from a document that wasn't retrieved is not grounded and goes to a person.

**Tuning:** the "Test a question" card on the Knowledge page shows the closest passages with their scores and which would be used. Raise the score if unrelated passages are used; lower it if the right passage is just below. Scores depend on the model, so check again after changing it.

## 5. Security

- Files are stored only in the private bucket. The API never serves them; downloads are presigned links valid for 5 minutes, forced to download as `application/octet-stream`.
- Every file and fetched page is scanned by ClamAV before it is parsed. `CLAMAV_HOST` is required outside development; in development without it the version records "not scanned".
- Web fetches resolve every host, require public addresses only (private, loopback, link-local, carrier-grade NAT, multicast and IPv4-mapped forms are all refused), connect to the checked address with the original Host header and TLS name, follow at most 3 redirects by hand, and ignore proxy settings. `KNOWLEDGE_URL_ALLOW_PRIVATE` exists for local testing and is refused in production.
- All five knowledge tables have forced row-level security; cross-tenant sweeps go through definer functions that return only IDs.
- Readers (`knowledge:read`, agents included) can see documents and passages and download files; only `knowledge:manage` (owners and admins) can add, change, delete, search-test or change settings.

## 6. Configuration

| Setting | Default | Meaning |
|---|---|---|
| `S3_ENDPOINT_URL` | (none) | S3 API endpoint the API and worker use |
| `S3_PUBLIC_ENDPOINT_URL` | endpoint | Address presigned download links point to (must be reachable by browsers) |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION` | | Credentials and private bucket (`python -m app.cli ensure-bucket` creates it) |
| `CLAMAV_HOST`, `CLAMAV_PORT` | (none), 3310 | clamd address; required outside development |
| `KNOWLEDGE_MAX_FILE_MB` | 20 | Upload size limit (also used by the frontend's `/api` proxy at build time) |
| `KNOWLEDGE_URL_MAX_MB`, `KNOWLEDGE_URL_TIMEOUT_SECONDS`, `KNOWLEDGE_URL_MAX_REDIRECTS` | 5, 15, 3 | Web page limits |
| `KNOWLEDGE_CHUNK_TOKENS`, `KNOWLEDGE_CHUNK_OVERLAP_TOKENS` | 600, 80 | Chunk size |
| `KNOWLEDGE_EMBEDDING_BATCH` | 96 | Texts per embedding call |

In Docker Compose, SeaweedFS provides the bucket (port 8333 is published for download links) and ClamAV starts with `docker compose --profile scan up -d` (needs about 3 to 4 GB of memory), after setting `CLAMAV_HOST=clamav`.

For XploSys: in the Super Admin console, sync or add an embedding model (kind "Knowledge search"), add its price and activate it, so businesses can choose it.
