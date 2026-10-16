-- ═══════════════════════════════════════════════════════
-- 0001 — Baseline schema (T-800)
--
-- This is the historical supabase_schema.sql captured as migration 0001. Every
-- statement is idempotent (IF NOT EXISTS / ADD COLUMN IF NOT EXISTS), so applying
-- it to an already-provisioned database is a safe no-op and applying it to a fresh
-- database creates the full baseline. New changes go in later numbered files.
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    location TEXT,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_trials_booked_slot
ON trials (location, date, time) WHERE status = 'booked';

CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    outcome TEXT,
    reason TEXT,
    duration_seconds INTEGER,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);

ALTER TABLE appointments  DISABLE ROW LEVEL SECURITY;
ALTER TABLE trials        DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs     DISABLE ROW LEVEL SECURITY;
ALTER TABLE settings      DISABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs    DISABLE ROW LEVEL SECURITY;

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_url TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transcript TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS estimated_cost DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);
ALTER TABLE campaigns DISABLE ROW LEVEL SECURITY;

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS purpose TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS feedback TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS prompt_status TEXT;

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calcom_booking_uid TEXT;

CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE contact_memory DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_contact_memory_phone ON contact_memory (phone_number);

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS agent_profile_id TEXT;

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT 'Aoede',
    model TEXT NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE agent_profiles DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    assistant_name TEXT DEFAULT 'Tina',
    inbound_numbers TEXT DEFAULT '[]',
    outbound_prompt TEXT,
    inbound_prompt TEXT,
    business_context TEXT,
    booking_config TEXT DEFAULT '{}',
    voice TEXT,
    model TEXT,
    greeting TEXT,
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE brands DISABLE ROW LEVEL SECURITY;

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS brand_id TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS brand_id TEXT;
ALTER TABLE trials    ADD COLUMN IF NOT EXISTS brand_id TEXT;

ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_location_check;
ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_duration_minutes_check;

ALTER TABLE trials ADD COLUMN IF NOT EXISTS service  TEXT;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS resource TEXT;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS end_time TEXT;
DROP INDEX IF EXISTS idx_trials_booked_slot;
CREATE INDEX IF NOT EXISTS idx_trials_brand_date
ON trials (brand_id, date) WHERE status = 'booked';

INSERT INTO brands (id, name, assistant_name, inbound_numbers, booking_config, is_default, created_at)
SELECT gen_random_uuid()::text, 'My Business', 'Tina', '[]', '{}',
       1, now()::text
WHERE NOT EXISTS (SELECT 1 FROM brands);

UPDATE call_logs SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
UPDATE campaigns SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
UPDATE trials    SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
