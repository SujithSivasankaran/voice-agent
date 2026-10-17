"""Editable campaign scripts: tokens are filled in, and the parts that make a call
work are always added — whatever an admin writes. No database."""

from datetime import datetime

import prompts
from business import MISSED_CLASS, TRIAL

NOW = datetime(2026, 10, 17, 10, 0)   # a Saturday
LOCS = [{"id": "1", "name": "ADAYAR", "address": "", "schedule": [{"days": [0, 1, 2, 3, 4, 5], "start": "06:00", "end": "10:00"}]}]


def test_custom_trial_script_is_used_with_tokens_filled():
    custom = "Hi {name}! This is {assistant} from {business}. Today is {today}. Keep it to one minute. {unknown}"
    p = prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=LOCS, script=custom)
    assert "Hi Rahul! This is Tara from Harry's Fitcamp. Today is Saturday, 2026-10-17. Keep it to one minute." in p
    assert "{unknown}" in p                        # unsupported braces are left alone
    assert "TALK TRACK" not in p                   # the default talk track is replaced, not appended


def test_trial_script_cannot_remove_booking():
    p = prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=LOCS, script="Just say hello.")
    for always in ("BOOKING A FREE TRIAL", "book_trial", "check_availability", "BRANCHES & TIMINGS",
                   "ADAYAR — open", "CALL OUTCOME", "end_call", "mark_do_not_call", "FACTS ABOUT"):
        assert always in p, always


def test_missed_class_script_cannot_remove_reason_recording():
    p = prompts.build_prompt(MISSED_CLASS, "Priya", missed_date="2026-10-14", now=NOW, locations=LOCS,
                             script="Check on {name}, who missed class {missed_when}.")
    assert "Check on Priya, who missed class on 2026-10-14." in p
    assert "record_missed_class_reason exactly once" in p and "not_given" in p
    assert "Today is Saturday, 2026-10-17." in p
    assert "book_trial" not in p                   # members aren't sold a trial


def test_no_script_means_default():
    assert prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=LOCS) == \
        prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=LOCS, script=None)
    assert "TALK TRACK" in prompts.build_prompt(TRIAL, "Rahul", now=NOW, locations=LOCS)


def test_every_documented_token_is_supported():
    filled = prompts.render_script(" ".join(prompts.SCRIPT_TOKENS), "Rahul", "2026-10-14", NOW)
    assert "{" not in filled
