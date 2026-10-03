import contextlib
import logging
import os
import re
import time
from datetime import UTC, datetime

from markupsafe import Markup, escape

try:
    from zoneinfo import ZoneInfo
except (
    ImportError
):  # pragma: no cover - Python <3.9 not officially supported but handle gracefully
    ZoneInfo = None  # type: ignore

# Mapping of server types to their desired pastel background colours
_SERVER_TAG_COLOURS = {
    "emby": "#77DD77",  # soft green
    "plex": "#FDFD96",  # pale yellow
    "jellyfin": "#DAB1DA",  # light lavender
    "audiobookshelf": "#E9C9AA",  # peach
    "abs": "#E9C9AA",  # alias for Audiobookshelf (ABS)
    "romm": "#C3B1E1",  # light purple
}

_DEFAULT_COLOUR = "#E0E0E0"  # neutral grey fallback


def _resolve_local_timezone():
    """Determine the timezone to use for rendering timestamps."""
    tz_name = os.environ.get("TZ")

    if tz_name and hasattr(time, "tzset"):
        with contextlib.suppress(Exception):
            time.tzset()

    if ZoneInfo is not None:
        # First try explicit TZ environment variable
        if tz_name:
            try:
                return ZoneInfo(tz_name)
            except Exception as exc:
                logging.debug(f"Failed to load timezone {tz_name}: {exc}")

        # Fall back to system tzname if available
        try:
            local_name = time.tzname[0] if time.tzname else None
            if local_name:
                return ZoneInfo(local_name)
        except Exception as exc:
            logging.debug(f"Failed to load system timezone {local_name}: {exc}")

    # Fallback: use the system local timezone as determined by datetime
    try:
        return datetime.now().astimezone().tzinfo
    except Exception:
        return None


_LOCAL_TIMEZONE = _resolve_local_timezone()


def _local_tz():
    """The display timezone, read at call time so tests can patch the module."""
    return _LOCAL_TIMEZONE or datetime.now().astimezone().tzinfo


def to_local(value: datetime) -> datetime:
    """Return *value* in the display timezone; a naive value is taken as UTC.

    Every timestamp sauron stores is naive UTC, so this is the one place the
    panel turns it into the admin's wall clock.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_local_tz())


def parse_local_datetime(raw: str) -> datetime:
    """Parse a form value typed in the display timezone into an aware UTC datetime.

    A ``datetime-local`` input carries no offset: the admin typed their own
    wall clock. Converting before the value reaches the model matters, because
    SQLite's DateTime drops tzinfo without converting — an aware -06:00 value
    would be stored as local time and read back as UTC, six hours off.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_tz())
    return parsed.astimezone(UTC)


def _server_colour(server_type: str) -> str:
    """Return the hex colour for the given *server_type* or a default grey."""
    if not server_type:
        return _DEFAULT_COLOUR
    return _SERVER_TAG_COLOURS.get(server_type.lower(), _DEFAULT_COLOUR)


def server_type_tag(server_type: str) -> Markup:
    """Return a ready-to-use <span> tag for the *server_type* with consistent colours.

    The tag renders with Tailwind utility spacing classes so it still fits nicely
    in existing layouts, while the background colour is controlled inline to
    guarantee the exact palette requested by design.
    """
    colour = _server_colour(server_type)
    # Always use black text for better contrast on these pastel backgrounds.
    text = escape(server_type.title() if server_type else "Unknown")
    html = (
        f'<span class="text-xs inline-block font-medium px-2 py-0.5 rounded-lg" '
        f'style="background-color: {colour}; color: #000;">{text}</span>'
    )
    return Markup(html)  # noqa: S704  # User input is escaped, colour from safe dict


def server_name_tag(server_type: str, server_name: str) -> Markup:
    """Return a ready-to-use <span> tag with custom text but server_type colours.

    Uses the server_type for determining the background colour but displays
    the server_name as the text content.
    """
    colour = _server_colour(server_type)
    # Always use black text for better contrast on these pastel backgrounds.
    text = escape(server_name if server_name else "Unknown")
    html = (
        f'<span class="text-xs inline-block font-medium px-2 py-0.5 rounded-lg" '
        f'style="background-color: {colour}; color: #000;">{text}</span>'
    )
    return Markup(html)  # noqa: S704  # User input is escaped, colour from safe dict


