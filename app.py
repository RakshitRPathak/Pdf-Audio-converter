"""PDF-to-Audio Generator: Flask app.

POST   /api/convert            multipart: pdf, first_page?, last_page?, voice?, rate?   -> 202 {job_id, ...}
GET    /api/jobs/<id>          progress / status
GET    /api/jobs/<id>/audio    the audio (supports Range requests so the player can seek); ?download=1 forces a download
DELETE /api/jobs/<id>          cancel
GET    /api/options            available voices and the active backend
"""
import os
import re
import tempfile

from flask import Flask, jsonify, render_template, request, send_file

from jobs import JobManager
from pdf_extract import PdfError, extract
from tts import LANGS, get_backend, split_text

WORDS_PER_MINUTE = 150


def create_app(backend=None, storage_dir=None, **overrides):
    app = Flask(__name__)
    app.config.update({"MAX_CONTENT_LENGTH": 20 * 1024 * 1024, "MAX_PAGES": 300, "MAX_CHARS": 200_000,
                       "JOB_TTL": 3600, "RETRY_BACKOFF": 1.0, **overrides})
    backend = backend or get_backend()
    jobs = JobManager(backend, storage_dir or tempfile.mkdtemp(prefix="pdf2audio-"),
                      ttl=app.config["JOB_TTL"], retry_backoff=app.config["RETRY_BACKOFF"])
    app.jobs = jobs

    def err(code, message, status):
        return jsonify(error={"code": code, "message": message}), status

    @app.errorhandler(PdfError)
    def _pdf(e):
        return err(e.code, e.message, e.status)

    @app.errorhandler(413)
    def _too_big(_):
        return err("FILE_TOO_LARGE", f"File is larger than {app.config['MAX_CONTENT_LENGTH'] // 2**20} MB", 413)

    @app.errorhandler(404)
    def _nf(_):
        return err("NOT_FOUND", "Not found", 404)

    @app.errorhandler(405)
    def _na(_):
        return err("METHOD_NOT_ALLOWED", "Method not allowed", 405)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/options")
    def options():
        return {"backend": backend.name, "voices": list(LANGS), "rate": {"min": 0.5, "max": 2.0, "default": 1.0}}

    @app.post("/api/convert")
    def convert():
        f = request.files.get("pdf")
        if not f or not f.filename:
            return err("NO_FILE", "Choose a PDF file to upload", 400)
        voice = request.form.get("voice", "en-US")
        if voice not in LANGS:
            return err("BAD_VOICE", f"voice must be one of {', '.join(LANGS)}", 400)
        try:
            rate = float(request.form.get("rate", 1.0))
        except ValueError:
            return err("BAD_RATE", "rate must be a number", 400)
        if not 0.5 <= rate <= 2.0:
            return err("BAD_RATE", "rate must be between 0.5 and 2.0", 400)

        doc = extract(f.read(), request.form.get("first_page"), request.form.get("last_page"), app.config["MAX_PAGES"])
        if len(doc.text) > app.config["MAX_CHARS"]:
            return err("TOO_LONG", f"That is {len(doc.text):,} characters; the limit is {app.config['MAX_CHARS']:,}. "
                                   "Pick a smaller page range.", 413)
        chunks = split_text(doc.text)
        words = len(doc.text.split())
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(f.filename)[0]).strip("._") or "audio"
        job = jobs.submit(chunks, voice, rate, f"{stem}_p{doc.first_page}-{doc.last_page}.{backend.ext}")
        return jsonify(**job.public(), pages=doc.pages_used, pages_total=doc.pages_total, characters=len(doc.text),
                       estimated_minutes=round(words / WORDS_PER_MINUTE / rate, 1), preview=doc.text[:400]), 202

    @app.get("/api/jobs/<job_id>")
    def status(job_id):
        job = jobs.get(job_id)
        return jsonify(job.public()) if job else err("NOT_FOUND", "Unknown job", 404)

    @app.get("/api/jobs/<job_id>/audio")
    def audio(job_id):
        job = jobs.get(job_id)
        if not job:
            return err("NOT_FOUND", "Unknown job", 404)
        if job.status != "done" or not job.path or not os.path.exists(job.path):
            return err("NOT_READY", f"Audio is not available (status: {job.status})", 409)
        return send_file(job.path, mimetype=backend.mime, conditional=True, max_age=0,
                         as_attachment=request.args.get("download") == "1", download_name=job.filename)

    @app.delete("/api/jobs/<job_id>")
    def cancel(job_id):
        job = jobs.cancel(job_id)
        return jsonify(job.public()) if job else err("NOT_FOUND", "Unknown job", 404)

    return app


if __name__ == "__main__":
    create_app().run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
