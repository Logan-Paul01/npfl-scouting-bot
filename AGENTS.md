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
5. **Storage stays in the free tier.** Google Sheets is the database; Google Drive only holds a copy of this manual. No media files in the repo.
6. **Request discipline.** Cache HTTP responses on disk, cap per-run request counts, respect `429`/`Retry-After`, and never run unbounded loops against a source.
7. **Data honesty over completeness.** If a stat is not available for free, the field stays blank. Never estimate, interpolate or invent a number to fill a column.

Hardware assumption: any machine that can run Python 3.11+ and `pip install -r requirements.txt` (≈ a 2 GB RAM laptop) and a GitHub Actions runner.

---

## 3. Approved Tech Stack (Do Not Deviate)

| Layer | Choice | Notes |
|---|---|---|
| Interface | **Telegram Bot** — `python-telegram-bot` | Free Bot API. Implemented in `bot.py` (Phase 2). |
| Database / storage | **Google Sheets** via `gspread` | Tabs act as tables. `sheets.py` is the only module allowed to touch Sheets. |
| AI brain | **Groq** `llama-3.3-70b-versatile` | **Tool-calling mandatory.** The LLM must query Python functions that read the Sheet. It never guesses or hallucinates statistics. |
| Tactical visuals | `mplsoccer` + `matplotlib` | Dark mode: background `#121826`. Pitch maps, shot maps, passing networks. Phase 3. |
| Video intelligence (Method A) | `youtube-transcript-api` → Groq → timestamp links | Store `https://youtu.be/{video_id}?t={seconds}`. No video files. |
| Data ingestion | **Sofascore** (primary), **Flashscore / FootyStats** (fallback) | Fixtures, form, lineups, incidents, shot coordinates. |
| Scheduling | **GitHub Actions** (`.github/workflows/daily_ingest.yml`) | Morning ingest, secrets injected from repo settings. |

### 3.1 Sheet schema (source of truth: `sheets.py::TAB_SCHEMAS`)

- `NPFL_Matches` — fixtures, results, kickoff (UTC + Lagos), venue, status, source URL. Key: `match_id`.
- `NPFL_Team_Stats` — standings + derived form, goals, and sampled xG/possession/shots. Key: `season` + `team_id`.
- `NPFL_Lineups` — starters/subs, shirt numbers, formation, average positions (`avg_x`, `avg_y` on a 0–100 pitch). Key: `match_id` + `team_id` + `player_id`.
- `NPFL_Events` — goals, cards, substitutions with minute and (when exposed) shot coordinates `x`,`y`. Key: `match_id` + `event_key`.
- `Video_Tactical_Clips` — one row per tactical clip with `clip_url` and `timestamp_seconds`. Key: `clip_id`.
- `Video_Queue` — YouTube URLs awaiting processing. Key: `video_url`.

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

- **Never hardcode** API keys, tokens, sheet IDs, service-account emails or any credential. Load everything from environment variables: `.env` locally (via `python-dotenv`), **GitHub Secrets** in CI.
- Never commit: `.env`, `credentials.json`, service-account JSON, `token.json`, `__pycache__/`, `.venv/`, temporary `.png` outputs, `.cache/`.
- `.gitignore` is created and maintained first; if a secret is ever committed, rotate the key rather than rewriting history silently.
- Google Sheets access uses a **service account** shared on the single spreadsheet (`GOOGLE_SHEET_ID`), not a personal OAuth login.
- Least privilege: the service account only needs edit access to that one Sheet and (optionally) one Drive folder.
- Logs must never print secrets or full credential JSON. Log lengths/prefixes only if a debug fingerprint is needed.
- `bot.py` installs a logging filter that redacts `TELEGRAM_BOT_TOKEN` and silences `httpx` request logging (httpx prints full URLs, which contain the token). A token that ever reaches a log is treated as compromised and rotated in @BotFather.
- Secrets are only ever read in the workflow `env:` block from `secrets.*`.
- CI must never write to `main` or push commits; it only writes to Google Sheets.

### Required environment variables / secrets

