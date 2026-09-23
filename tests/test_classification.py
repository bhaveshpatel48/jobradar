"""Offline tests for the pure classification/parsing logic.

Cases come from real postings where an earlier rule got it wrong.
Run: python -m unittest discover tests
"""

import unittest

from ats_detect import parse_url
from ats_scraper import classify_remote
from job_classify import is_engineering

ENGINEERING = [
    ("Staff Software Engineer, API", "Engineering & Design - Product"),
    ("Engineering Manager, Enterprise", "Engineering & Design - Product"),
    ("Member of Technical Staff", "AI Research & Engineering"),
    ("Staff+ Researcher, Cybersecurity Products", "Engineering & Design - Product"),
    ("Pre-training Distributed Systems Tech Lead / Manager", "AI Research & Engineering"),
    ("AI Operations Engineer, Partnerships", "Sales"),
    ("Senior Software Engineer, Process Automation", None),
    ("Software Engineer - Distributed Systems", None),
    ("Infrastructure Engineer", None),
    ("Platform Engineer", None),
    ("Senior Backend Developer", None),
    ("iOS Developer", None),
    ("Site Reliability Engineer", None),
    ("Design Engineer", None),
    ("Research Scientist, Interpretability", None),
    ("Data Scientist", None),
    ("Software Architect", None),
    ("Firmware Engineer", None),
]

NOT_ENGINEERING = [
    ("Infrastructure Tax Lead", "Finance"),
    ("Incident Response Manager - Product & Engineering", "Technical Program Management"),
    ("Data Center Electrical Engineer", None),
    ("Data Center Mechanical Engineer", None),
    ("Controls Engineer", None),
    ("AV Operations Specialist", "Security"),
    ("Protective Intelligence Analyst", "Security"),
    ("Applied AI Architect, Enterprise Tech", "Applied AI"),
    ("Research Operations, Reinforcement Learning", "AI Research & Engineering"),
    ("IT Support Engineer", "Security"),
    ("Sales Engineer", None),
    ("Solutions Engineer", None),
    ("Product Designer", None),
    ("Account Executive", "Sales"),
    ("Technical Program Manager", None),
]


class EngineeringClassifierTest(unittest.TestCase):
    def test_engineering_roles(self):
        for title, dept in ENGINEERING:
            with self.subTest(title=title, dept=dept):
                self.assertTrue(is_engineering(title, dept))

    def test_non_engineering_roles(self):
        for title, dept in NOT_ENGINEERING:
            with self.subTest(title=title, dept=dept):
                self.assertFalse(is_engineering(title, dept))


class RemoteClassifierTest(unittest.TestCase):
    def test_native_signal_wins(self):
        self.assertEqual(classify_remote("Berlin", "Engineer", True), "fully_remote")
        self.assertEqual(classify_remote("Remote", "Engineer", "hybrid"), "hybrid")
        self.assertEqual(classify_remote("Remote", "Engineer", "onsite"), "onsite")

    def test_keywords(self):
        self.assertEqual(classify_remote("Remote - US"), "fully_remote")
        self.assertEqual(classify_remote("Hybrid - NYC"), "hybrid")
        self.assertEqual(classify_remote("San Francisco, CA"), "onsite")
        self.assertEqual(classify_remote(None), "unknown")


class UrlDetectionTest(unittest.TestCase):
    CASES = {
        "https://job-boards.greenhouse.io/anthropic": ("greenhouse", "anthropic"),
        "https://boards.greenhouse.io/embed/job_board?for=figma": ("greenhouse", "figma"),
        "https://boards-api.greenhouse.io/v1/boards/duolingo/departments": ("greenhouse", "duolingo"),
        "https://jobs.lever.co/spotify/abc-123": ("lever", "spotify"),
        "https://jobs.ashbyhq.com/linear": ("ashby", "linear"),
        "https://apply.workable.com/huggingface/": ("workable", "huggingface"),
        "https://jobs.smartrecruiters.com/BoschGroup": ("smartrecruiters", "BoschGroup"),
        "https://bunq.recruitee.com/": ("recruitee", "bunq"),
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite":
            ("workday", "nvidia/wd5/NVIDIAExternalCareerSite"),
        "https://rei.bamboohr.com/careers": ("bamboohr", "rei"),
        "https://dozuki.breezy.hr/": ("breezy", "dozuki"),
        "https://apheris.jobs.personio.de/": ("personio", "apheris"),
        "https://article.pinpointhq.com/": ("pinpoint", "article"),
        "https://ats.rippling.com/boom-supersonic/jobs": ("rippling", "boom-supersonic"),
        "https://commonsku.na.teamtailor.com/jobs": ("teamtailor", "commonsku.na"),
        "https://jobs.gem.com/motion": ("gem", "motion"),
        "https://jobs.polymer.co/themis": ("polymer", "themis"),
        "https://prometheum.applytojob.com/apply": ("jazzhr", "prometheum"),
        "https://app.dover.com/apply/getcrate": ("dover", "getcrate"),
    }
    NOT_BOARDS = [
        "https://apply.workable.com/j/ABC123",
        "https://careers-analytics.recruitee.com/x",
        "https://example.com/careers",
    ]

    def test_known_boards(self):
        for url, expected in self.CASES.items():
            with self.subTest(url=url):
                self.assertEqual(parse_url(url), expected)

    def test_non_boards(self):
        for url in self.NOT_BOARDS:
            with self.subTest(url=url):
                self.assertIsNone(parse_url(url))


if __name__ == "__main__":
    unittest.main()
