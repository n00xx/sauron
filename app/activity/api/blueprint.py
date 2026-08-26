"""
Activity monitoring blueprint for Wizarr.

Provides routes for activity dashboard, analytics, and API endpoints
for managing and viewing media playback activity data.
"""

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required
from sqlalchemy.orm import joinedload

try:
    from flask_babel import gettext as _

    from app.extensions import db, limiter, scaled_limit
    from app.models import HistoricalImportJob, MediaServer
except ImportError:
    # For testing without Flask app context
    MediaServer = None  # type: ignore
    db = None  # type: ignore
    HistoricalImportJob = None  # type: ignore

    def _(x):  # type: ignore
        return x

    # Rate-limit decorators are applied at import time, so this fallback has to
    # produce a working pass-through decorator rather than None.
    def scaled_limit(limit_string: str):  # type: ignore
        return limit_string

    class _NoopLimiter:
        @staticmethod
        def limit(*_args, **_kwargs):
            def decorator(func):
                return func

            return decorator

    limiter = _NoopLimiter()  # type: ignore


from app.activity.domain.models import ActivityQuery
from app.models import ActivitySession, ActivitySnapshot
from app.services.activity import ActivityService
from app.services.historical import HistoricalDataService

# Create blueprint
activity_bp = Blueprint(
    "activity",
    __name__,
    url_prefix="/activity",
    template_folder="../templates",
)


# Helper utilities ---------------------------------------------------------


def _activity_settings_template() -> str:
    """Return template path for activity settings based on HX context."""
    return (
        "activity/settings_tab.html"
        if request.headers.get("HX-Request")
        else "activity/settings.html"
    )


def _default_monitor_status() -> dict[str, object]:
    """Provide a fallback monitor status structure."""
    return {"monitoring_enabled": False, "connection_status": {}}


def _load_monitor_status() -> dict[str, object]:
    """Return current activity monitor status."""
    monitor = getattr(current_app.extensions, "activity_monitor", None)
    return {
        "monitoring_enabled": monitor is not None,
        "connection_status": monitor.get_connection_status() if monitor else {},
    }


