"""Pluggable, ToS-compliant email enrichment.

One provider (Hunter) behind a `find_email(name, domain)` interface so others
can be swapped in. Missing key disables enrichment; it never raises.
No SMTP probing, no brute-force verification.
"""

import logging

import httpx

from config import HUNTER_API_KEY

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(HUNTER_API_KEY)


async def find_email(name: str, domain: str) -> tuple[str | None, str]:
    """Return (email, confidence) where confidence is 'verified' or 'none'."""
    if not HUNTER_API_KEY or not domain or not name:
        return None, "none"

    parts = name.strip().split()
    if len(parts) < 2:
        return None, "none"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.hunter.io/v2/email-finder",
                params={
                    "domain": domain,
                    "first_name": parts[0],
                    "last_name": parts[-1],
                    "api_key": HUNTER_API_KEY,
                },
                timeout=15.0,
            )
            if resp.status_code != 200:
                return None, "none"
            data = resp.json().get("data") or {}
            email = data.get("email")
            return (email, "verified") if email else (None, "none")
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("email enrichment failed for %s: %s", name, exc)
        return None, "none"


def guess_from_pattern(name: str, pattern: str | None, domain: str | None) -> str | None:
    """Apply a publicly evidenced company email pattern to a name.

    Only called when a real address at the domain was observed in research
    sources. Results are always stored with confidence='guessed'.
    """
    if not pattern or not domain or not name:
        return None
    parts = name.strip().lower().split()
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]

    local = {
        "first.last": f"{first}.{last}",
        "firstlast": f"{first}{last}",
        "first": first,
        "flast": f"{first[0]}{last}",
        "first_last": f"{first}_{last}",
    }.get(pattern)

    return f"{local}@{domain}" if local else None
