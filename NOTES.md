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

## Not verified

**No Gemini API key was available during the build**, so every LLM path is
verified only for graceful degradation (missing key ⇒ skip, never crash) and
against the SDK's real type signatures — not against live responses. Worth
confirming on first run with a key:

- That the configured model is accessible to your project.
- That grounded calls return `grounding_chunks` (if empty, the fallback is to
  ask for source URLs as a JSON field instead).
- Match-score quality, and whether the matcher prompt needs tuning.
