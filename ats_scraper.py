"""Per-platform ATS scrapers.

Each scraper returns normalised dicts:
    {external_id, title, location, url, description, department, remote_type}

and raises ScrapeError on failure so the worker can record why a board is
broken. Scrapers only fetch and normalise -- persistence, engineering
classification and queueing live in scraper_worker.py.

Field paths were verified against live boards; comments flag the non-obvious
ones (a wrong path silently yields empty data rather than an error).
"""

import html
import logging
import re
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(25.0)
HEADERS = {"User-Agent": "JobRadar/1.0 (personal job search tool)", "Accept": "application/json"}
# Some boards (Workday, SmartRecruiters) list thousands of roles; cap per company
# so one giant employer can't dominate a pass.
MAX_POSTINGS = 1000

FULLY_REMOTE = ("remote", "anywhere", "distributed", "work from home", "wfh")
HYBRID = ("hybrid", "flexible location")


class ScrapeError(RuntimeError):
    """A board could not be fetched or parsed."""


PLATFORMS = {
    "greenhouse": {"name": "Greenhouse", "example": "job-boards.greenhouse.io/{token}", "method": "Public job board API"},
    "lever": {"name": "Lever", "example": "jobs.lever.co/{token}", "method": "Public postings API"},
    "ashby": {"name": "Ashby", "example": "jobs.ashbyhq.com/{token}", "method": "Public job board API"},
    "workable": {"name": "Workable", "example": "apply.workable.com/{token}", "method": "Public widget API"},
    "smartrecruiters": {"name": "SmartRecruiters", "example": "jobs.smartrecruiters.com/{token}", "method": "Public postings API (paginated)"},
    "recruitee": {"name": "Recruitee", "example": "{token}.recruitee.com", "method": "Public offers API"},
    "workday": {"name": "Workday", "example": "{tenant}.wd5.myworkdayjobs.com/{site}", "method": "Careers site JSON API (paginated)"},
    "bamboohr": {"name": "BambooHR", "example": "{token}.bamboohr.com/careers", "method": "Careers list JSON"},
    "breezy": {"name": "Breezy HR", "example": "{token}.breezy.hr", "method": "Public JSON feed"},
    "personio": {"name": "Personio", "example": "{token}.jobs.personio.de", "method": "Public XML feed"},
    "pinpoint": {"name": "Pinpoint", "example": "{token}.pinpointhq.com", "method": "Public postings JSON"},
    "rippling": {"name": "Rippling", "example": "ats.rippling.com/{token}", "method": "Public board API"},
    "teamtailor": {"name": "Teamtailor", "example": "{token}.teamtailor.com", "method": "Public RSS feed"},
    "gem": {"name": "Gem", "example": "jobs.gem.com/{token}", "method": "Public job board API"},
    "polymer": {"name": "Polymer", "example": "jobs.polymer.co/{token}", "method": "Public jobs API"},
    "jazzhr": {"name": "JazzHR", "example": "{token}.applytojob.com", "method": "Public XML export feed"},
    "dover": {"name": "Dover", "example": "app.dover.com/apply/{token}", "method": "Public careers page API"},
}


def classify_remote(location: str | None, title: str | None = None, native=None) -> str:
    """Derive remote_type. A platform-native signal wins over keyword matching.

    `native` may be True (fully remote) or one of 'remote' / 'hybrid' / 'onsite'.
    """
    if native is True or native == "remote":
        return "fully_remote"
    if native == "hybrid":
        return "hybrid"
    if native == "onsite":
        return "onsite"

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


def _workplace(value) -> str | None:
    """Normalise the many spellings of workplace type to remote/hybrid/onsite."""
    if not value:
        return None
    v = str(value).lower().replace("-", "").replace("_", "").replace(" ", "")
    if v in ("remote", "fullyremote", "fully", "remotelocation"):
        return "remote"
    if v == "hybrid":
        return "hybrid"
    if v in ("onsite", "office", "inoffice"):
        return "onsite"
    return None


def _strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)  # Greenhouse double-escapes: &lt;div&gt; -> <div>
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _posting(external_id, title, location=None, url=None, description=None,
             department=None, native=None) -> dict:
    location = location or None
    return {
        "external_id": str(external_id),
        "title": (title or "").strip(),
        "location": location,
        "url": url,
        "description": _strip_html(description),
        "department": (department or "").strip() or None,
        "remote_type": classify_remote(location, title, native),
    }


