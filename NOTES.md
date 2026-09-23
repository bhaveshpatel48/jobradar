# Build notes & assumptions

Decisions made while building, where the spec left room or turned out to be
wrong in practice.

## Deviations from the spec

**`queue.py` → `task_queue.py`.** The spec's §12 names the file `queue.py`, but
that shadows Python's stdlib `queue`, and `anyio` (a transitive httpx dependency)
does `from queue import Queue` internally. With `queue.py` in the project root,
every async HTTP call raised `ImportError: cannot import name 'Queue'` — it broke
both workers at runtime, not import time. Renamed.

**Gemini instead of Anthropic.** Requested. Consequences:

- Grounded search and structured-JSON output are mutually exclusive on Gemini
  2.x (server-side 400), and on 3.x the combination reportedly returns empty
  grounding metadata. `llm/gemini.py:grounded_json` therefore makes two calls:
  grounded research, then tool-free JSON extraction of those notes.
- Grounding citations are `vertexaisearch.cloud.google.com` redirect URLs, not
  publisher URLs, and `web.domain` is usually null. Each citation is resolved
  with a HEAD request before being stored, so `sources` holds real URLs per §2.
- Default model is a 3.x GA model, because Google limits 2.5-era models to
  projects with prior usage — a fresh key can fail on `gemini-2.5-*`.

**Jinja templates dropped.** §10 allowed keeping them as a mid-build fallback.
The Vue SPA covers both views, so `templates/` was never created.

