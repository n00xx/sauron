from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Optional


class GeneralSettingsForm(FlaskForm):
    server_name = StringField("Display Name", validators=[DataRequired()])
    wizard_acl_enabled = BooleanField(
        "Protect Wizard Access", default=True, validators=[Optional()]
    )
    expiry_action = SelectField(
        "Expiry Action",
        choices=[
            ("delete", "Delete User"),
            ("disable", "Disable User (if supported)"),
        ],
        default="delete",
        validators=[DataRequired()],
    )
    # ── Cloudflare Turnstile (admin login protection) ──────────────────
    turnstile_enabled = BooleanField(
        "Protect Login with Cloudflare Turnstile",
        default=False,
        validators=[Optional()],
    )
    turnstile_site_key = StringField("Turnstile Site Key", validators=[Optional()])
    turnstile_secret_key = PasswordField(
        "Turnstile Secret Key", validators=[Optional()]
    )
