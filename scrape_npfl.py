"""NPFL pre-match data ingestion: Sofascore (primary) -> FootyStats/Flashscore fallback -> SQLite (`scouting.db`).

$0 constraints honoured here:
  * HTTP only, no browser automation, no video, no ML.
  * Disk cache + retry/backoff + a hard per-run request budget.
  * Missing free data stays blank instead of being estimated.

Usage:
    python scrape_npfl.py --demo                 # offline parser smoke test (no network, no writes)
    python scrape_npfl.py --dry-run              # fetch + normalise, print a summary, no writes
    python scrape_npfl.py                        # full ingest into scouting.db
    python scrape_npfl.py --detail-limit 8 --last-limit 30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

import db

LAGOS = timezone(timedelta(hours=1))
SOFASCORE_BASE = os.environ.get("SOFASCORE_BASE", "https://api.sofascore.com/api/v1")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
REQUEST_BUDGET = int(os.environ.get("NPFL_REQUEST_BUDGET", "400"))
_request_count = 0


def log(message: str) -> None:
    print(f"[scrape] {message}", flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SourceError(RuntimeError):
    """Raised when a data source cannot serve the requested payload."""


_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.sofascore.com/",
            "Origin": "https://www.sofascore.com",
        })
    return _session


# --- HTTP with cache + budget ----------------------------------------------------

def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.md5(url.encode("utf-8")).hexdigest() + ".json")


def _cache_read(url: str, ttl: int) -> Any | None:
    path = _cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            blob = json.load(fh)
        if time.time() - float(blob.get("fetched_at", 0)) > ttl:
            return None
        return blob.get("payload")
    except (OSError, ValueError):
        return None


def _cache_write(url: str, payload: Any) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(url), "w", encoding="utf-8") as fh:
            json.dump({"fetched_at": time.time(), "payload": payload}, fh)
    except OSError:
        pass


def get_json(
    url: str,
    *,
    use_cache: bool = True,
    ttl: int | None = None,
    retries: int = 3,
    timeout: int = 25,
) -> Any:
    """GET a JSON payload with disk caching, retry/backoff and a request budget."""
    global _request_count
    ttl = int(os.environ.get("NPFL_CACHE_TTL", "1800")) if ttl is None else ttl

    if use_cache:
        cached = _cache_read(url, ttl)
        if cached is not None:
            return cached

    if _request_count >= REQUEST_BUDGET:
        raise SourceError(f"request budget of {REQUEST_BUDGET} exhausted (raise NPFL_REQUEST_BUDGET)")
    _request_count += 1

    last_error = "unknown error"
    for attempt in range(retries):
        try:
            response = session().get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(1.5 * (attempt + 1))
            continue

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                raise SourceError(f"non-JSON response from {url}")
            if use_cache:
                _cache_write(url, payload)
            return payload

        if response.status_code == 404:
            raise SourceError(f"404 for {url}")

        if response.status_code in (403, 429, 500, 502, 503, 504):
            retry_after = response.headers.get("retry-after")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * (attempt + 1)
            last_error = f"HTTP {response.status_code}"
            log(f"{last_error} on {url} (attempt {attempt + 1}/{retries}), sleeping {wait:.1f}s")
            time.sleep(min(wait, 30))
            continue

        raise SourceError(f"HTTP {response.status_code} for {url}")

    raise SourceError(f"failed after {retries} attempts: {last_error} ({url})")


# --- Normalisers (shared by every source) ----------------------------------------

def _iso(ts: Any) -> tuple[str, str]:
    """Epoch seconds -> (UTC iso, Lagos local 'YYYY-MM-DD HH:MM')."""
    if not ts:
        return "", ""
    try:
        moment = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "", ""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ"), moment.astimezone(LAGOS).strftime("%Y-%m-%d %H:%M")


def _score(block: Any) -> Any:
    if isinstance(block, dict):
        for key in ("current", "display"):
            if block.get(key) is not None:
                return block[key]
        return ""
    if block is None:
        return ""
    return block


def _short_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _team_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _epoch(iso: Any) -> int | None:
    text = str(iso or "").strip().replace("Z", "")
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def _match_url(match_id: str, source: str) -> str:
    if not match_id:
        return ""
    if source.startswith("sofascore"):
        return f"https://www.sofascore.com/event/{match_id}"
    if source.startswith("thesportsdb"):
        return f"https://www.thesportsdb.com/event/{match_id}"
    if source.startswith("flashscore"):
        return f"https://www.flashscore.com/match/{match_id}"
    return ""


def normalize_match(event: dict, *, season: str, source: str, now: str | None = None) -> dict:
    now = now or utc_now()
    home = event.get("homeTeam") or {}
    away = event.get("awayTeam") or {}
    status = event.get("status") or {}
    kickoff_utc, kickoff_local = _iso(event.get("startTimestamp"))
    match_id = str(event.get("id") or "")
    venue = ((event.get("venue") or {}).get("stadium") or {}).get("name") or ""
    tournament = (event.get("tournament") or {}).get("name") or "NPFL"

    return {
        "match_id": match_id,
        "source": source,
        "season": season,
        "matchday": (event.get("roundInfo") or {}).get("round") or "",
        "kickoff_utc": kickoff_utc,
        "kickoff_lagos": kickoff_local,
        "status": status.get("type") or status.get("description") or "",
        "home_team": home.get("name") or home.get("shortName") or "",
        "home_team_id": str(home.get("id") or ""),
        "away_team": away.get("name") or away.get("shortName") or "",
        "away_team_id": str(away.get("id") or ""),
        "home_score": _score(event.get("homeScore")),
        "away_score": _score(event.get("awayScore")),
        "venue": venue,
        "tournament": tournament,
        "match_url": _match_url(match_id, source),
        "updated_at": now,
    }


def normalize_incidents(match_id: str, payload: dict, *, source: str = "sofascore",
                        shotmap: dict | None = None, now: str | None = None) -> list[dict]:
    """Goals, cards and substitutions -> events rows (with shot coords when exposed)."""
    now = now or utc_now()
    incidents = (payload or {}).get("incidents") or []

    # Index shot coordinates so goal rows can carry x/y and xG without extra requests.
    shots: dict[tuple, dict] = {}
    for shot in ((shotmap or {}).get("shotmap") or []):
        key = (shot.get("time"), bool(shot.get("isHome")))
        shots.setdefault(key, shot)

    rows: list[dict] = []
    for incident in incidents:
        kind = incident.get("incidentType")
        if kind not in ("goal", "card", "substitution"):
            continue

        minute = incident.get("time")
        added = incident.get("addedTime")
        minute_label = f"{minute}+{added}" if added else str(minute or "")
        side_home = bool(incident.get("isHome"))
        player = (incident.get("player") or {}).get("name") or ""
        player_id = (incident.get("player") or {}).get("id") or ""

        detail = incident.get("incidentClass") or ""
        if kind == "card":
            detail = incident.get("incidentClass") or "yellow"
        if kind == "substitution":
            detail = "substitution"

        shot = shots.get((minute, side_home), {})
        coords = shot.get("playerCoordinates") or {}

        rows.append({
            "match_id": match_id,
            "event_key": _short_hash(match_id, kind, minute, added or "", player_id or player, detail),
            "minute": minute or "",
            "minute_label": minute_label,
            "event_type": kind,
            "detail": detail,
            "team": "home" if side_home else "away",
            "player": player,
            "assist": (incident.get("assist1") or {}).get("name") or "",
            "home_score": incident.get("homeScore", ""),
            "away_score": incident.get("awayScore", ""),
            "x": coords.get("x", "") if kind == "goal" else "",
            "y": coords.get("y", "") if kind == "goal" else "",
            "shot_type": shot.get("shotType", "") if kind == "goal" else "",
            "body_part": (shot.get("bodyPart") or incident.get("bodyPart") or "") if kind == "goal" else "",
            "situation": (shot.get("situation") or incident.get("situation") or "") if kind == "goal" else "",
            "source": source,
            "updated_at": now,
        })
    return rows


def normalize_lineups(match_id: str, payload: dict, *, source: str = "sofascore",
                      now: str | None = None) -> list[dict]:
    now = now or utc_now()
    rows: list[dict] = []
    for side in ("home", "away"):
        block = (payload or {}).get(side) or {}
        team = block.get("team") or {}
        team_id = str(team.get("id") or "")
        team_name = team.get("name") or ""
        formation = block.get("formation") or ""
        for entry in block.get("players") or []:
            player = entry.get("player") or {}
            stats = entry.get("statistics") or {}
            average = entry.get("averagePosition") or {}
            rows.append({
                "match_id": match_id,
                "team_id": team_id,
                "team": team_name,
                "player_id": str(player.get("id") or ""),
                "player": player.get("name") or "",
                "shirt_number": player.get("jerseyNumber", ""),
                "position": entry.get("position") or player.get("position") or "",
                "is_starter": not bool(entry.get("substitute")),
                "formation": formation,
                "avg_x": average.get("x", ""),
                "avg_y": average.get("y", ""),
                "rating": stats.get("rating", ""),
                "source": source,
                "updated_at": now,
            })
    return rows


def extract_possession(statistics: dict) -> dict[str, float]:
    """Pull 'Ball possession' for the ALL period -> {'home': 54.0, 'away': 46.0}."""
    for period in (statistics or {}).get("statistics") or []:
        if str(period.get("period", "")).upper() not in ("ALL", "MATCH"):
            continue
        for group in period.get("groups") or []:
            for item in group.get("statisticsItems") or []:
                if "possession" in (item.get("name") or "").lower():
                    def num(value: Any) -> float:
                        try:
                            return float(str(value).replace("%", "").strip())
                        except (TypeError, ValueError):
                            return 0.0
                    return {"home": num(item.get("home")), "away": num(item.get("away"))}
    return {}


ON_TARGET = {"goal", "save"}


def aggregate_shots(shotmap: dict) -> dict[str, dict[str, float]]:
    """Shot totals, shots on target and xG per side from a Sofascore shotmap."""
    totals = {"home": {"shots": 0, "on_target": 0, "xg": 0.0},
              "away": {"shots": 0, "on_target": 0, "xg": 0.0}}
    for shot in (shotmap or {}).get("shotmap") or []:
        side = "home" if shot.get("isHome") else "away"
        totals[side]["shots"] += 1
        if str(shot.get("shotType", "")).lower() in ON_TARGET:
            totals[side]["on_target"] += 1
        try:
            totals[side]["xg"] += float(shot.get("xg") or 0)
        except (TypeError, ValueError):
            pass
    return totals


def form_string(matches: list[dict], team_id: str, limit: int = 5) -> str:
    played = [
        m for m in matches
        if m.get("status") == "finished" and team_id in (m.get("home_team_id"), m.get("away_team_id"))
    ]
    played.sort(key=lambda m: m.get("kickoff_utc") or "")
    letters = []
    for match in played[-limit:]:
        try:
            home_score = int(match.get("home_score") or 0)
            away_score = int(match.get("away_score") or 0)
        except (TypeError, ValueError):
            continue
        is_home = match.get("home_team_id") == team_id
        ours, theirs = (home_score, away_score) if is_home else (away_score, home_score)
        letters.append("W" if ours > theirs else ("D" if ours == theirs else "L"))
    return "".join(letters)


def clean_sheets(matches: list[dict], team_id: str) -> Any:
    count = 0
    for match in matches:
        if match.get("status") != "finished" or team_id not in (match.get("home_team_id"), match.get("away_team_id")):
            continue
        try:
            home_score = int(match.get("home_score") or 0)
            away_score = int(match.get("away_score") or 0)
        except (TypeError, ValueError):
            continue
        conceded = away_score if match.get("home_team_id") == team_id else home_score
        if conceded == 0:
            count += 1
    return count


def build_team_stats(
    *,
    season: str,
    standings_rows: list[dict],
    matches: list[dict],
    deep: dict[str, dict],
    source: str,
    now: str,
) -> list[dict]:
    rows: list[dict] = []
    for row in standings_rows or []:
        team = row.get("team") or {}
        team_id = str(team.get("id") or "")
        if not team_id:
            continue
        played = row.get("matches") or 0
        goals_for = row.get("scoresFor") or 0
        goals_against = row.get("scoresAgainst") or 0

        def per_game(value: Any) -> Any:
            try:
                return round(float(value) / played, 2) if played else ""
            except (TypeError, ValueError, ZeroDivisionError):
                return ""

        sampled = deep.get(team_id) or {}
        n = sampled.get("matches") or 0

        def avg(key: str, digits: int = 2) -> Any:
            return round(sampled[key] / n, digits) if n else ""

        xg_for, xg_against = avg("xg_for"), avg("xg_against")
        rows.append({
            "season": row.get("season") or season,
            "team_id": team_id,
            "team": team.get("name") or "",
            "position": row.get("position", ""),
            "played": played,
            "wins": row.get("wins", ""),
            "draws": row.get("draws", ""),
            "losses": row.get("losses", ""),
            "goals_for": goals_for,
            "goals_against": goals_against,
            "goal_diff": row.get("scoreDiff", (goals_for - goals_against) if isinstance(goals_for, int) else ""),
            "points": row.get("points", ""),
            "form": (row.get("form") or "").strip() or form_string(matches, team_id),
            "avg_goals_for": per_game(goals_for),
            "avg_goals_against": per_game(goals_against),
            "clean_sheets": clean_sheets(matches, team_id),
            "avg_possession": avg("possession", 1),
            "avg_shots": avg("shots", 1),
            "avg_shots_on_target": avg("on_target", 1),
            "xg_for": xg_for,
            "xg_against": xg_against,
            "xg_diff": round(xg_for - xg_against, 2) if n else "",
            "matches_sampled": n,
            "source": source,
            "updated_at": now,
        })
    return rows


def accumulate_deep(deep: dict[str, dict], match: dict, detail: dict) -> None:
    """Fold one detailed match (shotmap + possession) into per-team averages."""
    shots = aggregate_shots(detail.get("shotmap") or {})
    possession = extract_possession(detail.get("statistics") or {})

    for side, team_key in (("home", "home_team_id"), ("away", "away_team_id")):
        team_id = match.get(team_key)
        if not team_id:
            continue
        bucket = deep.setdefault(team_id, {
            "matches": 0, "shots": 0, "on_target": 0, "xg_for": 0.0,
            "xg_against": 0.0, "possession": 0.0,
        })
        other = "away" if side == "home" else "home"
        bucket["matches"] += 1
        bucket["shots"] += shots[side]["shots"]
        bucket["on_target"] += shots[side]["on_target"]
        bucket["xg_for"] += shots[side]["xg"]
        bucket["xg_against"] += shots[other]["xg"]
        bucket["possession"] += possession.get(side, 0.0)


# --- Sources --------------------------------------------------------------------

class SofascoreSource:
    """Primary source: Sofascore's public JSON API."""

    name = "sofascore"

    def __init__(self, tournament_id: int | None = None, season_id: int | None = None,
                 use_cache: bool = True) -> None:
        self.tournament_id = int(tournament_id) if tournament_id else None
        self.season_id = int(season_id) if season_id else None
        self.use_cache = use_cache
        self.season_label = ""
        self.tournament_name = "NPFL"

    def _get(self, path: str) -> dict:
        return get_json(f"{SOFASCORE_BASE}{path}", use_cache=self.use_cache)

    def connect(self) -> "SofascoreSource":
        self.tournament_id = self.tournament_id or discover_npfl_tournament(self.use_cache)
        seasons = self._get(f"/unique-tournament/{self.tournament_id}/seasons").get("seasons") or []
        if not seasons:
            raise SourceError("Sofascore returned no seasons for the NPFL tournament")

        if not self.season_id:
            current = max(seasons, key=_season_rank)
            self.season_id = int(current.get("id"))
            self.season_label = str(current.get("name") or current.get("year") or self.season_id)
        else:
            match = next((s for s in seasons if int(s.get("id", 0)) == self.season_id), {})
            self.season_label = str(match.get("name") or match.get("year") or self.season_id)

        self.tournament_name = (self._get(f"/unique-tournament/{self.tournament_id}").get("uniqueTournament") or {}).get("name") or "NPFL"
        log(f"connected: tournament={self.tournament_id} ({self.tournament_name}) season={self.season_id} ({self.season_label})")
        return self

    def events(self, kind: str, page: int = 0) -> list[dict]:
        payload = self._get(
            f"/unique-tournament/{self.tournament_id}/season/{self.season_id}/events/{kind}/{page}"
        )
        return payload.get("events") or []

    def standings_rows(self) -> list[dict]:
        payload = self._get(
            f"/unique-tournament/{self.tournament_id}/season/{self.season_id}/standings/total"
        )
        standings = payload.get("standings") or []
        return (standings[0].get("rows") or []) if standings else []

    def detail(self, match_id: str) -> dict:
        detail: dict[str, Any] = {}
        for key in ("incidents", "lineups", "shotmap", "statistics"):
            try:
                detail[key] = self._get(f"/event/{match_id}/{key}")
            except SourceError as exc:
                log(f"detail {key} missing for {match_id}: {exc}")
                detail[key] = {}
        return detail


