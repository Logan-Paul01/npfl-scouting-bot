"""Telegram front-end: pre-match NPFL scouting over the Google Sheet.

$0 / grounding rules enforced here:
  * Deterministic commands (`/preview`, `/form`, `/match`, `/clips`) read the Sheet
    directly and never invent a number.
  * Free text goes to Groq `llama-3.3-70b-versatile` with tool-calling; the model may
    only state facts returned by those tools in the same turn.
  * No video, no images, no paid services. Per-user cooldown protects the free tier.

Run:
    python bot.py                 # start polling
    python bot.py --check         # validate token / Sheet / Groq config, then exit

Env (see AGENTS.md §4): TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS,
GROQ_API_KEY, GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON|_FILE.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

try:  # local .env support (never committed)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - optional locally
    pass

import requests

import sheets

sheets.bootstrap_env()

try:
    from telegram import LinkPreviewOptions, Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "python-telegram-bot is required: pip install -r requirements.txt"
    ) from exc

LOG = logging.getLogger("npfl-bot")


class _TokenRedaction(logging.Filter):
    """Never let the bot token reach a log line (httpx logs full request URLs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = os.environ.get("TELEGRAM_BOT_TOKEN") or TOKEN
        if token:
            record.msg = str(record.msg).replace(token, "<telegram-token>")
            if isinstance(record.args, tuple):
                record.args = tuple(
                    str(arg).replace(token, "<telegram-token>") for arg in record.args
                )
        return True


def has_groq() -> bool:
    return bool((os.environ.get("GROQ_API_KEY") or "").strip())

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
MAX_MESSAGE = 3900
CACHE_TTL = int(os.environ.get("NPFL_BOT_CACHE_TTL", "60"))
LLM_COOLDOWN = float(os.environ.get("NPFL_LLM_COOLDOWN", "8"))
MAX_TOOL_ROUNDS = 4


def _allowed_ids() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    return {int(part) for part in re.split(r"[,\s]+", raw) if part.strip().isdigit()}


ALLOWED_USER_IDS = _allowed_ids()
ALLOW_ALL = (os.environ.get("TELEGRAM_ALLOW_ALL") or "").strip() == "1"


# --- Small helpers --------------------------------------------------------------

def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def matches_team(query: str, name: str) -> bool:
    """Loose team matching: substring either way, or all query tokens present."""
    q, n = norm(query), norm(name)
    if not q or not n:
        return False
    if q in n or n in q:
        return True
    tokens = [t for t in q.split() if len(t) > 2]
    return bool(tokens) and all(t in n for t in tokens)


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def ordinal(number: Any) -> str:
    n = to_int(number, 0)
    if not n:
        return "–"
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def pretty_kickoff(match: dict) -> str:
    local = match.get("kickoff_lagos") or ""
    if not local:
        return "kickoff TBC"
    try:
        moment = datetime.strptime(local, "%Y-%m-%d %H:%M")
    except ValueError:
        return local
    return moment.strftime("%d %b %Y, %H:%M") + " WAT"


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n"):
        block = block[:limit]
        if len(current) + len(block) + 1 > limit:
            chunks.append(current.rstrip())
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        chunks.append(current.rstrip())
    return chunks


class SheetCache:
    """Short-lived tab cache so one conversation does not re-read Sheets per message."""

    def __init__(self, ttl: int = CACHE_TTL) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def get(self, tab: str) -> list[dict[str, str]]:
        cached = self._data.get(tab)
        if cached and time.time() - cached[0] < self.ttl:
            return cached[1]
        records = sheets.read_records(tab)
        self._data[tab] = (time.time(), records)
        return records

    def invalidate(self) -> None:
        self._data.clear()


CACHE = SheetCache()


# --- Domain lookups -------------------------------------------------------------

def all_matches() -> list[dict[str, str]]:
    return CACHE.get("NPFL_Matches")


def match_by_id(match_id: str) -> dict[str, str] | None:
    return next((m for m in all_matches() if str(m.get("match_id")) == str(match_id)), None)


def team_stats(team_id: str = "", team_name: str = "") -> dict[str, str] | None:
    rows = CACHE.get("NPFL_Team_Stats")
    if team_id:
        for row in rows:
            if str(row.get("team_id")) == str(team_id):
                return row
    if team_name:
        for row in rows:
            if matches_team(team_name, row.get("team", "")):
                return row
    return None


def events_for(match_id: str) -> list[dict[str, str]]:
    rows = [e for e in CACHE.get("NPFL_Events") if str(e.get("match_id")) == str(match_id)]
    return sorted(rows, key=lambda e: (to_int(e.get("minute"), 999), e.get("minute_label") or ""))


def clips_for(match_id: str) -> list[dict[str, str]]:
    return [c for c in CACHE.get("Video_Tactical_Clips") if str(c.get("match_id")) == str(match_id)]


def head_to_head(home_id: str, away_id: str, limit: int = 3) -> list[dict[str, str]]:
    pairs = {frozenset((str(home_id), str(away_id)))}
    played = [
        m for m in all_matches()
        if str(m.get("status")) == "finished"
        and frozenset((str(m.get("home_team_id")), str(m.get("away_team_id")))) in pairs
    ]
    played.sort(key=lambda m: m.get("kickoff_utc") or "", reverse=True)
    return played[:limit]


def resolve_fixture(text: str) -> dict[str, str] | None:
    """'Team A vs Team B' or a bare match_id -> the most relevant stored fixture."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4,}", text):
        return match_by_id(text)

    parts = re.split(r"\s+(?:vs\.?|v\.?|versus|against)\s+|\s+-\s+", text, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None
    home_query, away_query = parts[0].strip(), parts[1].strip()

    direct = [
        m for m in all_matches()
        if matches_team(home_query, m.get("home_team", "")) and matches_team(away_query, m.get("away_team", ""))
    ]
    # Pre-match scouting: a fixture between the same two clubs counts even when the
    # hosts are reversed, so "A vs B" finds the upcoming B vs A as well.
    reversed_fixture = [
        m for m in all_matches()
        if matches_team(away_query, m.get("home_team", "")) and matches_team(home_query, m.get("away_team", ""))
    ]

    candidates: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for match in direct + reversed_fixture:
        match_id = str(match.get("match_id"))
        if match_id not in seen_ids:
            seen_ids.add(match_id)
            candidates.append(match)
    if not candidates:
        return None

    upcoming = [m for m in candidates if str(m.get("status")) != "finished"]
    if upcoming:
        return min(upcoming, key=lambda m: m.get("kickoff_utc") or "9999")
    return max(candidates, key=lambda m: m.get("kickoff_utc") or "")


def goal_timing(team_name: str, limit: int = 5) -> tuple[list[int], int, int]:
    """Goals scored by 15-minute window over the team's most recent stored matches."""
    bucket_counts = [0] * 6
    matches = [
        m for m in all_matches()
        if str(m.get("status")) == "finished"
        and team_name and team_name in (m.get("home_team"), m.get("away_team"))
    ]
    matches.sort(key=lambda m: m.get("kickoff_utc") or "", reverse=True)
    sampled = matches[:limit]
    if not sampled:
        return bucket_counts, 0, 0

    by_id = {str(m.get("match_id")): m for m in sampled}
    events = [e for e in CACHE.get("NPFL_Events") if str(e.get("match_id")) in by_id]
    total = 0
    for event in events:
        if str(event.get("event_type")).lower() != "goal":
            continue
        match = by_id.get(str(event.get("match_id")), {})
        side = str(event.get("team", "")).lower()
        scorer = match.get("home_team") if side == "home" else match.get("away_team") if side == "away" else ""
        if scorer != team_name:
            continue
        minute = to_int(event.get("minute"), -1)
        if minute < 0:
            continue
        bucket_counts[min(minute // 15, 5)] += 1
        total += 1
    return bucket_counts, len(sampled), total


# --- Rendering ------------------------------------------------------------------

def render_stats_block(row: dict[str, str] | None, label: str) -> tuple[str, str]:
    """(html, plain) for one team's stored season snapshot."""
    if not row:
        return (
            f"<b>{esc(label)}</b>\n<i>No stored stats yet — run scrape_npfl.py.</i>",
            f"{label}: no stored stats yet.",
        )

    name = row.get("team") or label
    header = f"{ordinal(row.get('position'))} · {row.get('points', '?')} pts · {row.get('played', '?')} played"
    record = f"{row.get('wins', '?')}W {row.get('draws', '?')}D {row.get('losses', '?')}L"
    form = row.get("form") or "—"
    goals = (
        f"{row.get('goals_for', '?')} GF / {row.get('goals_against', '?')} GA"
        f" ({row.get('avg_goals_for', '?')} / {row.get('avg_goals_against', '?')} per game)"
    )

    html_bits = [
        f"<b>{esc(name)}</b> — {esc(header)}",
        f"Record: {esc(record)} · Form: <code>{esc(form)}</code> · {esc(goals)}",
    ]
    plain_bits = [f"{name} — {header}", f"Record: {record} | Form: {form} | {goals}"]

    if to_int(row.get("matches_sampled"), 0) > 0:
        sampled = (
            f"xG {row.get('xg_for', '?')} for / {row.get('xg_against', '?')} against · "
            f"possession {row.get('avg_possession', '?')}% · shots {row.get('avg_shots', '?')} "
            f"({row.get('avg_shots_on_target', '?')} on target) — {row.get('matches_sampled')} sampled"
        )
        html_bits.append(f"<i>{esc(sampled)}</i>")
        plain_bits.append(sampled)

    return "\n".join(html_bits), "\n".join(plain_bits)


def render_timeline(match: dict[str, str], events: list[dict[str, str]]) -> tuple[str, str]:
    rows = []
    home, away = match.get("home_team", "Home"), match.get("away_team", "Away")
    for event in events:
        side = str(event.get("team", "")).lower()
        team = home if side == "home" else away if side == "away" else side or "?"
        minute = event.get("minute_label") or event.get("minute") or "?"
        detail = event.get("detail") or event.get("event_type") or ""
        player = event.get("player") or ""
        score = ""
        if event.get("home_score") not in ("", None) and event.get("away_score") not in ("", None):
            score = f" ({event.get('home_score')}-{event.get('away_score')})"
        rows.append(f"{minute}' {event.get('event_type', '').title()} — {team}: {player}{score}"
                    + (f" [{detail}]" if detail and detail != event.get("event_type") else ""))
    if not rows:
        return (
            "<i>No stored incidents for this match (increase --detail-limit on the scraper).</i>",
            "No stored incidents for this match.",
        )
    return "\n".join(esc(r) for r in rows), "\n".join(rows)


def render_clips(match: dict[str, str], clips: list[dict[str, str]]) -> tuple[str, str]:
    if not clips:
        return (
            "<i>No clips stored for this match yet. Queue one with "
            "<code>ingest_youtube.py --add &lt;url&gt; --match-id "
            f"{esc(match.get('match_id', ''))}</code>.</i>",
            "No clips stored for this match yet.",
        )
    ordered = sorted(clips, key=lambda c: to_int(c.get("timestamp_seconds"), 0))
    html_bits, plain_bits = [], []
    for clip in ordered[:8]:
        label = clip.get("timestamp_label") or "00:00"
        event_type = (clip.get("event_type") or "clip").replace("_", " ")
        note = clip.get("tactical_note") or ""
        url = clip.get("clip_url") or ""
        html_bits.append(f'<a href="{esc(url)}">{esc(label)} {esc(event_type)}</a> — {esc(note)}')
        plain_bits.append(f"{label} {event_type} — {note} {url}")
    return "\n".join(html_bits), "\n".join(plain_bits)


def build_preview(match: dict[str, str]) -> tuple[str, str]:
    home, away = match.get("home_team", "Home"), match.get("away_team", "Away")
    status = str(match.get("status") or "").lower()
    has_score = match.get("home_score") not in ("", None) and match.get("away_score") not in ("", None)

    headline = f"{home} vs {away}"
    meta = [match.get("tournament") or "NPFL"]
    if match.get("matchday"):
        meta.append(f"Matchday {match.get('matchday')}")
    meta.append(pretty_kickoff(match))
    meta_line = " · ".join(str(m) for m in meta if m)

    status_line = f"Result: {home} {match.get('home_score')}-{match.get('away_score')} {away}" if (
        status == "finished" and has_score
    ) else ("Kick-off pending" if status in ("notstarted", "") else f"Status: {status}")

    html_parts = [
        f"<b>{esc(headline)}</b>",
        esc(meta_line),
        esc(status_line),
    ]
    plain_parts = [headline, meta_line, status_line]
    if match.get("venue"):
        html_parts.append(f"Venue: {esc(match.get('venue'))}")
        plain_parts.append(f"Venue: {match.get('venue')}")

    for team_name, team_id in ((home, match.get("home_team_id", "")), (away, match.get("away_team_id", ""))):
        stats_row = team_stats(team_id=team_id, team_name=team_name)
        team_html, team_plain = render_stats_block(stats_row, team_name)
        html_parts.append("\n" + team_html)
        plain_parts.append("\n" + team_plain)

        buckets, sampled, total = goal_timing(team_name)
        if total:
            windows = " · ".join(
                f"{i * 15}-{i * 15 + 15}: {count}" for i, count in enumerate(buckets)
            )
            plural = "match" if sampled == 1 else "matches"
            line = f"Goals scored by window (last {sampled} stored {plural}): {windows}"
            html_parts.append(f"<i>{esc(line)}</i>")
            plain_parts.append(line)

    h2h = head_to_head(match.get("home_team_id", ""), match.get("away_team_id", ""))
    if h2h:
        html_parts.append("\n<b>Head to head</b>")
        plain_parts.append("\nHead to head")
        for previous in h2h:
            date = (previous.get("kickoff_lagos") or "")[:10]
            line = (
                f"{date}: {previous.get('home_team')} {previous.get('home_score')}-"
                f"{previous.get('away_score')} {previous.get('away_team')}"
            )
            html_parts.append(esc(line))
            plain_parts.append(line)

    clip_match = match
    clips = clips_for(match.get("match_id", ""))
    if not clips and h2h:
        clip_match = h2h[0]
        clips = clips_for(clip_match.get("match_id", ""))
    html_parts.append("\n<b>Clips</b>")
    plain_parts.append("\nClips")
    clip_html, clip_plain = render_clips(clip_match, clips)
    html_parts.append(clip_html)
    plain_parts.append(clip_plain)

    return "\n".join(html_parts), "\n".join(plain_parts)


# --- Groq -----------------------------------------------------------------------

def groq(messages: list[dict], *, tools: list[dict] | None = None, tool_choice: str | None = None,
         json_mode: bool = False, temperature: float = 0.2, max_tokens: int = 900) -> dict:
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")

    payload: dict[str, Any] = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    elif tool_choice:
        payload["tool_choice"] = tool_choice
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(4):
        response = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            wait = int(retry_after) if retry_after.isdigit() else 5 * (attempt + 1)
            LOG.warning("Groq rate limited, sleeping %ss", wait)
            time.sleep(min(wait, 45))
            continue
        if response.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Groq error {response.status_code}: {response.text[:200]}")
    raise RuntimeError("Groq unavailable after retries")


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_matches",
            "description": "Find stored NPFL fixtures/results for a team name. Returns match ids, teams, status, score, kickoff.",
            "parameters": {
                "type": "object",
                "properties": {"team": {"type": "string", "description": "team name, partial is fine"}},
                "required": ["team"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_match",
            "description": "Fetch one stored fixture by match id.",
            "parameters": {
                "type": "object",
                "properties": {"match_id": {"type": "string"}},
                "required": ["match_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_team_stats",
            "description": "Season table row and form for a team: position, points, W/D/L, goals, xG, possession, shots.",
            "parameters": {
                "type": "object",
                "properties": {"team": {"type": "string"}},
                "required": ["team"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_match_events",
            "description": "Stored goals/cards/substitutions with minutes for a match (timeline).",
            "parameters": {
                "type": "object",
                "properties": {"match_id": {"type": "string"}},
                "required": ["match_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lineups",
            "description": "Stored formations and starters (with average positions) for a match.",
            "parameters": {
                "type": "object",
                "properties": {"match_id": {"type": "string"}},
                "required": ["match_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clips",
            "description": "Stored timestamped YouTube tactical clips for a match, with exact second links.",
            "parameters": {
                "type": "object",
                "properties": {"match_id": {"type": "string"}},
                "required": ["match_id"],
            },
        },
    },
]

TOOL_FIELDS = {
    "match": ["match_id", "status", "home_team", "away_team", "home_score", "away_score",
              "kickoff_lagos", "venue", "matchday", "tournament"],
    "stats": ["team", "position", "played", "wins", "draws", "losses", "points", "form",
              "goals_for", "goals_against", "avg_goals_for", "avg_goals_against", "xg_for",
              "xg_against", "avg_possession", "avg_shots", "avg_shots_on_target", "matches_sampled"],
    "event": ["minute_label", "event_type", "detail", "team", "player", "home_score", "away_score"],
    "clip": ["timestamp_label", "event_type", "team", "player", "tactical_note", "clip_url"],
    "lineup": ["team", "player", "position", "shirt_number", "is_starter", "formation", "avg_x", "avg_y"],
}


def _pick(record: dict[str, str], fields: list[str]) -> dict[str, str]:
    return {field: record.get(field, "") for field in fields}


def run_tool(name: str, arguments: dict) -> dict:
    """Execute one grounding tool against the Sheet. Never raises."""
    try:
        if name == "search_matches":
            team = str(arguments.get("team", ""))
            hits = [m for m in all_matches()
                    if matches_team(team, m.get("home_team", "")) or matches_team(team, m.get("away_team", ""))]
            hits.sort(key=lambda m: m.get("kickoff_utc") or "", reverse=True)
            return {"matches": [_pick(m, TOOL_FIELDS["match"]) for m in hits[:8]]}

        if name == "get_match":
            match = match_by_id(str(arguments.get("match_id", "")))
            return {"match": _pick(match, TOOL_FIELDS["match"]) if match else None}

        if name == "get_team_stats":
            row = team_stats(team_name=str(arguments.get("team", "")))
            return {"stats": _pick(row, TOOL_FIELDS["stats"]) if row else None}

        if name == "get_match_events":
            rows = events_for(str(arguments.get("match_id", "")))
            return {"events": [_pick(e, TOOL_FIELDS["event"]) for e in rows]}

        if name == "get_lineups":
            match_id = str(arguments.get("match_id", ""))
            rows = [r for r in CACHE.get("NPFL_Lineups") if str(r.get("match_id")) == match_id]
            return {"lineups": [_pick(r, TOOL_FIELDS["lineup"]) for r in rows][:40]}

        if name == "get_clips":
            match_id = str(arguments.get("match_id", ""))
            rows = clips_for(match_id)
            return {"clips": [_pick(c, TOOL_FIELDS["clip"]) for c in rows][:15]}

        return {"error": f"unknown tool {name}"}
    except Exception as exc:  # a tool failure must not break the reply
        LOG.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


SYSTEM_PROMPT = """You are the NPFL pre-match scouting assistant for Nigerian coaches, players and analysts.
Today is {today}. You cover the Nigeria Premier Football League and the national teams.

Absolute rules:
1. Every score, table position, form string, xG, possession figure, minute, player and clip link you state MUST come from a tool result in THIS conversation. If no tool returns it, say plainly that the data is not in our database. Never guess, estimate or fill gaps from your own football knowledge.
2. Interpretation is allowed (e.g. "a back three that pushes high leaves space behind"), but label it as analysis and keep it separate from the stored facts.
3. Clip links must be copied exactly as returned, e.g. https://youtu.be/XXXX?t=754.
4. Be concise: at most 200 words, plain text, no markdown tables, no headings with #.
5. If the user asks about live scores, betting or predictions of an exact scoreline, explain those are out of scope.

Structure a scouting answer as: shape/build-up, pressing behaviour, set pieces, weaknesses to attack, then clips (if stored)."""


def run_agent(question: str) -> str:
    """Tool-calling loop over Groq; the model only sees Sheet-backed tool output."""
    if not (os.environ.get("GROQ_API_KEY") or "").strip():
        return (
            "The AI brain is not configured yet (GROQ_API_KEY missing).\n"
            "You can still use /preview, /form, /match and /clips — they read the Sheet directly."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))},
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        reply = groq(messages, tools=TOOLS)
        message = (reply.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)

        if not calls:
            answer = (message.get("content") or "").strip()
            return answer or "No answer produced — try /preview <team> vs <team>."

        for call in calls:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            result = run_tool(function.get("name", ""), arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": function.get("name", ""),
                "content": json.dumps(result, ensure_ascii=False)[:6000],
            })

    final = groq(messages, tool_choice="none")
    content = ((final.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return content.strip() or "No answer produced."


def coach_brief(facts: str) -> str:
    """Short coaching read of already-collected facts. Returns '' when unavailable."""
    if not (os.environ.get("GROQ_API_KEY") or "").strip() or not facts.strip():
        return ""
    messages = [
        {
            "role": "system",
            "content": (
                "You are an NPFL tactical analyst. Write a pre-match brief for a coach using ONLY the "
                "FACTS block. Never add a new number, player, match or statistic that is not in FACTS. "
                "If a section has no data, skip it rather than guessing. 4-6 bullets, under 120 words "
                "total, plain text, each bullet one concrete action or observation."
            ),
        },
        {"role": "user", "content": f"FACTS:\n{facts}"},
    ]
    try:
        reply = groq(messages, temperature=0.2, max_tokens=300)
    except Exception as exc:
        LOG.warning("coach brief failed: %s", exc)
        return ""
    return (((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


# --- Messaging helpers ----------------------------------------------------------

_last_llm_call: dict[int, float] = {}


def cooldown_remaining(user_id: int) -> float:
    last = _last_llm_call.get(user_id, 0.0)
    remaining = LLM_COOLDOWN - (time.time() - last)
    if remaining > 0:
        return remaining
    _last_llm_call[user_id] = time.time()
    return 0.0


async def reply(update: Update, text: str, *, html_mode: bool = True) -> None:
    message = update.effective_message
    if message is None:
        return
    for chunk in split_message(text):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML if html_mode else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


def restricted(handler):
    """Allowlist gate. Default deny unless TELEGRAM_ALLOW_ALL=1."""

    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id if user else 0
        if ALLOW_ALL or user_id in ALLOWED_USER_IDS:
            return await handler(update, context)

        LOG.warning("blocked user id=%s username=%s", user_id, getattr(user, "username", None))
        await reply(
            update,
            "You are not authorised for this bot.\n\n"
            f"Your Telegram user id is <code>{user_id}</code>.\n"
            "Ask the admin to add it to <code>TELEGRAM_ALLOWED_USER_IDS</code>.",
        )
        return None

    return wrapper


# --- Commands -------------------------------------------------------------------

@restricted
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>NPFL Pre-Match Scouting</b>\n"
        "Tactical dossiers, form and timestamped clips — pre-match only, no live tracking.\n\n"
        "<b>Commands</b>\n"
        "/preview &lt;team&gt; vs &lt;team&gt; — fixture dossier + coach brief\n"
        "/preview &lt;match_id&gt; — same, by id\n"
        "/form &lt;team&gt; — table snapshot, form, recent results\n"
        "/match &lt;match_id&gt; — fixture and incident timeline\n"
        "/clips &lt;match_id&gt; — timestamped YouTube tactical clips\n"
        "/teams — teams in the database\n"
        "/id — your Telegram user id\n"
        "/refresh — clear the local Sheet cache\n\n"
        "Or just ask a question, e.g. <i>how does Enyimba defend corners?</i>"
    )
    await reply(update, text)


@restricted
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


@restricted
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await reply(update, f"Your Telegram user id: <code>{user.id if user else 0}</code>")


@restricted
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    CACHE.invalidate()
    await reply(update, "Cache cleared — the next answer re-reads Google Sheets.")


@restricted
async def cmd_teams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = CACHE.get("NPFL_Team_Stats")
    names = sorted({r.get("team", "") for r in rows if r.get("team")})
    if not names:
        await reply(update, "No teams stored yet. Run <code>python scrape_npfl.py</code> first.")
        return
    await reply(update, "<b>Teams</b>\n" + esc(" · ".join(names)))


@restricted
async def cmd_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    argument = " ".join(context.args or []).strip()
    if not argument:
        await reply(update, "Usage: <code>/preview Enyimba vs Rangers International</code> or <code>/preview 12345678</code>")
        return

    match = await asyncio.to_thread(resolve_fixture, argument)
    if not match:
        await reply(
            update,
            f"No stored fixture for <b>{esc(argument)}</b>.\n"
            "Use <code>team vs team</code> or a match id. /teams lists what we have.",
        )
        return

    html_text, facts = await asyncio.to_thread(build_preview, match)

    brief = ""
    if has_groq() and not cooldown_remaining(update.effective_user.id):
        brief = await asyncio.to_thread(coach_brief, facts)

    text = html_text
    if brief:
        text += f"\n\n<b>🎯 Coach brief</b>\n{esc(brief)}"
    await reply(update, text)


@restricted
async def cmd_form(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    team = " ".join(context.args or []).strip()
    if not team:
        await reply(update, "Usage: <code>/form Enyimba</code>")
        return

    row = await asyncio.to_thread(team_stats, team_name=team)
    if not row:
        await reply(update, f"No stored stats for <b>{esc(team)}</b>. /teams lists what we have.")
        return

    html_block, _ = render_stats_block(row, team)
    matches = [
        m for m in await asyncio.to_thread(all_matches)
        if row.get("team") in (m.get("home_team"), m.get("away_team")) and str(m.get("status")) == "finished"
    ]
    matches.sort(key=lambda m: m.get("kickoff_utc") or "", reverse=True)

    lines = [html_block, "\n<b>Recent results</b>"]
    for match in matches[:5]:
        lines.append(esc(
            f"{(match.get('kickoff_lagos') or '')[:10]}: {match.get('home_team')} "
            f"{match.get('home_score')}-{match.get('away_score')} {match.get('away_team')}"
        ))
    if len(lines) == 2:
        lines.append("<i>No finished matches stored yet.</i>")
    await reply(update, "\n".join(lines))


@restricted
async def cmd_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    match_id = (context.args or [""])[0].strip()
    if not match_id:
        await reply(update, "Usage: <code>/match 12345678</code>")
        return

    match = await asyncio.to_thread(match_by_id, match_id)
    if not match:
        await reply(update, f"No stored match with id <code>{esc(match_id)}</code>.")
        return

    events = await asyncio.to_thread(events_for, match_id)
    html_text, _ = await asyncio.to_thread(build_preview, match)
    timeline_html, _ = render_timeline(match, events)
    await reply(update, f"{html_text}\n\n<b>Timeline</b>\n{timeline_html}")


@restricted
async def cmd_clips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    match_id = (context.args or [""])[0].strip()
    if not match_id:
        await reply(update, "Usage: <code>/clips 12345678</code>")
        return

    match = await asyncio.to_thread(match_by_id, match_id)
    clips = await asyncio.to_thread(clips_for, match_id)
    if not match:
        await reply(update, f"No stored match with id <code>{esc(match_id)}</code>.")
        return

    header = f"<b>{esc(match.get('home_team'))} {esc(match.get('home_score'))}-{esc(match.get('away_score'))} {esc(match.get('away_team'))}</b>"
    clip_html, _ = render_clips(match, clips)
    await reply(update, f"{header}\n\n{clip_html}")


@restricted
async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args or []).strip()
    if not question:
        await reply(update, "Usage: <code>/ask how does Enyimba build up?</code>")
        return
    await handle_question(update, question)


@restricted
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = (update.effective_message.text or "").strip()
    if question:
        await handle_question(update, question)


async def handle_question(update: Update, question: str) -> None:
    if len(question) > 600:
        await reply(update, "Please keep the question under 600 characters.")
        return

    if not has_groq():
        await reply(update, run_agent(question))
        return

    wait = cooldown_remaining(update.effective_user.id)
    if wait:
        await reply(update, f"One moment — try again in {wait:.0f}s (free-tier rate limit).")
        return

    try:
        answer = await asyncio.to_thread(run_agent, question)
    except Exception as exc:
        LOG.exception("agent failed")
        await reply(update, f"The AI brain failed: <code>{esc(type(exc).__name__)}</code>. Try /preview instead.")
        return

    await reply(update, answer, html_mode=False)


# --- Entry point ----------------------------------------------------------------

def build_application() -> Application:
    if not TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Add the bot token to your .env or project secrets."
        )
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("preview", cmd_preview))
    application.add_handler(CommandHandler("form", cmd_form))
    application.add_handler(CommandHandler("match", cmd_match))
    application.add_handler(CommandHandler("clips", cmd_clips))
    application.add_handler(CommandHandler("teams", cmd_teams))
    application.add_handler(CommandHandler("id", cmd_id))
    application.add_handler(CommandHandler("refresh", cmd_refresh))
    application.add_handler(CommandHandler("ask", cmd_ask))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def run_check() -> int:
    problems: list[str] = []

    print(f"[check] TELEGRAM_BOT_TOKEN: {'set' if TOKEN else 'MISSING'}")
    if TOKEN:
        try:
            response = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=15)
            data = response.json()
            if data.get("ok"):
                result = data.get("result") or {}
                print(f"[check] getMe ok: @{result.get('username')} (id={result.get('id')})")
            else:
                problems.append(f"getMe failed: {data.get('description')}")
        except Exception as exc:
            problems.append(f"getMe error: {str(exc).replace(TOKEN, '<token>')}")
    else:
        problems.append("TELEGRAM_BOT_TOKEN missing")

    print(f"[check] allowlist: {len(ALLOWED_USER_IDS)} user id(s){' (TELEGRAM_ALLOW_ALL=1)' if ALLOW_ALL else ''}")

    backend = sheets.storage_backend()
    try:
        matches = sheets.read_records("NPFL_Matches")
        location = sheets.local_store_path() if backend == "local" else "Google Sheets"
        print(f"[check] storage: {backend} ({location}) reachable: {len(matches)} row(s) in NPFL_Matches")
    except Exception as exc:
        problems.append(f"storage ({backend}): {exc}")

    groq_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    print(f"[check] GROQ_API_KEY: {'set' if groq_key else 'MISSING (chat answers disabled)'}")

    if problems:
        for problem in problems:
            print(f"[check] problem: {problem}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs, which include the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TokenRedaction())

    if "--local" in argv:
        os.environ["NPFL_STORAGE"] = "local"
        LOG.info("using local JSON store: %s", sheets.local_store_path())

    if "--check" in argv:
        return run_check()

    application = build_application()
    LOG.info("starting NPFL scouting bot (model=%s)", GROQ_MODEL)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
