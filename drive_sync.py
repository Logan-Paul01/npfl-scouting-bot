"""Optional helper: mirror AGENTS.md into Google Drive.

Runs only when Google credentials are available; otherwise it logs a skip and
exits 0 so it can never break ingestion in CI.

Usage:
    python drive_sync.py --file AGENTS.md [--folder-id <DRIVE_FOLDER_ID>]
"""

from __future__ import annotations

import argparse
import os
import sys

from sheets import load_credentials

MIME_MARKDOWN = "text/markdown"
MIME_FOLDER = "application/vnd.google-apps.folder"


def _drive_service():
    from googleapiclient.discovery import build

    creds = load_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_files(service, name: str, folder_id: str | None) -> list[dict]:
    query = f"name = '{name}' and trashed = false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    result = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name, modifiedTime)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return result.get("files", [])


def sync_agents_md(
    path: str = "AGENTS.md",
    folder_id: str | None = None,
    name: str | None = None,
) -> dict:
    """Upload ``path`` to Drive, updating an existing file of the same name.

    Returns a dict describing the action (``created`` / ``updated`` / ``skipped``).
    """
    folder_id = folder_id or (os.environ.get("DRIVE_FOLDER_ID") or "").strip() or None
    name = name or os.path.basename(path)

    if not os.path.exists(path):
        return {"action": "skipped", "reason": f"{path} not found"}

    from googleapiclient.http import MediaFileUpload

    service = _drive_service()
    media = MediaFileUpload(path, mimetype=MIME_MARKDOWN, resumable=False)
    existing = _find_files(service, name, folder_id)

    if existing:
        target = existing[0]["id"]
        updated = service.files().update(
            fileId=target,
            media_body=media,
            fields="id, name, modifiedTime, webViewLink",
            supportsAllDrives=True,
        ).execute()
        return {"action": "updated", "name": updated.get("name"), "id": updated.get("id"),
                "link": updated.get("webViewLink")}

    body = {"name": name, "mimeType": MIME_MARKDOWN}
    if folder_id:
        body["parents"] = [folder_id]
    created = service.files().create(
        body=body,
        media_body=media,
        fields="id, name, modifiedTime, webViewLink",
        supportsAllDrives=True,
    ).execute()
    return {"action": "created", "name": created.get("name"), "id": created.get("id"),
            "link": created.get("webViewLink")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror the project manual to Google Drive.")
    parser.add_argument("--file", default="AGENTS.md", help="local file to mirror (default: AGENTS.md)")
    parser.add_argument("--folder-id", default=None, help="Drive folder id (default: DRIVE_FOLDER_ID)")
    parser.add_argument("--name", default=None, help="Drive file name (default: basename of --file)")
    args = parser.parse_args(argv)

    try:
        result = sync_agents_md(args.file, args.folder_id, args.name)
    except Exception as exc:  # credentials missing or API error: never fatal
        print(f"[drive_sync] skipped: {exc}")
        return 0

    print(f"[drive_sync] {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
