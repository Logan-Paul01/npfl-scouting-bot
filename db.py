"""SQLite storage layer for the NPFL scouting bot (`scouting.db`).

This is the ONLY module allowed to touch the database. It owns:

  * the table schemas (the source of truth for column names),
  * connection handling and schema creation,
  * idempotent upserts keyed on stable IDs,
  * read helpers used by the scrapers and by the bot's LLM tools.

Python's built-in ``sqlite3`` only: no external database, no credentials, no
network. The file lives in the repository root (``scouting.db``) so the whole
dataset is a single portable artifact that can be opened with any SQLite client.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Iterable, Sequence

DEFAULT_DB_NAME = "scouting.db"
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# --- Schemas --------------------------------------------------------------------
# Column names are the contract: the scrapers, the video pipeline and the bot's
# LLM tools all use these exact snake_case names. Every column is TEXT so that a
# missing free-tier value stays an empty string instead of a fabricated zero.

SCHEMAS: dict[str, list[str]] = {
    "matches": [
        "match_id", "source", "season", "matchday", "kickoff_utc", "kickoff_lagos",
        "status", "home_team", "home_team_id", "away_team", "away_team_id",
        "home_score", "away_score", "venue", "tournament", "match_url", "updated_at",
    ],
    "team_stats": [
        "season", "team_id", "team", "position", "played", "wins", "draws", "losses",
        "goals_for", "goals_against", "goal_diff", "points", "form",
        "avg_goals_for", "avg_goals_against", "clean_sheets",
        "avg_possession", "avg_shots", "avg_shots_on_target", "xg_for", "xg_against",
        "xg_diff", "matches_sampled", "source", "updated_at",
    ],
    "lineups": [
        "match_id", "team_id", "team", "player_id", "player", "shirt_number",
        "position", "is_starter", "formation", "avg_x", "avg_y", "rating",
        "source", "updated_at",
    ],
    "events": [
        "match_id", "event_key", "minute", "minute_label", "event_type", "detail",
        "team", "player", "assist", "home_score", "away_score", "x", "y",
        "shot_type", "body_part", "situation", "source", "updated_at",
    ],
    "video_clips": [
        "clip_id", "match_id", "video_id", "video_url", "timestamp_seconds",
        "timestamp_label", "clip_url", "event_type", "minute", "team", "player",
        "tactical_note", "keywords", "confidence", "evidence_source", "created_at",
    ],
    "video_queue": [
        "video_url", "match_id", "status", "queued_at", "processed_at", "video_id", "notes",
    ],
}

# Stable primary keys (drive the upsert conflict target).
KEY_FIELDS: dict[str, list[str]] = {
    "matches": ["match_id"],
    "team_stats": ["season", "team_id"],
    "lineups": ["match_id", "team_id", "player_id"],
    "events": ["match_id", "event_key"],
    "video_clips": ["clip_id"],
    "video_queue": ["video_url"],
}


# --- Connection -----------------------------------------------------------------

def db_path() -> str:
    """Path to the SQLite file (``NPFL_DB_PATH`` overrides the repo-root default)."""
    configured = (os.environ.get("NPFL_DB_PATH") or "").strip()
    if configured:
        return configured
    return os.path.join(REPO_ROOT, DEFAULT_DB_NAME)


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with dict-like rows. Caller owns closing it."""
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create every table (and its primary key) if it does not exist yet."""
    for table, columns in SCHEMAS.items():
        keys = KEY_FIELDS[table]
        key_sql = ", ".join(f"{key} TEXT NOT NULL" for key in keys)
        key_names = ", ".join(keys)
        body = ", ".join(f"{column} TEXT" for column in columns if column not in keys)
        ddl = (
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"{key_sql}"
            + (f", {body}" if body else "")
            + f", PRIMARY KEY ({key_names}))"
        )
        conn.execute(ddl)
    conn.commit()


def ensure_schema(path: str | None = None) -> str:
    """Create the database file and its tables. Returns the path used."""
    target = path or db_path()
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect(target) as conn:
        init_db(conn)
    return target


# --- Writes ---------------------------------------------------------------------

def upsert_rows(
    table: str,
    records: Iterable[dict[str, Any]],
    *,
    key_fields: Sequence[str] | None = None,
) -> dict[str, int]:
    """Insert-or-update ``records`` into ``table``.

    Returns ``{"created": n, "updated": n, "skipped": n}``. A record whose key
    columns are not all present is skipped rather than guessed at.
    """
    _require_table(table)
    records = [record for record in records if record]
    stats = {"created": 0, "updated": 0, "skipped": 0}
    if not records:
        return stats

    columns = SCHEMAS[table]
    keys = list(key_fields or KEY_FIELDS[table])
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column not in keys)
    conflict = f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}" if updates else "DO NOTHING"
    insert_sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) {conflict}"
    )
    exists_sql = f"SELECT 1 FROM {table} WHERE {' AND '.join(f'{key} = ?' for key in keys)}"

    with connect() as conn:
        init_db(conn)
        for record in records:
            values = {column: _cell(record.get(column)) for column in columns}
            if not all(values[key].strip() for key in keys):
                stats["skipped"] += 1
                continue
            existed = conn.execute(exists_sql, [values[key] for key in keys]).fetchone()
            conn.execute(insert_sql, [values[column] for column in columns])
            stats["updated" if existed else "created"] += 1
    return stats


# --- Reads ----------------------------------------------------------------------

def read_records(table: str) -> list[dict[str, str]]:
    """Return every row of ``table`` as dicts of strings (empty string for NULL)."""
    _require_table(table)
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [
        {column: "" if row[column] is None else str(row[column]) for column in SCHEMAS[table]}
        for row in rows
    ]


def find_records(table: str, **equals: str) -> list[dict[str, str]]:
    """Filter ``read_records(table)`` by exact, case-insensitive column matches."""
    _require_table(table)
    wanted = {key: str(value).strip().lower() for key, value in equals.items()}
    out = []
    for record in read_records(table):
        if all(str(record.get(key, "")).strip().lower() == value for key, value in wanted.items()):
            out.append(record)
    return out


def count_rows(table: str) -> int:
    _require_table(table)
    with connect() as conn:
        init_db(conn)
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


# --- Helpers --------------------------------------------------------------------

def _require_table(table: str) -> None:
    if table not in SCHEMAS:
        raise ValueError(f"unknown table {table!r}; known: {', '.join(SCHEMAS)}")


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    return str(value)
