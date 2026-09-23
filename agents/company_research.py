"""Company Research Agent -- produces a sourced company_profiles row.

Prefers Google Search grounding. When the key has no grounding quota, falls
back to the company's own pages; either way every stored fact traces to a URL
that was actually retrieved.
"""

import json
import logging
import time

import sitefetch
from db import get_conn
from llm import gemini

log = logging.getLogger(__name__)

RESEARCH_PROMPT = """Research the company "{name}"{website}.

Find: what they build, their mission, funding stage and most recent round,
approximate headcount, known tech stack, and notable news from the last year.

Report only what you can find in search results, and cite the source for each
fact. If something cannot be found, say so explicitly -- do not estimate or
infer funding amounts or headcounts."""

FIELDS_SPEC = """{
  "summary": string,
  "mission": string,
  "funding_stage": string,
  "funding_amount": string,
  "last_funding_date": string,
  "employee_count_estimate": string,
  "tech_stack": [string],
  "recent_news": [{"headline": string, "url": string, "date": string}]
}"""

EXTRACT_PROMPT = f"""Convert the research notes below into strict JSON:

{FIELDS_SPEC}

Use null for any field the notes do not establish. Never invent a value. If the
notes say something could not be found, that field is null."""

PAGES_PROMPT = f"""Below are pages fetched from a company's own website. Build a
profile using ONLY facts stated in these pages -- no outside knowledge.

Return strict JSON:

{FIELDS_SPEC}

Use null for anything the pages do not state. recent_news items must use a URL
from the pages provided. Do not guess funding, headcount, or dates."""


def get_profile(company_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM company_profiles WHERE company_id = ?", (company_id,)
        ).fetchone()
        return dict(row) if row else None


async def _research_grounded(name: str, website: str | None):
    site = f" ({website})" if website else ""
    return await gemini.grounded_json(RESEARCH_PROMPT.format(name=name, website=site), EXTRACT_PROMPT)


async def _research_from_pages(website: str | None):
    pages = await sitefetch.fetch_company_pages(website)
    if not pages:
        return None, []
    payload = await gemini.json_from_pages(
        {url: page["text"] for url, page in pages.items()}, PAGES_PROMPT
    )
    return payload, list(pages)


async def research_company(company_id: int, name: str, website: str | None) -> dict | None:
    """Research and upsert. Returns the stored profile, or None on failure.

    Rate limits propagate (LLMRateLimited) so the worker can back off and retry.
    """
    if not gemini.available():
        log.warning("no Gemini key configured; skipping company research")
        return None

    try:
        try:
            payload, sources = await _research_grounded(name, website)
        except gemini.GroundingUnavailable:
            payload, sources = await _research_from_pages(website)
    except gemini.LLMRateLimited:
        raise
    except (ValueError, gemini.LLMError) as exc:
        log.warning("company research failed for %s: %s", name, exc)
        return None

    if not isinstance(payload, dict) or not sources:
        log.info("no sourced research found for %s", name)
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
