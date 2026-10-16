-- ═══════════════════════════════════════════════════════
-- 0004 — Single business (Harry's Fitcamp) with two fixed campaigns
--
-- The app no longer supports multiple brands or free-form campaigns. Every outbound
-- call is either a "trial" call (book a free trial) or a "missed_class" call (ask a
-- member why they missed class). Old brand/profile tables and columns are left in
-- place untouched so no history is lost; the code simply stops using them.
-- ═══════════════════════════════════════════════════════

-- Which campaign a call / call-list run belongs to: trial | missed_class | inbound.
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS campaign TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS kind TEXT;

-- Missed-class call lists can carry the date of the class the member missed.
ALTER TABLE campaign_contacts ADD COLUMN IF NOT EXISTS missed_date TEXT;

-- Reasons members give for missing class, recorded by the agent during the call.
CREATE TABLE IF NOT EXISTS missed_classes (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    member_name TEXT,
    missed_date TEXT,          -- the class they missed, if the call list said
    reason_category TEXT,      -- sick_or_injured | work | travel | family | schedule | lost_motivation | cost | other | not_given
    reason TEXT,               -- their reason, in a few words
    coming_back TEXT,          -- when they said they'd be back, if they said
    call_id TEXT,              -- call_logs.id of the call that recorded it
    created_at TEXT NOT NULL
);
ALTER TABLE missed_classes DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_missed_classes_created ON missed_classes (created_at);

-- One trial per branch per start time, enforced by the database so two calls can
-- never book the same slot at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS idx_trials_one_per_slot
ON trials (location, date, time) WHERE status = 'booked';
