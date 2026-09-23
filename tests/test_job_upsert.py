"""Job records are created once per (company, job id) and upserted thereafter.

Uses a throwaway SQLite file. Run: python -m unittest discover tests
"""

import tests  # noqa: F401,I001 -- must come first: isolates DB_PATH

import os
import subprocess
import sys
import time
import unittest

from db import get_conn, init_db
from scraper_worker import _upsert_job

tests.assert_isolated()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def posting(**overrides):
    base = {
        "external_id": "123",
        "title": "Senior Backend Engineer",
        "location": "Remote",
        "url": "https://example.com/jobs/123",
        "description": "Build APIs.",
        "department": "Engineering",
        "remote_type": "fully_remote",
    }
    return {**base, **overrides}


class JobUpsertTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with get_conn() as conn:
            for table in ("job_queue", "jobs", "companies"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT INTO companies (id, name, platform, board_token, created_at) VALUES "
                "(1, 'Acme', 'greenhouse', 'acme', 0), (2, 'Other', 'lever', 'other', 0)"
            )

    def _jobs(self):
        with get_conn() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY id")]

    def test_first_fetch_creates_one_record(self):
        with get_conn() as conn:
            job_id, engineering = _upsert_job(conn, 1, posting())
        self.assertIsNotNone(job_id)
        self.assertTrue(engineering)
        self.assertEqual(len(self._jobs()), 1)

    def test_refetch_updates_in_place(self):
        with get_conn() as conn:
            first_id, _ = _upsert_job(conn, 1, posting())
            original = self._jobs()[0]
            time.sleep(0.01)
            again_id, _ = _upsert_job(conn, 1, posting(title="Staff Backend Engineer", location="Remote - EU"))

        self.assertIsNone(again_id, "a refetch must not report the job as new")
        jobs = self._jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], first_id)
        self.assertEqual(jobs[0]["title"], "Staff Backend Engineer")
        self.assertEqual(jobs[0]["location"], "Remote - EU")
        self.assertEqual(jobs[0]["first_seen_at"], original["first_seen_at"])
        self.assertGreater(jobs[0]["last_seen_at"], original["last_seen_at"])

    def test_refetch_preserves_user_and_agent_fields(self):
        with get_conn() as conn:
            job_id, _ = _upsert_job(conn, 1, posting(description=""))
            conn.execute(
                "UPDATE jobs SET status='applied', match_score=87, description='Fetched later.' WHERE id=?",
                (job_id,),
            )
            _upsert_job(conn, 1, posting(description=""))

        job = self._jobs()[0]
        self.assertEqual(job["status"], "applied")
        self.assertEqual(job["match_score"], 87)
        self.assertEqual(job["description"], "Fetched later.")

    def test_same_job_id_at_different_companies_is_two_records(self):
        with get_conn() as conn:
            a, _ = _upsert_job(conn, 1, posting())
            b, _ = _upsert_job(conn, 2, posting())
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(len(self._jobs()), 2)

    def test_concurrent_scrapers_never_duplicate(self):
        """Two processes upserting the same 200 postings at once."""
        script = (
            "from db import get_conn\n"
            "from scraper_worker import _upsert_job\n"
            "with get_conn() as conn:\n"
            "    for i in range(200):\n"
            "        _upsert_job(conn, 1, {'external_id': str(i), 'title': f'Engineer {i}',"
            " 'remote_type': 'fully_remote'})\n"
        )
        env = {**os.environ, "PYTHONPATH": ROOT}
        procs = [
            subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, env=env, stderr=subprocess.PIPE)
            for _ in range(2)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err.decode())

        with get_conn() as conn:
            total = conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
            dupes = conn.execute(
                "SELECT COUNT(*) n FROM (SELECT 1 FROM jobs GROUP BY company_id, external_id "
                "HAVING COUNT(*) > 1)"
            ).fetchone()["n"]
        self.assertEqual(total, 200)
        self.assertEqual(dupes, 0)


if __name__ == "__main__":
    unittest.main()
