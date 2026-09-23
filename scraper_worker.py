"""Scraper worker.

Discovery only: fetch postings, upsert them, and enqueue genuinely new jobs for
the agent worker. Agents are never called from here, so LLM latency cannot slow
down discovery.

Modes:
    python scraper_worker.py --loop    # default; runs forever
    python scraper_worker.py --once    # single pass, then exit
"""

import argparse
import asyncio
import logging
import time

import httpx

import task_queue as job_queue
from ats_scraper import scrape_company
from config import SCRAPE_INTERVAL_SECONDS
from db import get_conn, init_db

log = logging.getLogger(__name__)


def _upsert_job(conn, company_id: int, posting: dict) -> int | None:
    """Insert or refresh a posting. Returns the job id only if newly created."""
    now = time.time()
    existing = conn.execute(
        "SELECT id FROM jobs WHERE company_id = ? AND external_id = ?",
        (company_id, posting["external_id"]),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE jobs SET title=?, location=?, url=?, description=?, remote_type=?, last_seen_at=? "
            "WHERE id=?",
            (
                posting["title"],
                posting["location"],
                posting["url"],
                posting["description"],
                posting["remote_type"],
                now,
                existing["id"],
            ),
        )
        return None

    cur = conn.execute(
        """INSERT INTO jobs
           (company_id, external_id, title, location, url, description, status,
            remote_type, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)""",
        (
            company_id,
            posting["external_id"],
            posting["title"],
            posting["location"],
            posting["url"],
            posting["description"],
            posting["remote_type"],
            now,
            now,
        ),
    )
    return cur.lastrowid


async def scrape_once() -> dict:
    """One pass over every tracked company."""
    with get_conn() as conn:
        companies = [dict(r) for r in conn.execute("SELECT * FROM companies").fetchall()]

    if not companies:
        log.info("no companies tracked yet")
        return {"companies": 0, "new_jobs": 0}

    new_job_ids: list[int] = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        results = await asyncio.gather(
            *(scrape_company(client, c["platform"], c["board_token"]) for c in companies),
            return_exceptions=True,
        )

    for company, postings in zip(companies, results):
        if isinstance(postings, BaseException):
            log.warning("scrape error for %s: %s", company["name"], postings)
            continue
        with get_conn() as conn:
            for posting in postings:
                try:
                    job_id = _upsert_job(conn, company["id"], posting)
                except Exception as exc:  # noqa: BLE001 - one bad posting shouldn't kill the pass
                    log.warning("could not store posting for %s: %s", company["name"], exc)
                    continue
                if job_id:
                    new_job_ids.append(job_id)

    for job_id in new_job_ids:
        job_queue.enqueue(job_id, "enrich_and_match")

    log.info("scrape pass complete: %d companies, %d new jobs", len(companies), len(new_job_ids))
    return {"companies": len(companies), "new_jobs": len(new_job_ids)}


async def scrape_loop() -> None:
    log.info("scraper loop started (interval %ss)", SCRAPE_INTERVAL_SECONDS)
    while True:
        try:
            await scrape_once()
        except asyncio.CancelledError:
            log.info("scraper loop cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - loop must survive a bad cycle
            log.exception("scrape pass failed: %s", exc)
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)


def start_background(loop: asyncio.AbstractEventLoop) -> asyncio.Task:
    """Schedule the loop on an existing event loop (used by app.py in dev)."""
    return loop.create_task(scrape_loop())


def main() -> None:
    parser = argparse.ArgumentParser(description="JobRadar scraper worker")
    parser.add_argument("--once", action="store_true", help="single pass, then exit")
    parser.add_argument("--loop", action="store_true", help="run forever (default)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [scraper] %(message)s"
    )
    init_db()

    if args.once:
        result = asyncio.run(scrape_once())
        print(f"scraped {result['companies']} companies, {result['new_jobs']} new jobs")
    else:
        try:
            asyncio.run(scrape_loop())
        except KeyboardInterrupt:
            log.info("shutting down")


if __name__ == "__main__":
    main()
