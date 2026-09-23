"""JobRadar API.

Serves JSON only -- the Vue SPA is a separate service. Under Docker this process
does not run the scraper or agent loops; dedicated worker containers own those.
"""

import asyncio
import json
import logging
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

import task_queue as job_queue
from agents import resume_parser
from ats_scraper import SCRAPERS
from config import API_PORT, CORS_ORIGINS, RUN_SCRAPER_IN_APP
from db import get_conn, get_settings, init_db

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
    return jsonify({"status": "ok", "queue": job_queue.stats()})


@app.get("/api/jobs")
def list_jobs():
    settings = get_settings()
    remote_only_param = request.args.get("remote_only")
    if remote_only_param is None:
        remote_only = bool(settings.get("remote_only"))
    else:
        remote_only = remote_only_param.lower() in ("1", "true", "yes")

    status = request.args.get("status")
    search = request.args.get("q")
    sort = request.args.get("sort", "match")

    clauses, params = [], []
    if remote_only:
        # 'unknown' is surfaced deliberately -- the user judges those.
        clauses.append("j.remote_type IN ('fully_remote', 'unknown')")
    if status:
        clauses.append("j.status = ?")
        params.append(status)
    if search:
        clauses.append("(j.title LIKE ? OR c.name LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = (
        "ORDER BY j.match_score DESC NULLS LAST, j.first_seen_at DESC"
        if sort == "match"
        else "ORDER BY j.first_seen_at DESC"
    )

    with get_conn() as conn:
        rows = conn.execute(
            f"""SELECT j.*, c.name AS company_name, c.website AS company_website
                FROM jobs j JOIN companies c ON c.id = j.company_id
                {where} {order} LIMIT 500""",
            params,
        ).fetchall()

    jobs = [_decode(dict(r), ("match_matched_skills", "match_concerns")) for r in rows]
    return jsonify({"jobs": jobs, "remote_only": remote_only, "count": len(jobs)})


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
        rows = conn.execute("SELECT * FROM companies ORDER BY name").fetchall()
    return jsonify({"companies": [dict(r) for r in rows]})


@app.post("/api/companies")
def add_company():
    body = request.json or {}
    name = (body.get("name") or "").strip()
    platform = (body.get("platform") or "").strip().lower()
    token = (body.get("board_token") or "").strip()

    if not name or not token:
        return jsonify({"error": "name and board_token are required"}), 400
    if platform not in SCRAPERS:
        return jsonify({"error": f"platform must be one of {sorted(SCRAPERS)}"}), 400

    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO companies (name, platform, board_token, website, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, platform, token, body.get("website"), time.time()),
            )
        except Exception:
            return jsonify({"error": "company already tracked"}), 409
    return jsonify({"id": cur.lastrowid, "name": name}), 201


@app.delete("/api/companies/<int:company_id>")
def delete_company(company_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return jsonify({"ok": True})


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
    fields = {
        "interests": json.dumps(body.get("interests") or []),
        "domains": json.dumps(body.get("domains") or []),
        "salary_min": body.get("salary_min"),
        "salary_max": body.get("salary_max"),
        "salary_currency": body.get("salary_currency") or "USD",
        "min_years_experience": body.get("min_years_experience"),
        "max_years_experience": body.get("max_years_experience"),
        "remote_only": 1 if body.get("remote_only", True) else 0,
        "updated_at": time.time(),
    }
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
        for table in ("job_queue", "jobs", "people", "company_profiles", "companies"):
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
