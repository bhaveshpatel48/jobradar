"""Company Research Agent -- produces a sourced company_profiles row."""

import json
import logging
import time

from config import COMPANY_RESEARCH_TTL_DAYS
from db import get_conn
from llm import gemini

log = logging.getLogger(__name__)

RESEARCH_PROMPT = """Research the company "{name}"{website}.

Find: what they build, their mission, funding stage and most recent round,
approximate headcount, known tech stack, and notable news from the last year.

Report only what you can find in search results, and cite the source for each
fact. If something cannot be found, say so explicitly -- do not estimate or
infer funding amounts or headcounts."""

EXTRACT_PROMPT = """Convert the research notes below into strict JSON:

{
  "summary": string,
  "mission": string,
  "funding_stage": string,
  "funding_amount": string,
  "last_funding_date": string,
  "employee_count_estimate": string,
  "tech_stack": [string],
  "recent_news": [{"headline": string, "url": string, "date": string}]
}

Use null for any field the notes do not establish. Never invent a value. If the
notes say something could not be found, that field is null."""


def get_profile(company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_profiles WHERE company_id = ?", (company_id,)
        ).fetchone()
        return dict(row) if row else None


def is_fresh(profile: dict | None) -> bool:
    if not profile or not profile.get("researched_at"):
        return False
    age = time.time() - profile["researched_at"]
    return age < COMPANY_RESEARCH_TTL_DAYS * 86400


async def research_company(company_id: int, name: str, website: str | None) -> dict | None:
    """Research and upsert. Returns the stored profile, or None on failure."""
    if not gemini.available():
        log.warning("no Gemini key configured; skipping company research")
        return None

    site = f" ({website})" if website else ""
    try:
        payload, sources = await gemini.grounded_json(
            RESEARCH_PROMPT.format(name=name, website=site), EXTRACT_PROMPT
        )
    except (ValueError, gemini.LLMUnavailable) as exc:
        log.warning("company research failed for %s: %s", name, exc)
        return None

    if not isinstance(payload, dict):
        log.warning("company research returned no usable JSON for %s", name)
        return None

    record = {
        "company_id": company_id,
        "summary": payload.get("summary"),
        "mission": payload.get("mission"),
        "funding_stage": payload.get("funding_stage"),
        "funding_amount": payload.get("funding_amount"),
        "last_funding_date": payload.get("last_funding_date"),
        "employee_count_estimate": payload.get("employee_count_estimate"),
        "tech_stack": json.dumps(payload.get("tech_stack") or []),
        "recent_news": json.dumps(payload.get("recent_news") or []),
        "sources": json.dumps(sources),
        "researched_at": time.time(),
    }

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO company_profiles
               (company_id, summary, mission, funding_stage, funding_amount,
                last_funding_date, employee_count_estimate, tech_stack,
                recent_news, sources, researched_at)
               VALUES (:company_id, :summary, :mission, :funding_stage, :funding_amount,
                       :last_funding_date, :employee_count_estimate, :tech_stack,
                       :recent_news, :sources, :researched_at)
               ON CONFLICT(company_id) DO UPDATE SET
                 summary=excluded.summary, mission=excluded.mission,
                 funding_stage=excluded.funding_stage, funding_amount=excluded.funding_amount,
                 last_funding_date=excluded.last_funding_date,
                 employee_count_estimate=excluded.employee_count_estimate,
                 tech_stack=excluded.tech_stack, recent_news=excluded.recent_news,
                 sources=excluded.sources, researched_at=excluded.researched_at""",
            record,
        )

    log.info("researched %s (%d sources)", name, len(sources))
    return record
