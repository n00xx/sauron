"""The onboarding video step: the first thing a buyer sees after signing up.

Three things are load-bearing here and none of them is the player markup.

``test_backfill_puts_the_video_first`` and
``test_backfill_pulls_device_setup_up_behind_the_video`` cover the ordering,
which is the entire point: the video narrates what the device-setup page then
walks you through, so those two have to be adjacent and first. The second case
is the one that actually bites, because ``ensure_quick_connect_step`` appends
and therefore left device setup sitting behind the tips page.

``test_backfill_leaves_customised_steps_alone`` guards the other side. This runs
on every startup against a live database whose steps an admin may have edited by
hand; it reorders, and it must never rewrite or drop content.

Note that ``(server_type, category, position)`` is unique and SQLite enforces it
per statement, so any reordering that writes final positions directly collides
with the rows it has not moved yet — it fails with an IntegrityError on a real
database while passing any test that only counts rows.
"""

import pytest

from app.extensions import db
from app.models import WizardStep


def _jellyfin_step(position, markdown="Nothing special here", category="post_invite"):
    return WizardStep(
        server_type="jellyfin",
        category=category,
        position=position,
        title=f"Step {position}",
        markdown=markdown,
        requires=[],
    )


def _jellyfin_steps(category="post_invite"):
    return (
        db.session.query(WizardStep)
        .filter(
            WizardStep.server_type == "jellyfin",
            WizardStep.category == category,
        )
        .order_by(WizardStep.position)
        .all()
    )


# ── Backfill onto existing installs ─────────────────────────────────────────


