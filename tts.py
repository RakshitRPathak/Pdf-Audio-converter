"""Text -> speech. Chunking, retry, and interchangeable backends (Google Cloud, gTTS, offline eSpeak)."""
import io
import os
import re
import shutil
import subprocess
import time
import wave

MAX_CHUNK_BYTES = 4500          # Google Cloud TTS rejects requests over 5000 bytes of text

LANGS = {   # user-facing voice -> per-backend settings
    "en-US": {"google": "en-US", "gtts": ("en", "com"),    "espeak": "en-us"},
    "en-GB": {"google": "en-GB", "gtts": ("en", "co.uk"),  "espeak": "en-gb"},
    "en-IN": {"google": "en-IN", "gtts": ("en", "co.in"),  "espeak": "en"},
    "hi-IN": {"google": "hi-IN", "gtts": ("hi", "co.in"),  "espeak": "hi"},
    "ta-IN": {"google": "ta-IN", "gtts": ("ta", "co.in"),  "espeak": "ta"},
}


class TtsError(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ chunking
_SENT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])|\n+")


def _hard_split(piece: str, limit: int):
    """Last resort for a 'sentence' longer than the limit: split on spaces, then on raw bytes."""
    words, cur = piece.split(" "), ""
    for w in words:
        while len(w.encode()) > limit:                       # absurdly long token (e.g. base64 blob)
            head = w.encode()[:limit].decode("utf-8", "ignore")
            if cur:
                yield cur
                cur = ""
            yield head
            w = w[len(head):]
        cand = f"{cur} {w}" if cur else w
        if len(cand.encode()) > limit:
            yield cur
            cur = w
        else:
            cur = cand
    if cur:
        yield cur


def split_text(text: str, max_bytes: int = MAX_CHUNK_BYTES) -> list[str]:
    """Pack whole sentences into chunks of <= max_bytes UTF-8 bytes; never cut mid-sentence unless forced."""
    sentences = [s.strip() for s in _SENT.split(text) if s and s.strip()]
    chunks, cur = [], ""
    for s in sentences:
        parts = [s] if len(s.encode()) <= max_bytes else list(_hard_split(s, max_bytes))
        for p in parts:
            cand = f"{cur} {p}" if cur else p
            if len(cand.encode()) <= max_bytes:
                cur = cand
            else:
                chunks.append(cur)
                cur = p
    if cur:
        chunks.append(cur)
    return chunks


# ------------------------------------------------------------------ backends
class Backend:
    name = "base"
    ext, mime = "mp3", "audio/mpeg"

    def synthesize(self, text: str, voice: str, rate: float) -> bytes:
        raise NotImplementedError

    def join(self, parts: list[bytes]) -> bytes:
        return b"".join(parts)                               # MP3 frames concatenate cleanly


class GoogleBackend(Backend):
    name = "google"

    def __init__(self):
        from google.cloud import texttospeech as t           # imported lazily: optional dependency
        self._t, self._client = t, t.TextToSpeechClient()

    def synthesize(self, text, voice, rate):
        t = self._t
        try:
            r = self._client.synthesize_speech(
                input=t.SynthesisInput(text=text),
                voice=t.VoiceSelectionParams(language_code=LANGS[voice]["google"]),
                audio_config=t.AudioConfig(audio_encoding=t.AudioEncoding.MP3, speaking_rate=rate),
                timeout=30)
            return r.audio_content
        except Exception as e:
            name = type(e).__name__
            permanent = name in {"InvalidArgument", "PermissionDenied", "Unauthenticated", "NotFound"}
            raise TtsError(f"Google TTS: {name}", retryable=not permanent) from e


class GttsBackend(Backend):
    name = "gtts"

    def synthesize(self, text, voice, rate):
        from gtts import gTTS
        lang, tld = LANGS[voice]["gtts"]
        try:
            buf = io.BytesIO()
            gTTS(text=text, lang=lang, tld=tld, slow=rate < 0.85).write_to_fp(buf)
            return buf.getvalue()
        except Exception as e:
            raise TtsError(f"gTTS: {type(e).__name__}") from e


class LocalBackend(Backend):
    """Offline eSpeak-NG: robotic, but needs no account or network. Handy for demos and CI."""
    name = "local"
    ext, mime = "wav", "audio/wav"

    def __init__(self):
        if not shutil.which("espeak-ng"):
            raise TtsError("espeak-ng is not installed", retryable=False)

    def synthesize(self, text, voice, rate):
        p = subprocess.run(["espeak-ng", "-v", LANGS[voice]["espeak"], "-s", str(int(170 * rate)), "--stdout", "--", text],
                           capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout:
            raise TtsError("espeak-ng failed", retryable=False)
        return p.stdout

    def join(self, parts):
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            for i, b in enumerate(parts):
                with wave.open(io.BytesIO(b), "rb") as r:
                    if i == 0:
                        w.setparams(r.getparams())
                    w.writeframes(r.readframes(r.getnframes()))
        return out.getvalue()


def get_backend(name: str | None = None) -> Backend:
    name = (name or os.environ.get("TTS_BACKEND", "auto")).lower()
    if name == "auto":
        name = "google" if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") else "gtts"
    return {"google": GoogleBackend, "gtts": GttsBackend, "local": LocalBackend}[name]()


# ------------------------------------------------------------------ orchestration
def synthesize_all(backend: Backend, chunks: list[str], voice: str, rate: float, on_progress=None,
                   should_cancel=lambda: False, retries: int = 3, backoff: float = 1.0) -> bytes:
    """Synthesize every chunk in order. Transient errors retry with exponential backoff; permanent ones fail fast."""
    parts = []
    for i, chunk in enumerate(chunks):
        for attempt in range(retries):
            if should_cancel():
                raise Cancelled()
            try:
                parts.append(backend.synthesize(chunk, voice, rate))
                break
            except TtsError as e:
                if not e.retryable or attempt == retries - 1:
                    raise TtsError(f"Speech service failed on part {i + 1} of {len(chunks)}: {e}", retryable=False) from e
                time.sleep(backoff * 2 ** attempt)
        if on_progress:
            on_progress(i + 1, len(chunks))
    return backend.join(parts)
