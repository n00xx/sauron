"""Tests for the Stripe event mirror and the dispute-evidence builder.

The money-adjacent behaviours these pin down:
  * classification (category/severity) drives the action queue, so a dispute
    must never render as routine noise;
  * extraction must be total — a shape change in Stripe degrades a column, it
    never drops an event;
  * ingestion must be idempotent, because the polling window deliberately
    overlaps and re-reads history on every tick;
  * the access_activity_log is the artefact that wins a digital-goods dispute.
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.extensions import db
from app.models import (
    ActivitySession,
    ActivitySnapshot,
    Invitation,
    MediaServer,
    StripeEvent,
    User,
)
from app.services import stripe_events as se
from app.services import stripe_evidence as sev

# ---------------------------------------------------------------- fixtures


def _event(event_type: str, obj: dict, **overrides) -> dict:
    payload = {
        "id": overrides.pop("id", f"evt_{event_type.replace('.', '_')}"),
        "type": event_type,
        "created": overrides.pop("created", 1_784_000_000),
        "livemode": overrides.pop("livemode", True),
        "api_version": "2024-06-20",
        "data": {"object": obj},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def clean_stripe_events(app):
    with app.app_context():
        StripeEvent.query.delete()
        db.session.commit()
        yield
        StripeEvent.query.delete()
        db.session.commit()


# ---------------------------------------------------------------- classification


class TestClassification:
    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            ("checkout.session.completed", "checkout"),
            ("checkout.session.expired", "checkout"),
            ("payment_intent.succeeded", "payment"),
            ("charge.succeeded", "payment"),
            ("charge.refunded", "refund"),
            ("refund.updated", "refund"),
            ("charge.dispute.created", "dispute"),
            ("charge.dispute.funds_withdrawn", "dispute"),
            ("radar.early_fraud_warning.created", "fraud"),
            ("review.opened", "fraud"),
            ("something.unknown", "other"),
        ],
    )
    def test_categorize(self, event_type, expected):
        assert se.categorize(event_type) == expected

    def test_dispute_ordering_beats_generic_charge_prefix(self):
        """`charge.dispute.*` must not fall into the generic `charge.*` bucket.

        Both prefixes match; if the dispute check ran second, every dispute
        would file itself as a routine payment and vanish from the queue.
        """
        assert se.categorize("charge.dispute.created") == "dispute"
        assert se.categorize("charge.refunded") == "refund"

    @pytest.mark.parametrize(
        ("event_type", "expected"),
        [
            ("charge.dispute.created", "critical"),
            ("charge.dispute.funds_withdrawn", "critical"),
            ("radar.early_fraud_warning.created", "critical"),
            ("payment_intent.payment_failed", "error"),
            ("charge.failed", "error"),
            ("refund.failed", "error"),
            ("charge.refunded", "warning"),
            ("review.opened", "warning"),
            ("payment_intent.succeeded", "info"),
        ],
    )
    def test_severity(self, event_type, expected):
        assert se.severity_for(event_type, {}) == expected

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("won", "info"), ("lost", "error"), ("under_review", "warning")],
    )
    def test_dispute_closed_severity_depends_on_outcome(self, status, expected):
        assert se.severity_for("charge.dispute.closed", {"status": status}) == expected

    def test_oxxo_and_subscription_types_are_not_monitored(self):
        """Events that cannot fire in this integration stay out of the set.

        The storefront sells one-off Checkout Sessions and OXXO was dropped, so
        monitoring these would only ever produce dead UI.
        """
        for dead in (
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "invoice.paid",
            "invoice.payment_failed",
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            assert dead not in se.MONITORED_EVENT_TYPES

    def test_early_fraud_warning_is_monitored(self):
        """The dispute-deflection primitive must be in the set."""
        assert "radar.early_fraud_warning.created" in se.MONITORED_EVENT_TYPES


# ---------------------------------------------------------------- extraction


class TestExtraction:
    def test_checkout_session(self):
        fields = se.extract_fields(
            _event(
                "checkout.session.completed",
                {
                    "id": "cs_123",
                    "payment_intent": "pi_123",
                    "amount_total": 29900,
                    "currency": "mxn",
                    "payment_status": "paid",
                    "customer_details": {"email": "Buyer@Example.com"},
                },
            )
        )
        assert fields["object_id"] == "cs_123"
        assert fields["payment_intent_id"] == "pi_123"
        assert fields["amount"] == 29900
        assert fields["currency"] == "mxn"
        assert fields["status"] == "paid"
        # Normalised for the join against User.email.
        assert fields["customer_email"] == "buyer@example.com"

    def test_payment_intent_uses_its_own_id_as_the_spine(self):
        """A PaymentIntent has no `payment_intent` field — it *is* the intent."""
        fields = se.extract_fields(
            _event("payment_intent.succeeded", {"id": "pi_abc", "amount": 100})
        )
        assert fields["payment_intent_id"] == "pi_abc"

    def test_charge_points_at_itself(self):
        fields = se.extract_fields(
            _event(
                "charge.succeeded",
                {"id": "ch_1", "payment_intent": "pi_1", "amount": 100},
            )
        )
        assert fields["charge_id"] == "ch_1"
        assert fields["payment_intent_id"] == "pi_1"

    def test_dispute_extracts_deadline_and_reason_code(self):
        due = int((datetime.now(UTC) + timedelta(days=6)).timestamp())
        fields = se.extract_fields(
            _event(
                "charge.dispute.created",
                {
                    "id": "dp_1",
                    "charge": "ch_1",
                    "payment_intent": "pi_1",
                    "reason": "fraudulent",
                    "amount": 29900,
                    "evidence_details": {"due_by": due},
                    "payment_method_details": {"card": {"network_reason_code": "10.4"}},
                },
            )
        )
        assert fields["charge_id"] == "ch_1"
        assert fields["dispute_reason"] == "fraudulent"
        assert fields["network_reason_code"] == "10.4"
        assert fields["dispute_due_by"] is not None
        assert fields["severity"] == "critical"

    def test_payment_failure_extracts_error(self):
        fields = se.extract_fields(
            _event(
                "payment_intent.payment_failed",
                {
                    "id": "pi_x",
                    "last_payment_error": {
                        "code": "card_declined",
                        "message": "Your card was declined.",
                    },
                },
            )
        )
        assert fields["error_code"] == "card_declined"
        assert "declined" in fields["error_message"]

    def test_extraction_never_raises_on_a_malformed_event(self):
        """Total function: a broken event degrades columns, it is not dropped."""
        for junk in (
            {},
            {"id": "evt_1"},
            {"id": "evt_1", "type": "charge.dispute.created", "data": None},
            {"id": "evt_1", "type": "charge.refunded", "data": {"object": "nope"}},
            {"id": "evt_1", "type": "x", "created": "not-a-number"},
        ):
            fields = se.extract_fields(junk)
            assert "stripe_event_id" in fields
            assert fields["created_at_stripe"] is not None

    def test_partial_refund_reports_the_refunded_amount(self):
        """A Charge has both `amount` and `amount_refunded`.

        Falling back by field order would show the full charge on a partial
        refund — money reported as returned that never was.
        """
        fields = se.extract_fields(
            _event(
                "charge.refunded",
                {
                    "id": "ch_partial",
                    "amount": 29900,
                    "amount_refunded": 10000,
                    "currency": "mxn",
                },
            )
        )
        assert fields["amount"] == 10000

    def test_payload_is_stored_verbatim(self):
        raw = _event("charge.refunded", {"id": "ch_1", "amount_refunded": 500})
        fields = se.extract_fields(raw)
        assert json.loads(fields["payload"]) == raw


# ---------------------------------------------------------------- ingestion


class TestSync:
    def test_sync_is_idempotent(self, app, clean_stripe_events, monkeypatch):
        """The polling window overlaps on purpose; re-reads must be no-ops."""
        events = [
            _event(
                "charge.dispute.created", {"id": "dp_1", "charge": "ch_1"}, id="evt_1"
            ),
            _event("payment_intent.succeeded", {"id": "pi_1"}, id="evt_2"),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            first = se.sync_stripe_events(force=True)
            second = se.sync_stripe_events(force=True)

            assert first["inserted"] == 2
            assert second["inserted"] == 0
            assert second["skipped"] == 2
            assert StripeEvent.query.count() == 2

    def test_unmonitored_types_are_dropped(self, app, clean_stripe_events, monkeypatch):
        events = [
            _event("payment_intent.succeeded", {"id": "pi_1"}, id="evt_keep"),
            _event("balance.available", {"id": "ba_1"}, id="evt_drop"),
            _event("invoice.paid", {"id": "in_1"}, id="evt_drop2"),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            summary = se.sync_stripe_events(force=True)

            assert summary["fetched"] == 3
            assert summary["monitored"] == 1
            assert StripeEvent.query.count() == 1

    def test_sync_without_a_key_does_nothing(self, app, clean_stripe_events):
        with app.app_context():
            se.set_setting("stripe_api_key", None)
            db.session.commit()
            assert se.sync_stripe_events(force=True) == {
                "skipped": True,
                "reason": "no_api_key",
            }

    def test_api_failure_is_recorded_not_raised(
        self, app, clean_stripe_events, monkeypatch
    ):
        """A scheduler job must survive Stripe being unreachable."""

        def _boom(*a, **k):
            raise se.StripeApiError("Stripe rejected the API key (401).", 401, False)

        monkeypatch.setattr(se, "fetch_events", _boom)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_bad")
            db.session.commit()
            result = se.sync_stripe_events(force=True)

            assert "error" in result
            assert result["retryable"] is False
            assert "401" in se.get_setting("stripe_sync_last_error")

    def test_disabled_sync_is_skipped(self, app, clean_stripe_events):
        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            se.set_setting("stripe_sync_enabled", "false")
            db.session.commit()
            assert se.sync_stripe_events()["reason"] == "disabled"

    def test_interval_floor_protects_stripe(self, app):
        """A zero or negative interval must never hammer the API."""
        with app.app_context():
            se.set_setting("stripe_sync_interval_minutes", "0")
            db.session.commit()
            assert se.get_sync_interval_minutes() == 1

            se.set_setting("stripe_sync_interval_minutes", "not-a-number")
            db.session.commit()
            assert se.get_sync_interval_minutes() == 15

    def test_one_bad_event_does_not_discard_the_batch(
        self, app, clean_stripe_events, monkeypatch
    ):
        """A single unwritable event must cost exactly one event.

        The regression this pins: a bare rollback() in the ingest loop discarded
        every row already flushed in the batch while the counter kept counting
        them, so the sync reported "2 new events stored" and stored one.
        """
        events = [
            _event("payment_intent.succeeded", {"id": "pi_ok1"}, id="evt_ok1"),
            _event("payment_intent.succeeded", {"id": "pi_bad"}, id="evt_bad"),
            _event("payment_intent.succeeded", {"id": "pi_ok2"}, id="evt_ok2"),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        real_extract = se.extract_fields

        def _poison_the_middle_one(event):
            fields = real_extract(event)
            if event.get("id") == "evt_bad":
                fields["type"] = None  # violates NOT NULL
            return fields

        monkeypatch.setattr(se, "extract_fields", _poison_the_middle_one)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            summary = se.sync_stripe_events(force=True)

            assert summary["inserted"] == 2
            assert summary["failed"] == 1
            # The count is the real assertion: what was reported must be what
            # survived the commit.
            assert StripeEvent.query.count() == summary["inserted"]

    def test_summary_separates_not_monitored_from_already_known(
        self, app, clean_stripe_events, monkeypatch
    ):
        """A quiet sync has two opposite causes; they must not look alike."""
        events = [
            _event("payment_intent.succeeded", {"id": "pi_1"}, id="evt_known"),
            _event("account.updated", {"id": "acct_1"}, id="evt_ignored_1"),
            _event("account.updated", {"id": "acct_2"}, id="evt_ignored_2"),
            _event("payout.paid", {"id": "po_1"}, id="evt_ignored_3"),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            se.sync_stripe_events(force=True)
            second = se.sync_stripe_events(force=True)

            assert second["fetched"] == 4
            assert second["monitored"] == 1
            assert second["skipped"] == 1  # already known
            assert second["inserted"] == 0
            # The histogram is what identifies a key aimed at another account.
            assert dict(second["unmonitored_types"]) == {
                "account.updated": 2,
                "payout.paid": 1,
            }

    def test_summary_reports_the_livemode_split(
        self, app, clean_stripe_events, monkeypatch
    ):
        """Stripe stamps every event with its mode — no second call needed."""
        events = [
            _event(
                "payment_intent.succeeded", {"id": "pi_1"}, id="evt_l", livemode=True
            ),
            _event("charge.succeeded", {"id": "ch_1"}, id="evt_t", livemode=False),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            summary = se.sync_stripe_events(force=True)

            assert summary["fetched_livemode"] == 1
            assert summary["fetched_testmode"] == 1
            assert summary["key_mode"] == "test"

    def test_key_mode_is_read_off_the_prefix(self):
        """A restricted key may not be allowed to read /v1/account."""
        assert se.describe_key_mode("rk_test_abc") == "test"
        assert se.describe_key_mode("sk_test_abc") == "test"
        assert se.describe_key_mode("rk_live_abc") == "live"
        assert se.describe_key_mode("sk_live_abc") == "live"
        assert se.describe_key_mode("garbage") == "unknown"
        assert se.describe_key_mode(None) == "unknown"

    def test_resetting_the_watermark_restores_the_full_lookback(self, app):
        """The fix for a key swapped while the watermark pointed elsewhere."""
        with app.app_context():
            se.set_setting("stripe_last_sync_at", datetime.now(UTC).isoformat())
            db.session.commit()
            resumed = se._sync_window_start()
            assert (datetime.now(UTC) - resumed) < timedelta(hours=1)

            se.reset_sync_watermark()
            db.session.commit()
            full = se._sync_window_start()
            assert (datetime.now(UTC) - full) > timedelta(days=29)

    def test_full_backfill_ignores_the_watermark(self, app):
        """The backfill button must not resume from the saved position."""
        with app.app_context():
            se.set_setting("stripe_last_sync_at", datetime.now(UTC).isoformat())
            db.session.commit()
            window = se._sync_window_start(full_backfill=True)
            assert (datetime.now(UTC) - window) > timedelta(days=29)

    def test_last_summary_survives_for_scheduled_runs(
        self, app, clean_stripe_events, monkeypatch
    ):
        """A background tick must be as inspectable as a clicked one."""
        events = [_event("payment_intent.succeeded", {"id": "pi_1"}, id="evt_s")]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            se.sync_stripe_events(force=True)

            stored = se.get_last_sync_summary()
            assert stored["fetched"] == 1
            assert stored["inserted"] == 1
            assert stored["key_mode"] == "test"

    def test_last_summary_tolerates_corruption(self, app):
        """A bad settings row must not take the tab down."""
        with app.app_context():
            se.set_setting("stripe_sync_last_summary", "{not json")
            db.session.commit()
            assert se.get_last_sync_summary() == {}


# ---------------------------------------------------------------- messaging


class TestSyncMessages:
    """The sentence an admin reads must name the actual outcome.

    These four cases all used to render as one green "no new events" banner,
    which is what made a key aimed at the wrong account indistinguishable from
    a healthy steady state.
    """

    def _message(self, app, summary):
        from app.activity.api.blueprint import _sync_result_message

        with app.app_context():
            return _sync_result_message(summary)

    def _summary(self, **overrides):
        base = {
            "fetched": 0,
            "monitored": 0,
            "inserted": 0,
            "skipped": 0,
            "failed": 0,
            "fetched_livemode": 0,
            "fetched_testmode": 0,
            "unmonitored_types": [],
            "window_start": "2026-07-25T00:00:00+00:00",
        }
        base.update(overrides)
        return base

    def test_new_events_read_as_success(self, app):
        message, kind = self._message(
            app, self._summary(fetched=5, monitored=5, inserted=5)
        )
        assert kind == "success"
        assert "5 new events" in message

    def test_all_known_reads_as_a_healthy_no_op(self, app):
        message, kind = self._message(
            app, self._summary(fetched=5, monitored=5, inserted=0, skipped=5)
        )
        assert kind == "success"
        assert "already stored" in message

    def test_nothing_monitored_warns_and_names_the_types(self, app):
        """The case that cost an afternoon: Stripe answered, nothing matched."""
        message, kind = self._message(
            app,
            self._summary(
                fetched=37,
                monitored=0,
                unmonitored_types=[["account.updated", 12], ["payout.paid", 5]],
                fetched_testmode=37,
            ),
        )
        assert kind == "warning"
        assert "37" in message
        assert "account.updated ×12" in message
        # It must point at the actual cause, not just report a number.
        assert "different account" in message
        assert "test-mode" in message

    def test_empty_response_is_distinct_from_nothing_monitored(self, app):
        message, kind = self._message(app, self._summary(fetched=0))
        assert kind == "warning"
        assert "no events at all" in message

    def test_unwritable_events_are_never_hidden_behind_success(self, app):
        message, kind = self._message(
            app, self._summary(fetched=3, monitored=3, inserted=2, failed=1)
        )
        assert kind == "error"
        assert "could not be stored" in message


# ---------------------------------------------------------------- evidence


class TestEvidence:
    @pytest.fixture
    def purchase(self, app, clean_stripe_events):
        """A redeemed invitation with playback history, and its dispute event."""
        with app.app_context():
            server = MediaServer(name="jf-test", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()

            invitation = Invitation(code="EVIDENCE1", used=True)
            user = User(
                username="buyer",
                email="buyer@example.com",
                code="EVIDENCE1",
                token="tok-evidence",
                server_id=server.id,
            )
            db.session.add_all([invitation, user])
            db.session.flush()
            invitation.users.append(user)

            base = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
            for offset, title, ip in [
                (0, "Dune", "189.10.0.1"),
                (2, "Arrival", "189.10.0.1"),
                (30, "Blade Runner", "189.10.0.9"),
            ]:
                db.session.add(
                    ActivitySession(
                        server_id=server.id,
                        session_id=f"s{offset}",
                        user_name="buyer",
                        media_title=title,
                        started_at=base + timedelta(hours=offset),
                        duration_ms=3_600_000,
                        ip_address=ip,
                        device_name="Android TV",
                        wizarr_user_id=user.id,
                        active=False,
                    )
                )

            event = StripeEvent(
                stripe_event_id="evt_dispute_ev",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=base - timedelta(hours=1),
                livemode=True,
                object_id="dp_1",
                charge_id="ch_1",
                payment_intent_id="pi_1",
                customer_email="buyer@example.com",
                amount=29900,
                currency="mxn",
                network_reason_code="10.4",
                invitation_id=invitation.id,
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()
            yield event.id

            StripeEvent.query.delete()
            ActivitySession.query.delete()
            db.session.delete(user)
            db.session.delete(invitation)
            db.session.delete(server)
            db.session.commit()

    def test_access_activity_log_contains_what_stripe_asks_for(self, app, purchase):
        """Stripe wants IPs, timestamps and recorded activity. All three appear."""
        with app.app_context():
            event = db.session.get(StripeEvent, purchase)
            log = sev.build_access_activity_log(event)

            assert "189.10.0.1" in log
            assert "189.10.0.9" in log
            assert "Dune" in log
            assert "Blade Runner" in log
            assert "2026-08-01" in log
            assert "Sessions recorded: 3" in log
            # Time-to-first-use needs a payment event as anchor; this fixture
            # has none, so the line is correctly absent. See
            # test_time_to_first_use_is_anchored_to_the_payment_not_the_dispute.
            assert "after payment" not in log

    def test_time_to_first_use_is_anchored_to_the_payment_not_the_dispute(
        self, app, purchase
    ):
        """A dispute is filed weeks after the charge.

        Anchoring on the dispute event's own timestamp yields a negative
        interval (silently dropped) or a wrong positive one. The anchor must be
        the payment event on the same PaymentIntent.
        """
        with app.app_context():
            event = db.session.get(StripeEvent, purchase)
            # Realistic: the dispute lands 45 days after the sessions.
            event.created_at_stripe = datetime(2026, 9, 20, tzinfo=UTC)
            # The real payment, an hour before the first stream.
            db.session.add(
                StripeEvent(
                    stripe_event_id="evt_the_payment",
                    type="checkout.session.completed",
                    category="checkout",
                    severity="info",
                    created_at_stripe=datetime(2026, 8, 1, 11, 0, tzinfo=UTC),
                    livemode=True,
                    payment_intent_id="pi_1",
                )
            )
            db.session.commit()

            log = sev.build_access_activity_log(event)
            assert "Payment received: 2026-08-01 11:00 UTC" in log
            assert "First access occurred 1h 00m after payment." in log

    def test_time_to_first_use_is_omitted_without_a_payment_event(self, app, purchase):
        """No anchor on file → drop the line rather than invent a number."""
        with app.app_context():
            event = db.session.get(StripeEvent, purchase)
            event.created_at_stripe = datetime(2026, 9, 20, tzinfo=UTC)
            db.session.commit()

            log = sev.build_access_activity_log(event)
            assert "after payment" not in log
            # The rest of the evidence still renders.
            assert "Sessions recorded: 3" in log

    def test_fraud_warning_resolves_through_its_charge(
        self, app, clean_stripe_events, monkeypatch
    ):
        """An EFW carries `charge` but no `payment_intent`.

        It is the deflection primitive, so it must reach the deterministic path
        via a sibling event on the same charge instead of falling through to
        the email guess.
        """
        with app.app_context():
            invitation = Invitation(code="EFWLINK", used=True)
            db.session.add(invitation)
            db.session.flush()

            db.session.add_all(
                [
                    # Already-correlated sibling on the same charge.
                    StripeEvent(
                        stripe_event_id="evt_efw_sibling",
                        type="charge.succeeded",
                        category="payment",
                        severity="info",
                        created_at_stripe=datetime.now(UTC),
                        livemode=True,
                        charge_id="ch_efw",
                        payment_intent_id="pi_efw",
                        invitation_id=invitation.id,
                    ),
                    StripeEvent(
                        stripe_event_id="evt_efw",
                        type="radar.early_fraud_warning.created",
                        category="fraud",
                        severity="critical",
                        created_at_stripe=datetime.now(UTC),
                        livemode=True,
                        charge_id="ch_efw",
                        payment_intent_id=None,
                    ),
                ]
            )
            db.session.commit()

            efw = StripeEvent.query.filter_by(stripe_event_id="evt_efw").one()
            # No metadata on the PaymentIntent, so correlation must fall through
            # to the sibling. Stubbed so the test never reaches the network.
            monkeypatch.setattr(sev, "fetch_payment_intent", lambda *a, **k: {})
            assert sev.resolve_event_links(efw, api_key="rk_test_x") is True
            assert efw.invitation_id == invitation.id

            db.session.rollback()
            StripeEvent.query.delete()
            db.session.delete(db.session.get(Invitation, invitation.id))
            db.session.commit()

    @pytest.fixture
    def two_buyers(self, app, clean_stripe_events):
        """Two users: one findable by email, one only by PaymentIntent metadata."""
        with app.app_context():
            server = MediaServer(
                name="jf-corr", server_type="jellyfin", url="http://corr"
            )
            db.session.add(server)
            db.session.flush()

            by_email = User(
                username="email-match",
                email="shared@example.com",
                code="CORR1",
                token="tok-corr-1",
                server_id=server.id,
            )
            by_metadata = User(
                username="metadata-match",
                email="metadata-buyer@example.com",
                code="CORR2",
                token="tok-corr-2",
                server_id=server.id,
            )
            db.session.add_all([by_email, by_metadata])
            db.session.commit()
            yield by_email.id, by_metadata.id

            StripeEvent.query.delete()
            db.session.delete(db.session.get(User, by_email.id))
            db.session.delete(db.session.get(User, by_metadata.id))
            db.session.delete(db.session.get(MediaServer, server.id))
            db.session.commit()

    def _pending(self, **overrides) -> StripeEvent:
        fields = {
            "stripe_event_id": "evt_corr",
            "type": "payment_intent.succeeded",
            "category": "payment",
            "severity": "info",
            "created_at_stripe": datetime.now(UTC),
            "livemode": False,
            "payment_intent_id": "pi_corr",
        }
        fields.update(overrides)
        return StripeEvent(**fields)

    def test_sauron_user_id_metadata_resolves_the_user(
        self, app, two_buyers, monkeypatch
    ):
        """The live contract: neexy stamps sauronUserId on the PaymentIntent."""
        _, metadata_user_id = two_buyers
        monkeypatch.setattr(
            sev,
            "fetch_payment_intent",
            lambda *a, **k: {"metadata": {"sauronUserId": str(metadata_user_id)}},
        )

        with app.app_context():
            event = self._pending()
            db.session.add(event)
            db.session.flush()

            assert sev.resolve_event_links(event, api_key="rk_test_x") is True
            assert event.wizarr_user_id == metadata_user_id

    def test_metadata_outranks_the_email_guess(self, app, two_buyers, monkeypatch):
        """Ordering is load-bearing, and this is the case that proves it.

        The checkout email is the BILLING address and resolves to a different
        person. If the email guess ran first it would answer for the whole
        purchase — every later event would reuse it and the authoritative
        metadata would never be read.
        """
        email_user_id, metadata_user_id = two_buyers
        monkeypatch.setattr(
            sev,
            "fetch_payment_intent",
            lambda *a, **k: {"metadata": {"sauronUserId": str(metadata_user_id)}},
        )

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            # Ingested email-first, which is the order that used to poison it.
            db.session.add_all(
                [
                    self._pending(
                        stripe_event_id="evt_corr_a",
                        type="charge.succeeded",
                        customer_email="shared@example.com",
                    ),
                    self._pending(
                        stripe_event_id="evt_corr_b",
                        customer_email="shared@example.com",
                    ),
                ]
            )
            db.session.commit()

            assert sev.resolve_pending_links() == 2

            for stripe_id in ("evt_corr_a", "evt_corr_b"):
                row = StripeEvent.query.filter_by(stripe_event_id=stripe_id).one()
                assert row.wizarr_user_id == metadata_user_id, stripe_id
                assert row.wizarr_user_id != email_user_id

    def test_correlation_stops_at_its_time_budget(self, app, two_buyers, monkeypatch):
        """The row cap was never a bound on *time*.

        Each unresolved purchase costs one PaymentIntent read at up to
        REQUEST_TIMEOUT seconds, so a backlog could outlast the sync interval.
        The job holds max_instances=1, so the next tick would not queue behind a
        slow pass — it would be dropped with a WARNING nobody reads, and the sync
        would look exactly as dead as the outage this module was written for.
        """
        _, metadata_user_id = two_buyers
        calls: list[str] = []

        def _slow_fetch(api_key, payment_intent_id, **kwargs):
            calls.append(payment_intent_id)
            return {"metadata": {"sauronUserId": str(metadata_user_id)}}

        monkeypatch.setattr(sev, "fetch_payment_intent", _slow_fetch)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.add_all(
                [
                    self._pending(
                        stripe_event_id=f"evt_budget_{n}",
                        payment_intent_id=f"pi_budget_{n}",
                    )
                    for n in range(5)
                ]
            )
            db.session.commit()

            # A budget already spent: the loop must stop before the first row,
            # not grind through all five.
            resolved = sev.resolve_pending_links(budget_seconds=-1)

        assert resolved == 0
        assert calls == [], "no Stripe read may happen once the budget is spent"

    def test_batch_reads_each_payment_intent_once(self, app, two_buyers, monkeypatch):
        """Every event of a purchase shares one PaymentIntent — read it once."""
        _, metadata_user_id = two_buyers
        calls: list[str] = []

        def _counting_fetch(api_key, payment_intent_id, **kwargs):
            calls.append(payment_intent_id)
            return {"metadata": {"sauronUserId": str(metadata_user_id)}}

        monkeypatch.setattr(sev, "fetch_payment_intent", _counting_fetch)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.add_all(
                [
                    self._pending(stripe_event_id=f"evt_corr_{n}", type=kind)
                    for n, kind in enumerate(
                        [
                            "charge.succeeded",
                            "payment_intent.succeeded",
                            "refund.created",
                        ]
                    )
                ]
            )
            db.session.commit()

            assert sev.resolve_pending_links() == 3
            assert calls == ["pi_corr"]

    def test_unknown_sauron_user_id_falls_through_to_email(
        self, app, two_buyers, monkeypatch
    ):
        """A stale id must not block the weaker path that could still match."""
        email_user_id, _ = two_buyers
        monkeypatch.setattr(
            sev,
            "fetch_payment_intent",
            lambda *a, **k: {"metadata": {"sauronUserId": "999999"}},
        )

        with app.app_context():
            event = self._pending(customer_email="shared@example.com")
            db.session.add(event)
            db.session.flush()

            assert sev.resolve_event_links(event, api_key="rk_test_x") is True
            assert event.wizarr_user_id == email_user_id

    def test_invitation_metadata_still_resolves(
        self, app, clean_stripe_events, monkeypatch
    ):
        """wizarrInvitationId is not sent today, but it is the richer link.

        This branch shipped with zero coverage — the only correlation test
        passed api_key=None, which skipped the PaymentIntent read entirely, so
        the suite stayed green against a contract that never existed.
        """
        with app.app_context():
            invitation = Invitation(code="METAINV", used=True)
            db.session.add(invitation)
            db.session.commit()
            invitation_id = invitation.id

            monkeypatch.setattr(
                sev,
                "fetch_payment_intent",
                lambda *a, **k: {
                    "metadata": {"wizarrInvitationId": str(invitation_id)}
                },
            )

            event = self._pending(stripe_event_id="evt_meta_inv")
            db.session.add(event)
            db.session.flush()

            assert sev.resolve_event_links(event, api_key="rk_test_x") is True
            assert event.invitation_id == invitation_id

            db.session.rollback()
            StripeEvent.query.delete()
            db.session.delete(db.session.get(Invitation, invitation_id))
            db.session.commit()

    def test_ce3_elements_expose_the_matching_keys(self, app, purchase):
        with app.app_context():
            event = db.session.get(StripeEvent, purchase)
            ce3 = sev.build_ce3_elements(event)

            assert ce3["customer_purchase_ip"] == ["189.10.0.1", "189.10.0.9"]
            assert ce3["customer_account_ids"] == ["buyer"]
            assert ce3["customer_email"] == "buyer@example.com"
            # 10.4 is necessary but not sufficient, and this fixture carries no
            # Stripe verdict — so the honest answer is "unconfirmed", not "yes".
            # See TestCE3Eligibility.
            assert ce3["ce3_eligibility"] == "unconfirmed"

    def test_packet_reports_absence_of_evidence_honestly(
        self, app, clean_stripe_events
    ):
        """An unmatched event must say so, not render an empty log as proof."""
        with app.app_context():
            orphan = StripeEvent(
                stripe_event_id="evt_orphan",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=datetime.now(UTC),
                livemode=True,
                customer_email="nobody@example.com",
            )
            db.session.add(orphan)
            db.session.commit()

            packet = sev.build_evidence_packet(orphan)
            assert packet["has_evidence"] is False
            assert packet["access_activity_log"] == ""
            assert packet["session_count"] == 0

    def test_ambiguous_email_is_not_matched(self, app, clean_stripe_events):
        """Two accounts on one email cannot identify a purchase — refuse to guess."""
        with app.app_context():
            server = MediaServer(name="jf-amb", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()
            for i in (1, 2):
                db.session.add(
                    User(
                        username=f"dup{i}",
                        email="shared@example.com",
                        code=f"C{i}",
                        token=f"tok-dup{i}",
                        server_id=server.id,
                    )
                )
            db.session.commit()

            assert sev._user_by_email("shared@example.com") is None

            User.query.filter_by(email="shared@example.com").delete()
            db.session.delete(server)
            db.session.commit()


# ---------------------------------------------------------------- routes


class TestRoutes:
    def test_eventos_routes_require_login(self, client):
        for path in ("/activity/eventos", "/activity/eventos/grid"):
            response = client.get(path)
            assert response.status_code in (302, 401), path

    def test_settings_and_sync_require_login(self, client):
        for path in ("/activity/eventos/settings", "/activity/eventos/sync"):
            response = client.post(path)
            assert response.status_code in (302, 401), path


class TestRendering:
    """Render every template for real — a Jinja error here is a 500 in prod."""

    @pytest.fixture
    def admin(self, app):
        from app.models import AdminAccount

        with app.app_context():
            account = AdminAccount.query.filter_by(username="stripeadmin").first()
            created = account is None
            if created:
                account = AdminAccount(username="stripeadmin")
                account.set_password("StripePass123")
                db.session.add(account)
                db.session.commit()
            yield
            if created:
                db.session.delete(
                    AdminAccount.query.filter_by(username="stripeadmin").first()
                )
                db.session.commit()

    @pytest.fixture
    def logged_in(self, client, admin):
        response = client.post(
            "/login", data={"username": "stripeadmin", "password": "StripePass123"}
        )
        assert response.status_code in {200, 302, 303}
        return client

    def test_tab_warns_when_the_scheduled_sync_is_stalled(
        self, app, logged_in, clean_stripe_events, monkeypatch
    ):
        """ "Enabled" is a saved setting, not evidence that anything runs.

        The tab showed sync enabled on a 15 minute interval while the last sync
        was 48 hours old, and said nothing.
        """

        class _StoppedScheduler:
            running = False

            def get_job(self, job_id):
                return None

        monkeypatch.setattr("app.extensions.scheduler", _StoppedScheduler())

        with app.app_context():
            se.set_setting("stripe_api_key", "sk_test_stalled")
            se.set_setting("stripe_sync_enabled", "true")
            se.set_setting("stripe_sync_interval_minutes", "15")
            se.set_setting(
                "stripe_last_sync_at",
                (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
            )
            db.session.commit()

        response = logged_in.get("/activity/eventos")

        assert response.status_code == 200
        assert b"Scheduled sync is not running" in response.data

    def test_one_dispute_is_listed_once_on_the_tab(
        self, app, logged_in, clean_stripe_events
    ):
        """The counter and the list both read the de-duplicated queue."""
        now = datetime.now(UTC)
        due = now + timedelta(days=5)
        with app.app_context():
            db.session.add_all(
                [
                    StripeEvent(
                        stripe_event_id=f"evt_dq_count_{i}",
                        type=event_type,
                        category="dispute",
                        severity="critical",
                        created_at_stripe=now - timedelta(hours=3 - i),
                        livemode=True,
                        object_id="dp_count",
                        customer_email="queue@example.com",
                        amount=29900,
                        currency="mxn",
                        status="needs_response",
                        dispute_reason="fraudulent",
                        dispute_due_by=due,
                        payload=json.dumps({"id": f"evt_dq_count_{i}"}),
                    )
                    for i, event_type in enumerate(
                        [
                            "charge.dispute.created",
                            "charge.dispute.updated",
                            "charge.dispute.funds_withdrawn",
                        ]
                    )
                ]
            )
            db.session.commit()

        body = logged_in.get("/activity/eventos?livemode=true").data.decode("utf-8")

        # One dispute, one row — not three deadlines for the same chargeback.
        assert body.count("queue@example.com") == 1

    def test_tab_renders_when_stripe_is_not_configured(
        self, app, logged_in, clean_stripe_events
    ):
        with app.app_context():
            se.set_setting("stripe_api_key", None)
            db.session.commit()

        response = logged_in.get("/activity/eventos")
        assert response.status_code == 200
        assert b"Stripe" in response.data

    def test_tab_and_grid_render_with_events(self, app, logged_in, clean_stripe_events):
        with app.app_context():
            due = datetime.now(UTC) + timedelta(days=5)
            db.session.add_all(
                [
                    StripeEvent(
                        stripe_event_id="evt_render_dispute",
                        type="charge.dispute.created",
                        category="dispute",
                        severity="critical",
                        created_at_stripe=datetime.now(UTC),
                        livemode=True,
                        customer_email="render@example.com",
                        amount=29900,
                        currency="mxn",
                        dispute_reason="fraudulent",
                        dispute_due_by=due,
                        network_reason_code="10.4",
                        payload=json.dumps({"id": "evt_render_dispute"}),
                    ),
                    StripeEvent(
                        stripe_event_id="evt_render_efw",
                        type="radar.early_fraud_warning.created",
                        category="fraud",
                        severity="critical",
                        created_at_stripe=datetime.now(UTC),
                        livemode=True,
                        customer_email="render@example.com",
                    ),
                ]
            )
            db.session.commit()

        tab = logged_in.get("/activity/eventos")
        assert tab.status_code == 200
        # The action queue and the CE 3.0 flag are the reason this tab exists.
        assert b"10.4" in tab.data or b"CE 3.0" in tab.data

        grid = logged_in.get("/activity/eventos/grid?livemode=true")
        assert grid.status_code == 200
        assert b"render@example.com" in grid.data
        assert b"charge.dispute.created" in grid.data

    def test_detail_renders_including_raw_payload(
        self, app, logged_in, clean_stripe_events
    ):
        with app.app_context():
            event = StripeEvent(
                stripe_event_id="evt_render_detail",
                type="payment_intent.payment_failed",
                category="payment",
                severity="error",
                created_at_stripe=datetime.now(UTC),
                livemode=True,
                error_code="card_declined",
                error_message="Your card was declined.",
                payload=json.dumps({"id": "evt_render_detail", "type": "x"}),
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        response = logged_in.get(f"/activity/eventos/{event_id}")
        assert response.status_code == 200
        assert b"card_declined" in response.data

    def test_sync_result_is_visible_on_the_tab(self, app, logged_in):
        """Nothing in this app renders flashed messages.

        A flashed sync error would be invisible: the admin would see an empty
        tab and no reason why. Both actions must re-render the tab WITH the
        result in the body.
        """
        with app.app_context():
            se.set_setting("stripe_api_key", None)
            db.session.commit()

        response = logged_in.post("/activity/eventos/sync")
        assert response.status_code == 200
        assert b"no API key configured" in response.data

        saved = logged_in.post(
            "/activity/eventos/settings", data={"stripe_api_key": ""}
        )
        assert saved.status_code == 200
        assert b"Add an API key" in saved.data

    def test_mode_defaults_to_test_when_only_test_events_exist(
        self, app, logged_in, clean_stripe_events
    ):
        """A sandbox-only account must not open on an empty Live view.

        Defaulting to Live with no live events makes a successful sync look
        broken.
        """
        with app.app_context():
            db.session.add(
                StripeEvent(
                    stripe_event_id="evt_only_test",
                    type="payment_intent.succeeded",
                    category="payment",
                    severity="info",
                    created_at_stripe=datetime.now(UTC),
                    livemode=False,
                    customer_email="sandbox-default@example.com",
                )
            )
            db.session.commit()

        response = logged_in.get("/activity/eventos")
        assert response.status_code == 200
        # The test-mode option is the selected one.
        assert b'value="false" selected' in response.data

    def test_missing_event_is_a_404_not_a_crash(self, logged_in):
        assert logged_in.get("/activity/eventos/999999").status_code == 404

    def test_changing_the_api_key_resets_the_sync_position(self, app, logged_in):
        """Pointing at a new account must re-arm the 30-day backfill.

        Carrying the old watermark over means the new account is only ever
        asked for events since the old account's last tick — the tab stays
        empty while every sync reports success.
        """
        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_old")
            se.set_setting("stripe_last_sync_at", datetime.now(UTC).isoformat())
            db.session.commit()

        response = logged_in.post(
            "/activity/eventos/settings", data={"stripe_api_key": "rk_test_new"}
        )
        assert response.status_code == 200
        # Silently resetting would look identical to the bug it fixes.
        assert b"full 30-day history" in response.data

        with app.app_context():
            assert se.get_setting("stripe_last_sync_at") is None

    def test_saving_without_touching_the_key_keeps_the_position(self, app, logged_in):
        """Editing the interval must not force a needless full re-read."""
        stamp = datetime.now(UTC).isoformat()
        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_same")
            se.set_setting("stripe_last_sync_at", stamp)
            db.session.commit()

        response = logged_in.post(
            "/activity/eventos/settings",
            data={"stripe_api_key": "", "stripe_sync_interval_minutes": "30"},
        )
        assert response.status_code == 200

        with app.app_context():
            assert se.get_setting("stripe_last_sync_at") == stamp

    def test_tab_shows_what_the_last_sync_actually_saw(
        self, app, logged_in, clean_stripe_events, monkeypatch
    ):
        """The whole point of the fix: no container log required.

        A sync that reaches Stripe and matches nothing must say so on the tab,
        naming the types it ignored.
        """
        events = [
            _event("account.updated", {"id": "acct_1"}, id=f"evt_ign_{n}")
            for n in range(3)
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

        response = logged_in.post("/activity/eventos/sync")
        assert response.status_code == 200
        body = response.data.decode()

        assert "account.updated" in body
        assert "different account" in body
        # The diagnostics panel must survive a plain reload, not just the POST.
        reloaded = logged_in.get("/activity/eventos").data.decode()
        assert "Last sync result" in reloaded
        assert "account.updated" in reloaded

    def test_full_backfill_button_reaches_the_service(
        self, app, logged_in, clean_stripe_events, monkeypatch
    ):
        seen: dict = {}

        def _capture(api_key, created_gte, **kwargs):
            seen["created_gte"] = created_gte
            return []

        monkeypatch.setattr(se, "fetch_events", _capture)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            se.set_setting("stripe_last_sync_at", datetime.now(UTC).isoformat())
            db.session.commit()

        logged_in.post("/activity/eventos/sync", data={"full_backfill": "1"})
        assert (datetime.now(UTC) - seen["created_gte"]) > timedelta(days=29)

    def test_key_mode_badge_is_shown(self, app, logged_in):
        """A masked key hides which account it reads; the prefix must not."""
        with app.app_context():
            se.set_setting("stripe_api_key", "rk_live_secret")
            db.session.commit()
        assert b"LIVE mode key" in logged_in.get("/activity/eventos").data

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_secret")
            db.session.commit()
        assert b"Test / sandbox key" in logged_in.get("/activity/eventos").data

    def test_grid_filters_out_test_mode_by_default(
        self, app, logged_in, clean_stripe_events
    ):
        """Sandbox events share the table; they must never read as real money."""
        with app.app_context():
            db.session.add(
                StripeEvent(
                    stripe_event_id="evt_sandbox_only",
                    type="payment_intent.succeeded",
                    category="payment",
                    severity="info",
                    created_at_stripe=datetime.now(UTC),
                    livemode=False,
                    customer_email="sandbox@example.com",
                )
            )
            db.session.commit()

        live = logged_in.get("/activity/eventos/grid?livemode=true")
        assert b"sandbox@example.com" not in live.data

        test = logged_in.get("/activity/eventos/grid?livemode=false")
        assert b"sandbox@example.com" in test.data

        both = logged_in.get("/activity/eventos/grid?livemode=all")
        assert b"sandbox@example.com" in both.data

    def test_both_mode_counts_match_the_table(
        self, app, logged_in, clean_stripe_events
    ):
        """`livemode=all` must widen the summary cards too, not just the grid.

        Filtering the cards to test-mode while the table showed both would put
        two contradictory numbers on one screen.
        """
        with app.app_context():
            for event_id, live in (("evt_both_live", True), ("evt_both_test", False)):
                db.session.add(
                    StripeEvent(
                        stripe_event_id=event_id,
                        type="payment_intent.succeeded",
                        category="payment",
                        severity="info",
                        created_at_stripe=datetime.now(UTC),
                        livemode=live,
                    )
                )
            db.session.commit()

        response = logged_in.get("/activity/eventos?livemode=all")
        assert response.status_code == 200
        # Both rows counted: the "Payments OK" card must read 2, not 1.
        assert b">2<" in response.data

    def test_saving_settings_does_not_wipe_the_key_when_left_blank(
        self, app, logged_in
    ):
        """The masked field is submitted empty on every save — it must not clear."""
        with app.app_context():
            se.set_setting("stripe_api_key", "rk_live_keepme")
            db.session.commit()

        logged_in.post(
            "/activity/eventos/settings",
            data={"stripe_api_key": "", "stripe_sync_enabled": "on"},
        )

        with app.app_context():
            assert se.get_setting("stripe_api_key") == "rk_live_keepme"
            se.set_setting("stripe_api_key", None)
            se.set_setting("stripe_sync_enabled", "false")
            db.session.commit()


class TestRefundNotifications:
    """Alerts fire once per refund, from the pass that actually stored it."""

    def test_a_new_refund_alerts_once(self, app, clean_stripe_events, monkeypatch):
        events = [
            _event(
                "charge.refunded",
                {
                    "id": "ch_refunded",
                    "amount_refunded": 20000,
                    "currency": "mxn",
                    "billing_details": {"email": "buyer@example.com"},
                },
                id="evt_refund_1",
            )
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            with patch("app.services.notifications.notify") as mock_notify:
                se.sync_stripe_events(force=True)

            assert mock_notify.call_count == 1
            assert mock_notify.call_args.kwargs["event_type"] == "stripe_refund"

    def test_the_same_refund_does_not_alert_again(
        self, app, clean_stripe_events, monkeypatch
    ):
        """The rolling window re-reads events for many ticks after they land.

        Alerting on anything but a fresh insert would re-announce the same
        refund every few minutes until it aged out of the window.
        """
        events = [
            _event(
                "charge.refunded",
                {"id": "ch_again", "amount_refunded": 15000, "currency": "mxn"},
                id="evt_refund_2",
            )
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            se.sync_stripe_events(force=True)
            with patch("app.services.notifications.notify") as mock_notify:
                second = se.sync_stripe_events(force=True)

            assert second["skipped"] == 1
            assert not mock_notify.called

    def test_non_refund_events_do_not_alert(
        self, app, clean_stripe_events, monkeypatch
    ):
        events = [_event("payment_intent.succeeded", {"id": "pi_ok"}, id="evt_ok")]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            with patch("app.services.notifications.notify") as mock_notify:
                se.sync_stripe_events(force=True)

            assert not mock_notify.called

    def test_a_broken_agent_does_not_fail_the_sync(
        self, app, clean_stripe_events, monkeypatch
    ):
        """Rows are already committed when the alert is attempted."""
        events = [
            _event(
                "charge.refunded",
                {"id": "ch_noisy", "amount_refunded": 100, "currency": "mxn"},
                id="evt_refund_3",
            )
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            with patch(
                "app.services.notifications.notify",
                side_effect=RuntimeError("agent down"),
            ):
                summary = se.sync_stripe_events(force=True)

            assert summary["inserted"] == 1
            assert StripeEvent.query.filter_by(stripe_event_id="evt_refund_3").count()


class TestDisputeQueue:
    """The action queue must show one row per DISPUTE, not one per event.

    Stripe emits up to five events for a single dispute (created, updated,
    closed, funds_withdrawn, funds_reinstated), all carrying the same dispute id
    in ``object_id`` and the same ``evidence_details.due_by``. The queue read
    rows straight out of ``stripe_event``, so one chargeback appeared up to five
    times and ``disputes_open`` counted every copy — a panel titled "Disputes
    awaiting response" showing five deadlines that are all the same one.

    The second half matters more: nothing filtered on the dispute's outcome, so
    a dispute already won or lost stayed in the queue until its response window
    lapsed, telling an operator to answer something that was already settled.
    """

    @staticmethod
    def _dispute_event(
        event_id: str,
        *,
        dispute_id: str,
        event_type: str,
        created: datetime,
        due_by: datetime,
        status: str | None = None,
    ) -> StripeEvent:
        return StripeEvent(
            stripe_event_id=event_id,
            type=event_type,
            category="dispute",
            severity="critical",
            created_at_stripe=created,
            livemode=True,
            object_id=dispute_id,
            charge_id="ch_queue",
            customer_email="queue@example.com",
            amount=29900,
            currency="mxn",
            status=status,
            dispute_reason="fraudulent",
            dispute_due_by=due_by,
            payload=json.dumps({"id": event_id}),
        )

    def _queue(self, app):
        from app.activity.api.blueprint import _open_disputes

        with app.app_context():
            return _open_disputes(StripeEvent.query)

    def test_five_events_for_one_dispute_are_one_row(self, app, clean_stripe_events):
        now = datetime.now(UTC)
        due = now + timedelta(days=5)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        f"evt_dq_{i}",
                        dispute_id="dp_same",
                        event_type=event_type,
                        created=now - timedelta(hours=5 - i),
                        due_by=due,
                        status="needs_response",
                    )
                    for i, event_type in enumerate(
                        [
                            "charge.dispute.created",
                            "charge.dispute.updated",
                            "charge.dispute.funds_withdrawn",
                            "charge.dispute.updated",
                            "charge.dispute.updated",
                        ]
                    )
                ]
            )
            db.session.commit()

        queue = self._queue(app)

        assert len(queue) == 1
        assert queue[0].object_id == "dp_same"

    def test_the_row_kept_is_the_most_recent_event(self, app, clean_stripe_events):
        """The row links to an event detail page — it must show the latest state."""
        now = datetime.now(UTC)
        due = now + timedelta(days=5)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        "evt_dq_old",
                        dispute_id="dp_one",
                        event_type="charge.dispute.created",
                        created=now - timedelta(days=2),
                        due_by=due,
                        status="needs_response",
                    ),
                    self._dispute_event(
                        "evt_dq_new",
                        dispute_id="dp_one",
                        event_type="charge.dispute.updated",
                        created=now - timedelta(minutes=5),
                        due_by=due,
                        status="under_review",
                    ),
                ]
            )
            db.session.commit()

        queue = self._queue(app)

        assert len(queue) == 1
        assert queue[0].stripe_event_id == "evt_dq_new"

    def test_distinct_disputes_are_all_listed(self, app, clean_stripe_events):
        now = datetime.now(UTC)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        "evt_dq_a",
                        dispute_id="dp_a",
                        event_type="charge.dispute.created",
                        created=now,
                        due_by=now + timedelta(days=9),
                        status="needs_response",
                    ),
                    self._dispute_event(
                        "evt_dq_b",
                        dispute_id="dp_b",
                        event_type="charge.dispute.created",
                        created=now,
                        due_by=now + timedelta(days=2),
                        status="needs_response",
                    ),
                ]
            )
            db.session.commit()

        queue = self._queue(app)

        # Still ordered by deadline: the one running out first comes first.
        assert [d.object_id for d in queue] == ["dp_b", "dp_a"]

    def test_a_settled_dispute_leaves_the_queue(self, app, clean_stripe_events):
        """Won or lost means there is nothing left to answer."""
        now = datetime.now(UTC)
        due = now + timedelta(days=5)
        for outcome in ("won", "lost", "warning_closed"):
            with app.app_context():
                StripeEvent.query.delete()
                db.session.add_all(
                    [
                        self._dispute_event(
                            "evt_dq_open",
                            dispute_id="dp_settled",
                            event_type="charge.dispute.created",
                            created=now - timedelta(days=3),
                            due_by=due,
                            status="needs_response",
                        ),
                        self._dispute_event(
                            "evt_dq_closed",
                            dispute_id="dp_settled",
                            event_type="charge.dispute.closed",
                            created=now,
                            due_by=due,
                            status=outcome,
                        ),
                    ]
                )
                db.session.commit()

            assert self._queue(app) == [], f"{outcome} should leave the queue"

    def test_a_dispute_under_review_stays(self, app, clean_stripe_events):
        """Evidence submitted is not the same as resolved — keep it visible."""
        now = datetime.now(UTC)
        with app.app_context():
            db.session.add(
                self._dispute_event(
                    "evt_dq_review",
                    dispute_id="dp_review",
                    event_type="charge.dispute.updated",
                    created=now,
                    due_by=now + timedelta(days=4),
                    status="under_review",
                )
            )
            db.session.commit()

        assert len(self._queue(app)) == 1

    def test_the_summary_card_counts_disputes_not_events(
        self, app, clean_stripe_events
    ):
        """ "Disputes: 5" beside a queue of 1 is the same lie in a louder place.

        Unlike the queue, this counts every dispute in the window — settled or
        not, deadline passed or not. It is a summary, not an action list.
        """
        from app.activity.api.blueprint import _dispute_count

        now = datetime.now(UTC)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        f"evt_dc_{i}",
                        dispute_id=dispute_id,
                        event_type="charge.dispute.updated",
                        created=now,
                        due_by=now + timedelta(days=3),
                        status="needs_response",
                    )
                    for i, dispute_id in enumerate(
                        ["dp_x", "dp_x", "dp_x", "dp_y", None, None]
                    )
                ]
            )
            db.session.commit()

            # dp_x once, dp_y once, and the two id-less rows counted separately
            # rather than collapsed into one.
            assert _dispute_count(StripeEvent.query) == 4

    def test_the_card_ignores_non_dispute_events(self, app, clean_stripe_events):
        now = datetime.now(UTC)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        "evt_dc_real",
                        dispute_id="dp_real",
                        event_type="charge.dispute.created",
                        created=now,
                        due_by=now + timedelta(days=3),
                        status="needs_response",
                    ),
                    StripeEvent(
                        stripe_event_id="evt_dc_refund",
                        type="charge.refunded",
                        category="refund",
                        severity="warning",
                        created_at_stripe=now,
                        livemode=True,
                        object_id="ch_refund",
                        payload=json.dumps({"id": "evt_dc_refund"}),
                    ),
                ]
            )
            db.session.commit()

            from app.activity.api.blueprint import _dispute_count

            assert _dispute_count(StripeEvent.query) == 1

    def test_a_dispute_with_no_id_is_never_hidden(self, app, clean_stripe_events):
        """Grouping must not swallow rows that cannot be grouped.

        object_id is nullable, and losing a chargeback because extraction
        drifted would be the worst possible outcome of a de-duplication fix.
        """
        now = datetime.now(UTC)
        with app.app_context():
            db.session.add_all(
                [
                    self._dispute_event(
                        "evt_dq_null1",
                        dispute_id=None,
                        event_type="charge.dispute.created",
                        created=now,
                        due_by=now + timedelta(days=3),
                        status="needs_response",
                    ),
                    self._dispute_event(
                        "evt_dq_null2",
                        dispute_id=None,
                        event_type="charge.dispute.created",
                        created=now,
                        due_by=now + timedelta(days=6),
                        status="needs_response",
                    ),
                ]
            )
            db.session.commit()

        assert len(self._queue(app)) == 2


# ---------------------------------------------------------------- dispute alerts


class TestDisputeAlerts:
    """A dispute goes unanswered by default, so silence here costs money.

    The alert exists to carry two things the tab cannot push: that a deadline
    started, and how strong the evidence behind it is. Both are only knowable
    after correlation has tied the event to an invitation, which is why the
    ordering test below is the one that matters most.
    """

    @pytest.fixture
    def alerts(self, monkeypatch):
        sent: list[dict] = []

        def _fake_notify(title, message, tags, event_type="user_joined", **kwargs):
            sent.append({"title": title, "message": message, "event_type": event_type})

        monkeypatch.setattr("app.services.notifications.notify", _fake_notify)
        return sent

    def _dispute_row(self, **overrides) -> StripeEvent:
        fields = {
            "stripe_event_id": "evt_dp_new",
            "type": "charge.dispute.created",
            "category": "dispute",
            "severity": "critical",
            "created_at_stripe": datetime.now(UTC),
            "livemode": True,
            "object_id": "dp_1",
            "charge_id": "ch_1",
            "payment_intent_id": "pi_1",
            "customer_email": "buyer@example.com",
            "amount": 15000,
            "currency": "mxn",
            "dispute_reason": "fraudulent",
            "dispute_due_by": datetime.now(UTC) + timedelta(days=8),
            "network_reason_code": "10.4",
        }
        fields.update(overrides)
        return StripeEvent(**fields)

    def test_only_the_three_actionable_types_are_handed_back_for_alerting(
        self, app, clean_stripe_events, monkeypatch
    ):
        """`updated` and `funds_withdrawn` are bookkeeping on a known dispute.

        Alerting on all five dispute events would page four times per chargeback,
        which is how an operator learns to swipe the channel away.
        """
        events = [
            _event("charge.dispute.created", {"id": "dp_1"}, id="evt_a"),
            _event("charge.dispute.updated", {"id": "dp_1"}, id="evt_b"),
            _event("charge.dispute.funds_withdrawn", {"id": "dp_1"}, id="evt_c"),
            _event(
                "charge.dispute.closed", {"id": "dp_1", "status": "lost"}, id="evt_d"
            ),
            _event("radar.early_fraud_warning.created", {"id": "efw_1"}, id="evt_e"),
            _event("charge.refunded", {"id": "ch_9"}, id="evt_f"),
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            summary = se.sync_stripe_events(force=True)

        assert sorted(summary["alertable_event_ids"]) == ["evt_a", "evt_d", "evt_e"]

    def test_a_redelivered_dispute_does_not_alert_twice(
        self, app, clean_stripe_events, monkeypatch
    ):
        """The polling window overlaps, so the same dispute is re-read for hours."""
        events = [_event("charge.dispute.created", {"id": "dp_1"}, id="evt_a")]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()

            first = se.sync_stripe_events(force=True)
            second = se.sync_stripe_events(force=True)

        assert first["alertable_event_ids"] == ["evt_a"]
        assert second["alertable_event_ids"] == []

    def test_correlation_runs_before_the_alert_is_composed(
        self, app, clean_stripe_events, monkeypatch, alerts
    ):
        """The trap this whole design exists to avoid.

        `_notify_new_refunds` fires inside sync_stripe_events, before anything is
        correlated. A dispute alert built there would report a purchase with
        months of history as "not linked to any sauron account" — wrong in the
        exact direction that teaches people to distrust the alerts.
        """
        events = [
            _event(
                "charge.dispute.created",
                {"id": "dp_1"},
                id="evt_a",
                created=int(datetime.now(UTC).timestamp()),
            )
        ]
        monkeypatch.setattr(se, "fetch_events", lambda *a, **k: events)

        order: list[str] = []

        def _fake_correlate(*args, provenance=None, **kwargs):
            order.append("correlate")
            invitation = Invitation(code="ORDER1", used=True)
            db.session.add(invitation)
            db.session.flush()
            row = StripeEvent.query.filter_by(stripe_event_id="evt_a").one()
            row.invitation_id = invitation.id
            db.session.commit()
            if provenance is not None:
                provenance["evt_a"] = sev.PROVENANCE_METADATA
            return 1

        real_notify = sev.notify_new_disputes

        def _spy_notify(ids, provenance=None):
            order.append("notify")
            return real_notify(ids, provenance=provenance)

        monkeypatch.setattr(sev, "resolve_pending_links", _fake_correlate)
        monkeypatch.setattr(sev, "notify_new_disputes", _spy_notify)

        with app.app_context():
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.commit()
            se.sync_and_correlate(force=True)

        assert order == ["correlate", "notify"]
        # And the proof it mattered: the link the alert reports is the one
        # correlation established, which did not exist when the row was stored.
        assert "authoritative" in alerts[0]["message"]

    def test_an_empty_packet_says_so_instead_of_implying_evidence(
        self, app, clean_stripe_events, alerts
    ):
        """Answering a dispute with an empty access log is worse than late."""
        with app.app_context():
            db.session.add(self._dispute_row())
            db.session.commit()

            sev.notify_new_disputes(["evt_dp_new"])

        assert len(alerts) == 1
        assert "NO playback recorded" in alerts[0]["message"]
        assert alerts[0]["event_type"] == "stripe_dispute_opened"

    def test_the_alert_carries_the_deadline_and_the_ce3_flag(
        self, app, clean_stripe_events, alerts
    ):
        with app.app_context():
            db.session.add(self._dispute_row())
            db.session.commit()

            sev.notify_new_disputes(["evt_dp_new"])

        message = alerts[0]["message"]
        assert "due by" in message
        assert "150.00 MXN" in message
        # The row carries 10.4 but no Stripe verdict, so the alert names CE 3.0
        # while saying plainly that Stripe has not granted it. Promising the
        # strongest defence off the reason code alone is what sent an operator
        # after a remedy Stripe had already ruled out.
        assert "Compelling Evidence 3.0" in message
        assert "NOT marked this dispute eligible" in message

    def _linked_user(self) -> int:
        server = MediaServer(name="jf-al", server_type="jellyfin", url="http://al")
        db.session.add(server)
        db.session.flush()
        user = User(
            username="linked",
            email="buyer@example.com",
            code="ALERT1",
            token="tok-alert-1",
            server_id=server.id,
        )
        db.session.add(user)
        db.session.flush()
        return user.id

    def test_an_email_only_match_is_flagged_as_a_guess(
        self, app, clean_stripe_events, alerts
    ):
        """Correlation's last resort matches the BILLING email, which need not be
        the account that redeemed the invite. The alert must not present that as
        settled fact."""
        with app.app_context():
            db.session.add(self._dispute_row(wizarr_user_id=self._linked_user()))
            db.session.commit()

            sev.notify_new_disputes(
                ["evt_dp_new"], provenance={"evt_dp_new": sev.PROVENANCE_EMAIL}
            )

        assert "checkout email only" in alerts[0]["message"]
        assert "authoritative" not in alerts[0]["message"]

    def test_a_metadata_match_is_not_slandered_as_a_guess(
        self, app, clean_stripe_events, alerts
    ):
        """The shape every real dispute actually has, and the trap in reading it
        off the row.

        The storefront stamps `sauronUserId` on the PaymentIntent and no
        invitation id, so the AUTHORITATIVE path writes `wizarr_user_id` and
        leaves `invitation_id` NULL — byte for byte what the email fallback
        leaves behind. Judging by the columns would therefore label every single
        production dispute a guess: the warning inverted, on the alert that most
        needs to be trusted.
        """
        with app.app_context():
            db.session.add(self._dispute_row(wizarr_user_id=self._linked_user()))
            db.session.commit()

            sev.notify_new_disputes(
                ["evt_dp_new"], provenance={"evt_dp_new": sev.PROVENANCE_METADATA}
            )

        message = alerts[0]["message"]
        assert "authoritative" in message
        assert "email only" not in message

    def test_correlation_records_how_it_resolved_each_event(
        self, app, clean_stripe_events, monkeypatch
    ):
        """End to end: the provenance the alert relies on is really written."""
        provenance: dict[str, str] = {}
        with app.app_context():
            metadata_user_id = self._linked_user()
            monkeypatch.setattr(
                sev,
                "fetch_payment_intent",
                lambda *a, **k: {"metadata": {"sauronUserId": str(metadata_user_id)}},
            )
            se.set_setting("stripe_api_key", "rk_test_x")
            db.session.add(
                StripeEvent(
                    stripe_event_id="evt_prov",
                    type="charge.dispute.created",
                    category="dispute",
                    severity="critical",
                    created_at_stripe=datetime.now(UTC),
                    livemode=True,
                    payment_intent_id="pi_prov",
                )
            )
            db.session.commit()

            sev.resolve_pending_links(provenance=provenance)

            row = StripeEvent.query.filter_by(stripe_event_id="evt_prov").one()
            # Exactly the production shape: user set, invitation NULL.
            assert row.wizarr_user_id == metadata_user_id
            assert row.invitation_id is None

        assert provenance["evt_prov"] == sev.PROVENANCE_METADATA

    def test_a_closed_dispute_reports_the_outcome(
        self, app, clean_stripe_events, alerts
    ):
        with app.app_context():
            db.session.add(
                self._dispute_row(
                    stripe_event_id="evt_dp_lost",
                    type="charge.dispute.closed",
                    status="lost",
                    severity="error",
                )
            )
            db.session.commit()

            sev.notify_new_disputes(["evt_dp_lost"])

        assert alerts[0]["event_type"] == "stripe_dispute_closed"
        assert "LOST" in alerts[0]["message"]
        # A lost dispute on a still-live account is the worst of both.
        assert "revoked" in alerts[0]["message"]
        # A verdict is not a task: the window is gone, so telling the operator to
        # weigh the evidence "before answering" would be an instruction they
        # cannot act on, attached to the one alert that most needs to be read.
        assert "before answering" not in alerts[0]["message"]
        assert "does not submit anything" not in alerts[0]["message"]

    def test_a_fraud_warning_points_at_the_refund_window(
        self, app, clean_stripe_events, alerts
    ):
        """Refunding inside the window stops the chargeback existing at all."""
        with app.app_context():
            db.session.add(
                self._dispute_row(
                    stripe_event_id="evt_efw",
                    type="radar.early_fraud_warning.created",
                    category="fraud",
                    dispute_reason="made_with_stolen_card",
                    dispute_due_by=None,
                    network_reason_code=None,
                )
            )
            db.session.commit()

            sev.notify_new_disputes(["evt_efw"])

        assert alerts[0]["event_type"] == "stripe_fraud_warning"
        assert "prevents the chargeback" in alerts[0]["message"]

    def test_the_link_is_absolute_when_the_public_url_is_configured(
        self, app, clean_stripe_events, alerts
    ):
        """These alerts are sent from the scheduler, where there is no request to
        infer a host from — and sauron sits behind a proxy whose internal address
        would produce a link that works for nobody."""
        with app.app_context():
            se.set_setting("resend_public_base_url", "https://sauron.example.net")
            db.session.add(self._dispute_row())
            db.session.commit()
            row = StripeEvent.query.filter_by(stripe_event_id="evt_dp_new").one()
            event_id = row.id

            sev.notify_new_disputes(["evt_dp_new"])

        assert (
            f"https://sauron.example.net/activity/eventos/{event_id}"
            in alerts[0]["message"]
        )

    def test_without_a_public_url_the_alert_still_names_the_page(
        self, app, clean_stripe_events, alerts
    ):
        with app.app_context():
            se.set_setting("resend_public_base_url", None)
            db.session.add(self._dispute_row())
            db.session.commit()
            row = StripeEvent.query.filter_by(stripe_event_id="evt_dp_new").one()
            event_id = row.id

            sev.notify_new_disputes(["evt_dp_new"])

        assert f"/activity/eventos/{event_id}" in alerts[0]["message"]

    def test_a_broken_packet_still_produces_an_alert(
        self, app, clean_stripe_events, alerts, monkeypatch
    ):
        """The deadline is real whether or not sauron can describe the case."""
        monkeypatch.setattr(
            sev,
            "build_evidence_packet",
            lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with app.app_context():
            db.session.add(self._dispute_row())
            db.session.commit()

            sent = sev.notify_new_disputes(["evt_dp_new"])

        assert sent == 1
        assert "could not be built" in alerts[0]["message"]

    def test_one_dead_notifier_does_not_swallow_the_sync(
        self, app, clean_stripe_events, monkeypatch
    ):
        """Rows are already committed; a down Telegram must not undo that."""

        def _explode(*args, **kwargs):
            raise RuntimeError("telegram unreachable")

        monkeypatch.setattr("app.services.notifications.notify", _explode)

        with app.app_context():
            db.session.add(self._dispute_row())
            db.session.commit()

            assert sev.notify_new_disputes(["evt_dp_new"]) == 0
            assert (
                StripeEvent.query.filter_by(stripe_event_id="evt_dp_new").count() == 1
            )

    def test_a_backfilled_dispute_does_not_page_anyone(
        self, app, clean_stripe_events, alerts
    ):
        """The first sync of an install reaches back 30 days, and "Re-sync last
        30 days" does it on demand. Newly STORED is not newly HAPPENED, and an
        inbox full of settled cases on day one teaches the operator to mute the
        channel before it ever matters."""
        with app.app_context():
            db.session.add(
                self._dispute_row(
                    created_at_stripe=datetime.now(UTC) - timedelta(days=20)
                )
            )
            db.session.commit()

            assert sev.notify_new_disputes(["evt_dp_new"]) == 0

        assert alerts == []

    def test_a_dispute_from_yesterday_still_pages(
        self, app, clean_stripe_events, alerts
    ):
        """The cutoff has to survive a weekend outage, not just the happy path."""
        with app.app_context():
            db.session.add(
                self._dispute_row(
                    created_at_stripe=datetime.now(UTC) - timedelta(days=1)
                )
            )
            db.session.commit()

            assert sev.notify_new_disputes(["evt_dp_new"]) == 1

        assert len(alerts) == 1

    def test_nothing_calls_sync_without_going_through_the_pipeline(self):
        """Source-level invariant, because no runtime test can catch the drift.

        A new caller reaching for `sync_stripe_events` directly gets rows stored
        and no correlation and no alerts — and because ingestion is idempotent,
        the NEXT pass sees those rows as already known and never alerts either.
        The dispute vanishes silently, which is the failure this whole module
        exists to prevent. `sync_and_correlate` is the only supported entry.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "app"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "sync_stripe_events(" not in line:
                    continue
                if "def sync_stripe_events(" in line:
                    continue
                if path.name == "stripe_events.py":
                    continue  # sync_and_correlate itself
                offenders.append(f"{path.relative_to(root)}:{number}")

        assert not offenders, (
            "call sync_and_correlate() instead of sync_stripe_events(): "
            + ", ".join(offenders)
        )

    def test_the_dispute_events_reach_agents_that_never_opted_in(self):
        """Operational, like the stalled-sync alert.

        Subscription is opt-in and agent rows keep whatever was saved when they
        were created, so a newly added subscribable event is born mute. For a
        chargeback deadline that failure mode is not acceptable.
        """
        from app.services.notification_events import is_operational

        for key in (
            "stripe_dispute_opened",
            "stripe_dispute_closed",
            "stripe_fraud_warning",
        ):
            assert is_operational(key), key


