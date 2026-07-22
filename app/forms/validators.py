"""
Shared form validation constants and filters.
"""

import logging
from functools import lru_cache
from typing import Any

import dns.exception
import dns.resolver
from wtforms.validators import ValidationError

logger = logging.getLogger(__name__)

USERNAME_PATTERN = r"^[\w'.-]+$"
USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 15
USERNAME_LENGTH_MESSAGE = "Username must be 3 to 15 characters."
USERNAME_ALLOWED_CHARS_MESSAGE = (
    "Username can contain letters, numbers, dashes (-), underscores (_), "
    "apostrophes ('), and periods (.)."
)

# ─── Email domain existence validation ──────────────────────────────────────
EMAIL_DOMAIN_INVALID_MESSAGE = "Please enter a valid email address."

# Total DNS budget kept short so an unreachable nameserver can never stall
# account creation. NXDOMAIN answers are fast; a dead nameserver is the risk.
_DNS_TIMEOUT_SECONDS = 3.0
_DNS_RECORD_TYPES = ("MX", "A", "AAAA")


@lru_cache(maxsize=1)
def _get_resolver() -> dns.resolver.Resolver:
    """Build a shared, cached DNS resolver with a bounded time budget."""
    try:
        resolver = dns.resolver.Resolver()
    except dns.resolver.NoResolverConfiguration:
        # No /etc/resolv.conf (some sandboxes) — fall back to an unconfigured
        # resolver rather than blowing up at import time.
        resolver = dns.resolver.Resolver(configure=False)
    resolver.timeout = _DNS_TIMEOUT_SECONDS
    resolver.lifetime = _DNS_TIMEOUT_SECONDS
    resolver.cache = dns.resolver.LRUCache()
    return resolver


def _domain_has_dns_records(domain: str) -> bool:
    """Return whether an email domain resolves in DNS.

    Checks MX, then A, then AAAA. Returns ``True`` as soon as any record is
    found, and ``False`` only when the domain provably does not exist (NXDOMAIN)
    or has none of those records.

    Fails **open** (returns ``True``) on timeouts and nameserver/network errors
    so a transient DNS problem never blocks a legitimate signup — mirroring the
    "fail open when unreachable" behaviour used for Turnstile elsewhere.
    """
    resolver = _get_resolver()
    for record_type in _DNS_RECORD_TYPES:
        try:
            if len(resolver.resolve(domain, record_type)):
                return True
        except dns.resolver.NXDOMAIN:
            # Domain genuinely does not exist — no point trying other records.
            return False
        except dns.resolver.NoAnswer:
            # This record type is missing; try the next one.
            continue
        except dns.exception.DNSException as exc:
            # Timeout, no reachable nameserver, etc. — fail open.
            logger.warning("DNS lookup failed for %s (%s): %s", domain, record_type, exc)
            return True
    return False


def validate_email_domain_exists(_form, field) -> None:
    """WTForms validator rejecting emails whose domain does not resolve in DNS."""
    email = (field.data or "").strip()
    if "@" not in email:
        return  # Malformed input is the Email() validator's responsibility.
    domain = email.rsplit("@", 1)[1].strip().rstrip(".")
    if not domain:
        return
    if not _domain_has_dns_records(domain):
        raise ValidationError(EMAIL_DOMAIN_INVALID_MESSAGE)


def strip_filter(value: Any):
    """Trim leading/trailing whitespace from string inputs before validation."""
    if isinstance(value, str):
        return value.strip()
    return value