def _season_rank(season: dict) -> tuple[int, int]:
    digits = re.sub(r"\D", "", str(season.get("year") or season.get("name") or ""))
    return (int(digits) if digits else 0, int(season.get("id") or 0))


def discover_npfl_tournament(use_cache: bool = True) -> int:
    override = (os.environ.get("SOFASCORE_NPFL_TOURNAMENT_ID") or "").strip()
    if override:
        return int(override)

    query = quote("Nigeria Premier League")
    payload = get_json(f"{SOFASCORE_BASE}/search/all?q={query}", use_cache=use_cache)
    for result in payload.get("results") or []:
        entity = result.get("entity") or {}
        unique = entity.get("uniqueTournament") or {}
        name = str(unique.get("name") or entity.get("name") or "")
        slug = str(unique.get("slug") or entity.get("slug") or "").lower()
        blob = f"{name} {slug}".lower()
        if "premier" in blob and ("nigeria" in blob or "npfl" in blob):
            return int(unique.get("id") or entity.get("id"))

    raise SourceError(
        "could not discover the NPFL tournament id on Sofascore; "
        "set SOFASCORE_NPFL_TOURNAMENT_ID explicitly"
    )


class FootyStatsSource:
    """Fallback 1: FootyStats free tier (fixtures + basic stats, no lineups)."""

    name = "footystats"
    BASE = "https://api.footystats.org"

    def __init__(self, use_cache: bool = True) -> None:
        self.key = (os.environ.get("FOOTYSTATS_API_KEY") or "").strip()
        self.league_id = (os.environ.get("FOOTYSTATS_NPFL_LEAGUE_ID") or "").strip()
        self.season_label = (os.environ.get("NPFL_SEASON_LABEL") or "").strip()
        self.use_cache = use_cache
        self.payload: list[dict] = []

    def connect(self) -> "FootyStatsSource":
        if not self.key:
            raise SourceError("FOOTYSTATS_API_KEY not set")
        if not self.league_id:
            raise SourceError("FOOTYSTATS_NPFL_LEAGUE_ID not set (free tier needs an explicit league id)")
        data = get_json(
            f"{self.BASE}/league-matches?key={self.key}&league_id={self.league_id}",
            use_cache=self.use_cache,
        )
        self.payload = data.get("data") or []
        log(f"connected: footystats league_id={self.league_id} ({len(self.payload)} matches)")
        return self

    def events(self, kind: str, page: int = 0) -> list[dict]:
        # FootyStats returns one league payload; map it into the shared event shape.
        events = []
        for item in self.payload:
            finished = str(item.get("status", "")).lower() in ("complete", "finished")
            if kind == "last" and not finished:
                continue
            if kind == "next" and finished:
                continue
            events.append({
                "id": item.get("id") or _short_hash(item.get("home_name"), item.get("away_name"), item.get("date_unix")),
                "startTimestamp": item.get("date_unix"),
                "homeTeam": {"id": item.get("homeID"), "name": item.get("home_name")},
                "awayTeam": {"id": item.get("awayID"), "name": item.get("away_name")},
                "homeScore": {"current": item.get("homeGoalCount")},
                "awayScore": {"current": item.get("awayGoalCount")},
                "status": {"type": "finished" if finished else "notstarted"},
                "tournament": {"name": "NPFL"},
            })
        return events

    def standings_rows(self) -> list[dict]:
        # Standings are derived from results so the fallback never fabricates a table.
        table: dict[str, dict] = {}
        for event in self.events("last"):
            home = event["homeTeam"]
            away = event["awayTeam"]
            for team, other, ours, theirs in (
                (home, away, event["homeScore"]["current"], event["awayScore"]["current"]),
                (away, home, event["awayScore"]["current"], event["homeScore"]["current"]),
            ):
                try:
                    ours_i, theirs_i = int(ours), int(theirs)
                except (TypeError, ValueError):
                    continue
                row = table.setdefault(str(team.get("id")), {
                    "team": team, "matches": 0, "wins": 0, "draws": 0, "losses": 0,
                    "scoresFor": 0, "scoresAgainst": 0, "points": 0,
                })
                row["matches"] += 1
                row["scoresFor"] += ours_i
                row["scoresAgainst"] += theirs_i
                if ours_i > theirs_i:
                    row["wins"] += 1
                    row["points"] += 3
                elif ours_i == theirs_i:
                    row["draws"] += 1
                    row["points"] += 1
                else:
                    row["losses"] += 1
        ordered = sorted(table.values(), key=lambda r: (-r["points"], -(r["scoresFor"] - r["scoresAgainst"])))
        for position, row in enumerate(ordered, start=1):
            row["position"] = position
            row["scoreDiff"] = row["scoresFor"] - row["scoresAgainst"]
        return ordered

    def detail(self, match_id: str) -> dict:
        return {}


