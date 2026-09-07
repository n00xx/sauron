"""Babel message extractor for the wizard step markdown files.

The step files are Jinja templates with one non-Jinja addition: widget
placeholders written as ``{{ widget:quick_connect }}``. Jinja cannot parse
``widget:quick_connect`` as an expression, so the stock ``jinja2`` extractor
raises on the first placeholder and Babel then skips the *entire file* without
saying a word.

The result was invisible but expensive: every step that contains a widget —
which is most of them — contributed no msgids at all, so none of the text a
buyer reads inside those steps could ever be translated. It rendered in English
no matter the locale.

Blanking the placeholders before handing the file to the stock extractor fixes
that. The runtime is unaffected: it strips the same placeholders in its own pass
before Jinja ever sees them (``wizard_widgets.process_widget_placeholders``).

Because Babel imports this module by dotted path, extraction needs the repo root
on the import path::

    PYTHONPATH=. uv run pybabel extract -F babel.cfg -k _l -k _ -o messages.pot .

Compiling (``pybabel compile``, what CI and the Dockerfile run) needs none of
this — it only reads the .po files.
"""

from __future__ import annotations

import io
import re
from typing import Any

from jinja2.ext import babel_extract

# Mirrors the runtime pattern in wizard_widgets.process_widget_placeholders.
_WIDGET_PLACEHOLDER = re.compile(rb"\{\{\s*widget:[^}]*\}\}")

# Widget parameters carry their own gettext calls, e.g.
#   {{ widget:button url="external_url" text=_("Go to Jellyfin") }}
# Those survive to runtime because the widget's HTML output is passed through
# render_template_string afterwards, so Jinja evaluates the _() there. Blanking
# the placeholder would drop the label from the catalogue, so pull it out first.
_PARAM_GETTEXT = re.compile(
    rb"""_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*\)"""
)


def _blank(match: re.Match[bytes]) -> bytes:
    """Replace a placeholder with same-length filler.

    Newlines are kept so the line numbers Babel reports in ``#:`` comments still
    point at the right line of the original file.
    """
    return bytes(b if b == 0x0A else 0x20 for b in match.group(0))


def extract_wizard_step(
    fileobj: io.BufferedReader,
    keywords: Any,
    comment_tags: Any,
    options: Any,
):
    """Yield the same tuples as the jinja2 extractor, widgets neutralised."""
    source = fileobj.read()

    messages = []
    for placeholder in _WIDGET_PLACEHOLDER.finditer(source):
        lineno = source.count(b"\n", 0, placeholder.start()) + 1
        for param in _PARAM_GETTEXT.finditer(placeholder.group(0)):
            raw = param.group(1) if param.group(1) is not None else param.group(2)
            text = raw.decode("utf-8").replace("\\'", "'").replace('\\"', '"')
            messages.append((lineno, "gettext", text, []))

    cleaned = _WIDGET_PLACEHOLDER.sub(_blank, source)
    messages.extend(babel_extract(io.BytesIO(cleaned), keywords, comment_tags, options))

    return sorted(messages, key=lambda entry: entry[0])
