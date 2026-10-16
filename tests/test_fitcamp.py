"""Harry's Fitcamp rules: trial-slot validation, call scripts, call-list parsing and
the per-campaign tool sets. No network or database."""

from datetime import datetime

import pytest

import db
import prompts
from business import BRANCHES, CLOSED_WEEKDAYS, MISSED_CLASS, SESSION_TIMES, TRIAL, campaign_or_default

# Thursday 2026-10-15, 10:00 local — a fixed "now" so the tests never depend on the clock.
NOW = datetime(2026, 10, 15, 10, 0)


# ── Trial slots ───────────────────────────────────────────────────────────────

def test_valid_slot_is_normalised():
    assert db.validate_trial_slot("2026-10-16", "6:00", "adyar", now=NOW) == ("2026-10-16", "06:00", "ADAYAR")
    assert db.validate_trial_slot("2026-10-16", "16:30", "ECR", now=NOW) == ("2026-10-16", "16:30", "ECR")


@pytest.mark.parametrize("spoken", ["ADAYAR", "Adyar", "adaiyar", "ecr", "East Coast Road", "E.C.R"])
def test_branch_aliases(spoken):
    assert db.normalize_branch(spoken) in BRANCHES


@pytest.mark.parametrize("spoken", ["", "Velachery", "both"])
def test_unknown_branch_rejected(spoken):
    with pytest.raises(ValueError, match="ADAYAR or ECR"):
        db.normalize_branch(spoken)


def test_closed_on_sunday():
    assert 6 in CLOSED_WEEKDAYS
    with pytest.raises(ValueError, match="closed"):
        db.validate_trial_slot("2026-10-18", "07:00", "ECR", now=NOW)  # a Sunday


def test_saturday_is_open():
    assert db.validate_trial_slot("2026-10-17", "07:00", "ECR", now=NOW)[0] == "2026-10-17"


@pytest.mark.parametrize("time", ["10:00", "12:00", "16:00", "20:30", "05:00"])
def test_only_session_start_times(time):
    assert time not in SESSION_TIMES
    with pytest.raises(ValueError, match="trials start at"):
        db.validate_trial_slot("2026-10-16", time, "ECR", now=NOW)


def test_past_slot_rejected():
    with pytest.raises(ValueError, match="future"):
        db.validate_trial_slot("2026-10-15", "09:00", "ECR", now=NOW)  # an hour ago


@pytest.mark.parametrize("date,time", [("16-10-2026", "07:00"), ("2026-10-16", "7pm")])
def test_bad_formats_rejected(date, time):
    with pytest.raises(ValueError):
        db.validate_trial_slot(date, time, "ECR", now=NOW)


# ── Call scripts ──────────────────────────────────────────────────────────────

def test_campaign_default_is_trial():
    assert campaign_or_default(None) == TRIAL
    assert campaign_or_default("nonsense") == TRIAL
    assert campaign_or_default("Missed_Class") == MISSED_CLASS


def test_trial_prompt_books_and_knows_the_date_and_phone():
    p = prompts.build_prompt(TRIAL, "Rahul", caller_phone="+919876543210", now=NOW)
    assert "Rahul" in p
    assert "book_trial" in p and "check_availability" in p
    assert "Thursday, 2026-10-15" in p
    assert "+919876543210" in p and "never ask for it" in p
    assert "record_missed_class_reason" not in p


def test_missed_class_prompt_asks_why_and_does_not_sell():
    p = prompts.build_prompt(MISSED_CLASS, "Priya", missed_date="2026-10-14", now=NOW)
    assert "Priya" in p and "on 2026-10-14" in p
    assert "record_missed_class_reason" in p
    assert "Do NOT pitch a trial" in p
    assert "book_trial" not in p
    for category in ("sick_or_injured", "lost_motivation", "not_given"):
        assert category in p


def test_missed_class_prompt_without_date():
    assert "missed their class recently" in prompts.build_prompt(MISSED_CLASS, "Priya", now=NOW)


def test_placeholder_name_is_not_spoken():
    # "there" is the call-list default when no name is given — never address someone as "there".
    p = prompts.build_prompt(TRIAL, "there", now=NOW)
    assert "speaking with the person" in p


def test_inbound_prompt_does_not_greet_again():
    p = prompts.build_inbound_prompt(caller_phone="+919876543210", now=NOW)
    assert "ALREADY been spoken" in p and "book_trial" in p


# ── Call lists ────────────────────────────────────────────────────────────────

def test_parse_call_list_reads_optional_columns():
    server = pytest.importorskip("server")
    csv_text = "Name,Phone,Missed_Date\nPriya,+91 98765 43210,2026-10-14\nNo Phone,,\nRavi,9876500000,\n"
    rows = server._parse_call_list(csv_text)
    assert rows == [
        {"phone": "+91 98765 43210", "name": "Priya", "missed_date": "2026-10-14"},
        {"phone": "9876500000", "name": "Ravi", "missed_date": ""},
    ]


# ── Tool sets ─────────────────────────────────────────────────────────────────

def _tool_names(campaign):
    tools = pytest.importorskip("tools")
    return {t.__name__ for t in tools.CallTools(None, "+919876543210", "Priya", campaign=campaign).tool_list()}


def test_trial_tools():
    names = _tool_names(TRIAL)
    assert {"check_availability", "book_trial", "end_call", "mark_do_not_call"} <= names
    assert "record_missed_class_reason" not in names


def test_missed_class_tools():
    names = _tool_names(MISSED_CLASS)
    assert {"record_missed_class_reason", "end_call", "mark_do_not_call"} <= names
    assert "book_trial" not in names
