"""PDF -> clean, speakable text.

Reading PDF text aloud fails in predictable ways, and each one is handled here:
  * hard line wraps in the middle of sentences       -> re-flowed into paragraphs
  * words split with a hyphen at a line end          -> re-joined ("exam-\nple" -> "example")
  * running headers/footers and page numbers         -> detected by repetition and removed
  * ligatures / odd unicode (ﬁ, ’, non-breaking sp)  -> NFKC-normalised
  * URLs and citation markers like [12]              -> URLs become "link", markers dropped
  * headings with no punctuation (TTS runs on)       -> get a full stop so the voice pauses
  * scanned / image-only or encrypted PDFs           -> a clear error instead of silent audio
"""
import io
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class PdfError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass
class Extracted:
    text: str
    pages_total: int
    first_page: int
    last_page: int

    @property
    def pages_used(self) -> int:
        return self.last_page - self.first_page + 1


PAGE_NUMBER = re.compile(r"^\s*(page\s+)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
CITATION = re.compile(r"\[\d+(?:\s*[,–-]\s*\d+)*\]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MIN_CHARS_PER_PAGE = 20
MAX_HEADER_LEN = 60          # running headers/footers are short; long repeated-looking lines are body text


def _shape(line: str) -> str:
    return re.sub(r"\d+", "#", line.strip().lower())


def _strip_page_edges(pages: list[list[str]]) -> list[list[str]]:
    """Remove page numbers and any edge line that repeats on at least half the pages."""
    counts: Counter[str] = Counter()
    if len(pages) >= 3:
        for lines in pages:
            edge = [l for l in lines[:2] + lines[-2:] if len(l.strip()) <= MAX_HEADER_LEN]
            for shape in set(map(_shape, edge)):
                counts[shape] += 1
    threshold = max(3, len(pages) // 2)
    repeated = {s for s, c in counts.items() if c >= threshold and s}
    out = []
    for lines in pages:
        keep = list(lines)
        for edge in (slice(0, 2), slice(-2, None)):
            for ln in lines[edge]:
                if PAGE_NUMBER.match(ln) or (len(ln.strip()) <= MAX_HEADER_LEN and _shape(ln) in repeated):
                    if ln in keep:
                        keep.remove(ln)
        out.append(keep)
    return out


def _join(lines: list[str]) -> str:
    """Re-flow hard-wrapped lines into one paragraph string."""
    buf = ""
    for ln in lines:
        ln = ln.strip()
        if not buf:
            buf = ln
        elif buf.endswith("-") and ln[:1].islower() and len(buf) > 1 and buf[-2].isalpha():
            buf = buf[:-1] + ln                     # exam-\nple -> example
        else:
            buf += " " + ln
    return buf


def clean_pages(raw_pages: list[str]) -> str:
    pages = []
    for raw in raw_pages:
        raw = unicodedata.normalize("NFKC", raw)
        raw = CONTROL.sub("", raw).replace("\u00ad", "")         # soft hyphens
        pages.append([ln.rstrip() for ln in raw.splitlines()])
    stripped = _strip_page_edges([[l for l in p if l.strip()] for p in pages])
    # Re-insert paragraph breaks: a blank line in the original is a paragraph boundary.
    paragraphs: list[list[str]] = [[]]
    for original, kept in zip(pages, stripped):
        kept_set = Counter(kept)
        for ln in original:
            if not ln.strip():
                if paragraphs[-1]:
                    paragraphs.append([])
            elif kept_set[ln] > 0:
                kept_set[ln] -= 1
                paragraphs[-1].append(ln)
    out = []
    for lines in paragraphs:
        if not lines:
            continue
        para = _join(lines)
        para = URL.sub("link", para)
        para = CITATION.sub("", para)
        para = re.sub(r"\s+", " ", para).strip()
        if not para:
            continue
        if len(para) < 80 and para[-1] not in ".!?:;,\"')":
            para += "."                                      # heading -> audible pause
        out.append(para)
    return "\n\n".join(out)


def _parse_int(value, name: str):
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise PdfError("BAD_RANGE", f"{name} must be a whole number")
    if n < 1:
        raise PdfError("BAD_RANGE", f"{name} must be 1 or greater")
    return n


def extract(data: bytes, first=None, last=None, max_pages: int = 300) -> Extracted:
    if not data.lstrip()[:5] == b"%PDF-":
        raise PdfError("NOT_A_PDF", "That file is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise PdfError("ENCRYPTED", "This PDF is password protected")
        total = len(reader.pages)
    except PdfError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError, RecursionError):
        raise PdfError("CORRUPT_PDF", "This PDF could not be read; it may be damaged")
    if total == 0:
        raise PdfError("EMPTY_PDF", "This PDF has no pages")

    f, l = _parse_int(first, "first_page") or 1, _parse_int(last, "last_page") or total
    if f > l or l > total:
        raise PdfError("BAD_RANGE", f"Page range must be within 1-{total} (first ≤ last)")
    if l - f + 1 > max_pages:
        raise PdfError("TOO_MANY_PAGES", f"Please choose at most {max_pages} pages at a time", 413)

    raw = []
    for i in range(f - 1, l):
        try:
            raw.append(reader.pages[i].extract_text() or "")
        except Exception:                                    # one bad page shouldn't kill the document
            raw.append("")
    if sum(len(r.strip()) for r in raw) < MIN_CHARS_PER_PAGE * (l - f + 1) * 0.25:
        raise PdfError("NO_TEXT", "No readable text found. This looks like a scanned PDF (images of pages); "
                                  "run OCR on it first.", 422)
    return Extracted(text=clean_pages(raw), pages_total=total, first_page=f, last_page=l)