async def _get(client: httpx.AsyncClient, url: str, **kw) -> httpx.Response:
    resp = await client.get(url, timeout=TIMEOUT, headers=kw.pop("headers", HEADERS), **kw)
    if resp.status_code == 404:
        raise ScrapeError("board not found (404)")
    resp.raise_for_status()
    return resp


async def _get_json(client, url: str, **kw):
    resp = await _get(client, url, **kw)
    try:
        return resp.json()
    except ValueError as exc:
        raise ScrapeError("board did not return JSON (unknown token?)") from exc


# --- the original six ------------------------------------------------------


async def scrape_greenhouse(client, token: str) -> list[dict]:
    # content=true is required: without it departments/offices/content are absent.
    data = await _get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for job in data.get("jobs", []):
        departments = job.get("departments") or []
        out.append(_posting(
            job.get("id"), job.get("title"),
            location=(job.get("location") or {}).get("name"),
            url=job.get("absolute_url"),
            description=job.get("content"),
            department=departments[0].get("name") if departments else None,
        ))
    return out


async def scrape_lever(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://api.lever.co/v0/postings/{token}?mode=json")
    if isinstance(data, dict):  # {"ok": false, "error": "Document not found"}
        raise ScrapeError(data.get("error") or "board not found")
    out = []
    for job in data:
        cats = job.get("categories") or {}
        out.append(_posting(
            job.get("id"), job.get("text"),
            location=cats.get("location"),
            url=job.get("hostedUrl"),
            description=" ".join(filter(None, (job.get("descriptionPlain"), job.get("additionalPlain")))),
            # Some boards (e.g. Palantir) have no department, only team.
            department=cats.get("department") or cats.get("team"),
            native=_workplace(job.get("workplaceType")),
        ))
    return out


async def scrape_ashby(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    out = []
    for job in data.get("jobs", []):
        if job.get("isListed") is False:
            continue
        department, team = job.get("department"), job.get("team")
        out.append(_posting(
            job.get("id"), job.get("title"),
            location=job.get("location"),
            url=job.get("jobUrl"),
            description=job.get("descriptionPlain") or job.get("descriptionHtml"),
            # Boards file Engineering under either field (Linear: dept "Product",
            # team "Engineering"), so keep both for the classifier.
            department=" / ".join(dict.fromkeys(filter(None, (department, team)))),
            # isRemote is true for Hybrid jobs too -- workplaceType is authoritative.
            native=_workplace(job.get("workplaceType")),
        ))
    return out


async def scrape_workable(client, token: str) -> list[dict]:
    # details=true is required for descriptions.
    data = await _get_json(client, f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    out = []
    for job in data.get("jobs", []):
        loc = ", ".join(p for p in (job.get("city"), job.get("state"), job.get("country")) if p)
        out.append(_posting(
            job.get("shortcode") or job.get("id"), job.get("title"),
            location=loc,
            url=job.get("url") or job.get("application_url"),
            description=job.get("description"),
            department=job.get("department") or job.get("function"),
            native=True if job.get("telecommuting") else None,
        ))
    return out


async def scrape_smartrecruiters(client, token: str) -> list[dict]:
    out, offset, total = [], 0, None
    while total is None or (offset < total and offset < MAX_POSTINGS):
        data = await _get_json(
            client,
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params={"limit": 100, "offset": offset},
        )
        total = data.get("totalFound") or 0
        page = data.get("content", [])
        for job in page:
            loc = job.get("location") or {}
            loc_str = ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)
            native = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else None
            # department is usually {} -- function.label carries "Engineering".
            department = (job.get("department") or {}).get("label") or (job.get("function") or {}).get("label")
            out.append(_posting(
                job.get("id"), job.get("name"),
                location=loc_str,
                # The list has no public URL, only the API `ref`.
                url=f"https://jobs.smartrecruiters.com/{token}/{job.get('id')}",
                department=department,
                native=native,
            ))
        if not page:
            break
        offset += len(page)
    return out


async def scrape_recruitee(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://{token}.recruitee.com/api/offers/")
    out = []
    for job in data.get("offers", []):
        native = "remote" if job.get("remote") else "hybrid" if job.get("hybrid") else (
            "onsite" if job.get("on_site") else None
        )
        out.append(_posting(
            job.get("id"), job.get("title"),
            location=job.get("location"),
            url=job.get("careers_url"),
            description=" ".join(filter(None, (job.get("description"), job.get("requirements")))),
            department=job.get("department"),
            native=native,
        ))
    return out


# --- additional platforms -------------------------------------------------


async def scrape_workday(client, token: str) -> list[dict]:
    """token = "tenant/wdN/site", e.g. "nvidia/wd5/NVIDIAExternalCareerSite"."""
    try:
        tenant, wd, site = token.split("/")
    except ValueError as exc:
        raise ScrapeError("Workday token must be tenant/wdN/site") from exc
    host = f"https://{tenant}.{wd}.myworkdayjobs.com"
    endpoint = f"{host}/wday/cxs/{tenant}/{site}/jobs"

    out, offset, total = [], 0, None
    while total is None or (offset < total and offset < MAX_POSTINGS):
        # limit > 20 is rejected with a 400.
        resp = await client.post(
            endpoint,
            json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""},
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if resp.status_code in (404, 422):
            raise ScrapeError(f"Workday site not found ({resp.status_code})")
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data.get("total") or 0  # only correct on the first page
        page = data.get("jobPostings") or []
        for job in page:
            path = job.get("externalPath") or ""
            bullets = job.get("bulletFields") or []
            out.append(_posting(
                bullets[0] if bullets else path, job.get("title"),
                location=job.get("locationsText"),
                url=f"{host}/{site}{path}" if path else None,
                native=_workplace(job.get("remoteType")),
            ))
        if not page:
            break
        offset += len(page)
    return out


async def scrape_bamboohr(client, token: str) -> list[dict]:
    resp = await client.get(
        f"https://{token}.bamboohr.com/careers/list",
        headers=HEADERS, timeout=TIMEOUT, follow_redirects=False,
    )
    # Unknown accounts redirect rather than 404.
    if resp.is_redirect or resp.status_code == 404:
        raise ScrapeError("BambooHR account not found")
    resp.raise_for_status()
    data = resp.json()
    out = []
    for job in data.get("result", []):
        loc = job.get("location") or {}
        loc_str = ", ".join(p for p in (loc.get("city"), loc.get("state")) if p)
        out.append(_posting(
            job.get("id"), job.get("jobOpeningName"),
            location=loc_str,
            url=f"https://{token}.bamboohr.com/careers/{job.get('id')}",
            department=job.get("departmentLabel"),
            native=True if job.get("isRemote") else None,
        ))
    return out


async def scrape_breezy(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://{token}.breezy.hr/json")
    if not isinstance(data, list):
        raise ScrapeError("Breezy board not found")
    out = []
    for job in data:
        loc = job.get("location") or {}
        remote_detail = ((loc.get("remote_details") or {}).get("value") or "").lower()
        native = "remote" if loc.get("is_remote") or remote_detail.startswith("remote") else None
        out.append(_posting(
            job.get("id"), job.get("name"),
            location=loc.get("name"),
            url=job.get("url"),
            department=job.get("department"),
            native=native,
        ))
    return out


async def scrape_personio(client, token: str) -> list[dict]:
    resp = await _get(
        client, f"https://{token}.jobs.personio.de/xml?language=en",
        headers={**HEADERS, "Accept": "application/xml"},
    )
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise ScrapeError("Personio feed was not valid XML") from exc
    out = []
    for pos in root.iter("position"):
        text = lambda tag: (pos.findtext(tag) or "").strip() or None  # noqa: E731
        description = " ".join(
            (d.findtext("value") or "") for d in pos.iter("jobDescription")
        )
        job_id = text("id")
        out.append(_posting(
            job_id, text("name"),
            location=text("office"),  # free text, e.g. "Remote (UTC +/- 2 hrs)"
            url=f"https://{token}.jobs.personio.de/job/{job_id}",
            description=description,
            department=text("department") or text("recruitingCategory"),
        ))
    return out


async def scrape_pinpoint(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://{token}.pinpointhq.com/postings.json")
    out = []
    for job in data.get("data", []):
        loc = job.get("location") or {}
        department = ((job.get("job") or {}).get("department") or {}).get("name")
        out.append(_posting(
            job.get("id"), job.get("title"),
            location=loc.get("name"),
            url=job.get("url"),
            description=" ".join(filter(None, (
                job.get("description"), job.get("key_responsibilities"), job.get("skills_knowledge_expertise"),
            ))),
            department=department,
            native=_workplace(job.get("workplace_type")),
        ))
    return out


async def scrape_rippling(client, token: str) -> list[dict]:
    out, page, pages = [], 0, None
    while pages is None or (page < pages and len(out) < MAX_POSTINGS):
        data = await _get_json(
            client, f"https://ats.rippling.com/api/v2/board/{token}/jobs",
            params={"page": page, "pageSize": 100},
        )
        pages = data.get("totalPages") or 0
        for job in data.get("items", []):
            locations = job.get("locations") or []
            first = locations[0] if locations else {}
            out.append(_posting(
                job.get("id"), job.get("name"),
                location="; ".join(l.get("name") for l in locations if l.get("name")),
                url=job.get("url"),
                department=(job.get("department") or {}).get("name"),
                native=_workplace(first.get("workplaceType")),
            ))
        page += 1
    return out


async def scrape_teamtailor(client, token: str) -> list[dict]:
    """token is the subdomain, including any region, e.g. "iqm" or "commonsku.na"."""
    resp = await _get(
        client, f"https://{token}.teamtailor.com/jobs.rss",
        headers={**HEADERS, "Accept": "application/rss+xml"},
    )
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise ScrapeError("Teamtailor feed was not valid XML") from exc
    ns = "{https://teamtailor.com/locations}"
    out = []
    for item in root.iter("item"):
        location = item.find(f".//{ns}location")
        loc_str = None
        if location is not None:
            loc_str = ", ".join(
                p for p in (location.findtext(f"{ns}city"), location.findtext(f"{ns}country")) if p
            ) or location.findtext(f"{ns}name")
        out.append(_posting(
            item.findtext("guid") or item.findtext("link"), item.findtext("title"),
            location=loc_str,
            url=item.findtext("link"),
            description=item.findtext("description"),
            department=item.findtext(f"{ns}department"),
            native=_workplace(item.findtext("remoteStatus")),
        ))
    return out


async def scrape_gem(client, token: str) -> list[dict]:
    data = await _get_json(client, f"https://api.gem.com/job_board/v0/{token}/job_posts/")
    jobs = data if isinstance(data, list) else data.get("job_posts") or data.get("jobs") or []
    out = []
    for job in jobs:
        departments = job.get("departments") or []
        out.append(_posting(
            job.get("id"), job.get("title"),
            location=(job.get("location") or {}).get("name"),
            url=job.get("absolute_url"),
            description=job.get("content_plain") or job.get("content"),
            department=departments[0].get("name") if departments else None,
            native=_workplace(job.get("location_type")),
        ))
    return out


async def scrape_polymer(client, token: str) -> list[dict]:
    out, page = [], 1
    while len(out) < MAX_POSTINGS:
        data = await _get_json(
            client, f"https://api.polymer.co/v1/hire/organizations/{token}/jobs", params={"page": page}
        )
        for job in data.get("items", []):
            remoteness = (job.get("remoteness_pretty") or "").lower()
            out.append(_posting(
                job.get("id"), job.get("title"),
                location=job.get("display_location"),
                url=job.get("job_post_url"),
                # department is null in practice; job_category_name carries it.
                department=job.get("job_category_name"),
                native="remote" if "remote" in remoteness else "hybrid" if "hybrid" in remoteness else None,
            ))
        meta = data.get("meta") or {}
        if meta.get("is_last", True) or not meta.get("next_page"):
            break
        page = meta["next_page"]
    return out


async def scrape_jazzhr(client, token: str) -> list[dict]:
    resp = await _get(
        client, f"https://app.jazz.co/feeds/export/jobs/{token}",
        headers={**HEADERS, "Accept": "application/xml"},
    )
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise ScrapeError("JazzHR feed was not valid XML") from exc
    out = []
    for job in root.iter("job"):
        text = lambda tag: (job.findtext(tag) or "").strip() or None  # noqa: E731
        if (text("status") or "open").lower() not in ("open", "active"):
            continue
        loc = ", ".join(p for p in (text("city"), text("state"), text("country")) if p)
        out.append(_posting(
            text("id"), text("title"),
            location=loc,
            url=text("url"),
            description=text("description"),
            department=text("department"),
        ))
    return out


async def scrape_dover(client, token: str) -> list[dict]:
    page_info = await _get_json(client, f"https://app.dover.com/api/v1/careers-page-slug/{token}")
    page_id = page_info.get("id") if isinstance(page_info, dict) else None
    if not page_id:
        raise ScrapeError("Dover careers page not found")
    out, url = [], f"https://app.dover.com/api/v1/careers-page/{page_id}/jobs"
    while url and len(out) < MAX_POSTINGS:
        data = await _get_json(client, url)
        for job in data.get("results", []):
            locations = job.get("locations") or []
            first = locations[0] if locations else {}
            out.append(_posting(
                job.get("id"), job.get("title"),
                location="; ".join(filter(None, ((l.get("name") or l.get("location_name")) for l in locations))) or None,
                url=f"https://app.dover.com/apply/{token}/{job.get('id')}",
                native=_workplace(job.get("workplace_type") or first.get("location_type")),
            ))
        url = data.get("next")
    return out


SCRAPERS = {
    "greenhouse": scrape_greenhouse,
    "lever": scrape_lever,
    "ashby": scrape_ashby,
    "workable": scrape_workable,
    "smartrecruiters": scrape_smartrecruiters,
    "recruitee": scrape_recruitee,
    "workday": scrape_workday,
    "bamboohr": scrape_bamboohr,
    "breezy": scrape_breezy,
    "personio": scrape_personio,
    "pinpoint": scrape_pinpoint,
    "rippling": scrape_rippling,
    "teamtailor": scrape_teamtailor,
    "gem": scrape_gem,
    "polymer": scrape_polymer,
    "jazzhr": scrape_jazzhr,
    "dover": scrape_dover,
}


async def _smartrecruiters_detail(client, token, job_id):
    data = await _get_json(client, f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}")
    sections = ((data.get("jobAd") or {}).get("sections") or {}).values()
    return " ".join(s.get("text") or "" for s in sections if isinstance(s, dict))


async def _bamboohr_detail(client, token, job_id):
    data = await _get_json(client, f"https://{token}.bamboohr.com/careers/{job_id}/detail")
    return ((data.get("result") or {}).get("jobOpening") or {}).get("description")


async def _rippling_detail(client, token, job_id):
    data = await _get_json(client, f"https://api.rippling.com/platform/api/ats/v1/board/{token}/jobs/{job_id}")
    desc = data.get("description") or {}
    return " ".join(filter(None, (desc.get("company"), desc.get("role")))) if isinstance(desc, dict) else desc


async def _dover_detail(client, token, job_id):
    data = await _get_json(client, f"https://app.dover.com/api/v1/inbound/application-portal-job/{job_id}")
    return data.get("user_provided_description")


_DETAIL_FETCHERS = {
    "smartrecruiters": _smartrecruiters_detail,
    "bamboohr": _bamboohr_detail,
    "rippling": _rippling_detail,
    "dover": _dover_detail,
}


async def fetch_description(platform: str, token: str, external_id: str) -> str | None:
    """Per-job description for platforms whose list endpoint omits it and whose
    hosted pages are JS-rendered. Returns None when there's no detail API or it
    fails; callers then fall back to fetching the job page."""
    fetcher = _DETAIL_FETCHERS.get(platform)
    if fetcher is None:
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            text = _strip_html(await fetcher(client, token, external_id))
    except (ScrapeError, httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        log.debug("detail fetch failed %s/%s/%s: %s", platform, token, external_id, exc)
        return None
    return text or None


async def scrape_company(client: httpx.AsyncClient, platform: str, token: str) -> list[dict]:
    """Scrape one board. Raises ScrapeError with a human-readable reason."""
    scraper = SCRAPERS.get(platform)
    if scraper is None:
        raise ScrapeError(f"unsupported platform {platform!r}")
    try:
        postings = await scraper(client, token)
    except ScrapeError:
        raise
    except httpx.HTTPStatusError as exc:
        raise ScrapeError(f"HTTP {exc.response.status_code} from {platform}") from exc
    except httpx.HTTPError as exc:
        raise ScrapeError(f"network error: {type(exc).__name__}") from exc
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ScrapeError(f"unexpected response shape from {platform}: {exc}") from exc
    return [p for p in postings[:MAX_POSTINGS] if p["title"]]