def human_date(date_value) -> str:
    """Format a UTC timestamp as 'Jan 15, 2024 at 2:30 PM' in local time."""
    if not date_value:
        return "—"

    # Handle string datetime values (ISO format)
    if isinstance(date_value, str):
        try:
            # Parse common ISO formats
            for fmt in [
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
            ]:
                try:
                    date_value = datetime.strptime(date_value, fmt).replace(tzinfo=UTC)  # type: ignore
                    break
                except ValueError:
                    continue
            else:
                # If we can't parse it, just return the original truncated string
                return date_value[:16] if len(date_value) > 16 else date_value  # type: ignore
        except (ValueError, AttributeError):
            return date_value[:16] if len(date_value) > 16 else date_value  # type: ignore

    # Handle datetime objects
    if isinstance(date_value, datetime):
        date_value = to_local(date_value)
    if hasattr(date_value, "strftime"):
        return date_value.strftime("%b %-d, %Y at %-I:%M %p")

    # Fallback for unknown types
    return str(date_value)[:16]


def local_date(date_value, format_str="%m/%d %H:%M") -> str:
    """Convert UTC datetime to local timezone."""
    if not date_value:
        return "—"

    # Parse string to datetime if needed
    if isinstance(date_value, str):
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
            with contextlib.suppress(ValueError):
                date_value = datetime.strptime(date_value.rstrip("Z"), fmt).replace(
                    tzinfo=UTC
                )
                break
        else:
            return str(date_value)[:16]

    # Format datetime object
    if isinstance(date_value, datetime):
        return to_local(date_value).strftime(format_str)

    return str(date_value)


def nl2br(text: str) -> Markup:
    """Convert newlines to HTML <br> tags."""
    if not text:
        return Markup("")

    # Escape HTML to prevent XSS, then replace newlines with <br> tags
    escaped_text = escape(text)
    html = str(escaped_text).replace("\n", "<br>")
    return Markup(html)  # noqa: S704  # Text is escaped before markup conversion


_TRANSLATION_CALL_RE = re.compile(r"\{\{\s*_\(\s*(['\"])(?P<msg>.*?)\1\s*\)\s*\}\}")


def render_jinja(text: str) -> Markup:
    """Resolve ``{{ _('...') }}`` translation calls in database-stored text.

    Deliberately *not* a Jinja render. This text comes from the database —
    wizard step titles, which are importable from bundle files — so handing it
    to ``render_template_string`` would let a stored value evaluate arbitrary
    Jinja: ``{{ config }}`` leaks SECRET_KEY and ``__subclasses__`` traversal
    reaches code execution. Autoescaping does not help, because it guards the
    output, not the evaluation.

    Everything outside a translation call is escaped and emitted verbatim.
    """
    if not text:
        return Markup("")

    from flask_babel import gettext

    parts: list[str] = []
    cursor = 0
    for match in _TRANSLATION_CALL_RE.finditer(text):
        parts.append(str(escape(text[cursor : match.start()])))
        parts.append(str(escape(gettext(match.group("msg")))))
        cursor = match.end()
    parts.append(str(escape(text[cursor:])))

    return Markup("".join(parts))  # noqa: S704  # every part escaped above


def expiry_status(expires) -> str:
    """Jinja filter wrapper around app.services.expiry.get_expiry_status.

    Imported lazily to avoid a circular import at module load time.
    """
    from app.services.expiry import get_expiry_status

    return get_expiry_status(expires)


def register_filters(app):
    """Register the custom Jinja filters on the given Flask *app*."""
    app.jinja_env.filters.setdefault("server_type_tag", server_type_tag)
    app.jinja_env.filters.setdefault("server_name_tag", server_name_tag)
    app.jinja_env.filters.setdefault("server_colour", _server_colour)
    app.jinja_env.filters.setdefault("human_date", human_date)
    app.jinja_env.filters.setdefault("local_date", local_date)
    app.jinja_env.filters.setdefault("nl2br", nl2br)
    app.jinja_env.filters.setdefault("render_jinja", render_jinja)
    app.jinja_env.filters.setdefault("expiry_status", expiry_status)

    # Add Python built-in functions to Jinja globals
    app.jinja_env.globals.setdefault("max", max)
    app.jinja_env.globals.setdefault("min", min)
