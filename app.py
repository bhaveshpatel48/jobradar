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
import task_queue as job_queue
from agents import job_search, resume_parser
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


def _query_jobs(args, limit: int = 500) -> tuple[list[dict], dict]:
    """Apply the shared job filters. Used by both the list and AI search, so the
    AI only ever judges jobs the user could see anyway."""
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
    order = (
        "ORDER BY j.first_seen_at DESC"
        if args.get("sort") == "newest"
        else "ORDER BY j.match_score DESC NULLS LAST, j.first_seen_at DESC"
    )

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT j.*, c.name AS company_name, c.website AS company_website,
                       c.platform AS company_platform
                FROM jobs j JOIN companies c ON c.id = j.company_id
                {where} {order} LIMIT ?""",
            [*params, limit],
        ).fetchall()

    jobs = [_decode(dict(r), ("match_matched_skills", "match_concerns")) for r in rows]
    applied = {"remote_only": remote_only, "engineering_only": engineering_only, "scope": scope}
    return jobs, applied


@app.get("/api/jobs")
def list_jobs():
    jobs, applied = _query_jobs(request.args)
    return jsonify({"jobs": jobs, "count": len(jobs), **applied})


@app.post("/api/jobs/ai-search")
def ai_search_jobs():
    body = request.json or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "describe the jobs you want"}), 400
    if len(query) > 500:
        return jsonify({"error": "keep the request under 500 characters"}), 400
    if not gemini.available():
        return jsonify({"error": "AI search needs GEMINI_API_KEY"}), 503

    filters = {k: v for k, v in (body.get("filters") or {}).items() if v not in (None, "")}
    jobs, applied = _query_jobs(filters, limit=job_search.MAX_JOBS + 1)

    try:
        result = asyncio.run(job_search.ai_filter(query, jobs))
    except gemini.LLMQuotaExhausted as exc:
        return jsonify({"error": str(exc), "quota_resets_at": exc.reset_at}), 429
    except gemini.LLMRateLimited:
        return jsonify({"error": "Gemini rate limit reached -- try again in a minute"}), 429
    except gemini.LLMError as exc:
        log.warning("ai search failed: %s", exc)
        return jsonify({"error": "AI search failed; try again"}), 502

    matched = []
    for job in jobs:
        verdict = result["matches"].get(job["id"])
        if verdict:
            job["ai_relevance"] = verdict["relevance"]
            job["ai_reason"] = verdict["reason"]
            matched.append(job)
    matched.sort(key=lambda j: j["ai_relevance"], reverse=True)

    return jsonify(
        {
            "jobs": matched,
            "count": len(matched),
            "judged": result["judged"],
            "truncated": result["truncated"],
            "partial": result["partial"],
            **applied,
        }
    )


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

    return jsonify(
        {
            "filename": upload.filename,
            "characters": len(text),
            "structured": structured,
            "parsed": structured is not None,
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
