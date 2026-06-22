-- ═══════════════════════════════════════════════════════
-- T-800 — Complete Database Schema
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

-- One-hour trial bookings. Capacity is one trial per location/start time.
CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    location TEXT NOT NULL CHECK (location IN ('ADAYAR', 'ECR')),
    duration_minutes INTEGER NOT NULL DEFAULT 60 CHECK (duration_minutes = 60),
    status TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled')),
    created_at TEXT NOT NULL
);

-- Partial uniqueness allows a cancelled slot to be booked again while making
-- simultaneous booking attempts for the same live slot safe.
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
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS estimated_cost DOUBLE PRECISION;  -- per-call Gemini spend (USD)

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

-- Campaign purpose → LLM-generated prompt + summary + cumulative feedback
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS purpose TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS summary TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS feedback TEXT;          -- JSON array of {text, at}
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS prompt_status TEXT;     -- generating | rewriting | ready | error

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

-- ═══════════════════════════════════════════════════════
-- Brands (multi-tenant). Each brand is a separate business with its own
-- prompts, facts, booking setup, identity, and inbound phone numbers (DIDs).
-- A brand row with NULL prompt fields falls back to the built-in Harry's
-- prompts in prompts.py, so the seeded default brand behaves exactly as before.
-- ═══════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS brands (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    assistant_name TEXT DEFAULT 'Tina',
    inbound_numbers TEXT DEFAULT '[]',  -- JSON array of E.164 DIDs that route here
    outbound_prompt TEXT,               -- default script for general outbound calls
    inbound_prompt TEXT,                -- front-desk script for incoming calls
    business_context TEXT,              -- authoritative facts (replaces DEFAULT_BUSINESS_CONTEXT)
    booking_config TEXT DEFAULT '{}',   -- JSON: locations, slot_times, duration_minutes, off_days, pricing_text, transfer_number
    voice TEXT,                         -- optional Gemini voice override
    model TEXT,                         -- optional model override
    is_default INTEGER DEFAULT 0,       -- brand used when no DID match / no brand_id supplied
    created_at TEXT NOT NULL
);
ALTER TABLE brands DISABLE ROW LEVEL SECURITY;

-- Scope existing tables to a brand.
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS brand_id TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS brand_id TEXT;
ALTER TABLE trials    ADD COLUMN IF NOT EXISTS brand_id TEXT;

-- Multi-brand booking: a location/time may now exist once PER BRAND. Drop the
-- old global slot index and the Harry's-only CHECK constraints so each brand
-- can define its own locations and trial duration.
ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_location_check;
ALTER TABLE trials DROP CONSTRAINT IF EXISTS trials_duration_minutes_check;

-- Dynamic booking engine: a booking can name a service and a resource (stylist/
-- court/room) and span a variable duration, so availability is interval+capacity
-- based rather than one-per-exact-slot. The old exact-slot unique index can't
-- express overlap/capacity/resources, so it is dropped; correctness is enforced
-- in application logic (db.py) with a post-insert re-check.
ALTER TABLE trials ADD COLUMN IF NOT EXISTS service  TEXT;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS resource TEXT;
ALTER TABLE trials ADD COLUMN IF NOT EXISTS end_time TEXT;  -- HH:MM, for overlap checks
DROP INDEX IF EXISTS idx_trials_booked_slot;
CREATE INDEX IF NOT EXISTS idx_trials_brand_date
ON trials (brand_id, date) WHERE status = 'booked';

-- Seed Harry's Fitcamp as the default brand. NULL prompt fields => the
-- built-in prompts.py content is used, so nothing changes for Harry's.
-- After running, set this brand's inbound_numbers to the real Harry's DID
-- (via the Brands page or an UPDATE) so inbound routing matches it.
INSERT INTO brands (id, name, assistant_name, inbound_numbers, booking_config, is_default, created_at)
SELECT gen_random_uuid()::text, 'Harry''s Fitcamp', 'Tina', '[]',
       '{"locations":["ADAYAR","ECR"],"slot_times":["06:00","07:00","08:00","09:00","16:30","17:30","18:30","19:30"],"duration_minutes":60,"off_days":[6],"pricing_text":"3 months ₹35,000; 6 months ₹60,000; 1 year ₹80,000"}',
       1, now()::text
WHERE NOT EXISTS (SELECT 1 FROM brands);

-- Backfill existing rows onto the default brand so history/bookings stay valid.
UPDATE call_logs SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
UPDATE campaigns SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
UPDATE trials    SET brand_id = (SELECT id FROM brands WHERE is_default = 1 LIMIT 1) WHERE brand_id IS NULL;
