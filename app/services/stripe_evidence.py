"""Turn a Stripe dispute into the evidence only this instance can produce.

Stripe knows a payment happened. The storefront knows an invitation was issued.
Neither knows whether the buyer actually *used* the service — that lives here,
in ``ActivitySession``: what they streamed, when, from which IPs and devices.

For a digital-goods dispute that is the decisive artefact. Stripe's evidence
form calls it ``access_activity_log``:

    "Server or activity logs showing proof that the customer accessed or
    downloaded the purchased digital product after making the payment. Ideally
    include IP addresses, corresponding timestamps, and any detailed recorded
    activity."

This module builds that text, plus the element set Visa CE 3.0 matches on
(purchase IP, device, account id, email). It is read-only in both directions: it
never writes to Stripe, and the operator pastes the result into the Stripe
dashboard or a Smart Disputes packet themselves.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
import structlog

from app.extensions import db
from app.models import (
    ActivitySession,
    ActivitySnapshot,
    Invitation,
    StripeEvent,
    User,
)
from app.services.stripe_events import (
    REQUEST_TIMEOUT,
    STRIPE_API_BASE,
    StripeApiError,
    get_setting,
)

logger = structlog.get_logger(__name__)

# Cap on sessions rendered into an evidence log. Stripe caps evidence files at
# 4.5 MB / 19 pages, and a wall of rows reads as padding rather than proof.
MAX_SESSIONS_IN_LOG = 200

# Wall-clock budget for one correlation pass, well under the 15 minute default
# sync interval. See resolve_pending_links for why a row cap was not enough.
MAX_CORRELATION_SECONDS = 120.0

# How a link between a Stripe event and a sauron account was established. The
# row cannot answer this on its own — see resolve_event_links.
PROVENANCE_METADATA = "metadata"  # stamped on the PaymentIntent by the storefront
PROVENANCE_SIBLING = "sibling"  # inherited from another event on the same payment
PROVENANCE_EMAIL = "email"  # matched on the checkout email: a guess
PROVENANCE_EXISTING = "existing"  # already linked before this pass

# An event Stripe created longer ago than this is history being imported, not
# news. Generous enough to survive a long-weekend outage; short enough that a
# 30-day backfill cannot page once per historical dispute.
MAX_ALERT_AGE_HOURS = 72


# --------------------------------------------------------------------------
# Stripe → sauron correlation
# --------------------------------------------------------------------------


def fetch_payment_intent(api_key: str, payment_intent_id: str) -> dict[str, Any]:
    """Read one PaymentIntent. Read-only; used purely to reach its metadata."""
    try:
        response = requests.get(
            f"{STRIPE_API_BASE}/payment_intents/{payment_intent_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise StripeApiError(f"Stripe request failed: {exc}", None, True) from exc

    if response.status_code >= 400:
        raise StripeApiError(
            f"Could not read PaymentIntent ({response.status_code}).",
            response.status_code,
            response.status_code >= 500 or response.status_code == 429,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise StripeApiError("Stripe returned invalid JSON.", None, True) from exc
    return body if isinstance(body, dict) else {}


def _invitation_from_metadata(metadata: Any) -> Invitation | None:
    """Read an invitation id off the PaymentIntent metadata, if one is there.

    The storefront does not currently send this key — it sends ``sauronUserId``
    (see :func:`_user_from_metadata`). It is kept because an invitation is a
    strictly richer link than a bare user id: it populates ``invitation_id`` and
    the user can still be derived from it.

    Only the id travels through Stripe — never the invite code, which is a live
    bearer credential.
    """
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("wizarrInvitationId")
    try:
        invitation_id = int(raw)
    except (TypeError, ValueError):
        return None
    return db.session.get(Invitation, invitation_id)


def _user_from_metadata(metadata: Any) -> User | None:
    """Read the sauron user id the storefront stamps onto the PaymentIntent.

    This is the live contract: neexy sends ``sauronUserId`` (and ``orderId``,
    which sauron has nowhere to put and deliberately ignores) on the
    PaymentIntent, not on the Checkout Session.

    It identifies a *purchase's* account directly, which is why it outranks the
    checkout email — that email is the billing one and need not belong to the
    person who redeemed the invite.
    """
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("sauronUserId")
    try:
        user_id = int(raw)
    except (TypeError, ValueError):
        return None
    return db.session.get(User, user_id)


def _payment_intent_metadata(
    payment_intent_id: str,
    api_key: str | None,
    cache: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Metadata for a PaymentIntent, or ``None`` when it cannot be read.

    ``cache`` memoises per batch. Every monitored event of one purchase carries
    the same ``payment_intent_id``, so without it a five-event purchase costs
    five identical round trips. Failures are cached too — a key that cannot read
    PaymentIntents must not be retried once per event.
    """
    if cache is not None and payment_intent_id in cache:
        return cache[payment_intent_id]

    metadata: dict[str, Any] | None = None
    key = api_key or get_setting("stripe_api_key")
    if key:
        try:
            payment_intent = fetch_payment_intent(key, payment_intent_id)
        except StripeApiError as exc:
            logger.debug(
                "PaymentIntent lookup failed during correlation",
                payment_intent=payment_intent_id,
                error=str(exc),
            )
        else:
            raw = payment_intent.get("metadata")
            metadata = raw if isinstance(raw, dict) else {}

    if cache is not None:
        cache[payment_intent_id] = metadata
    return metadata