def _load_verified_media_servers() -> list:
    """Return verified media servers available for historical import."""
    if MediaServer is None:
        return []

    try:
        return (
            MediaServer.query.filter_by(verified=True)
            .order_by(MediaServer.name.asc())
            .all()
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        structlog.get_logger(__name__).warning(
            "Failed to load media servers: %s", exc, exc_info=True
        )
        return []


def _render_activity_settings(
    *,
    status: dict[str, object] | None = None,
    error: str | None = None,
    success: str | None = None,
    selected_server_id: int | None = None,
    selected_days_back: int | None = None,
):
    """Render the activity settings (full page or partial)."""
    template = _activity_settings_template()
    logger = structlog.get_logger(__name__)

    if status is None:
        try:
            status = _load_monitor_status()
        except Exception as exc:
            logger.error(
                "Failed to load activity settings status: %s", exc, exc_info=True
            )
            status = _default_monitor_status()
            error = error or _("Failed to load settings")

    media_servers = _load_verified_media_servers()
    if selected_days_back is None:
        selected_days_back = request.args.get("days_back", type=int, default=30)

    return render_template(
        template,
        status=status,
        media_servers=media_servers,
        error=error,
        success=success,
        selected_server_id=selected_server_id,
        selected_days_back=selected_days_back,
    )


def _settings_action_response(
    *,
    success: str | None = None,
    error: str | None = None,
    selected_server_id: int | None = None,
    selected_days_back: int | None = None,
):
    """
    Return an appropriate response for activity settings actions.

    HTMX requests receive the re-rendered settings partial. Non-HTMX requests
    flash a message and redirect back to the settings page to avoid duplicate
    submissions.
    """
    if request.headers.get("HX-Request"):
        return _render_activity_settings(
            success=success,
            error=error,
            selected_server_id=selected_server_id,
            selected_days_back=selected_days_back,
        )

    if success:
        flash(success, "success")
    if error:
        flash(error, "error")

    extra_params: dict[str, Any] = {}
    if selected_days_back is not None:
        extra_params["days_back"] = selected_days_back
    return redirect(url_for("activity.activity_settings", **extra_params))


def _render_historical_jobs_partial(server_id: int | None):
    if HistoricalImportJob is None:
        jobs: list = []
    else:
        query = HistoricalImportJob.query.options(
            joinedload(HistoricalImportJob.server)  # type: ignore
        ).order_by(HistoricalImportJob.created_at.desc())

        if server_id:
            query = query.filter(HistoricalImportJob.server_id == server_id)

        jobs = query.limit(10).all()

    return render_template(
        "activity/_historical_jobs.html",
        jobs=jobs,
        selected_server_id=server_id,
    )


def _delete_all_activity_data() -> int:
    """Remove all stored activity sessions and snapshots."""
    if db is None:
        raise RuntimeError("Database not initialised")

    try:
        deleted_snapshots = ActivitySnapshot.query.delete()
        deleted_sessions = ActivitySession.query.delete()
        db.session.commit()
        return (deleted_snapshots or 0) + (deleted_sessions or 0)
    except Exception as exc:
        db.session.rollback()
        raise exc


def _parse_int(form_key: str, default: int) -> int:
    """Parse an integer from request.form with graceful fallback."""
    try:
        value = request.form.get(form_key, default)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# Template filters
@activity_bp.app_template_filter("format_duration")
def format_duration_filter(value):
    """Format duration in hours to human-readable string."""
    if not value or value == 0:
        return "0m"

    hours = int(value)
    minutes = int((value - hours) * 60)

    if hours > 0:
        if minutes > 0:
            return f"{hours}h {minutes}m"
        return f"{hours}h"
    return f"{minutes}m"


@activity_bp.app_template_filter("days_until")
def days_until_filter(value):
    """Time left until a deadline, as a short badge string.

    Used for dispute response windows, where "2d left" is the number that
    decides what the operator does next.
    """
    if not value:
        return "-"
    deadline = value if value.tzinfo else value.replace(tzinfo=UTC)
    remaining = deadline - datetime.now(UTC)
    if remaining.total_seconds() <= 0:
        return _("overdue")
    days = remaining.days
    if days >= 1:
        return _("%(n)sd left", n=days)
    hours = int(remaining.total_seconds() // 3600)
    return _("%(n)sh left", n=max(1, hours))


@activity_bp.route("/", methods=["GET"], strict_slashes=False)
@login_required
def activity_dashboard():
    """Display activity index with tabbed interface."""
    return render_template("activity/index.html")


@activity_bp.route("/dashboard")
@login_required
def dashboard_tab():
    """Display dashboard tab with statistics."""
    try:
        activity_service = ActivityService()

        # Get query parameters
        days = int(request.args.get("days", 7))

        # Get enhanced activity statistics
        stats = activity_service.get_dashboard_stats(days=days)

        return render_template("activity/dashboard_tab.html", stats=stats, days=days)

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to load dashboard: %s", e, exc_info=True)
        return render_template(
            "activity/dashboard_tab.html",
            error=_("Failed to load dashboard data"),
            stats={},
            days=7,
        )


@activity_bp.route("/history")
@login_required
def history_tab():
    """Display history tab with activity table."""
    try:
        # Get available servers for filtering
        servers = []
        if db is not None:
            servers = db.session.query(MediaServer).filter_by(verified=True).all()

        return render_template("activity/history_tab.html", servers=servers)

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to load history tab: %s", e, exc_info=True)
        return render_template(
            "activity/history_tab.html",
            error=_("Failed to load history data"),
            servers=[],
        )


@activity_bp.route("/grid")
@login_required
def activity_grid():
    """Get activity grid data."""
    try:
        activity_service = ActivityService()

        # Get query parameters
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", 20))  # Table view - fewer rows per page
        days = request.args.get(
            "days", type=int
        )  # None if not provided - shows all data
        server_id = request.args.get("server_id", type=int)
        user_name = request.args.get("user_name")
        media_type = request.args.get("media_type")

        # Calculate offset
        offset = (page - 1) * limit

        # Build query
        if days is None or days == 0:
            # All time - no date filter (default for history tab)
            start_date = None
        else:
            # Apply date filter (for dashboard tab)
            start_date = datetime.now(UTC) - timedelta(days=days)

        sort_by = request.args.get("sort_by", "started_at")
        sort_direction = request.args.get("sort_direction", "desc").lower()

        # Map friendly sort keys to safe model attributes
        sort_field_map: dict[str, str] = {
            "started_at": "started_at",
            "start_time": "started_at",
            "user": "user_name",
            "user_name": "user_name",
            "media_title": "media_title",
        }

        resolved_sort_field = sort_field_map.get(sort_by, "started_at")
        resolved_direction = "asc" if sort_direction == "asc" else "desc"

        query = ActivityQuery(
            server_ids=[server_id] if server_id else None,
            user_names=[user_name] if user_name else None,
            media_types=[media_type] if media_type else None,
            start_date=start_date,
            limit=limit,
            offset=offset,
            order_by=resolved_sort_field,
            order_direction=resolved_direction,
        )

        sessions, total_count = activity_service.get_activity_sessions(query)

        # Perform in-memory sorting for fields not backed by database columns
        if sort_by == "playback":
            status_rank = {
                "transcoding": 3,
                "remux": 2,
                "direct_play": 1,
                "unknown": 0,
            }

            def playback_sort_key(session: ActivitySession):
                info = session.get_transcoding_info()
                is_transcoding = bool(info.get("is_transcoding"))
                is_direct_play = bool(info.get("direct_play"))
                if is_transcoding:
                    bucket = status_rank["transcoding"]
                elif is_direct_play:
                    bucket = status_rank["direct_play"]
                elif info:
                    bucket = status_rank["remux"]
                else:
                    bucket = status_rank["unknown"]
                # Secondary sort by start time for consistency
                started_at = session.started_at or datetime.min.replace(tzinfo=UTC)
                return (bucket, started_at)

            sessions.sort(key=playback_sort_key, reverse=resolved_direction == "desc")

        # Calculate pagination info
        total_pages = (total_count + limit - 1) // limit
        has_next = page < total_pages
        has_prev = page > 1

        return render_template(
            "activity/_activity_table.html",
            sessions=sessions,
            page=page,
            has_next=has_next,
            has_prev=has_prev,
            total_count=total_count,
            total_pages=total_pages,
            sort_by=sort_by,
            sort_direction=resolved_direction,
        )

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to load activity grid: %s", e, exc_info=True)
        return render_template(
            "activity/_activity_table.html",
            sessions=[],
            error=_("Failed to load activity data"),
        )


@activity_bp.route("/summary")
@login_required
def activity_summary():
    """Provide a lightweight snapshot of current activity state for refresh decisions."""
    if db is None:
        return jsonify(
            {
                "active_sessions": 0,
                "latest_started_at": None,
                "latest_updated_at": None,
            }
        )

    try:
        from sqlalchemy import func

        active_sessions = (
            db.session.query(func.count(ActivitySession.id))
            .filter(ActivitySession.active.is_(True))
            .scalar()
            or 0
        )

        latest_started_at = db.session.query(
            func.max(ActivitySession.started_at)
        ).scalar()
        latest_updated_at = db.session.query(
            func.max(ActivitySession.updated_at)
        ).scalar()

        return jsonify(
            {
                "active_sessions": active_sessions,
                "latest_started_at": latest_started_at.isoformat()
                if latest_started_at
                else None,
                "latest_updated_at": latest_updated_at.isoformat()
                if latest_updated_at
                else None,
                "server_time": datetime.now(UTC).isoformat(),
            }
        )
    except Exception as exc:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to load activity summary: %s", exc, exc_info=True)
        return (
            jsonify(
                {
                    "active_sessions": None,
                    "latest_started_at": None,
                    "latest_updated_at": None,
                    "error": _("Failed to load activity summary"),
                }
            ),
            500,
        )


@activity_bp.route("/stats")
@login_required
def activity_stats():
    """Get activity statistics."""
    try:
        activity_service = ActivityService()
        days = int(request.args.get("days", 7))

        stats = activity_service.get_activity_stats(days=days)
        return jsonify(stats)

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to get activity stats: %s", e, exc_info=True)
        return jsonify({"error": _("Failed to get activity statistics")}), 500


@activity_bp.route("/session/<int:session_id>")
@login_required
def activity_session(session_id):
    """Get session details."""
    try:
        if db is None:
            return jsonify({"error": _("Database not available")}), 500

        session = db.get_or_404(ActivitySession, session_id)

        return jsonify(session.to_dict())

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error(
            "Failed to get session %s: %s",
            session_id,
            e,
            exc_info=True,
        )
        return jsonify({"error": _("Failed to get session details")}), 500


@activity_bp.route("/export")
@login_required
def activity_export():
    """Export activity data as CSV or JSON."""
    try:
        activity_service = ActivityService()

        # Get query parameters
        format_type = request.args.get("format", "csv").lower()
        days = int(request.args.get("days", 30))
        server_id = request.args.get("server_id", type=int)
        user_name = request.args.get("user_name")

        # Build query
        query = ActivityQuery(
            server_ids=[server_id] if server_id else None,
            user_names=[user_name] if user_name else None,
            start_date=datetime.now(UTC) - timedelta(days=days),
            order_by="started_at",
            order_direction="desc",
        )

        sessions, _ = activity_service.get_activity_sessions(query)

        if format_type == "json":
            return jsonify([session.to_dict() for session in sessions])
        # CSV export
        import csv
        import io

        from flask import Response

        output = io.StringIO()
        writer = csv.writer(output)

        # Write headers
        writer.writerow(
            [
                "Session ID",
                "User Name",
                "Media Title",
                "Media Type",
                "Started At",
                "Duration (minutes)",
                "Device Name",
                "Client Name",
                "Server ID",
            ]
        )

        # Write data
        for session in sessions:
            writer.writerow(
                [
                    session.session_id,
                    session.user_name,
                    session.media_title,
                    session.media_type,
                    session.started_at.isoformat() if session.started_at else "",
                    session.duration_minutes,
                    session.device_name,
                    session.client_name,
                    session.server_id,
                ]
            )

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=activity_export_{days}days.csv"
            },
        )

    except Exception as e:
        logger = structlog.get_logger(__name__)
        logger.error("Failed to export activity data: %s", e, exc_info=True)
        return (
            jsonify({"error": _("Failed to export activity data")}),  # type: ignore
            500,
        )


