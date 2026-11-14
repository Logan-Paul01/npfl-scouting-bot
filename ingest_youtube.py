"""Video intelligence (Method A): public YouTube transcript -> Groq tactical events -> timestamped clips.

$0 rules enforced here:
  * No video download, no frame processing. Text only.
  * `youtube-transcript-api` on a public transcript; if absent we fall back to
    description chapters, then to goal minutes already stored in NPFL_Events.
  * Groq tool-calling grounds the model in our Sheet. The model never invents stats.
  * Clip links point at the exact second: https://youtu.be/{video_id}?t={seconds}

Usage:
    python ingest_youtube.py --url https://youtu.be/XXXX --match-id 12345678
    python ingest_youtube.py --url ... --match-id ... --dry-run
    python ingest_youtube.py --add https://youtu.be/XXXX --match-id 12345678   # queue only
    python ingest_youtube.py --queue                                          # process queue
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

import sheets

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
DEFAULT_PREROLL = int(os.environ.get("NPFL_CLIP_PREROLL", "6"))
MAX_CHUNK_CHARS = int(os.environ.get("NPFL_CHUNK_CHARS", "2600"))
LANGUAGES = ("en", "en-NG", "en-GB", "en-US")

EVENT_TYPES = [
    "goal", "chance_created", "chance_conceded", "press_trigger", "high_line",
    "low_block", "build_up_pattern", "set_piece", "defensive_error", "transition",
    "substitution_impact", "tactical_shift", "momentum_swing", "individual_duel",
]


def log(message: str) -> None:
    print(f"[video] {message}", flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TranscriptUnavailable(RuntimeError):
    """No usable transcript, chapters or goal minutes for this video."""


# --- Video id / timestamp helpers ------------------------------------------------

def video_id_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower().replace("www.", "")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
    elif "youtube.com" in host:
        if parsed.path.startswith("/shorts/") or parsed.path.startswith("/embed/") or parsed.path.startswith("/live/"):
            candidate = parsed.path.strip("/").split("/")[1]
        else:
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
    else:
        candidate = url.strip()

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate or ""):
        raise ValueError(f"could not extract an 11-character YouTube video id from {url!r}")
    return candidate


def parse_timestamp(value: Any) -> int | None:
    """'MM:SS', 'H:MM:SS', '90' or 90 -> seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return max(0, int(float(text)))
    parts = text.split(":")
    if not all(p.strip().isdigit() for p in parts) or len(parts) not in (2, 3):
        return None
    numbers = [int(p) for p in parts]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]


