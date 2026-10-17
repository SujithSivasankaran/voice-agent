# business.py — everything specific to Harry's Fitcamp, in one place.
#
# The app serves exactly one business with exactly two outbound campaigns. The facts
# and scripts are fixed here (and in prompts.py) — edit and redeploy to change them.
# Branches and their timings are the one thing edited from the dashboard: they live
# in the `locations` table, and the helpers below describe them for the scripts.

BUSINESS_NAME = "Harry's Fitcamp"
ASSISTANT_NAME = "Tara"
DEFAULT_VOICE = "Leda"  # Gemini Live voice; overrides GEMINI_TTS_VOICE

# ── Trial booking ─────────────────────────────────────────────────────────────
# Every trial is a one-hour, coach-led session; one trial per branch per start time.
# Each branch's open days and start times come from the `locations` table.
SESSION_MINUTES = 60

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Spellings callers (and the model) commonly use for a branch name.
LOCATION_ALIASES = {
    "ADYAR": "ADAYAR", "ADAIYAR": "ADAYAR",
    "EAST COAST ROAD": "ECR",
}


def location_key(name: str) -> str:
    """Comparable form of a branch name: upper case, no dots, single spaces."""
    key = " ".join(str(name or "").upper().replace(".", "").split())
    return LOCATION_ALIASES.get(key, key)


def _spoken_time(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {suffix}" if m else f"{h12} {suffix}"


def _mins(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


# ── Branch schedules ──────────────────────────────────────────────────────────
# A branch's schedule is a list of timings, each {"days": [0..6], "start": "HH:MM",
# "end": "HH:MM"} — e.g. Mon–Fri 06:00–10:00, Mon–Fri 16:30–20:30, Sat 07:00–10:00.
# One-hour trials can start on the hour from `start` while they still end by `end`.

def block_starts(start: str, end: str) -> list:
    """'16:30','20:30' → ['16:30','17:30','18:30','19:30']."""
    out, t = [], _mins(start)
    while t + SESSION_MINUTES <= _mins(end):
        out.append(_hhmm(t))
        t += SESSION_MINUTES
    return out


def schedule_from_slots(days, slot_times) -> list:
    """Convert the older format (one set of days + start times) into timings by merging
    back-to-back one-hour sessions: 06:00..09:00 → 06:00–10:00."""
    mins = sorted({_mins(t) for t in slot_times})
    blocks, i = [], 0
    while i < len(mins):
        start = end = mins[i]
        while i + 1 < len(mins) and mins[i + 1] == end + SESSION_MINUTES:
            i += 1
            end = mins[i]
        blocks.append({"days": sorted(set(days)), "start": _hhmm(start), "end": _hhmm(end + SESSION_MINUTES)})
        i += 1
    return blocks


def slots_for_weekday(schedule: list, weekday: int) -> list:
    """Every trial start time on a given weekday (Mon=0), across all timings."""
    return sorted({t for b in schedule if weekday in b["days"] for t in block_starts(b["start"], b["end"])})


def describe_days(days) -> str:
    """[0,1,2,3,4,5] → 'Monday to Saturday'; [0,2,4] → 'Monday, Wednesday, Friday'."""
    days = sorted(set(int(d) for d in days))
    if not days:
        return "no days set"
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return f"{WEEKDAY_NAMES[days[0]]} to {WEEKDAY_NAMES[days[-1]]}"
    return ", ".join(WEEKDAY_NAMES[d] for d in days)


def schedule_groups(schedule: list) -> list:
    """Group timings that share the same days, in weekday order:
    [("Monday to Friday", ["6 AM–10 AM", "4:30 PM–8:30 PM"]), ("Saturday", ["7 AM–10 AM"])]."""
    groups: dict = {}
    for b in sorted(schedule, key=lambda b: (min(b["days"]) if b["days"] else 9, b["start"])):
        groups.setdefault(tuple(sorted(b["days"])), []).append(
            f"{_spoken_time(b['start'])}–{_spoken_time(b['end'])}")
    return [(describe_days(days), ranges) for days, ranges in groups.items()]


def describe_schedule(schedule: list) -> str:
    """'Monday to Friday 6 AM–10 AM and 4:30 PM–8:30 PM; Saturday 7 AM–10 AM'."""
    return "; ".join(f"{days} {' and '.join(ranges)}" for days, ranges in schedule_groups(schedule)) or "no timings set"


def describe_location(loc: dict) -> str:
    """One block per branch, used in the call scripts: hours, then the exact trial start
    times for each group of days."""
    schedule = loc.get("schedule") or []
    lines = [f"• {loc['name']} — open {describe_schedule(schedule)}."]
    groups: dict = {}
    for wd in range(7):
        starts = slots_for_weekday(schedule, wd)
        if starts:
            groups.setdefault(tuple(starts), []).append(wd)
    for starts, days in groups.items():
        lines.append(f"  Trial start times on {describe_days(days)}: {', '.join(_spoken_time(t) for t in starts)}.")
    if loc.get("address"):
        lines.append(f"  Address: {loc['address']}.")
    return "\n".join(lines)


# ── Facts the agent may state on any call ─────────────────────────────────────
FACTS = """\
• Harry's Fitcamp brought the first strength-training gym to Chennai 12 years ago, when there were only
  bodybuilding gyms. The goal was never treadmills or machines just for physique — it's doing the right
  type of movement so you live a pain-free, healthy life as you get older.
• Run more like a school than a gym: every class is guided by a coach — nobody is left to do their own thing.
• Every workout is customised to the individual. Choose one-on-one, or a community class: one coach with up
  to eight members, each on their own program.
• Coaches are certified strength-and-conditioning professionals, backed by an in-house physiotherapist
  for injury management and rehabilitation.
• Members include beginners, prenatal/postnatal mothers, perimenopausal women, cricketers, marathon runners,
  cyclists, kids from age seven, and members around 70 who come to stay active. It's run more like a
  wellness studio than a conventional gym.
• Miss a weekday class and you can make it up on Saturday. Each branch's days and timings are listed under
  BRANCHES & TIMINGS.
• The trial and assessment session is free, lasts one hour, and carries no obligation.
• Memberships: 3 months ₹35,000; 6 months ₹60,000; 1 year ₹80,000."""

# ── The two campaigns ─────────────────────────────────────────────────────────
TRIAL = "trial"
MISSED_CLASS = "missed_class"
INBOUND = "inbound"  # not a campaign; used to label incoming calls

CAMPAIGNS = {
    TRIAL: {
        "label": "Trial booking",
        "summary": "Call new leads, introduce Harry's Fitcamp, and book a free one-hour trial.",
    },
    MISSED_CLASS: {
        "label": "Missed class",
        "summary": "Call members who missed class, ask why, record the reason, and encourage them to come regularly.",
    },
}

# Reason categories the agent picks from when it records why a member missed class.
MISSED_REASONS = {
    "sick_or_injured": "Sick or injured",
    "work": "Work",
    "travel": "Travelling",
    "family": "Family",
    "schedule": "Timing doesn't suit",
    "lost_motivation": "Lost motivation",
    "cost": "Cost / membership",
    "other": "Other",
    "not_given": "Didn't say",
}


def campaign_or_default(value) -> str:
    """Normalise a campaign key; anything unknown falls back to trial booking."""
    value = str(value or "").strip().lower()
    return value if value in CAMPAIGNS else TRIAL
