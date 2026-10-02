"""The "before you start" step becomes a short video.

The page shipped in 2026.10.18 as text with six screenshots. It is now a ~55 s
video with the two links you cannot click inside a video underneath it.

Fresh installs read the new file from disk. Live installs already have the old
text in the database, and the seeder never edits a row, so a one-time backfill
swaps it — but only when the row still holds exactly what was shipped. The
step is plain text an admin is expected to reword; if they did, their version
stays and the log says so.
"""

import hashlib
import logging
import re
from pathlib import Path

import frontmatter

from app.extensions import db
from app.models import Settings, WizardStep

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = REPO_ROOT / "tests" / "fixtures" / "wizard_before_you_start_2026_10_18.md"
VIDEO_SRC = "video/neexy-antes-de-empezar.mp4"
POSTER_SRC = "video/neexy-antes-de-empezar-poster.jpg"


def _shipped_markdown() -> str:
    return frontmatter.load(str(SHIPPED)).content


def _bundled_markdown() -> str:
    from app.services.wizard_seed import BEFORE_YOU_START_SOURCE

    return frontmatter.load(str(BEFORE_YOU_START_SOURCE)).content


def _step(position, markdown, category="post_invite", title=None):
    return WizardStep(
        server_type="jellyfin",
        category=category,
        position=position,
        title=title or f"Step {position}",
        markdown=markdown,
        requires=[],
    )


def _jellyfin_steps():
    return (
        db.session.query(WizardStep)
        .filter(
            WizardStep.server_type == "jellyfin",
            WizardStep.category == "post_invite",
        )
        .order_by(WizardStep.position)
        .all()
    )


def _run():
    from app.services.wizard_seed import ensure_before_you_start_video

    ensure_before_you_start_video()


# ── What counts as "unedited" ───────────────────────────────────────────────


def test_the_fingerprint_in_code_is_the_text_that_shipped():
    """Guards the constant: if it drifts, production silently keeps the text."""
    from app.services.wizard_seed import (
        BEFORE_YOU_START_TEXT_SHA256,
        _normalised_sha256,
    )

    assert _normalised_sha256(_shipped_markdown()) == BEFORE_YOU_START_TEXT_SHA256


def test_line_endings_and_trailing_spaces_do_not_count_as_edits():
    """A textarea save round-trip turns \\n into \\r\\n."""
    from app.services.wizard_seed import _normalised_sha256

    text = _shipped_markdown()
    resaved = "\r\n".join(line + "  " for line in text.split("\n")) + "\r\n"

    assert _normalised_sha256(resaved) == _normalised_sha256(text)


# ── Backfill onto existing installs ─────────────────────────────────────────


def test_an_unedited_step_is_replaced_by_the_video(app, session):
    db.session.add_all([_step(0, _shipped_markdown()), _step(1, "{{ widget:video }}")])
    db.session.commit()

    with app.app_context():
        _run()

    steps = _jellyfin_steps()
    assert steps[0].markdown == _bundled_markdown()
    assert VIDEO_SRC in steps[0].markdown
    # Position, title and the step after it are untouched.
    assert steps[0].title == "Step 0"
    assert [s.position for s in steps] == [0, 1]
    assert steps[1].markdown == "{{ widget:video }}"


def test_a_step_saved_back_with_windows_line_endings_is_still_replaced(app, session):
    resaved = _shipped_markdown().replace("\n", "\r\n")
    db.session.add(_step(0, resaved))
    db.session.commit()

    with app.app_context():
        _run()

    assert VIDEO_SRC in _jellyfin_steps()[0].markdown


def test_a_step_the_admin_edited_is_left_alone_and_logged(app, session, caplog):
    edited = _shipped_markdown().replace(
        "Nos pasa a todos.", "Nos pasa a todos, de verdad."
    )
    db.session.add(_step(0, edited))
    db.session.commit()

    # conftest runs the Alembic upgrade in-process, and its fileConfig disables
    # every logger that already exists, the app's included. In production the
    # upgrade is a separate process (docker-entrypoint.sh), so this is test-only.
    was_disabled = app.logger.disabled
    app.logger.disabled = False
    app.logger.addHandler(caplog.handler)
    try:
        with app.app_context(), caplog.at_level(logging.WARNING):
            _run()
    finally:
        app.logger.removeHandler(caplog.handler)
        app.logger.disabled = was_disabled

    assert _jellyfin_steps()[0].markdown == edited
    assert any(
        r.levelno == logging.WARNING and "before you start" in r.getMessage().lower()
        for r in caplog.records
    )


def test_a_deleted_step_is_not_brought_back(app, session):
    from app.services.wizard_seed import BEFORE_YOU_START_VIDEO_FLAG

    db.session.add(_step(0, "{{ widget:video }}"))
    db.session.commit()

    with app.app_context():
        _run()

    assert [s.markdown for s in _jellyfin_steps()] == ["{{ widget:video }}"]
    assert Settings.query.filter_by(key=BEFORE_YOU_START_VIDEO_FLAG).count() == 1


