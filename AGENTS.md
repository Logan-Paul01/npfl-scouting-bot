# AGENTS.md — NPFL Pre-Match Scouting Bot (Project Manual)

> **Read this file at the start of every session. Update the checklist at the bottom at the end of every task.**

**Project Owner / Supervisor:** user (Product Owner)
**Lead Software Engineer:** Buffy (this agent)
**External reviewer:** AI Architect (reviews the GitHub repository)
**Repository:** https://github.com/Logan-Paul01/npfl-scouting-bot (public — the Architect reviews here)
**Last updated:** 2026-11-14

---

## 1. Project Purpose

A **pre-match tactical scouting and analysis assistant** for Nigerian coaches, players and analysts covering the **Nigeria Premier Football League (NPFL)** and the **Nigerian national teams**.

Scope is **strictly pre-match**:

- Opposition scouting dossiers (shape, build-up pattern, pressing behaviour, set-piece habits, weaknesses).
- Team form, results, goal timing and home/away splits.
- Tactical breakdowns written for a coach: short, concrete, actionable sentences.
- Pitch maps: shot maps, average-position maps, passing networks (dark mode).
- Timestamped YouTube clips: every tactical claim links to the exact second of public NPFL/NFF footage.

**Explicitly out of scope (do not build):**

- Live in-match tracking, live scores, live event streaming.
- Real-time video streams, full 90-minute video storage or processing.
- Betting odds, predictions of exact scorelines, gambling features.
- Player transfer/contracts data, social media scraping.

Why: live tracking and raw video processing are the two things that break a $0 budget. Pre-match analysis is the part coaches actually use on matchday-minus-one, and it is cheap to compute.

---

## 2. Strict $0 Budget & Hardware Constraints (Non-negotiable)

We spend **$0** on servers, GPUs, paid APIs, paid data feeds or cloud video. There is no GPU and no always-on machine.

Rules for every future task:

1. **No heavy local computer vision.** No YOLO/OpenCV tracking models, no pose estimation, no per-frame inference, no model training or fine-tuning. Pitch maps are drawn from *coordinates already exposed by the data source* (or nothing) — never inferred from raw pixels.
2. **No raw 90-minute video pipelines.** Never download full match videos. Video intelligence comes from public transcripts and description chapters only (Method A).
3. **No paid APIs, no paid tiers, no trials that expire.** Free tiers only, and the system must degrade gracefully when a free quota runs out.
4. **Everything runs on GitHub Actions (free minutes for public repos) or the owner's laptop.** Scripts must be short-lived batch jobs, not daemons. No paid hosting, no always-on server, no managed database.
5. **Storage stays in the free tier.** SQLite (`scouting.db`) is the database — one file, no server, no cloud account, no console setup. No media files in the repo.
6. **Request discipline.** Cache HTTP responses on disk, cap per-run request counts, respect `429`/`Retry-After`, and never run unbounded loops against a source.
7. **Data honesty over completeness.** If a stat is not available for free, the field stays blank. Never estimate, interpolate or invent a number to fill a column.

Hardware assumption: any machine that can run Python 3.11+ and `pip install -r requirements.txt` (≈ a 2 GB RAM laptop) and a GitHub Actions runner.

---

## 3. Approved Tech Stack (Do Not Deviate)

| Layer | Choice | Notes |
|---|---|---|
| Interface | **Telegram Bot** — `python-telegram-bot` | Free Bot API. Implemented in `bot.py` (Phase 2). |
| Database / storage | **SQLite** — `scouting.db` via stdlib `sqlite3` | One file in the repo root. `db.py` is the only module allowed to touch the database. |
| AI brain | **Groq** `qwen/qwen3.8-27b` (default, override with `GROQ_MODEL`) | **Tool-calling mandatory.** The LLM must query Python functions that read the database. It never guesses or hallucinates statistics. |
| Tactical visuals | `mplsoccer` + `matplotlib` | Dark mode: background `#121826`. Pitch maps, shot maps, passing networks. Phase 3. |
| Video intelligence (Method A) | `youtube-transcript-api` → Groq → timestamp links | Store `https://youtu.be/{video_id}?t={seconds}`. No video files. |
| Data ingestion | **Sofascore** (primary), **Flashscore / FootyStats** (fallback) | Fixtures, form, lineups, incidents, shot coordinates. |
| Scheduling | **GitHub Actions** (`.github/workflows/daily_ingest.yml`) | Morning ingest, secrets injected from repo settings. |