class FlashscoreSource:
    """Fallback 2: Flashscore public results page (fixtures/scores only, best effort)."""

    name = "flashscore"

    def __init__(self, use_cache: bool = True) -> None:
        self.url = os.environ.get("FLASHSCORE_NPFL_URL", "https://www.flashscore.com/football/nigeria/npfl/")
        self.season_label = (os.environ.get("NPFL_SEASON_LABEL") or "").strip()
        self.use_cache = use_cache
        self.html = ""

    def connect(self) -> "FlashscoreSource":
        key = hashlib.md5(self.url.encode()).hexdigest()
        path = os.path.join(CACHE_DIR, f"flashscore_{key}.html")
        if self.use_cache and os.path.exists(path) and time.time() - os.path.getmtime(path) < int(
            os.environ.get("NPFL_CACHE_TTL", "1800")
        ):
            with open(path, "r", encoding="utf-8") as fh:
                self.html = fh.read()
            return self

        response = session().get(self.url, timeout=25)
        if response.status_code != 200:
            raise SourceError(f"Flashscore returned HTTP {response.status_code}")
        self.html = response.text
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.html)
        except OSError:
            pass
        if "event__match" not in self.html and "data-event-id" not in self.html:
            raise SourceError("Flashscore markup not parseable (page may be JS-rendered)")
        return self

    def events(self, kind: str, page: int = 0) -> list[dict]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(self.html, "lxml")
        events = []
        for row in soup.select("div.event__match, div[data-event-id]"):
            event_id = row.get("id", "").replace("g_1_", "") or row.get("data-event-id", "")
            home = row.select_one(".event__homeParticipant, .event__participant--home")
            away = row.select_one(".event__awayParticipant, .event__participant--away")
            home_score = row.select_one(".event__score--home")
            away_score = row.select_one(".event__score--away")
            if not (home and away and event_id):
                continue
            finished = home_score is not None and away_score is not None
            events.append({
                "id": f"fs_{event_id}",
                "startTimestamp": None,
                "homeTeam": {"name": home.get_text(strip=True)},
                "awayTeam": {"name": away.get_text(strip=True)},
                "homeScore": {"current": home_score.get_text(strip=True) if home_score else ""},
                "awayScore": {"current": away_score.get_text(strip=True) if away_score else ""},
                "status": {"type": "finished" if finished else "notstarted"},
                "tournament": {"name": "NPFL"},
            })
        if not events:
            raise SourceError("Flashscore page contained no match rows")
        return events

    def standings_rows(self) -> list[dict]:
        return []

    def detail(self, match_id: str) -> dict:
        return {}


