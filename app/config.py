# config.py
import json
import os
import secrets
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

from app.utils.session_cache import RobustFileSystemCache

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Initialize session cache at module level so Flask-Session can access it

# Ensure database directory exists
# Use /data/database for container deployments, fall back to local for development


if Path("/data").exists():
    DATABASE_DIR = Path("/data/database")
else:
    DATABASE_DIR = BASE_DIR / "database"
DATABASE_DIR.mkdir(exist_ok=True)

SESSION_CACHELIB = RobustFileSystemCache(
    str(DATABASE_DIR / "sessions"),
    threshold=1000,  # Max files before cleanup
    default_timeout=86400,  # 24 hours
    mode=0o600,  # Restrict file permissions
)

# Define secrets file location next to database
SECRETS_FILE = DATABASE_DIR / "secrets.json"


def generate_secret_key():
    """Generate a secure random secret key."""
    return secrets.token_hex(32)


def load_secrets():
    """Load secrets from the secrets file."""
    if not SECRETS_FILE.exists():
        return {}

    try:
        with SECRETS_FILE.open() as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_secrets(secrets_dict):
    """Save secrets to the secrets file, readable only by the owner.

    This file holds SECRET_KEY, which signs session cookies. Writing it with
    the default umask left it world-readable (0644) next to the database.
    """
    # Ensure database directory exists
    DATABASE_DIR.mkdir(exist_ok=True)

    fd = os.open(SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(secrets_dict, f, indent=2)

    # os.open honours the umask, so tighten explicitly for pre-existing files.
    SECRETS_FILE.chmod(0o600)


def get_or_create_secret(key, generator_func):
    """Get a secret from the secrets file or create it if it doesn't exist."""
    secrets_dict = load_secrets()

    if key not in secrets_dict:
        secrets_dict[key] = generator_func()
        save_secrets(secrets_dict)

    return secrets_dict[key]


class BaseConfig:
    # Flask
    TEMPLATES_AUTO_RELOAD = True
    SECRET_KEY = get_or_create_secret("SECRET_KEY", generate_secret_key)
    # Rate limiting (Flask-Limiter reads this key). Must stay on: the
    # @limiter.limit decorators guarding /login and the invite endpoints are
    # inert without it.
    RATELIMIT_ENABLED = True
    # Sessions
    SESSION_TYPE = "cachelib"  # Changed from 'filesystem' to 'cachelib'
    SESSION_CACHELIB = SESSION_CACHELIB  # Reference the module-level cache

    # Babel / i18n
    LANGUAGES: ClassVar[dict[str, str]] = {
        "en": "English",
        "ca": "Catalan",
        "cs": "Czech",
        "da": "Danish",
        "de": "German",
        "es": "Spanish",
        "es_MX": "Spanish (Mexico)",
        "fa": "Persian",
        "fr": "French",
        "gsw": "Swiss German",
        "he": "Hebrew",
        "hr": "Croatian",
        "hu": "Hungarian",
        "is": "Icelandic",
        "it": "Italian",
        "lt": "Lithuanian",
        "nb_NO": "Norwegian Bokmål",
        "nl": "Dutch",
        "pl": "Polish",
        "pt": "Portuguese",
        "pt_BR": "Portuguese (Brazil)",
        "ro": "Romanian",
        "ru": "Russian",
        "sv": "Swedish",
        "zh_Hans": "Chinese (Simplified)",
        "zh_Hant": "Chinese (Traditional)",
    }
    BABEL_DEFAULT_LOCALE = "en"
    BABEL_TRANSLATION_DIRECTORIES = str(BASE_DIR / "app" / "translations")
    # Allow forcing a specific language via environment variable
    FORCE_LANGUAGE = os.getenv("FORCE_LANGUAGE")
    # Scheduler. The REST API stays off: extensions.py also disables it before
    # init_app, but a dangerous default here would expose job management if
    # that initialisation order ever changed.
    SCHEDULER_API_ENABLED = False
    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{DATABASE_DIR / 'database.db'}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # SQLite engine options for concurrent write support
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict] = {
        "connect_args": {
            "timeout": 30,  # 30 second timeout for lock waits
            "check_same_thread": False,  # Allow multi-threaded access
        },
        "pool_pre_ping": True,  # Verify connections before using
        "pool_recycle": 3600,  # Recycle connections after 1 hour
    }


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean from the environment, keeping *default* when unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    # Local development is plain HTTP, so a Secure cookie would never be sent.
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"


class ProductionConfig(BaseConfig):
    DEBUG = False
    # Cookie hardening. SameSite=Lax is defence in depth behind CSRFProtect;
    # Secure can be turned off for LAN deployments that terminate no TLS, but
    # it defaults to on so the safe case needs no configuration.
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", True)
    REMEMBER_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", True)
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SAMESITE = "Lax"
