"""People/Contact Finder Agent.

Finds publicly named people at a company and their publicly indexed LinkedIn
profile URL. Never fetches or reproduces LinkedIn page content -- the URL is
stored as a pointer only, per the ground rules.
"""

import logging
import re
import time
from urllib.parse import urlparse

from config import COMPANY_RESEARCH_TTL_DAYS
from db import get_conn
from llm import gemini

from . import email_enrichment

log = logging.getLogger(__name__)

RESEARCH_PROMPT = """Find people who publicly work at "{name}"{website} --
founders, executives, engineering leaders, hiring managers, or anyone listed on
the company's public team page.

For each person report their name, job title, and their publicly indexed
LinkedIn profile URL if one appears in search results. Do not open or summarise
LinkedIn pages -- only report the profile URL itself.

Also report any company email addresses that appear publicly (for example on a
contact page, in a press release, or in open-source commits), exactly as written.
Only include people and emails you actually find in search results."""

EXTRACT_PROMPT = """Convert the research notes below into strict JSON:

{
  "people": [{"name": string, "title": string, "linkedin_url": string, "source": string}],
  "observed_emails": [string]
}

Only include people explicitly named in the notes. Use null for a missing
linkedin_url or title. Never invent a person or a URL. observed_emails must
contain only addresses literally quoted in the notes."""

EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+\.[\w.-]+)")


def _infer_pattern(emails: list[str], domain: str) -> str | None:
    """Infer the company's email convention from publicly observed addresses."""
    for email in emails:
        if not isinstance(email, str) or "@" not in email:
            continue
        local, _, host = email.strip().lower().partition("@")
        if domain not in host:
            continue
        if re.fullmatch(r"[a-z]+\.[a-z]+", local):
            return "first.last"
        if re.fullmatch(r"[a-z]+_[a-z]+", local):
            return "first_last"
    return None


def _domain_of(website: str | None) -> str | None:
    if not website:
        return None
    parsed = urlparse(website if "//" in website else f"https://{website}")
    host = (parsed.netloc or parsed.path).lower()
    return host[4:] if host.startswith("www.") else host or None


def has_fresh_people(company_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(discovered_at) AS latest FROM people WHERE company_id = ?",
            (company_id,),
        ).fetchone()
    latest = row["latest"] if row else None
    if not latest:
        return False
    return (time.time() - latest) < COMPANY_RESEARCH_TTL_DAYS * 86400


async def find_people(company_id: int, name: str, website: str | None) -> int:
    """Discover people and upsert them. Returns the number stored."""
    if not gemini.available():
        log.warning("no Gemini key configured; skipping people lookup")
        return 0

    site = f" ({website})" if website else ""
    try:
        payload, _sources = await gemini.grounded_json(
            RESEARCH_PROMPT.format(name=name, website=site), EXTRACT_PROMPT
        )
    except (ValueError, gemini.LLMUnavailable) as exc:
        log.warning("people lookup failed for %s: %s", name, exc)
        return 0

    if not isinstance(payload, dict):
        return 0

    people = payload.get("people") or []
    domain = _domain_of(website)
    pattern = _infer_pattern(payload.get("observed_emails") or [], domain or "")

    stored = 0
    for person in people:
        if not isinstance(person, dict):
            continue
        person_name = (person.get("name") or "").strip()
        if not person_name:
            continue

        linkedin = person.get("linkedin_url")
        if linkedin and "linkedin.com" not in str(linkedin).lower():
            linkedin = None

        email, confidence = None, "none"
        if domain:
            if email_enrichment.enabled():
                email, confidence = await email_enrichment.find_email(person_name, domain)
            if not email and pattern:
                guessed = email_enrichment.guess_from_pattern(person_name, pattern, domain)
                if guessed:
                    email, confidence = guessed, "guessed"

        with get_conn() as conn:
            conn.execute(
                """INSERT INTO people
                   (company_id, name, title, linkedin_url, email, email_confidence,
                    source, discovered_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(company_id, name) DO UPDATE SET
                     title=excluded.title, linkedin_url=excluded.linkedin_url,
                     email=excluded.email, email_confidence=excluded.email_confidence,
                     source=excluded.source, discovered_at=excluded.discovered_at""",
                (
                    company_id,
                    person_name,
                    person.get("title"),
                    linkedin,
                    email,
                    confidence,
                    person.get("source"),
                    time.time(),
                ),
            )
        stored += 1

    log.info("found %d people at %s", stored, name)
    return stored