class TheSportsDBSource:
    """Fallback: TheSportsDB free API — real NPFL results and the league table.

    No lineups, incidents, shot coordinates or xG, so those columns stay blank.
    Useful because it works from datacentre IPs where Sofascore returns 403.
    """

    name = "thesportsdb"
    BASE = "https://www.thesportsdb.com/api/v1/json"

    def __init__(self, use_cache: bool = True) -> None:
        self.key = (os.environ.get("THESPORTSDB_API_KEY") or "123").strip()
        self.league_id = (os.environ.get("THESPORTSDB_NPFL_LEAGUE_ID") or "4827").strip()
        self.season_label = (os.environ.get("NPFL_SEASON_LABEL") or "").strip()
        self.use_cache = use_cache
        self._teams: dict[str, str] = {}
        self._past: list[dict] = []
        self._table: list[dict] = []

    def _get(self, path: str) -> dict:
        return get_json(f"{self.BASE}/{self.key}/{path}", use_cache=self.use_cache)

    def connect(self) -> "TheSportsDBSource":
        payload = self._get(f"eventspastleague.php?id={self.league_id}")
        self._past = payload.get("events") or []
        if self._past and not self.season_label:
            self.season_label = str(self._past[0].get("strSeason") or "")

        rows: list[dict] = []
        if self.season_label:
            rows = self._get(
                f"lookuptable.php?l={self.league_id}&s={self.season_label}"
            ).get("table") or []
        if not rows:
            rows = self._get(f"lookuptable.php?l={self.league_id}").get("table") or []
        self._table = rows

        for row in rows:
            name = str(row.get("strTeam") or "")
            if name:
                self._teams[_team_key(name)] = str(row.get("idTeam") or name)
        if not self.season_label:
            self.season_label = str((rows[0] if rows else {}).get("strSeason") or "")

        log(
            f"connected: thesportsdb league={self.league_id} "
            f"({len(self._past)} results, {len(rows)} table rows, season={self.season_label or 'unknown'})"
        )
        return self

    def events(self, kind: str, page: int = 0) -> list[dict]:
        if kind == "last":
            source = list(self._past)
        else:
            payload = self._get(f"eventsnextleague.php?id={self.league_id}")
            source = payload.get("events") or []

        out: list[dict] = []
        for event in source:
            home = str(event.get("strHomeTeam") or "")
            away = str(event.get("strAwayTeam") or "")
            home_score = event.get("intHomeScore")
            away_score = event.get("intAwayScore")
            finished = str(home_score).strip().isdigit() and str(away_score).strip().isdigit()
            stamp = event.get("strTimestamp") or (
                f"{event.get('dateEvent', '')}T{event.get('strTime') or '00:00:00'}"
            )
            out.append({
                "id": str(event.get("idEvent") or ""),
                "startTimestamp": _epoch(stamp),
                "homeTeam": {"id": self._teams.get(_team_key(home), _team_key(home)), "name": home},
                "awayTeam": {"id": self._teams.get(_team_key(away), _team_key(away)), "name": away},
                "homeScore": {"current": home_score if finished else ""},
                "awayScore": {"current": away_score if finished else ""},
                "status": {"type": "finished" if finished else "notstarted"},
                "roundInfo": {"round": event.get("intRound") or ""},
                "venue": {"stadium": {"name": event.get("strVenue") or ""}},
                "tournament": {"name": "NPFL"},
            })
        return out

    def standings_rows(self) -> list[dict]:
        rows: list[dict] = []
        for row in self._table:
            goals_for = _int(row.get("intGoalsFor"))
            goals_against = _int(row.get("intGoalsAgainst"))
            rows.append({
                "position": _int(row.get("intRank")),
                "team": {"id": str(row.get("idTeam") or ""), "name": row.get("strTeam") or ""},
                "matches": _int(row.get("intPlayed")),
                "wins": _int(row.get("intWin")),
                "draws": _int(row.get("intDraw")),
                "losses": _int(row.get("intLoss")),
                "scoresFor": goals_for,
                "scoresAgainst": goals_against,
                "points": _int(row.get("intPoints")),
                "scoreDiff": goals_for - goals_against,
                "form": str(row.get("strForm") or ""),
                "season": str(row.get("strSeason") or ""),
            })
        return rows

    def detail(self, match_id: str) -> dict:
        return {}