@activity_bp.route("/settings", methods=["GET", "POST"])
@login_required
def activity_settings():
    """Activity monitoring settings."""
    if request.method == "POST":
        try:
            action = request.form.get("action")

            if action == "restart_monitoring":
                monitor = getattr(current_app.extensions, "activity_monitor", None)
                if monitor:
                    monitor.stop_monitoring()
                    monitor.start_monitoring()
                    return jsonify(
                        {"success": True, "message": _("Monitoring restarted")}
                    )
                return jsonify(
                    {"success": False, "message": _("Monitor not available")}
                )

            if action == "cleanup_old_data":
                activity_service = ActivityService()
                retention_days = int(request.form.get("retention_days", 90))
                deleted_count = activity_service.cleanup_old_activity(retention_days)
                return jsonify(
                    {
                        "success": True,
                        "message": _("Cleaned up {} old activity records").format(
                            deleted_count
                        ),
                    }
                )

            if action == "end_stale_sessions":
                activity_service = ActivityService()
                timeout_hours = int(request.form.get("timeout_hours", 24))
                ended_count = activity_service.end_stale_sessions(timeout_hours)
                return jsonify(
                    {
                        "success": True,
                        "message": _("Ended {} stale sessions").format(ended_count),
                    }
                )

            return jsonify({"success": False, "message": _("Unknown action")})

        except Exception as e:
            logger = structlog.get_logger(__name__)
            logger.error("Failed to update activity settings: %s", e, exc_info=True)
            return jsonify(
                {"success": False, "message": _("Failed to update settings")}
            ), 500

    return _render_activity_settings()


@activity_bp.route("/settings/delete-activity-data", methods=["POST"])
@login_required
def delete_activity_data():
    """Delete all stored activity monitoring data."""
    logger = structlog.get_logger(__name__)

    try:
        deleted = _delete_all_activity_data()
        success_message = _(
            "Activity data has been successfully deleted ({} records)."
        ).format(deleted)
        return _settings_action_response(success=success_message)
    except Exception as exc:
        logger.error("Failed to delete activity data: %s", exc, exc_info=True)
        error_message = _("Failed to delete activity data: {}").format(str(exc))
        return _settings_action_response(error=error_message)


@activity_bp.route("/settings/import-historical-data", methods=["POST"])
@login_required
def import_historical_activity():
    """Import historical viewing data for a selected server."""
    logger = structlog.get_logger(__name__)
    server_id = request.form.get("server_id", type=int)
    days_back = _parse_int("days_back", 30)
    days_back = max(1, min(days_back, 365))
    max_results_raw = request.form.get("max_results")
    max_results = None
    if max_results_raw not in (None, ""):
        parsed_limit = _parse_int("max_results", 0)
        max_results = parsed_limit if parsed_limit > 0 else None

    if not server_id:
        return _settings_action_response(
            error=_("Please select a media server."),
            selected_days_back=days_back,
        )

    try:
        if MediaServer is None:
            raise RuntimeError("Media server model unavailable")

        media_server = MediaServer.query.get(server_id)
        if not media_server:
            return _settings_action_response(
                error=_("Media server not found."),
                selected_server_id=server_id,
                selected_days_back=days_back,
            )

        supported_servers = {"plex", "jellyfin", "emby", "audiobookshelf"}
        server_type = (media_server.server_type or "").lower()

        if server_type not in supported_servers:
            return _settings_action_response(
                error=_(
                    "Historical data import is currently only supported for Plex, Jellyfin, Emby, and AudiobookShelf servers."
                ),
                selected_server_id=server_id,
                selected_days_back=days_back,
            )

        service = HistoricalDataService(server_id)
        job = service.start_async_import(days_back=days_back, max_results=max_results)

        server_label = server_type.title()
        success_message = _(
            "Historical import job #{job_id} started for {server} (last {days} days)."
        ).format(job_id=job.id, server=server_label, days=days_back)
        return _settings_action_response(
            success=success_message,
            selected_server_id=server_id,
            selected_days_back=days_back,
        )

    except Exception as exc:
        logger.error("Failed to import historical data: %s", exc, exc_info=True)
        error_message = _("Failed to import historical data: {}").format(str(exc))
        return _settings_action_response(
            error=error_message,
            selected_server_id=server_id,
            selected_days_back=days_back,
        )