| Name | Where | Purpose |
|---|---|---|
| `GOOGLE_SHEET_ID` | `.env` + Secret | Spreadsheet that acts as the DB |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Secret (JSON string) | Service-account credentials (CI) |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `.env` (default `credentials.json`) | Local credentials file |
| `GROQ_API_KEY` | `.env` + Secret | LLM calls |
| `GROQ_MODEL` | optional | Defaults to `llama-3.3-70b-versatile` |
| `SOFASCORE_NPFL_TOURNAMENT_ID` | optional | Override auto-discovery of the NPFL tournament id |
| `FOOTYSTATS_API_KEY` | optional Secret | Enables the FootyStats fallback |
| `DRIVE_FOLDER_ID` | optional Secret | Target folder for the `AGENTS.md` mirror |
| `NPFL_CACHE_TTL` | optional | HTTP cache TTL in seconds (default 1800) |
| `TELEGRAM_BOT_TOKEN` | `.env` + Secret | Bot token from @BotFather (secret-store spellings such as `Telegram_Bot_Token` are normalised) |
| `TELEGRAM_ALLOWED_USER_IDS` | `.env` + Secret | Comma-separated Telegram user ids allowed to use the bot |
| `TELEGRAM_ALLOW_ALL` | optional, local only | `1` bypasses the allowlist — never set in production |
| `NPFL_BOT_CACHE_TTL` | optional | Seconds to cache Sheet reads inside the bot (default 60) |
| `NPFL_LLM_COOLDOWN` | optional | Per-user seconds between LLM calls (default 8) |
| `NPFL_STORAGE` | optional | `sheets` (default, production) or `local` (credential-free JSON store for testing) |
| `NPFL_LOCAL_STORE` | optional | Path for the local store (default `.cache/npfl_local_store.json`, gitignored) |

---

## 5. Google Drive Manual Sync

`drive_sync.py` mirrors this file to Drive whenever credentials exist:

- Uses `google-api-python-client` (`drive` v3) with the same service account.
- Finds an existing `AGENTS.md` by name (optionally scoped to `DRIVE_FOLDER_ID`) and **updates** it, otherwise creates it.
- Called at the end of the daily workflow as an **optional** step: if no credentials are present it logs a skip and exits `0`, so it can never break ingestion.
- Only this manual is synced. No data, no media, no credentials.

### 5.1 Local test store (temporary, credential-free)

Until the Google service account exists, the full stack can be exercised against a JSON file that mimics the subset of gspread calls we use:

```bash
python scrape_npfl.py --demo --write --local     # seed .cache/npfl_local_store.json from bundled payloads
python scrape_npfl.py --local                    # real ingest into the local store
python bot.py --check --local                    # validate config against the local store
NPFL_STORAGE=local python bot.py                 # run the bot on local data
```

`sheets.py` owns this switch (`NPFL_STORAGE=local`); it is a **testing** backend only, never the production path, and the store file is gitignored. It exists so bot behaviour and parsing can be validated before any Google credentials are issued.

---

## 6. Repository Layout

```
AGENTS.md                       # this manual (auto-read each session, mirrored to Drive)
.gitignore                      # blocks secrets, caches, temp images
requirements.txt                # pinned-ish dependency set
sheets.py                       # only module that touches Google Sheets (schemas + upsert)
scrape_npfl.py                  # Phase 1: Sofascore -> (FootyStats/Flashscore fallback) -> Sheets
ingest_youtube.py               # Phase 1: transcript -> Groq -> timestamped tactical clips
drive_sync.py                   # Optional: mirror AGENTS.md to Google Drive
bot.py                          # Phase 2: Telegram commands + Groq tool-calling over the Sheet
.github/workflows/daily_ingest.yml   # Morning scheduled ingestion
```

Phase 3 (not built yet): `pitch_maps.py` (mplsoccer dark-mode visuals).

### 6.1 CLI surface (current)

```bash
python scrape_npfl.py --demo                  # offline parser smoke test, no network, no writes
python scrape_npfl.py --dry-run               # fetch + normalise, print rows, do not write
python scrape_npfl.py                         # full ingest into Google Sheets
python ingest_youtube.py --url <YT_URL> --match-id <ID> [--dry-run]
python ingest_youtube.py --add <YT_URL> --match-id <ID>
python ingest_youtube.py --queue              # process every pending row of Video_Queue
python drive_sync.py --file AGENTS.md [--folder-id <ID>]

python bot.py --check        # validate token / Sheet / Groq config, then exit
python bot.py                # start Telegram polling
```

### 6.2 Bot surface (`bot.py`)

| Command | Behaviour |
|---|---|
| `/preview <team> vs <team>` / `/preview <match_id>` | Deterministic dossier from the Sheet (fixture, both team snapshots, goal-timing windows, head-to-head, clips) plus a Groq "coach brief" that may only restate those facts. |
| `/form <team>` | Table row, form string, recent results. |
| `/match <match_id>` | Fixture dossier + incident timeline. |
| `/clips <match_id>` | Timestamped YouTube clips with exact-second links. |
| `/teams`, `/id`, `/refresh`, `/start`, `/help` | Utility commands. |
| Free text / `/ask` | Groq tool-calling loop with `search_matches`, `get_match`, `get_team_stats`, `get_match_events`, `get_lineups`, `get_clips`. Facts come only from tool results in the same turn. |