def build_source(args: argparse.Namespace):
    errors: list[str] = []

    try:
        return SofascoreSource(args.tournament_id, args.season_id, not args.no_cache).connect()
    except Exception as exc:
        errors.append(f"sofascore: {exc}")
        log(f"Sofascore unavailable ({exc}); trying fallbacks")

    if (os.environ.get("FOOTYSTATS_API_KEY") or "").strip():
        try:
            return FootyStatsSource(not args.no_cache).connect()
        except Exception as exc:
            errors.append(f"footystats: {exc}")

    try:
        return TheSportsDBSource(not args.no_cache).connect()
    except Exception as exc:
        errors.append(f"thesportsdb: {exc}")
        log(f"TheSportsDB unavailable ({exc}); trying Flashscore")

    try:
        return FlashscoreSource(not args.no_cache).connect()
    except Exception as exc:
        errors.append(f"flashscore: {exc}")

    raise SystemExit("All sources failed -> " + "; ".join(errors))


# --- Demo payload ---------------------------------------------------------------

DEMO = {
    "events": [
        {
            "id": 12345678,
            "startTimestamp": 1762694400,
            "roundInfo": {"round": 11},
            "status": {"type": "finished", "description": "Ended"},
            "homeTeam": {"id": 9031, "name": "Rangers International"},
            "awayTeam": {"id": 9032, "name": "Enyimba"},
            "homeScore": {"current": 2},
            "awayScore": {"current": 1},
            "tournament": {"name": "NPFL"},
            "venue": {"stadium": {"name": "Nnamdi Azikiwe Stadium"}},
        },
        {
            "id": 12345679,
            "startTimestamp": 1763299200,
            "roundInfo": {"round": 12},
            "status": {"type": "notstarted", "description": "Not started"},
            "homeTeam": {"id": 9032, "name": "Enyimba"},
            "awayTeam": {"id": 9031, "name": "Rangers International"},
            "homeScore": {"current": None},
            "awayScore": {"current": None},
            "tournament": {"name": "NPFL"},
        },
    ],
    "standings": {"standings": [{"rows": [
        {"position": 1, "team": {"id": 9031, "name": "Rangers International"}, "matches": 11,
         "wins": 7, "draws": 3, "losses": 1, "scoresFor": 18, "scoresAgainst": 7, "points": 24, "scoreDiff": 11},
        {"position": 2, "team": {"id": 9032, "name": "Enyimba"}, "matches": 11,
         "wins": 6, "draws": 2, "losses": 3, "scoresFor": 15, "scoresAgainst": 11, "points": 20, "scoreDiff": 4},
    ]}]},
    "incidents": {"incidents": [
        {"incidentType": "goal", "incidentClass": "regular", "time": 23, "isHome": True,
         "player": {"id": 501, "name": "K. Nwankwo"}, "assist1": {"name": "S. Bello"},
         "homeScore": 1, "awayScore": 0},
        {"incidentType": "card", "incidentClass": "yellow", "time": 41, "isHome": False,
         "player": {"id": 601, "name": "O. Chinedu"}},
        {"incidentType": "substitution", "time": 68, "isHome": True,
         "player": {"id": 503, "name": "A. Musa"}},
        {"incidentType": "goal", "incidentClass": "penalty", "time": 88, "isHome": True,
         "player": {"id": 502, "name": "T. Adeyemi"}, "homeScore": 2, "awayScore": 1},
    ]},
    "shotmap": {"shotmap": [
        {"time": 23, "isHome": True, "shotType": "goal", "xg": 0.31,
         "playerCoordinates": {"x": 84, "y": 46}, "bodyPart": "right-foot", "situation": "open-play"},
        {"time": 55, "isHome": False, "shotType": "save", "xg": 0.12,
         "playerCoordinates": {"x": 18, "y": 60}, "bodyPart": "head", "situation": "corner-kick"},
        {"time": 88, "isHome": True, "shotType": "goal", "xg": 0.78,
         "playerCoordinates": {"x": 88, "y": 50}, "bodyPart": "penalty", "situation": "penalty"},
    ]},
    "lineups": {
        "home": {"team": {"id": 9031, "name": "Rangers International"}, "formation": "4-3-3",
                 "players": [
                     {"player": {"id": 501, "name": "K. Nwankwo", "jerseyNumber": 9}, "position": "F",
                      "substitute": False, "statistics": {"rating": 7.8}, "averagePosition": {"x": 78, "y": 44}},
                     {"player": {"id": 510, "name": "P. Okoro", "jerseyNumber": 1}, "position": "G",
                      "substitute": False, "statistics": {"rating": 6.9}, "averagePosition": {"x": 8, "y": 50}},
                 ]},
        "away": {"team": {"id": 9032, "name": "Enyimba"}, "formation": "4-2-3-1",
                 "players": [
                     {"player": {"id": 601, "name": "O. Chinedu", "jerseyNumber": 10}, "position": "M",
                      "substitute": False, "statistics": {"rating": 7.1}, "averagePosition": {"x": 62, "y": 55}},
                 ]},
    },
    "statistics": {"statistics": [{"period": "ALL", "groups": [
        {"statisticsItems": [{"name": "Ball possession", "home": "54%", "away": "46%"}]},
    ]}]},
}


