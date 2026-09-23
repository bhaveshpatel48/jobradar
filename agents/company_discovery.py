"""Company Discovery Agent -- find companies worth tracking.

Gemini proposes candidates from the user's domains and interests. Each one is
then verified by actually locating its job board (ATS probe or careers page);
only companies whose board returns postings are stored. A model suggestion is
never persisted on its own say-so.
"""

import asyncio
import json
import logging
import time

import httpx

import ats_detect
from db import get_conn
from llm import gemini

log = logging.getLogger(__name__)

CANDIDATES = 30
CONCURRENCY = 6

PROMPT = """Suggest {n} real companies a job seeker should track.

Preferred domains: {domains}
Interests / roles: {interests}
Extra request: {query}

Favour startups and scale-ups that hire software engineers and are known to
hire remotely. Only name companies you are confident exist and are operating.
Do NOT include any of these, already tracked: {exclude}

For each give the company website, and -- only if you actually know it -- the
applicant tracking system it uses (one of: {platforms}) and its job-board slug.
Use null when unsure; a wrong guess is worse than null.

Return strict JSON:
{{"companies": [{{"name": string, "website": string, "ats": string|null, "board_token": string|null}}]}}
"""


def _tracked() -> tuple[set[str], set[tuple[str, str]]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT name, platform, board_token FROM companies").fetchall()
    names = {r["name"].strip().lower() for r in rows}
    boards = {(r["platform"], r["board_token"].lower()) for r in rows}
    return names, boards


def _append_log(run_id: int, line: str) -> None:
    with get_conn() as conn:
        row = conn.execute("SELECT log FROM discovery_runs WHERE id = ?", (run_id,)).fetchone()
        lines = json.loads(row["log"]) if row and row["log"] else []
        lines.append(line)
        conn.execute(
            "UPDATE discovery_runs SET log = ? WHERE id = ?", (json.dumps(lines[-200:]), run_id)
        )


async def _propose(settings: dict, query: str | None, exclude: set[str]) -> list[dict]:
    prompt = PROMPT.format(
        n=CANDIDATES,
        domains=", ".join(settings.get("domains") or []) or "any",
        interests=", ".join(settings.get("interests") or []) or "software engineering",
        query=query or "none",
        exclude=", ".join(sorted(exclude)[:150]) or "none",
        platforms=", ".join(ats_detect.PROBEABLE),
    )
    payload = await gemini.generate_json(prompt)
    companies = payload.get("companies") if isinstance(payload, dict) else payload
    return [c for c in companies or [] if isinstance(c, dict) and (c.get("name") or "").strip()]


async def run_discovery(run_id: int, settings: dict, query: str | None) -> int:
    """Propose, verify, store. Returns how many companies were added."""
    tracked_names, tracked_boards = _tracked()
    candidates = await _propose(settings, query, tracked_names)
    fresh = [c for c in candidates if c["name"].strip().lower() not in tracked_names]

    with get_conn() as conn:
        conn.execute("UPDATE discovery_runs SET candidates = ? WHERE id = ?", (len(fresh), run_id))
    _append_log(run_id, f"Gemini proposed {len(candidates)} companies, {len(fresh)} not yet tracked")

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(follow_redirects=True) as client:

        async def verify(candidate: dict):
            async with semaphore:
                return candidate, await ats_detect.detect_for_company(
                    client,
                    name=candidate["name"],
                    website=candidate.get("website"),
                    hint_platform=candidate.get("ats"),
                    hint_token=candidate.get("board_token"),
                )

        results = await asyncio.gather(*(verify(c) for c in fresh), return_exceptions=True)

    added = 0
    for result in results:
        if isinstance(result, BaseException):
            log.warning("discovery verification error: %s", result)
            continue
        candidate, found = result
        name = candidate["name"].strip()
        if not found:
            _append_log(run_id, f"✗ {name}: no reachable job board found")
            continue
        if (found.platform, found.token.lower()) in tracked_boards:
            continue
        with get_conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO companies
                   (name, platform, board_token, website, careers_url, source, preferred, created_at)
                   VALUES (?, ?, ?, ?, ?, 'discovered', 0, ?)""",
                (
                    name,
                    found.platform,
                    found.token,
                    candidate.get("website"),
                    found.careers_url,
                    time.time(),
                ),
            )
        tracked_boards.add((found.platform, found.token.lower()))
        added += 1
        _append_log(run_id, f"✓ {name}: {found.platform} ({found.job_count} open roles)")

    return added
