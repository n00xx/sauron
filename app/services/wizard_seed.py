from __future__ import annotations

import hashlib
from pathlib import Path

import frontmatter
from flask import current_app
from sqlalchemy import inspect  # NEW

from app.extensions import db
from app.models import Settings, WizardStep

# Folder containing the bundled markdown files (wizard_steps/<server>/*.md)
BASE_DIR = Path(__file__).resolve().parent.parent.parent / "wizard_steps"

# No override directory – wizard steps are now managed from the UI.  The
# bundled markdown files are only used to:
# 1. Bootstrap fresh installations with all default steps
# 2. Add steps for NEW server types on upgrades (not new steps for existing types)


def _gather_step_files() -> list[Path]:
    """Return all bundled markdown files."""
    if not BASE_DIR.exists():
        return []
    return list(BASE_DIR.rglob("*.md"))


def _collect_server_files(root: Path) -> dict[str, list[Path]]:
    """Return mapping *server_type* → list[Path] under *root* (markdown only).

    Ignores the *root* directory completely if it does not exist.  Files are
    collected recursively so both flat and nested structures work.
    """
    if not root.exists():
        return {}

    server_dirs: dict[str, list[Path]] = {}
    for path in root.rglob("*.md"):
        server = path.parent.name  # assumes layout: <root>/<server_type>/<file>.md
        server_dirs.setdefault(server, []).append(path)
    return server_dirs


def _parse_markdown(path: Path) -> dict:
    post = frontmatter.load(str(path))
    requires = post.get("requires", [])  # list[str]
    title = post.get("title")

    # If no explicit title in front-matter, derive from first markdown header
    if not title:
        for line in post.content.splitlines():
            if line.lstrip().startswith("# "):
                title = line.lstrip("# ").strip()
                break

    return {
        "title": title,
        "markdown": post.content,
        "requires": requires,
    }


def _collect_builtin_files() -> dict[str, list[Path]]:
    """Return mapping *server_type* → list[Path] for built-in markdown files."""
    return _collect_server_files(BASE_DIR)


def import_default_wizard_steps() -> None:
    """Ensure the database contains wizard steps for server types.

    Behavior:
    • First installation (empty wizard_step table): Import all default steps
    • Upgrades: Only import steps for NEW server types that don't exist in DB yet
    • Existing steps are never modified to preserve UI customizations
    """

    # Skip entirely when running under pytest / testing
    if current_app.config.get("TESTING"):
        return

    # ─── Guard: table might not exist (first run before migrations) ─────────
    # Avoid querying WizardStep if its table is absent to prevent errors when
    # the app factory is invoked by commands that run *before* Alembic
    # migrations (e.g. `flask db upgrade`).  In that scenario the bootstrap
    # should be a no-op and will run again once the app starts for real.
    inspector = inspect(db.engine)
    if not inspector.has_table(WizardStep.__tablename__):
        return

    # ------------------------------------------------------------------
    # 1. Gather built-in wizard step markdown files
    # ------------------------------------------------------------------
    builtin_sources = _collect_builtin_files()

    # Nothing to do if repository contains no markdown assets
    if not builtin_sources:
        return

    # ------------------------------------------------------------------
    # 2. Check if this is a fresh installation or upgrade
    # ------------------------------------------------------------------
    existing_server_types = set(
        db.session.query(WizardStep.server_type).distinct().all()
    )
    existing_server_types = {row[0] for row in existing_server_types}

    is_fresh_install = len(existing_server_types) == 0

    # ------------------------------------------------------------------
    # 3. Determine which server types to process
    # ------------------------------------------------------------------
    if is_fresh_install:
        # Fresh install: import all server types
        server_types_to_import = set(builtin_sources.keys())
        current_app.logger.info(
            f"Fresh install detected: importing wizard steps for all {len(server_types_to_import)} server types"
        )
    else:
        # Upgrade: only import NEW server types not in database
        available_server_types = set(builtin_sources.keys())
        server_types_to_import = available_server_types - existing_server_types

        if server_types_to_import:
            current_app.logger.info(
                f"Upgrade detected: importing wizard steps for {len(server_types_to_import)} new server types: {sorted(server_types_to_import)}"
            )
        else:
            current_app.logger.debug("Upgrade detected: no new server types to import")

    # ------------------------------------------------------------------
    # 4. Import steps for determined server types only
    # ------------------------------------------------------------------
    for server_type in server_types_to_import:
        files = builtin_sources[server_type]
        files_sorted = sorted(files)

        for pos, path in enumerate(files_sorted):
            # Create new row for this step
            meta = _parse_markdown(path)
            step = WizardStep(
                server_type=server_type,
                category="post_invite",  # Default steps are post-invite
                position=pos,
                title=meta["title"],
                markdown=meta["markdown"],
                requires=meta["requires"],
            )
            db.session.add(step)

    if server_types_to_import:
        db.session.commit()
        current_app.logger.info(
            f"Successfully imported wizard steps for: {sorted(server_types_to_import)}"
        )

    # NOTE: existing steps are never modified to preserve UI customizations.
    # This function only imports steps for server types that don't exist yet.