Access control: default **deny** unless the Telegram user id is in `TELEGRAM_ALLOWED_USER_IDS` (or `TELEGRAM_ALLOW_ALL=1` for local testing). The bot tells unknown users their id so the admin can add it. Deterministic commands never call the LLM; free text is rate-limited per user.

---

## 7. Milestones & Checklist

Update this checklist at the end of **every** task. `[x]` means implemented **and** verified (at minimum: compiles, and the offline/demo path runs).

### Phase 0 — Project setup
- [x] `AGENTS.md` project manual (purpose, budget, stack, security, checklist)
- [x] `.gitignore` blocking `.env`, `credentials.json`, `__pycache__/`, temp `.png`, `.cache/`, `.venv/`
- [x] `requirements.txt`
- [x] `sheets.py` — tab schemas, service-account auth, idempotent upsert, read helpers
- [x] `sheets.py` — local JSON backend (`NPFL_STORAGE=local`) for credential-free end-to-end tests

### Phase 1 — Data & video ingestion
- [x] `scrape_npfl.py` — Sofascore fixtures/results, standings, form, lineups, incidents, shot coordinates
- [x] `scrape_npfl.py` — FootyStats + Flashscore fallback adapters (basic fixtures, gracefully degrading)
- [x] `scrape_npfl.py` — upsert into `NPFL_Matches` / `NPFL_Team_Stats` / `NPFL_Lineups` / `NPFL_Events`
- [x] `scrape_npfl.py` — `--demo` offline parser test, `--dry-run`, request cache and retry/backoff
- [x] `ingest_youtube.py` — URL → transcript (with description-chapters and goal-minute fallbacks)
- [x] `ingest_youtube.py` — Groq tool-calling extraction of structured tactical events
- [x] `ingest_youtube.py` — `Video_Tactical_Clips` upsert with `&t={seconds}` links, dedupe, queue mode
- [x] `.github/workflows/daily_ingest.yml` — daily cron + manual dispatch + optional Drive mirror
- [x] `drive_sync.py` — optional `AGENTS.md` → Google Drive mirror

### Phase 2 — Telegram bot
- [x] `bot.py` — `python-telegram-bot` entrypoint, allowlist enforced (default deny)
- [x] Groq `llama-3.3-70b-versatile` with tools: `search_matches`, `get_match`, `get_team_stats`, `get_match_events`, `get_lineups`, `get_clips`
- [x] `/preview <home> vs <away>` dossier: form, goal-timing, head-to-head, clip links + facts-only coach brief
- [x] Hard refusal path when a stat is absent (deterministic commands render "no stored data" instead of a number)
- [x] `/form`, `/match`, `/clips`, `/teams`, `/id`, `/refresh` plus free-text `/ask` with per-user cooldown
- [x] Handler-level end-to-end run against the local store: real ingest → `/teams`, `/preview`, `/form`, `/match`, `/clips` (Telegram transport validated separately by `getMe` + polling startup)
- [ ] Deploy: run on a free-tier host or the owner's laptop; no paid infra (needs the owner's machine or a free host — not built yet)

### Phase 3 — Tactical visuals (not started)
- [ ] `pitch_maps.py` — dark-mode mplsoccer shot map (`#121826`)
- [ ] `pitch_maps.py` — average-position map from `NPFL_Lineups.avg_x/avg_y`
- [ ] `pitch_maps.py` — passing network from exposed passing data (skip cleanly if absent)
- [ ] Bot sends generated PNGs, then deletes temp files (files stay out of the repo)

### Backlog / ideas (not approved yet)
- [ ] Per-team scouting notes generated weekly and stored in a `Scouting_Notes` tab
- [ ] National-team filter for NPFL-based call-ups
- [ ] Coverage audit tab: which matches lack lineups/shot coordinates (data-quality gate)

---

## 8. Working Agreements

- Small, reviewable commits with messages that say *why*.
- New data source? Add it as an adapter behind the existing normalisers; never let a fallback change the Sheet schema.
- Never widen scope silently. If something is out of budget or out of the pre-match scope, say so and stop.
- Keep this manual current: update sections 3.1, 6 and 7 whenever schemas, files or milestones change.
