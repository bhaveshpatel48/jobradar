# System Prompt: Build "JobRadar Pro" — Multi-Agent Remote Job Matching System

You are extending an existing local Python app called **JobRadar** (Flask + SQLite) that
scrapes startup careers pages via their ATS public APIs (Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, Recruitee) and shows new job postings as cards in a browser
UI. The existing files are `db.py`, `ats_scraper.py`, `worker.py`, `app.py`,
`templates/index.html`. If those files are present in the working directory, extend
them in place. If they are not present, build the full system from scratch using the
spec below — it is self-contained.

This build also splits the system into **separate deployable services** (API, scraper
worker, agent worker, frontend) and **dockerizes everything** — see §13 for the
runtime/process architecture and §14 for the Docker setup. These are not optional
extras bolted on at the end; the file layout in §12, the orchestration in §9, and the
UI in §10 are written assuming this split, so read §13–§14 before starting.

Work through this spec end to end without stopping to ask clarifying questions;
where something is ambiguous, make the most reasonable choice, note the assumption in
a `NOTES.md` file you create, and keep going. Build in the phases listed at the end so
the app stays runnable throughout.

---

## 1. What you're adding

1. **Settings & resume, persisted in SQLite** — a settings page where the user uploads
   a resume (PDF/DOCX), and enters interests, salary range, years of experience, and
   preferred company domain(s) (e.g. "B2B SaaS", "fintech", "devtools"). This is saved
   as durable state, not session state — restarting the app must not lose it.
2. **Remote-only filtering** — the user only wants fully-remote roles. Every job must
   be normalized to a `remote_type`, and the UI/API must support filtering to
   remote-only (on by default).
3. **Full startup profiles** — for every company being tracked, gather and display:
   a written company research summary (what they do, funding stage, recent news),
   and a list of people who work there with, where legitimately available, their
   LinkedIn profile link and business email.
4. **A multi-agent system** that takes (resume + interests + salary range + experience
   + domain preference) and a job posting + company profile, and produces a match
   score with reasoning — so the UI can be sorted by "best fit for me" instead of only
   "newest."

---

## 2. Ground rules — read before writing any scraping/enrichment code

These are hard constraints, not style preferences:

