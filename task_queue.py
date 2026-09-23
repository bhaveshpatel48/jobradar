"""job_queue helpers -- the handoff between scraper_worker and agent_worker."""

import time

from db import get_conn, transaction

MAX_ATTEMPTS = 3

# Task types: automatic scoring of a newly scraped job, a settings-driven
# re-score, and an explicit user request ("Check fit" in bulk).
NEW_JOB = "enrich_and_match"
RESCORE = "rescore_only"
USER_REQUESTED = "score"


def enqueue(job_id: int, task_type: str = "enrich_and_match") -> None:
    with get_conn() as conn:
        # Don't stack duplicate pending work for the same job.
        existing = conn.execute(
            "SELECT 1 FROM job_queue WHERE job_id = ? AND task_type = ? "
            "AND status IN ('pending', 'in_progress')",
            (job_id, task_type),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO job_queue (job_id, task_type, status, created_at) VALUES (?, ?, 'pending', ?)",
            (job_id, task_type, time.time()),
        )


def enqueue_all_jobs(task_type: str = RESCORE) -> int:
    with get_conn() as conn:
        rows = conn.execute("SELECT id FROM jobs").fetchall()
    for row in rows:
        enqueue(row["id"], task_type)
    return len(rows)


def enqueue_many(job_ids: list[int], task_type: str = USER_REQUESTED) -> int:
    """Queue several jobs in one transaction; returns how many were newly queued
    (jobs already waiting in the queue are skipped)."""
    now = time.time()
    with get_conn() as conn:
        queued = {
            r["job_id"]
            for r in conn.execute(
                "SELECT job_id FROM job_queue WHERE status IN ('pending', 'in_progress')"
            )
        }
        fresh = [j for j in dict.fromkeys(job_ids) if j not in queued]
        conn.executemany(
            "INSERT INTO job_queue (job_id, task_type, status, created_at) VALUES (?, ?, 'pending', ?)",
            [(j, task_type, now) for j in fresh],
        )
    return len(fresh)


def claim_next() -> dict | None:
    """Atomically claim one pending task. BEGIN IMMEDIATE prevents two agent
    workers from grabbing the same row."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM job_queue WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE job_queue SET status = 'in_progress', started_at = ?, attempts = attempts + 1 "
            "WHERE id = ?",
            (time.time(), row["id"]),
        )
        return dict(row)


def mark_done(queue_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_queue SET status = 'done', completed_at = ?, error = NULL WHERE id = ?",
            (time.time(), queue_id),
        )


def release(queue_id: int) -> None:
    """Return a claimed task to pending without counting the attempt."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE job_queue SET status = 'pending', started_at = NULL, "
            "attempts = MAX(attempts - 1, 0) WHERE id = ?",
            (queue_id,),
        )


def mark_failed(queue_id: int, error: str) -> None:
    """Retry until MAX_ATTEMPTS, then park the row as failed."""
    with get_conn() as conn:
        row = conn.execute("SELECT attempts FROM job_queue WHERE id = ?", (queue_id,)).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        status = "pending" if attempts < MAX_ATTEMPTS else "failed"
        conn.execute(
            "UPDATE job_queue SET status = ?, error = ?, completed_at = ? WHERE id = ?",
            (status, error[:1000], time.time(), queue_id),
        )


def reset_stale(older_than_seconds: int = 900) -> int:
    """Requeue in_progress rows orphaned by a worker crash/restart."""
    cutoff = time.time() - older_than_seconds
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE job_queue SET status = 'pending' "
            "WHERE status = 'in_progress' AND started_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def stats() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM job_queue GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
