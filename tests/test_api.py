"""API behaviour: grouped/flat pagination and the scoring endpoints.

Gemini is stubbed, so these run offline and never spend quota.
Run: python -m unittest discover tests
"""

import tests  # noqa: F401,I001 -- must come first: isolates DB_PATH

import time
import unittest
from unittest import mock

import app as app_module
from db import get_conn, init_db
from llm import gemini

tests.assert_isolated()

# Companies: (id, name, job count). Beta's jobs score 90/89/88; Acme and Gamma
# each have one job at 51, so their order is decided by the newest job.
COMPANIES = [(1, "Acme", 12), (2, "Beta", 3), (3, "Gamma", 7)]


def seed():
    with get_conn() as conn:
        for table in ("job_queue", "jobs", "companies"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE user_settings SET interests = '[\"backend\"]', engineering_only = 1, remote_only = 1")
        n = 0
        for cid, name, count in COMPANIES:
            conn.execute(
                "INSERT INTO companies (id, name, platform, board_token, created_at, preferred) "
                "VALUES (?, ?, 'greenhouse', ?, 0, 1)",
                (cid, name, name.lower()),
            )
            for i in range(count):
                n += 1
                score = (90 - i) if cid == 2 else (50 + i if i < 2 else None)
                conn.execute(
                    "INSERT INTO jobs (company_id, external_id, title, status, remote_type, "
                    "is_engineering, first_seen_at, match_score, description) "
                    "VALUES (?, ?, ?, 'new', 'fully_remote', 1, ?, ?, 'Build things.')",
                    (cid, f"{cid}-{i}", f"Engineer {cid}-{i}", time.time() + n, score),
                )


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = app_module.app.test_client()

    def setUp(self):
        seed()


class GroupedPaginationTest(ApiTestCase):
    def test_groups_are_paged_by_company(self):
        g = self.client.get("/api/jobs/grouped?page_size=2&jobs_per_company=5").get_json()
        self.assertEqual((g["total"], g["total_pages"], g["total_jobs"]), (3, 2, 22))
        self.assertEqual([x["company_name"] for x in g["groups"]], ["Beta", "Gamma"])
        self.assertEqual([j["match_score"] for j in g["groups"][0]["jobs"]], [90, 89, 88])
        self.assertTrue(all(len(x["jobs"]) <= 5 for x in g["groups"]))

        g2 = self.client.get("/api/jobs/grouped?page=2&page_size=2&jobs_per_company=5").get_json()
        self.assertEqual(
            [(x["company_name"], len(x["jobs"]), x["has_more"]) for x in g2["groups"]],
            [("Acme", 5, True)],
        )

    def test_show_more_pages_through_one_company_without_overlap(self):
        pages = [
            self.client.get(f"/api/jobs?company_id=1&page={i}&page_size=5").get_json() for i in (1, 2, 3)
        ]
        self.assertEqual([len(p["jobs"]) for p in pages], [5, 5, 2])
        self.assertEqual((pages[0]["total"], pages[0]["total_pages"]), (12, 3))
        ids = [j["id"] for p in pages for j in p["jobs"]]
        self.assertEqual(len(set(ids)), 12)
        self.assertNotIn("description", pages[0]["jobs"][0])

    def test_out_of_range_and_malformed_pages(self):
        past = self.client.get("/api/jobs/grouped?page=99").get_json()
        self.assertEqual((past["groups"], past["total"]), ([], 3))
        bad = self.client.get("/api/jobs/grouped?page=abc&page_size=-4").get_json()
        self.assertEqual((bad["page"], bad["page_size"]), (1, 1))

    def test_counts_unscored_and_pending(self):
        g = self.client.get("/api/jobs/grouped").get_json()
        self.assertEqual(g["unscored_jobs"], 15)
        self.assertEqual(g["pending_scores"], 0)


class ScoringEndpointTest(ApiTestCase):
    def _unscored_job_id(self):
        with get_conn() as conn:
            return conn.execute("SELECT id FROM jobs WHERE match_score IS NULL LIMIT 1").fetchone()[0]

    def test_score_one_job_stores_and_returns_score(self):
        job_id = self._unscored_job_id()
        result = {"match_score": 77, "reasoning": "Good fit.", "matched_skills": ["Python"], "concerns": []}
        with mock.patch("agents.matcher.score_job", mock.AsyncMock(return_value=result)) as score:
            resp = self.client.post(f"/api/jobs/{job_id}/score")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["job"]["match_score"], 77)
        self.assertEqual(score.call_args.kwargs["priority"], gemini.INTERACTIVE)
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT match_score FROM jobs WHERE id=?", (job_id,)).fetchone()[0], 77)

    def test_score_one_job_reports_quota_exhaustion(self):
        job_id = self._unscored_job_id()
        exhausted = gemini.LLMQuotaExhausted("Gemini daily quota is used up", time.time() + 3600)
        with mock.patch("agents.matcher.score_job", mock.AsyncMock(side_effect=exhausted)):
            resp = self.client.post(f"/api/jobs/{job_id}/score")
        self.assertEqual(resp.status_code, 429)
        self.assertIn("quota", resp.get_json()["error"])

    def test_score_one_job_needs_a_profile(self):
        with get_conn() as conn:
            conn.execute("UPDATE user_settings SET interests = NULL, resume_structured = NULL")
        resp = self.client.post(f"/api/jobs/{self._unscored_job_id()}/score")
        self.assertEqual(resp.status_code, 400)

    def test_score_all_queues_only_unscored_matching_jobs_once(self):
        first = self.client.post("/api/jobs/score", json={"filters": {"company_id": "1"}}).get_json()
        self.assertEqual((first["matched"], first["queued"]), (10, 10))  # Acme: 12 jobs, 2 scored
        again = self.client.post("/api/jobs/score", json={"filters": {"company_id": "1"}}).get_json()
        self.assertEqual((again["queued"], again["already_queued"]), (0, 10))
        g = self.client.get("/api/jobs/grouped").get_json()
        self.assertEqual(g["pending_scores"], 10)
        self.assertEqual(g["unscored_jobs"], 5, "queued jobs must not count as still-to-queue")
        with get_conn() as conn:
            types = {r[0] for r in conn.execute("SELECT DISTINCT task_type FROM job_queue")}
        self.assertEqual(types, {"score"})


if __name__ == "__main__":
    unittest.main()
