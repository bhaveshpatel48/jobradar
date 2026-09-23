"""JobRadar API.

Serves JSON only -- the Vue SPA is a separate service. Under Docker this process
does not run the scraper or agent loops; dedicated worker containers own those.
"""

import asyncio
import json
import logging
import sqlite3
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

import ats_detect
import scoring
import task_queue as job_queue
from agents import job_search, matcher, resume_parser
from ats_scraper import PLATFORMS, SCRAPERS
from config import API_PORT, CORS_ORIGINS, RUN_SCRAPER_IN_APP
from db import get_conn, get_settings, init_db
from llm import gemini

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB resume cap
CORS(app, origins=CORS_ORIGINS)

JSON_FIELDS = ("resume_structured", "interests", "domains")


def _decode(row: dict, fields: tuple) -> dict:
    for field in fields:
        raw = row.get(field)
        if raw:
            try:
                row[field] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                row[field] = None
    return row


@app.get("/api/health")
def health():
    reset_at = gemini.quota_reset_at()
    return jsonify(
        {
            "status": "ok",
            "queue": job_queue.stats(),
            "gemini": {
                "configured": gemini.available(),
                "quota_exhausted": reset_at is not None,
                "quota_resets_at": reset_at,
                "grounding": gemini.grounding_available(),
            },
        }
    )


@app.post("/api/llm/quota/clear")
def clear_quota():
    gemini.clear_quota_flag()
    return jsonify({"ok": True})


def _flag(args, name: str, default: bool) -> bool:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).lower() in ("1", "true", "yes", "on")


# Columns returned to the UI. `description` is deliberately excluded: it can be
# ~8 KB per job and no list view shows it.
JOB_LIST_COLUMNS = """
    j.id, j.company_id, j.external_id, j.title, j.location, j.url, j.status,
    j.remote_type, j.department, j.is_engineering,
    j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.salary_text,
    j.match_score, j.match_reasoning, j.match_matched_skills, j.match_concerns, j.matched_at,
    j.first_seen_at, j.last_seen_at,
    c.name AS company_name, c.website AS company_website, c.platform AS company_platform,
    EXISTS (SELECT 1 FROM job_queue q WHERE q.job_id = j.id
            AND q.status IN ('pending', 'in_progress')) AS score_pending
"""
JOB_JSON_FIELDS = ("match_matched_skills", "match_concerns")

DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 20, 100
DEFAULT_COMPANIES_PER_PAGE, MAX_COMPANIES_PER_PAGE = 10, 50
DEFAULT_JOBS_PER_COMPANY, MAX_JOBS_PER_COMPANY = 5, 20


