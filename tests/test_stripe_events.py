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
from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import ActivitySession, Invitation, MediaServer, StripeEvent, User
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

    def test_fraud_warning_resolves_through_its_charge(self, app, clean_stripe_events):
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
            assert sev.resolve_event_links(efw, api_key=None) is True
            assert efw.invitation_id == invitation.id

            db.session.rollback()
            StripeEvent.query.delete()
            db.session.delete(db.session.get(Invitation, invitation.id))
            db.session.commit()

    def test_ce3_elements_expose_the_matching_keys(self, app, purchase):
        with app.app_context():
            event = db.session.get(StripeEvent, purchase)
            ce3 = sev.build_ce3_elements(event)

            assert ce3["customer_purchase_ip"] == ["189.10.0.1", "189.10.0.9"]
            assert ce3["customer_account_ids"] == ["buyer"]
            assert ce3["customer_email"] == "buyer@example.com"
            # Visa 10.4 is the reason code eligible for a CE 3.0 response.
            assert ce3["ce3_eligible_reason_code"] is True

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
