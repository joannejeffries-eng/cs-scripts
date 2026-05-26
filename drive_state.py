"""
Drive-backed JSON state store.

Streamlit Cloud's filesystem is ephemeral — anything written under
~/.claude/scheduled-tasks/ is lost on every redeploy or restart. To keep
TL submissions, lunch overrides, scheduled messages etc. safe, the apps
persist state to a dedicated folder in Google Drive when running on
the cloud.

When running locally on Jo's Mac, state files keep going to the
existing ~/.claude/scheduled-tasks/ paths — nothing changes.

Folder name on Drive: 'cs-scripts-state'. Created automatically the
first time anything is saved.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from compat import get_google_credentials, running_on_cloud

DRIVE_FOLDER_NAME = 'cs-scripts-state'
_FOLDER_ID_CACHE = {'id': None}


def _drive_service():
    """Build a Drive v3 client using our shared OAuth credentials."""
    from googleapiclient.discovery import build
    creds = get_google_credentials()
    return build('drive', 'v3', credentials=creds)


def _get_or_create_folder() -> str:
    """Return the Drive folder ID for cs-scripts-state, creating if absent."""
    if _FOLDER_ID_CACHE['id']:
        return _FOLDER_ID_CACHE['id']
    svc = _drive_service()
    # Search for an existing folder with this name owned by us
    q = (f"name = '{DRIVE_FOLDER_NAME}' and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    resp = svc.files().list(q=q, fields='files(id, name)', pageSize=10).execute()
    for f in resp.get('files', []):
        _FOLDER_ID_CACHE['id'] = f['id']
        return f['id']
    # Create it
    body = {'name': DRIVE_FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'}
    created = svc.files().create(body=body, fields='id').execute()
    _FOLDER_ID_CACHE['id'] = created['id']
    return created['id']


def _find_file(filename: str) -> str | None:
    """Return the Drive file ID for filename in our folder, or None."""
    svc = _drive_service()
    folder_id = _get_or_create_folder()
    q = (f"name = '{filename}' and '{folder_id}' in parents and trashed = false")
    resp = svc.files().list(q=q, fields='files(id)', pageSize=1).execute()
    files = resp.get('files', [])
    return files[0]['id'] if files else None


def drive_read_json(filename: str):
    """Read a JSON file by name from the cs-scripts-state folder.
    Returns the parsed object, or None if the file doesn't exist."""
    file_id = _find_file(filename)
    if not file_id:
        return None
    from googleapiclient.http import MediaIoBaseDownload
    svc = _drive_service()
    request = svc.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return json.loads(buf.read().decode('utf-8'))


def drive_write_json(filename: str, data) -> str:
    """Write data as JSON to filename inside the cs-scripts-state folder.
    Creates the file if it doesn't exist; overwrites otherwise.
    Returns the Drive file ID."""
    from googleapiclient.http import MediaIoBaseUpload
    svc = _drive_service()
    folder_id = _get_or_create_folder()
    payload = json.dumps(data, indent=2).encode('utf-8')
    media = MediaIoBaseUpload(io.BytesIO(payload), mimetype='application/json',
                               resumable=False)
    file_id = _find_file(filename)
    if file_id:
        svc.files().update(fileId=file_id, media_body=media).execute()
        return file_id
    body = {'name': filename, 'parents': [folder_id]}
    created = svc.files().create(body=body, media_body=media, fields='id').execute()
    return created['id']


def drive_list_files(prefix: str = '') -> list[str]:
    """Return file names in the cs-scripts-state folder, optionally filtered
    by prefix (e.g. 'week_')."""
    svc = _drive_service()
    folder_id = _get_or_create_folder()
    q = f"'{folder_id}' in parents and trashed = false"
    names = []
    page_token = None
    while True:
        resp = svc.files().list(
            q=q, fields='nextPageToken, files(name)',
            pageSize=100, pageToken=page_token,
        ).execute()
        for f in resp.get('files', []):
            n = f['name']
            if not prefix or n.startswith(prefix):
                names.append(n)
        page_token = resp.get('nextPageToken')
        if not page_token:
            break
    return sorted(names)


# ── Hybrid local-or-Drive read/write ────────────────────────────────────────
# Callers pass a local Path AND a Drive filename. We auto-route based on
# whether we're on cloud.

def read_json_either(local_path: Path, drive_filename: str):
    """Read JSON from Drive when on cloud; from local_path otherwise.
    Returns parsed object or None if not found."""
    if running_on_cloud():
        return drive_read_json(drive_filename)
    if not local_path.exists():
        return None
    return json.loads(local_path.read_text())


def write_json_either(local_path: Path, drive_filename: str, data) -> None:
    """Write JSON to both local FS and Drive when running locally;
    Drive only when on cloud (no useful local FS — it's ephemeral).

    The dual-write matters because the *cloud* rota app can only see
    state that's been pushed to Drive. The role-change daemon and the
    local Streamlit app both run on Jo's Mac, but their state has to
    reach the cloud somehow — and Drive is the bus we use.

    Drive write failures don't fail the call — the local copy stays
    correct for the local app. The cloud-side reader will see stale
    state until the next successful sync.
    """
    if running_on_cloud():
        drive_write_json(drive_filename, data)
        return
    # Local: write to both. Local first so the local app is never blocked
    # on a Drive hiccup.
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(json.dumps(data, indent=2))
    try:
        drive_write_json(drive_filename, data)
    except Exception as e:
        import logging
        logging.warning(f"Drive write failed for {drive_filename}: {e}. "
                         f"Local copy is fine; cloud will be stale.")


def list_files_either(local_dir: Path, drive_prefix: str) -> list[str]:
    """List file names matching prefix in Drive when on cloud, or filenames
    in local_dir matching the same prefix otherwise. Returns just the
    basenames, no path."""
    if running_on_cloud():
        return drive_list_files(drive_prefix)
    if not local_dir.exists():
        return []
    return sorted(p.name for p in local_dir.iterdir() if p.name.startswith(drive_prefix))