def _int_arg(args, name: str, default: int, maximum: int) -> int:
    try:
        value = int(args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _pagination(total: int, page: int, size: int) -> dict:
    total_pages = max(1, -(-total // size))
    return {"page": page, "page_size": size, "total": total, "total_pages": total_pages}


def _job_order(args, prefix: str = "j.") -> str:
    """ORDER BY body for jobs; `prefix` is "" inside the filtered CTE."""
    if args.get("sort") == "newest":
        return f"{prefix}first_seen_at DESC, {prefix}id DESC"
    return f"{prefix}match_score DESC NULLS LAST, {prefix}first_seen_at DESC, {prefix}id DESC"


def _job_filters(args) -> tuple[str, list, dict]:
    """The shared WHERE clause for every job listing, including AI search, so
    the AI only ever judges jobs the user could see anyway."""
    settings = get_settings()
    remote_only = _flag(args, "remote_only", bool(settings.get("remote_only")))
    engineering_only = _flag(args, "engineering_only", bool(settings.get("engineering_only")))
    scope = args.get("scope") or settings.get("scrape_scope") or "preferred"

    clauses, params = [], []
    if remote_only:
        # 'unknown' is surfaced deliberately -- the user judges those.
        clauses.append("j.remote_type IN ('fully_remote', 'unknown')")
    if engineering_only:
        clauses.append("j.is_engineering = 1")
    if scope == "preferred":
        clauses.append("c.preferred = 1")
    company_id = str(args.get("company_id") or "")
    if company_id.isdigit():
        clauses.append("j.company_id = ?")
        params.append(int(company_id))
    if args.get("platform"):
        clauses.append("c.platform = ?")
        params.append(args["platform"])
    if args.get("status"):
        clauses.append("j.status = ?")
        params.append(args["status"])
    if args.get("q"):
        clauses.append("(j.title LIKE ? OR c.name LIKE ? OR j.department LIKE ?)")
        params.extend([f"%{args['q']}%"] * 3)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    applied = {"remote_only": remote_only, "engineering_only": engineering_only, "scope": scope}
    return where, params, applied


def _llm_error_response(exc: gemini.LLMError):
    if isinstance(exc, gemini.LLMQuotaExhausted):
        return jsonify({"error": str(exc), "quota_resets_at": exc.reset_at}), 429
    if isinstance(exc, gemini.LLMTemporarilyUnavailable):
        return jsonify({"error": "Gemini is overloaded right now -- try again in a minute"}), 503
    if isinstance(exc, gemini.LLMRateLimited):
        return jsonify({"error": "Gemini rate limit reached -- try again in a minute"}), 429
    log.warning("Gemini call failed: %s", exc)
    return jsonify({"error": "The AI request failed; try again"}), 502


@app.get("/api/jobs")
def list_jobs():
    """Flat, paginated. Used for "show more" inside a company group."""
    where, params, applied = _job_filters(request.args)
    page = _int_arg(request.args, "page", 1, 10**6)
    size = _int_arg(request.args, "page_size", DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id = j.company_id {where}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT {JOB_LIST_COLUMNS} FROM jobs j JOIN companies c ON c.id = j.company_id
                {where} ORDER BY {_job_order(request.args)} LIMIT ? OFFSET ?""",
            [*params, size, (page - 1) * size],
        ).fetchall()
    jobs = [_decode(dict(r), JOB_JSON_FIELDS) for r in rows]
    return jsonify({"jobs": jobs, **_pagination(total, page, size), **applied})


@app.get("/api/jobs/grouped")
def list_jobs_grouped():
    """Jobs grouped by company, paginated by company.

    Each page holds `page_size` companies, each with its first
    `jobs_per_company` jobs; the UI pages further into a company through
    GET /api/jobs?company_id=... . Everything is sliced in SQL, so response
    size stays flat however many jobs are scraped.
    """
    args = request.args
    where, params, applied = _job_filters(args)
    page = _int_arg(args, "page", 1, 10**6)
    size = _int_arg(args, "page_size", DEFAULT_COMPANIES_PER_PAGE, MAX_COMPANIES_PER_PAGE)
    per_company = _int_arg(args, "jobs_per_company", DEFAULT_JOBS_PER_COMPANY, MAX_JOBS_PER_COMPANY)

    filtered = f"""filtered AS (
        SELECT {JOB_LIST_COLUMNS} FROM jobs j JOIN companies c ON c.id = j.company_id {where}
    )"""
    group_order = (
        "newest DESC, company_name COLLATE NOCASE"
        if args.get("sort") == "newest"
        else "best_score DESC NULLS LAST, newest DESC, company_name COLLATE NOCASE"
    )

    with get_conn() as conn:
        totals = conn.execute(
            f"""WITH {filtered}
                SELECT COUNT(DISTINCT company_id) AS companies, COUNT(*) AS jobs,
                       -- "unscored" = still needs a request: not scored, not already queued.
                       SUM(match_score IS NULL AND NOT score_pending) AS unscored,
                       SUM(score_pending) AS pending
                FROM filtered""",
            params,
        ).fetchone()
        groups = [
            dict(r)
            for r in conn.execute(
                f"""WITH {filtered}
                    SELECT company_id, company_name, company_platform, company_website,
                           COUNT(*) AS job_count, MAX(match_score) AS best_score,
                           MAX(first_seen_at) AS newest
                    FROM filtered GROUP BY company_id
                    ORDER BY {group_order} LIMIT ? OFFSET ?""",
                [*params, size, (page - 1) * size],
            )
        ]
        jobs_by_company: dict[int, list] = {g["company_id"]: [] for g in groups}
        if groups:
            marks = ",".join("?" * len(groups))
            rows = conn.execute(
                f"""WITH {filtered}
                    SELECT * FROM (
                        SELECT filtered.*, ROW_NUMBER() OVER (
                            PARTITION BY company_id ORDER BY {_job_order(args, prefix="")}
                        ) AS rn
                        FROM filtered WHERE company_id IN ({marks})
                    ) WHERE rn <= ? ORDER BY rn""",
                [*params, *jobs_by_company, per_company],
            ).fetchall()
            for row in rows:
                job = _decode(dict(row), JOB_JSON_FIELDS)
                job.pop("rn", None)
                jobs_by_company[job["company_id"]].append(job)

    for group in groups:
        group["jobs"] = jobs_by_company[group["company_id"]]
        group["has_more"] = group["job_count"] > len(group["jobs"])

    return jsonify(
        {
            "groups": groups,
            **_pagination(totals["companies"], page, size),
            "total_jobs": totals["jobs"],
            "unscored_jobs": totals["unscored"] or 0,
            "pending_scores": totals["pending"] or 0,
            "jobs_per_company": per_company,
            **applied,
        }
    )


def _group_by_company(jobs: list[dict], page: int, size: int) -> tuple[list[dict], int]:
    """Group an already-ranked job list by company and slice one page of groups."""
    groups: dict[int, dict] = {}
    for job in jobs:
        group = groups.setdefault(
            job["company_id"],
            {
                "company_id": job["company_id"],
                "company_name": job["company_name"],
                "company_platform": job["company_platform"],
                "company_website": job["company_website"],
                "jobs": [],
            },
        )
        group["jobs"].append(job)
    ordered = list(groups.values())  # insertion order = best job first
    for group in ordered:
        group["job_count"] = len(group["jobs"])
        group["has_more"] = False
    start = (page - 1) * size
    return ordered[start : start + size], len(ordered)


@app.post("/api/jobs/ai-search")
def ai_search_jobs():
    """Gemini filters the candidates; results come back grouped and paginated.
    Verdicts are cached per (query, job), so paging costs no further requests."""
    body = request.json or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "describe the jobs you want"}), 400
    if len(query) > 500:
        return jsonify({"error": "keep the request under 500 characters"}), 400
    if not gemini.available():
        return jsonify({"error": "AI search needs GEMINI_API_KEY"}), 503

    filters = {k: v for k, v in (body.get("filters") or {}).items() if v not in (None, "")}
    page = _int_arg(body, "page", 1, 10**6)
    size = _int_arg(body, "page_size", DEFAULT_COMPANIES_PER_PAGE, MAX_COMPANIES_PER_PAGE)
    where, params, applied = _job_filters(filters)

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT {JOB_LIST_COLUMNS}, j.description
                FROM jobs j JOIN companies c ON c.id = j.company_id
                {where} ORDER BY {_job_order(filters)} LIMIT ?""",
            [*params, job_search.MAX_JOBS + 1],
        ).fetchall()
    candidates = [_decode(dict(r), JOB_JSON_FIELDS) for r in rows]

    try:
        result = asyncio.run(job_search.ai_filter(query, candidates))
    except gemini.LLMError as exc:
        return _llm_error_response(exc)

    matched = []
    for job in candidates:
        verdict = result["matches"].get(job["id"])
        if verdict:
            job.pop("description", None)
            job["ai_relevance"] = verdict["relevance"]
            job["ai_reason"] = verdict["reason"]
            matched.append(job)
    matched.sort(key=lambda j: j["ai_relevance"], reverse=True)
    groups, total_companies = _group_by_company(matched, page, size)

    return jsonify(
        {
            "groups": groups,
            **_pagination(total_companies, page, size),
            "total_jobs": len(matched),
            "judged": result["judged"],
            "truncated": result["truncated"],
            "partial": result["partial"],
            **applied,
        }
    )


@app.post("/api/jobs/<int:job_id>/score")
def score_one_job(job_id: int):
    """Score one job now ("Check my fit"). Runs synchronously at interactive
    priority, using any existing company research -- it doesn't trigger new
    research, which would cost extra requests and time."""
    if not gemini.available():
        return jsonify({"error": "scoring needs GEMINI_API_KEY"}), 503
    job = scoring.load_job(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404

    from agents import company_research

    try:
        result = asyncio.run(
            scoring.score_and_store(
                job,
                scoring.decoded_settings(),
                company_research.get_profile(job["company_id"]),
                priority=gemini.INTERACTIVE,
            )
        )
    except scoring.NoProfile as exc:
        return jsonify({"error": str(exc)}), 400
    except gemini.LLMError as exc:
        return _llm_error_response(exc)
    if result is None:
        return jsonify({"error": "Gemini didn't return a usable score; try again"}), 502

    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {JOB_LIST_COLUMNS} FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.id = ?",
            (job_id,),
        ).fetchone()
    return jsonify({"job": _decode(dict(row), JOB_JSON_FIELDS)})


@app.post("/api/jobs/score")
def score_many_jobs():
    """Queue every job matching the current filters for scoring ("Check fit
    for all"). Queued rather than run inline: each job is one Gemini request."""
    body = request.json or {}
    if not gemini.available():
        return jsonify({"error": "scoring needs GEMINI_API_KEY"}), 503
    if not matcher.has_profile(scoring.decoded_settings()):
        return jsonify({"error": "add a resume or interests on the Settings page first"}), 400

    filters = {k: v for k, v in (body.get("filters") or {}).items() if v not in (None, "")}
    where, params, _ = _job_filters(filters)
    if body.get("only_unscored", True):
        where += (" AND " if where else "WHERE ") + "j.match_score IS NULL"
    with get_conn() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT j.id FROM jobs j JOIN companies c ON c.id = j.company_id {where}", params
            )
        ]
    queued = job_queue.enqueue_many(ids, job_queue.USER_REQUESTED)
    return jsonify({"matched": len(ids), "queued": queued, "already_queued": len(ids) - queued})


@app.get("/api/ats")
def list_ats():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.platform, COUNT(DISTINCT c.id) AS companies, COUNT(j.id) AS jobs,
                      SUM(CASE WHEN j.is_engineering = 1 THEN 1 ELSE 0 END) AS engineering_jobs
               FROM companies c LEFT JOIN jobs j ON j.company_id = c.id
               GROUP BY c.platform"""
        ).fetchall()
    usage = {r["platform"]: dict(r) for r in rows}
    platforms = []
    for key, meta in PLATFORMS.items():
        stats = usage.get(key, {})
        platforms.append(
            {
                "key": key,
                **meta,
                "companies": stats.get("companies", 0),
                "jobs": stats.get("jobs", 0),
                "engineering_jobs": stats.get("engineering_jobs") or 0,
            }
        )
    return jsonify({"platforms": platforms})


@app.patch("/api/jobs/<int:job_id>")
def update_job(job_id: int):
    status = (request.json or {}).get("status")
    if status not in ("new", "saved", "applied", "dismissed"):
        return jsonify({"error": "invalid status"}), 400
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    return jsonify({"ok": True})


@app.get("/api/companies")
def list_companies():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT c.id, c.name, c.platform, c.board_token, c.website, c.careers_url,
                      c.source, c.preferred, c.last_scraped_at, c.last_scrape_error, c.created_at,
                      COUNT(j.id) AS jobs,
                      SUM(CASE WHEN j.is_engineering = 1 THEN 1 ELSE 0 END) AS engineering_jobs
               FROM companies c LEFT JOIN jobs j ON j.company_id = c.id
               GROUP BY c.id ORDER BY c.preferred DESC, c.name COLLATE NOCASE"""
        ).fetchall()
    companies = [{**dict(r), "engineering_jobs": r["engineering_jobs"] or 0} for r in rows]
    return jsonify({"companies": companies})


