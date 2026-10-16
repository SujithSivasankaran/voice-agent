"""Calling-window + DNC-parsing logic. No network; times are passed explicitly."""

import os
from datetime import datetime

import compliance


def _at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute)


def test_parse_hhmm_valid_and_fallback():
    from datetime import time as t
    assert compliance._parse_hhmm("09:30", t(0, 0)) == t(9, 30)
    assert compliance._parse_hhmm("bad", t(1, 2)) == t(1, 2)
    # Out-of-range clamps rather than raising.
    assert compliance._parse_hhmm("99:99", t(0, 0)) == t(23, 59)


def test_window_allows_inside(monkeypatch):
    monkeypatch.setenv("CALLING_WINDOW_START", "09:00")
    monkeypatch.setenv("CALLING_WINDOW_END", "20:00")
    monkeypatch.delenv("CALLING_BLOCKED_DAYS", raising=False)
    ok, reason = compliance.calling_window_status(_at(2026, 7, 14, 12))  # Tuesday noon
    assert ok and reason == ""


def test_window_blocks_before_open(monkeypatch):
    monkeypatch.setenv("CALLING_WINDOW_START", "09:00")
    monkeypatch.setenv("CALLING_WINDOW_END", "20:00")
    monkeypatch.delenv("CALLING_BLOCKED_DAYS", raising=False)
    ok, reason = compliance.calling_window_status(_at(2026, 7, 14, 7))
    assert not ok and "outside calling window" in reason


def test_window_wraps_past_midnight(monkeypatch):
    # 20:00 → 06:00 window: 23:00 allowed, 07:00 blocked.
    monkeypatch.setenv("CALLING_WINDOW_START", "20:00")
    monkeypatch.setenv("CALLING_WINDOW_END", "06:00")
    monkeypatch.delenv("CALLING_BLOCKED_DAYS", raising=False)
    assert compliance.calling_window_status(_at(2026, 7, 14, 23))[0] is True
    assert compliance.calling_window_status(_at(2026, 7, 14, 7))[0] is False


def test_blocked_weekday(monkeypatch):
    monkeypatch.setenv("CALLING_WINDOW_START", "00:00")
    monkeypatch.setenv("CALLING_WINDOW_END", "23:59")
    monkeypatch.setenv("CALLING_BLOCKED_DAYS", "sun")
    # 2026-07-19 is a Sunday.
    ok, reason = compliance.calling_window_status(_at(2026, 7, 19, 12))
    assert not ok and "Sunday" in reason
    # Saturday still allowed.
    assert compliance.calling_window_status(_at(2026, 7, 18, 12))[0] is True


def test_blocked_weekday_numeric(monkeypatch):
    monkeypatch.setenv("CALLING_BLOCKED_DAYS", "6")  # Sunday = 6
    assert 6 in compliance._blocked_weekdays()
