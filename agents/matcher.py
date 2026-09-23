"""Score a job against the user's profile."""

import json
import logging

from llm import gemini

log = logging.getLogger(__name__)

PROMPT = """You are screening a job for a candidate. Judge fit honestly -- a low
score is more useful than a generous one.

--- CANDIDATE ---
Resume: {resume}
Interests: {interests}
Preferred domains: {domains}
Salary expectation: {salary}
Experience range sought: {experience}

--- JOB ---
Title: {title}
Company: {company}
Location: {location} (classified: {remote_type})
Description: {description}

--- COMPANY PROFILE ---
{company_profile}

Return strict JSON:
{{
  "match_score": 0-100,
  "reasoning": "2-3 sentences",
  "matched_skills": [string],
  "concerns": [string]
}}

Concerns should name concrete mismatches, e.g. "salary range not listed" or
"requires 8+ yrs, resume shows 4".
"""


def _fmt(value) -> str:
    if value in (None, "", [], {}):
        return "not specified"
    if isinstance(value, (dict, list)):
        return json.dumps(value)[:4000]
    return str(value)


def passes_prefilter(job: dict, settings: dict) -> bool:
    """Cheap hard-constraint check so we don't spend a call on hopeless jobs."""
    if settings.get("remote_only") and job.get("remote_type") == "onsite":
        return False
    return True


def has_profile(settings: dict) -> bool:
    return bool(settings.get("resume_structured") or settings.get("interests"))


async def score_job(job: dict, settings: dict, company_profile: dict | None) -> dict | None:
    if not gemini.available():
        log.warning("no Gemini key configured; skipping match scoring")
        return None

    salary = "not specified"
    if settings.get("salary_min") or settings.get("salary_max"):
        currency = settings.get("salary_currency") or "USD"
        salary = f"{settings.get('salary_min') or '?'}-{settings.get('salary_max') or '?'} {currency}"

    experience = "not specified"
    if settings.get("min_years_experience") or settings.get("max_years_experience"):
        experience = (
            f"{settings.get('min_years_experience') or 0}-"
            f"{settings.get('max_years_experience') or '?'} years"
        )

    profile_text = "no research available"
    if company_profile:
        profile_text = _fmt(
            {
                "summary": company_profile.get("summary"),
                "funding_stage": company_profile.get("funding_stage"),
                "employee_count_estimate": company_profile.get("employee_count_estimate"),
            }
        )

    prompt = PROMPT.format(
        resume=_fmt(settings.get("resume_structured")),
        interests=_fmt(settings.get("interests")),
        domains=_fmt(settings.get("domains")),
        salary=salary,
        experience=experience,
        title=job.get("title"),
        company=job.get("company_name"),
        location=_fmt(job.get("location")),
        remote_type=job.get("remote_type"),
        description=_fmt(job.get("description"))[:8000],
        company_profile=profile_text,
    )

    try:
        result = await gemini.generate_json(prompt)
    except (ValueError, gemini.LLMUnavailable) as exc:
        log.warning("scoring failed for job %s: %s", job.get("id"), exc)
        return None

    if not isinstance(result, dict):
        return None

    score = result.get("match_score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        return None

    return {
        "match_score": score,
        "reasoning": str(result.get("reasoning") or "")[:2000],
        "matched_skills": result.get("matched_skills") or [],
        "concerns": result.get("concerns") or [],
    }
