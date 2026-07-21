"""Regression tests for update-check version comparison.

Sauron builds stamp APP_VERSION with a non-PEP 440 string
(e.g. "2026.7.1-sauron.<sha>"). check_update_available() is the only
request-path caller (the /admin dashboard), so an unguarded vparse() on
that string 500s the admin dashboard. These tests lock in graceful
degradation.
"""

import app.services.update_check as uc


def _patch_manifest(monkeypatch, manifest):
    monkeypatch.setattr(uc, "_manifest", lambda: manifest)


def test_non_pep440_current_version_does_not_raise(monkeypatch):
    # Reproduces the /admin 500: fork version string + a populated manifest.
    _patch_manifest(monkeypatch, {"latest_version": "2026.8.0"})
    assert uc.check_update_available("2026.7.1-sauron.801c0a6") is False


def test_dev_version_returns_false(monkeypatch):
    _patch_manifest(monkeypatch, {"latest_version": "2026.8.0"})
    assert uc.check_update_available("dev") is False


def test_empty_manifest_returns_false(monkeypatch):
    _patch_manifest(monkeypatch, {})
    assert uc.check_update_available("2026.7.1-sauron.801c0a6") is False


def test_normal_comparison_still_works(monkeypatch):
    _patch_manifest(monkeypatch, {"latest_version": "2026.8.0"})
    assert uc.check_update_available("2026.7.1") is True
    assert uc.check_update_available("2026.9.9") is False


def test_unparseable_latest_version_does_not_raise(monkeypatch):
    _patch_manifest(monkeypatch, {"latest_version": "garbage-not-a-version"})
    assert uc.check_update_available("2026.7.1") is False