@app.post("/api/companies")
def add_company():
    """Add by careers URL (auto-detected) or by explicit platform + token."""
    body = request.json or {}
    name = (body.get("name") or "").strip()
    website = (body.get("website") or "").strip() or None
    url = (body.get("url") or "").strip()

    if url:
        try:
            found = asyncio.run(ats_detect.detect_from_url(url))
        except Exception as exc:  # noqa: BLE001 - detection is best-effort network I/O
            log.warning("detection failed for %s: %s", url, exc)
            found = None
        if found is None:
            return jsonify({"error": "couldn't find a job board at that URL"}), 422
        platform, token, careers_url = found.platform, found.token, found.careers_url
        name = name or found.name_hint or token
    else:
        platform = (body.get("platform") or "").strip().lower()
        token = (body.get("board_token") or "").strip()
        careers_url = None
        if not name or not token:
            return jsonify({"error": "provide a careers URL, or name + platform + board token"}), 400
        if platform not in SCRAPERS:
            return jsonify({"error": f"platform must be one of {sorted(SCRAPERS)}"}), 400

    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO companies
                   (name, platform, board_token, website, careers_url, source, preferred, created_at)
                   VALUES (?, ?, ?, ?, ?, 'manual', 1, ?)""",
                (name, platform, token, website, careers_url, time.time()),
            )
    except sqlite3.IntegrityError:
        return jsonify({"error": "that job board is already tracked"}), 409
    return jsonify({"id": cur.lastrowid, "name": name, "platform": platform, "board_token": token}), 201


@app.patch("/api/companies/<int:company_id>")
def update_company(company_id: int):
    body = request.json or {}
    if "preferred" not in body:
        return jsonify({"error": "nothing to update"}), 400
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE companies SET preferred = ? WHERE id = ?",
            (1 if body["preferred"] else 0, company_id),
        )
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.delete("/api/companies/<int:company_id>")
def delete_company(company_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return jsonify({"ok": True})


@app.post("/api/discover")
def start_discovery():
    if not gemini.available():
        return jsonify({"error": "discovery needs GEMINI_API_KEY"}), 503
    reset_at = gemini.quota_reset_at()
    if reset_at:
        return jsonify({"error": gemini.quota_message(reset_at)}), 429
    query =((request.json or {}).get("query") or "").strip()[:300] or None
    with get_conn() as conn:
        busy = conn.execute(
            "SELECT id FROM discovery_runs WHERE status IN ('pending', 'running')"
        ).fetchone()
        if busy:
            return jsonify({"error": "a discovery run is already in progress", "id": busy["id"]}), 409
        cur = conn.execute(
            "INSERT INTO discovery_runs (query, status, created_at) VALUES (?, 'pending', ?)",
            (query, time.time()),
        )
    return jsonify({"id": cur.lastrowid, "status": "pending"}), 202


@app.get("/api/discover")
def discovery_status():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM discovery_runs ORDER BY id DESC LIMIT 5").fetchall()
    return jsonify({"runs": [_decode(dict(r), ("log",)) for r in rows]})


@app.get("/api/companies/<int:company_id>/profile")
def company_profile(company_id: int):
    with get_conn() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
        if company is None:
            return jsonify({"error": "not found"}), 404
        profile = conn.execute(
            "SELECT * FROM company_profiles WHERE company_id = ?", (company_id,)
        ).fetchone()
        people = conn.execute(
            "SELECT * FROM people WHERE company_id = ? ORDER BY name", (company_id,)
        ).fetchall()

    profile_dict = (
        _decode(dict(profile), ("tech_stack", "recent_news", "sources")) if profile else None
    )
    return jsonify(
        {
            "company": dict(company),
            "profile": profile_dict,
            "people": [dict(p) for p in people],
        }
    )


@app.get("/api/settings")
def read_settings():
    return jsonify(_decode(get_settings(), JSON_FIELDS))


@app.post("/api/settings")
def write_settings():
    body = request.json or {}
    # Partial update: only fields present in the request are written, so the
    # Sources page saving scrape_scope can't clobber the profile, or vice versa.
    converters = {
        "interests": lambda v: json.dumps(v or []),
        "domains": lambda v: json.dumps(v or []),
        "salary_min": lambda v: v,
        "salary_max": lambda v: v,
        "salary_currency": lambda v: v or "USD",
        "min_years_experience": lambda v: v,
        "max_years_experience": lambda v: v,
        "remote_only": lambda v: 1 if v else 0,
        "engineering_only": lambda v: 1 if v else 0,
        "scrape_scope": lambda v: "all" if v == "all" else "preferred",
    }
    fields = {k: convert(body[k]) for k, convert in converters.items() if k in body}
    if not fields:
        return jsonify({"error": "no recognised settings in request"}), 400
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE user_settings SET {assignments} WHERE id = 1", fields)
    return jsonify(_decode(get_settings(), JSON_FIELDS))


@app.post("/api/settings/resume")
def upload_resume():
    upload = request.files.get("resume")
    if upload is None or not upload.filename:
        return jsonify({"error": "no file uploaded"}), 400

    try:
        text = resume_parser.extract_text(upload.read(), upload.filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001 - malformed upload shouldn't 500
        log.warning("resume extraction failed: %s", exc)
        return jsonify({"error": "could not read that file"}), 400

    if not text:
        return jsonify({"error": "no text found in that file"}), 400

    structured = asyncio.run(resume_parser.parse_resume(text))

    with get_conn() as conn:
        conn.execute(
            "UPDATE user_settings SET resume_filename=?, resume_text=?, resume_structured=?, "
            "updated_at=? WHERE id=1",
            (
                upload.filename,
                text,
                json.dumps(structured) if structured else None,
                time.time(),
            ),
        )

    parse_error = None
    if structured is None:
        reset_at = gemini.quota_reset_at()
        if not gemini.available():
            parse_error = "Resume saved, but parsing needs GEMINI_API_KEY."
        elif reset_at:
            parse_error = f"Resume saved, but not parsed: {gemini.quota_message(reset_at)}. Re-upload after that."
        else:
            parse_error = "Resume saved, but Gemini couldn't parse it right now. Try re-uploading shortly."

    return jsonify(
        {
            "filename": upload.filename,
            "characters": len(text),
            "structured": structured,
            "parsed": structured is not None,
            "parse_error": parse_error,
        }
    )


@app.post("/api/rescore")
def rescore():
    count = job_queue.enqueue_all_jobs("rescore_only")
    return jsonify({"queued": count})


@app.delete("/api/data/research")
def purge_research():
    with get_conn() as conn:
        conn.execute("DELETE FROM people")
        conn.execute("DELETE FROM company_profiles")
    return jsonify({"ok": True})


@app.delete("/api/data/all")
def wipe_database():
    with get_conn() as conn:
        for table in ("job_queue", "discovery_runs", "jobs", "people", "company_profiles", "companies"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE user_settings SET resume_filename=NULL, resume_text=NULL, "
                     "resume_structured=NULL WHERE id=1")
    return jsonify({"ok": True})


def _maybe_start_scraper():
    """Local-dev convenience only; Docker leaves this off."""
    if not RUN_SCRAPER_IN_APP:
        return
    import threading

    import scraper_worker

    def run():
        asyncio.run(scraper_worker.scrape_loop())

    threading.Thread(target=run, daemon=True, name="scraper").start()
    log.info("scraper loop started inside API process (RUN_SCRAPER_IN_APP=true)")


init_db()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [api] %(message)s")
    _maybe_start_scraper()
    app.run(host="0.0.0.0", port=API_PORT)