def test_backfill_puts_the_video_first(app, session):
    """Position 0, with everything else pushed down."""
    from app.services.wizard_seed import VIDEO_MARKER, ensure_video_step

    db.session.add_all(
        [
            _jellyfin_step(0, markdown="What is Jellyfin?"),
            _jellyfin_step(1, markdown="Download the clients"),
            _jellyfin_step(2, markdown="Tips"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    steps = _jellyfin_steps()
    assert [s.position for s in steps] == [0, 1, 2, 3], "positions must stay dense"
    assert VIDEO_MARKER in steps[0].markdown
    assert [s.markdown for s in steps[1:]] == [
        "What is Jellyfin?",
        "Download the clients",
        "Tips",
    ], "the existing order must survive underneath the video"


def test_backfill_pulls_device_setup_up_behind_the_video(app, session):
    """The narration ends by telling the buyer to go set up their device.

    ensure_quick_connect_step appends, so on installs that already had steps the
    device-setup page landed dead last — behind the tips page. The video is
    useless as an introduction if the thing it introduces is three clicks away.
    """
    from app.services.wizard_seed import (
        QUICK_CONNECT_MARKER,
        VIDEO_MARKER,
        ensure_video_step,
    )

    db.session.add_all(
        [
            _jellyfin_step(0, markdown="What is Jellyfin?"),
            _jellyfin_step(1, markdown="Download the clients"),
            _jellyfin_step(2, markdown="Tips for the best experience"),
            _jellyfin_step(3, markdown=f"{{{{ {QUICK_CONNECT_MARKER} }}}}"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    steps = _jellyfin_steps()
    assert VIDEO_MARKER in steps[0].markdown
    assert QUICK_CONNECT_MARKER in steps[1].markdown
    assert [s.position for s in steps] == [0, 1, 2, 3, 4]
    # The pages it jumped over keep their own relative order.
    assert [s.markdown for s in steps[2:]] == [
        "What is Jellyfin?",
        "Download the clients",
        "Tips for the best experience",
    ]


def test_backfill_is_idempotent(app, session):
    """It runs on every startup; a second pass must change nothing."""
    from app.services.wizard_seed import ensure_video_step

    db.session.add_all([_jellyfin_step(0), _jellyfin_step(1)])
    db.session.commit()

    with app.app_context():
        ensure_video_step()
        ensure_video_step()
        ensure_video_step()

    steps = _jellyfin_steps()
    assert len(steps) == 3
    assert [s.position for s in steps] == [0, 1, 2]


def test_backfill_skips_a_fresh_install(app, session):
    """No Jellyfin rows means import_default_wizard_steps owns the seeding.

    The bundled file is named 00_video.md precisely so the seeder's sort puts it
    at position 0 without any help from here.
    """
    from app.services.wizard_seed import ensure_video_step

    with app.app_context():
        ensure_video_step()

    assert _jellyfin_steps() == []


def test_backfill_respects_a_step_an_admin_already_moved(app, session):
    """The marker is matched wherever it lives, not by title or position."""
    from app.services.wizard_seed import VIDEO_MARKER, ensure_video_step

    db.session.add_all(
        [
            _jellyfin_step(0),
            _jellyfin_step(
                0,
                markdown=f"Renamed by the admin {{{{ {VIDEO_MARKER} }}}}",
                category="pre_invite",
            ),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    assert len(_jellyfin_steps()) == 1, "must not add a second copy"


def test_backfill_leaves_customised_steps_alone(app, session):
    """Additive only: it never edits or deletes what an admin wrote."""
    from app.services.wizard_seed import ensure_video_step

    db.session.add_all([_jellyfin_step(0, markdown="Hand-written by the admin")])
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    steps = _jellyfin_steps()
    assert steps[1].markdown == "Hand-written by the admin"
    assert steps[1].title == "Step 0"


def test_backfill_ignores_the_pre_invite_sequence(app, session):
    """Position is unique per (server_type, category).

    A pre_invite step sitting at position 0 must neither collide with the new
    post_invite row nor get shifted by it.
    """
    from app.services.wizard_seed import ensure_video_step

    db.session.add_all(
        [
            _jellyfin_step(0, markdown="Terms", category="pre_invite"),
            _jellyfin_step(0, markdown="Welcome"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    assert [s.position for s in _jellyfin_steps("pre_invite")] == [0]
    assert _jellyfin_steps("pre_invite")[0].markdown == "Terms"
    assert [s.position for s in _jellyfin_steps()] == [0, 1]


# ── The widget itself ───────────────────────────────────────────────────────


def test_widget_renders_a_player_pointing_at_the_bundled_video(app):
    from app.services.wizard_widgets import VideoWidget

    with app.test_request_context():
        html = VideoWidget().render("jellyfin")

    assert "<video" in html
    assert "/static/video/neexy-wizard.mp4" in html
    assert "/static/video/neexy-wizard-poster.jpg" in html
    assert "temporarily unavailable" not in html


def test_widget_does_not_preload_the_whole_file(app):
    """It is an 11 MB file on a step many buyers will skip."""
    from app.services.wizard_widgets import VideoWidget

    with app.test_request_context():
        html = VideoWidget().render("jellyfin")

    assert 'preload="metadata"' in html
    assert "autoplay" not in html


def test_widget_accepts_a_different_clip(app):
    from app.services.wizard_widgets import VideoWidget

    with app.test_request_context():
        html = VideoWidget().render(
            "jellyfin",
            src="https://cdn.example.com/other.mp4",
            caption="Cómo conectar tu tele",
        )

    assert "https://cdn.example.com/other.mp4" in html
    assert "Cómo conectar tu tele" in html


def test_placeholder_is_replaced_in_step_markdown(app):
    """`{{ widget:video }}` is what the markdown file actually contains."""
    from app.services.wizard_widgets import process_widget_placeholders

    with app.test_request_context():
        html = process_widget_placeholders("{{ widget:video }}", "jellyfin")

    assert "<video" in html
    assert "widget:video" not in html


# ── The bundled markdown ────────────────────────────────────────────────────


def test_bundled_step_sorts_ahead_of_every_other_jellyfin_step():
    """The seeder orders by filename, so the 00_ prefix is what makes it first."""
    from app.services.wizard_seed import BASE_DIR

    files = sorted(p.name for p in (BASE_DIR / "jellyfin").glob("*.md"))
    assert files[0] == "00_video.md"


def test_bundled_step_carries_the_marker_the_backfill_looks_for():
    from app.services.wizard_seed import VIDEO_MARKER, VIDEO_SOURCE

    assert VIDEO_SOURCE.exists()
    assert VIDEO_MARKER in VIDEO_SOURCE.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "asset",
    ["app/static/video/neexy-wizard.mp4", "app/static/video/neexy-wizard-poster.jpg"],
)
def test_video_assets_ship_with_the_app(asset):
    """The player is useless if the build drops the file it points at."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / asset
    assert path.exists(), f"{asset} is missing"
    assert path.stat().st_size > 0
