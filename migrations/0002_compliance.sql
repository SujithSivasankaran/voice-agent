-- ═══════════════════════════════════════════════════════
-- 0002 — Telephony compliance: Do-Not-Call suppression list
--
-- A number on this list is never dialed by campaigns or single/batch calls. Entries
-- are per-brand-optional: a NULL brand_id is a GLOBAL suppression (applies to every
-- brand); a set brand_id suppresses only that brand's calls. Sources: caller opt-out
-- during a call, manual dashboard entry, or a regulatory DNC import.
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS dnc_list (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    brand_id TEXT,                 -- NULL => global suppression across all brands
    reason TEXT,                   -- 'caller_opt_out' | 'manual' | 'regulatory' | ...
    source TEXT,                   -- who/what added it (agent, dashboard, import)
    created_at TEXT NOT NULL
);
ALTER TABLE dnc_list DISABLE ROW LEVEL SECURITY;

-- One suppression per (number, brand-scope). Distinct partial indexes because NULL
-- is not comparable in a normal unique index — we want at most one GLOBAL row per
-- number and at most one row per (number, brand).
CREATE UNIQUE INDEX IF NOT EXISTS idx_dnc_global
ON dnc_list (phone_number) WHERE brand_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dnc_brand
ON dnc_list (phone_number, brand_id) WHERE brand_id IS NOT NULL;
