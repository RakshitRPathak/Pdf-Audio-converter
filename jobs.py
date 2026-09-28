"""Background conversion jobs with progress, cancellation, content-addressed caching and expiry."""
import hashlib
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from tts import Backend, Cancelled, TtsError, synthesize_all

JOB_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class Job:
    id: str
    status: str = "queued"                  # queued | synthesizing | done | error | cancelled
    done: int = 0
    total: int = 0
    error: str | None = None
    cached: bool = False
    filename: str = "audio"
    path: str | None = None
    created: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self):
        return {"job_id": self.id, "status": self.status, "progress": {"done": self.done, "total": self.total},
                "error": self.error, "cached": self.cached}


class JobManager:
    def __init__(self, backend: Backend, storage_dir: str, workers: int = 2, ttl: int = 3600, retry_backoff: float = 1.0):
        self.backend, self.dir, self.ttl, self.backoff = backend, storage_dir, ttl, retry_backoff
        os.makedirs(storage_dir, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def _cache_path(self, chunks, voice, rate):
        h = hashlib.sha256("\x00".join([self.backend.name, voice, f"{rate:.2f}", *chunks]).encode()).hexdigest()
        return os.path.join(self.dir, f"{h}.{self.backend.ext}")

    def submit(self, chunks: list[str], voice: str, rate: float, filename: str) -> Job:
        self.purge()
        job = Job(id=uuid.uuid4().hex, total=len(chunks), filename=filename)
        path = self._cache_path(chunks, voice, rate)
        with self.lock:
            self.jobs[job.id] = job
        if os.path.exists(path):                             # identical request converted before
            os.utime(path)
            job.path, job.status, job.done, job.cached = path, "done", job.total, True
            return job
        self.pool.submit(self._run, job, chunks, voice, rate, path)
        return job

    def _run(self, job, chunks, voice, rate, path):
        job.status = "synthesizing"
        try:
            audio = synthesize_all(self.backend, chunks, voice, rate,
                                   on_progress=lambda d, t: setattr(job, "done", d),
                                   should_cancel=job.cancel.is_set, backoff=self.backoff)
            tmp = f"{path}.{job.id}.tmp"
            with open(tmp, "wb") as f:
                f.write(audio)
            os.replace(tmp, path)                            # atomic: readers never see a half-written file
            job.path, job.status = path, "done"
        except Cancelled:
            job.status = "cancelled"
        except TtsError as e:
            job.status, job.error = "error", str(e)
        except Exception:
            job.status, job.error = "error", "Unexpected error while generating audio"

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id) if JOB_ID.match(job_id or "") else None

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job:
            job.cancel.set()
            if job.status == "queued":
                job.status = "cancelled"
        return job

    def purge(self):
        now = time.time()
        with self.lock:
            for jid in [j for j, job in self.jobs.items() if now - job.created > self.ttl and job.status != "synthesizing"]:
                del self.jobs[jid]
        for name in os.listdir(self.dir):
            p = os.path.join(self.dir, name)
            try:
                if now - os.path.getmtime(p) > self.ttl:
                    os.remove(p)
            except OSError:
                pass