# --- Main -----------------------------------------------------------------------

def collect(args: argparse.Namespace) -> dict[str, list[dict]]:
    """Fetch and normalise everything; returns table -> rows (no writes)."""
    now = utc_now()

    if args.demo:
        season = "demo"
        events_payload = DEMO["events"]
        standings_rows = DEMO["standings"]["standings"][0]["rows"]
        detail_for = {str(ev["id"]): DEMO for ev in DEMO["events"][:1]}
        source_name = "sofascore-demo"
        log("demo mode: using bundled Sofascore-shaped payloads (no network)")
    else:
        source = build_source(args)
        season = getattr(source, "season_label", "") or ""
        source_name = source.name
        upcoming = source.events("next")[: args.next_limit]
        recent = source.events("last")[: args.last_limit]
        seen: set[str] = set()
        events_payload = []
        for event in upcoming + recent:
            key = str(event.get("id"))
            if key and key not in seen:
                seen.add(key)
                events_payload.append(event)
        standings_rows = source.standings_rows()
        finished = [e for e in events_payload if (e.get("status") or {}).get("type") == "finished"]
        finished.sort(key=lambda e: e.get("startTimestamp") or 0, reverse=True)
        detail_for = {}
        for event in finished[: max(args.detail_limit, 0)]:
            match_id = str(event.get("id"))
            log(f"fetching detail for match {match_id}")
            detail_for[match_id] = source.detail(match_id)

    matches = [normalize_match(event, season=season, source=source_name, now=now)
               for event in events_payload]

    rows: dict[str, list[dict]] = {
        "matches": matches,
        "team_stats": [],
        "lineups": [],
        "events": [],
    }

    deep: dict[str, dict] = {}
    for match in matches:
        detail = detail_for.get(match["match_id"])
        if not detail:
            continue
        rows["lineups"].extend(
            normalize_lineups(match["match_id"], detail.get("lineups") or {}, source=source_name, now=now)
        )
        rows["events"].extend(
            normalize_incidents(
                match["match_id"],
                detail.get("incidents") or {},
                source=source_name,
                shotmap=detail.get("shotmap") or {},
                now=now,
            )
        )
        if match.get("status") == "finished":
            accumulate_deep(deep, match, detail)

    rows["team_stats"] = build_team_stats(
        season=season,
        standings_rows=standings_rows,
        matches=matches,
        deep=deep,
        source=source_name,
        now=now,
    )
    return rows