def _users_for_invitation(invitation: Invitation) -> list[User]:
    """Everyone who redeemed this invitation, newest relationship first."""
    users = list(invitation.users or [])
    if not users and invitation.used_by is not None:
        users = [invitation.used_by]
    return users


def _user_by_email(email: str | None) -> User | None:
    """Fall back to the checkout email.

    Weaker than the metadata link on purpose, and the reason the storefront
    stamps the invitation id: this email is the BILLING one, which need not be
    the address used to redeem the invite, and it identifies a person rather
    than a purchase. Only accepted when it is unambiguous.
    """
    if not email:
        return None
    matches = User.query.filter(db.func.lower(User.email) == email.lower()).all()
    return matches[0] if len(matches) == 1 else None


def resolve_event_links(
    event: StripeEvent,
    api_key: str | None = None,
    metadata_cache: dict[str, Any] | None = None,
    provenance: dict[str, str] | None = None,
) -> bool:
    """Attach ``invitation_id`` / ``wizarr_user_id`` to an event row.

    Returns True when something was resolved. Never raises: an unresolvable
    event stays visible in the UI as unmatched, which is strictly better than
    hiding it — the operator needs to know the link is missing precisely when a
    dispute lands.

    The three sources are tried strongest first, and the order is load-bearing:
    the email fallback is a guess, and if it ran first it would answer for the
    whole purchase — every later event would reuse it and the authoritative
    metadata would never be read.

    ``provenance``, when given, records WHICH of the three answered, keyed by
    ``stripe_event_id``. Nothing on the row itself can express that: the
    authoritative path and the email guess both end up writing ``wizarr_user_id``
    and nothing else, so after the fact the two are indistinguishable — and a
    dispute alert that calls a metadata match a guess is worse than one that says
    nothing at all.
    """

    def _record(source: str) -> bool:
        if provenance is not None and event.stripe_event_id:
            provenance[event.stripe_event_id] = source
        return True

    if event.invitation_id or event.wizarr_user_id:
        return _record(PROVENANCE_EXISTING)

    # An early fraud warning carries `charge` but NO `payment_intent`, and it is
    # the dispute-deflection primitive — the one event type that most needs to
    # resolve. Recover the PaymentIntent from a sibling event on the same charge
    # so EFWs take the deterministic path instead of falling through to email.
    payment_intent_id = event.payment_intent_id
    if not payment_intent_id and event.charge_id:
        by_charge = (
            StripeEvent.query.filter(
                StripeEvent.charge_id == event.charge_id,
                StripeEvent.payment_intent_id.isnot(None),
            )
            .order_by(StripeEvent.id.asc())
            .first()
        )
        if by_charge is not None:
            payment_intent_id = by_charge.payment_intent_id

    # 1. Authoritative: what the storefront stamped on the PaymentIntent.
    if payment_intent_id:
        metadata = _payment_intent_metadata(payment_intent_id, api_key, metadata_cache)
        if metadata:
            invitation = _invitation_from_metadata(metadata)
            user = _user_from_metadata(metadata)
            if invitation is not None:
                event.invitation_id = invitation.id
                if user is None:
                    users = _users_for_invitation(invitation)
                    if len(users) == 1:
                        user = users[0]
            if user is not None:
                event.wizarr_user_id = user.id
            if invitation is not None or user is not None:
                return _record(PROVENANCE_METADATA)

    # 2. Reuse a sibling event on the same payment that resolved to an
    #    invitation. Only invitation links qualify: a bare `wizarr_user_id` may
    #    have come from the email fallback below, and copying a guess across the
    #    purchase would make it indistinguishable from a real match.
    if payment_intent_id:
        sibling = (
            StripeEvent.query.filter(
                StripeEvent.payment_intent_id == payment_intent_id,
                StripeEvent.invitation_id.isnot(None),
            )
            .order_by(StripeEvent.id.desc())
            .first()
        )
        if sibling is not None:
            invitation = db.session.get(Invitation, sibling.invitation_id)
            if invitation is not None:
                event.invitation_id = invitation.id
                users = _users_for_invitation(invitation)
                if len(users) == 1:
                    event.wizarr_user_id = users[0].id
                return _record(PROVENANCE_SIBLING)

    # 3. Last resort: the checkout email.
    user = _user_by_email(event.customer_email)
    if user is not None:
        event.wizarr_user_id = user.id
        return _record(PROVENANCE_EMAIL)

    return False


