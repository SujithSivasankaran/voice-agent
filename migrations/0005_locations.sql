-- ═══════════════════════════════════════════════════════
-- 0005 — Branches with their own timings, editable from the dashboard
--
-- Each branch has the weekdays it is open (JSON array, Mon=0 … Sun=6) and the
-- start times of its one-hour sessions (JSON array of "HH:MM"). Trial bookings and
-- the call scripts read from here, so adding a branch needs no redeploy.
-- Seeded with the two branches as they were configured before this change.
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address TEXT,
    days TEXT NOT NULL DEFAULT '[]',
    slot_times TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
ALTER TABLE locations DISABLE ROW LEVEL SECURITY;
CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_name ON locations (upper(name));

INSERT INTO locations (id, name, days, slot_times, created_at)
SELECT gen_random_uuid()::text, v.name, '[0,1,2,3,4,5]',
       '["06:00","07:00","08:00","09:00","16:30","17:30","18:30","19:30"]', now()::text
FROM (VALUES ('ADAYAR', 1), ('ECR', 2)) AS v(name, ord)
WHERE NOT EXISTS (SELECT 1 FROM locations)
ORDER BY v.ord;
