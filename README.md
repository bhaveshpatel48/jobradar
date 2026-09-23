# JobRadar Pro

Personal remote-job radar. Scrapes company job boards across 17 ATS platforms,
filters to remote engineering roles, researches each company with Gemini, and
scores every posting against your resume so you can sort by fit instead of
recency. You can also ask for jobs in plain English ("senior backend roles in Go
on distributed systems") and let Gemini filter the list.

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

On the **Sources** page, paste a careers or job-board URL (e.g.
`https://jobs.ashbyhq.com/linear` or `https://www.figma.com/careers/`) and
JobRadar detects the ATS and board token itself. New companies are scraped
within ~15 seconds. Then upload a resume and save your preferences on
**Settings**. To force a full pass:

```bash
docker compose run --rm scraper-worker python scraper_worker.py --once
```

## What gets scraped

- **Supported systems:** Greenhouse, Lever, Ashby, Workable, SmartRecruiters,
  Recruitee, Workday, BambooHR, Breezy HR, Personio, Pinpoint, Rippling,
  Teamtailor, Gem, Polymer, JazzHR, Dover. The Sources page lists them with live
  company/job counts. All use public, unauthenticated job-board endpoints.
- **Company careers pages:** most are JavaScript-rendered, but many link or
  embed their ATS board in the raw HTML; pasting the careers URL finds it.
- **Scope:** *Preferred companies only* (starred) or *All companies* (also those
  found by AI discovery). Star or unstar any company on the Sources page.
- **AI discovery:** Gemini suggests companies from your preferred domains and
  interests. A suggestion is stored only after JobRadar reaches a real board
  with open roles, so a wrong ATS guess gets corrected and an invented company
  is rejected. Discovered companies start unstarred.
- **Engineering only** (default on): classified mainly from the job title, since
  boards file engineers under "Security" or "Applied AI" next to analysts and
  sales architects. Non-engineering jobs are stored but not shown or scored.
  Non-software disciplines (data-center electrical, mechanical, …) are excluded.

## AI search

Type what you want into **Ask AI** on the Jobs page. The normal filters run
first, and Gemini then judges up to 400 of the remaining jobs in batches of 100.
It keeps only the ones that fit, each with a relevance score and a one-line
reason. Verdicts are cached per (query, job), so refining other filters doesn't
spend quota again.

Default host ports are **8088** (API) and **5174** (UI), since 8000/5173 are
often taken. Override with `API_HOST_PORT` / `UI_HOST_PORT` in `.env`.

## Configuration

All secrets live in `.env` (gitignored); `.env.example` is the template.

| Variable                      | Default            | Notes                                               |
| ----------------------------- | ------------------ | --------------------------------------------------- |
| `GEMINI_API_KEY`              | –                  | Required for research, parsing, scoring.            |
| `GEMINI_MODEL`                | `gemini-3.5-flash` | See model note below.                               |
| `GEMINI_RPM`                  | `6`                | Requests/min shared by all containers.              |
| `GEMINI_INTERACTIVE_RESERVE`  | `2`                | Slots background work leaves free for AI search.    |
| `HUNTER_API_KEY`              | –                  | Optional email enrichment.                          |
| `COMPANY_RESEARCH_TTL_DAYS`   | `7`                | Min interval before re-researching a company.       |
| `SCRAPE_INTERVAL_SECONDS`     | `600`              | Scrape loop cadence.                                |
| `AGENT_POLL_INTERVAL_SECONDS` | `5`                | Queue poll cadence when idle.                       |
| `RUN_SCRAPER_IN_APP`          | `false`            | Local-dev only; leave false under Docker.           |

Every enrichment feature degrades gracefully: with no `GEMINI_API_KEY` the stack
still scrapes, stores and serves jobs — they simply stay unscored, and the UI
says so.

### Gemini quotas (read this if you're on the free tier)

- **Daily cap:** a free-tier key allows only **20 requests per day per model**.
  A single scoring pass over a few hundred jobs uses that up quickly. When
  Google reports the daily quota exhausted, JobRadar records the reset time
  (midnight US Pacific) and stops calling Gemini in every container until then.
  A banner shows this. Scraping keeps running, and queued work resumes after
  the reset. If you raise your quota, click **Retry now** in the banner.
- **Per-minute limit:** calls are also rate-limited across containers through a
  shared ledger in SQLite (`GEMINI_RPM`). Background scoring always leaves a
  reserve for interactive AI search and resume parsing.
- **No grounding:** free-tier keys also have no Google Search grounding quota.
  Company research and the people finder then fall back to the company's own
  website pages. Those URLs are stored as the sources, and funding and
  headcount stay null unless the site states them.

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

## Tests

```bash
python -m unittest discover tests      # classifier, remote detection, URL parsing
```

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
agent_worker.py     agent orchestrator + discovery runs (--loop | --once)
ats_scraper.py      17 per-platform scrapers + remote classification
ats_detect.py       URL / careers-page -> (platform, token), verified by probing
job_classify.py     engineering-role classifier
sitefetch.py        fetch company pages and job descriptions
task_queue.py       job_queue helpers
db.py               schema, guarded migrations, WAL connections
config.py           env config
agents/             resume_parser, company_research, people_finder, matcher,
                    job_search (AI filter), company_discovery
llm/gemini.py       Gemini wrapper: grounding + fallback, shared rate ledger,
                    daily-quota handling
tests/              offline unit tests
ui/                 Vue 3 + Vite SPA (Jobs, Sources, Settings)
```