**gunicorn in the API container.** The spec's compose sketch used `python app.py`
(Flask's dev server). Swapped for gunicorn with threaded workers, which suits
SQLite/IO-bound handlers. `python app.py` still works for local dev.

**`API_PORT` split into three variables.** The spec used one name for both the
host port mapping and the port the app binds. Now `API_PORT` is the dev-server
bind port, and `API_HOST_PORT` / `UI_HOST_PORT` are the Docker host mappings.
Defaults are 8088/5174 because 8000 and 5173 were already occupied on this
machine.

## Additions

- **`attempts` column on `job_queue`** (not in §3's DDL). Failed tasks retry
  3 times then park as `failed`; without a counter a poison task loops forever.
- **`reset_stale()`** requeues `in_progress` rows orphaned by a worker restart.
- **`companies` and `jobs` tables defined in full.** §3 assumed they already
  existed, but the working directory was empty, so this is a from-scratch build.
- **Company CRUD endpoints.** Needed something to seed the scraper with; the
  spec never said how companies get tracked.

## Interpretations

- **`remote_only` includes `unknown`.** §5 says surface unknowns rather than
  silently dropping them, so the filter matches `fully_remote` OR `unknown`.
  Only `onsite` and `hybrid` are excluded.
- **Prefilter is advisory.** §9's cheap prefilter skips scoring for onsite jobs
  when `remote_only` is on. Those jobs stay in the DB and are still listed when
  the filter is off — they just carry no score.
- **Email pattern inference** only fires when a real address at the company
  domain appeared in research output, and only recognizes `first.last` and
  `first_last`. Anything else stays `none` rather than guessing a convention.

## Verified

Against a live Greenhouse board (627 postings) and the running stack:

- Scrape → enqueue → drain pipeline, with no duplicates on re-scrape.
- All four containers sharing one WAL SQLite file concurrently.
- `remote_only` on ⇒ zero onsite jobs in the default list.
- Settings, jobs and queue state surviving `docker compose down && up`.
- Standalone `scraper_worker.py --once` in a container with nothing else running.
- SPA rendering live data through the nginx `/api` proxy, no console errors.

## Round 2: more ATS, scope, discovery, engineering filter, AI search

### What the real Gemini key showed

- **Free tier = 20 requests/day/model** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`).
  I first read the 429s as a per-minute limit, which was wrong. The quota ID in
  Google's error settled it. Handling: a daily-exhaustion flag in `app_state`
  stops every container calling until midnight Pacific; a UI banner explains it;
  "Retry now" clears the flag after a quota increase.
- **No Google Search grounding quota** on this key (429 on every model), so
  research/people fall back to fetching the company's own site. A circuit breaker
  avoids re-trying grounding for 30 minutes.
- `gemini-2.5-*` returns 404 for this key, as expected.

### Bugs found and fixed

- **Event loop reuse:** the single cached Gemini client failed on its second use
  in a new event loop ("Event loop is closed"), so the second resume upload would
  have crashed. Clients are now cached per loop.
- **Re-research storm:** freshness was keyed on *having* a profile or people rows,
  so a company where nothing was found got researched again for every one of its
  jobs. Attempt timestamps on `companies` now gate it.
- **Quota errors escaped agents:** the agents caught only `ValueError`, so Gemini
  API errors failed whole tasks. Errors are now typed (`LLMError`,
  `LLMRateLimited`, `LLMQuotaExhausted`, `GroundingUnavailable`). A rate limit
  releases the task without using up a retry.
- **Settings clobbering:** `POST /api/settings` wrote every field, so saving one
  page reset another's settings (e.g. scrape scope). It's now a partial update.
- **Descriptions wiped:** re-scraping boards that have no list descriptions
  overwrote fetched descriptions with "". The upsert now keeps them.
- **Per-process throttling was blind:** the API and agent worker shared one quota
  but throttled separately. They now share a ledger table (`llm_calls`), and
  background work leaves a reserve for interactive calls.

### Decisions

- **Discovery never trusts the model.** Candidates are stored only after a live
  board probe returns jobs. Tested with fixed proposals: a wrong ATS hint was
  corrected, and a made-up company was rejected.
- **Engineering = title-first.** Departments are too mixed ("Security" covers
  analysts, "Applied AI" covers pre-sales). A non-engineering department overrides
  unless the title names an engineer. Non-software disciplines are excluded.
  Cases live in `tests/test_classification.py`.
- **Only engineering jobs are queued for scoring** when the filter is on, to
  save quota. Toggling the filter off and pressing Re-score picks the rest up.
- **Per-company cap of 1000 postings** (Workday and SmartRecruiters boards reach
  thousands). NVIDIA Workday takes about 60s for 1000 jobs because pages are
  capped at 20.
- **Descriptions fetched lazily**, only for jobs about to be scored. Detail APIs
  (SmartRecruiters, BambooHR, Rippling, Dover) come first, then JobPosting
  JSON-LD, then page text.
- **Comeet and Jobvite are not supported**: Comeet needs a token scraped out of
  HTML, and Jobvite is HTML only.

## Round 3: fit buttons, company grouping + pagination, salary

### Incident: the test suite wrote to the live database

I moved test isolation into `tests/__init__.py`. But `unittest discover tests`
imports test files as top-level modules, so that file never ran, and the tests
fell back to `.env`'s `DB_PATH`, the live database. One run cleared
`companies`, `jobs` and `job_queue`, and overwrote `user_settings.interests`
and `resume_structured`.

- **Restored:** the Linear company (re-scraped, 31 jobs) and interests
  `["backend", "Python", "Senior Software Engineer"]`, the last value read
  before the incident.
- **Not recoverable:** original first-seen dates, any per-job status, and the
  parsed resume. The resume text survived, so re-uploading re-parses it.
- A snapshot taken just before the restore is at `data/jobradar.before-restore.db`.
- **Fix:** every test module imports `tests` first, and `assert_isolated()`
  refuses to run against anything but the temp database. I verified the live DB
  was unchanged across `discover tests`, `discover -s tests -t .` and
  single-module runs.

### Also fixed

- **Silent non-scores:** transient Gemini 503s were caught inside the matcher,
  which returned "no result", and the task was then marked done, so jobs were
  never scored or retried. 5xx errors are now retried once, then raised as
  `LLMTemporarilyUnavailable`, which requeues the task.
- **Retry cost:** the SDK doesn't retry by default (no double retries), but
  failed requests appear to count toward the 20/day free quota. So there's
  one transient retry, not several.
- "Check fit for all N" counted jobs that were already queued; it now counts
  only jobs still needing a request.
- A Greenhouse hourly range showed without a period ("$45.2–80.3"); the range
  title now drives the period, falling back to magnitude.
- A resume that failed to parse because the quota was spent said "needs a
  Gemini API key"; it now gives the real reason.

## Not verified

- **A live "Check my fit" score.** The quota was spent before it could run.
  The flow was checked with the matcher stubbed, both in the API tests and in
  a browser preview (82% badge, "Why this matches", "Re-check fit").
- **Live AI search and live discovery.** Both are built and exercised to the
  point of the Gemini call, and discovery's verification half was tested with
  fixed proposals. The daily quota ran out before a live end-to-end run.
  Worth checking once the quota resets.
- BambooHR `locationType` → remote mapping (no remote example found live).
- Whether Teamtailor's RSS feed caps its item count.
- Lever EU boards (`api.eu.lever.co`).
- Match-score quality on a real parsed resume (so far scored only against
  interests).
