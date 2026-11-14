"""Google Sheets layer for the NPFL scouting bot.

This is the ONLY module allowed to talk to Google Sheets. It owns:
  * the tab schemas (our "relational" tables),
  * service-account authentication (env vars only, never hardcoded),
  * idempotent upserts keyed on stable IDs,
  * read helpers used by the LLM tools in Phase 2.

Nothing here is imported eagerly by the scrapers' parsers, so the offline
``--demo`` paths work without gspread installed.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Sequence

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DEFAULT_SHEET_NAME = "NPFL Scouting DB"
DEFAULT_LOCAL_STORE = os.path.join(".cache", "npfl_local_store.json")

# --- Tab schemas -----------------------------------------------------------------
# Header names are the contract. `video_id` etc. stay lowercase snake_case so both
# the scrapers and the LLM tools use the same field names.

TAB_SCHEMAS: dict[str, list[str]] = {
    "NPFL_Matches": [
        "match_id", "source", "season", "matchday", "kickoff_utc", "kickoff_lagos",
        "status", "home_team", "home_team_id", "away_team", "away_team_id",
        "home_score", "away_score", "venue", "tournament", "match_url", "updated_at",
    ],
    "NPFL_Team_Stats": [
        "season", "team_id", "team", "position", "played", "wins", "draws", "losses",
        "goals_for", "goals_against", "goal_diff", "points", "form",
        "avg_goals_for", "avg_goals_against", "clean_sheets",
        "avg_possession", "avg_shots", "avg_shots_on_target", "xg_for", "xg_against",
        "xg_diff", "matches_sampled", "source", "updated_at",
    ],
    "NPFL_Lineups": [
        "match_id", "team_id", "team", "player_id", "player", "shirt_number",
        "position", "is_starter", "formation", "avg_x", "avg_y", "rating",
        "source", "updated_at",
    ],
    "NPFL_Events": [
        "match_id", "event_key", "minute", "minute_label", "event_type", "detail",
        "team", "player", "assist", "home_score", "away_score", "x", "y",
        "shot_type", "body_part", "situation", "source", "updated_at",
    ],
    "Video_Tactical_Clips": [
        "clip_id", "match_id", "video_id", "video_url", "timestamp_seconds",
        "timestamp_label", "clip_url", "event_type", "minute", "team", "player",
        "tactical_note", "keywords", "confidence", "evidence_source", "created_at",
    ],
    "Video_Queue": [
        "video_url", "match_id", "status", "queued_at", "processed_at", "video_id", "notes",
    ],
}

# Stable primary keys per tab (used for upsert de-duplication).
KEY_FIELDS: dict[str, list[str]] = {
    "NPFL_Matches": ["match_id"],
    "NPFL_Team_Stats": ["season", "team_id"],
    "NPFL_Lineups": ["match_id", "team_id", "player_id"],
    "NPFL_Events": ["match_id", "event_key"],
    "Video_Tactical_Clips": ["clip_id"],
    "Video_Queue": ["video_url"],
}


# --- Auth -----------------------------------------------------------------------

# Secret stores hand us names like "Telegram_Bot_Token" or "Telegram Bot Token";
# normalise them to the canonical env names used across the codebase.
CANONICAL_ENV_KEYS = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "GROQ_API_KEY", "GROQ_MODEL",
    "GOOGLE_SHEET_ID", "GOOGLE_SHEET_NAME", "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_SERVICE_ACCOUNT_FILE", "DRIVE_FOLDER_ID",
]
_ENV_ALIASES = {
    "telegram_token": "TELEGRAM_BOT_TOKEN",
    "bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_bot_api_token": "TELEGRAM_BOT_TOKEN",
    "google_service_account": "GOOGLE_SERVICE_ACCOUNT_JSON",
    "sheet_id": "GOOGLE_SHEET_ID",
    "spreadsheet_id": "GOOGLE_SHEET_ID",
}


def _normalise_env_key(key: str) -> str:
    return "_".join(part for part in "".join(
        char if char.isalnum() else "_" for char in key.lower()
    ).split("_") if part)


def bootstrap_env() -> None:
    """Copy secret-store spellings onto the canonical env var names (values untouched)."""
    lookup = {_normalise_env_key(name): name for name in CANONICAL_ENV_KEYS}
    lookup.update(_ENV_ALIASES)
    for key, value in list(os.environ.items()):
        canonical = lookup.get(_normalise_env_key(key))
        if canonical and value.strip() and not (os.environ.get(canonical) or "").strip():
            os.environ[canonical] = value.strip()


def load_credentials(scopes: Sequence[str] | None = None):
    """Build service-account credentials from environment variables.

    Order: ``GOOGLE_SERVICE_ACCOUNT_JSON`` (JSON string, used in CI) then
    ``GOOGLE_SERVICE_ACCOUNT_FILE`` (defaults to ``credentials.json``).
    """
    from google.oauth2.service_account import Credentials

    bootstrap_env()
    scopes = list(scopes or SCOPES)
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=scopes)

    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "credentials.json"
    if not os.path.exists(path):
        raise RuntimeError(
            "No Google credentials found. Set GOOGLE_SERVICE_ACCOUNT_JSON or provide "
            f"{path} (service account with access to GOOGLE_SHEET_ID)."
        )
    return Credentials.from_service_account_file(path, scopes=scopes)


def get_client():
    """Authorised gspread client."""
    import gspread

    return gspread.authorize(load_credentials())


# --- Local store (temporary, credential-free backend) ----------------------------
# Set NPFL_STORAGE=local to run the whole stack against a JSON file that mimics the
# handful of gspread calls we actually use. This exists only so the pipeline and the
# bot can be validated before Google credentials exist. Production stays on Sheets.


class LocalWorksheetNotFound(Exception):
    """Raised by the local backend when a tab does not exist yet."""


class LocalWorksheet:
    def __init__(self, store: "LocalSpreadsheet", title: str) -> None:
        self._store = store
        self.title = title

    def _rows(self) -> list[list[str]]:
        return self._store._data.setdefault(self.title, [])

    def row_values(self, index: int) -> list[str]:
        rows = self._rows()
        return list(rows[index - 1]) if 0 < index <= len(rows) else []

    def get_all_values(self) -> list[list[str]]:
        return [list(row) for row in self._rows()]

    def update(self, values=None, range_name="A1", value_input_option="RAW", **_) -> None:
        values = values or []
        match = re.match(r"^([A-Za-z]+)(\d+)$", str(range_name or "A1"))
        start_col = _column_index(match.group(1)) if match else 0
        start_row = (int(match.group(2)) - 1) if match else 0
        rows = self._rows()
        for offset, row in enumerate(values):
            target = start_row + offset
            while len(rows) <= target:
                rows.append([])
            existing = rows[target]
            cells = list(row) if isinstance(row, (list, tuple)) else [row]
            for col_offset, cell in enumerate(cells):
                col = start_col + col_offset
                while len(existing) <= col:
                    existing.append("")
                existing[col] = "" if cell is None else str(cell)
        self._store._flush()

    def batch_update(self, data, value_input_option="RAW", **_) -> None:
        for item in data:
            self.update(values=item.get("values"), range_name=item.get("range", "A1"))

    def append_rows(self, rows, value_input_option="RAW", **_) -> None:
        target = self._rows()
        for row in rows:
            cells = list(row) if isinstance(row, (list, tuple)) else [row]
            target.append(["" if cell is None else str(cell) for cell in cells])
        self._store._flush()


class LocalSpreadsheet:
    """JSON-file stand-in for a gspread Spreadsheet (same subset of methods)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._data: dict[str, list[list[str]]] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = {
                        key: [list(row) for row in value]
                        for key, value in loaded.items()
                        if isinstance(value, list)
                    }
            except (OSError, ValueError):
                self._data = {}

    def worksheet(self, title: str) -> LocalWorksheet:
        if title not in self._data:
            raise LocalWorksheetNotFound(title)
        return LocalWorksheet(self, title)

    def add_worksheet(self, title: str, rows: int = 100, cols: int = 10) -> LocalWorksheet:
        self._data.setdefault(title, [])
        self._flush()
        return LocalWorksheet(self, title)

    def _flush(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


def _column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return max(0, index - 1)


def storage_backend() -> str:
    """'sheets' (production) or 'local' (credential-free testing)."""
    return (os.environ.get("NPFL_STORAGE") or "sheets").strip().lower()


def local_store_path() -> str:
    return (os.environ.get("NPFL_LOCAL_STORE") or DEFAULT_LOCAL_STORE).strip()


def open_spreadsheet(client: Any | None = None):
    """Open the configured store: local JSON file when NPFL_STORAGE=local, else Sheets."""
    if storage_backend() == "local":
        path = local_store_path()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        return LocalSpreadsheet(path)

    client = client or get_client()
    sheet_id = (os.environ.get("GOOGLE_SHEET_ID") or "").strip()
    if sheet_id:
        return client.open_by_key(sheet_id)
    return client.open(os.environ.get("GOOGLE_SHEET_NAME", DEFAULT_SHEET_NAME))


# --- Tabs -----------------------------------------------------------------------

def ensure_worksheet(spreadsheet: Any, title: str):
    """Get the tab, creating it (and its header row) if missing."""
    schema = TAB_SCHEMAS.get(title, [])
    try:
        ws = spreadsheet.worksheet(title)
    except Exception as exc:  # gspread's WorksheetNotFound, or the local equivalent
        if type(exc).__name__ not in ("WorksheetNotFound", "LocalWorksheetNotFound"):
            raise
        ws = spreadsheet.add_worksheet(
            title=title, rows=100, cols=max(len(schema) + 2, 8)
        )
    _sync_header(ws, schema)
    return ws


def ensure_all_tabs(spreadsheet: Any) -> dict[str, Any]:
    return {title: ensure_worksheet(spreadsheet, title) for title in TAB_SCHEMAS}


def _sync_header(ws: Any, headers: list[str]) -> None:
    """Guarantee the tab's headers contain every schema column (order preserved)."""
    if not headers:
        return
    existing: list[str] = []
    try:
        existing = ws.row_values(1)
    except Exception:  # empty sheet / transient read error
        existing = []

    if not existing:
        _write(ws, [headers], "A1")
        return

    missing = [h for h in headers if h not in existing]
    if missing:
        _write(ws, [list(existing) + missing], "A1")


def _write(ws: Any, values: list[list[Any]], range_name: str) -> None:
    # Keyword form works on both gspread 5.x and 6.x.
    ws.update(values=values, range_name=range_name, value_input_option="RAW")


# --- Upsert ---------------------------------------------------------------------

def upsert_rows(
    ws: Any,
    records: Iterable[dict[str, Any]],
    *,
    tab: str | None = None,
    key_fields: Sequence[str] | None = None,
) -> dict[str, int]:
    """Insert-or-update records in ``ws`` keyed on ``key_fields``.

    Returns ``{"created": n, "updated": n, "skipped": n}``.
    """
    records = [r for r in records if r]
    stats = {"created": 0, "updated": 0, "skipped": 0}
    if not records:
        return stats

    tab = tab or getattr(ws, "title", "")
    headers = TAB_SCHEMAS.get(tab) or list(records[0].keys())
    key_fields = list(key_fields or KEY_FIELDS.get(tab) or [headers[0]])

    values = ws.get_all_values()
    header = list(values[0]) if values and values[0] else list(headers)
    for h in headers:
        if h not in header:
            header.append(h)
    if not values or values[0] != header:
        _write(ws, [header], "A1")

    index = {h: i for i, h in enumerate(header)}
    if any(k not in index for k in key_fields):
        raise ValueError(f"{tab}: key fields {key_fields} missing from header {header}")

    existing_row: dict[tuple[str, ...], int] = {}
    for offset, row in enumerate(values[1:], start=2):
        padded = list(row) + [""] * (len(header) - len(row))
        key = tuple(padded[index[k]].strip() for k in key_fields)
        if any(key):
            existing_row[key] = offset

    updates: list[dict[str, Any]] = []
    appends: list[list[Any]] = []

    for rec in records:
        key = tuple(_cell(rec.get(k)).strip() for k in key_fields)
        if not all(key):
            stats["skipped"] += 1
            continue
        row = [_cell(rec.get(h)) for h in header]
        if key in existing_row:
            updates.append({"range": f"A{existing_row[key]}", "values": [row]})
            stats["updated"] += 1
        else:
            appends.append(row)
            stats["created"] += 1

    if updates:
        for chunk in _chunks(updates, 200):
            ws.batch_update(chunk, value_input_option="RAW")
    if appends:
        for chunk in _chunks(appends, 500):
            ws.append_rows(chunk, value_input_option="RAW")
    return stats


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        # Keep spreadsheet-friendly precision without scientific notation.
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def _chunks(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --- Reads ----------------------------------------------------------------------

def read_records(tab: str, spreadsheet: Any | None = None) -> list[dict[str, str]]:
    """Return all non-empty rows of ``tab`` as dicts (header-driven)."""
    bootstrap_env()

    sh = spreadsheet or open_spreadsheet()
    try:
        ws = sh.worksheet(tab)
    except Exception as exc:
        if type(exc).__name__ in ("WorksheetNotFound", "LocalWorksheetNotFound"):
            return []
        raise

    values = ws.get_all_values()
    if len(values) < 2:
        return []
    header = values[0]
    records = []
    for row in values[1:]:
        if not any(str(c).strip() for c in row):
            continue
        padded = list(row) + [""] * (len(header) - len(row))
        records.append(dict(zip(header, padded)))
    return records


bootstrap_env()


def find_records(tab: str, **equals: str) -> list[dict[str, str]]:
    """Filter ``read_records(tab)`` by exact, case-insensitive column matches."""
    wanted = {k: str(v).strip().lower() for k, v in equals.items()}
    out = []
    for rec in read_records(tab):
        if all(str(rec.get(k, "")).strip().lower() == v for k, v in wanted.items()):
            out.append(rec)
    return out
