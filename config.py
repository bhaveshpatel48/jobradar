import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


DB_PATH = os.environ.get("DB_PATH", "./data/jobradar.db")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# Requests per minute across ALL processes. Background work leaves
# GEMINI_INTERACTIVE_RESERVE of them free so AI search never queues behind scoring.
GEMINI_RPM = _int("GEMINI_RPM", 6)
GEMINI_INTERACTIVE_RESERVE = _int("GEMINI_INTERACTIVE_RESERVE", 2)

HUNTER_API_KEY = os.environ.get("HUNTER_API_KEY")

COMPANY_RESEARCH_TTL_DAYS = _int("COMPANY_RESEARCH_TTL_DAYS", 7)
SCRAPE_INTERVAL_SECONDS = _int("SCRAPE_INTERVAL_SECONDS", 600)
AGENT_POLL_INTERVAL_SECONDS = _int("AGENT_POLL_INTERVAL_SECONDS", 5)

RUN_SCRAPER_IN_APP = _bool("RUN_SCRAPER_IN_APP", False)
API_PORT = _int("API_PORT", 8000)
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()
]
