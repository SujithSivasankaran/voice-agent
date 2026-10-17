-- ═══════════════════════════════════════════════════════
-- 0007 — Per-day timings for each branch
--
-- `schedule` is a JSON list of timings: [{"days": [0..6], "start": "HH:MM",
-- "end": "HH:MM"}, …] — e.g. Mon–Fri 06:00–10:00 and Saturday 07:00–10:00.
-- Branches saved before this keep working: the app derives their timings from the
-- older `days` + `slot_times` columns until they are next edited.
-- ═══════════════════════════════════════════════════════

ALTER TABLE locations ADD COLUMN IF NOT EXISTS schedule TEXT;