# Marker used to recognise the Quick Connect step wherever it ended up. Matching
# on the widget rather than on the title survives an admin renaming the step or
# translating it.
QUICK_CONNECT_MARKER = "widget:quick_connect"
QUICK_CONNECT_SOURCE = BASE_DIR / "jellyfin" / "03_setup_device.md"

# Same idea for the onboarding video step.
VIDEO_MARKER = "widget:video"
VIDEO_SOURCE = BASE_DIR / "jellyfin" / "00_video.md"

# The "before you start" page has no widget to recognise it by, so it carries an
# HTML comment instead. That is editable text and will not survive an admin
# rewording the step, which is why the backfill also leaves a Settings flag
# behind: once it has run, the step is the admin's to edit or delete.
BEFORE_YOU_START_MARKER = "sauron:before-you-start"
BEFORE_YOU_START_SOURCE = BASE_DIR / "jellyfin" / "00_before_you_start.md"
BEFORE_YOU_START_FLAG = "wizard_before_you_start_seeded"

# The text-and-screenshots version of that page, as shipped in 2026.10.18 and
# fingerprinted with _normalised_sha256. Since then the bundled file is a short
# video; a live row is swapped for it only while it still matches this, because
# anything else is an admin's edit. The original is kept in
# tests/fixtures/wizard_before_you_start_2026_10_18.md and a test pins the two.
BEFORE_YOU_START_TEXT_SHA256 = (
    "020e7839bba4fab29f2bcef65188961c278a6163032f525d23756ec174f00ca7"
)
BEFORE_YOU_START_VIDEO_FLAG = "wizard_before_you_start_video_seeded"


