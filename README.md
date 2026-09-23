# JobRadar Pro

Personal remote-job radar. Scrapes startup careers pages via public ATS APIs,
researches each company with Gemini + Google Search grounding, and scores every
posting against your resume so you can sort by fit instead of recency.

Four services, one shared SQLite file on a persistent volume:

| Service          | Role                                                        |
| ---------------- | ----------------------------------------------------------- |
| `api`            | Flask JSON API (gunicorn). No scraping, no LLM loops.        |
| `scraper-worker` | Async scrape loop. Discovers jobs, enqueues them.            |
| `agent-worker`   | Drains the queue: company research → people → match scoring. |
| `ui`             | Vue 3 SPA built by Vite, served by nginx (proxies `/api`).   |

Splitting the workers keeps LLM latency off the discovery path and keeps the API
responsive regardless of what the agents are doing.

## Quick start

```bash
cp .env.example .env     # then add your GEMINI_API_KEY
docker compose up -d
open http://localhost:5174
```

Add a company under **Settings → Tracked companies** (e.g. name `Anthropic`,
platform `greenhouse`, board token `anthropic`), upload a resume, save your
preferences. The scraper picks it up on its next cycle, or trigger one now:

```bash
docker compose run --rm scraper-worker python scraper_worker.py --once
```

Default host ports are **8088** (API) and **5174** (UI), since 8000/5173 are
often taken. Override with `API_HOST_PORT` / `UI_HOST_PORT` in `.env`.

## Configuration

All secrets live in `.env` (gitignored); `.env.example` is the template.

| Variable                      | Default            | Notes                                               |
| ----------------------------- | ------------------ | --------------------------------------------------- |
| `GEMINI_API_KEY`              | –                  | Required for research, parsing, scoring.            |
| `GEMINI_MODEL`                | `gemini-3.5-flash` | See model note below.                               |
| `HUNTER_API_KEY`              | –                  | Optional email enrichment.                          |
| `COMPANY_RESEARCH_TTL_DAYS`   | `7`                | Min interval before re-researching a company.       |
| `SCRAPE_INTERVAL_SECONDS`     | `600`              | Scrape loop cadence.                                |
| `AGENT_POLL_INTERVAL_SECONDS` | `5`                | Queue poll cadence when idle.                       |
| `RUN_SCRAPER_IN_APP`          | `false`            | Local-dev only; leave false under Docker.           |

Every enrichment feature degrades gracefully: with no `GEMINI_API_KEY` the stack
still scrapes, stores and serves jobs — they simply stay unscored, and the UI
says so.

### A note on the Gemini model

Google restricts the 2.5-era models to projects that already used them, so a new
API key may get an error on `gemini-2.5-*`. The default here is a 3.x GA model.

Grounded search and strict-JSON output can't be combined in one call on Gemini
2.x (the server rejects it), and on 3.x that combination drops the grounding
metadata we need for citations. So `llm/gemini.py` deliberately does research and
JSON extraction as **two** calls. Grounding chunks also cite a
`vertexaisearch.cloud.google.com` redirect rather than the publisher, so each
citation is resolved via HEAD before being stored in `sources`.

## Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python db.py                                  # create the schema

python app.py                                 # API on :8000
python scraper_worker.py --loop               # or --once
python agent_worker.py --loop                 # or --once

cd ui && npm install && npm run dev           # SPA on :5173, proxies /api
```

Setting `RUN_SCRAPER_IN_APP=true` starts the scrape loop inside the Flask process
instead, so `python app.py` alone is enough for quick local work.

## Operations

```bash
docker compose logs -f agent-worker    # watch research + scoring
docker compose ps                      # health
curl localhost:8088/api/health         # queue depth by status
```

The database lives at `./data/jobradar.db` and survives `docker compose down`.
Only `docker compose down -v`, or the **Wipe database** button, removes it.

Failed queue tasks retry up to 3 times, then park as `failed` with the error
recorded. Tasks orphaned by a worker restart are requeued automatically.

## Ground rules baked into the code

- **No LinkedIn scraping.** Profile URLs are stored as pointers, discovered via
  public search. Nothing fetches linkedin.com.
- **Sourced research only.** Company facts carry resolved source URLs. Anything
  unfound is stored as null and shown as missing rather than invented.
- **Guessed emails are labeled.** An address is `verified` only when an
  enrichment API returns it; pattern-inferred addresses are `guessed` and the UI
  says so. No SMTP probing.
- **Data minimization + deletion.** Only business-relevant fields are stored, and
  research data or the whole DB can be purged from Settings.
- **Not a mailing tool.** Contact info is surfaced for manual, individual outreach.

## Layout

```
app.py              Flask API
scraper_worker.py   scrape loop (--loop | --once)
agent_worker.py     agent orchestrator (--loop | --once)
ats_scraper.py      per-platform scrapers + remote classification
task_queue.py       job_queue helpers
db.py               schema + WAL connections
config.py           env config
agents/             resume_parser, company_research, people_finder, matcher
llm/gemini.py       Gemini wrapper (grounding, JSON, citation resolution)
ui/                 Vue 3 + Vite SPA
```

Supported ATS platforms: Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
Recruitee.
