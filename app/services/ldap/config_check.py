"""Startup validation for LDAP configuration.

The form now refuses to save ``allow_admin_bind`` without an admin group, but
databases configured before that guard existed can already hold the state.
Authentication denies it at login time, which is safe but opaque -- an
operator upgrading would see admins locked out with no explanation.

This check runs at boot and says exactly what is wrong and how to fix it.
"""

from flask import Flask


def warn_on_unsafe_ldap_config(app: Flask) -> bool:
    """Log an error if LDAP admin login is enabled without an admin group.

    Returns True when an unsafe configuration was found, so callers (and
    tests) can act on it. Never raises: a misconfigured LDAP row must not
    prevent the application from starting.
    """
    try:
        from app.models import LDAPConfiguration

        unsafe = (
            LDAPConfiguration.query.filter_by(enabled=True, allow_admin_bind=True)
            .filter(
                (LDAPConfiguration.admin_group_dn.is_(None))
                | (LDAPConfiguration.admin_group_dn == "")
            )
            .first()
        )
    except Exception:
        # Table may not exist yet if migrations have not run.
        app.logger.debug("LDAP config check skipped (table may not exist yet)")
        return False

    if unsafe is None:
        return False

    app.logger.error(
        "LDAP admin login is enabled but admin_group_dn is empty. Admin logins "
        "via LDAP will be REFUSED until a group is set, because without one "
        "every directory user would be granted administrator access. "
        "Set the admin group in Settings > LDAP."
    )
    return True