- **Never scrape LinkedIn directly** (no logging into LinkedIn, no automating a
  headless browser against linkedin.com, no bypassing its anti-bot/login wall). This
  violates LinkedIn's Terms of Service. Instead:
  - Use web search to find **publicly indexed** LinkedIn profile URLs for named
    individuals (via the person's name + company), and store only the URL — never
    scrape the profile page's content.
  - Optionally integrate one pluggable, ToS-compliant people/contact enrichment API
    (e.g. Apollo.io, Hunter.io, Clearbit, PeopleDataLabs — pick one or make the
    provider swappable) via an API key the user supplies in `.env`. If no key is
    configured, skip enrichment gracefully; do not build a scraping workaround
    instead.
- **Email discovery is heuristic, and must be labeled as such.** A reasonable,
  widely-used approach: (a) prefer a verified email from an enrichment API if
  configured, (b) otherwise infer the likely address from the company's known email
  pattern (e.g. `first.last@domain.com`) if that pattern is publicly evidenced (e.g.
  found in a press release, GitHub commit, or the company's own "contact us" page) —
  and store this row with `confidence = "guessed"`. Never claim a guessed email is
  verified. Never attempt SMTP-based brute-force verification against arbitrary
  addresses.
- **Company research must be sourced, not fabricated.** Every fact in a company
  summary should trace back to a URL you actually fetched. Store the source list
  alongside the summary. If you can't find real information, say so in the UI rather
  than inventing funding numbers or headcounts.
- **Data minimization.** Only store business-relevant fields for people (name, title,
  company, LinkedIn URL, work email, source, confidence). Do not collect personal
  details unrelated to their professional role.
- **Provide a data deletion path.** A button/endpoint to purge all people/company
  research data (and, separately, to wipe the whole DB) — this is personal
  job-search tooling, not a data broker, and it should be easy to reset.
- **Respect rate limits.** Company research and people lookups run once per company
  (cached), not on every scrape cycle. Add a minimum re-research interval (default: 7
  days) before refreshing a company's profile.
- **This tool is for the user's own personal job search outreach — not bulk emailing.**
  Don't build any bulk-send/mail-merge feature; it only surfaces contact info for the
  user to reach out manually.

---

## 3. Data model changes (SQLite)

Extend the existing schema (or create it, if starting fresh) with these tables /
columns. Use migrations that are safe to run repeatedly (`CREATE TABLE IF NOT EXISTS`,
guarded `ALTER TABLE ... ADD COLUMN`).

```sql
-- jobs: add normalized remote classification + match results
ALTER TABLE jobs ADD COLUMN remote_type TEXT DEFAULT 'unknown';
  -- one of: fully_remote | hybrid | onsite | unknown
ALTER TABLE jobs ADD COLUMN match_score INTEGER;        -- 0-100, nullable until scored
ALTER TABLE jobs ADD COLUMN match_reasoning TEXT;        -- short natural-language why
ALTER TABLE jobs ADD COLUMN match_matched_skills TEXT;   -- JSON array
ALTER TABLE jobs ADD COLUMN match_concerns TEXT;         -- JSON array, e.g. salary mismatch
ALTER TABLE jobs ADD COLUMN matched_at REAL;

-- one settings row for this single-user tool
CREATE TABLE IF NOT EXISTS user_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    resume_filename TEXT,
    resume_text TEXT,                 -- raw extracted text
    resume_structured TEXT,           -- JSON: {skills:[], years_experience, past_titles:[], education:[]}
    interests TEXT,                   -- JSON array of role/keyword strings
    domains TEXT,                     -- JSON array, e.g. ["B2B SaaS", "Fintech"]
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT DEFAULT 'USD',
    min_years_experience INTEGER,
    max_years_experience INTEGER,
    remote_only INTEGER DEFAULT 1,
    updated_at REAL
);

-- one row per tracked company, refreshed periodically
CREATE TABLE IF NOT EXISTS company_profiles (
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    summary TEXT,                     -- what they do, in plain language
    mission TEXT,
    funding_stage TEXT,
    funding_amount TEXT,
    last_funding_date TEXT,
    employee_count_estimate TEXT,
    tech_stack TEXT,                  -- JSON array, best-effort
    recent_news TEXT,                 -- JSON array of {headline, url, date}
    sources TEXT,                     -- JSON array of URLs used to build this profile
    researched_at REAL
);

-- people at a tracked company
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    name TEXT NOT NULL,
    title TEXT,
    linkedin_url TEXT,
    email TEXT,
    email_confidence TEXT,            -- verified | guessed | none
    source TEXT,                      -- where this record came from
    discovered_at REAL
);

-- cross-process handoff: scraper worker enqueues, agent worker dequeues (§13)
CREATE TABLE IF NOT EXISTS job_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    task_type TEXT NOT NULL DEFAULT 'enrich_and_match', -- or 'rescore_only'
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | in_progress | done | failed
    error TEXT,
    created_at REAL,
    started_at REAL,
    completed_at REAL
);
```

---

## 4. Settings & resume UI

Add a `/settings` page (and matching `/api/settings` GET/POST routes) with:

- Resume upload (PDF or DOCX). On upload:
  - Extract raw text (`pdfplumber` for PDF, `python-docx` for DOCX).
  - Run the **Resume Parser Agent** (see §7) to produce `resume_structured` JSON.
  - Store both raw text and structured JSON in `user_settings`.
- Form fields: interests (free-text tags/keywords, e.g. "backend", "developer
  relations", "founding engineer"), preferred company domain(s), salary range (min/max
  + currency), years of experience range, remote-only toggle (default on).
- Save persists to `user_settings` (id=1, upsert). Reloading the app must show the
  previously saved values — verify this by restarting the process during testing.
- After saving, offer a "Re-score all jobs" button that re-runs the Matching Agent
  (§7) against every stored job with the updated profile.

---

## 5. Remote-only filtering

Update `ats_scraper.py`'s per-platform scrapers to derive `remote_type` using, in
priority order:

1. **Platform-native remote flags** where they exist (Ashby's `isRemote`, Workable's
   `location.telecommuting`, SmartRecruiters' `location.remote`).
2. **Explicit keyword match** on the location/title string (`"remote"`, `"anywhere"`,
   `"distributed"`) → `fully_remote`; (`"hybrid"`) → `hybrid`; otherwise if a specific
   city/office is named with no remote keyword → `onsite`.
3. Otherwise `unknown` — surface these in the UI rather than silently dropping them,
   so the user can judge for themselves.

Add `remote_only` as a first-class filter in `GET /api/jobs` (default **on**, driven by
`user_settings.remote_only`), and a visible remote/hybrid/onsite badge on every job
card regardless of filter state.

---

## 6. Company Research Agent

Purpose: given a company name + domain/website, produce the `company_profiles` row.

Recommended implementation: call the Anthropic Messages API with the built-in
server-side web search tool enabled, so the model can search and cite sources in one
call, rather than standing up a separate search API integration:

```python
import anthropic
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

response = client.messages.create(
    model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    max_tokens=1500,
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    messages=[{
        "role": "user",
        "content": (
            f"Research the company '{company_name}' ({company_website}). "
            "Return strict JSON with keys: summary, mission, funding_stage, "
            "funding_amount, last_funding_date, employee_count_estimate, "
            "tech_stack (array), recent_news (array of {headline,url,date}), "
            "sources (array of URLs actually used). If something can't be found, "
            "use null rather than guessing."
        ),
    }],
)
```

Parse the JSON out of the response (strip code fences defensively), validate it, and
upsert into `company_profiles`. Only run this when a company has no profile yet, or
its `researched_at` is older than the re-research interval (default 7 days,
configurable via env var).

---

## 7. People/Contact Finder Agent

Purpose: given a company, produce rows in `people`.

1. Use the same web-search-enabled Messages API call pattern as §6 to ask for
   publicly named people at the company (founders, hiring managers, engineers on the
   team page, etc.) and their **publicly listed** LinkedIn profile URL, returned as
   JSON: `[{name, title, linkedin_url, source}]`. Do not ask the model to fetch or
   reproduce LinkedIn page content — only the profile URL.
2. For email, in this order:
   - If an enrichment API key is configured (`HUNTER_API_KEY`, `APOLLO_API_KEY`, etc.
     — implement one provider behind a small `find_email(name, domain)` interface so
     others can be swapped in later), call it and mark `email_confidence = "verified"`
     if it returns one.
   - Otherwise, if a company email pattern is evidenced somewhere in the research
     sources (e.g. a public email like `jane@company.com` appeared in a fetched
     page), apply that pattern to the person's name and mark
     `email_confidence = "guessed"`.
   - Otherwise leave `email` null and `email_confidence = "none"`.
3. Same caching rule as company research: don't re-run per scrape cycle.

---

## 8. Resume Parser Agent

Purpose: turn raw resume text into structured JSON for matching.

Single Messages API call, no tools needed:

```
Extract from this resume: {skills: [...], years_experience: number,
past_titles: [...], seniority: string, education: [...], summary: string}.
Return strict JSON only.
```

Store the result in `user_settings.resume_structured`. Re-run whenever a new resume
is uploaded.

---

## 9. Matching Agent

Purpose: score a job against the user's profile.

**Cost-control step first:** before calling the LLM, apply a cheap heuristic
pre-filter — skip scoring (or heavily deprioritize) jobs that obviously fail a hard
constraint the user set (e.g. `remote_only` is on and `remote_type == 'onsite'`).
This keeps LLM calls limited to jobs actually worth evaluating.

For jobs that pass the pre-filter, call the Messages API with the job + company
profile + user's resume_structured + interests + salary range + experience range +
domain preference, and ask for strict JSON:

```
{match_score: 0-100, reasoning: "2-3 sentences", matched_skills: [...],
 concerns: [...]}   // concerns e.g. "salary range not listed", "requires 8+ yrs, resume shows 4"
```

Store the four fields on the `jobs` row plus `matched_at`. Run this:
- Once automatically whenever a new job is discovered by the scraper (picked up by the
  agent worker — see §13).
- On demand via the "Re-score all jobs" button after settings change (enqueued the same
  way, via the `job_queue` table described in §13).

**Orchestration flow** (tie §6–9 together in the **agent worker** process, §13):

```
new job discovered by scraper worker ──▶ row inserted into job_queue (SQLite)
                                                    │
                                                    ▼
                                     agent worker polls job_queue
                                                    │
                                                    ▼
company has a fresh profile? ──no──▶ Company Research Agent ──▶ company_profiles
        │ yes                                                        │
        ▼                                                            │
company has fresh people list? ──no──▶ People Finder Agent ──▶ people│
        │ yes                                                        │
        ▼◀───────────────────────────────────────────────────────────┘
passes remote/hard filters? ──no──▶ skip scoring, still shown if filters allow
        │ yes
        ▼
Matching Agent ──▶ jobs.match_score / match_reasoning
```

Keep each agent as an independent, separately callable Python function/class:
`agents/company_research.py`, `agents/people_finder.py`, `agents/resume_parser.py`,
`agents/matcher.py`. A thin orchestrator in `agent_worker.py` sequences them by polling
`job_queue` (see §13) — this is a **separate process from the scraper**, so LLM-call
latency never blocks discovery of new postings. A heavyweight agent framework
(LangGraph, CrewAI, AutoGen) is not required — plain functions calling the Anthropic
Messages API are sufficient — but you may use one if it doesn't obscure the flow above.

---

## 10. UI changes — separate Vue.js frontend

The UI is being pulled out of Flask/Jinja into a **standalone Vue 3 single-page app**,
built with Vite, living in its own `ui/` project and its own container (§13–§14). It
talks to the API container **only over HTTP** via the existing/extended JSON endpoints
(`/api/jobs`, `/api/settings`, `/api/companies/:id`, etc.) — no server-rendered
templates. `templates/index.html` / `templates/settings.html` can be deleted once the
Vue app covers their functionality; keep them only if you need a working UI mid-build
before the SPA is done (see build order, §15).

Structure (`ui/src/`):
- `views/JobsView.vue` — the main job list/board.
- `views/SettingsView.vue` — settings form.
- `components/JobCard.vue`, `components/CompanyDrawer.vue`, `components/FilterBar.vue`.
- `stores/` (Pinia) for jobs, settings, and filter state.
- `api/client.ts` — thin fetch/axios wrapper around the API base URL, read from a
  build-time env var (`VITE_API_BASE_URL`, defaulting to `http://localhost:8000` in
  dev and to the API container's service name in Docker, e.g. `http://api:8000`, proxied
  through nginx in production — see §14).

Functional requirements (unchanged from before, now as Vue views/components):

- **Job card**: match-score badge (e.g. "87% match"), remote/hybrid/onsite badge, and
  an expandable "Why this matches" line from `match_reasoning`. Default sort becomes
  match score (desc), with a toggle back to "Newest."
- **Company drawer/panel** (click company name on a job card): show the research
  summary, funding info, recent news with source links, and the people list (name,
  title, LinkedIn link if found, email + confidence badge if found).
- **Settings view**: resume upload, structured-resume preview (read-only, so the user
  can sanity-check what was extracted), interests/domain/salary/experience form,
  remote-only toggle, "Re-score all jobs" button, and a "Delete all research data" /
  "Wipe database" action per §2.
- Filter bar gains a remote-only toggle (defaulting to the saved setting) alongside
  the existing status filter and search box.
- The API must send permissive CORS headers (or the dev server must proxy `/api/*`) so
  the Vue dev server (`vite dev`, typically port 5173) can call the API container
  directly during local development.

---

## 11. Config

Add a `.env.example` (never commit real secrets):

```
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-6
HUNTER_API_KEY=            # optional, email enrichment
COMPANY_RESEARCH_TTL_DAYS=7
SCRAPE_INTERVAL_SECONDS=600
AGENT_POLL_INTERVAL_SECONDS=5
DB_PATH=./data/jobradar.db  # overridden to /data/jobradar.db inside containers
RUN_SCRAPER_IN_APP=false    # set true only for non-Docker local dev without scraper_worker.py running separately
```

Every enrichment feature must degrade gracefully (skip, don't crash) when its API key
is absent.

---

## 12. Expected file layout after this build

```
jobradar/
  app.py                   # Flask API only — no scraper/agent loop runs in-process
  db.py
  ats_scraper.py
  scraper_worker.py        # scraper as an asyncio task; see §13
  agent_worker.py          # agent/matching orchestrator as an asyncio task; see §13
  agents/
    __init__.py
    resume_parser.py
    company_research.py
    people_finder.py
    matcher.py
  queue.py                 # job_queue helpers shared by scraper_worker/agent_worker
  templates/                # legacy Jinja UI, retained only per §10's fallback note
    index.html
    settings.html
  requirements.txt        # add: anthropic, pdfplumber, python-docx
  .env.example
  README.md               # update with new setup steps
  NOTES.md                # any assumptions you made along the way
  Dockerfile               # shared base image for api/scraper/agent (see §14)
  docker-compose.yml
  data/                     # bind-mounted volume for sqlite file (gitignored)
  ui/                       # separate Vue 3 + Vite frontend project
    Dockerfile
    package.json
    vite.config.ts
    nginx.conf              # prod static-serve + /api reverse proxy
    src/
      main.ts
      App.vue
      views/
        JobsView.vue
        SettingsView.vue
      components/
        JobCard.vue
        CompanyDrawer.vue
        FilterBar.vue
      stores/
      api/
        client.ts
```

---

## 13. Process architecture — scraper as an independent async worker

The scraper must satisfy two requirements simultaneously:

1. **Autostart** — when the scraper-worker container starts, it immediately begins
   running the scrape loop as a background `asyncio` task, with no manual trigger
   needed.
2. **Independently runnable** — the same scraper logic can be invoked standalone
   (e.g. `python scraper_worker.py --once` for a single pass, or
   `docker compose run scraper-worker python scraper_worker.py --once`), for manual
   backfills or debugging, without needing the API or agent-worker running.

Concretely:

- `ats_scraper.py` keeps the actual per-platform scraping functions (Greenhouse,
  Lever, Ashby, etc.) as plain async functions — no change to their logic, only to
  what calls them.
- `scraper_worker.py` is the **entrypoint** for the scraper container:
  - `async def scrape_loop()`: loops forever, calling each platform scraper every
    `SCRAPE_INTERVAL_SECONDS`, upserting new/changed jobs into `jobs`, and — for every
    genuinely new job — inserting a row into `job_queue` (task_type
    `enrich_and_match`) so the agent worker picks it up. Never call agents directly
    from here; the scraper's only job is discovery + enqueueing.
  - `if __name__ == "__main__":` supports two modes via an argument or env var:
    `--loop` (default; runs `scrape_loop()` forever — used by the container's default
    CMD) and `--once` (runs a single scrape pass and exits — used for manual/backfill
    runs).
  - Exposes `start_background(loop)` — a small helper that schedules `scrape_loop()`
    as an `asyncio.create_task` — so it can be imported and started from `app.py`
    too, **only when `RUN_SCRAPER_IN_APP=true`** is set (default `false`; leave unset
    in Docker, since the dedicated scraper-worker container handles it there). This
    keeps `python app.py` alone useful for quick local dev without Docker or a second
    process, per §11's env vars, while Docker Compose keeps concerns split.
- `agent_worker.py` is the **entrypoint** for the agent-worker container: an
  `asyncio` loop that polls `job_queue` for `pending` rows (short poll interval, e.g.
  every 5s, configurable via `AGENT_POLL_INTERVAL_SECONDS`), marks each `in_progress`,
  runs the §9 orchestration flow (Company Research → People Finder → Matching Agent)
  for that job, and marks the row `done` or `failed` (with `error` populated). Also
  supports `--once` for a single drain-the-queue pass. The "Re-score all jobs" button
  (§4) works by having the API enqueue one `job_queue` row per job with task_type
  `rescore_only`, which the agent worker treats as "skip research if already fresh,
  always re-run the Matching Agent."
- `app.py` (the API container) never runs `scrape_loop()` or the agent poll loop
  itself in the Docker deployment — it only reads/writes SQLite directly and serves
  HTTP. This keeps the API responsive regardless of scraper/LLM latency.
- All three processes (api, scraper-worker, agent-worker) open the **same** SQLite
  file. Use WAL mode (`PRAGMA journal_mode=WAL`) in `db.py`'s connection setup so
  concurrent readers/writers across processes don't lock each other out, and keep
  transactions short (no long-held write transactions spanning an LLM call).

---

## 14. Dockerization

Everything runs under Docker Compose: **one API container, two worker containers
(scraper + agent), and one UI container**, plus a persistent volume for the SQLite
file.

```
jobradar/
├── Dockerfile              # shared image: python deps + app code (api/scraper/agent
│                            #   all reuse this image, just with a different CMD)
├── docker-compose.yml
├── data/                    # host bind-mount → /data in every backend container
└── ui/
    └── Dockerfile           # multi-stage: vite build → nginx serving the static bundle
```

**`Dockerfile`** (shared by api / scraper-worker / agent-worker):
- `FROM python:3.12-slim`, install `requirements.txt`, copy the app source.
- No `CMD` baked in beyond a sensible default (e.g. runs the API) — each service in
  `docker-compose.yml` overrides `command:` to pick its entrypoint, so one image
  serves all three backend roles instead of building three near-duplicate images.

**`docker-compose.yml`** (shape, not literal — adapt names/ports as needed):

```yaml
services:
  api:
    build: .
    command: python app.py
    ports: ["8000:8000"]
    environment:
      - DB_PATH=/data/jobradar.db
    env_file: .env
    volumes:
      - ./data:/data
    restart: unless-stopped

  scraper-worker:
    build: .
    command: python scraper_worker.py --loop
    environment:
      - DB_PATH=/data/jobradar.db
    env_file: .env
    volumes:
      - ./data:/data
    restart: unless-stopped
    depends_on: [api]

  agent-worker:
    build: .
    command: python agent_worker.py --loop
    environment:
      - DB_PATH=/data/jobradar.db
    env_file: .env
    volumes:
      - ./data:/data
    restart: unless-stopped
    depends_on: [api]

  ui:
    build: ./ui
    ports: ["5173:80"]
    environment:
      - VITE_API_BASE_URL=http://localhost:8000
    depends_on: [api]
    restart: unless-stopped

volumes:
  data:
```

Key points:
- **SQLite + persistent storage**: the `./data` bind mount (or a named Docker volume,
  either is fine — bind mount is easier to inspect/back up locally) holds
  `jobradar.db` and is shared by `api`, `scraper-worker`, and `agent-worker` so all
  three see the same database. It must survive `docker compose down` (only
  `docker compose down -v` should wipe it) — this is what makes "wipe database" (§2)
  an explicit user action rather than something that happens on every redeploy.
- **Scraper independence**: `docker compose run --rm scraper-worker python
  scraper_worker.py --once` runs a single scrape pass without booting the whole
  stack, satisfying the "start independently" requirement at the container level too.
- **UI container**: `ui/Dockerfile` is a multi-stage build — `node` stage runs
  `npm run build` to produce static assets, final stage copies them into an `nginx`
  image. `ui/nginx.conf` serves the built SPA and (in production) reverse-proxies
  `/api/*` to the `api` service by Docker Compose service name, so the browser only
  ever talks to one origin and CORS isn't needed outside local dev.
- `.env` (real secrets, gitignored) is read by all three backend services via
  `env_file`; `.env.example` (§11) stays the template committed to the repo.
- Update `README.md` with `docker compose up -d`, `docker compose logs -f
  agent-worker`, and the standalone scrape command as the primary way to run the
  whole system.

---

## 15. Build order (keep the app runnable at each phase)

1. Schema migrations (§3, including `job_queue`) + `.env.example` +
   `requirements.txt` updates.
2. Settings page + resume upload + resume parser agent (§4, §8) — verify persistence
   survives an app restart. Build against the existing Jinja templates for now if the
   Vue app isn't ready yet, so there's always a working UI to test against.
3. Remote classification in the scraper + remote-only filter end-to-end (§5).
4. Split the scraper out into `scraper_worker.py` per §13 (loop + `--once` +
   `job_queue` enqueueing) and verify it runs standalone, outside Docker, first.
5. Company Research Agent + People Finder Agent + Matching Agent, wired together in
   `agent_worker.py` polling `job_queue` (§6, §7, §9).
6. Scaffold the Vue app in `ui/` (§10) — jobs view, settings view, company drawer —
   calling the Flask API's existing JSON endpoints. Retire the Jinja templates once
   it's at parity.
7. Dockerize: shared `Dockerfile`, `ui/Dockerfile`, `docker-compose.yml`, persistent
   `./data` volume (§14). Bring the full stack up with `docker compose up -d` and
   confirm all four containers see the same data.
8. Data-deletion controls, `.env.example` finalized, README updated, smoke-test the
   full pipeline end to end (Docker Compose, not just local processes) with at least
   one real tracked company.

## 16. Definition of done

- Restarting the app (and, once dockerized, restarting/recreating any individual
  container) preserves settings, resume, all researched companies/people, and all
  match scores — nothing lives only in memory, and the SQLite file survives
  `docker compose down` (not `-v`).
- With `remote_only` on, no onsite job appears in the default job list.
- Every company with at least one tracked job has a `company_profiles` row with
  non-empty `sources`.
- Every job has a `match_score` within a few seconds of being scraped (or is queued
  and appears shortly after) once a resume and settings exist; before settings exist,
  jobs show with no score and the UI explains why.
- No code path scrapes linkedin.com directly.
- Missing optional API keys (Hunter, etc.) never crash the app — enrichment for that
  feature is simply skipped.
- `docker compose up -d` brings up all four services (api, scraper-worker,
  agent-worker, ui) against the same persistent SQLite volume, and the Vue UI at its
  published port shows live data from the API.
- The scraper starts automatically when its container starts, and can also be run as
  a one-off (`docker compose run --rm scraper-worker python scraper_worker.py
  --once`) without the rest of the stack.
