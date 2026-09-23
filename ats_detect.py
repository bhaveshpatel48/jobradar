"""Find a company's job board.

Resolves a pasted URL -- or links embedded in a company's own careers page --
to (platform, token), then *probes* the board to confirm it really lists jobs.
Nothing is reported as found without a successful live scrape.

Most company careers pages are JS-rendered and carry no JobPosting JSON-LD,
but many embed or link their ATS board in raw HTML (e.g. Figma links
boards.greenhouse.io/figma), which is what the marker scan relies on.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

import sitefetch
from ats_scraper import SCRAPERS, ScrapeError, scrape_company

log = logging.getLogger(__name__)

# Tokens that URL patterns can capture but that are never real boards.
_NOT_TOKENS = {"www", "embed", "v1", "api", "jobs", "careers", "careers-analytics", "app", "j", "static", "assets"}

# (platform, regex, token builder). Order matters: more specific first.
_PATTERNS: list[tuple[str, re.Pattern, callable]] = [
    ("workday", re.compile(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)"),
     lambda m: f"{m[1]}/{m[2]}/{m[3]}" if m[3].lower() != "wday" else None),
    ("greenhouse", re.compile(
        r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)?([\w-]+)"),
     lambda m: m[1]),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([\w.-]+)"), lambda m: m[1]),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([\w.-]+)"), lambda m: m[1]),
    ("workable", re.compile(r"apply\.workable\.com/(?!j/)([\w-]+)"), lambda m: m[1]),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)"), lambda m: m[1]),
    ("recruitee", re.compile(r"([\w-]+)\.recruitee\.com"), lambda m: m[1]),
    ("bamboohr", re.compile(r"([\w-]+)\.bamboohr\.com"), lambda m: m[1]),
    ("breezy", re.compile(r"([\w-]+)\.breezy\.hr"), lambda m: m[1]),
    ("personio", re.compile(r"([\w-]+)\.jobs\.personio\.(?:de|com)"), lambda m: m[1]),
    ("pinpoint", re.compile(r"([\w-]+)\.pinpointhq\.com"), lambda m: m[1]),
    ("rippling", re.compile(r"ats\.rippling\.com/([\w-]+)"), lambda m: m[1]),
    ("teamtailor", re.compile(r"([\w-]+)((?:\.na)?)\.teamtailor\.com"), lambda m: f"{m[1]}{m[2]}"),
    ("gem", re.compile(r"jobs\.gem\.com/([\w-]+)"), lambda m: m[1]),
    ("polymer", re.compile(r"jobs\.polymer\.co/([\w-]+)"), lambda m: m[1]),
    ("jazzhr", re.compile(r"([\w-]+)\.applytojob\.com"), lambda m: m[1]),
    ("dover", re.compile(r"app\.dover\.com/(?:apply|jobs)/([\w-]+)"), lambda m: m[1]),
]

# Platforms that can be probed from a bare slug (Workday needs tenant/wdN/site).
PROBEABLE = [p for p in SCRAPERS if p != "workday"]
# Cheap slug-guessing order when nothing else points at a board.
_GUESS_PLATFORMS = ("greenhouse", "ashby", "lever")
CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/company/careers", "/about/careers", "")


@dataclass
class Detected:
    platform: str
    token: str
    job_count: int
    careers_url: str | None = None
    name_hint: str | None = None


def find_boards(text: str) -> list[tuple[str, str]]:
    """Every (platform, token) referenced anywhere in a URL or HTML blob."""
    found: list[tuple[str, str]] = []
    for platform, pattern, build in _PATTERNS:
        for match in pattern.finditer(text):
            token = build(match)
            if not token or token.lower() in _NOT_TOKENS:
                continue
            pair = (platform, token)
            if pair not in found:
                found.append(pair)
    return found


def parse_url(url: str) -> tuple[str, str] | None:
    boards = find_boards(url)
    return boards[0] if boards else None


def _pretty(token: str) -> str:
    base = token.split("/")[0].split(".")[0]
    return re.sub(r"[-_]+", " ", base).strip().title() or token


async def probe(client: httpx.AsyncClient, platform: str, token: str) -> int | None:
    """Return the board's job count, or None if it doesn't exist."""
    try:
        return len(await scrape_company(client, platform, token))
    except ScrapeError as exc:
        log.debug("probe %s/%s failed: %s", platform, token, exc)
        return None


async def _first_live(client, candidates, *, require_jobs: bool, careers_url=None) -> Detected | None:
    for platform, token in candidates:
        count = await probe(client, platform, token)
        if count is None or (require_jobs and count == 0):
            continue
        return Detected(platform, token, count, careers_url, _pretty(token))
    return None


async def _scan_pages(client, base_urls: list[str]) -> tuple[list[tuple[str, str]], str | None]:
    """Fetch candidate careers pages and collect every ATS board they reference."""
    results = await asyncio.gather(*(sitefetch.fetch_html(client, u) for u in base_urls), return_exceptions=True)
    boards: list[tuple[str, str]] = []
    careers_url = None
    for result in results:
        if not isinstance(result, tuple):
            continue
        final_url, raw = result
        # The final URL may itself be an ATS board after a redirect.
        page_boards = find_boards(final_url) + find_boards(raw)
        if page_boards and careers_url is None:
            careers_url = final_url
        for pair in page_boards:
            if pair not in boards:
                boards.append(pair)
    return boards, careers_url


async def detect_from_url(url: str) -> Detected | None:
    """Resolve a user-pasted careers/job-board URL. Accepts empty boards."""
    url = url.strip()
    if "//" not in url:
        url = f"https://{url}"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        direct = parse_url(url)
        if direct:
            found = await _first_live(client, [direct], require_jobs=False, careers_url=url)
            if found:
                return found

        base = sitefetch.normalize_website(url)
        pages = [url] + ([base + p for p in CAREERS_PATHS] if base else [])
        boards, careers_url = await _scan_pages(client, list(dict.fromkeys(pages)))
        found = await _first_live(client, boards, require_jobs=False, careers_url=careers_url or url)
        if found and base:
            found.name_hint = _pretty(urlparse(base).netloc.removeprefix("www.").split(".")[0])
        return found


def _slug_variants(name: str, website: str | None) -> list[str]:
    variants = []
    words = re.findall(r"[a-z0-9]+", name.lower())
    if words:
        variants += ["".join(words), "-".join(words)]
    if website:
        host = urlparse(website if "//" in website else f"https://{website}").netloc.lower()
        root = host.removeprefix("www.").split(".")[0]
        if root:
            variants.append(root)
    return list(dict.fromkeys(v for v in variants if v))


async def detect_for_company(
    client: httpx.AsyncClient,
    *,
    name: str,
    website: str | None,
    hint_platform: str | None = None,
    hint_token: str | None = None,
) -> Detected | None:
    """Locate a live board with at least one job. Used by AI discovery, where a
    board with no openings isn't worth tracking."""
    hint_platform = (hint_platform or "").strip().lower()
    hint_token = (hint_token or "").strip()
    if hint_platform in PROBEABLE and hint_token:
        found = await _first_live(client, [(hint_platform, hint_token)], require_jobs=True)
        if found:
            return found

    base = sitefetch.normalize_website(website)
    if base:
        boards, careers_url = await _scan_pages(client, [base + p for p in CAREERS_PATHS])
        found = await _first_live(client, boards, require_jobs=True, careers_url=careers_url)
        if found:
            return found

    guesses = [(p, slug) for slug in _slug_variants(name, website) for p in _GUESS_PLATFORMS]
    return await _first_live(client, guesses, require_jobs=True)
