import io
import os
import sys
import time

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app          # noqa: E402
from tts import Backend, TtsError   # noqa: E402


class FakeBackend(Backend):
    """Deterministic, offline. `script` is consumed one call at a time: None = succeed, else raise."""
    name = "fake"

    def __init__(self):
        self.calls = []
        self.script = []
        self.delay = 0.0

    def synthesize(self, text, voice, rate):
        self.calls.append(text)
        if self.delay:
            time.sleep(self.delay)
        if self.script:
            outcome = self.script.pop(0)
            if outcome is not None:
                raise outcome
        return b"AUD:" + text.encode()[:12] + b"|"


def make_pdf(pages, header=None, footer_numbers=False) -> bytes:
    """pages: list of list-of-lines. Optionally adds a running header and 'Page N' footers."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for n, lines in enumerate(pages, 1):
        y = 800
        if header:
            c.drawString(60, y, header)
            y -= 40
        for ln in lines:
            c.drawString(60, y, ln)
            y -= 16
        if footer_numbers:
            c.drawString(280, 40, f"Page {n}")
        c.showPage()
    c.save()
    return buf.getvalue()


LOREM = ["The quick brown fox jumps over the lazy dog. This sentence exists so the",
         "page has enough readable text to pass the scanned-document check easily."]


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def client(backend, tmp_path):
    app = create_app(backend=backend, storage_dir=str(tmp_path), RETRY_BACKOFF=0.0)
    return app.test_client()


def upload(client, pdf: bytes, name="doc.pdf", **form):
    data = {"pdf": (io.BytesIO(pdf), name), **{k: str(v) for k, v in form.items()}}
    return client.post("/api/convert", data=data, content_type="multipart/form-data")


def wait_done(client, job_id, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        s = client.get(f"/api/jobs/{job_id}").get_json()
        if s["status"] in ("done", "error", "cancelled"):
            return s
        time.sleep(0.02)
    raise AssertionError("job did not finish")
