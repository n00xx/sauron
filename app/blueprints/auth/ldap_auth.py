from flask import current_app
from flask_babel import gettext as _

from app.extensions import db
from app.models import AdminAccount, LDAPConfiguration
from app.services.ldap.client import LDAPClient


def handle_ldap_login(
    username: str, password: str
) -> tuple[bool, str, AdminAccount | None]:
    """Authenticate *username* against LDAP and return the matching admin.

    Deliberately does **not** establish a session. The caller owns that
    decision, because only the route knows whether a second factor is still
    outstanding — logging in here silently bypassed passkey 2FA.

    Returns ``(success, message, account)``.
    """
    # Get LDAP configuration
    ldap_config = LDAPConfiguration.query.first()

    if not ldap_config or not ldap_config.enabled:
        return False, _("LDAP authentication is not enabled"), None

    if not ldap_config.allow_admin_bind:
        return False, _("LDAP admin authentication is not allowed"), None

    # Admin group membership is mandatory. Without it any directory user who
    # can bind would be provisioned as a Wizarr administrator.
    if not ldap_config.admin_group_dn:
        current_app.logger.error(
            "LDAP admin bind is enabled but no admin_group_dn is configured; "
            "refusing to authenticate '%s'",
            username,
        )
        return False, _("LDAP admin group is not configured"), None

    # Authenticate via LDAP
    client = LDAPClient(ldap_config)

    try:
        success, user_attrs = client.authenticate_user(username, password)

        if not success:
            current_app.logger.warning(
                "LDAP authentication failed for user: %s", username
            )
            return False, _("Invalid LDAP credentials"), None

        # Get user DN and attributes
        user_dn = user_attrs.get("dn")
        if not user_dn:
            current_app.logger.warning("LDAP user DN not found for user: %s", username)
            return False, _("User DN not found in LDAP response"), None
        # Check admin group membership. admin_group_dn is guaranteed present
        # by the guard at the top of this function.
        groups = client.get_user_groups(user_dn)
        group_dns = [g.get("dn").lower() for g in groups if g.get("dn")]

        # Normalize admin group DN for comparison (case-insensitive)
        admin_group_dn_lower = ldap_config.admin_group_dn.lower()

        if admin_group_dn_lower not in group_dns:
            current_app.logger.warning(
                "User %s not in admin group. Required: %s, User groups: %s",
                username,
                ldap_config.admin_group_dn,
                [g.get("dn") for g in groups],
            )
            return False, _("User is not authorized as an administrator"), None

        # Find existing admin account by username
        # LDAP authentication allows logging into existing local accounts
        admin = AdminAccount.query.filter_by(username=username).first()

        if not admin:
            # Create new admin account if none exists
            admin = AdminAccount(username=username)
            admin.auth_source = "local"  # Default to local, can use both methods
            admin.password_hash = None  # No local password set initially
            admin.external_id = user_dn
            admin.email = user_attrs.get(ldap_config.email_attribute)

            db.session.add(admin)
            db.session.commit()
            current_app.logger.info("Created new admin account '%s' via LDAP", username)
        # Update existing account with LDAP attributes
        # This syncs the external_id and email on every LDAP login
        else:
            needs_update = False
            if admin.external_id != user_dn:
                current_app.logger.info(
                    "Updating LDAP DN for account '%s': %s -> %s",
                    username,
                    admin.external_id,
                    user_dn,
                )
                admin.external_id = user_dn
                needs_update = True

            ldap_email = user_attrs.get(ldap_config.email_attribute)
            # AdminAccount has no `email` column, so this value is never
            # persisted and reading it back off a DB-loaded row raises
            # AttributeError -- which the except clause below turned into a
            # generic "LDAP authentication failed", locking out every admin
            # who already existed. getattr keeps the sync non-fatal until the
            # schema actually gains the column.
            if ldap_email and getattr(admin, "email", None) != ldap_email:
                admin.email = ldap_email
                needs_update = True

            if needs_update:
                db.session.commit()

        current_app.logger.info(
            "LDAP admin authenticated: %s (DN: %s)", username, user_dn
        )
        # The caller establishes the session; see the docstring.
        return True, _("Login successful"), admin

    except Exception as e:
        current_app.logger.error("LDAP authentication error: %r", e, exc_info=True)
        # Rollback any pending transaction
        db.session.rollback()
        return False, _("LDAP authentication failed"), None