def _normalised_sha256(text: str) -> str:
    """Fingerprint *text* ignoring line endings and trailing whitespace.

    Saving a step from the admin's textarea sends it back with CRLF line endings.
    That is not an edit, and comparing raw bytes would quietly skip the backfill.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalised = "\n".join(line.rstrip() for line in lines).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def ensure_quick_connect_step() -> None:
    """Add the Jellyfin device-setup step to installations that predate it.

    ``import_default_wizard_steps`` deliberately only seeds server types that
    are missing entirely, so a server already running Jellyfin would never see a
    newly shipped step. This is the narrow exception: without it the Quick
    Connect feature ships as code nobody can reach.

    Idempotent and additive. It appends when the marker is absent and otherwise
    does nothing at all — it never edits or reorders an existing row, which
    keeps the module's promise not to trample UI customisations.
    """
    inspector = inspect(db.engine)
    if not inspector.has_table(WizardStep.__tablename__):
        return

    if not QUICK_CONNECT_SOURCE.exists():
        return

    existing = (
        db.session.query(WizardStep).filter(WizardStep.server_type == "jellyfin").all()
    )

    # Nothing seeded for Jellyfin yet: this is a fresh install and
    # import_default_wizard_steps already picks the file up from disk.
    if not existing:
        return

    # Marker check spans every category: an admin may have moved the step.
    if any(QUICK_CONNECT_MARKER in (row.markdown or "") for row in existing):
        return

    # Position is unique per (server_type, category), so it has to be computed
    # within post_invite alone — the highest pre_invite position would collide
    # or leave a gap.
    post_invite = [row for row in existing if row.category == "post_invite"]
    next_position = max((row.position for row in post_invite), default=-1) + 1

    meta = _parse_markdown(QUICK_CONNECT_SOURCE)
    step = WizardStep(
        server_type="jellyfin",
        category="post_invite",
        position=next_position,
        title=meta["title"],
        markdown=meta["markdown"],
        requires=meta["requires"],
    )
    db.session.add(step)
    db.session.commit()
    current_app.logger.info("Added the Jellyfin Quick Connect wizard step")


def ensure_video_step() -> None:
    """Open the Jellyfin wizard with the video, then the device setup.

    Same narrow exception as ``ensure_quick_connect_step``: the seeder only
    bootstraps server types that are missing entirely, so a live Jellyfin install
    would never receive this.

    Two things happen here, both once, both guarded by the video marker:

    1. The video goes in at position 0. It is the introduction — it narrates what
       the remaining steps then let you do, so it cannot sit anywhere else.
    2. The Quick Connect step is pulled up right behind it. ``ensure_quick_connect
       _step`` *appends*, so on installs that already had wizard steps it landed
       after the tips page, leaving the video followed by reading material and the
       actual "connect your device" instructions at the very end.

    Everything else keeps its relative order. This reorders; it never deletes or
    rewrites content. The two text steps the video supersedes ("What is
    Jellyfin?" and "Download Jellyfin Clients") are gone from the bundled files
    for fresh installs, but on an existing install they stay put until an admin
    removes them in the UI — they may have been edited, and that is not a call
    this function gets to make.
    """
    inspector = inspect(db.engine)
    if not inspector.has_table(WizardStep.__tablename__):
        return

    if not VIDEO_SOURCE.exists():
        return

    existing = (
        db.session.query(WizardStep).filter(WizardStep.server_type == "jellyfin").all()
    )

    # Nothing seeded for Jellyfin yet: this is a fresh install and
    # import_default_wizard_steps already picks the file up from disk, where its
    # 00_ filename prefix sorts it ahead of the steps it introduces.
    if not existing:
        return

    # Marker check spans every category: an admin may have moved the step. The
    # "before you start" page embeds a video of its own since 2026.10.20, so the
    # widget alone no longer identifies this step.
    if any(
        VIDEO_MARKER in (row.markdown or "")
        and BEFORE_YOU_START_MARKER not in (row.markdown or "")
        for row in existing
    ):
        return

    meta = _parse_markdown(VIDEO_SOURCE)
    video = WizardStep(
        server_type="jellyfin",
        category="post_invite",
        position=_PARKING_OFFSET,
        title=meta["title"],
        markdown=meta["markdown"],
        requires=meta["requires"],
    )
    db.session.add(video)

    post_invite = sorted(
        (row for row in existing if row.category == "post_invite"),
        key=lambda row: row.position,
    )
    quick_connect = [
        row for row in post_invite if QUICK_CONNECT_MARKER in (row.markdown or "")
    ]
    rest = [row for row in post_invite if row not in quick_connect]

    _renumber([video, *quick_connect, *rest])
    db.session.commit()
    current_app.logger.info("Added the Jellyfin onboarding video wizard step")


def ensure_before_you_start_step() -> None:
    """Open the Jellyfin wizard with the "before you start" page.

    Same narrow exception as the two backfills above, for the same reason: the
    seeder only bootstraps server types that are missing entirely.

    It goes in at position 0, ahead of the video. The video introduces the
    device setup that follows it; this page is about none of that — renewing,
    recovering a login, freeing a device slot — so it sits in front rather than
    between the two. Everything else keeps its relative order, and nothing
    already there is edited.

    Runs once. The flag, not the marker, is what makes that hold on an install
    whose admin has since reworded or removed the step.
    """
    inspector = inspect(db.engine)
    if not inspector.has_table(WizardStep.__tablename__):
        return

    if not BEFORE_YOU_START_SOURCE.exists():
        return

    if Settings.query.filter_by(key=BEFORE_YOU_START_FLAG).first():
        return

    existing = (
        db.session.query(WizardStep).filter(WizardStep.server_type == "jellyfin").all()
    )

    # Nothing seeded for Jellyfin yet: this is a fresh install and
    # import_default_wizard_steps already picks the file up from disk, where its
    # filename sorts it into position 0 on its own.
    if not existing:
        return

    # Already there — seeded from disk on a fresh install, or moved by an admin.
    if not any(BEFORE_YOU_START_MARKER in (row.markdown or "") for row in existing):
        meta = _parse_markdown(BEFORE_YOU_START_SOURCE)
        step = WizardStep(
            server_type="jellyfin",
            category="post_invite",
            position=_PARKING_OFFSET,
            title=meta["title"],
            markdown=meta["markdown"],
            requires=meta["requires"],
        )
        db.session.add(step)

        post_invite = sorted(
            (row for row in existing if row.category == "post_invite"),
            key=lambda row: row.position,
        )
        _renumber([step, *post_invite])
        current_app.logger.info("Added the Jellyfin 'before you start' wizard step")

    db.session.add(Settings(key=BEFORE_YOU_START_FLAG, value="1"))
    db.session.commit()


def ensure_before_you_start_video() -> None:
    """Swap the text "before you start" page for its video, once.

    The page shipped in 2026.10.18 as text with six screenshots; the bundled file
    is now a ~55 s video with the two links underneath that a video cannot make
    clickable. Fresh installs read that file from disk. Live installs hold the
    old text in the database and the seeder never edits a row, so this replaces
    it — but only while the row is still exactly what shipped. An admin who
    reworded it keeps their version, and the log says so.

    Runs once, recorded by its own flag, so pasting the old text back later is
    not undone at the next restart. Position and title are left alone.
    """
    inspector = inspect(db.engine)
    if not inspector.has_table(WizardStep.__tablename__):
        return

    if not BEFORE_YOU_START_SOURCE.exists():
        return

    if Settings.query.filter_by(key=BEFORE_YOU_START_VIDEO_FLAG).first():
        return

    existing = (
        db.session.query(WizardStep).filter(WizardStep.server_type == "jellyfin").all()
    )

    # Fresh install: import_default_wizard_steps already seeded the video version.
    if not existing:
        return

    page = next(
        (row for row in existing if BEFORE_YOU_START_MARKER in (row.markdown or "")),
        None,
    )
    if page is not None:
        bundled = _parse_markdown(BEFORE_YOU_START_SOURCE)["markdown"]
        current = _normalised_sha256(page.markdown)
        if current == BEFORE_YOU_START_TEXT_SHA256:
            page.markdown = bundled
            current_app.logger.info(
                "Replaced the Jellyfin 'before you start' text with its video"
            )
        elif current != _normalised_sha256(bundled):
            current_app.logger.warning(
                "Jellyfin 'before you start' step was edited by an admin; left as "
                "is instead of replacing it with the video"
            )

    db.session.add(Settings(key=BEFORE_YOU_START_VIDEO_FLAG, value="1"))
    db.session.commit()


# Positions are moved out to this range before being written back in their final
# order. (server_type, category, position) is unique and SQLite checks it per
# statement, so without the detour a reorder collides with the rows it has not
# moved yet.
_PARKING_OFFSET = 1000


def _renumber(rows: list[WizardStep]) -> None:
    """Give *rows* positions 0..n-1 in the order supplied."""
    for index, row in enumerate(rows):
        row.position = _PARKING_OFFSET + index
    db.session.flush()

    for index, row in enumerate(rows):
        row.position = index
    db.session.flush()