def label(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


# --- Transcript ----------------------------------------------------------------

def fetch_transcript(video_id: str, languages: tuple[str, ...] = LANGUAGES) -> list[dict]:
    """Timestamped transcript snippets -> [{'text','start','duration'}]"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (NoTranscriptFound, TranscriptsDisabled,
                                                   VideoUnavailable)
    except ImportError as exc:
        raise TranscriptUnavailable(f"youtube-transcript-api not installed: {exc}") from exc

    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=list(languages))
    except AttributeError:
        # youtube-transcript-api < 1.0 legacy static API.
        try:
            fetched = YouTubeTranscriptApi.get_transcript(video_id, languages=list(languages))
        except Exception as exc:  # noqa: BLE001 - normalised below
            raise TranscriptUnavailable(str(exc)) from exc
    except (NoTranscriptFound, TranscriptsDisabled, VideoUnavailable) as exc:
        raise TranscriptUnavailable(f"{type(exc).__name__}: {exc}") from exc

    snippets = []
    for item in fetched:
        if isinstance(item, dict):
            text = item.get("text", "")
            start = item.get("start", 0)
            duration = item.get("duration", 0)
        else:
            text = getattr(item, "text", "")
            start = getattr(item, "start", 0)
            duration = getattr(item, "duration", 0)
        text = re.sub(r"\s+", " ", str(text)).strip()
        if text:
            snippets.append({"text": text, "start": float(start), "duration": float(duration)})

    if not snippets:
        raise TranscriptUnavailable("transcript was empty")
    return snippets


def fetch_description(video_id: str) -> str:
    """Public watch page description (no API key). Used only for chapter fallback."""
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            timeout=25,
        )
    except requests.RequestException as exc:
        raise TranscriptUnavailable(f"description fetch failed: {exc}") from exc

    if response.status_code != 200:
        raise TranscriptUnavailable(f"description fetch HTTP {response.status_code}")

    match = re.search(r'"shortDescription":"(.*?)","isCrawlable"', response.text, re.S)
    if not match:
        raise TranscriptUnavailable("no shortDescription in watch page")
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return match.group(1).replace("\\n", "\n")


def chapters_from_description(description: str) -> list[dict]:
    """' 12:34 - Build-up shape' lines -> pseudo transcript snippets."""
    snippets = []
    for line in description.splitlines():
        match = re.match(r"^\s*\(?(\d{1,2}:\d{2}(?::\d{2})?)\)?\s*[-–—:|]?\s*(.+?)\s*$", line)
        if not match:
            continue
        seconds = parse_timestamp(match.group(1))
        text = match.group(2).strip()
        if seconds is None or len(text) < 3:
            continue
        snippets.append({"text": text, "start": float(seconds), "duration": 0.0})
    return snippets


def goal_minute_segments(match_id: str) -> list[dict]:
    """Last resort: build segments from the match's stored incidents."""
    try:
        events = sheets.find_records("NPFL_Events", match_id=match_id)
    except Exception as exc:  # credentials / network: fall through
        raise TranscriptUnavailable(f"could not read NPFL_Events: {exc}") from exc

    snippets = []
    for event in events:
        if (event.get("event_type") or "").lower() not in ("goal", "card"):
            continue
        minute = parse_timestamp(event.get("minute_label") or event.get("minute"))
        if minute is None:
            continue
        text = (
            f"Minute {event.get('minute_label') or minute}: {event.get('event_type')} for "
            f"{event.get('team')} - {event.get('player')} ({event.get('detail') or 'regular'})"
        )
        snippets.append({"text": text, "start": float(minute * 60), "duration": 0.0})

    if not snippets:
        raise TranscriptUnavailable("no stored goal/card minutes for this match")
    return snippets


def load_evidence(video_id: str, match_id: str, languages: tuple[str, ...]) -> tuple[list[dict], str]:
    """Returns (segments, evidence_source) trying transcript -> chapters -> stored minutes."""
    try:
        return fetch_transcript(video_id, languages), "transcript"
    except TranscriptUnavailable as exc:
        log(f"no transcript ({exc}); trying description chapters")

    try:
        chapters = chapters_from_description(fetch_description(video_id))
        if chapters:
            return chapters, "description_chapters"
    except TranscriptUnavailable as exc:
        log(f"no description chapters ({exc})")

    log("falling back to stored goal/card minutes from NPFL_Events")
    return goal_minute_segments(match_id), "goal_minutes"


# --- Chunking -------------------------------------------------------------------

def build_chunks(segments: list[dict], max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Group timestamped segments into LLM-sized chunks, keeping [MM:SS] anchors."""
    lines = [f"[{label(int(seg['start']))}] {seg['text']}" for seg in segments]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


# --- Groq + tools ---------------------------------------------------------------

SYSTEM_PROMPT = f"""You are the tactical video analyst for an NPFL (Nigeria Premier League) pre-match scouting desk.
You receive a timestamped transcript excerpt from a public match video and extract tactical events a coach can use before facing these teams.

Hard rules:
- Only record events the transcript explicitly supports. Never invent a player, score, minute, statistic or event.
- Every anchor MUST be copied from a [MM:SS] marker that appears in the transcript.
- If evidence is thin, lower confidence below 0.4 or omit the event entirely.
- tactical_note is a concrete coaching observation (max 220 characters): what happened, why it matters, what to exploit.
- Allow event types: {", ".join(EVENT_TYPES)}.
- Match context comes from your tools (our Google Sheet). Use them when the transcript refers to a team, score or form you need to disambiguate.
- Reply with JSON only, no prose, no markdown fences.

JSON schema:
{{"events":[{{"anchor":"MM:SS","event_type":"goal","team":"home|away|<team name>|unknown","player":"","minute":0,"note":"...","keywords":["..."],"confidence":0.0}}]}}"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_match_context",
            "description": "Fetch the stored fixture row (teams, score, venue, kickoff) for a match id.",
            "parameters": {
                "type": "object",
                "properties": {"match_id": {"type": "string", "description": "NPFL_Matches match_id"}},
                "required": ["match_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_match_events",
            "description": "Fetch stored goals/cards/substitutions with minutes for a match. Use to confirm the match timeline.",
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
            "name": "get_team_form",
            "description": "Fetch stored season stats and recent form for a team name.",
            "parameters": {
                "type": "object",
                "properties": {"team": {"type": "string"}},
                "required": ["team"],
            },
        },
    },
]


def run_tool(name: str, arguments: dict) -> dict:
    """Execute a grounding tool locally against Google Sheets."""
    try:
        if name == "get_match_context":
            rows = sheets.find_records("NPFL_Matches", match_id=str(arguments.get("match_id", "")))
            return {"match": rows[0] if rows else None}
        if name == "get_match_events":
            rows = sheets.find_records("NPFL_Events", match_id=str(arguments.get("match_id", "")))
            keep = ("minute_label", "event_type", "detail", "team", "player", "home_score", "away_score")
            return {"events": [{k: row.get(k, "") for k in keep} for row in rows]}
        if name == "get_team_form":
            team = str(arguments.get("team", "")).strip().lower()
            rows = [r for r in sheets.read_records("NPFL_Team_Stats")
                    if team and team in (r.get("team") or "").lower()]
            return {"teams": rows[:3]}
    except Exception as exc:  # never let a tool failure break extraction
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": f"unknown tool {name}"}


def groq_chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> dict:
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
        payload["tool_choice"] = "auto"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_error = ""
    for attempt in range(4):
        response = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            wait = int(retry_after) if retry_after.isdigit() else 5 * (attempt + 1)
            last_error = "rate limited"
            log(f"Groq 429, sleeping {min(wait, 60)}s")
            time.sleep(min(wait, 60))
            continue
        if response.status_code >= 500:
            last_error = f"HTTP {response.status_code}"
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Groq error {response.status_code}: {response.text[:300]}")

    raise RuntimeError(f"Groq unavailable after retries ({last_error})")


def _json_from_content(content: str) -> dict:
    text = (content or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except ValueError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"model did not return JSON: {text[:200]}")


def extract_chunk(chunk: str, *, match_id: str, video_id: str, source: str) -> list[dict]:
    """One transcript chunk -> validated tactical events (Groq tool-calling + JSON pass)."""
    user_prompt = (
        f"Video: https://youtu.be/{video_id}\nMatch id: {match_id}\n"
        f"Evidence source: {source}\n\n"
        "Transcript chunk (anchors are the only valid timestamps):\n"
        f"{chunk}\n\n"
        "Extract the tactical events. Use your tools if you need the stored match context."
    )
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Tool-calling rounds: the model may ask for grounded context.
    for _ in range(3):
        reply = groq_chat(messages, tools=TOOLS)
        message = (reply.get("choices") or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        assistant_message: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        if calls:
            assistant_message["tool_calls"] = calls
        messages.append(assistant_message)
        if not calls:
            break
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

    # Forced JSON pass so the batch stays machine-readable.
    messages.append({
        "role": "user",
        "content": (
            "Return ONLY the JSON object described in the system prompt for THIS chunk. "
            "No markdown, no commentary. Empty list is acceptable if nothing tactical is evidenced."
        ),
    })
    reply = groq_chat(messages, json_mode=True, temperature=0.0)
    content = ((reply.get("choices") or [{}])[0].get("message") or {}).get("content") or "{}"
    return validate_events(_json_from_content(content), chunk=chunk, source=source)


def validate_events(payload: dict, *, chunk: str, source: str) -> list[dict]:
    """Drop anything unanchored or invented; cap confidence for weak evidence."""
    anchors = set(re.findall(r"\[(\d{1,3}:\d{2})\]", chunk))
    events = payload.get("events") if isinstance(payload, dict) else None
    cleaned: list[dict] = []

    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "").strip().lower()
        anchor = str(event.get("anchor") or "").strip().strip("[]")
        if event_type not in EVENT_TYPES or anchor not in anchors:
            continue
        try:
            confidence = float(event.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if source != "transcript":
            confidence = min(confidence, 0.5)
        keywords = event.get("keywords")
        if isinstance(keywords, list):
            keywords = ", ".join(str(k).strip() for k in keywords if str(k).strip())
        cleaned.append({
            "anchor": anchor,
            "event_type": event_type,
            "team": str(event.get("team") or "unknown")[:80],
            "player": str(event.get("player") or "")[:80],
            "minute": parse_timestamp(event.get("minute")) if parse_timestamp(event.get("minute")) is not None else "",
            "note": str(event.get("note") or "").strip()[:220],
            "keywords": str(keywords or "")[:200],
            "confidence": round(max(0.0, min(confidence, 1.0)), 2),
        })
    return cleaned


# --- Clip rows ------------------------------------------------------------------

def build_clip_rows(
    events: list[dict],
    *,
    match_id: str,
    video_id: str,
    video_url: str,
    source: str,
    preroll: int = DEFAULT_PREROLL,
) -> list[dict]:
    created_at = utc_now()
    rows: list[dict] = []
    seen: set[tuple] = set()

    for event in sorted(events, key=lambda e: parse_timestamp(e.get("anchor")) or 0):
        anchor_seconds = parse_timestamp(event.get("anchor"))
        if anchor_seconds is None:
            continue
        start = max(0, anchor_seconds - preroll)
        dedupe_key = (start // 4, event["event_type"], event.get("note", "")[:60])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        clip_url = f"https://youtu.be/{video_id}?t={start}"
        rows.append({
            "clip_id": hashlib.sha1(
                f"{match_id}|{video_id}|{start}|{event['event_type']}|{event.get('note', '')[:40]}".encode()
            ).hexdigest()[:16],
            "match_id": match_id,
            "video_id": video_id,
            "video_url": video_url,
            "timestamp_seconds": start,
            "timestamp_label": label(start),
            "clip_url": clip_url,
            "event_type": event["event_type"],
            "minute": event.get("minute", ""),
            "team": event.get("team", ""),
            "player": event.get("player", ""),
            "tactical_note": event.get("note", ""),
            "keywords": event.get("keywords", ""),
            "confidence": event.get("confidence", 0),
            "evidence_source": source,
            "created_at": created_at,
        })
    return rows


# --- Orchestration --------------------------------------------------------------

def process_video(
    url: str,
    match_id: str,
    *,
    dry_run: bool = False,
    max_chunks: int = 12,
    preroll: int = DEFAULT_PREROLL,
    languages: tuple[str, ...] = LANGUAGES,
) -> dict:
    video_id = video_id_from_url(url)
    log(f"video {video_id} for match {match_id}")

    segments, source = load_evidence(video_id, match_id, languages)
    log(f"evidence: {len(segments)} segments via {source}")

    chunks = build_chunks(segments)[:max_chunks]
    log(f"chunks to analyse: {len(chunks)}")

    if not (os.environ.get("GROQ_API_KEY") or "").strip():
        log("GROQ_API_KEY missing: showing the plan only, no extraction")
        for index, chunk in enumerate(chunks, start=1):
            log(f"--- chunk {index} ({len(chunk)} chars) ---\n{chunk[:600]}")
        return {"video_id": video_id, "clips": [], "planned_chunks": len(chunks), "dry_run": True}

    events: list[dict] = []
    for index, chunk in enumerate(chunks, start=1):
        try:
            found = extract_chunk(chunk, match_id=match_id, video_id=video_id, source=source)
        except Exception as exc:  # one bad chunk must not kill the batch
            log(f"chunk {index} failed: {exc}")
            continue
        log(f"chunk {index}: {len(found)} events")
        events.extend(found)
        time.sleep(1.0)  # stay friendly to the free tier

    rows = build_clip_rows(
        events,
        match_id=match_id,
        video_id=video_id,
        video_url=f"https://youtu.be/{video_id}",
        source=source,
        preroll=preroll,
    )
    log(f"clips prepared: {len(rows)}")

    if dry_run:
        for row in rows[:10]:
            log(f"  {row['timestamp_label']} {row['event_type']} [{row['team']}] {row['clip_url']} :: {row['tactical_note'][:120]}")
        log("dry-run: nothing written")
        return {"video_id": video_id, "clips": rows, "planned_chunks": len(chunks), "dry_run": True}

    spreadsheet = sheets.open_spreadsheet()
    tabs = sheets.ensure_all_tabs(spreadsheet)
    stats = sheets.upsert_rows(tabs["Video_Tactical_Clips"], rows, tab="Video_Tactical_Clips")
    log(f"Video_Tactical_Clips: created={stats['created']} updated={stats['updated']}")
    return {"video_id": video_id, "clips": rows, "planned_chunks": len(chunks), "dry_run": False,
            "sheet_stats": stats}


def enqueue(url: str, match_id: str) -> dict:
    """Add a URL to Video_Queue so the daily workflow picks it up."""
    video_id = video_id_from_url(url)
    spreadsheet = sheets.open_spreadsheet()
    tabs = sheets.ensure_all_tabs(spreadsheet)
    row = {
        "video_url": url.strip(),
        "match_id": match_id,
        "status": "pending",
        "queued_at": utc_now(),
        "processed_at": "",
        "video_id": video_id,
        "notes": "",
    }
    stats = sheets.upsert_rows(tabs["Video_Queue"], [row], tab="Video_Queue")
    log(f"queued {video_id} for match {match_id}: {stats}")
    return row


def process_queue(*, dry_run: bool = False, max_chunks: int = 12) -> int:
    pending = [
        row for row in sheets.read_records("Video_Queue")
        if (row.get("status") or "").strip().lower() in ("", "pending", "queued", "new", "retry")
    ]
    if not pending:
        log("queue is empty")
        return 0

    log(f"processing {len(pending)} queued video(s)")
    spreadsheet = None if dry_run else sheets.open_spreadsheet()
    tabs = None if dry_run else sheets.ensure_all_tabs(spreadsheet)

    failures = 0
    for row in pending:
        url, match_id = row.get("video_url", ""), row.get("match_id", "")
        status, notes = "done", ""
        try:
            result = process_video(url, match_id, dry_run=dry_run, max_chunks=max_chunks)
            notes = f"{len(result['clips'])} clips, evidence={result.get('clips') and result['clips'][0]['evidence_source'] or 'n/a'}"
        except Exception as exc:
            failures += 1
            status, notes = "failed", f"{type(exc).__name__}: {exc}"[:300]
            log(f"failed {url}: {exc}")

        if not dry_run:
            sheets.upsert_rows(
                tabs["Video_Queue"],
                [{**row, "status": status, "processed_at": utc_now(), "notes": notes}],
                tab="Video_Queue",
            )
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract tactical clips from public YouTube transcripts into Google Sheets.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="YouTube URL to analyse now")
    group.add_argument("--add", help="YouTube URL to queue for the daily workflow")
    group.add_argument("--queue", action="store_true", help="process every pending Video_Queue row")
    parser.add_argument("--match-id", default="", help="match_id from NPFL_Matches")
    parser.add_argument("--dry-run", action="store_true", help="analyse and print, never write")
    parser.add_argument("--max-chunks", type=int, default=12, help="cap chunks per video (free-tier safety, default 12)")
    parser.add_argument("--preroll", type=int, default=DEFAULT_PREROLL, help="seconds before the evidence to start the clip")
    parser.add_argument("--languages", default=",".join(LANGUAGES), help="comma-separated transcript languages")
    parser.add_argument("--local", action="store_true", help="use the local JSON store instead of Google Sheets")
    args = parser.parse_args(argv)

    if args.local:
        os.environ["NPFL_STORAGE"] = "local"
        log(f"local store: {sheets.local_store_path()}")

    if args.url or args.add:
        if not args.match_id:
            parser.error("--match-id is required with --url/--add")

    try:
        if args.add:
            enqueue(args.add, args.match_id)
            return 0

        languages = tuple(lang.strip() for lang in args.languages.split(",") if lang.strip())
        if args.queue:
            failures = process_queue(dry_run=args.dry_run, max_chunks=args.max_chunks)
            return 1 if failures else 0

        process_video(
            args.url,
            args.match_id,
            dry_run=args.dry_run,
            max_chunks=args.max_chunks,
            preroll=args.preroll,
            languages=languages,
        )
        return 0
    except TranscriptUnavailable as exc:
        log(f"no evidence available: {exc}")
        return 2
    except Exception as exc:
        log(f"error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
