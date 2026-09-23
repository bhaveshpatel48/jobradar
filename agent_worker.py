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
import json
import logging
import time

import task_queue as job_queue
from agents import company_research, matcher, people_finder
from config import AGENT_POLL_INTERVAL_SECONDS
from db import get_conn, get_settings, init_db

log = logging.getLogger(__name__)


def _load_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT j.*, c.name AS company_name, c.website AS company_website
               FROM jobs j JOIN companies c ON c.id = j.company_id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None


def _decoded_settings() -> dict:
    settings = get_settings()
    for field in ("resume_structured", "interests", "domains"):
        raw = settings.get(field)
        if raw:
            try:
                settings[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return settings


def _store_match(job_id: int, result: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE jobs SET match_score=?, match_reasoning=?, match_matched_skills=?,
                   match_concerns=?, matched_at=? WHERE id=?""",
            (
                result["match_score"],
                result["reasoning"],
                json.dumps(result["matched_skills"]),
                json.dumps(result["concerns"]),
                time.time(),
                job_id,
            ),
        )


async def process_task(task: dict) -> None:
    job = _load_job(task["job_id"])
    if job is None:
        log.warning("job %s no longer exists", task["job_id"])
        return

    company_id = job["company_id"]
    name = job["company_name"]
    website = job["company_website"]

    profile = company_research.get_profile(company_id)
    if not company_research.is_fresh(profile):
        refreshed = await company_research.research_company(company_id, name, website)
        profile = refreshed or profile

    if not people_finder.has_fresh_people(company_id):
        await people_finder.find_people(company_id, name, website)

    settings = _decoded_settings()

    if not matcher.has_profile(settings):
        log.info("no resume/interests saved yet; leaving job %s unscored", job["id"])
        return

    if not matcher.passes_prefilter(job, settings):
        log.info("job %s filtered out before scoring (%s)", job["id"], job["remote_type"])
        return

    result = await matcher.score_job(job, settings, profile)
    if result:
        _store_match(job["id"], result)
        log.info("scored job %s: %s", job["id"], result["match_score"])


async def drain_queue() -> int:
    """Process every pending task, then return how many were handled."""
    handled = 0
    while True:
        task = job_queue.claim_next()
        if task is None:
            return handled
        try:
            await process_task(task)
            job_queue.mark_done(task["id"])
        except Exception as exc:  # noqa: BLE001 - one bad task shouldn't stop the drain
            log.exception("task %s failed: %s", task["id"], exc)
            job_queue.mark_failed(task["id"], str(exc))
        handled += 1


async def agent_loop() -> None:
    log.info("agent loop started (poll %ss)", AGENT_POLL_INTERVAL_SECONDS)
    requeued = job_queue.reset_stale()
    if requeued:
        log.info("requeued %d stale in-progress tasks", requeued)

    while True:
        try:
            handled = await drain_queue()
            if handled == 0:
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
        handled = asyncio.run(drain_queue())
        print(f"processed {handled} tasks")
    else:
        try:
            asyncio.run(agent_loop())
        except KeyboardInterrupt:
            log.info("shutting down")


if __name__ == "__main__":
    main()
