# Activity › Eventos — status and remaining work

**As of 2026-08-24.** Paused deliberately: other work is more urgent. This file
exists so the next session starts from evidence instead of re-investigating.

Scope: the Stripe event mirror (`app/services/stripe_events.py`) and the
dispute-evidence builder (`app/services/stripe_evidence.py`).

---

## 1. What works today (verified in the deployed instance)

Shipped in **v2026.8.4** and **v2026.8.5**:

| Fix | Where |
| --- | --- |
| Changing the API key resets `stripe_last_sync_at`, so the new account backfills | `blueprint.py` `eventos_settings` + `stripe_events.reset_sync_watermark` |
| One-time watermark clear for instances already poisoned | `migrations/versions/20260824_reset_stripe_watermark.py` |
| SAVEPOINT per event — one bad event no longer discards the batch while reporting success | `stripe_events.sync_stripe_events` |
| Sync outcomes no longer collapse into one green "no new events" banner | `blueprint._sync_result_message` |
| "Last sync result" panel, key-mode badge, "Re-sync last 30 days" button | `activity/eventos_tab.html` |
| Tailwind never scanned `app/activity/templates` — all Activity-only classes were dead | `app/static/src/style.css` `@source` |

Confirmed working on the box: 105 events fetched over the 30-day window
(**all test mode, 0 live**), 22 stored — 5 payments OK, 2 failed, 1 refund.
That matches the sandbox exactly.

The original symptom ("37 already known or not monitored", empty tab) **was the
stale watermark**. Closed.

## 2. Ruled out — do not re-investigate

- **Live-mode key.** Live account `acct_1TumprPUvd1op10Y` is dormant: 0 payments,
  0 products, 0 customers. A live key returns 0 events, not 37.
- **Broken migration chain.** `20260824_stripe_events` → `20260823_invite_claim`
  chains correctly; single alembic head.
- **Insert path / schema drift.** Reproduced locally against the real schema with
  three realistic sandbox events — all flush cleanly.
- **UI hiding the rows.** `_default_livemode()` falls back to test mode when only
  test events exist, so an empty tab meant an empty table.

The 37 non-monitored events in the narrow pre-fix window were Connect platform
noise — the same shape the panel still reports as ignored:
`capability.updated ×44, account.updated ×15, payment_intent.created ×7,
charge.updated ×5, balance.available ×5, …`

---

## 3. BLOCKER — the evidence correlation cannot resolve

This is the reason the tab exists, and it is currently running on the weak path
or not resolving at all.

`resolve_event_links` looks for the invitation here
(`app/services/stripe_evidence.py:82`):

```python
raw = metadata.get("wizarrInvitationId")   # read off PaymentIntent.metadata
```

What the storefront (neexy) actually sends — verified on two Checkout Sessions
from different dates, one from production `neexy.net`:

```json
"metadata": {
  "deviceLimit": "2",
  "durationDays": "30",
  "orderToken": "htc81atlFZYLEAWDqzOmoCvQjWvEDB2R",
  "package": "plus"
}
```

Two mismatches at once:

| | sauron expects | neexy sends |
| --- | --- | --- |
| Object | `PaymentIntent.metadata` | `CheckoutSession.metadata` |
| Key | `wizarrInvitationId` | `orderToken` |

Supporting evidence:

- All 7 PaymentIntents inspected in the 30-day window carry `metadata: {}` — empty.
- `orderToken` appears **nowhere** in sauron's source.
- `wizarrInvitationId` appears in exactly **one** line — the one that reads it.
  Nothing writes it, no fixture references it, no doc defines the contract.
- **Zero test coverage** on that branch: the only correlation test
  (`tests/test_stripe_events.py:730`) passes `api_key=None`, which skips the
  PaymentIntent fetch entirely. That is why the suite is green while the contract
  has never existed.

Consequence: every event falls through to step 3, the checkout-email fallback,
which the code itself documents as weaker — it identifies *a person*, not
*a purchase*, and only resolves when exactly one `User.email` matches.

### Open decision (needs neexy, not in this repo)

**Does neexy know the invitation id when it creates the Checkout Session, or is
the invitation created after payment during fulfilment?**

- If it knows it up front → cheapest fix is on neexy: stamp
  `payment_intent_data.metadata.wizarrInvitationId` on the session so it
  propagates to the PaymentIntent, and sauron needs no change.
- If the invitation is created after the charge → `orderToken` is the correct
  bridge. sauron must persist the order token on `Invitation` and resolve through
  it, reading the **Checkout Session**, not the PaymentIntent.

Do not pick one without answering this. Whichever way it goes, the branch needs a
test that actually exercises the metadata path (i.e. with an `api_key`, mocking
`fetch_payment_intent`).

---

## 4. Never exercised: disputes and fraud warnings

Current counters: **Disputes 0, Fraud warnings 0.** The action queue, the dispute
deadline ordering, the CE 3.0 badge and `build_evidence_packet` have never run
against real data.

Official Stripe test cards (from https://docs.stripe.com/testing):

| Purpose | Card | PaymentMethod token |
| --- | --- | --- |
| Dispute, reason `fraudulent` | `4000000000000259` | `pm_card_createDispute` |
| **Visa CE 3.0 eligible** (`network_reason_code` `10.4`) | `4000000404000038` | `pm_card_createCe3EligibleDispute` |
| Early fraud warning | `4000000000005423` | `pm_card_createIssuerFraudRecord` |
| Product not received | `4000000000002685` | `pm_card_createDisputeProductNotReceived` |

The CE 3.0 card is the important one: `eventos_tab.html` and
`_eventos_table.html` both special-case `network_reason_code == "10.4"`, and that
is the only card that produces it.

---

## 5. Execution plan for the next session

1. **Answer the neexy question above**, then implement the correlation fix and a
   test that exercises the metadata path with an api_key.
2. Run the three test-card checkouts in the sandbox, "Sync now", and verify:
   - Disputes / Fraud warnings counters leave 0
   - the action queue orders by `dispute_due_by` and renders `Xd left`
   - the CE 3.0 badge appears for `4000000404000038`
3. **Make the evidence packet non-empty.** A packet with no `ActivitySession`
   rows proves nothing. The disputed payment must resolve to a Jellyfin user who
   actually has playback history — sauron is Jellyfin-only, so `_do_join` is the
   only provisioning path.
   - Shortcut to test the renderer today without touching neexy: run the test
     checkout using the email of an existing sauron user that already has
     playback. The email fallback resolves and the packet renders. This does
     **not** validate the authoritative path.
4. Consider whether `UNMONITORED_SAMPLE_SIZE = 8` should surface a "+N more
   types" hint — the panel currently shows the top 8 of what can be a longer tail
   (105 fetched − 22 monitored = 83 ignored, top 8 accounted for 80).

## 6. Useful context

- Sandbox: `acct_1Tumq0Bg0iQ4ZRR4` ("neexy sandbox"). Live: `acct_1TumprPUvd1op10Y`
  ("neexy"), dormant.
- Key in use is `rk_test_…`, restricted and read-only. Nothing in sauron writes to
  Stripe.
- Stripe retains events for 30 days, so anything older than the window is gone —
  the July payments in the sandbox are already unreachable.
