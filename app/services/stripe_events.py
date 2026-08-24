"""Mirror Stripe events into sauron by polling the Events API.

sauron is deliberately NOT a Stripe webhook endpoint. The webhook belongs to the
storefront (neexy), which owns fulfillment; putting sauron on Stripe's delivery
path would mean a second endpoint secret, a public route on a self-hosted box,
and sauron's uptime showing up as "endpoint failing" in someone else's Stripe
dashboard. Polling ``GET /v1/events`` instead:

  * needs no change to the storefront and no new endpoint here,
  * is not limited by a webhook's ``enabled_events`` list, so every monitored
    type arrives without reconfiguring anything in Stripe,
  * and backfills up to Stripe's 30-day retention on the very first run, so the
    tab is populated immediately rather than starting empty.

Latency is minutes, which is irrelevant here: dispute and early-fraud-warning
response windows are measured in days.

The API key is expected to be a RESTRICTED, READ-ONLY key. Nothing in this
module writes to Stripe.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
import structlog

from app.extensions import db
from app.models import Settings, StripeEvent

logger = structlog.get_logger(__name__)

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Per-request timeout and page size. Stripe caps `limit` at 100.
REQUEST_TIMEOUT = 20
PAGE_SIZE = 100
# Hard stop so a misconfiguration can never spin forever on a paginated list.
MAX_PAGES = 50

# How far back the first ever sync reaches. Stripe retains events for 30 days;
# asking for more just returns nothing.
INITIAL_LOOKBACK_DAYS = 30
# Re-scan window on every incremental run. Events can be created slightly out of
# order relative to when they become listable, so re-reading a little history is
# what keeps a gap from forming. Re-reads are free: the unique constraint on
# stripe_event_id turns them into no-ops.
OVERLAP_MINUTES = 15


# --------------------------------------------------------------------------
# What we monitor
# --------------------------------------------------------------------------

# Curated on purpose: the storefront sells one-off Checkout Sessions, so this is
# the set that can actually fire AND carries signal for dispute defence.
#
# Deliberately absent:
#   * invoice.* and customer.subscription.*  — no Invoices or Subscriptions in
#     this integration; they would never fire.
#   * checkout.session.async_payment_*       — OXXO and other async methods are
#     discarded; the integration is card-only.
MONITORED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        # Checkout
        "checkout.session.completed",
        "checkout.session.expired",
        # Payments
        "payment_intent.succeeded",
        "payment_intent.payment_failed",
        "charge.succeeded",
        "charge.failed",
        # Refunds
        "charge.refunded",
        "refund.created",
        "refund.updated",
        "refund.failed",
        # Disputes
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
        "charge.dispute.funds_withdrawn",
        "charge.dispute.funds_reinstated",
        # Fraud signals — early_fraud_warning is the dispute-deflection
        # primitive: refund inside the window and the chargeback never happens.
        "radar.early_fraud_warning.created",
        "radar.early_fraud_warning.updated",
        "review.opened",
        "review.closed",
    }
)

CATEGORY_CHECKOUT = "checkout"
CATEGORY_PAYMENT = "payment"
CATEGORY_REFUND = "refund"
CATEGORY_DISPUTE = "dispute"
CATEGORY_FRAUD = "fraud"
CATEGORY_OTHER = "other"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_CRITICAL = "critical"

# Events that mean money is leaving or an account is under formal attack.
_CRITICAL_TYPES = frozenset(
    {
        "charge.dispute.created",
        "charge.dispute.funds_withdrawn",
        "radar.early_fraud_warning.created",
    }
)
_ERROR_TYPES = frozenset(
    {
        "payment_intent.payment_failed",
        "charge.failed",
        "refund.failed",
    }
)
_WARNING_TYPES = frozenset(
    {
        "charge.dispute.updated",
        "charge.refunded",
        "review.opened",
        "checkout.session.expired",
        "radar.early_fraud_warning.updated",
    }
)


def categorize(event_type: str) -> str:
    """Coarse bucket for an event type, so the UI groups without a type table."""
    if event_type.startswith("checkout.session."):
        return CATEGORY_CHECKOUT
    if event_type.startswith("charge.dispute."):
        return CATEGORY_DISPUTE
    if event_type.startswith(("radar.", "review.")):
        return CATEGORY_FRAUD
    if event_type.startswith("refund.") or event_type == "charge.refunded":
        return CATEGORY_REFUND
    if event_type.startswith(("payment_intent.", "charge.")):
        return CATEGORY_PAYMENT
    return CATEGORY_OTHER


def severity_for(event_type: str, obj: dict[str, Any]) -> str:
    """Map an event to a severity band.

    ``charge.dispute.closed`` is the one type whose severity depends on the
    payload: a dispute closed as ``won`` is good news, ``lost`` is not.
    """
    if event_type == "charge.dispute.closed":
        status = (obj.get("status") or "").lower()
        if status == "won":
            return SEVERITY_INFO
        if status in {"lost", "warning_closed"}:
            return SEVERITY_ERROR
        return SEVERITY_WARNING
    if event_type in _CRITICAL_TYPES:
        return SEVERITY_CRITICAL
    if event_type in _ERROR_TYPES:
        return SEVERITY_ERROR
    if event_type in _WARNING_TYPES:
        return SEVERITY_WARNING
    return SEVERITY_INFO


# --------------------------------------------------------------------------
# Payload extraction
# --------------------------------------------------------------------------


def _as_id(value: Any) -> str | None:
    """Return an object id whether the field is expanded or a bare string."""
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        raw = value.get("id")
        return raw if isinstance(raw, str) and raw else None
    return None


def _ts_to_dt(value: Any) -> datetime | None:
    """Unix seconds → aware UTC datetime, tolerating junk."""
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _extract_email(obj: dict[str, Any]) -> str | None:
    """Find the buyer's email wherever this object type happens to keep it."""
    candidates = [
        obj.get("customer_email"),
        (obj.get("customer_details") or {}).get("email")
        if isinstance(obj.get("customer_details"), dict)
        else None,
        (obj.get("billing_details") or {}).get("email")
        if isinstance(obj.get("billing_details"), dict)
        else None,
        obj.get("receipt_email"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().lower()
    return None


def _extract_charge_id(event_type: str, obj: dict[str, Any]) -> str | None:
    """Charge id, from whichever field this object type uses."""
    # Disputes, refunds and EFWs all point at a charge; a charge points at itself.
    if event_type.startswith("charge.") and not event_type.startswith(
        "charge.dispute."
    ):
        return _as_id(obj.get("id"))
    return _as_id(obj.get("charge"))


def _extract_error(obj: dict[str, Any]) -> tuple[str | None, str | None]:
    """(code, message) for a failure, from either shape Stripe uses."""
    error = obj.get("last_payment_error")
    if not isinstance(error, dict):
        error = obj.get("failure_reason")
        if isinstance(error, str):
            return error, obj.get("failure_message") if isinstance(
                obj.get("failure_message"), str
            ) else None
        # Charges expose flat failure_code/failure_message.
        code = obj.get("failure_code")
        message = obj.get("failure_message")
        return (
            code if isinstance(code, str) else None,
            message if isinstance(message, str) else None,
        )
    code = error.get("code") or error.get("decline_code")
    message = error.get("message")
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


def _extract_amount(obj: dict[str, Any]) -> int | None:
    """Amount in the smallest currency unit, per object type."""
    for key in ("amount_total", "amount", "amount_refunded"):
        value = obj.get(key)
        if isinstance(value, int):
            return value
    return None


def extract_fields(event: dict[str, Any]) -> dict[str, Any]:
    """Denormalise a raw Stripe event into StripeEvent column values.

    Total function: never raises on a malformed event. Anything it cannot read
    comes back as ``None`` and the raw payload is stored regardless, so a shape
    change in Stripe degrades a column rather than dropping the event.
    """
    event_type = str(event.get("type") or "")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        obj = {}

    created = _ts_to_dt(event.get("created")) or datetime.now(UTC)
    error_code, error_message = _extract_error(obj)

    # `payment_intent` is the spine linking checkout → charge → refund →
    # dispute. A PaymentIntent object carries its own id in `id`, not in a
    # `payment_intent` field.
    if event_type.startswith("payment_intent."):
        payment_intent_id = _as_id(obj.get("id"))
    else:
        payment_intent_id = _as_id(obj.get("payment_intent"))

    dispute_reason = None
    dispute_due_by = None
    network_reason_code = None
    if event_type.startswith("charge.dispute."):
        reason = obj.get("reason")
        dispute_reason = reason if isinstance(reason, str) else None
        evidence_details = obj.get("evidence_details")
        if isinstance(evidence_details, dict):
            dispute_due_by = _ts_to_dt(evidence_details.get("due_by"))
        pmd = obj.get("payment_method_details")
        if isinstance(pmd, dict) and isinstance(pmd.get("card"), dict):
            code = pmd["card"].get("network_reason_code")
            network_reason_code = code if isinstance(code, str) else None
    elif event_type.startswith("radar.early_fraud_warning."):
        reason = obj.get("fraud_type")
        dispute_reason = reason if isinstance(reason, str) else None

    status = obj.get("status")
    if not isinstance(status, str):
        # Checkout sessions carry the useful state in payment_status.
        status = obj.get("payment_status")
    currency = obj.get("currency")

    return {
        "stripe_event_id": str(event.get("id") or ""),
        "type": event_type,
        "category": categorize(event_type),
        "severity": severity_for(event_type, obj),
        "created_at_stripe": created,
        "livemode": bool(event.get("livemode")),
        "api_version": event.get("api_version")
        if isinstance(event.get("api_version"), str)
        else None,
        "object_id": _as_id(obj.get("id")),
        "payment_intent_id": payment_intent_id,
        "charge_id": _extract_charge_id(event_type, obj),
        "customer_email": _extract_email(obj),
        "amount": _extract_amount(obj),
        "currency": currency.lower() if isinstance(currency, str) else None,
        "status": status if isinstance(status, str) else None,
        "error_code": error_code,
        "error_message": error_message,
        "dispute_reason": dispute_reason,
        "dispute_due_by": dispute_due_by,
        "network_reason_code": network_reason_code,
        "payload": json.dumps(event, ensure_ascii=False),
    }


# --------------------------------------------------------------------------
# Settings access
# --------------------------------------------------------------------------


def get_setting(key: str, default: str | None = None) -> str | None:
    row = Settings.query.filter_by(key=key).first()
    if row is None or row.value is None or row.value == "":
        return default
    return row.value


def set_setting(key: str, value: str | None) -> None:
    row = Settings.query.filter_by(key=key).first()
    if row is None:
        db.session.add(Settings(key=key, value=value))
    else:
        row.value = value


def is_sync_enabled() -> bool:
    """Sync runs only when explicitly enabled AND a key is present."""
    return (
        bool(get_setting("stripe_api_key"))
        and get_setting("stripe_sync_enabled", "false") == "true"
    )


def get_sync_interval_minutes(default: int = 15) -> int:
    raw = get_setting("stripe_sync_interval_minutes")
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    # Floor at 1 minute so a bad value cannot hammer Stripe.
    return max(1, value)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class StripeApiError(RuntimeError):
    """A Stripe API call failed. ``retryable`` mirrors the storefront's model."""

    def __init__(self, message: str, status: int | None, retryable: bool):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


def fetch_events(
    api_key: str,
    created_gte: datetime,
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """List raw Stripe events created at or after ``created_gte``, newest first.

    No server-side ``types[]`` filter: that parameter has an undocumented cap on
    the number of values, and silently truncating the monitored set would lose
    events with no error to notice. Filtering happens locally in
    :func:`sync_stripe_events`, where the volume is trivially small.
    """
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "Stripe-Version": "2024-06-20",
        }
    )

    collected: list[dict[str, Any]] = []
    starting_after: str | None = None

    for _ in range(max_pages):
        params: dict[str, Any] = {
            "limit": page_size,
            "created[gte]": int(created_gte.timestamp()),
        }
        if starting_after:
            params["starting_after"] = starting_after

        try:
            response = session.get(
                f"{STRIPE_API_BASE}/events", params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException as exc:
            raise StripeApiError(f"Stripe request failed: {exc}", None, True) from exc

        if response.status_code == 401:
            raise StripeApiError(
                "Stripe rejected the API key (401). Check that the restricted "
                "key is valid and has read access to Events.",
                401,
                False,
            )
        if response.status_code == 429:
            raise StripeApiError("Stripe rate limit reached (429).", 429, True)
        if response.status_code >= 500:
            raise StripeApiError(
                f"Stripe server error ({response.status_code}).",
                response.status_code,
                True,
            )
        if response.status_code >= 400:
            raise StripeApiError(
                f"Stripe rejected the request ({response.status_code}): "
                f"{response.text[:200]}",
                response.status_code,
                False,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise StripeApiError("Stripe returned invalid JSON.", None, True) from exc

        page = body.get("data")
        if not isinstance(page, list) or not page:
            break

        collected.extend(item for item in page if isinstance(item, dict))

        if not body.get("has_more"):
            break
        last = page[-1]
        starting_after = last.get("id") if isinstance(last, dict) else None
        if not starting_after:
            break

    return collected


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def _sync_window_start() -> datetime:
    """Where this run starts reading from.

    First run reaches back the full retention window so the tab is populated
    immediately; later runs re-read a short overlap so no event slips through a
    boundary.
    """
    last = get_setting("stripe_last_sync_at")
    if not last:
        return datetime.now(UTC) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    try:
        parsed = datetime.fromisoformat(last)
    except ValueError:
        return datetime.now(UTC) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed - timedelta(minutes=OVERLAP_MINUTES)


def sync_stripe_events(*, force: bool = False) -> dict[str, Any]:
    """Pull new Stripe events into the local archive.

    Returns a summary dict. Must be called inside an app context.
    """
    if not force and not is_sync_enabled():
        return {"skipped": True, "reason": "disabled"}

    api_key = get_setting("stripe_api_key")
    if not api_key:
        return {"skipped": True, "reason": "no_api_key"}

    started_at = datetime.now(UTC)
    window_start = _sync_window_start()

    try:
        raw_events = fetch_events(api_key, window_start)
    except StripeApiError as exc:
        logger.warning(
            "Stripe event sync failed", error=str(exc), retryable=exc.retryable
        )
        set_setting("stripe_sync_last_error", str(exc))
        db.session.commit()
        return {"error": str(exc), "retryable": exc.retryable}

    monitored = [
        event for event in raw_events if event.get("type") in MONITORED_EVENT_TYPES
    ]

    inserted = 0
    skipped = 0
    for event in monitored:
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id:
            continue
        exists = (
            db.session.query(StripeEvent.id).filter_by(stripe_event_id=event_id).first()
        )
        if exists:
            skipped += 1
            continue
        try:
            db.session.add(StripeEvent(**extract_fields(event)))
            db.session.flush()
            inserted += 1
        except Exception as exc:
            db.session.rollback()
            logger.warning(
                "Skipping unparseable Stripe event", event_id=event_id, error=str(exc)
            )

    set_setting("stripe_last_sync_at", started_at.isoformat())
    set_setting("stripe_sync_last_error", None)
    db.session.commit()

    summary = {
        "fetched": len(raw_events),
        "monitored": len(monitored),
        "inserted": inserted,
        "skipped": skipped,
        "window_start": window_start.isoformat(),
    }
    logger.info("Stripe event sync completed", **summary)
    return summary