# --------------------------------------------------------------------------
# Evidence generation
# --------------------------------------------------------------------------


def _sessions_for(event: StripeEvent) -> list[ActivitySession]:
    """Playback sessions belonging to this purchase.

    Prefers every redeemer of the resolved invitation; falls back to the single
    matched user.
    """
    user_ids: set[int] = set()

    if event.invitation_id:
        invitation = db.session.get(Invitation, event.invitation_id)
        if invitation is not None:
            user_ids.update(user.id for user in _users_for_invitation(invitation))
    if event.wizarr_user_id:
        user_ids.add(event.wizarr_user_id)

    if not user_ids:
        return []

    return (
        ActivitySession.query.filter(ActivitySession.wizarr_user_id.in_(user_ids))
        .order_by(ActivitySession.started_at.asc())
        .limit(MAX_SESSIONS_IN_LOG)
        .all()
    )


def _watched_ms_for(session: ActivitySession) -> int | None:
    """How far playback actually got, or ``None`` when nothing was measured.

    ``ActivitySession.duration_ms`` holds the FILE RUNTIME for as long as the
    session is open: the collectors only swap in the playback position on
    ``session_end``. Summing it for a live session reports the length of the
    title as though it had been watched — observed once at 1h 26m against 13
    seconds of real playback, an overstatement of roughly 400x on the single
    number a card issuer weighs.

    The snapshots carry the measured value (``PositionTicks``, recorded on
    start, update and end), so the furthest position reached is the honest
    answer while a session is live.
    """
    furthest = (
        db.session.query(db.func.max(ActivitySnapshot.position_ms))
        .filter(ActivitySnapshot.session_id == session.id)
        .scalar()
    )
    if furthest:
        return int(furthest)

    # A closed session's stored duration IS the measured position — the
    # `session_end` branch already put it there — and every historical import
    # lands this way, with no snapshots behind it.
    if not session.active and session.duration_ms:
        return int(session.duration_ms)

    return None


def _total_watched_ms(sessions: Sequence[ActivitySession]) -> int | None:
    """Summed playback across sessions, or ``None`` if none was measured.

    ``None`` and ``0`` mean different things here and must not collapse: the
    first is "we did not measure", the second is "we measured no playback".
    Only the second is evidence.
    """
    measured = [ms for ms in (_watched_ms_for(s) for s in sessions) if ms is not None]
    return sum(measured) if measured else None


def _fmt_duration(duration_ms: int | None) -> str:
    """Human duration, keeping sub-minute values visible.

    Thirteen seconds of playback rendered as "0m" reads like a rounding
    artefact; rendered as "13s" it reads like what it is. In a dispute the
    difference between "barely used" and "not measured" is the whole argument.
    """
    if not duration_ms or duration_ms <= 0:
        return "-"
    total_minutes = duration_ms // 60000
    if not total_minutes:
        return f"{duration_ms // 1000}s"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%d %H:%M UTC")


# Events that mark the moment money actually arrived, best anchor first.
_PAYMENT_ANCHOR_TYPES = (
    "checkout.session.completed",
    "payment_intent.succeeded",
    "charge.succeeded",
)


