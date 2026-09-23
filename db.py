import os
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    board_token TEXT NOT NULL,
    website TEXT,
    created_at REAL,
    UNIQUE (platform, board_token)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    url TEXT,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    remote_type TEXT DEFAULT 'unknown',
    match_score INTEGER,
    match_reasoning TEXT,
    match_matched_skills TEXT,
    match_concerns TEXT,
    matched_at REAL,
    first_seen_at REAL,
    last_seen_at REAL,
    UNIQUE (company_id, external_id)
);

CREATE TABLE IF NOT EXISTS user_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    resume_filename TEXT,
    resume_text TEXT,
    resume_structured TEXT,
    interests TEXT,
    domains TEXT,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT DEFAULT 'USD',
    min_years_experience INTEGER,
    max_years_experience INTEGER,
    remote_only INTEGER DEFAULT 1,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS company_profiles (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    summary TEXT,
    mission TEXT,
    funding_stage TEXT,
    funding_amount TEXT,
    last_funding_date TEXT,
    employee_count_estimate TEXT,
    tech_stack TEXT,
    recent_news TEXT,
    sources TEXT,
    researched_at REAL
);

CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    title TEXT,
    linkedin_url TEXT,
    email TEXT,
    email_confidence TEXT,
    source TEXT,
    discovered_at REAL,
    UNIQUE (company_id, name)
);

CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task_type TEXT NOT NULL DEFAULT 'enrich_and_match',
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    started_at REAL,
    completed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON job_queue(status, id);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_people_company ON people(company_id);
"""


def connect() -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(directory, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the api / scraper / agent processes read and write the shared
    # file concurrently instead of serialising behind a global write lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT 1 FROM user_settings WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO user_settings (id, remote_only, salary_currency, updated_at) "
                "VALUES (1, 1, 'USD', ?)",
                (time.time(),),
            )


def get_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE id = 1").fetchone()
        return dict(row) if row else {}


if __name__ == "__main__":
    init_db()
    print(f"initialised {DB_PATH}")
