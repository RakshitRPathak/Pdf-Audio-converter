# PDF-to-Audio Generator

Upload a text-based PDF, get a narration back. Flask + pypdf + Google Cloud Text-to-Speech
(with gTTS and an offline eSpeak backend as alternatives). 34 tests, all offline.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                                          # http://localhost:5000, uses gTTS by default (needs internet)
python -m pytest -q                                    # tests use a fake speech backend, no network or credentials

TTS_BACKEND=local python app.py                        # fully offline (needs espeak-ng), robotic voice, WAV output
GOOGLE_APPLICATION_CREDENTIALS=key.json TTS_BACKEND=google python app.py    # Google Cloud TTS, MP3 output
gunicorn "app:create_app()" -w 1 --threads 8           # production-style (one process: job state is in memory)
```
`samples/` has a demo PDF and the audio the offline backend produced from it.

## Layout
| File | Job |
|---|---|
| `pdf_extract.py` | PDF -> clean speakable text, and clear errors for unusable PDFs |
| `tts.py` | chunking, retry with backoff, interchangeable speech backends |
| `jobs.py` | background jobs: progress, cancel, cache, expiry |
| `app.py` | HTTP API + validation; `templates/index.html` is the UI |

## The problems this is built around
**PDF text does not read aloud cleanly.** `pdf_extract.py` re-flows hard-wrapped lines, re-joins words split by a
hyphen at a line end, removes page numbers and repeated running headers/footers, normalises ligatures, replaces URLs
with "link", drops citation markers like `[12]`, and adds a full stop after heading-like lines so the voice pauses.
Scanned (image-only) and password-protected PDFs get a specific error instead of silent or empty audio.

**Speech APIs have request limits.** Google rejects text over 5,000 bytes per request. `split_text` packs whole
sentences into chunks of at most 4,500 UTF-8 bytes (bytes, not characters: Hindi and Tamil are 3 bytes per character),
and falls back to word and byte splits only for text that has no sentence breaks.

**Long documents are slow and APIs fail.** Conversion runs in a background job with a progress bar. Each chunk retries
with exponential backoff on transient errors and fails fast on permanent ones (bad credentials, quota). The error
names the part that failed. Jobs can be cancelled between chunks.

**Repeat work costs money.** Audio is cached by a hash of (backend, voice, speed, text), so converting the same
pages again returns instantly without another API call. Files are written atomically and expire after an hour.

## API
`POST /api/convert` (form: `pdf`, `first_page`, `last_page`, `voice`, `rate`) -> 202 with job id, page and character counts,
estimated minutes and a text preview. `GET /api/jobs/<id>` progress. `GET /api/jobs/<id>/audio` audio with HTTP Range
support so the player can seek (`?download=1` to download). `DELETE /api/jobs/<id>` cancel. Errors are always
`{"error": {"code", "message"}}`.

## Limits
- Only text PDFs. Scanned PDFs need OCR first (e.g. `ocrmypdf`).
- Running-header removal needs 3 or more pages, so it never deletes real content from a short document.
- A compound word split exactly at a line end (`well-` / `known`) is joined as `wellknown`; that is indistinguishable
  from ordinary hyphenation.
- Multi-column layouts and tables come out in the order pypdf reads them, which is not always the human order.
- Job state is in memory: run one worker process (threads are fine), and add a shared store if you scale out.
- No authentication or rate limiting on the API, and limits are 20 MB, 300 pages and 200,000 characters per request.
- The Google Cloud and gTTS backends are written to their documented APIs but were not run against the live services
  here; the offline backend was run end to end (real PDF in, 17-second WAV out, Range requests verified).
