"""Username/password rules for dashboard users. No database."""

import pytest

import auth


@pytest.mark.parametrize("raw,clean", [("Sujith", "sujith"), ("  hari ", "hari"), ("front.desk-2", "front.desk-2")])
def test_usernames_are_normalised(raw, clean):
    assert auth.clean_username(raw) == clean


@pytest.mark.parametrize("raw", ["", "ab", "has space", "x" * 33, "name@gym"])
def test_bad_usernames_rejected(raw):
    with pytest.raises(ValueError, match="username"):
        auth.clean_username(raw)


def test_password_length():
    assert auth.check_new_password("Sujith@123") == "Sujith@123"
    with pytest.raises(ValueError, match="at least 10"):
        auth.check_new_password("short")


def test_password_hash_roundtrip():
    hashed = auth.hash_password("Hari@fitkits")
    assert auth.verify_password("Hari@fitkits", hashed)
    assert not auth.verify_password("hari@fitkits", hashed)