### 3.1 Database schema (source of truth: `db.py::SCHEMAS`)

- `matches` — fixtures, results, kickoff (UTC + Lagos), venue, status, source URL. Primary key: `match_id`.
- `team_stats` — standings + derived form, goals, and sampled xG/possession/shots. Primary key: `season` + `team_id`.
- `lineups` — starters/subs, shirt numbers, formation, average positions (`avg_x`, `avg_y` on a 0–100 pitch). Primary key: `match_id` + `team_id` + `player_id`.
- `events` — goals, cards, substitutions with minute and (when exposed) shot coordinates `x`,`y`. Primary key: `match_id` + `event_key`.
- `video_clips` — one row per tactical clip with `clip_url` and `timestamp_seconds`. Primary key: `clip_id`.
- `video_queue` — YouTube URLs awaiting processing. Primary key: `video_url`.

Every row carries `source` and a `*_at`/`updated_at` UTC timestamp so the Architect can audit provenance.

Derived fields are honest about their window: `form` and `clean_sheets` are computed from the matches actually ingested (Sofascore `last` pages), while `xg_for`/`xg_against`/`avg_possession`/`avg_shots` are only filled when that match's shotmap/statistics were fetched, with `matches_sampled` stating the sample size. Blank means "not available for free", never zero.

### 3.2 Grounding contract for the AI (applies to Phase 2 and any LLM call)

1. Any factual claim about a team, player, result or stat must come from a tool result in the same conversation turn.
2. If no tool returns the value, the answer must say the data is missing. No plausible-sounding numbers.
3. Tool outputs are the only permitted source; the model's own football knowledge may only be used for *interpretation* wording, clearly framed as analysis, never as data.
4. Every video claim must carry a timestamp link produced by `ingest_youtube.py`.

### 3.3 Known source constraints

- **Sofascore blocks datacentre IPs.** `api.sofascore.com` returns `403` from cloud/GitHub-Actions-style addresses; the mobile host `api.sofascore.app` is the probe target when the primary host refuses. This is a source limitation, not a bug: the adapter chain must degrade to FootyStats/Flashscore and the run must report which source actually served data.
- **Flashscore is JS-rendered.** Its results page often contains no parseable match rows; the adapter raises a clear `SourceError` instead of guessing.
- **TheSportsDB** works keyless from datacentres with real NPFL data (league `4827`), but its free key is truncated: ~5 table rows, 1 recent event, and quick `429`s. It proves the pipeline and gives partial results/table; it cannot carry production scouting.
- Consequence: ingestion is verified on a normal residential IP or via a fallback source, never assumed from a cloud run.

---

## 4. Security & GitHub Rules

- **Never hardcode** API keys, tokens, chat IDs or any credential. Load everything from environment variables: `.env` locally (via `python-dotenv`), **GitHub Secrets** in CI.
- Never commit: `.env`, `__pycache__/`, `.venv/`, temporary `.png` outputs, `.cache/`, and the generated `scouting.db` (with its `-journal`/`-wal`/`-shm` sidecars).
- `.gitignore` is created and maintained first; if a secret is ever committed, rotate the key rather than rewriting history silently.
- The database is a local SQLite file (`scouting.db`): nothing is sent to a third party, so there are no database credentials or service accounts to manage.
- The database holds only public football data — never put secrets or personal data in it.
- Logs must never print secrets or full credential JSON. Log lengths/prefixes only if a debug fingerprint is needed.
- `bot.py` installs a logging filter that redacts `TELEGRAM_BOT_TOKEN` and silences `httpx` request logging (httpx prints full URLs, which contain the token). A token that ever reaches a log is treated as compromised and rotated in @BotFather.
- Secrets are only ever read in the workflow `env:` block from `secrets.*`.
- CI must never write to `main` or push commits; it only writes the SQLite file on the runner.

### Required environment variables / secrets

