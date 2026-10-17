"""Harry's Fitcamp rules: trial-slot validation, call scripts, call-list parsing and
the per-campaign tool sets. No network or database."""

from datetime import datetime

import pytest

import db
import prompts
from business import MISSED_CLASS, TRIAL, campaign_or_default, describe_days, describe_hours

# Thursday 2026-10-15, 10:00 local — a fixed "now" so the tests never depend on the clock.
NOW = datetime(2026, 10, 15, 10, 0)

TIMES = ["06:00", "07:00", "08:00", "09:00", "16:30", "17:30", "18:30", "19:30"]
LOCS = [
    {"id": "1", "name": "ADAYAR", "address": "", "days": [0, 1, 2, 3, 4, 5], "slot_times": TIMES},
    {"id": "2", "name": "ECR", "address": "", "days": [0, 1, 2, 3, 4, 5], "slot_times": TIMES},
    # A branch with its own schedule: weekday evenings only.
    {"id": "3", "name": "ANNA NAGAR", "address": "2nd Avenue", "days": [0, 1, 2, 3, 4], "slot_times": ["18:00", "19:00"]},
]


def slot(date, time, where):
    return db.validate_trial_slot(date, time, where, LOCS, now=NOW)


# ── Trial slots ───────────────────────────────────────────────────────────────

def test_valid_slot_is_normalised():
    assert slot("2026-10-16", "6:00", "adyar") == ("2026-10-16", "06:00", "ADAYAR")
    assert slot("2026-10-16", "16:30", "ECR") == ("2026-10-16", "16:30", "ECR")


@pytest.mark.parametrize("spoken,name", [("ADAYAR", "ADAYAR"), ("Adyar", "ADAYAR"), ("adaiyar", "ADAYAR"),
                                         ("ecr", "ECR"), ("East Coast Road", "ECR"), ("E.C.R", "ECR"),
                                         ("anna nagar", "ANNA NAGAR")])
def test_branch_aliases(spoken, name):
    assert db.find_location(spoken, LOCS)["name"] == name


@pytest.mark.parametrize("spoken", ["", "Velachery", "both"])
def test_unknown_branch_rejected(spoken):
    with pytest.raises(ValueError, match="ADAYAR or ECR or ANNA NAGAR"):
        db.find_location(spoken, LOCS)


def test_no_branches_set_up():
    with pytest.raises(ValueError, match="no branches"):
        db.validate_trial_slot("2026-10-16", "07:00", "ECR", [], now=NOW)


def test_closed_on_sunday():
    with pytest.raises(ValueError, match="ECR is closed"):
        slot("2026-10-18", "07:00", "ECR")  # a Sunday


def test_saturday_is_open_at_adayar_but_not_anna_nagar():
    assert slot("2026-10-17", "07:00", "ECR")[0] == "2026-10-17"
    with pytest.raises(ValueError, match="ANNA NAGAR is closed"):
        slot("2026-10-17", "18:00", "Anna Nagar")


@pytest.mark.parametrize("time", ["10:00", "12:00", "16:00", "20:30", "05:00"])
def test_only_that_branchs_start_times(time):
    with pytest.raises(ValueError, match="trials at ECR start at"):
        slot("2026-10-16", time, "ECR")


def test_each_branch_has_its_own_times():
    assert slot("2026-10-16", "18:00", "ANNA NAGAR")[1] == "18:00"
    with pytest.raises(ValueError, match="ANNA NAGAR start at 18:00, 19:00"):
        slot("2026-10-16", "06:00", "ANNA NAGAR")
    with pytest.raises(ValueError, match="trials at ECR"):
        slot("2026-10-16", "18:00", "ECR")


def test_past_slot_rejected():
    with pytest.raises(ValueError, match="future"):
        slot("2026-10-15", "09:00", "ECR")  # an hour ago


@pytest.mark.parametrize("date,time", [("16-10-2026", "07:00"), ("2026-10-16", "7pm"), ("2026-10-16", "25:00")])
def test_bad_formats_rejected(date, time):
    with pytest.raises(ValueError):
        slot(date, time, "ECR")


# ── Locations from the dashboard ──────────────────────────────────────────────

def test_clean_location_normalises_input():
    row = db.clean_location({"name": "  anna   nagar ", "days": ["4", 0, 0], "slot_times": "19:00, 6:00,19:00"})
    assert row["name"] == "ANNA NAGAR"
    assert row["days"] == "[0, 4]" and row["slot_times"] == '["06:00", "19:00"]'


@pytest.mark.parametrize("body,msg", [
    ({"days": [0], "slot_times": "06:00"}, "name"),
    ({"name": "X", "days": [], "slot_times": "06:00"}, "day"),
    ({"name": "X", "days": [0], "slot_times": ""}, "start time"),
    ({"name": "X", "days": [0], "slot_times": "6am"}, "HH:MM"),
])
def test_clean_location_rejects_bad_input(body, msg):
    with pytest.raises(ValueError, match=msg):
        db.clean_location(body)


def test_spoken_schedule():
    assert describe_days([0, 1, 2, 3, 4, 5]) == "Monday to Saturday"
    assert describe_hours(TIMES) == "6 AM–10 AM and 4:30 PM–8:30 PM"


# ── Call scripts ──────────────────────────────────────────────────────────────

def test_campaign_default_is_trial():
    assert campaign_or_default(None) == TRIAL
    assert campaign_or_default("nonsense") == TRIAL
    assert campaign_or_default("Missed_Class") == MISSED_CLASS


def test_trial_prompt_books_and_knows_the_date_and_phone():
    p = prompts.build_prompt(TRIAL, "Rahul", caller_phone="+919876543210", now=NOW, locations=LOCS)
    assert "Rahul" in p
    assert "book_trial" in p and "check_availability" in p
    assert "Thursday, 2026-10-15" in p
    assert "+919876543210" in p and "never ask for it" in p
    assert "record_missed_class_reason" not in p
    # Asks the branch first, and lists each branch's own timings.
    assert "Ask which branch suits them first (ADAYAR or ECR or ANNA NAGAR)" in p
    assert "ANNA NAGAR — open Monday to Friday, 6 PM–8 PM" in p and "Address: 2nd Avenue" in p


def test_no_branches_means_no_booking_offer():
    assert "No branches are set up" in prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=[])


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


def test_trial_prompt_follows_the_talk_track_and_adapts():
    p = prompts.build_prompt(TRIAL, "Rahul", now=NOW)
    for point in ("TALK TRACK", "first strength-training gym", "up to eight", "₹35,000", "₹80,000",
                  "Saturday", "answer it first", "IF THE CALLER ASKS"):
        assert point in p, point
    assert "up to seven" not in p


def test_call_list_from_excel_and_table():
    server = pytest.importorskip("server")
    openpyxl = pytest.importorskip("openpyxl")
    import io
    from datetime import datetime as dt
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Phone", "Missed Date"])
    ws.append(["Priya", 919876543210, dt(2026, 10, 14)])   # Excel stores numbers and dates natively
    ws.append(["No phone", None, None])
    ws.append(["Ravi", "+91 98765 00000", None])
    buf = io.BytesIO()
    wb.save(buf)
    assert server._parse_xlsx(buf.getvalue()) == [
        {"phone": "919876543210", "name": "Priya", "missed_date": "2026-10-14"},
        {"phone": "+91 98765 00000", "name": "Ravi", "missed_date": ""},
    ]
    # The on-screen table sends the same columns.
    assert server._rows_to_contacts([{"name": "A", "phone": "+911", "missed_date": ""}, {"name": "B", "phone": ""}]) == [
        {"phone": "+911", "name": "A", "missed_date": ""}]
