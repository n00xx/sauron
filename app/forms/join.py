from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Regexp

from app.forms.validators import (
    JOIN_USERNAME_ALLOWED_CHARS_MESSAGE,
    JOIN_USERNAME_LENGTH_MESSAGE,
    JOIN_USERNAME_MAX_LENGTH,
    JOIN_USERNAME_MIN_LENGTH,
    JOIN_USERNAME_PATTERN,
    strip_filter,
    validate_email_domain_exists,
)


class JoinForm(FlaskForm):
    username = StringField(
        "Username",
        filters=[strip_filter],
        validators=[
            DataRequired(message=_l("This field is required.")),
            Length(
                min=JOIN_USERNAME_MIN_LENGTH,
                max=JOIN_USERNAME_MAX_LENGTH,
                message=JOIN_USERNAME_LENGTH_MESSAGE,
            ),
            Regexp(JOIN_USERNAME_PATTERN, message=JOIN_USERNAME_ALLOWED_CHARS_MESSAGE),
        ],
        render_kw={
            "minlength": JOIN_USERNAME_MIN_LENGTH,
            "maxlength": JOIN_USERNAME_MAX_LENGTH,
            "autocapitalize": "none",
            "autocomplete": "username",
            "spellcheck": "false",
        },
    )
    email = StringField(
        "Email",
        filters=[strip_filter],
        validators=[
            DataRequired(message=_l("This field is required.")),
            Email(message=_l("Please enter a valid email address.")),
            validate_email_domain_exists,
        ],
    )
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(message=_l("This field is required.")),
            Length(min=8, message=_l("Password must be at least 8 characters.")),
            Regexp(
                r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).+$",
                message=_l(
                    "Password must contain at least one uppercase letter, "
                    "one lowercase letter, and one number."
                ),
            ),
        ],
    )
    confirm_password = PasswordField(
        "Confirm password",
        validators=[
            DataRequired(message=_l("This field is required.")),
            EqualTo("password", message=_l("Passwords must match.")),
        ],
    )
    code = StringField(
        "Invite Code",
        filters=[strip_filter],
        validators=[
            DataRequired(message=_l("This field is required.")),
            Length(min=6, max=10),
        ],
        render_kw={"minlength": 6, "maxlength": 10},
    )
