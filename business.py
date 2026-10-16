# business.py — everything specific to Harry's Fitcamp, in one place.
#
# The app serves exactly one business with exactly two outbound campaigns. There
# is deliberately no editor for any of this: to change the hours, branches, facts
# or a script, edit this file (and prompts.py) and redeploy.

BUSINESS_NAME = "Harry's Fitcamp"
ASSISTANT_NAME = "Tara"
DEFAULT_VOICE = "Leda"  # Gemini Live voice; overrides GEMINI_TTS_VOICE

# ── Trial booking schedule ────────────────────────────────────────────────────
# Every trial is a one-hour, coach-led session. Classes run Mon–Fri 6–10 AM and
# 4:30–8:30 PM, so these are the one-hour starts that fit inside those windows.
# One trial per branch per start time.
BRANCHES = ("ADAYAR", "ECR")
SESSION_TIMES = ("06:00", "07:00", "08:00", "09:00", "16:30", "17:30", "18:30", "19:30")
SESSION_MINUTES = 60
CLOSED_WEEKDAYS = {6}  # Mon=0 … Sun=6 → closed Sunday

# How callers (and the model) commonly spell the branches.
BRANCH_ALIASES = {
    "ADAYAR": "ADAYAR", "ADYAR": "ADAYAR", "ADAIYAR": "ADAYAR",
    "ECR": "ECR", "EAST COAST ROAD": "ECR",
}

# Spoken form of SESSION_TIMES, for prompts.
SESSION_TIMES_SPOKEN = "6, 7, 8 or 9 in the morning, or 4:30, 5:30, 6:30 or 7:30 in the evening"

# ── Facts the agent may state on any call ─────────────────────────────────────
FACTS = """\
• Harry's Fitcamp opened Chennai's first strength-training gym 12 years ago. The focus is correct
  movement, pain-free living and long-term health — not appearance alone.
• Every session is coach-guided and individually customised: one-to-one training, or small groups of up
  to seven people, each following their own program.
• Coaches are certified strength-and-conditioning professionals, backed by an in-house physiotherapist
  for injury management and rehabilitation.
• Members include beginners, athletes, children from age seven, adults into their 70s,
  prenatal/postnatal and perimenopausal women, and people in injury rehab.
• Hours: Monday–Friday, 6–10 AM and 4:30–8:30 PM. Missed weekday sessions can be made up on Saturday.
• Branches: ADAYAR and ECR.
• The trial and assessment session is free, lasts one hour, and carries no obligation.
• Membership prices — only quote if asked: 3 months ₹35,000; 6 months ₹60,000; 1 year ₹80,000."""

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
