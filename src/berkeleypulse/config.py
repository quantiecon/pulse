from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv

ENV_MAP = {
    "PULSE_CANVAS_BASE_URL": "canvas_base_url",
    "PULSE_CANVAS_TOKEN": "canvas_token",
    "PULSE_IMAP_HOST": "imap_host",
    "PULSE_IMAP_PORT": "imap_port",
    "PULSE_IMAP_USER": "imap_user",
    "PULSE_IMAP_PASSWORD": "imap_password",
    "PULSE_IMAP_FOLDER": "imap_folder",
    "PULSE_WORK_STYLE": "work_style",
    "PULSE_SESSION_MINUTES": "session_minutes",
    "PULSE_POLL_MINUTES": "poll_minutes",
    "PULSE_LLM_BASE_URL": "llm_base_url",
    "PULSE_LLM_API_KEY": "llm_api_key",
    "PULSE_LLM_MODEL": "llm_model",
    "PULSE_TIMEZONE": "timezone",
}


def project_root() -> Path:
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "berkeleypulse").exists():
            return candidate
        if candidate == candidate.parent:
            break
    return cwd


def data_dir() -> Path:
    override = os.environ.get("PULSE_DATA_DIR", "").strip()
    preferred = Path(override) if override else project_root() / "data"
    if _prepare_dir(preferred):
        return preferred
    # Vercel’s deployment filesystem is read-only outside /tmp. An explicit
    # PULSE_DATA_DIR still has to succeed on its own.
    if override:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    fallback = Path(os.environ.get("TMPDIR") or "/tmp") / "pulse"
    fallback.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(fallback, 0o700)
    except OSError:
        pass
    return fallback


def _prepare_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            return False
        os.chmod(path, 0o700)
    except OSError:
        return False
    return True


@dataclass
class Settings:
    canvas_base_url: str = "https://bcourses.berkeley.edu"
    canvas_token: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    work_style: str = "spacer"
    session_minutes: int = 50
    poll_minutes: int = 15
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    timezone: str = "America/Los_Angeles"

    @property
    def canvas_ready(self) -> bool:
        return bool(self.canvas_token.strip())

    @property
    def mail_ready(self) -> bool:
        return bool(self.imap_user.strip() and self.imap_password.strip())

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_api_key.strip())

    @property
    def parser_tag(self) -> str:
        if self.llm_ready:
            return "llm:" + self.llm_model
        return "heuristic"


def _field_names() -> set:
    return {item.name for item in dataclasses.fields(Settings)}


def locked_fields() -> Dict[str, str]:
    """Environment variables that override the settings file."""
    _load_dotenv()
    found = {}
    for env_key, field in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            found[field] = env_key
    return found


def posthog_public() -> dict:
    """Browser analytics config. Empty until PULSE_POSTHOG_KEY is set."""
    _load_dotenv()
    key = os.environ.get("PULSE_POSTHOG_KEY", "").strip()
    # phc_ is the public project key. phs_ is a server secret and must stay off the page.
    if not key.startswith("phc_"):
        return {}
    host = os.environ.get("PULSE_POSTHOG_HOST", "https://us.i.posthog.com").strip()
    if not host.startswith("https://"):
        host = "https://us.i.posthog.com"
    return {"key": key, "host": host.rstrip("/")}


def _load_dotenv() -> None:
    env_path = project_root() / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _coerce(field: str, value):
    if field in {"imap_port", "session_minutes", "poll_minutes"}:
        return int(value)
    return value


def load_settings() -> Settings:
    _load_dotenv()
    raw = {}
    path = data_dir() / "config.json"
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                raw = loaded
        except json.JSONDecodeError:
            raw = {}
    names = _field_names()
    clean = {}
    for key, value in raw.items():
        if key not in names:
            continue
        try:
            clean[key] = _coerce(key, value)
        except (TypeError, ValueError):
            continue
    settings = Settings(**clean)
    for env_key, field in ENV_MAP.items():
        value = os.environ.get(env_key)
        if not value:
            continue
        try:
            setattr(settings, field, _coerce(field, value))
        except (TypeError, ValueError):
            continue
    if settings.work_style not in {"spacer", "crammer"}:
        settings.work_style = "spacer"
    settings.session_minutes = min(180, max(25, int(settings.session_minutes)))
    settings.poll_minutes = min(180, max(5, int(settings.poll_minutes)))
    settings.canvas_base_url = settings.canvas_base_url.rstrip("/") or "https://bcourses.berkeley.edu"
    if "gmail" in settings.imap_host:
        settings.imap_password = settings.imap_password.replace(" ", "")
    return settings


def save_settings(settings: Settings) -> None:
    path = data_dir() / "config.json"
    payload = dataclasses.asdict(settings)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def update_settings(**changes) -> Settings:
    current = load_settings()
    locked = set(locked_fields())
    for key, value in changes.items():
        if key in locked or not hasattr(current, key):
            continue
        setattr(current, key, value)
    if current.work_style not in {"spacer", "crammer"}:
        current.work_style = "spacer"
    current.session_minutes = min(180, max(25, int(current.session_minutes)))
    current.poll_minutes = min(180, max(5, int(current.poll_minutes)))
    save_settings(current)
    return load_settings()
