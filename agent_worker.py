"""Agent worker -- the orchestrator.

Polls job_queue and runs, per job:
    company research (if stale) -> people finder (if stale) -> matching agent

Separate from the scraper so LLM latency never blocks job discovery.

Modes:
    python agent_worker.py --loop    # default; runs forever
    python agent_worker.py --once    # drain the queue, then exit
"""

import argparse
import asyncio
import logging
import time

import scoring
import task_queue as job_queue
from agents import company_discovery, company_research, matcher, people_finder
from config import AGENT_POLL_INTERVAL_SECONDS, COMPANY_RESEARCH_TTL_DAYS
from db import get_conn, init_db, transaction
from llm import gemini

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF_SECONDS = 60
QUOTA_RECHECK_SECONDS = 30 * 60
DRAIN_BATCH = 10


def _attempt_is_fresh(attempted_at: float | None) -> bool:
    return bool(attempted_at) and (time.time() - attempted_at) < COMPANY_RESEARCH_TTL_DAYS * 86400


def _mark_attempt(company_id: int, column: str) -> None:
    assert column in ("research_attempted_at", "people_attempted_at")
    with get_conn() as conn:
        conn.execute(f"UPDATE companies SET {column} = ? WHERE id = ?", (time.time(), company_id))


async def process_task(task: dict) -> None:
    job = scoring.load_job(task["job_id"])
    if job is None:
        log.warning("job %s no longer exists", task["job_id"])
        return

    settings = scoring.decoded_settings()
    # A "score" task is an explicit user request, so the automatic gates
    # (engineering-only, remote pre-filter) don't apply to it.
    requested = task["task_type"] == job_queue.USER_REQUESTED
    if not requested and settings.get("engineering_only") and not job.get("is_engineering"):
        return

    company_id = job["company_id"]
    name = job["company_name"]
    website = job["company_website"]

    profile = company_research.get_profile(company_id)
    if not _attempt_is_fresh(job.get("research_attempted_at")):
        refreshed = await company_research.research_company(company_id, name, website)
        _mark_attempt(company_id, "research_attempted_at")
        profile = refreshed or profile

    if not _attempt_is_fresh(job.get("people_attempted_at")):
        await people_finder.find_people(company_id, name, website)
        _mark_attempt(company_id, "people_attempted_at")

    if not matcher.has_profile(settings):
        log.info("no resume/interests saved yet; leaving job %s unscored", job["id"])
        return

    if not requested and not matcher.passes_prefilter(job, settings):
        log.info("job %s filtered out before scoring (%s)", job["id"], job["remote_type"])
        return

    await scoring.score_and_store(job, settings, profile)


async def drain_queue(limit: int | None = None) -> int:
    """Process pending tasks (up to `limit`), then return how many were handled.

    A rate limit releases the task untouched and backs off, rather than burning
    one of its retry attempts on a quota problem.
    """
    handled = 0
    while limit is None or handled < limit:
        task = job_queue.claim_next()
        if task is None:
            return handled
        try:
            await process_task(task)
            job_queue.mark_done(task["id"])
        except gemini.LLMQuotaExhausted as exc:
            job_queue.release(task["id"])
            # Sleep in chunks so clearing the flag early (POST /api/llm/quota/clear,
            # e.g. after enabling billing) takes effect within half an hour.
            pause = max(60, min(exc.reset_at - time.time(), QUOTA_RECHECK_SECONDS))
            log.warning("%s; pausing agent work for %ds", exc, pause)
            await asyncio.sleep(pause)
            return handled
        except gemini.LLMRateLimited:
            job_queue.release(task["id"])
            log.warning("Gemini rate limit hit; backing off %ss", RATE_LIMIT_BACKOFF_SECONDS)
            await asyncio.sleep(RATE_LIMIT_BACKOFF_SECONDS)
            continue
        except Exception as exc:  # noqa: BLE001 - one bad task shouldn't stop the drain
            log.exception("task %s failed: %s", task["id"], exc)
            job_queue.mark_failed(task["id"], str(exc))
        handled += 1
    return handled


def _reset_stale_discovery() -> None:
    """A run left 'running' by a restart is retried, not stuck forever."""
    with get_conn() as conn:
        conn.execute("UPDATE discovery_runs SET status = 'pending' WHERE status = 'running'")


def _claim_discovery() -> dict | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT * FROM discovery_runs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE discovery_runs SET status = 'running' WHERE id = ?", (row["id"],))
        return dict(row)


async def run_pending_discovery() -> bool:
    run = _claim_discovery()
    if run is None:
        return False
    log.info("discovery run %s started", run["id"])
    status, error, added = "done", None, 0
    try:
        added = await company_discovery.run_discovery(run["id"], scoring.decoded_settings(), run["query"])
    except Exception as exc:  # noqa: BLE001 - record the failure on the run
        log.exception("discovery run %s failed", run["id"])
        status, error = "failed", str(exc)[:1000]
    with get_conn() as conn:
        conn.execute(
            "UPDATE discovery_runs SET status=?, error=?, added=?, completed_at=? WHERE id=?",
            (status, error, added, time.time(), run["id"]),
        )
    log.info("discovery run %s %s: %d companies added", run["id"], status, added)
    return True


async def agent_loop() -> None:
    log.info("agent loop started (poll %ss)", AGENT_POLL_INTERVAL_SECONDS)
    requeued = job_queue.reset_stale()
    if requeued:
        log.info("requeued %d stale in-progress tasks", requeued)
    _reset_stale_discovery()

    while True:
        try:
            # Small batches so a user-triggered discovery run never waits
            # behind a long backlog of scoring tasks.
            ran_discovery = await run_pending_discovery()
            handled = await drain_queue(limit=DRAIN_BATCH)
            if handled == 0 and not ran_discovery:
                await asyncio.sleep(AGENT_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("agent loop cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - loop must survive
            log.exception("agent loop error: %s", exc)
            await asyncio.sleep(AGENT_POLL_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="JobRadar agent worker")
    parser.add_argument("--once", action="store_true", help="drain the queue, then exit")
    parser.add_argument("--loop", action="store_true", help="run forever (default)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [agent] %(message)s")
    init_db()

    if args.once:

        async def once() -> int:
            await run_pending_discovery()
            return await drain_queue()

        handled = asyncio.run(once())
        print(f"processed {handled} tasks")
    else:
        try:
            asyncio.run(agent_loop())
        except KeyboardInterrupt:
            log.info("shutting down")


if __name__ == "__main__":
    main()