| Name | Where | Purpose |
|---|---|---|
| `NPFL_DB_PATH` | optional | SQLite file to use (default `scouting.db` in the repo root) |
| `GROQ_API_KEY` | `.env` + Secret | LLM calls |
| `GROQ_MODEL` | optional | Defaults to `qwen/qwen3.8-27b` (must be a Groq model with tool use + JSON mode) |
| `SOFASCORE_NPFL_TOURNAMENT_ID` | optional | Override auto-discovery of the NPFL tournament id |
| `FOOTYSTATS_API_KEY` | optional Secret | Enables the FootyStats fallback |
| `NPFL_CACHE_TTL` | optional | HTTP cache TTL in seconds (default 1800) |
| `TELEGRAM_BOT_TOKEN` | `.env` + Secret | Bot token from @BotFather (secret-store spellings such as `Telegram_Bot_Token` are normalised) |
| `TELEGRAM_ALLOWED_USER_IDS` | `.env` + Secret | Comma-separated Telegram user ids allowed to use the bot |
| `TELEGRAM_ALLOW_ALL` | optional, local only | `1` bypasses the allowlist — never set in production |
| `NPFL_BOT_CACHE_TTL` | optional | Seconds to cache database reads inside the bot (default 60) |
| `NPFL_LLM_COOLDOWN` | optional | Per-user seconds between LLM calls (default 8) |

---

## 5. SQLite Database

The whole dataset lives in one file, `scouting.db`, in the repository root:

- Created automatically by `db.ensure_schema()` on the first write, with the tables from §3.1.
- Writes are idempotent upserts keyed on stable IDs, so re-running an ingest updates rows instead of duplicating them.
- `NPFL_DB_PATH` points the stack at a different file (tests, scratch copies).
- The database and its `-journal`/`-wal`/`-shm` sidecars are gitignored: it is generated data, not source.
- Inspect it with any SQLite client: `sqlite3 scouting.db ".tables"` or `sqlite3 scouting.db "select * from matches limit 5"`.

### 5.1 Persistence on GitHub Actions

A workflow run starts from a clean checkout, so a database built on the runner is **not** carried into the next run. The daily workflow uploads `scouting.db` as a 7-day artifact so each run is inspectable, but durable history needs one of: running the ingest where the file lives, committing the database deliberately, or adding a cache/restore step. Until one of those is chosen, the local copy is the source of truth.

---

## 6. Repository Layout

```
AGENTS.md                       # this manual (auto-read each session)
.gitignore                      # blocks secrets, caches, temp images
requirements.txt                # pinned-ish dependency set
db.py                           # only module that touches the database (schemas + upsert)
scouting.db                     # generated SQLite database (gitignored)
scrape_npfl.py                  # Phase 1: Sofascore -> (FootyStats/Flashscore fallback) -> SQLite
ingest_youtube.py               # Phase 1: transcript -> Groq -> timestamped tactical clips
bot.py                          # Phase 2: Telegram commands + Groq tool-calling over the database
.github/workflows/daily_ingest.yml   # Morning scheduled ingestion
```

Phase 3 (not built yet): `pitch_maps.py` (mplsoccer dark-mode visuals).

### 6.1 CLI surface (current)

```bash
python scrape_npfl.py --demo                  # offline parser smoke test, no network, no writes
python scrape_npfl.py --dry-run               # fetch + normalise, print rows, do not write
python scrape_npfl.py                         # full ingest into scouting.db
python ingest_youtube.py --url <YT_URL> --match-id <ID> [--dry-run]
python ingest_youtube.py --add <YT_URL> --match-id <ID>
python ingest_youtube.py --queue              # process every pending row of video_queue

python bot.py --check        # validate token / database / Groq config, then exit
python bot.py                # start Telegram polling
```

### 6.2 Bot surface (`bot.py`)

| Command | Behaviour |
|---|---|
| `/preview <team> vs <team>` / `/preview <match_id>` | Deterministic dossier from the database (fixture, both team snapshots, goal-timing windows, head-to-head, clips) plus a Groq "coach brief" that may only restate those facts. |
| `/form <team>` | Table row, form string, recent results. |
| `/match <match_id>` | Fixture dossier + incident timeline. |
| `/clips <match_id>` | Timestamped YouTube clips with exact-second links. |
| `/teams`, `/id`, `/refresh`, `/start`, `/help` | Utility commands. |
| Free text / `/ask` | Groq tool-calling loop with `search_matches`, `get_match`, `get_team_stats`, `get_match_events`, `get_lineups`, `get_clips`. Facts come only from tool results in the same turn, read from `scouting.db`. |

