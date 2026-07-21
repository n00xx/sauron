import json

from packaging.version import InvalidVersion
from packaging.version import parse as vparse


def _manifest() -> dict:
    """Get cached manifest data from database."""
    from app.models import Settings

    try:
        manifest_setting = Settings.query.filter_by(key="cached_manifest").first()
        if manifest_setting and manifest_setting.value:
            return json.loads(manifest_setting.value)
        return {}
    except Exception:
        # on failure, return empty manifest
        return {}


def check_update_available(current_version: str) -> bool:
    """True if a newer semantic version exists in the manifest."""
    latest = _manifest().get("latest_version")
    if not latest or current_version == "dev":
        return False  # can't compare
    try:
        return vparse(latest) > vparse(current_version)
    except InvalidVersion:
        # Fork builds use non-PEP 440 version strings (e.g.
        # "2026.7.1-sauron.<sha>") that vparse can't compare. Without this
        # guard the InvalidVersion propagates and 500s the /admin dashboard,
        # the only request-path caller. Treat unparseable versions as current.
        return False


def get_sponsors() -> list[dict]:
    """Returns list like [{'login': 'alice', 'url': '…', 'avatarUrl': '…'}, …]."""
    return _manifest().get("sponsors", [])


def get_manifest_last_fetch() -> str | None:
    """Get the timestamp of the last manifest fetch."""
    from app.models import Settings

    try:
        timestamp_setting = Settings.query.filter_by(key="manifest_last_fetch").first()
        return timestamp_setting.value if timestamp_setting else None
    except Exception:
        return None
