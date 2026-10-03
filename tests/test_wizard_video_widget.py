"""The video widget: an inline player for a clip bundled with the app.

It no longer has a wizard step of its own. The Smart TV path of device setup
includes the same player template for its install video, and an admin can still
drop `{{ widget:video }}` into any step.
"""

import pytest

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


@pytest.mark.parametrize(
    "asset",
    [
        "app/static/video/neexy-wizard.mp4",
        "app/static/video/neexy-wizard-poster.jpg",
        "app/static/video/neexy-moonfin-smart-tv.mp4",
        "app/static/video/neexy-moonfin-smart-tv-poster.jpg",
    ],
)
def test_video_assets_ship_with_the_app(asset):
    """The player is useless if the build drops the file it points at."""
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / asset
    assert path.exists(), f"{asset} is missing"
    assert path.stat().st_size > 0
