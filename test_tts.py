import shutil
import wave
import io

import pytest

from conftest import FakeBackend
from tts import Cancelled, LocalBackend, TtsError, split_text, synthesize_all


def test_chunks_respect_byte_limit_and_lose_no_words():
    text = " ".join(f"Sentence number {i} says something quite ordinary." for i in range(400))
    chunks = split_text(text, 500)
    assert len(chunks) > 5 and all(len(c.encode()) <= 500 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_chunks_break_on_sentence_boundaries_when_possible():
    chunks = split_text("First sentence here. Second sentence here. Third sentence here.", 45)
    assert chunks == ["First sentence here. Second sentence here.", "Third sentence here."]


def test_multibyte_text_is_measured_in_bytes_not_characters():
    text = "नमस्ते दुनिया। " * 200                         # 3 bytes per Devanagari char
    chunks = split_text(text, 300)
    assert all(len(c.encode()) <= 300 for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_a_giant_sentence_with_no_punctuation_is_still_split():
    chunks = split_text("word " * 5000, 1000)
    assert all(len(c.encode()) <= 1000 for c in chunks) and len(chunks) >= 25


def test_an_unbroken_token_longer_than_the_limit_is_hard_split():
    chunks = split_text("A" * 2500, 1000)
    assert all(len(c.encode()) <= 1000 for c in chunks) and "".join(chunks) == "A" * 2500


def test_empty_input_gives_no_chunks():
    assert split_text("") == [] and split_text("  \n\n ") == []


def test_transient_errors_are_retried_then_succeed():
    b = FakeBackend(); b.script = [TtsError("503"), TtsError("503"), None]
    out = synthesize_all(b, ["hello there"], "en-US", 1.0, backoff=0)
    assert out.startswith(b"AUD:") and len(b.calls) == 3


class FailsOnSecondChunk(FakeBackend):
    def synthesize(self, text, voice, rate):
        if text == "two":
            self.calls.append(text)
            raise TtsError("503")
        return super().synthesize(text, voice, rate)


def test_retries_are_exhausted_and_reported_with_part_number():
    b = FailsOnSecondChunk()
    with pytest.raises(TtsError, match=r"part 2 of 2"):
        synthesize_all(b, ["one", "two"], "en-US", 1.0, retries=3, backoff=0)
    assert b.calls.count("two") == 3


def test_permanent_errors_fail_fast_without_retrying():
    b = FakeBackend(); b.script = [TtsError("bad credentials", retryable=False)]
    with pytest.raises(TtsError):
        synthesize_all(b, ["x"], "en-US", 1.0, backoff=0)
    assert len(b.calls) == 1


def test_cancellation_stops_between_chunks_and_progress_is_reported():
    b, seen, stop = FakeBackend(), [], {"flag": False}

    def progress(done, total):
        seen.append((done, total))
        stop["flag"] = done == 2

    with pytest.raises(Cancelled):
        synthesize_all(b, ["a", "b", "c", "d"], "en-US", 1.0, on_progress=progress, should_cancel=lambda: stop["flag"])
    assert seen == [(1, 4), (2, 4)] and len(b.calls) == 2


@pytest.mark.skipif(not shutil.which("espeak-ng"), reason="espeak-ng not installed")
def test_local_backend_produces_one_valid_joined_wav():
    b = LocalBackend()
    parts = [b.synthesize(t, "en-US", 1.0) for t in ("Hello world.", "This is a second chunk.")]
    joined = b.join(parts)
    with wave.open(io.BytesIO(joined)) as w:
        frames = w.getnframes()
    def real_frames(b):            # eSpeak streams a WAV header with an "unknown length" placeholder, so count what is present
        with wave.open(io.BytesIO(b)) as r:
            return len(r.readframes(r.getnframes())) // (r.getsampwidth() * r.getnchannels())
    assert frames == sum(real_frames(p) for p in parts) and frames > 0