@activity_bp.route("/settings/clear-historical-data", methods=["POST"])
@login_required
def clear_historical_activity():
    """Remove imported historical data for the selected server."""
    logger = structlog.get_logger(__name__)
    server_id = request.form.get("server_id", type=int)

    if not server_id:
        return _settings_action_response(error=_("Please select a media server."))

    try:
        service = HistoricalDataService(server_id)
        result = service.clear_historical_data()

        if result.get("success"):
            success_message = _("Successfully cleared {} historical entries.").format(
                result.get("deleted_count", 0)
            )
            return _settings_action_response(
                success=success_message, selected_server_id=server_id
            )

        error_message = _("Failed to clear historical data: {}").format(
            result.get("error", _("Unknown error"))
        )
        return _settings_action_response(
            error=error_message, selected_server_id=server_id
        )

    except Exception as exc:
        logger.error("Failed to clear historical data: %s", exc, exc_info=True)
        error_message = _("Failed to clear historical data: {}").format(str(exc))
        return _settings_action_response(
            error=error_message, selected_server_id=server_id
        )


@activity_bp.route("/settings/historical-jobs", methods=["GET"])
@login_required
def historical_import_jobs():
    """Return recent historical import jobs for display."""
    server_id = request.args.get("server_id", type=int)
    return _render_historical_jobs_partial(server_id)


@activity_bp.route("/settings/historical-jobs/<int:job_id>/delete", methods=["POST"])
@login_required
def delete_historical_job(job_id: int):
    """Delete a historical import job (typically failed/completed ones)."""
    if HistoricalImportJob is None:
        return "", 204

    server_id = request.form.get("server_id", type=int)
    job = HistoricalImportJob.query.get(job_id)

    if not job:
        return _render_historical_jobs_partial(server_id)

    if job.is_active:
        return (
            jsonify(
                {"error": _("Cannot remove a job that is still queued or running.")}
            ),
            400,
        )

    db.session.delete(job)
    db.session.commit()
    return _render_historical_jobs_partial(server_id)


@activity_bp.route("/settings/historical-data-stats/<int:server_id>")
@login_required
def historical_data_stats(server_id: int):
    """Expose stored historical data statistics for a server."""
    try:
        service = HistoricalDataService(server_id)
        stats = service.get_import_statistics()
        return jsonify(stats)
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to load historical data stats: %s", exc, exc_info=True
        )
        return (
            jsonify(
                {
                    "total_entries": 0,
                    "unique_users": 0,
                    "date_range": {"oldest": None, "newest": None},
                    "error": _("An internal error has occurred."),
                }
            ),
            500,
        )


# ─────────────────────────── Stripe events (Eventos) ───────────────────────
#
# Read-only by design. sauron holds a RESTRICTED, READ-ONLY Stripe key and never
# writes to Stripe: the click that moves money or submits evidence stays in the
# Stripe dashboard, where it has confirmations and an audit trail. What this tab
# adds is the evidence Stripe cannot produce on its own — proof the buyer
# actually used the service.

EVENTOS_PAGE_SIZE = 25


def _default_livemode() -> str:
    """Which mode to show when the user has not picked one.

    Live by default — sandbox events share this table and must never be mistaken
    for real money. But if there is no live traffic at all and test events do
    exist, show those instead: the alternative is an empty tab right after a
    successful sync, which reads as "it's broken" rather than "wrong mode". The
    selector still shows which mode won, so nothing is hidden.
    """
    from app.models import StripeEvent

    try:
        if StripeEvent.query.filter(StripeEvent.livemode.is_(True)).first():
            return "true"
        if StripeEvent.query.filter(StripeEvent.livemode.is_(False)).first():
            return "false"
    except Exception as exc:  # pragma: no cover - table may not exist yet
        structlog.get_logger(__name__).debug(
            "Could not pick a default Stripe mode: %s", exc
        )
    return "true"


def _eventos_filters() -> dict[str, object]:
    """Read filter args once, so tab and grid always agree on the query."""
    return {
        "category": request.args.get("category") or None,
        "severity": request.args.get("severity") or None,
        "event_type": request.args.get("event_type") or None,
        "search": (request.args.get("search") or "").strip() or None,
        "livemode": request.args.get("livemode") or _default_livemode(),
        "days": request.args.get("days", type=int),
    }


def _eventos_query(filters: dict[str, object]):
    from app.models import StripeEvent

    query = StripeEvent.query

    if filters["livemode"] in ("true", "false"):
        query = query.filter(StripeEvent.livemode.is_(filters["livemode"] == "true"))
    if filters["category"]:
        query = query.filter(StripeEvent.category == filters["category"])
    if filters["severity"]:
        query = query.filter(StripeEvent.severity == filters["severity"])
    if filters["event_type"]:
        query = query.filter(StripeEvent.type == filters["event_type"])
    if filters["days"]:
        cutoff = datetime.now(UTC) - timedelta(days=int(filters["days"]))
        query = query.filter(StripeEvent.created_at_stripe >= cutoff)
    if filters["search"]:
        term = f"%{filters['search']}%"
        query = query.filter(
            db.or_(
                StripeEvent.customer_email.ilike(term),
                StripeEvent.object_id.ilike(term),
                StripeEvent.payment_intent_id.ilike(term),
                StripeEvent.charge_id.ilike(term),
                StripeEvent.stripe_event_id.ilike(term),
            )
        )
    return query


@activity_bp.route("/eventos")
@login_required
def eventos_tab():
    """Display the Stripe events tab."""
    return _render_eventos_tab()


# Terminal dispute outcomes. Once Stripe reports one of these the case is
# decided and there is nothing left to submit, so it must not sit in a panel
# headed "Disputes awaiting response".
_DISPUTE_SETTLED_STATUSES = frozenset({"won", "lost", "warning_closed"})

# How many disputes the action queue shows at once.
DISPUTE_QUEUE_SIZE = 20