class TestWatchTimeHonesty:
    """The number that goes to a card issuer must be one we actually measured.

    Reproduces the 2026-08-27 finding: a live session reported "1h 26m watched"
    while the player had moved 13 seconds. ``ActivitySession.duration_ms`` holds
    the FILE RUNTIME for any session that has not ended yet — only the
    ``session_end`` branch of the collectors swaps in the playback position — so
    summing it blindly overstates use by orders of magnitude.
    """

    @pytest.fixture
    def live_playback(self, app, clean_stripe_events):
        """One session still playing: runtime 87 min, position 13 s."""
        with app.app_context():
            server = MediaServer(name="jf-live", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()

            invitation = Invitation(code="LIVEWATCH", used=True)
            user = User(
                username="watcher",
                email="watcher@example.com",
                code="LIVEWATCH",
                token="tok-live",
                server_id=server.id,
            )
            db.session.add_all([invitation, user])
            db.session.flush()
            invitation.users.append(user)

            started = datetime(2026, 8, 27, 2, 7, tzinfo=UTC)
            session = ActivitySession(
                server_id=server.id,
                session_id="live-1",
                user_name="watcher",
                media_title="The End of Evangelion",
                started_at=started,
                # RunTimeTicks of the title, which is what the collector stores
                # while the session is still open. 87 minutes.
                duration_ms=5_220_000,
                ip_address="189.10.0.1",
                device_name="MacBookPro18 1",
                wizarr_user_id=user.id,
                active=True,
            )
            db.session.add(session)
            db.session.flush()

            # What the player actually reported: 13 seconds in.
            for offset, position in ((0, 0), (30, 6_000), (90, 13_000)):
                db.session.add(
                    ActivitySnapshot(
                        session_id=session.id,
                        timestamp=started + timedelta(seconds=offset),
                        position_ms=position,
                        state="playing",
                    )
                )

            event = StripeEvent(
                stripe_event_id="evt_live_watch",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=started + timedelta(days=3),
                livemode=True,
                object_id="dp_live",
                charge_id="ch_live",
                payment_intent_id="pi_live",
                customer_email="watcher@example.com",
                amount=15000,
                currency="mxn",
                network_reason_code="10.4",
                invitation_id=invitation.id,
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()
            yield event.id

            StripeEvent.query.delete()
            ActivitySnapshot.query.delete()
            ActivitySession.query.delete()
            db.session.delete(user)
            db.session.delete(invitation)
            db.session.delete(server)
            db.session.commit()

    def test_live_session_reports_position_not_file_runtime(self, app, live_playback):
        """13 seconds played must not be rendered as an hour and a half."""
        with app.app_context():
            event = db.session.get(StripeEvent, live_playback)
            log = sev.build_access_activity_log(event)

            assert "1h 27m" not in log
            assert "1h 26m" not in log
            assert "5220000" not in log

    def test_packet_watch_time_comes_from_the_snapshot(self, app, live_playback):
        with app.app_context():
            event = db.session.get(StripeEvent, live_playback)
            packet = sev.build_evidence_packet(event)

            # 13 s rounds to under a minute; the file runtime would be 1h 27m.
            assert packet["furthest_position"] not in ("1h 27m", "1h 26m")

    def test_label_says_position_reached_not_watch_time(self, app, live_playback):
        """`position_ms` is how far the player got, not time accumulated.

        Seeking forward inflates it and a rewatch does not add to it, so calling
        it "watch time" claims a precision we do not have.
        """
        with app.app_context():
            event = db.session.get(StripeEvent, live_playback)
            log = sev.build_access_activity_log(event)

            assert "Total watch time" not in log
            assert "Furthest playback position" in log

    def test_no_position_data_omits_the_line_rather_than_guessing(
        self, app, clean_stripe_events
    ):
        """An absent line costs nothing; a fabricated one loses the dispute.

        Same rule `_payment_time` already applies to time-to-first-use.
        """
        with app.app_context():
            server = MediaServer(name="jf-np", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()

            user = User(
                username="noposition",
                email="np@example.com",
                code="NOPOS1",
                token="tok-np",
                server_id=server.id,
            )
            db.session.add(user)
            db.session.flush()

            db.session.add(
                ActivitySession(
                    server_id=server.id,
                    session_id="np-1",
                    user_name="noposition",
                    media_title="Solaris",
                    started_at=datetime(2026, 8, 27, 2, 7, tzinfo=UTC),
                    duration_ms=9_000_000,  # runtime only, no snapshots at all
                    wizarr_user_id=user.id,
                    active=True,
                )
            )

            event = StripeEvent(
                stripe_event_id="evt_no_position",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=datetime(2026, 8, 30, tzinfo=UTC),
                livemode=True,
                object_id="dp_np",
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()

            log = sev.build_access_activity_log(event)
            assert "2h 30m" not in log
            assert "Furthest playback position" not in log

            StripeEvent.query.delete()
            ActivitySession.query.delete()
            db.session.delete(user)
            db.session.delete(server)
            db.session.commit()

    def test_ended_session_without_snapshots_still_counts(
        self, app, clean_stripe_events
    ):
        """Closed sessions keep a trustworthy `duration_ms`.

        The collectors' `session_end` branch already swaps the file runtime for
        the playback position, so the stored value is the measured one. Dropping
        it would throw away the evidence this module exists to produce — and
        every historical import lands this way.
        """
        with app.app_context():
            server = MediaServer(name="jf-end", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()

            user = User(
                username="finished",
                email="fin@example.com",
                code="FIN1",
                token="tok-fin",
                server_id=server.id,
            )
            db.session.add(user)
            db.session.flush()

            db.session.add(
                ActivitySession(
                    server_id=server.id,
                    session_id="end-1",
                    user_name="finished",
                    media_title="Stalker",
                    started_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
                    duration_ms=3_600_000,
                    wizarr_user_id=user.id,
                    active=False,
                )
            )

            event = StripeEvent(
                stripe_event_id="evt_ended",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=datetime(2026, 8, 30, tzinfo=UTC),
                livemode=True,
                object_id="dp_end",
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()

            log = sev.build_access_activity_log(event)
            assert "1h 00m" in log

            StripeEvent.query.delete()
            ActivitySession.query.delete()
            db.session.delete(user)
            db.session.delete(server)
            db.session.commit()


class TestCE3MatchingElements:
    """What may honestly be offered as a Visa CE 3.0 matching element.

    CE 3.0 asks the disputed transaction and two prior undisputed ones to agree
    on the customer's purchase IP. The address Jellyfin reports is the one it
    sees behind the proxy — on 2026-08-27 that was `172.16.10.1`, while the IP
    Stripe recorded for the same purchase was `201.156.50.146`. An RFC 1918
    address cannot match anything Stripe ever saw, so offering it under
    "matching elements" promises a coincidence that cannot happen.
    """

    @pytest.fixture
    def dispute_with_ips(self, app, clean_stripe_events):
        def _make(ips: list[str]):
            server = MediaServer(name="jf-ip", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()

            user = User(
                username="ipuser",
                email="ip@example.com",
                code="IPCODE1",
                token="tok-ip",
                server_id=server.id,
            )
            db.session.add(user)
            db.session.flush()

            for index, ip in enumerate(ips):
                db.session.add(
                    ActivitySession(
                        server_id=server.id,
                        session_id=f"ip-{index}",
                        user_name="ipuser",
                        media_title="Solaris",
                        started_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
                        duration_ms=3_600_000,
                        ip_address=ip,
                        device_name="Android TV",
                        wizarr_user_id=user.id,
                        active=False,
                    )
                )

            event = StripeEvent(
                stripe_event_id="evt_ce3_ip",
                type="charge.dispute.created",
                category="dispute",
                severity="critical",
                created_at_stripe=datetime(2026, 8, 30, tzinfo=UTC),
                livemode=True,
                object_id="dp_ip",
                network_reason_code="10.4",
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()
            return event.id

        with app.app_context():
            yield _make

            StripeEvent.query.delete()
            ActivitySession.query.delete()
            User.query.filter_by(username="ipuser").delete()
            MediaServer.query.filter_by(name="jf-ip").delete()
            db.session.commit()

    def test_private_ip_is_not_offered_as_a_matching_element(
        self, app, dispute_with_ips
    ):
        event_id = dispute_with_ips(["172.16.10.1"])
        event = db.session.get(StripeEvent, event_id)

        ce3 = sev.build_ce3_elements(event)

        assert ce3["customer_purchase_ip"] == []

    def test_private_ip_is_still_reported_as_what_the_server_saw(
        self, app, dispute_with_ips
    ):
        """It is true, and it belongs in the narrative — just not as a match."""
        event_id = dispute_with_ips(["172.16.10.1"])
        event = db.session.get(StripeEvent, event_id)

        ce3 = sev.build_ce3_elements(event)
        log = sev.build_access_activity_log(event)

        assert ce3["server_observed_ip"] == ["172.16.10.1"]
        assert "172.16.10.1" in log

    def test_public_ip_still_qualifies(self, app, dispute_with_ips):
        event_id = dispute_with_ips(["201.156.50.146"])
        event = db.session.get(StripeEvent, event_id)

        ce3 = sev.build_ce3_elements(event)

        assert ce3["customer_purchase_ip"] == ["201.156.50.146"]
        assert ce3["server_observed_ip"] == []

    @pytest.mark.parametrize(
        "private",
        ["10.0.0.4", "192.168.1.30", "172.31.255.1", "127.0.0.1", "::1", "fd00::1"],
    )
    def test_every_private_range_is_excluded(self, app, dispute_with_ips, private):
        event_id = dispute_with_ips([private, "201.156.50.146"])
        event = db.session.get(StripeEvent, event_id)

        ce3 = sev.build_ce3_elements(event)

        assert ce3["customer_purchase_ip"] == ["201.156.50.146"]
        assert private in ce3["server_observed_ip"]

    def test_unparseable_address_is_not_promoted_to_a_match(
        self, app, dispute_with_ips
    ):
        """A hostname or a mangled value is not something Stripe can match on."""
        event_id = dispute_with_ips(["not-an-ip"])
        event = db.session.get(StripeEvent, event_id)

        ce3 = sev.build_ce3_elements(event)

        assert ce3["customer_purchase_ip"] == []
        assert ce3["server_observed_ip"] == ["not-an-ip"]


class TestCE3Eligibility:
    """Whether a dispute may be announced as CE 3.0 eligible.

    sauron inferred eligibility from the Visa reason code alone. The real
    criteria also require two prior undisputed transactions on the same payment
    method, 120-364 days old, with matching elements — none of which a new
    customer can have. Stripe answers this directly in
    ``enhanced_eligibility_types``, and on both disputes of the 2026-08-26
    battery it answered with an empty list while sauron's badge said "eligible".

    Announcing the strongest available defence for a dispute Stripe has already
    ruled out sends the operator down a road that does not exist.
    """

    def _dispute(self, *, reason_code="10.4", enhanced=None, event_id="evt_ce3"):
        obj = {
            "id": "dp_ce3",
            "charge": "ch_ce3",
            "payment_intent": "pi_ce3",
            "reason": "fraudulent",
            "status": "needs_response",
            "amount": 15000,
            "currency": "mxn",
            "payment_method_details": {"card": {"network_reason_code": reason_code}},
        }
        if enhanced is not None:
            obj["enhanced_eligibility_types"] = enhanced
        return _event("charge.dispute.created", obj, id=event_id)

    def _store(self, payload):
        """Through the real extractor, so the payload column is the real one."""
        event = StripeEvent(**se.extract_fields(payload))
        db.session.add(event)
        db.session.commit()
        return event

    def test_stripe_confirming_eligibility_is_reported_as_confirmed(
        self, app, clean_stripe_events
    ):
        with app.app_context():
            event = self._store(self._dispute(enhanced=["visa_compelling_evidence_3"]))

            assert sev.ce3_eligibility(event) == "confirmed"

    def test_reason_code_alone_is_not_confirmation(self, app, clean_stripe_events):
        """The exact case observed live: 10.4 with an empty Stripe verdict."""
        with app.app_context():
            event = self._store(self._dispute(enhanced=[]))

            assert sev.ce3_eligibility(event) == "unconfirmed"

    def test_absent_field_is_not_confirmation_either(self, app, clean_stripe_events):
        """Stripe populates this late, or only in livemode. Absent is not yes."""
        with app.app_context():
            event = self._store(self._dispute(enhanced=None))

            assert sev.ce3_eligibility(event) == "unconfirmed"

    def test_other_reason_codes_are_not_applicable(self, app, clean_stripe_events):
        with app.app_context():
            event = self._store(self._dispute(reason_code="13.1"))

            assert sev.ce3_eligibility(event) == "not_applicable"

    def test_alert_does_not_promise_the_strongest_answer_without_stripe(
        self, app, clean_stripe_events
    ):
        """The Telegram copy has to move with the badge, or the fix is half done."""
        with app.app_context():
            event = self._store(self._dispute(enhanced=[]))
            body = sev._dispute_alert_body(event, None)

            assert "strongest answer available" not in body
            assert "10.4" in body
            assert "not marked" in body.lower() or "no marc" in body.lower()

    def test_alert_still_says_strongest_when_stripe_confirms(
        self, app, clean_stripe_events
    ):
        with app.app_context():
            event = self._store(self._dispute(enhanced=["visa_compelling_evidence_3"]))
            body = sev._dispute_alert_body(event, None)

            assert "strongest answer available" in body

    def test_packet_exposes_the_three_states_not_a_boolean(
        self, app, clean_stripe_events
    ):
        with app.app_context():
            event = self._store(self._dispute(enhanced=[]))
            ce3 = sev.build_ce3_elements(event)

            assert ce3["ce3_eligibility"] == "unconfirmed"


class TestCE3BadgeRendering:
    """The badge in the dispute queue is the third surface of one fact.

    A corrected packet and a corrected alert beside a list still shouting
    "CE 3.0 eligible" is the same defect wearing different clothes, so this
    pins the badge to the same verdict the other two read.
    """

    @pytest.fixture
    def logged_in(self, client, app):
        from app.models import AdminAccount

        with app.app_context():
            account = AdminAccount.query.filter_by(username="ce3admin").first()
            created = account is None
            if created:
                account = AdminAccount(username="ce3admin")
                account.set_password("Ce3Pass12345")
                db.session.add(account)
                db.session.commit()
        response = client.post(
            "/login", data={"username": "ce3admin", "password": "Ce3Pass12345"}
        )
        assert response.status_code in {200, 302, 303}
        yield client
        with app.app_context():
            if created:
                db.session.delete(
                    AdminAccount.query.filter_by(username="ce3admin").first()
                )
                db.session.commit()

    def _open_dispute(self, app, enhanced):
        obj = {
            "id": "dp_badge",
            "charge": "ch_badge",
            "payment_intent": "pi_badge",
            "reason": "fraudulent",
            "status": "needs_response",
            "amount": 15000,
            "currency": "mxn",
            "evidence_details": {
                "due_by": int((datetime.now(UTC) + timedelta(days=5)).timestamp())
            },
            "payment_method_details": {"card": {"network_reason_code": "10.4"}},
            "enhanced_eligibility_types": enhanced,
        }
        payload = _event("charge.dispute.created", obj, id="evt_badge")
        with app.app_context():
            StripeEvent.query.delete()
            db.session.add(StripeEvent(**se.extract_fields(payload)))
            db.session.commit()

    def test_badge_absent_when_stripe_has_not_confirmed(
        self, app, logged_in, clean_stripe_events
    ):
        self._open_dispute(app, [])

        body = logged_in.get("/activity/eventos").get_data(as_text=True)

        assert "CE 3.0 eligible" not in body

    def test_badge_shown_when_stripe_confirms(
        self, app, logged_in, clean_stripe_events
    ):
        self._open_dispute(app, ["visa_compelling_evidence_3"])

        body = logged_in.get("/activity/eventos").get_data(as_text=True)

        assert "CE 3.0 eligible" in body


class TestLinkKind:
    """ "Linked to an account" must not be said of an unredeemed invitation.

    Event 26 of the 2026-08-26 battery: the storefront stamped
    ``wizarrInvitationId`` on a purchase whose invitation was created and never
    redeemed. The link is real, but it points at an INVITATION — no account was
    ever created. The packet said "linked to an account, but no playback
    sessions were recorded" and the alert told the operator to "check that the
    account was actually revoked", both about something that never existed.

    And it loses the stronger argument. For a fraud dispute on a signup that was
    never redeemed, the fact worth stating is not "no playback" — it is that no
    access was ever delivered.
    """

    def _event_with(self, *, invitation_id=None, user_id=None, event_type=None):
        event = StripeEvent(
            stripe_event_id=f"evt_link_{invitation_id}_{user_id}",
            type=event_type or "charge.dispute.created",
            category="dispute",
            severity="critical",
            created_at_stripe=datetime(2026, 8, 26, 14, 39, tzinfo=UTC),
            livemode=True,
            object_id="dp_link",
            amount=15000,
            currency="mxn",
            invitation_id=invitation_id,
            wizarr_user_id=user_id,
        )
        db.session.add(event)
        db.session.commit()
        return event

    @pytest.fixture
    def unredeemed(self, app, clean_stripe_events):
        """An invitation that was minted and never used — no account behind it."""
        with app.app_context():
            invitation = Invitation(code="NEVERUSED", used=False)
            db.session.add(invitation)
            db.session.commit()
            yield invitation.id

            StripeEvent.query.delete()
            db.session.delete(db.session.get(Invitation, invitation.id))
            db.session.commit()

    @pytest.fixture
    def redeemed(self, app, clean_stripe_events):
        with app.app_context():
            server = MediaServer(name="jf-lk", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()
            invitation = Invitation(code="WASUSED1", used=True)
            user = User(
                username="realuser",
                email="real@example.com",
                code="WASUSED1",
                token="tok-lk",
                server_id=server.id,
            )
            db.session.add_all([invitation, user])
            db.session.flush()
            invitation.users.append(user)
            db.session.commit()
            yield invitation.id, user.id

            StripeEvent.query.delete()
            db.session.delete(user)
            db.session.delete(invitation)
            db.session.delete(server)
            db.session.commit()

    def test_unredeemed_invitation_is_not_an_account(self, app, unredeemed):
        with app.app_context():
            event = self._event_with(invitation_id=unredeemed)

            packet = sev.build_evidence_packet(event)

            assert packet["link_kind"] == "invitation_unredeemed"

    def test_redeemed_invitation_is_an_account(self, app, redeemed):
        invitation_id, user_id = redeemed
        with app.app_context():
            event = self._event_with(invitation_id=invitation_id, user_id=user_id)

            packet = sev.build_evidence_packet(event)

            assert packet["link_kind"] == "account"

    def test_no_link_at_all(self, app, clean_stripe_events):
        with app.app_context():
            event = self._event_with()

            packet = sev.build_evidence_packet(event)

            assert packet["link_kind"] == "none"

    def test_alert_states_no_access_was_delivered(self, app, unredeemed):
        """The strongest fact available, and the one the copy used to omit."""
        with app.app_context():
            event = self._event_with(invitation_id=unredeemed)
            packet = sev.build_evidence_packet(event)

            text = sev._evidence_text(packet)

            assert "never redeemed" in text.lower()
            assert "no playback recorded" not in text.lower()

    def test_closed_dispute_does_not_ask_to_check_a_nonexistent_account(
        self, app, unredeemed
    ):
        """The exact wrong instruction event 26 produced."""
        with app.app_context():
            event = self._event_with(
                invitation_id=unredeemed, event_type="charge.dispute.closed"
            )
            event.status = "lost"
            db.session.commit()
            packet = sev.build_evidence_packet(event)

            body = sev._dispute_alert_body(event, packet)

            assert "account was actually revoked" not in body

    def test_closed_dispute_still_asks_when_an_account_exists(self, app, redeemed):
        invitation_id, user_id = redeemed
        with app.app_context():
            event = self._event_with(
                invitation_id=invitation_id,
                user_id=user_id,
                event_type="charge.dispute.closed",
            )
            event.status = "lost"
            db.session.commit()
            packet = sev.build_evidence_packet(event)

            body = sev._dispute_alert_body(event, packet)

            assert "account was actually revoked" in body

    def test_view_copy_distinguishes_the_two(self, app, unredeemed):
        """The template must read the field, not re-derive it from the columns.

        Branching on `event.invitation_id or event.wizarr_user_id` is exactly
        the inference that produced the bug.
        """
        template = pathlib.Path(
            "app/activity/templates/activity/_eventos_detail.html"
        ).read_text()

        assert "packet.link_kind" in template
        assert "event.wizarr_user_id or event.invitation_id" not in template


class TestEvidenceEmailFallback:
    """An EFW object carries no email, but the linked account does.

    Event 75 rendered ``Email: -`` while event 73, on the very same purchase,
    had it. Customer email is a SECONDARY CE 3.0 element: in a case with few
    elements it decides between qualifying and not.
    """

    def test_email_falls_back_to_the_linked_account(self, app, clean_stripe_events):
        with app.app_context():
            server = MediaServer(name="jf-em", server_type="jellyfin", url="http://x")
            db.session.add(server)
            db.session.flush()
            user = User(
                username="efwuser",
                email="efw@example.com",
                code="EFWCODE1",
                token="tok-efw",
                server_id=server.id,
            )
            db.session.add(user)
            db.session.flush()

            event = StripeEvent(
                stripe_event_id="evt_efw_email",
                type="radar.early_fraud_warning.created",
                category="dispute",
                severity="critical",
                created_at_stripe=datetime(2026, 8, 27, 2, 2, tzinfo=UTC),
                livemode=True,
                object_id="issfr_1",
                charge_id="ch_efw",
                customer_email=None,
                wizarr_user_id=user.id,
            )
            db.session.add(event)
            db.session.commit()

            ce3 = sev.build_ce3_elements(event)

            assert ce3["customer_email"] == "efw@example.com"

            StripeEvent.query.delete()
            db.session.delete(user)
            db.session.delete(server)
            db.session.commit()