def _payment_time(event: StripeEvent) -> datetime | None:
    """When the disputed payment was made — not when this event fired.

    Looks for the original payment event on the same PaymentIntent (or charge).
    Returns ``None`` when no payment event is on file: the "time to first use"
    line is then omitted entirely rather than computed from a wrong anchor. An
    absent line costs nothing; a wrong one submitted as evidence is worse than
    no evidence at all.
    """
    if event.type in _PAYMENT_ANCHOR_TYPES:
        return (
            event.created_at_stripe.replace(tzinfo=UTC)
            if event.created_at_stripe and event.created_at_stripe.tzinfo is None
            else event.created_at_stripe
        )

    if not event.payment_intent_id and not event.charge_id:
        return None

    keys = []
    if event.payment_intent_id:
        keys.append(StripeEvent.payment_intent_id == event.payment_intent_id)
    if event.charge_id:
        keys.append(StripeEvent.charge_id == event.charge_id)

    anchor = (
        StripeEvent.query.filter(
            db.or_(*keys),
            StripeEvent.type.in_(_PAYMENT_ANCHOR_TYPES),
        )
        .order_by(StripeEvent.created_at_stripe.asc())
        .first()
    )
    if anchor is None or anchor.created_at_stripe is None:
        return None
    return (
        anchor.created_at_stripe
        if anchor.created_at_stripe.tzinfo
        else anchor.created_at_stripe.replace(tzinfo=UTC)
    )


def build_access_activity_log(event: StripeEvent) -> str:
    """Render Stripe's ``access_activity_log`` for this purchase.

    Plain text on purpose: it is pasted into Stripe's evidence form or attached
    to a Smart Disputes packet, both of which take text.
    """
    sessions = _sessions_for(event)
    if not sessions:
        return ""

    lines: list[str] = []
    users = sorted({s.user_name for s in sessions if s.user_name})
    ips = sorted({s.ip_address for s in sessions if s.ip_address})
    devices = sorted(
        {
            s.device_name or s.client_name
            for s in sessions
            if s.device_name or s.client_name
        }
    )
    total_ms = _total_watched_ms(sessions)
    first = sessions[0].started_at
    last = sessions[-1].started_at

    lines.append("MEDIA SERVER ACCESS LOG")
    lines.append(f"Account(s): {', '.join(users) if users else 'n/a'}")
    lines.append(f"Sessions recorded: {len(sessions)}")
    # Deliberately not called "watch time": this is the furthest point the
    # player reached, which a seek inflates and a rewatch does not add to. And
    # when nothing was measured the line is omitted rather than filled with the
    # title's runtime — the same rule `_payment_time` applies below, for the
    # same reason: an absent line costs nothing, a wrong one submitted as
    # evidence is worse than no evidence at all.
    if total_ms is not None:
        lines.append(f"Furthest playback position reached: {_fmt_duration(total_ms)}")
    lines.append(f"First access: {_fmt_dt(first)}")
    lines.append(f"Last access: {_fmt_dt(last)}")
    if ips:
        lines.append(f"Source IP addresses: {', '.join(ips)}")
    if devices:
        lines.append(f"Devices/clients: {', '.join(devices)}")

    # Time-to-first-use is the single most persuasive number in a
    # "product not received" dispute — which is exactly why it must be anchored
    # to when the PAYMENT happened, never to this event's own timestamp. A
    # dispute is filed weeks or months after the charge, so anchoring on
    # `event.created_at_stripe` would yield a negative interval (silently
    # dropped) or, on a refund event, a plausible-looking wrong one.
    paid = _payment_time(event)
    if paid and first:
        started = first if first.tzinfo else first.replace(tzinfo=UTC)
        delta = started - paid
        if delta.total_seconds() >= 0:
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            lines.append(f"Payment received: {_fmt_dt(paid)}")
            lines.append(
                f"First access occurred {hours}h {minutes:02d}m after payment."
            )

    lines.append("")
    lines.append("DETAILED SESSION LOG")
    lines.append(
        f"{'Timestamp (UTC)':<20} {'Account':<18} {'IP':<16} "
        f"{'Device':<22} {'Position':<10} Title"
    )
    lines.extend(
        f"{_fmt_dt(session.started_at):<20} "
        f"{(session.user_name or '-')[:17]:<18} "
        f"{(session.ip_address or '-')[:15]:<16} "
        f"{(session.device_name or session.client_name or '-')[:21]:<22} "
        f"{_fmt_duration(_watched_ms_for(session)):<10} "
        f"{session.media_title or '-'}"
        for session in sessions
    )

    return "\n".join(lines)


# Visa's Compelling Evidence 3.0 remedy, as Stripe names it in
# `enhanced_eligibility_types`.
CE3_ELIGIBILITY_TYPE = "visa_compelling_evidence_3"

# The one Visa network reason code CE 3.0 can answer.
CE3_REASON_CODE = "10.4"

CE3_CONFIRMED = "confirmed"
CE3_UNCONFIRMED = "unconfirmed"
CE3_NOT_APPLICABLE = "not_applicable"


