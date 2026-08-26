"""Every badge colour in the event catalogue must be declared to Tailwind.

Badge classes live in ``app/services/notification_events.py``. Tailwind scans
templates, JS and markdown — never Python — so a class used only there is never
generated and silently does nothing. The failure is invisible in code review and
in every test that checks markup: the class IS on the element, it just has no
rule behind it.

That has now happened twice on the same badge. The first fix added an
``@source inline(...)`` listing the base utilities, which looked correct because
four of the five colours rendered fine. They rendered fine by accident: red,
blue, green and purple each appear as a bare ``dark:bg-<colour>-900`` somewhere
in real markup, so Tailwind generated them from the scan. Amber only ever
appears as ``dark:bg-amber-900/20`` and ``/40`` — an opacity modifier makes a
DIFFERENT class — so the bare one was still missing, while ``dark:text-amber-200``
did exist. Pale text on a pale background, in production, for two releases.

So this asserts the contract directly, rather than trusting incidental markup:
every literal class in the catalogue must be covered by an ``@source inline``
declaration. Static and dependency-free on purpose — it needs neither node nor a
CSS build, so it runs in the same pytest pass as everything else.
"""

from __future__ import annotations

import re
from itertools import product
from pathlib import Path

from app.services.notification_events import EVENT_TYPES

STYLE_CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "src" / "style.css"


def _declared_classes() -> set[str]:
    """Expand every ``@source inline("...")`` in style.css into class names.

    Implements the brace expansion Tailwind applies to these strings, so the
    test reads the same declaration the build does instead of a second copy of
    the list that could drift from it.
    """
    css = STYLE_CSS.read_text(encoding="utf-8")
    declared: set[str] = set()

    for raw in re.findall(r'@source\s+inline\(\s*"([^"]+)"\s*\)', css):
        for candidate in raw.split():
            groups = re.findall(r"\{([^}]*)\}", candidate)
            skeleton = re.sub(r"\{[^}]*\}", "{}", candidate)
            options = [group.split(",") for group in groups]
            for combo in product(*options) if options else [()]:
                declared.add(skeleton.format(*combo))

    return declared


def test_every_badge_class_is_declared_to_tailwind():
    declared = _declared_classes()

    used = {css_class for event in EVENT_TYPES for css_class in event.badge.split()}

    missing = sorted(used - declared)

    assert not missing, (
        "These badge classes are used in notification_events.py but are not "
        "declared in an @source inline(...) in app/static/src/style.css, so "
        "Tailwind will not generate them and the badge will render unstyled:\n  "
        + "\n  ".join(missing)
    )


def test_the_declaration_covers_dark_variants():
    """A `dark:` variant is its own candidate — listing the base is not enough.

    Pinned separately because this is the exact half that was missed: the base
    utilities were declared, the `dark:` ones were not, and four colours hid it.
    """
    declared = _declared_classes()

    dark_classes = {
        css_class
        for event in EVENT_TYPES
        for css_class in event.badge.split()
        if css_class.startswith("dark:")
    }

    assert dark_classes, "the catalogue is expected to style dark mode explicitly"
    assert dark_classes <= declared
