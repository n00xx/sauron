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
from collections import Counter
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

# How many distinct non-monitored event types a summary names before it stops.
# The point is to let an admin recognise what the key is actually looking at
# ("account.updated x12" says "wrong account" at a glance), not to enumerate
# every type Stripe has.
UNMONITORED_SAMPLE_SIZE = 8


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


def _extract_amount(event_type: str, obj: dict[str, Any]) -> int | None:
    """Amount in the smallest currency unit, per object type.

    ``charge.refunded`` needs its own branch: the object is a Charge, which
    carries BOTH ``amount`` (the original charge) and ``amount_refunded``. A
    plain field-order fallback would report the full charge on a partial
    refund, i.e. show money back that was never returned.
    """
    if event_type == "charge.refunded":
        refunded = obj.get("amount_refunded")
        if isinstance(refunded, int):
            return refunded

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
        "amount": _extract_amount(event_type, obj),
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


def describe_key_mode(api_key: str | None) -> str:
    """``"test"`` | ``"live"`` | ``"unknown"``, from the key prefix alone.

    Read off the prefix rather than from ``GET /v1/account`` on purpose: a
    restricted key may have no Account read permission, so an API call would
    403 exactly when the admin most needs to be told which mode they are in.
    The prefix is always present and always truthful.
    """
    if not api_key:
        return "unknown"
    if "_test_" in api_key:
        return "test"
    if "_live_" in api_key:
        return "live"
    return "unknown"


def reset_sync_watermark() -> None:
    """Forget where the last sync got to, so the next one backfills in full.

    Must be called whenever the API key changes. The watermark records a
    position in *one account's* event stream; carrying it over to a different
    key means the new account is only ever asked for events since the old
    account's last tick, and its 30 days of history are never read. That failure
    is silent — the sync reports success and stores nothing.
    """
    set_setting("stripe_last_sync_at", None)


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
    # No Stripe-Version header on purpose: the account default applies. Pinning
    # an older version would reshape exactly the two fields the dispute queue
    # depends on (evidence_details.due_by and
    # payment_method_details.card.network_reason_code), and extraction here is
    # written to tolerate drift rather than to freeze a schema.
    session.headers.update({"Authorization": f"Bearer {api_key}"})

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


def _sync_window_start(*, full_backfill: bool = False) -> datetime:
    """Where this run starts reading from.

    First run reaches back the full retention window so the tab is populated
    immediately; later runs re-read a short overlap so no event slips through a
    boundary. ``full_backfill`` ignores the watermark entirely, which is what
    "Re-sync last 30 days" in the UI runs.
    """
    if full_backfill:
        return datetime.now(UTC) - timedelta(days=INITIAL_LOOKBACK_DAYS)

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


def sync_stripe_events(
    *, force: bool = False, full_backfill: bool = False
) -> dict[str, Any]:
    """Pull new Stripe events into the local archive.

    Returns a summary dict. Must be called inside an app context.

    The summary distinguishes every reason an event can fail to land — not
    monitored, already known, or unwritable. Collapsing those into one "nothing
    new" number makes a misconfigured key indistinguishable from a healthy
    steady state, which is the difference between a one-line fix and an
    afternoon of guessing.
    """
    if not force and not is_sync_enabled():
        return {"skipped": True, "reason": "disabled"}

    api_key = get_setting("stripe_api_key")
    if not api_key:
        return {"skipped": True, "reason": "no_api_key"}

    started_at = datetime.now(UTC)
    window_start = _sync_window_start(full_backfill=full_backfill)

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

    # What came back that we deliberately ignore. A key pointed at the wrong
    # account still returns a full page of events; naming the types is what
    # makes that visible ("account.updated x12" is not a storefront).
    unmonitored_types = Counter(
        str(event.get("type") or "?")
        for event in raw_events
        if event.get("type") not in MONITORED_EVENT_TYPES
    )
    # Stripe stamps every event with the mode it belongs to, so this settles
    # "is this key looking at test or live data?" without a second API call.
    fetched_livemode = sum(1 for event in raw_events if event.get("livemode") is True)

    inserted = 0
    skipped = 0
    failed = 0
    # Only events written on THIS pass get an alert. The sync runs on an
    # interval over a rolling window, so a refund stays in `monitored` for many
    # ticks after it lands — notifying on anything but a fresh insert would
    # re-announce the same refund every few minutes.
    new_refunds: list[dict[str, Any]] = []
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
            # SAVEPOINT per event. A plain rollback() here would discard every
            # row already flushed in this batch while `inserted` kept counting
            # them — one malformed event late in a page would drop the whole
            # page and still report success.
            with db.session.begin_nested():
                db.session.add(StripeEvent(**extract_fields(event)))
            inserted += 1
            if categorize(str(event.get("type") or "")) == CATEGORY_REFUND:
                new_refunds.append(event)
        except Exception as exc:
            failed += 1
            logger.warning(
                "Skipping unparseable Stripe event", event_id=event_id, error=str(exc)
            )

    set_setting("stripe_last_sync_at", started_at.isoformat())
    set_setting("stripe_sync_last_error", None)

    summary = {
        "fetched": len(raw_events),
        "monitored": len(monitored),
        "inserted": inserted,
        "skipped": skipped,
        "failed": failed,
        "fetched_livemode": fetched_livemode,
        "fetched_testmode": len(raw_events) - fetched_livemode,
        "key_mode": describe_key_mode(api_key),
        "unmonitored_types": unmonitored_types.most_common(UNMONITORED_SAMPLE_SIZE),
        "window_start": window_start.isoformat(),
        "full_backfill": full_backfill,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    # Persisted so the tab can explain a *scheduled* run too. Without this the
    # only run an admin can ever see the shape of is one they clicked.
    set_setting("stripe_sync_last_summary", json.dumps(summary, ensure_ascii=False))
    db.session.commit()

    # After the commit: an alert about a refund that failed to persist would be
    # worse than no alert at all.
    _notify_new_refunds(new_refunds)

    logger.info("Stripe event sync completed", **summary)
    return summary


def _format_amount(obj: dict[str, Any]) -> str:
    """Human-readable amount for a refund-ish Stripe object, or "" if absent."""
    for field in ("amount_refunded", "amount"):
        value = obj.get(field)
        if isinstance(value, int):
            currency = str(obj.get("currency") or "").upper()
            return f"{value / 100:.2f} {currency}".strip()
    return ""


def _notify_new_refunds(events: list[dict[str, Any]]) -> None:
    """Best-effort operational alert, one per refund written on this pass.

    Isolated behind its own try/except: this runs from the scheduler, and a
    notification agent that is down must not fail a sync whose rows are already
    committed.
    """
    if not events:
        return
    try:
        from app.services.notifications import notify

        for event in events:
            obj = event.get("data", {}).get("object", {})
            obj = obj if isinstance(obj, dict) else {}
            amount = _format_amount(obj)
            email = _extract_email(obj)
            details = " • ".join(
                part
                for part in (
                    str(event.get("type") or ""),
                    amount,
                    email or "",
                )
                if part
            )
            notify(
                "Stripe refund",
                f"A refund was recorded in Stripe: {details}",
                tags="money_with_wings",
                event_type="stripe_refund",
            )
    except Exception as exc:
        logger.warning("Failed to send refund notification", error=str(exc))


def get_last_sync_summary() -> dict[str, Any]:
    """The stored summary of the most recent completed sync, or ``{}``.

    Never raises: this feeds a template, and a corrupt settings row must not
    take the tab down.
    """
    raw = get_setting("stripe_sync_last_summary")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