def ce3_eligibility(event: StripeEvent) -> str:
    """Stripe's verdict on CE 3.0 for this dispute, in three states.

    The reason code is a necessary condition, never a sufficient one. CE 3.0
    also demands two prior undisputed transactions on the same payment method,
    120-364 days old, agreeing on matching elements — a new customer cannot
    qualify no matter what code the network assigned. Stripe evaluates all of
    that and answers in ``enhanced_eligibility_types``; both disputes of the
    2026-08-26 battery came back with an empty list while sauron's own badge
    claimed eligibility.

    ``CE3_UNCONFIRMED`` covers absent as well as empty on purpose. Stripe
    populates the field late, and sometimes only in livemode, so "not there
    yet" is not a yes — and this reading is the difference between offering the
    operator a defence and sending them after one Stripe already ruled out.

    Read from the stored payload: no extra API call, the row already has it.
    """
    if event.network_reason_code != CE3_REASON_CODE:
        return CE3_NOT_APPLICABLE

    obj = event.payload_dict.get("data", {})
    obj = obj.get("object", {}) if isinstance(obj, dict) else {}
    types = obj.get("enhanced_eligibility_types") if isinstance(obj, dict) else None

    if isinstance(types, list) and CE3_ELIGIBILITY_TYPE in types:
        return CE3_CONFIRMED
    return CE3_UNCONFIRMED


# How firmly this purchase ties to something in sauron. Not the same question
# as PROVENANCE_* above, which records HOW the link was found; this records
# WHAT was found at the end of it.
LINK_ACCOUNT = "account"
LINK_INVITATION_UNREDEEMED = "invitation_unredeemed"
LINK_NONE = "none"


def _link_kind(event: StripeEvent, users: list[User]) -> str:
    """Account, unredeemed invitation, or nothing at all.

    An invitation the storefront minted and nobody redeemed is a real link to a
    real row — and to no account whatsoever. Collapsing it into "linked to an
    account" told an operator to verify the revocation of a user that never
    existed, and buried the better argument: for a signup never redeemed, the
    fact that answers a fraud dispute is that ACCESS WAS NEVER DELIVERED, which
    is stronger than reporting an account with no viewing.
    """
    if users:
        return LINK_ACCOUNT
    if event.invitation_id:
        return LINK_INVITATION_UNREDEEMED
    if event.wizarr_user_id:
        # Row points at a user that is gone: still an account link, just a
        # deleted one. Not the unredeemed case.
        return LINK_ACCOUNT
    return LINK_NONE


def _is_matchable_ip(value: str) -> bool:
    """Whether Stripe could ever have seen this address.

    CE 3.0 matches the purchase IP of the disputed transaction against those of
    prior ones, all of them recorded by Stripe from the public internet. The
    media server sits behind a proxy, so what it logs is often an RFC 1918
    address — observed as ``172.16.10.1`` against the ``201.156.50.146`` Stripe
    held for the very same purchase. A LAN address cannot coincide with
    anything Stripe recorded, so listing it as a matching element promises a
    match that is impossible by construction.

    Anything unparseable is treated the same way: a hostname or a mangled value
    is not something Stripe can match on either.
    """
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        return False
    return parsed.is_global


def build_ce3_elements(event: StripeEvent) -> dict[str, Any]:
    """The element set Visa CE 3.0 matches a disputed charge against.

    CE 3.0 needs the disputed transaction and two prior undisputed ones to agree
    on either two main elements, or one main plus one secondary:

        main       — customer purchase IP, device fingerprint / device ID
        secondary  — shipping address, customer email, customer account ID

    sauron supplies the IP, the device and the account ID. Stripe supplies the
    prior transactions; this function only surfaces what to match on.

    Addresses are split rather than filtered away: a private one is still a true
    statement about what the server saw and belongs in the narrative log, it
    just cannot serve as a matching element. See :func:`_is_matchable_ip`.
    """
    sessions = _sessions_for(event)
    observed = {s.ip_address for s in sessions if s.ip_address}
    matchable = {ip for ip in observed if _is_matchable_ip(ip)}
    # An early fraud warning object carries no email, but the account it
    # resolved to does. Email is a SECONDARY CE 3.0 element, so in a case
    # carrying few elements this is the difference between qualifying and not.
    email = event.customer_email
    if not email and event.wizarr_user_id:
        linked = db.session.get(User, event.wizarr_user_id)
        email = linked.email if linked else None
    return {
        "customer_purchase_ip": sorted(matchable),
        "server_observed_ip": sorted(observed - matchable),
        "device_ids": sorted({s.device_name for s in sessions if s.device_name}),
        "clients": sorted({s.client_name for s in sessions if s.client_name}),
        "customer_account_ids": sorted({s.user_name for s in sessions if s.user_name}),
        "customer_email": email,
        # Three states, not a boolean: Stripe's own verdict, the reason code
        # without it, and neither. See :func:`ce3_eligibility`.
        "ce3_eligibility": ce3_eligibility(event),
    }


