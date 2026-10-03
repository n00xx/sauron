"""Dropping the two pages that used to open the Jellyfin wizard.

"Algunas cosas que debes saber antes de empezar" moved to the guides at
neexy.net/blog and "Watch this first" was dropped, so the wizard now opens on
device setup. Fresh installs get that from the bundled files alone. A live
install holds both pages as rows the seeder never touches, which is what
``retire_intro_steps`` is for.

Two things are load-bearing. Device setup has to survive and land at position 0,
since it is now the whole wizard. And the cleanup runs once: an admin who adds
either page back from the UI keeps it.

``(server_type, category, position)`` is unique and SQLite enforces it per
statement, so closing the gap by writing final positions directly would collide
with rows not yet moved. That fails with an IntegrityError on a real database
while passing any test that only counts rows, hence the position assertions.
"""

from app.extensions import db
from app.models import Settings, WizardStep

BEFORE_YOU_START = (
    "<!-- sauron:before-you-start -->\n\n## Algunas cosas\n\n"
    '{{ widget:video src="video/neexy-antes-de-empezar.mp4" }}'
)
WATCH_THIS_FIRST = "## {{ _('Everything in 90 seconds') }}\n\n{{ widget:video }}"
DEVICE_SETUP = "## {{ _('Set up your first device') }}\n\n{{ widget:quick_connect }}"


def _step(position, markdown, category="post_invite", server_type="jellyfin"):
    return WizardStep(
        server_type=server_type,
        category=category,
        position=position,
        title=f"Step {position}",
        markdown=markdown,
        requires=[],
    )


def _steps(category="post_invite", server_type="jellyfin"):
    return (
        db.session.query(WizardStep)
        .filter(
            WizardStep.server_type == server_type,
            WizardStep.category == category,
        )
        .order_by(WizardStep.position)
        .all()
    )


def _flag_set():
    from app.services.wizard_seed import INTRO_STEPS_RETIRED_FLAG

    return Settings.query.filter_by(key=INTRO_STEPS_RETIRED_FLAG).first() is not None


def test_the_live_wizard_opens_on_device_setup(app, session):
    """The three steps the live install holds today, in their order."""
    from app.services.wizard_seed import retire_intro_steps

    db.session.add_all(
        [
            _step(0, BEFORE_YOU_START),
            _step(1, WATCH_THIS_FIRST),
            _step(2, DEVICE_SETUP),
        ]
    )
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    steps = _steps()
    assert [s.markdown for s in steps] == [DEVICE_SETUP]
    assert [s.position for s in steps] == [0]
    assert _flag_set()


def test_later_steps_close_ranks_behind_device_setup(app, session):
    from app.services.wizard_seed import retire_intro_steps

    db.session.add_all(
        [
            _step(0, BEFORE_YOU_START),
            _step(1, WATCH_THIS_FIRST),
            _step(2, DEVICE_SETUP),
            _step(3, "Tips"),
        ]
    )
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    steps = _steps()
    assert [s.markdown for s in steps] == [DEVICE_SETUP, "Tips"]
    assert [s.position for s in steps] == [0, 1], "positions must stay dense"


def test_device_setup_survives_even_with_a_video_in_it(app, session):
    """Recognising "Watch this first" by its video must not catch device setup."""
    from app.services.wizard_seed import retire_intro_steps

    with_video = f"{DEVICE_SETUP}\n\n{{{{ widget:video }}}}"
    db.session.add_all([_step(0, WATCH_THIS_FIRST), _step(1, with_video)])
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    assert [s.markdown for s in _steps()] == [with_video]


def test_a_page_an_admin_moved_is_found_in_its_new_category(app, session):
    from app.services.wizard_seed import retire_intro_steps

    db.session.add_all(
        [
            _step(0, BEFORE_YOU_START, category="pre_invite"),
            _step(1, "Terms", category="pre_invite"),
            _step(0, DEVICE_SETUP),
        ]
    )
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    pre_invite = _steps("pre_invite")
    assert [s.markdown for s in pre_invite] == ["Terms"]
    assert [s.position for s in pre_invite] == [0]
    assert [s.markdown for s in _steps()] == [DEVICE_SETUP]


def test_it_runs_once_so_an_admin_can_add_a_page_back(app, session):
    from app.services.wizard_seed import retire_intro_steps

    db.session.add_all([_step(0, BEFORE_YOU_START), _step(1, DEVICE_SETUP)])
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    # The admin puts the page back from the settings screen.
    db.session.add(_step(1, BEFORE_YOU_START))
    db.session.commit()

    with app.app_context():
        retire_intro_steps()
        retire_intro_steps()

    assert [s.markdown for s in _steps()] == [DEVICE_SETUP, BEFORE_YOU_START]


def test_it_skips_a_fresh_install(app, session):
    """No Jellyfin rows yet: the seeder fills them from disk, without the pages."""
    from app.services.wizard_seed import retire_intro_steps

    with app.app_context():
        retire_intro_steps()

    assert _steps() == []
    assert not _flag_set()


def test_other_server_types_are_left_alone(app, session):
    from app.services.wizard_seed import retire_intro_steps

    db.session.add_all(
        [
            _step(0, DEVICE_SETUP),
            _step(0, "{{ widget:video }}", server_type="plex"),
        ]
    )
    db.session.commit()

    with app.app_context():
        retire_intro_steps()

    assert [s.markdown for s in _steps(server_type="plex")] == ["{{ widget:video }}"]


def test_a_fresh_install_opens_on_device_setup():
    """The seeder orders by filename, so the first bundled file is step 1."""
    from app.services.wizard_seed import BASE_DIR, QUICK_CONNECT_SOURCE

    files = sorted((BASE_DIR / "jellyfin").glob("*.md"))
    assert files[0] == QUICK_CONNECT_SOURCE
    assert {"00_before_you_start.md", "00_video.md"}.isdisjoint(
        path.name for path in files
    )