def write_to_db(rows: dict[str, list[dict]]) -> dict[str, dict[str, int]]:
    target = db.ensure_schema()
    log(f"storage backend: sqlite -> {target}")
    summary: dict[str, dict[str, int]] = {}
    for table, records in rows.items():
        if not records:
            summary[table] = {"created": 0, "updated": 0, "skipped": 0}
            continue
        summary[table] = db.upsert_rows(table, records)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest NPFL fixtures, form, lineups and events into a local SQLite database.")
    parser.add_argument("--tournament-id", type=int, default=None, help="Sofascore unique-tournament id override")
    parser.add_argument("--season-id", type=int, default=None, help="Sofascore season id override")
    parser.add_argument("--next-limit", type=int, default=20, help="upcoming fixtures to ingest (default 20)")
    parser.add_argument("--last-limit", type=int, default=20, help="recent results to ingest (default 20)")
    parser.add_argument("--detail-limit", type=int, default=5,
                        help="finished matches to fetch lineups/incidents/shotmaps for (default 5, keeps requests low)")
    parser.add_argument("--dry-run", action="store_true", help="normalise and print, never write")
    parser.add_argument("--demo", action="store_true", help="offline parser test on bundled payloads (implies --dry-run unless --write)")
    parser.add_argument("--write", action="store_true", help="write results even with --demo")
    parser.add_argument("--no-cache", action="store_true", help="disable the HTTP disk cache")
    args = parser.parse_args(argv)

    if args.demo and not args.write:
        args.dry_run = True

    rows = collect(args)
    log(f"requests used this run: {_request_count} (budget {REQUEST_BUDGET})")

    for table, records in rows.items():
        log(f"{table}: {len(records)} rows prepared")

    if args.dry_run:
        for table, records in rows.items():
            for record in records[:3]:
                log(f"  {table} sample: {json.dumps(record, ensure_ascii=False)[:400]}")
        log("dry-run: nothing written")
        return 0

    summary = write_to_db(rows)
    for table, stats in summary.items():
        log(f"{table}: created={stats['created']} updated={stats['updated']} skipped={stats['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
