"""Scraper worker.

Discovery only: fetch postings, upsert them, and enqueue new jobs for the agent
worker. Agents are never called from here, so LLM latency cannot slow down
discovery.

Scope follows settings.scrape_scope: 'preferred' scrapes only companies you
marked preferred; 'all' also scrapes companies found by AI discovery. Newly
added companies are picked up within seconds rather than waiting for the next
full pass.

Modes:
    python scraper_worker.py --loop    # default; runs forever
    python scraper_worker.py --once    # single pass, then exit
"""

import argparse
import asyncio
import logging
import time

import httpx

import salary
import task_queue as job_queue
from ats_scraper import ScrapeError, scrape_company
from config import SCRAPE_INTERVAL_SECONDS
from db import get_conn, get_settings, init_db
from job_classify import is_engineering

log = logging.getLogger(__name__)

CONCURRENCY = 8
NEW_COMPANY_POLL_SECONDS = 15


def _upsert_job(conn, company_id: int, posting: dict) -> tuple[int | None, bool]:
    """Insert or refresh a posting in one atomic statement, keyed on
    (company_id, external_id). Returns (job_id if newly created, is_engineering).

    A single ON CONFLICT upsert, rather than check-then-insert, so two scrapers
    running at once (the loop plus a manual --once) can't race on the same job.
    """
    now = time.time()
    engineering = 1 if is_engineering(posting["title"], posting.get("department")) else 0
    row = conn.execute(
        """INSERT INTO jobs
           (company_id, external_id, title, location, url, description, remote_type,
            department, is_engineering, salary_min, salary_max, salary_currency,
            salary_period, salary_text, salary_source, status, first_seen_at, last_seen_at)
           VALUES (:company_id, :external_id, :title, :location, :url, :description,
                   :remote_type, :department, :is_engineering, :salary_min, :salary_max,
                   :salary_currency, :salary_period, :salary_text, :salary_source,
                   'new', :now, :now)
           ON CONFLICT(company_id, external_id) DO UPDATE SET
               title = excluded.title,
               location = excluded.location,
               url = excluded.url,
               -- Boards without list descriptions send "", which must not wipe a
               -- description the agent worker fetched from the job page.
               description = COALESCE(NULLIF(excluded.description, ''), jobs.description),
               remote_type = excluded.remote_type,
               department = excluded.department,
               is_engineering = excluded.is_engineering,
               -- Likewise, keep a salary parsed from a later-fetched description
               -- when this scrape has none; a salary the board does send wins.
               salary_min = CASE WHEN excluded.salary_source IS NULL THEN jobs.salary_min ELSE excluded.salary_min END,
               salary_max = CASE WHEN excluded.salary_source IS NULL THEN jobs.salary_max ELSE excluded.salary_max END,
               salary_currency = CASE WHEN excluded.salary_source IS NULL THEN jobs.salary_currency ELSE excluded.salary_currency END,
               salary_period = CASE WHEN excluded.salary_source IS NULL THEN jobs.salary_period ELSE excluded.salary_period END,
               salary_text = CASE WHEN excluded.salary_source IS NULL THEN jobs.salary_text ELSE excluded.salary_text END,
               salary_source = COALESCE(excluded.salary_source, jobs.salary_source),
               last_seen_at = excluded.last_seen_at
           RETURNING id, first_seen_at""",
        {
            "company_id": company_id,
            "external_id": posting["external_id"],
            "title": posting["title"],
            "location": posting.get("location"),
            "url": posting.get("url"),
            "description": posting.get("description"),
            "remote_type": posting.get("remote_type", "unknown"),
            "department": posting.get("department"),
            "is_engineering": engineering,
            **{k: posting.get(k) for k in salary.EMPTY},
            "now": now,
        },
    ).fetchone()
    # The update branch never touches first_seen_at, so it equals `now` only
    # when this statement inserted the row.
    created = row["first_seen_at"] == now
    return (row["id"] if created else None), bool(engineering)


def _companies_in_scope(only_unscraped: bool = False) -> list[dict]:
    scope = get_settings().get("scrape_scope") or "preferred"
    clauses = []
    if scope != "all":
        clauses.append("preferred = 1")
    if only_unscraped:
        clauses.append("last_scraped_at IS NULL")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM companies {where}").fetchall()]


def _record_scrape(company_id: int, error: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE companies SET last_scraped_at = ?, last_scrape_error = ? WHERE id = ?",
            (time.time(), error, company_id),
        )


async def scrape_companies(companies: list[dict]) -> dict:
    if not companies:
        return {"companies": 0, "new_jobs": 0, "failed": 0}

    engineering_only = bool(get_settings().get("engineering_only"))
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(follow_redirects=True) as client:

        async def one(company: dict):
            async with semaphore:
                return await scrape_company(client, company["platform"], company["board_token"])

        results = await asyncio.gather(*(one(c) for c in companies), return_exceptions=True)

    new_job_ids: list[int] = []
    failed = 0
    for company, result in zip(companies, results):
        if isinstance(result, BaseException):
            failed += 1
            message = str(result) if isinstance(result, ScrapeError) else f"unexpected: {result}"
            log.warning("scrape failed for %s: %s", company["name"], message)
            _record_scrape(company["id"], message[:500])
            continue

        with get_conn() as conn:
            for posting in result:
                try:
                    job_id, engineering = _upsert_job(conn, company["id"], posting)
                except Exception as exc:  # noqa: BLE001 - one bad posting shouldn't kill the pass
                    log.warning("could not store posting for %s: %s", company["name"], exc)
                    continue
                # Only spend LLM quota on jobs the user will actually see.
                if job_id and (engineering or not engineering_only):
                    new_job_ids.append(job_id)
        _record_scrape(company["id"], None)

    for job_id in new_job_ids:
        job_queue.enqueue(job_id, "enrich_and_match")

    log.info(
        "scraped %d companies (%d failed), %d new jobs queued",
        len(companies), failed, len(new_job_ids),
    )
    return {"companies": len(companies), "new_jobs": len(new_job_ids), "failed": failed}


async def scrape_once() -> dict:
    """One pass over every company in scope."""
    companies = _companies_in_scope()
    if not companies:
        log.info("no companies in scope yet")
    return await scrape_companies(companies)


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

        deadline = time.monotonic() + SCRAPE_INTERVAL_SECONDS
        while time.monotonic() < deadline:
            await asyncio.sleep(min(NEW_COMPANY_POLL_SECONDS, max(0, deadline - time.monotonic())))
            try:
                fresh = _companies_in_scope(only_unscraped=True)
                if fresh:
                    log.info("scraping %d newly added companies", len(fresh))
                    await scrape_companies(fresh)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("new-company scrape failed: %s", exc)


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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    init_db()

    if args.once:
        result = asyncio.run(scrape_once())
        print(
            f"scraped {result['companies']} companies ({result['failed']} failed), "
            f"{result['new_jobs']} new jobs queued"
        )
    else:
        try:
            asyncio.run(scrape_loop())
        except KeyboardInterrupt:
            log.info("shutting down")


if __name__ == "__main__":
    main()
