import io
import threading

from conftest import LOREM, make_pdf, upload, wait_done
from tts import TtsError

PDF = make_pdf([LOREM, LOREM])


def test_full_flow_convert_poll_stream_and_download(client, backend):
    r = upload(client, PDF, "My Report (final).pdf", voice="en-GB", rate=1.2)
    assert r.status_code == 202
    body = r.get_json()
    assert body["pages"] == 2 and body["characters"] > 100 and body["estimated_minutes"] > 0
    assert body["preview"].startswith("The quick brown fox")
    s = wait_done(client, body["job_id"])
    assert s["status"] == "done" and s["progress"]["done"] == s["progress"]["total"] >= 1

    a = client.get(f"/api/jobs/{body['job_id']}/audio")
    assert a.status_code == 200 and a.mimetype == "audio/mpeg" and a.data.startswith(b"AUD:")
    d = client.get(f"/api/jobs/{body['job_id']}/audio?download=1")
    assert "attachment" in d.headers["Content-Disposition"] and "My_Report_final_p1-2.mp3" in d.headers["Content-Disposition"]


def test_audio_supports_range_requests_so_the_player_can_seek(client):
    job = upload(client, PDF).get_json()["job_id"]
    wait_done(client, job)
    full = client.get(f"/api/jobs/{job}/audio").data
    part = client.get(f"/api/jobs/{job}/audio", headers={"Range": "bytes=2-5"})
    assert part.status_code == 206 and part.data == full[2:6]
    assert part.headers["Content-Range"] == f"bytes 2-5/{len(full)}"


def test_identical_request_is_served_from_cache_without_calling_the_backend(client, backend):
    wait_done(client, upload(client, PDF, voice="en-US").get_json()["job_id"])
    n = len(backend.calls)
    second = upload(client, PDF, voice="en-US").get_json()
    assert second["status"] == "done" and second["cached"] is True and len(backend.calls) == n
    third = upload(client, PDF, voice="en-GB").get_json()            # different voice = different audio
    wait_done(client, third["job_id"])
    assert len(backend.calls) > n


def test_input_validation_errors_are_precise_json(client):
    assert client.post("/api/convert", data={}).get_json()["error"]["code"] == "NO_FILE"
    assert upload(client, b"just text", "a.pdf").get_json()["error"]["code"] == "NOT_A_PDF"
    assert upload(client, PDF, voice="klingon").get_json()["error"]["code"] == "BAD_VOICE"
    assert upload(client, PDF, rate="fast").get_json()["error"]["code"] == "BAD_RATE"
    assert upload(client, PDF, rate=9).get_json()["error"]["code"] == "BAD_RATE"
    assert upload(client, PDF, first_page=3).get_json()["error"]["code"] == "BAD_RANGE"
    assert upload(client, make_pdf([[]])).status_code == 422


def test_oversized_upload_is_rejected(backend, tmp_path):
    from app import create_app
    small = create_app(backend=backend, storage_dir=str(tmp_path), MAX_CONTENT_LENGTH=2000).test_client()
    r = upload(small, make_pdf([LOREM] * 10))
    assert r.status_code == 413 and r.get_json()["error"]["code"] == "FILE_TOO_LARGE"


def test_document_over_the_character_limit_asks_for_a_smaller_range(backend, tmp_path):
    from app import create_app
    c = create_app(backend=backend, storage_dir=str(tmp_path), MAX_CHARS=50).test_client()
    r = upload(c, PDF)
    assert r.status_code == 413 and r.get_json()["error"]["code"] == "TOO_LONG"
    assert backend.calls == []


def test_unknown_malformed_and_traversal_job_ids_are_404(client):
    for jid in ("deadbeef", "0" * 32, "../../etc/passwd", "%2e%2e%2fsecret"):
        assert client.get(f"/api/jobs/{jid}").status_code == 404
        assert client.get(f"/api/jobs/{jid}/audio").status_code == 404
    assert client.delete("/api/jobs/nope").status_code == 404
    assert client.get("/api/nope").get_json()["error"]["code"] == "NOT_FOUND"


def test_speech_service_failure_surfaces_as_a_job_error_not_a_crash(client, backend):
    backend.script = [TtsError("quota exceeded", retryable=False)]
    job = upload(client, PDF).get_json()["job_id"]
    s = wait_done(client, job)
    assert s["status"] == "error" and "quota exceeded" in s["error"]
    r = client.get(f"/api/jobs/{job}/audio")
    assert r.status_code == 409 and r.get_json()["error"]["code"] == "NOT_READY"


def test_transient_failures_are_retried_transparently(client, backend):
    backend.script = [TtsError("503"), None]
    assert wait_done(client, upload(client, PDF).get_json()["job_id"])["status"] == "done"


def test_cancelling_a_running_job(client, backend):
    backend.delay = 0.15
    long_pdf = make_pdf([["Sentence one is here. " * 60]] * 4)
    job = upload(client, long_pdf).get_json()["job_id"]
    import time; time.sleep(0.05)
    assert client.delete(f"/api/jobs/{job}").status_code == 200
    assert wait_done(client, job)["status"] == "cancelled"


def test_many_simultaneous_conversions_all_complete_correctly(client, backend):
    results, errors = {}, []

    def one(i):
        try:
            pdf = make_pdf([[f"Document {i} has its own unique sentence about topic {i}. " * 3]])
            results[i] = wait_done(client, upload(client, pdf, f"d{i}.pdf").get_json()["job_id"])["status"]
        except Exception as e:                                        # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors and set(results.values()) == {"done"} and len(results) == 12


def test_index_options_and_health(client):
    assert client.get("/").status_code == 200 and b"PDF to Audio" in client.get("/").data
    assert "en-US" in client.get("/api/options").get_json()["voices"]
    assert client.get("/health").get_json() == {"ok": True}
    assert client.post("/health").get_json()["error"]["code"] == "METHOD_NOT_ALLOWED"
