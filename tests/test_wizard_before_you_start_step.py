"""The "Algunas cosas que debes saber antes de empezar" step.

It opens the Jellyfin wizard — ahead of the video — and tells a new customer the
three things support gets asked about most: how to renew, how to recover a
forgotten username or password, and how to free a device slot.

Like the video and Quick Connect steps it has to be backfilled: the seeder only
bootstraps server types that are missing entirely, so a live install would
never receive a newly bundled file.

What differs here is the guard against adding it twice. This step is plain text
an admin is expected to reword from the settings screen, so the marker inside it
cannot be the only thing standing between a restart and a duplicate: a Settings
flag records that the backfill already ran, and after that the step is the
admin's to edit or delete.
"""

import re
from pathlib import Path

import frontmatter

from app.extensions import db
from app.models import Settings, WizardStep

REPO_ROOT = Path(__file__).resolve().parent.parent
TITLE = "Algunas cosas que debes saber antes de empezar"


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


def test_backfill_puts_the_step_first_ahead_of_the_video(app, session):
    from app.services.wizard_seed import (
        BEFORE_YOU_START_MARKER,
        VIDEO_MARKER,
        ensure_before_you_start_step,
    )

    db.session.add_all(
        [
            _jellyfin_step(0, markdown=f"{{{{ {VIDEO_MARKER} }}}}"),
            _jellyfin_step(1, markdown="Set up your device"),
            _jellyfin_step(2, markdown="Tips"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    steps = _jellyfin_steps()
    assert [s.position for s in steps] == [0, 1, 2, 3], "positions must stay dense"
    assert BEFORE_YOU_START_MARKER in steps[0].markdown
    assert steps[0].title == TITLE
    assert [s.markdown for s in steps[1:]] == [
        f"{{{{ {VIDEO_MARKER} }}}}",
        "Set up your device",
        "Tips",
    ], "the existing order must survive underneath it"


def test_backfill_is_idempotent(app, session):
    """It runs on every startup; a second pass must change nothing."""
    from app.services.wizard_seed import ensure_before_you_start_step

    db.session.add_all([_jellyfin_step(0), _jellyfin_step(1)])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()
        ensure_before_you_start_step()
        ensure_before_you_start_step()

    steps = _jellyfin_steps()
    assert len(steps) == 3
    assert [s.position for s in steps] == [0, 1, 2]


def test_backfill_skips_a_fresh_install(app, session):
    """No Jellyfin rows means import_default_wizard_steps owns the seeding."""
    from app.services.wizard_seed import ensure_before_you_start_step

    with app.app_context():
        ensure_before_you_start_step()

    assert _jellyfin_steps() == []


def test_a_fresh_install_seeded_from_disk_is_not_given_a_second_copy(app, session):
    """The seeder already placed the bundled file; the backfill must see that."""
    from app.services.wizard_seed import (
        BEFORE_YOU_START_SOURCE,
        ensure_before_you_start_step,
    )

    bundled = frontmatter.load(str(BEFORE_YOU_START_SOURCE))
    db.session.add_all([_jellyfin_step(0, markdown=bundled.content), _jellyfin_step(1)])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    assert len(_jellyfin_steps()) == 2


def test_a_step_the_admin_reworded_is_not_added_again(app, session):
    """The marker is an HTML comment in editable text; it will not survive a rewrite."""
    from app.services.wizard_seed import ensure_before_you_start_step

    db.session.add_all([_jellyfin_step(0)])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    first = _jellyfin_steps()[0]
    first.markdown = "Reescrito por el admin, sin el marcador."
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    steps = _jellyfin_steps()
    assert len(steps) == 2
    assert steps[0].markdown == "Reescrito por el admin, sin el marcador."


def test_a_step_the_admin_deleted_stays_deleted(app, session):
    from app.services.wizard_seed import ensure_before_you_start_step

    db.session.add_all([_jellyfin_step(0)])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    db.session.delete(_jellyfin_steps()[0])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    assert [s.title for s in _jellyfin_steps()] == ["Step 0"]


def test_backfill_leaves_customised_steps_alone(app, session):
    """Additive only: it never edits or deletes what an admin wrote."""
    from app.services.wizard_seed import ensure_before_you_start_step

    db.session.add_all([_jellyfin_step(0, markdown="Hand-written by the admin")])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    steps = _jellyfin_steps()
    assert steps[1].markdown == "Hand-written by the admin"
    assert steps[1].title == "Step 0"


def test_backfill_ignores_the_pre_invite_sequence(app, session):
    """Position is unique per (server_type, category)."""
    from app.services.wizard_seed import ensure_before_you_start_step

    db.session.add_all(
        [
            _jellyfin_step(0, markdown="Terms", category="pre_invite"),
            _jellyfin_step(0, markdown="Welcome"),
        ]
    )
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    assert [s.position for s in _jellyfin_steps("pre_invite")] == [0]
    assert _jellyfin_steps("pre_invite")[0].markdown == "Terms"
    assert [s.position for s in _jellyfin_steps()] == [0, 1]


def test_backfill_records_that_it_ran(app, session):
    from app.services.wizard_seed import (
        BEFORE_YOU_START_FLAG,
        ensure_before_you_start_step,
    )

    db.session.add_all([_jellyfin_step(0)])
    db.session.commit()

    with app.app_context():
        ensure_before_you_start_step()

    assert Settings.query.filter_by(key=BEFORE_YOU_START_FLAG).count() == 1


# ── The bundled markdown ────────────────────────────────────────────────────


def _bundled():
    from app.services.wizard_seed import BEFORE_YOU_START_SOURCE

    return frontmatter.load(str(BEFORE_YOU_START_SOURCE))


def test_bundled_step_sorts_ahead_of_every_other_jellyfin_step():
    """The seeder orders by filename, so a fresh install gets the same order."""
    from app.services.wizard_seed import BASE_DIR, BEFORE_YOU_START_SOURCE

    files = sorted(p.name for p in (BASE_DIR / "jellyfin").glob("*.md"))
    assert files[0] == BEFORE_YOU_START_SOURCE.name
    assert files[1] == "00_video.md"


def test_bundled_step_carries_the_marker_and_the_title():
    from app.services.wizard_seed import BEFORE_YOU_START_MARKER

    post = _bundled()
    assert BEFORE_YOU_START_MARKER in post.content
    assert post["title"] == TITLE


def test_bundled_step_covers_renewal_recovery_and_signing_out():
    content = _bundled().content

    assert "https://neexy.net/pay?renovar=1" in content
    assert "**Validar cuenta**" in content
    assert "**Recuperar**" in content
    assert "**Sign Out**" in content
    assert "No te preocupes" in content


def test_every_screenshot_the_step_points_at_ships_with_the_app():
    content = _bundled().content
    # Each one appears twice: as the image and as the link to its full size.
    names = set(re.findall(r"filename='(img/wizard/[^']+)'", content))

    assert len(names) == 6
    for name in names:
        path = REPO_ROOT / "app" / "static" / name
        assert path.exists(), f"{name} is missing"
        assert path.stat().st_size > 0


def test_step_renders_to_html_with_working_image_urls(app):
    """Jinja runs before markdown: a typo in url_for would surface here as the
    generic "could not be loaded" box instead of the step."""
    from app.blueprints.wizard.routes import _render

    with app.test_request_context():
        html = _render(_bundled(), {}, server_type="jellyfin")

    assert "Error Loading Step" not in html
    assert "Algunas cosas que debes saber antes de empezar" in html
    srcs = re.findall(r'<img[^>]*src="([^"]+)"', html)
    assert len(srcs) == 6
    assert all(src.startswith("/static/img/wizard/") for src in srcs)
    assert "{{" not in html
    assert 'href="https://neexy.net/pay?renovar=1"' in html
    # Every screenshot says what it shows and reserves its space.
    for tag in re.findall(r"<img[^>]*>", html):
        assert re.search(r'alt="[^"]{10,}"', tag)
        assert 'width="' in tag and 'height="' in tag


def test_step_does_not_mention_settings():
    """The step is managed from the admin's settings screen; that is where it
    lives, not something the customer is told about."""
    assert "settings" not in _bundled().content.lower()
