"""Per-platform ATS scrapers.

Each scraper returns a list of normalised dicts:
    {external_id, title, location, url, description, remote_type}

Scrapers only fetch and normalise -- persistence and queueing live in
scraper_worker.py.
"""

import logging
import re

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0)
HEADERS = {"User-Agent": "JobRadar/1.0 (personal job search tool)"}

FULLY_REMOTE = ("remote", "anywhere", "distributed", "work from home", "wfh")
HYBRID = ("hybrid", "flexible location")


def classify_remote(location: str | None, title: str | None = None, native_flag=None) -> str:
    """Derive remote_type. Platform-native flags win over keyword matching."""
    if native_flag is True:
        return "fully_remote"

    haystack = " ".join(p for p in (location, title) if p).lower()
    if not haystack.strip():
        return "unknown"

    if any(k in haystack for k in HYBRID):
        return "hybrid"
    if any(k in haystack for k in FULLY_REMOTE):
        return "fully_remote"
    # A named place with no remote keyword reads as onsite.
    if location and location.strip():
        return "onsite"
    return "unknown"


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    return re.sub(r"\s+", " ", text).strip()


async def _get_json(client: httpx.AsyncClient, url: str):
    resp = await client.get(url, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


async def scrape_greenhouse(client, token: str) -> list[dict]:
    data = await _get_json(
        client, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    )
    out = []
    for job in data.get("jobs", []):
        location = (job.get("location") or {}).get("name")
        out.append(
            {
                "external_id": str(job.get("id")),
                "title": job.get("title", ""),
                "location": location,
                "url": job.get("absolute_url"),
                "description": _strip_html(job.get("content")),
                "remote_type": classify_remote(location, job.get("title")),
            }
        )
    return out


async def scrape_lever(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for job in data:
        categories = job.get("categories") or {}
        location = categories.get("location")
        workplace = (job.get("workplaceType") or "").lower()
        native = True if workplace == "remote" else None
        out.append(
            {
                "external_id": str(job.get("id")),
                "title": job.get("text", ""),
                "location": location,
                "url": job.get("hostedUrl"),
                "description": _strip_html(job.get("descriptionPlain") or job.get("description")),
                "remote_type": classify_remote(location, job.get("text"), native),
            }
        )
    return out


async def scrape_ashby(client, token: str) -> list[dict]:
    data = await _get_json(
        client,
        f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
    )
    out = []
    for job in data.get("jobs", []):
        location = job.get("location")
        out.append(
            {
                "external_id": str(job.get("id")),
                "title": job.get("title", ""),
                "location": location,
                "url": job.get("jobUrl"),
                "description": _strip_html(job.get("descriptionPlain") or job.get("descriptionHtml")),
                "remote_type": classify_remote(location, job.get("title"), job.get("isRemote")),
            }
        )
    return out


async def scrape_workable(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{token}")
    out = []
    for job in data.get("jobs", []):
        location = job.get("location") or {}
        if isinstance(location, dict):
            parts = [location.get("city"), location.get("region"), location.get("country")]
            loc_str = ", ".join(p for p in parts if p)
            native = location.get("telecommuting")
        else:
            loc_str, native = str(location), None
        out.append(
            {
                "external_id": str(job.get("id") or job.get("shortcode")),
                "title": job.get("title", ""),
                "location": loc_str,
                "url": job.get("url") or job.get("application_url"),
                "description": _strip_html(job.get("description")),
                "remote_type": classify_remote(loc_str, job.get("title"), native),
            }
        )
    return out


async def scrape_smartrecruiters(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{token}/postings")
    out = []
    for job in data.get("content", []):
        location = job.get("location") or {}
        parts = [location.get("city"), location.get("region"), location.get("country")]
        loc_str = ", ".join(p for p in parts if p)
        out.append(
            {
                "external_id": str(job.get("id")),
                "title": job.get("name", ""),
                "location": loc_str,
                "url": job.get("ref") or job.get("applyUrl"),
                "description": "",
                "remote_type": classify_remote(loc_str, job.get("name"), location.get("remote")),
            }
        )
    return out


async def scrape_recruitee(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://{token}.recruitee.com/api/offers/")
    out = []
    for job in data.get("offers", []):
        location = job.get("location")
        out.append(
            {
                "external_id": str(job.get("id")),
                "title": job.get("title", ""),
                "location": location,
                "url": job.get("careers_url") or job.get("careers_apply_url"),
                "description": _strip_html(job.get("description")),
                "remote_type": classify_remote(location, job.get("title"), job.get("remote")),
            }
        )
    return out


SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "workable": scrape_workable,
    "smartrecruiters": scrape_smartrecruiters,
    "recruitee": scrape_recruitee,
}


async def scrape_company(client: httpx.AsyncClient, platform: str, token: str) -> list[dict]:
    scraper = SCRAPERS.get(platform)
    if scraper is None:
        log.warning("unknown platform %r", platform)
        return []
    try:
        return await scraper(client, token)
    except httpx.HTTPError as exc:
        log.warning("scrape failed for %s/%s: %s", platform, token, exc)
        return []
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("could not parse response for %s/%s: %s", platform, token, exc)
        return []
