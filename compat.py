"""
Local / Streamlit Cloud compatibility layer.

The apps need to run unchanged on Jo's Mac (where secrets live in files
under ~/.config/juno/claude-code/) AND on Streamlit Community Cloud
(where they live in st.secrets, populated via the Cloud UI).

These helpers prefer st.secrets when available and fall back to the
local file/env when not — so the same source works in both places.
"""
from __future__ import annotations

import os
from pathlib import Path

SLACK_TOKEN_LOCAL_PATH = Path.home() / '.config/juno/claude-code/slack-token'
GOOGLE_CREDS_LOCAL_PATH = Path.home() / '.config/juno/claude-code/google-credentials.json'
LOOKER_POSTGRES_ENV_VAR = 'STAFF_APP_LOOKER_POSTGRES_URL'


def _secrets():
    """Return st.secrets if Streamlit is loaded and has secrets; else None.

    Importing streamlit raises in environments without it; accessing
    st.secrets raises if no secrets file exists locally. Both are normal
    fallbacks — we just want to return None and let callers use the
    local path.
    """
    try:
        import streamlit as st
        # st.secrets behaves like a dict; accessing keys can raise if
        # there's no secrets.toml anywhere. Touch it cheaply first.
        _ = list(st.secrets.keys())  # noqa: F841 — just probing
        return st.secrets
    except Exception:
        return None


def get_secret_str(name: str) -> str | None:
    """Return st.secrets[name] as a string, or None."""
    s = _secrets()
    if s is None:
        return None
    val = s.get(name)
    return val if isinstance(val, str) else None


def get_slack_token() -> str:
    """Return the Slack bot/user token. Cloud: st.secrets['SLACK_TOKEN'].
    Local: ~/.config/juno/claude-code/slack-token."""
    tok = get_secret_str('SLACK_TOKEN')
    if tok:
        return tok.strip()
    if SLACK_TOKEN_LOCAL_PATH.exists():
        return SLACK_TOKEN_LOCAL_PATH.read_text().strip()
    raise RuntimeError(
        "No Slack token found — set st.secrets['SLACK_TOKEN'] (cloud) "
        f"or create {SLACK_TOKEN_LOCAL_PATH} (local)."
    )


def get_postgres_url() -> str:
    """Return the Looker Postgres URL. Cloud: st.secrets['LOOKER_POSTGRES_URL'].
    Local: STAFF_APP_LOOKER_POSTGRES_URL env var."""
    url = get_secret_str('LOOKER_POSTGRES_URL')
    if url:
        return url
    env = os.environ.get(LOOKER_POSTGRES_ENV_VAR)
    if env:
        return env
    raise RuntimeError(
        "No Looker Postgres URL found — set st.secrets['LOOKER_POSTGRES_URL'] (cloud) "
        f"or {LOOKER_POSTGRES_ENV_VAR} env var (local)."
    )


def get_google_credentials():
    """Return google.oauth2.credentials.Credentials for OAuth user creds.

    Cloud: st.secrets['GOOGLE_SERVICE_ACCOUNT'] — the JSON content
           of the authorised-user-credentials file, pasted as a TOML
           multi-line string OR a TOML table.
    Local: ~/.config/juno/claude-code/google-credentials.json.

    Despite the secret name being 'GOOGLE_SERVICE_ACCOUNT' (chosen for
    historical reasons), this is actually OAuth user credentials with a
    refresh token — they renew themselves without browser interaction.
    """
    from google.oauth2.credentials import Credentials

    s = _secrets()
    if s is not None and 'GOOGLE_SERVICE_ACCOUNT' in s:
        import json
        raw = s['GOOGLE_SERVICE_ACCOUNT']
        # Accept either a TOML string (JSON content) or a TOML table (already dict)
        if isinstance(raw, str):
            info = json.loads(raw)
        elif hasattr(raw, 'to_dict'):
            info = raw.to_dict()
        else:
            info = dict(raw)
        return Credentials.from_authorized_user_info(info)

    if GOOGLE_CREDS_LOCAL_PATH.exists():
        return Credentials.from_authorized_user_file(str(GOOGLE_CREDS_LOCAL_PATH))

    raise RuntimeError(
        "No Google credentials found — set st.secrets['GOOGLE_SERVICE_ACCOUNT'] (cloud) "
        f"or create {GOOGLE_CREDS_LOCAL_PATH} (local)."
    )


def running_on_cloud() -> bool:
    """True if Streamlit secrets are loaded — i.e. we're on Streamlit Cloud
    (or any environment where st.secrets is set up)."""
    return _secrets() is not None