def test_it_runs_once(app, session):
    """After the first run the step is the admin's again, even if they paste
    the old text back."""
    from app.services.wizard_seed import BEFORE_YOU_START_VIDEO_FLAG

    db.session.add(_step(0, _shipped_markdown()))
    db.session.commit()

    with app.app_context():
        _run()

    _jellyfin_steps()[0].markdown = _shipped_markdown()
    db.session.commit()

    with app.app_context():
        _run()

    assert _jellyfin_steps()[0].markdown == _shipped_markdown()
    assert Settings.query.filter_by(key=BEFORE_YOU_START_VIDEO_FLAG).count() == 1


def test_a_fresh_install_needs_no_backfill(app, session):
    """No Jellyfin rows: the seeder reads the new file from disk."""
    from app.services.wizard_seed import BEFORE_YOU_START_VIDEO_FLAG

    with app.app_context():
        _run()

    assert _jellyfin_steps() == []
    assert Settings.query.filter_by(key=BEFORE_YOU_START_VIDEO_FLAG).count() == 0


def test_a_step_already_seeded_with_the_video_is_not_reported_as_edited(
    app, session, caplog
):
    """A fresh install seeds the new file from disk before this runs; that is
    not an admin's edit and must not be logged as one."""
    db.session.add(_step(0, _bundled_markdown()))
    db.session.commit()

    was_disabled = app.logger.disabled
    app.logger.disabled = False
    app.logger.addHandler(caplog.handler)
    try:
        with app.app_context(), caplog.at_level(logging.WARNING):
            _run()
    finally:
        app.logger.removeHandler(caplog.handler)
        app.logger.disabled = was_disabled

    assert _jellyfin_steps()[0].markdown == _bundled_markdown()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_other_server_types_are_never_touched(app, session):
    db.session.add(
        WizardStep(
            server_type="plex",
            category="post_invite",
            position=0,
            title="Plex",
            markdown=_shipped_markdown(),
            requires=[],
        )
    )
    db.session.add(_step(0, "Welcome"))
    db.session.commit()

    with app.app_context():
        _run()

    plex = db.session.query(WizardStep).filter_by(server_type="plex").one()
    assert plex.markdown == _shipped_markdown()


# ── The onboarding video step must still be recognised ─────────────────────


def test_the_onboarding_video_is_still_added_when_only_step_one_has_a_video(
    app, session
):
    """Both steps now embed `widget:video`. Step 1's must not make the
    onboarding-video backfill think it already ran."""
    from app.services.wizard_seed import ensure_video_step

    db.session.add(_step(0, _bundled_markdown()))
    db.session.commit()

    with app.app_context():
        ensure_video_step()

    steps = _jellyfin_steps()
    assert len(steps) == 2
    assert "{{ widget:video }}" in steps[0].markdown
    assert VIDEO_SRC in steps[1].markdown


# ── The bundled file ────────────────────────────────────────────────────────


def test_bundled_step_keeps_the_marker_the_backfills_look_for():
    from app.services.wizard_seed import BEFORE_YOU_START_MARKER

    assert BEFORE_YOU_START_MARKER in _bundled_markdown()


def test_bundled_step_embeds_the_video_and_its_poster():
    content = _bundled_markdown()

    assert f'src="{VIDEO_SRC}"' in content
    assert f'poster="{POSTER_SRC}"' in content
    for path in (VIDEO_SRC, POSTER_SRC):
        file = REPO_ROOT / "app" / "static" / path
        assert file.exists(), f"{path} is missing"
        assert file.stat().st_size > 0


def test_bundled_step_keeps_the_links_a_video_cannot_make_clickable():
    content = _bundled_markdown()

    assert 'href="https://neexy.net/pay?renovar=1"' in content
    assert 'href="https://neexy.net"' in content
    assert "**Recuperar**" in content


def test_bundled_step_uses_no_tailwind_classes():
    """The Docker builder only scans app/ — a class used only here is never built."""
    assert "class=" not in _bundled_markdown()


def test_screenshots_of_the_old_text_still_ship():
    """An install whose admin edited the old text keeps pointing at them."""
    names = set(re.findall(r"filename='(img/wizard/[^']+)'", _shipped_markdown()))

    assert len(names) == 6
    for name in names:
        assert (REPO_ROOT / "app" / "static" / name).exists(), f"{name} is missing"


def test_step_renders_a_player_and_the_links(app):
    """Jinja and the widget run before markdown; a mistake in either surfaces
    here as the generic error box instead of the step."""
    from app.blueprints.wizard.routes import _render
    from app.services.wizard_seed import BEFORE_YOU_START_SOURCE

    with app.test_request_context():
        html = _render(
            frontmatter.load(str(BEFORE_YOU_START_SOURCE)), {}, server_type="jellyfin"
        )

    assert "Error Loading Step" not in html
    assert "{{" not in html
    assert '<source src="/static/video/neexy-antes-de-empezar.mp4"' in html
    assert 'poster="/static/video/neexy-antes-de-empezar-poster.jpg"' in html
    assert 'href="https://neexy.net/pay?renovar=1"' in html


def test_fingerprint_helper_is_plain_sha256_of_the_normalised_text():
    from app.services.wizard_seed import _normalised_sha256

    assert _normalised_sha256("a  \r\nb\n\n") == hashlib.sha256(b"a\nb").hexdigest()
