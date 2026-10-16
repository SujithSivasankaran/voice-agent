"""Pure helpers in db.py that need no database connection."""

import db


def test_normalize_phone_strips_formatting():
    assert db._normalize_phone("+1 (555) 123-4567") == "+15551234567"
    assert db._normalize_phone("555.123.4567") == "5551234567"


def test_normalize_phone_preserves_plus():
    assert db._normalize_phone("+919876543210") == "+919876543210"
    assert db._normalize_phone("919876543210") == "919876543210"


def test_normalize_phone_empty():
    assert db._normalize_phone("") == ""
    assert db._normalize_phone(None) == ""


def test_app_timezone_default():
    # Falls back to a valid zone even if unset.
    assert db.APP_TIMEZONE
    assert db.now_local().tzinfo is not None