def _open_disputes(base, limit: int = DISPUTE_QUEUE_SIZE) -> list:
    """One row per DISPUTE still awaiting a response, soonest deadline first.

    Stripe emits up to five events for a single dispute — created, updated,
    closed, funds_withdrawn, funds_reinstated — and each is its own row here,
    carrying the same dispute id in ``object_id`` and the same
    ``evidence_details.due_by``. Reading rows straight out of the table showed
    one chargeback up to five times and counted every copy, so a single dispute
    looked like a pile of deadlines that were all the same deadline.

    Two rules, in this order:

      * one entry per ``object_id``, keeping the most recent event, because the
        row links to an event detail page and must show the latest state;
      * drop a dispute whose latest event reports a terminal outcome. Nothing
        used to filter on the outcome, so a dispute already won or lost stayed
        in the queue until its response window lapsed — telling an operator to
        answer a case that was already settled.
    """
    # Imported here, not at module scope: the top of this file tolerates the
    # models being unavailable so it can be imported without an app context.
    from app.models import StripeEvent
    from app.services.stripe_events import MONITORED_EVENT_TYPES

    # Every row for one dispute carries the same due_by, so ordering by deadline
    # keeps them adjacent: reading `limit` x (events per dispute) rows is enough
    # to be certain the earliest `limit` DISTINCT disputes are all in hand.
    # Derived from the catalogue instead of hardcoded, so adding a sixth dispute
    # event type cannot quietly start truncating the queue.
    events_per_dispute = max(
        1,
        sum(1 for name in MONITORED_EVENT_TYPES if name.startswith("charge.dispute.")),
    )

    rows = (
        base.filter(
            StripeEvent.category == "dispute",
            StripeEvent.dispute_due_by.isnot(None),
            StripeEvent.dispute_due_by >= datetime.now(UTC),
        )
        .order_by(
            StripeEvent.dispute_due_by.asc(),
            StripeEvent.created_at_stripe.desc(),
        )
        .limit(limit * events_per_dispute)
        .all()
    )

    queue: list = []
    seen: set[str] = set()
    for row in rows:
        if len(queue) >= limit:
            break
        # object_id is nullable. A dispute whose id never extracted cannot be
        # grouped, and collapsing all the id-less rows together would hide a
        # live chargeback — the worst possible outcome of a de-duplication fix.
        # So they are each kept as their own entry.
        if row.object_id is not None:
            if row.object_id in seen:
                continue
            seen.add(row.object_id)
        # Judged on the newest event, which is the one reached first here.
        # An unknown status keeps the dispute visible: silence is not a verdict.
        if (row.status or "").lower() in _DISPUTE_SETTLED_STATUSES:
            continue
        queue.append(row)

    return queue


def _dispute_count(base) -> int:
    """How many DISPUTES are in the window, not how many dispute events.

    The summary card sat next to the action queue reading straight off the row
    count, so a single chargeback with five events showed "Disputes: 5" beside a
    queue listing it once. Every other card on that row is effectively an entity
    count already, which made this the odd one out and the more believable of
    the two numbers.

    Unlike the queue this counts everything in the window — settled or not,
    deadline passed or not. It is a summary, not an action list.
    """
    from sqlalchemy import distinct, func

    from app.models import StripeEvent

    disputes = base.filter(StripeEvent.category == "dispute")

    identified = (
        disputes.filter(StripeEvent.object_id.isnot(None))
        .with_entities(func.count(distinct(StripeEvent.object_id)))
        .scalar()
        or 0
    )
    # COUNT(DISTINCT ...) skips NULLs, and a dispute whose id never extracted is
    # still a dispute. Counted as one apiece rather than collapsed together.
    unidentified = disputes.filter(StripeEvent.object_id.is_(None)).count()

    return identified + unidentified


def _render_eventos_tab(message: str | None = None, message_kind: str = "success"):
    """Render the Eventos tab, optionally with a result banner.

    Save and "Sync now" render through here rather than redirecting: nothing in
    this app renders `get_flashed_messages`, so a flashed sync error would be
    invisible — the admin would see an empty tab and no reason why.
    """
    from app.services.scheduler_health import check_stripe_sync_health
    from app.services.stripe_events import (
        MONITORED_EVENT_TYPES,
        describe_key_mode,
        get_last_sync_summary,
        get_setting,
        is_sync_enabled,
    )

    try:
        from app.models import StripeEvent

        filters = _eventos_filters()

        # "all" means no mode filter. Without this branch the summary cards
        # would silently fall back to test-mode counts while the table below
        # showed both — two different numbers on one screen.
        base = StripeEvent.query
        if filters["livemode"] in ("true", "false"):
            base = base.filter(StripeEvent.livemode.is_(filters["livemode"] == "true"))

        # The action queue: disputes still inside their response window, plus
        # unresolved fraud warnings. Ordered by deadline — an unanswered dispute
        # is a lost dispute. One entry per dispute, not per event; see
        # _open_disputes.
        open_disputes = _open_disputes(base)
        fraud_warnings = (
            base.filter(StripeEvent.type == "radar.early_fraud_warning.created")
            .order_by(StripeEvent.created_at_stripe.desc())
            .limit(20)
            .all()
        )

        def _count(**kwargs) -> int:
            query = base
            for key, value in kwargs.items():
                query = query.filter(getattr(StripeEvent, key) == value)
            return query.count()

        stats = {
            "total": base.count(),
            "payments_ok": _count(type="payment_intent.succeeded"),
            "payments_failed": _count(type="payment_intent.payment_failed"),
            "refunds": _count(type="charge.refunded"),
            # One per dispute, not per event — see _dispute_count.
            "disputes": _dispute_count(base),
            "disputes_open": len(open_disputes),
            "fraud_warnings": len(fraud_warnings),
            "errors": base.filter(
                StripeEvent.severity.in_(["error", "critical"])
            ).count(),
        }

        return render_template(
            "activity/eventos_tab.html",
            stats=stats,
            open_disputes=open_disputes,
            fraud_warnings=fraud_warnings,
            filters=filters,
            event_types=sorted(MONITORED_EVENT_TYPES),
            configured=bool(get_setting("stripe_api_key")),
            sync_enabled=is_sync_enabled(),
            last_sync=get_setting("stripe_last_sync_at"),
            last_error=get_setting("stripe_sync_last_error"),
            interval=get_setting("stripe_sync_interval_minutes", "15"),
            key_mode=describe_key_mode(get_setting("stripe_api_key")),
            last_summary=_decorate_summary(get_last_sync_summary()),
            sync_health=check_stripe_sync_health(),
            message=message,
            message_kind=message_kind,
        )
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to load eventos tab: %s", exc, exc_info=True
        )
        return render_template(
            "activity/eventos_tab.html",
            error=_("Failed to load Stripe events"),
            stats={},
            open_disputes=[],
            fraud_warnings=[],
            filters=_eventos_filters(),
            event_types=[],
            configured=False,
            sync_enabled=False,
            key_mode="unknown",
            last_summary={},
        )