def build_evidence_packet(event: StripeEvent) -> dict[str, Any]:
    """Everything the operator needs to answer one dispute, in one dict."""
    sessions = _sessions_for(event)
    invitation = (
        db.session.get(Invitation, event.invitation_id) if event.invitation_id else None
    )
    users = _users_for_invitation(invitation) if invitation else []
    if not users and event.wizarr_user_id:
        user = db.session.get(User, event.wizarr_user_id)
        if user is not None:
            users = [user]

    return {
        "event": event,
        "invitation": invitation,
        "users": users,
        "session_count": len(sessions),
        "furthest_position": _fmt_duration(_total_watched_ms(sessions)),
        "first_access": sessions[0].started_at if sessions else None,
        "last_access": sessions[-1].started_at if sessions else None,
        "access_activity_log": build_access_activity_log(event),
        "ce3": build_ce3_elements(event),
        "has_evidence": bool(sessions),
        # Computed once, here, and read verbatim by the view and the alerts.
        # Both used to re-derive it from `invitation_id or wizarr_user_id`,
        # which cannot tell a redeemed invitation from an unredeemed one.
        "link_kind": _link_kind(event, users),
    }


def resolve_pending_links(
    limit: int = 200,
    budget_seconds: float = MAX_CORRELATION_SECONDS,
    provenance: dict[str, str] | None = None,
) -> int:
    """Correlate event rows that have no sauron link yet. Returns how many stuck.

    Runs after each sync, bounded twice: by row count and by wall clock.

    The row cap alone was not a bound on *time*. Each unresolved purchase costs
    one PaymentIntent read at up to ``REQUEST_TIMEOUT`` seconds, so a backlog of
    200 against a slow Stripe could run past twenty minutes — longer than the
    default 15 minute interval. The job holds ``max_instances=1``, so the next
    tick would not queue behind it, it would be dropped outright with a WARNING
    nobody reads, and the sync would look exactly as dead as it did when this
    module's watchdog was written. Unresolved rows stay visible as unmatched and
    the next tick picks them up.
    """
    deadline = time.monotonic() + budget_seconds
    api_key = get_setting("stripe_api_key")
    pending = (
        StripeEvent.query.filter(
            StripeEvent.invitation_id.is_(None),
            StripeEvent.wizarr_user_id.is_(None),
        )
        .order_by(StripeEvent.created_at_stripe.desc())
        .limit(limit)
        .all()
    )

    # One PaymentIntent read per purchase, not per event: the events of a single
    # purchase all share a payment_intent_id, and correlation now consults that
    # PaymentIntent for every one of them.
    metadata_cache: dict[str, Any] = {}

    resolved = 0
    for event in pending:
        if time.monotonic() >= deadline:
            logger.info(
                "Correlation budget spent; the rest waits for the next tick",
                resolved=resolved,
                remaining=len(pending) - resolved,
            )
            break
        try:
            if resolve_event_links(event, api_key, metadata_cache, provenance):
                resolved += 1
        except Exception as exc:
            logger.debug(
                "Correlation failed for event",
                event_id=event.stripe_event_id,
                error=str(exc),
            )

    if resolved:
        db.session.commit()
    return resolved


# --------------------------------------------------------------------------
# Alerting
# --------------------------------------------------------------------------

# Reused deliberately instead of adding a second "what is my public address?"
# setting. The operator already fills this in for password-reset links, and it
# is the same answer: sauron sits behind a reverse proxy, so `request.url_root`
# is an internal address — and these alerts are sent from the scheduler, where
# there is no request at all.
_PUBLIC_URL_SETTING = "resend_public_base_url"

_ALERT_TITLES = {
    "charge.dispute.created": "Stripe dispute opened",
    "charge.dispute.closed": "Stripe dispute closed",
    "radar.early_fraud_warning.created": "Stripe early fraud warning",
}

_ALERT_EVENT_TYPES = {
    "charge.dispute.created": "stripe_dispute_opened",
    "charge.dispute.closed": "stripe_dispute_closed",
    "radar.early_fraud_warning.created": "stripe_fraud_warning",
}