Access control: default **deny** unless the Telegram user id is in `TELEGRAM_ALLOWED_USER_IDS` (or `TELEGRAM_ALLOW_ALL=1` for local testing). The bot tells unknown users their id so the admin can add it. Deterministic commands never call the LLM; free text is rate-limited per user.

---

## 7. Milestones & Checklist

Update this checklist at the end of **every** task. `[x]` means implemented **and** verified (at minimum: compiles, and the offline/demo path runs).

### Phase 0 — Project setup
- [x] `AGENTS.md` project manual (purpose, budget, stack, security, checklist)
- [x] `.gitignore` blocking `.env`, `credentials.json`, `__pycache__/`, temp `.png`, `.cache/`, `.venv/`
- [x] `requirements.txt`
- [x] `db.py` — SQLite schemas, idempotent upsert, read helpers (stdlib `sqlite3`, no credentials)
- [x] `db.py` — `scouting.db` in the repository root, `NPFL_DB_PATH` override
- [x] Storage migrated from Google Sheets/Drive to local SQLite; the Google Cloud console setup is dropped for the MVP

### Phase 1 — Data & video ingestion
- [x] `scrape_npfl.py` — Sofascore fixtures/results, standings, form, lineups, incidents, shot coordinates
- [x] `scrape_npfl.py` — FootyStats + Flashscore fallback adapters (basic fixtures, gracefully degrading)
- [x] `scrape_npfl.py` — upsert into `matches` / `team_stats` / `lineups` / `events`
- [x] `scrape_npfl.py` — `--demo` offline parser test, `--dry-run`, request cache and retry/backoff
- [x] `ingest_youtube.py` — URL → transcript (with description-chapters and goal-minute fallbacks)
- [x] `ingest_youtube.py` — Groq tool-calling extraction of structured tactical events
- [x] `ingest_youtube.py` — `video_clips` upsert with `&t={seconds}` links, dedupe, `video_queue` mode
- [x] `.github/workflows/daily_ingest.yml` — daily cron + manual dispatch + `scouting.db` artifact

### Phase 2 — Telegram bot
- [x] `bot.py` — `python-telegram-bot` entrypoint, allowlist enforced (default deny)
- [x] Groq `qwen/qwen3.8-27b` with tools: `search_matches`, `get_match`, `get_team_stats`, `get_match_events`, `get_lineups`, `get_clips`
- [x] `/preview <home> vs <away>` dossier: form, goal-timing, head-to-head, clip links + facts-only coach brief
- [x] Hard refusal path when a stat is absent (deterministic commands render "no stored data" instead of a number)
- [x] `/form`, `/match`, `/clips`, `/teams`, `/id`, `/refresh` plus free-text `/ask` with per-user cooldown
- [x] Handler-level end-to-end run against the local store: real ingest → `/teams`, `/preview`, `/form`, `/match`, `/clips` (Telegram transport validated separately by `getMe` + polling startup)
- [ ] Deploy: run on a free-tier host or the owner's laptop; no paid infra (needs the owner's machine or a free host — not built yet)

### Phase 3 — Tactical visuals (not started)
- [ ] `pitch_maps.py` — dark-mode mplsoccer shot map (`#121826`)
- [ ] `pitch_maps.py` — average-position map from `lineups.avg_x/avg_y`
- [ ] `pitch_maps.py` — passing network from exposed passing data (skip cleanly if absent)
- [ ] Bot sends generated PNGs, then deletes temp files (files stay out of the repo)

### Backlog / ideas (not approved yet)
- [ ] Per-team scouting notes generated weekly and stored in a `scouting_notes` table
- [ ] National-team filter for NPFL-based call-ups
- [ ] Coverage audit table: which matches lack lineups/shot coordinates (data-quality gate)

---

## 8. Working Agreements

- Small, reviewable commits with messages that say *why*.
- New data source? Add it as an adapter behind the existing normalisers; never let a fallback change the database schema.
- Never widen scope silently. If something is out of budget or out of the pre-match scope, say so and stop.
- Keep this manual current: update sections 3.1, 6 and 7 whenever schemas, files or milestones change.
