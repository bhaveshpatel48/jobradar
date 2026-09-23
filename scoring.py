"""Score one job against the user's profile and persist the result.

Shared by the agent worker (queued, background priority) and the API's
"Check my fit" button (synchronous, interactive priority), so both paths fetch
descriptions and store results identically.
"""

import json
import logging
import time

import ats_scraper
import salary
import sitefetch
from agents import matcher
from db import get_conn, get_settings
from llm import gemini

log = logging.getLogger(__name__)


class NoProfile(RuntimeError):
    """Nothing to match against yet -- no resume or interests saved."""


def load_job(job_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT j.*, c.name AS company_name, c.website AS company_website,
                      c.platform AS company_platform, c.board_token AS company_token,
                      c.research_attempted_at, c.people_attempted_at
               FROM jobs j JOIN companies c ON c.id = j.company_id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None


def decoded_settings() -> dict:
    settings = get_settings()
    for field in ("resume_structured", "interests", "domains"):
        raw = settings.get(field)
        if raw:
            try:
                settings[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    return settings


async def ensure_description(job: dict) -> None:
    """Several ATS list endpoints omit descriptions (and pay); fetch them
    before scoring, from the platform's detail API or the job page."""
    if job.get("description"):
        return
    description, pay = await ats_scraper.fetch_details(
        job["company_platform"], job["company_token"], job["external_id"]
    )
    if not description:
        description = await sitefetch.fetch_job_description(job.get("url"))
    if not description:
        return
    job["description"] = description
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET description = ? WHERE id = ?", (description, job["id"]))

    # Salary couldn't be read at scrape time without these; a structured
    # detail field wins over a range parsed from the description.
    if not job.get("salary_source"):
        pay = pay or salary.parse_salary_text(description)
        if pay:
            job.update(pay)
            with get_conn() as conn:
                conn.execute(
                    f"UPDATE jobs SET {', '.join(f'{k} = :{k}' for k in pay)} WHERE id = :id",
                    {**pay, "id": job["id"]},
                )


def _store(job_id: int, result: dict) -> None:
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


async def score_and_store(
    job: dict, settings: dict, company_profile: dict | None, *, priority: str = gemini.BACKGROUND
) -> dict | None:
    """Score `job` and persist it. Returns the result, or None if the model
    gave no usable answer. Retry-later errors (LLMRateLimited and subclasses)
    propagate so callers can requeue instead of recording a non-result."""
    if not matcher.has_profile(settings):
        raise NoProfile("add a resume or interests on the Settings page first")
    await ensure_description(job)
    result = await matcher.score_job(job, settings, company_profile, priority=priority)
    if result:
        _store(job["id"], result)
        log.info("scored job %s: %s", job["id"], result["match_score"])
    return result