@activity_bp.route("/eventos/grid")
@login_required
def eventos_grid():
    """Paginated Stripe event table."""
    from app.models import StripeEvent

    try:
        filters = _eventos_filters()
        page = max(1, request.args.get("page", 1, type=int))
        query = _eventos_query(filters)

        total_count = query.count()
        events = (
            query.order_by(StripeEvent.created_at_stripe.desc())
            .offset((page - 1) * EVENTOS_PAGE_SIZE)
            .limit(EVENTOS_PAGE_SIZE)
            .all()
        )

        total_pages = (total_count + EVENTOS_PAGE_SIZE - 1) // EVENTOS_PAGE_SIZE
        return render_template(
            "activity/_eventos_table.html",
            events=events,
            page=page,
            total_count=total_count,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1,
            filters=filters,
        )
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to load eventos grid: %s", exc, exc_info=True
        )
        return render_template(
            "activity/_eventos_table.html",
            events=[],
            error=_("Failed to load Stripe events"),
            filters=_eventos_filters(),
        )


@activity_bp.route("/eventos/<int:event_id>")
@login_required
def eventos_detail(event_id: int):
    """Event detail with the sauron-side evidence packet."""
    from app.models import StripeEvent
    from app.services.stripe_evidence import build_evidence_packet

    event = db.session.get(StripeEvent, event_id)
    if event is None:
        return render_template(
            "activity/_eventos_detail.html", event=None, error=_("Event not found")
        ), 404

    try:
        packet = build_evidence_packet(event)
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to build evidence packet: %s", exc, exc_info=True
        )
        packet = None

    return render_template("activity/_eventos_detail.html", event=event, packet=packet)


def _format_window(iso: str | None) -> str:
    """ISO timestamp → 'YYYY-MM-DD HH:MM UTC', or the raw value if unparseable."""
    if not iso:
        return "?"
    try:
        parsed = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _describe_unmonitored(pairs: list) -> str:
    """'account.updated x12, payout.paid x5' from the summary's histogram."""
    return ", ".join(
        f"{pair[0]} ×{pair[1]}"
        for pair in pairs
        if isinstance(pair, list | tuple) and len(pair) == 2
    )


def _decorate_summary(summary: dict) -> dict:
    """Add display-ready fields to a stored sync summary.

    Returns ``{}`` unchanged for "never synced", so the template can treat a
    falsy summary as "no diagnostics yet".
    """
    if not summary:
        return {}
    decorated = dict(summary)
    decorated["window_label"] = _format_window(summary.get("window_start"))
    decorated["finished_label"] = _format_window(summary.get("finished_at"))
    decorated["unmonitored_label"] = _describe_unmonitored(
        summary.get("unmonitored_types") or []
    )
    return decorated


def _sync_result_message(summary: dict) -> tuple[str, str]:
    """Turn a sync summary into (message, kind).

    Every outcome that used to collapse into "no new events" gets its own
    sentence. The distinction that matters most: "everything I saw was already
    stored" is a healthy steady state, while "I saw events but none of a type
    this integration can produce" means the key is pointed somewhere else — and
    those two used to render identically.
    """
    inserted = summary.get("inserted", 0)
    fetched = summary.get("fetched", 0)
    monitored = summary.get("monitored", 0)
    already = summary.get("skipped", 0)
    failed = summary.get("failed", 0)
    window = _format_window(summary.get("window_start"))

    failure_note = (
        " "
        + _(
            "%(failed)s could not be stored — check the log.",
            failed=failed,
        )
        if failed
        else ""
    )

    if inserted:
        return (
            _("Sync completed: %(new)s new events stored.", new=inserted)
            + failure_note,
            "error" if failed else "success",
        )

    if not fetched:
        return (
            _(
                "Sync completed, but Stripe returned no events at all since "
                "%(window)s. The key works — this account simply has no event "
                "history in that window.",
                window=window,
            ),
            "warning",
        )

    if not monitored:
        # The loud case. Stripe answered with a full page of events and not one
        # of them was a payment, refund, dispute or fraud signal — which a
        # storefront cannot be true of.
        detail = _describe_unmonitored(summary.get("unmonitored_types") or [])
        mode_note = ""
        live = summary.get("fetched_livemode", 0)
        test = summary.get("fetched_testmode", 0)
        if live and not test:
            mode_note = " " + _("All of them are live-mode events.")
        elif test and not live:
            mode_note = " " + _("All of them are test-mode events.")
        return (
            _(
                "Sync completed, but none of the %(seen)s events Stripe returned "
                "since %(window)s are types sauron monitors (%(types)s). That "
                "usually means this key belongs to a different account or "
                "sandbox than the one taking payments.",
                seen=fetched,
                window=window,
                types=detail or _("no recognisable types"),
            )
            + mode_note,
            "warning",
        )

    return (
        _(
            "Sync completed: nothing new. All %(known)s monitored events since "
            "%(window)s were already stored.",
            known=already,
            window=window,
        )
        + failure_note,
        "error" if failed else "success",
    )


@activity_bp.route("/eventos/sync", methods=["POST"])
@login_required
def eventos_sync():
    """Run a sync now, instead of waiting for the next scheduled tick.

    ``full_backfill`` re-reads Stripe's whole 30-day retention window instead of
    resuming from the watermark. That is the escape hatch for a key that was
    swapped while the watermark still pointed into another account's stream.
    """
    from app.services.stripe_events import sync_stripe_events
    from app.services.stripe_evidence import resolve_pending_links

    full_backfill = request.form.get("full_backfill") == "1"

    try:
        summary = sync_stripe_events(force=True, full_backfill=full_backfill)
        if summary.get("error"):
            # Surfaced verbatim: "401 — check the restricted key" is actionable,
            # a generic "sync failed" is not.
            return _render_eventos_tab(
                _("Stripe sync failed: %(err)s", err=summary["error"]), "error"
            )
        if summary.get("skipped"):
            return _render_eventos_tab(
                _("Stripe sync skipped: no API key configured."), "error"
            )

        resolve_pending_links()
        message, kind = _sync_result_message(summary)
        return _render_eventos_tab(message, kind)
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Manual Stripe sync failed: %s", exc, exc_info=True
        )
        return _render_eventos_tab(_("Stripe sync failed."), "error")