def _event_link(event: StripeEvent) -> str:
    """Deep link to the evidence view, absolute when we can build one.

    Falls back to the bare path rather than to nothing: "look at
    /eventos/12" is still an instruction the operator can follow, where a
    missing line is just a worse alert.
    """
    path = f"/activity/eventos/{event.id}"
    base = (get_setting(_PUBLIC_URL_SETTING) or "").rstrip("/")
    return f"{base}{path}" if base else path


def _amount_text(event: StripeEvent) -> str:
    if event.amount is None:
        return ""
    return f"{event.amount / 100:.2f} {(event.currency or '').upper()}".strip()


def _evidence_text(packet: dict[str, Any] | None) -> str:
    """One line on how strong the packet is — the point of the whole alert.

    A dispute answered with an empty activity log is worse than one answered
    late, so "there is nothing to send" has to be visible from the notification
    itself, not two clicks away.
    """
    if packet is None:
        return "Evidence packet could not be built — open the event and check."
    if packet.get("link_kind") == LINK_INVITATION_UNREDEEMED:
        # Stronger than "no playback": nothing was ever handed over. Saying
        # "no playback recorded" here understates the case and, worse, implies
        # an account exists to have played nothing.
        return (
            "The invitation for this purchase was NEVER REDEEMED: no account "
            "was created and no access was ever delivered."
        )
    if not packet.get("has_evidence"):
        return (
            "NO playback recorded for this purchase: the access log would go out "
            "empty. Check the link before answering."
        )
    position = packet.get("furthest_position") or "-"
    if position == "-":
        # Sessions exist but nothing measured how far they got. Saying "0m
        # watched" here would be a claim; saying nothing was measured is a fact.
        return (
            f"{packet['session_count']} playback session(s) recorded, but no "
            "playback position was measured. Open the link before answering."
        )
    return (
        f"{packet['session_count']} playback session(s), furthest position {position}."
    )


def _link_quality_text(event: StripeEvent, source: str | None) -> str:
    """Whether this event is tied to a real account, and how firmly.

    Reads the correlation's own provenance rather than guessing from the row,
    because the row cannot tell these apart. The storefront stamps
    ``sauronUserId`` on the PaymentIntent and no invitation id, so the
    AUTHORITATIVE path and the email fallback both leave exactly the same trace:
    ``wizarr_user_id`` set, ``invitation_id`` NULL. Inferring from the columns
    would label every real dispute a guess — precisely inverting the warning
    this line exists to give.
    """
    if source == PROVENANCE_METADATA:
        return "Linked via the PaymentIntent the storefront stamped (authoritative)."
    if source == PROVENANCE_SIBLING:
        return "Linked through another event on the same payment (authoritative)."
    if source == PROVENANCE_EMAIL:
        return (
            "Matched on the checkout email only — that is the BILLING address and "
            "need not be the account that redeemed the invite. Verify before use."
        )
    if event.invitation_id or event.wizarr_user_id:
        # Linked on an earlier pass, so this run never saw how.
        return "Linked to a sauron account (linked on an earlier sync)."
    return "NOT linked to any sauron account: the packet has no activity to draw on."


