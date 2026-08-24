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

from datetime import UTC, datetime
from typing import Any

import requests
import structlog

from app.extensions import db
from app.models import ActivitySession, Invitation, StripeEvent, User
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
    """Read the invitation id the storefront stamps onto the PaymentIntent.

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


def resolve_event_links(event: StripeEvent, api_key: str | None = None) -> bool:
    """Attach ``invitation_id`` / ``wizarr_user_id`` to an event row.

    Returns True when something was resolved. Never raises: an unresolvable
    event stays visible in the UI as unmatched, which is strictly better than
    hiding it — the operator needs to know the link is missing precisely when a
    dispute lands.
    """
    if event.invitation_id or event.wizarr_user_id:
        return True

    invitation: Invitation | None = None

    # 1. Reuse a sibling event on the same payment that is already resolved.
    #    Cheap, and avoids re-reading the same PaymentIntent for every event.
    if event.payment_intent_id:
        sibling = (
            StripeEvent.query.filter(
                StripeEvent.payment_intent_id == event.payment_intent_id,
                StripeEvent.invitation_id.isnot(None),
            )
            .order_by(StripeEvent.id.desc())
            .first()
        )
        if sibling is not None:
            invitation = db.session.get(Invitation, sibling.invitation_id)

    # 2. Authoritative: the invitation id the storefront stamped on the PI.
    if invitation is None and event.payment_intent_id:
        key = api_key or get_setting("stripe_api_key")
        if key:
            try:
                payment_intent = fetch_payment_intent(key, event.payment_intent_id)
                invitation = _invitation_from_metadata(payment_intent.get("metadata"))
            except StripeApiError as exc:
                logger.debug(
                    "PaymentIntent lookup failed during correlation",
                    payment_intent=event.payment_intent_id,
                    error=str(exc),
                )

    if invitation is not None:
        event.invitation_id = invitation.id
        users = _users_for_invitation(invitation)
        if len(users) == 1:
            event.wizarr_user_id = users[0].id
        return True

    # 3. Last resort: the checkout email.
    user = _user_by_email(event.customer_email)
    if user is not None:
        event.wizarr_user_id = user.id
        return True

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


def _fmt_duration(duration_ms: int | None) -> str:
    if not duration_ms or duration_ms <= 0:
        return "-"
    total_minutes = duration_ms // 60000
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%d %H:%M UTC")


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
    total_ms = sum(s.duration_ms or 0 for s in sessions)
    first = sessions[0].started_at
    last = sessions[-1].started_at

    lines.append("MEDIA SERVER ACCESS LOG")
    lines.append(f"Account(s): {', '.join(users) if users else 'n/a'}")
    lines.append(f"Sessions recorded: {len(sessions)}")
    lines.append(f"Total watch time: {_fmt_duration(total_ms)}")
    lines.append(f"First access: {_fmt_dt(first)}")
    lines.append(f"Last access: {_fmt_dt(last)}")
    if ips:
        lines.append(f"Source IP addresses: {', '.join(ips)}")
    if devices:
        lines.append(f"Devices/clients: {', '.join(devices)}")

    # Time-to-first-use is the single most persuasive number in a
    # "product not received" dispute.
    if event.created_at_stripe and first:
        paid = event.created_at_stripe
        started = first
        if paid.tzinfo is None:
            paid = paid.replace(tzinfo=UTC)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        delta = started - paid
        if delta.total_seconds() >= 0:
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            lines.append(
                f"First access occurred {hours}h {minutes:02d}m after the "
                f"disputed payment."
            )

    lines.append("")
    lines.append("DETAILED SESSION LOG")
    lines.append(
        f"{'Timestamp (UTC)':<20} {'Account':<18} {'IP':<16} "
        f"{'Device':<22} {'Duration':<10} Title"
    )
    lines.extend(
        f"{_fmt_dt(session.started_at):<20} "
        f"{(session.user_name or '-')[:17]:<18} "
        f"{(session.ip_address or '-')[:15]:<16} "
        f"{(session.device_name or session.client_name or '-')[:21]:<22} "
        f"{_fmt_duration(session.duration_ms):<10} "
        f"{session.media_title or '-'}"
        for session in sessions
    )

    return "\n".join(lines)


def build_ce3_elements(event: StripeEvent) -> dict[str, Any]:
    """The element set Visa CE 3.0 matches a disputed charge against.

    CE 3.0 needs the disputed transaction and two prior undisputed ones to agree
    on either two main elements, or one main plus one secondary:

        main       — customer purchase IP, device fingerprint / device ID
        secondary  — shipping address, customer email, customer account ID

    sauron supplies the IP, the device and the account ID. Stripe supplies the
    prior transactions; this function only surfaces what to match on.
    """
    sessions = _sessions_for(event)
    return {
        "customer_purchase_ip": sorted(
            {s.ip_address for s in sessions if s.ip_address}
        ),
        "device_ids": sorted({s.device_name for s in sessions if s.device_name}),
        "clients": sorted({s.client_name for s in sessions if s.client_name}),
        "customer_account_ids": sorted({s.user_name for s in sessions if s.user_name}),
        "customer_email": event.customer_email,
        # 10.4 is the Visa reason code eligible for a CE 3.0 counter-response.
        "ce3_eligible_reason_code": event.network_reason_code == "10.4",
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
        "total_watch_time": _fmt_duration(sum(s.duration_ms or 0 for s in sessions)),
        "first_access": sessions[0].started_at if sessions else None,
        "last_access": sessions[-1].started_at if sessions else None,
        "access_activity_log": build_access_activity_log(event),
        "ce3": build_ce3_elements(event),
        "has_evidence": bool(sessions),
    }


def resolve_pending_links(limit: int = 200) -> int:
    """Correlate event rows that have no sauron link yet. Returns how many stuck.

    Runs after each sync. Bounded so a large backlog cannot stall the scheduler.
    """
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

    resolved = 0
    for event in pending:
        try:
            if resolve_event_links(event, api_key):
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