def _refresh_stripe_sync_job() -> None:
    """(Re)register or drop the Stripe sync job to match the saved settings.

    Never raises: the settings themselves are already committed by the time this
    runs, and a scheduler hiccup must not surface as a failed save.

    This used to return early whenever the scheduler was not running, which made
    the worst case unrecoverable from the UI: an admin could save a valid key,
    read "Settings saved", and get no sync at all until someone restarted the
    container. ``ensure_stripe_sync_job`` starts the scheduler instead, and logs
    above debug when it cannot.
    """
    from app.services.scheduler_health import ensure_stripe_sync_job

    try:
        app = current_app._get_current_object()  # type: ignore[attr-defined]
        ensure_stripe_sync_job(app)
    except Exception as exc:  # pragma: no cover - defensive
        structlog.get_logger(__name__).warning(
            "Could not refresh the Stripe sync job: %s", exc
        )


@activity_bp.route("/eventos/settings", methods=["POST"])
@login_required
def eventos_settings():
    """Save the Stripe polling settings."""
    from app.services.stripe_events import (
        describe_key_mode,
        get_setting,
        reset_sync_watermark,
        set_setting,
    )

    try:
        submitted_key = (request.form.get("stripe_api_key") or "").strip()
        clearing = request.form.get("clear_api_key") == "1"
        previous_key = get_setting("stripe_api_key")

        # An untouched masked field must never wipe the stored key.
        if clearing:
            set_setting("stripe_api_key", None)
        elif submitted_key and not submitted_key.startswith("•"):
            set_setting("stripe_api_key", submitted_key)

        # A different key means a different event stream. The watermark records
        # a position in the OLD account's stream; keeping it would ask the new
        # account only for events since the old account's last tick, so its 30
        # days of history would never be read and the tab would stay empty while
        # every sync reported success.
        key_changed = bool(
            not clearing
            and submitted_key
            and not submitted_key.startswith("•")
            and submitted_key != previous_key
        )
        if key_changed or clearing:
            reset_sync_watermark()

        # Clearing the key always disables sync, whatever the checkbox says —
        # otherwise removing the key while "enabled" is ticked would leave sync
        # switched on with nothing to authenticate with.
        set_setting(
            "stripe_sync_enabled",
            "true"
            if not clearing and request.form.get("stripe_sync_enabled") == "on"
            else "false",
        )

        raw_interval = request.form.get("stripe_sync_interval_minutes")
        if raw_interval:
            # A non-numeric interval just keeps the stored one.
            with contextlib.suppress(ValueError):
                set_setting(
                    "stripe_sync_interval_minutes", str(max(1, int(raw_interval)))
                )

        db.session.commit()

        # The scheduler job is registered at boot from the stored key. Without
        # re-registering here, the first admin to configure Stripe would save a
        # key and see nothing sync until the container restarted — the tab would
        # just sit empty. replace_existing also picks up an interval change.
        _refresh_stripe_sync_job()

        if not get_setting("stripe_api_key"):
            message = _("Settings saved. Add an API key to start syncing.")
        elif key_changed:
            # Say the backfill is armed. Silently resetting the watermark would
            # look identical to the bug it fixes.
            message = _(
                "New API key saved (%(mode)s mode). The sync position was reset, "
                'so the next "Sync now" reads Stripe\'s full 30-day history.',
                mode=describe_key_mode(get_setting("stripe_api_key")),
            )
        else:
            message = _('Settings saved. Click "Sync now" to pull events.')
        return _render_eventos_tab(message, "success")
    except Exception as exc:
        db.session.rollback()
        structlog.get_logger(__name__).error(
            "Failed to save Stripe settings: %s", exc, exc_info=True
        )
        return _render_eventos_tab(_("Failed to save Stripe settings."), "error")


# ══════════════════════════════════════════════════════════════════════════
# Resend tab — outbound transactional email
# ══════════════════════════════════════════════════════════════════════════
#
# The tab that makes "olvidé mi contraseña" possible. sauron already mints
# password reset tokens (app.services.password_reset) and already serves
# /reset/<code>; what it never had was a way to put that link in front of the
# user without an admin copying it by hand. This is that way.
#
# Everything renders through _render_resend_tab rather than redirecting, for
# the same reason the Eventos tab does: nothing in this app renders
# get_flashed_messages, so a flashed error is an error nobody sees.

RESEND_PAGE_SIZE = 25


def _resend_filters() -> dict[str, object]:
    """Read filter args once, so tab and grid always agree on the query."""
    return {
        "status": request.args.get("status") or None,
        "kind": request.args.get("kind") or None,
        "search": (request.args.get("search") or "").strip() or None,
    }


def _resend_query(filters: dict[str, object]):
    from app.models import ResendEmail

    query = ResendEmail.query

    if filters["status"]:
        query = query.filter(ResendEmail.status == filters["status"])
    if filters["kind"]:
        query = query.filter(ResendEmail.kind == filters["kind"])
    if filters["search"]:
        term = f"%{filters['search']}%"
        query = query.filter(
            db.or_(
                ResendEmail.to_address.ilike(term),
                ResendEmail.subject.ilike(term),
                ResendEmail.error_code.ilike(term),
            )
        )
    return query


@activity_bp.route("/resend")
@login_required
def resend_tab():
    """Display the Resend (outbound email) tab."""
    return _render_resend_tab()


