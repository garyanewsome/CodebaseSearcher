"""Background research jobs with progress.

Indexing a big repo takes tens of minutes, which is far longer than any HTTP
request should stay open, and the chat needs to show what is happening
meanwhile. So /research/start runs the work on a thread and hands back a job
id; /research/jobs/{id} reports where it is (and the result when done).

Jobs live in memory only: if the service restarts, a running job is lost and
the caller simply starts again (nothing half-built is ever marked as indexed,
see vector_store.replace_repo_index).
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

KEEP_FINISHED_SECONDS = 3600


@dataclass
class Job:
    id: str
    key: tuple
    repo: str
    question: str | None
    stage: str = "queued"
    detail: str = ""
    done: int = 0
    total: int = 0
    started_at: float = field(default_factory=time.time)
    embed_started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    def message(self) -> str:
        """A line the chat can show as is."""
        if self.stage == "embedding" and self.total:
            text = f"{self.detail} {self.done:,} of {self.total:,} chunks"
            eta = self.eta_seconds()
            if eta is not None:
                text += f" (about {_fmt_duration(eta)} left)"
            return text
        return self.detail or self.stage

    def eta_seconds(self) -> float | None:
        if self.stage != "embedding" or not self.done or not self.embed_started_at:
            return None
        elapsed = time.time() - self.embed_started_at
        if elapsed < 20:  # too early for a steady rate
            return None
        return (self.total - self.done) * elapsed / self.done

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "stage": self.stage,
            "message": self.message(),
            "done": self.done,
            "total": self.total,
            "eta_seconds": self.eta_seconds(),
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at),
            "result": self.result,
            "error": self.error,
        }


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{max(seconds, 1)} s"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{seconds // 3600} h {(seconds % 3600) // 60} min"


_jobs: dict[str, Job] = {}
_guard = threading.Lock()


def _prune() -> None:
    cutoff = time.time() - KEEP_FINISHED_SECONDS
    for job_id in [j.id for j in _jobs.values() if j.finished_at and j.finished_at < cutoff]:
        del _jobs[job_id]


def start(repo: str, question: str | None, runner: Callable[[Callable], dict]) -> Job:
    """Starts `runner(progress)` on a thread, or returns the job already
    running for the same repo and question (a retry joins it rather than
    starting a duplicate). `progress(stage, detail=None, done=None,
    total=None)` is what the runner calls to report."""
    key = (repo.strip().lower(), (question or "").strip())
    with _guard:
        _prune()
        for existing in _jobs.values():
            if existing.key == key and not existing.finished:
                return existing
        job = Job(id=uuid.uuid4().hex[:12], key=key, repo=repo.strip(), question=question)
        _jobs[job.id] = job

    def progress(stage: str, detail: str | None = None, done: int | None = None, total: int | None = None) -> None:
        if stage == "embedding" and job.stage != "embedding":
            job.embed_started_at = time.time()
        job.stage = stage
        if detail is not None:
            job.detail = detail
        if done is not None:
            job.done = done
        if total is not None:
            job.total = total

    def run() -> None:
        try:
            job.result = runner(progress)
            job.stage = "done"
        except Exception as exc:
            # HTTPException carries the useful text in .detail
            job.error = str(getattr(exc, "detail", None) or exc)
            job.stage = "error"
        finally:
            job.finished_at = time.time()

    threading.Thread(target=run, name=f"research-{job.id}", daemon=True).start()
    return job


def get(job_id: str) -> Job | None:
    with _guard:
        return _jobs.get(job_id)