def _dispute_alert_body(
    event: StripeEvent, packet: dict[str, Any] | None, source: str | None = None
) -> str:
    """The message for one alertable event, one idea per line.

    Newline-separated rather than one paragraph: every agent this reaches
    (Discord, ntfy, Apprise) renders them, and these messages carry a deadline,
    a verdict and a link that all have to survive being read on a phone.
    """
    lines: list[str] = []

    amount = _amount_text(event)
    who = event.customer_email or event.charge_id or event.object_id or "unknown"
    # A closed dispute is a verdict, not a task: the window is gone and there is
    # nothing left to submit, so the evidence lines below would be noise dressed
    # up as an instruction.
    is_actionable = event.type != "charge.dispute.closed"

    if event.type == "charge.dispute.created":
        head = f"A chargeback was opened on {who}"
        if amount:
            head += f" for {amount}"
        lines.append(f"{head}. Reason: {event.dispute_reason or 'unspecified'}.")
        if event.dispute_due_by:
            lines.append(
                f"Evidence is due by {event.dispute_due_by:%Y-%m-%d %H:%M} UTC. "
                "An unanswered dispute is a lost dispute."
            )
        eligibility = ce3_eligibility(event)
        if eligibility == CE3_CONFIRMED:
            lines.append(
                "Visa reason code 10.4, and Stripe confirms this dispute is "
                "eligible for a Compelling Evidence 3.0 counter-response — the "
                "strongest answer available."
            )
        elif eligibility == CE3_UNCONFIRMED:
            # Saying "eligible" here on the reason code alone is how an
            # operator ends up building a CE 3.0 response Stripe will not take.
            lines.append(
                "Visa reason code 10.4: a Compelling Evidence 3.0 response "
                "would apply given prior transaction history, but Stripe has "
                "NOT marked this dispute eligible. Answer on the access log "
                "instead."
            )
    elif event.type == "charge.dispute.closed":
        outcome = (event.status or "unknown").lower()
        verdict = {
            "won": "WON — the funds stay with us.",
            "lost": "LOST — the funds and the dispute fee are gone.",
        }.get(outcome, f"closed as '{outcome}'.")
        head = f"The chargeback on {who}"
        if amount:
            head += f" ({amount})"
        lines.append(f"{head} {verdict}")
        if outcome == "lost":
            if packet is not None and packet.get("link_kind") == (
                LINK_INVITATION_UNREDEEMED
            ):
                # There is no account to revoke. Sending the operator to check
                # one is how this line read on event 26.
                lines.append("No account to revoke: the invitation was never redeemed.")
            else:
                lines.append(
                    "Check that the account was actually revoked — a lost "
                    "dispute with a live account is the worst of both."
                )
    else:  # radar.early_fraud_warning.created
        lines.append(
            f"Stripe flagged {who} as likely fraud"
            + (f" ({amount})" if amount else "")
            + f". Type: {event.dispute_reason or 'unspecified'}."
        )
        lines.append(
            "Refunding inside this window prevents the chargeback entirely — no "
            "dispute fee and no hit to the dispute rate. Weigh that against the "
            "evidence below."
        )

    if is_actionable:
        lines.append(_evidence_text(packet))
    lines.append(_link_quality_text(event, source))
    lines.append(f"Evidence and full detail: {_event_link(event)}")
    if is_actionable:
        lines.append("sauron does not submit anything to Stripe — you do.")
    return "\n".join(lines)


def notify_new_disputes(
    stripe_event_ids: Sequence[str],
    provenance: dict[str, str] | None = None,
) -> int:
    """Alert on freshly stored disputes and fraud warnings. Returns how many.

    MUST run after :func:`resolve_pending_links`, and that ordering is the whole
    reason this is not done inside ``sync_stripe_events`` next to the refund
    alert. The value of a dispute alert is the evidence packet it points at, and
    the packet is assembled from the invitation the event correlates to. Alerting
    at insert time would send "not linked to any account, no playback recorded"
    for a purchase with months of history — the alarm would be wrong in exactly
    the direction that makes people stop reading alarms.

    Best effort, per event: a notification agent that is down must not cost the
    remaining alerts, and none of this may fail the sync whose rows are already
    committed. Fires once, on the pass that stored the row — the durable signal
    is the dispute queue in the Eventos tab, which does not depend on this.
    """
    if not stripe_event_ids:
        return 0

    from app.services.notifications import notify

    rows = StripeEvent.query.filter(
        StripeEvent.stripe_event_id.in_(list(stripe_event_ids))
    ).all()

    cutoff = datetime.now(UTC) - timedelta(hours=MAX_ALERT_AGE_HOURS)
    sent = 0
    for event in rows:
        # "Newly stored" is not "newly happened". The first sync of an install
        # reaches back INITIAL_LOOKBACK_DAYS, and "Re-sync last 30 days" does it
        # on demand, so without this a fresh connection would page once per
        # historical chargeback — an inbox full of settled cases on day one is
        # how an operator learns to ignore this channel before it ever matters.
        created = event.created_at_stripe
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                logger.info(
                    "Skipping alert for a backfilled event",
                    event_id=event.stripe_event_id,
                    created_at=created.isoformat(),
                )
                continue
        try:
            try:
                packet = build_evidence_packet(event)
            except Exception as exc:
                # A packet that will not build must still produce an alert: the
                # deadline is real whether or not sauron can describe the case.
                logger.warning(
                    "Evidence packet failed while alerting",
                    event_id=event.stripe_event_id,
                    error=str(exc),
                )
                packet = None

            notify(
                _ALERT_TITLES.get(event.type, "Stripe dispute"),
                _dispute_alert_body(
                    event,
                    packet,
                    (provenance or {}).get(event.stripe_event_id),
                ),
                tags="rotating_light",
                event_type=_ALERT_EVENT_TYPES.get(event.type, "stripe_dispute_opened"),
            )
            sent += 1
        except Exception as exc:
            logger.warning(
                "Could not send the dispute alert",
                event_id=event.stripe_event_id,
                error=str(exc),
            )

    return sent
