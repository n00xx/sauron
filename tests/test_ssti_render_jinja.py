"""Regression tests for SSTI in the ``render_jinja`` filter (F-03).

``render_jinja`` used ``render_template_string`` on wizard step titles read
straight from the database. Jinja autoescaping protects the *output* from XSS
but does nothing to stop Jinja *evaluating* the template, so a stored title
could read ``{{ config }}`` to leak SECRET_KEY, or walk ``__subclasses__`` to
reach code execution. Wizard bundles are importable from files, which makes
this a supply-chain path too.

The filter only ever needed to resolve ``{{ _('...') }}`` translation calls.
"""

from app.jinja_filters import render_jinja


def test_config_expression_is_not_evaluated(app):
    """A title of ``{{ config }}`` must not expose the app configuration."""
    with app.test_request_context():
        out = str(render_jinja("{{ config }}"))

    assert "SECRET_KEY" not in out
    assert "SQLALCHEMY_DATABASE_URI" not in out


def test_secret_key_cannot_be_exfiltrated(app):
    """The concrete attack: read SECRET_KEY out of a stored title."""
    secret = app.config["SECRET_KEY"]

    with app.test_request_context():
        out = str(render_jinja("{{ config['SECRET_KEY'] }}"))

    assert secret not in out, "SSTI: SECRET_KEY leaked through a wizard step title"


def test_class_traversal_is_not_evaluated(app):
    """The usual RCE stepping stone must not resolve."""
    with app.test_request_context():
        out = str(render_jinja("{{ ''.__class__.__mro__ }}"))

    assert "class" not in out.lower() or "{{" in out
    assert "object" not in out.lower()


def test_arithmetic_expression_is_not_evaluated(app):
    """Nothing generic should evaluate -- a blunt canary for template execution."""
    with app.test_request_context():
        out = str(render_jinja("{{ 7 * 6 }}"))

    assert "42" not in out, "SSTI: arbitrary Jinja expressions still evaluate"


def test_translation_calls_still_resolve(app):
    """The legitimate use case must keep working."""
    with app.test_request_context():
        out = str(render_jinja("{{ _('Welcome') }}"))

    assert "Welcome" in out
    assert "{{" not in out and "_(" not in out


def test_plain_text_is_escaped(app):
    """Ordinary titles render, and HTML in them stays inert."""
    with app.test_request_context():
        assert "Getting Started" in str(render_jinja("Getting Started"))

        out = str(render_jinja("<script>alert(1)</script>"))
        assert "<script>" not in out


def test_empty_input_is_safe(app):
    with app.test_request_context():
        assert str(render_jinja("")) == ""
