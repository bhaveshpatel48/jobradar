"""People/Contact Finder Agent.

Finds publicly named people at a company and their LinkedIn profile URL. Never
fetches or reproduces LinkedIn page content -- the URL is stored as a pointer
only. In the fallback path a LinkedIn URL is accepted only if it appears as a
literal link on a page we fetched from the company's own site.
"""

import logging
import re
import time
from urllib.parse import urlparse

import sitefetch
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

PEOPLE_SPEC = """{
  "people": [{"name": string, "title": string, "linkedin_url": string, "source": string}],
  "observed_emails": [string]
}"""

EXTRACT_PROMPT = f"""Convert the research notes below into strict JSON:

{PEOPLE_SPEC}

Only include people explicitly named in the notes. Use null for a missing
linkedin_url or title. Never invent a person or a URL. observed_emails must
contain only addresses literally quoted in the notes."""

PAGES_PROMPT = f"""Below are pages fetched from a company's own website, followed
by the LinkedIn profile links and email addresses that literally appear on them.

List the people these pages name as working at the company, as strict JSON:

{PEOPLE_SPEC}

Rules: only people named in the pages. linkedin_url must be copied exactly from
the LINKEDIN LINKS list, or null -- never construct one. source is the page URL
the person appears on. observed_emails may only contain addresses from the
EMAILS list."""

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LINKEDIN_PROFILE = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+/?", re.I)


def _infer_pattern(emails: list[str], domain: str) -> str | None:
    """Infer the company's email convention from publicly observed addresses."""
    for email in emails:
        if not isinstance(email, str) or "@" not in email:
            continue
        local, _, host = email.strip().lower().partition("@")
        if not domain or domain not in host:
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


async def _from_pages(website: str | None) -> tuple[object, set[str]]:
    pages = await sitefetch.fetch_company_pages(website)
    if not pages:
        return None, set()

    linkedin_links: set[str] = set()
    emails: set[str] = set()
    for page in pages.values():
        for link in page["links"]:
            if link.startswith("mailto:"):
                emails.add(link[7:].split("?")[0])
            elif LINKEDIN_PROFILE.match(link):
                linkedin_links.add(link.split("?")[0])
        emails.update(EMAIL_RE.findall(page["text"]))

    appendix = {
        "LINKEDIN LINKS": "\n".join(sorted(linkedin_links)) or "(none)",
        "EMAILS": "\n".join(sorted(emails)) or "(none)",
    }
    page_texts = {url: page["text"] for url, page in pages.items()}
    page_texts.update(appendix)
    payload = await gemini.json_from_pages(page_texts, PAGES_PROMPT)
    return payload, linkedin_links


async def find_people(company_id: int, name: str, website: str | None) -> int:
    """Discover people and upsert them. Returns the number stored."""
    if not gemini.available():
        log.warning("no Gemini key configured; skipping people lookup")
        return 0

    site = f" ({website})" if website else ""
    allowed_linkedin: set[str] | None = None
    try:
        try:
            payload, _sources = await gemini.grounded_json(
                RESEARCH_PROMPT.format(name=name, website=site), EXTRACT_PROMPT
            )
        except gemini.GroundingUnavailable:
            payload, allowed_linkedin = await _from_pages(website)
    except gemini.LLMRateLimited:
        raise
    except (ValueError, gemini.LLMError) as exc:
        log.warning("people lookup failed for %s: %s", name, exc)
        return 0

    if not isinstance(payload, dict):
        return 0

    domain = _domain_of(website)
    pattern = _infer_pattern(payload.get("observed_emails") or [], domain or "")

    stored = 0
    for person in payload.get("people") or []:
        if not isinstance(person, dict):
            continue
        person_name = (person.get("name") or "").strip()
        if not person_name:
            continue

        linkedin = (person.get("linkedin_url") or "").split("?")[0] or None
        if linkedin and not LINKEDIN_PROFILE.match(linkedin):
            linkedin = None
        if linkedin and allowed_linkedin is not None and linkedin not in allowed_linkedin:
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
