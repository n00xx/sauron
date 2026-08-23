# app/error_handlers.py
import logging

from flask import render_template, request
from flask_babel import gettext as _
from flask_wtf.csrf import CSRFError

# The public signup POST. A CSRF failure here strands someone who has already
# paid for their invitation, so it gets the form back instead of a bare 400.
JOIN_ENDPOINT = "public.process_invitation"


def _reissue_join_form():
    """Re-render the signup form with a fresh token and the code intact.

    Nothing here mutates state: a request that failed CSRF validation is only
    ever re-rendered, never acted on. It does not provision, does not claim the
    invitation, and does not touch the media server.

    Username and email are carried over because they were never the problem.
    Passwords are not: echoing an unvalidated password back into the DOM is a
    worse trade than asking the user to retype it.
    """
    from app.extensions import db
    from app.forms.join import JoinForm
    from app.models import Invitation
    from app.services.invitation_flow.workflows import (
        _create_join_form_template_data,
    )

    code = (request.form.get("code") or "").strip()
    # Exact, case-insensitive match -- never LIKE. This runs before CSRF has
    # been established, so the submitted code is fully attacker-controlled:
    # `ilike` would treat "%" as a wildcard and echo a real, paid invitation
    # code back to an unauthenticated caller.
    invitation = (
        Invitation.query.filter(db.func.lower(Invitation.code) == code.lower()).first()
        if code
        else None
    )
    if not invitation:
        # No code to recover, so there is no form worth reissuing.
        return None

    form = JoinForm(formdata=None)
    form.code.data = invitation.code
    form.username.data = request.form.get("username") or ""
    form.email.data = request.form.get("email") or ""

    servers = list(invitation.servers) if invitation.servers else []
    if not servers and invitation.server:
        servers = [invitation.server]

    context = _create_join_form_template_data(
        invitation,
        servers,
        form=form,
        error=_(
            "Your session expired before the form was submitted. "
            "Please check your details and send it again."
        ),
    )
    template_name = context.pop("template_name")
    return render_template(template_name, **context)


def register_error_handlers(app):
    @app.errorhandler(CSRFError)
    def error_csrf(e):
        logging.info("CSRF failure on %s: %s", request.path, e.description)

        if request.endpoint == JOIN_ENDPOINT:
            reissued = _reissue_join_form()
            if reissued is not None:
                return reissued, 400

        return render_template("error/csrf.html", reason=e.description), 400

    @app.errorhandler(500)
    def error_500(e):
        logging.error("500: %s", e, exc_info=True)
        return render_template("error/500.html"), 500

    @app.errorhandler(404)
    def error_404(e):
        logging.info("404: %s", e)
        return render_template("error/404.html"), 404

    @app.errorhandler(401)
    def error_401(e):
        logging.info("401: %s", e)
        return render_template("error/401.html"), 401
