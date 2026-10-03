"""Build the phone-path GIF of the device-setup wizard step.

Takes setup.gif (six Moonfin screens recorded on a phone) and puts a strip on
top of each frame naming the step it shows — the same numbered badge and the
same words as the list in app/templates/wizard/widgets/quick_connect.html — so
the buyer always knows which step they are looking at. The strip is part of
the image, so a renamed step means editing STEPS and running this again:

    python3 promo/phone-setup/build.py

Needs Pillow, ffmpeg and macOS (for the SF font).
"""

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "setup.gif"
OUTPUT = HERE.parents[1] / "app/static/img/neexy-moonfin-phone-setup.gif"

# Same order and wording as the phone path in quick_connect.html. Step 6 is
# shortened: the full sentence does not fit on one line.
STEPS = [
    "Instalar",
    "Abrir",
    "Añadir servidor",
    "Dirección del servidor",
    "Selecciona Contraseña",
    "Ingresa usuario y contraseña",
]
SECONDS_PER_FRAME = 3

FONT = "/System/Library/Fonts/SFNS.ttf"
PRIMARY = (254, 65, 85)  # --color-primary in app/static/src/style.css
STRIP_BG = (17, 24, 39)  # gray-900, the wizard's dark background
TRACK = (55, 65, 81)  # gray-700
WHITE = (255, 255, 255)

STRIP_HEIGHT = 104
PADDING = 24
BADGE = 56
SEGMENT_HEIGHT = 6
SEGMENT_GAP = 8


def _font(size: int, weight: str) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(FONT, size)
    font.set_variation_by_name(weight)
    return font


def _strip(width: int, step: int) -> Image.Image:
    """Badge and label, with a six-segment progress track underneath."""
    strip = Image.new("RGB", (width, STRIP_HEIGHT), STRIP_BG)
    draw = ImageDraw.Draw(strip)

    track_top = STRIP_HEIGHT - PADDING // 2 - SEGMENT_HEIGHT
    centre_y = track_top // 2 + 2

    badge_box = (PADDING, centre_y - BADGE // 2, PADDING + BADGE, centre_y + BADGE // 2)
    draw.ellipse(badge_box, fill=PRIMARY)
    draw.text(
        (PADDING + BADGE / 2, centre_y),
        str(step + 1),
        font=_font(30, "Bold"),
        fill=WHITE,
        anchor="mm",
    )
    draw.text(
        (PADDING + BADGE + 16, centre_y),
        STEPS[step],
        font=_font(38, "Bold"),
        fill=WHITE,
        anchor="lm",
    )

    segment = (width - 2 * PADDING - SEGMENT_GAP * (len(STEPS) - 1)) / len(STEPS)
    for index in range(len(STEPS)):
        left = PADDING + index * (segment + SEGMENT_GAP)
        draw.rounded_rectangle(
            (left, track_top, left + segment, track_top + SEGMENT_HEIGHT),
            radius=SEGMENT_HEIGHT // 2,
            fill=PRIMARY if index <= step else TRACK,
        )
    return strip


def _frames(source: Image.Image) -> list[Image.Image]:
    frames = []
    for index in range(source.n_frames):
        source.seek(index)
        frames.append(source.convert("RGB"))
    if len(frames) != len(STEPS):
        raise SystemExit(
            f"{SOURCE.name} has {len(frames)} frames, STEPS has {len(STEPS)}"
        )
    return frames


def main() -> None:
    frames = _frames(Image.open(SOURCE))
    width, height = frames[0].size

    with tempfile.TemporaryDirectory() as tmp:
        for index, frame in enumerate(frames):
            labelled = Image.new("RGB", (width, STRIP_HEIGHT + height))
            labelled.paste(_strip(width, index), (0, 0))
            labelled.paste(frame, (0, STRIP_HEIGHT))
            labelled.save(Path(tmp) / f"{index}.png")

        # One palette for every frame: the screens share their colours, and a
        # per-frame palette roughly doubles the file for no visible gain.
        subprocess.run(  # noqa: S603 - fixed argv, no outside input
            [  # noqa: S607 - ffmpeg from PATH, like the rest of promo/
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-framerate",
                f"1/{SECONDS_PER_FRAME}",
                "-i",
                str(Path(tmp) / "%d.png"),
                "-filter_complex",
                "split[a][b];[a]palettegen=stats_mode=full[p];[b][p]paletteuse=dither=none",
                "-loop",
                "0",
                str(OUTPUT),
            ],
            check=True,
        )

    print(
        f"{OUTPUT.relative_to(HERE.parents[1])}: {width}x{STRIP_HEIGHT + height}, "
        f"{OUTPUT.stat().st_size // 1024}K"
    )


if __name__ == "__main__":
    main()