def _render_resend_tab(message: str | None = None, message_kind: str = "success"):
    """Render the Resend tab, optionally with a result banner."""
    from app.services import resend_email as resend_service

    try:
        from app.models import ResendEmail

        # The log itself is fetched by the table's own hx-trigger="load", the
        # way the Eventos grid does it — rendering rows here too would paint
        # them twice on every save.
        filters = _resend_filters()

        stats = {
            "total": ResendEmail.query.count(),
            "sent": ResendEmail.query.filter_by(
                status=resend_service.STATUS_SENT
            ).count(),
            "failed": ResendEmail.query.filter_by(
                status=resend_service.STATUS_FAILED
            ).count(),
            "resets": ResendEmail.query.filter_by(
                kind=resend_service.KIND_PASSWORD_RESET
            ).count(),
        }

        return render_template(
            "activity/resend_tab.html",
            stats=stats,
            usage=resend_service.quota_usage(),
            filters=filters,
            configured=resend_service.is_configured(),
            enabled=resend_service.is_enabled(),
            sandbox_sender=resend_service.uses_sandbox_sender(),
            masked_key=resend_service.mask_api_key(
                resend_service.get_setting(resend_service.SETTING_API_KEY)
            ),
            from_address=resend_service.get_setting(resend_service.SETTING_FROM, ""),
            reply_to=resend_service.get_setting(resend_service.SETTING_REPLY_TO, ""),
            # Pre-fill from the request when unset so the operator sees the value
            # that would be used anyway, and can correct it if the proxy lies.
            public_url=resend_service.get_setting(
                resend_service.SETTING_PUBLIC_URL, request.url_root.rstrip("/")
            ),
            last_error=resend_service.get_setting(resend_service.SETTING_LAST_ERROR),
            message=message,
            message_kind=message_kind,
        )
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to load resend tab: %s", exc, exc_info=True
        )
        return render_template(
            "activity/resend_tab.html",
            error=_("Failed to load email delivery log"),
            stats={},
            usage={},
            filters=_resend_filters(),
            configured=False,
            enabled=False,
            sandbox_sender=False,
            masked_key="",
            from_address="",
            reply_to="",
            public_url="",
        )


@activity_bp.route("/resend/grid")
@login_required
def resend_grid():
    """Paginated outbound email log."""
    from app.models import ResendEmail

    try:
        filters = _resend_filters()
        page = max(1, request.args.get("page", 1, type=int))
        query = _resend_query(filters)

        total_count = query.count()
        emails = (
            query.order_by(ResendEmail.created_at.desc())
            .offset((page - 1) * RESEND_PAGE_SIZE)
            .limit(RESEND_PAGE_SIZE)
            .all()
        )

        total_pages = (total_count + RESEND_PAGE_SIZE - 1) // RESEND_PAGE_SIZE
        return render_template(
            "activity/_resend_table.html",
            emails=emails,
            page=page,
            total_count=total_count,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1,
            filters=filters,
        )
    except Exception as exc:
        structlog.get_logger(__name__).error(
            "Failed to load resend grid: %s", exc, exc_info=True
        )
        return render_template(
            "activity/_resend_table.html",
            emails=[],
            error=_("Failed to load email delivery log"),
            filters=_resend_filters(),
        )


@activity_bp.route("/resend/settings", methods=["POST"])
@login_required
def resend_settings():
    """Save the Resend configuration."""
    from app.services import resend_email as resend_service

    try:
        submitted_key = (request.form.get("resend_api_key") or "").strip()
        clearing = request.form.get("clear_api_key") == "1"

        # An untouched masked field must never wipe the stored key — the form
        # renders the mask, so the browser posts bullets back on every save.
        if clearing:
            resend_service.set_setting(resend_service.SETTING_API_KEY, None)
        elif submitted_key and not submitted_key.startswith("•"):
            resend_service.set_setting(resend_service.SETTING_API_KEY, submitted_key)

        resend_service.set_setting(
            resend_service.SETTING_FROM,
            (request.form.get("resend_from_address") or "").strip() or None,
        )
        resend_service.set_setting(
            resend_service.SETTING_REPLY_TO,
            (request.form.get("resend_reply_to") or "").strip() or None,
        )
        # Stored without the trailing slash so the reset link is never built
        # with a double slash — some proxies 404 on those.
        resend_service.set_setting(
            resend_service.SETTING_PUBLIC_URL,
            (request.form.get("resend_public_base_url") or "").strip().rstrip("/")
            or None,
        )

        # Clearing the key always disables sending, whatever the checkbox says:
        # otherwise removing the key with "enabled" ticked leaves sauron
        # believing it can mail users with nothing to authenticate with.
        resend_service.set_setting(
            resend_service.SETTING_ENABLED,
            "true"
            if not clearing and request.form.get("resend_enabled") == "on"
            else "false",
        )

        db.session.commit()

        if not resend_service.is_configured():
            message = _(
                "Settings saved. Add an API key and a 'From' address to start sending."
            )
            kind = "warning"
        elif not resend_service.is_enabled():
            # Saving a complete config with the switch off is not a success:
            # test sends still work (they only need a key), so everything looks
            # healthy while every real password reset is refused.
            message = _(
                "Settings saved, but outbound email is turned off. Password reset "
                'emails will not be sent until you tick "Enable outbound email".'
            )
            kind = "warning"
        elif resend_service.uses_sandbox_sender():
            # Not an error and not a success: sends will work for the Resend
            # account owner and fail for every real user. Rendering this green
            # is exactly how an operator ships a broken password reset.
            message = _(
                "Settings saved, but you are sending from Resend's shared "
                "onboarding domain. It only delivers to your own Resend account "
                "address — verify your own domain before real users depend on this."
            )
            kind = "warning"
        else:
            message = _('Settings saved. Use "Send test" to verify your domain.')
            kind = "success"
        return _render_resend_tab(message, kind)
    except Exception as exc:
        db.session.rollback()
        structlog.get_logger(__name__).error(
            "Failed to save Resend settings: %s", exc, exc_info=True
        )
        return _render_resend_tab(_("Failed to save Resend settings."), "error")


@activity_bp.route("/resend/test", methods=["POST"])
@login_required
# Rate limited because every click burns one of the free tier's 100 daily sends,
# and that quota is shared with the password resets that actually matter.
@limiter.limit(scaled_limit("5 per minute"))
def resend_test():
    """Send a test email to prove the API key and the sending domain."""
    from app.services import resend_email as resend_service

    recipient = (request.form.get("test_recipient") or "").strip()
    if not recipient:
        return _render_resend_tab(_("Enter an address to send the test to."), "error")

    result = resend_service.send_test_email(recipient)

    if result.ok:
        # A passing test proves the key and the domain, not that sauron will
        # mail anyone: sending can still be switched off. Say so here rather
        # than letting a green banner imply the reset path is live.
        if not resend_service.is_enabled():
            return _render_resend_tab(
                _(
                    "Test email accepted by Resend (check %(inbox)s), but outbound "
                    "email is still turned off, so password resets will not be sent.",
                    inbox=recipient,
                ),
                "warning",
            )
        return _render_resend_tab(
            _("Test email accepted by Resend. Check %(inbox)s.", inbox=recipient),
            "success",
        )

    return _render_resend_tab(
        _(
            "Test failed: %(hint)s",
            hint=resend_service.describe_error(result.error_code, result.error_message),
        ),
        "error",
    )
