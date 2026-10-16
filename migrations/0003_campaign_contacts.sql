-- ═══════════════════════════════════════════════════════
-- 0003 — Per-contact campaign state (resumable, idempotent campaigns)
--
-- Previously a campaign's contacts lived only in campaigns.contacts_json and were
-- dialed by an in-memory loop: a restart mid-run re-dialed everyone and there was no
-- per-contact status. This table gives every (campaign, contact) an explicit state
-- so runs are resumable and a contact is never dialed twice for the same campaign.
--
-- status: queued | dialing | done | failed | skipped
--   queued  — not yet attempted
--   dialing — dispatch in flight (guards against double-dispatch across restarts)
--   done    — successfully dispatched to LiveKit
--   failed  — dispatch failed after retries (eligible for a later manual retry)
--   skipped — suppressed (DNC, outside calling window, or no phone)
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    outcome TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    skip_reason TEXT,
    created_at TEXT NOT NULL
);
ALTER TABLE campaign_contacts DISABLE ROW LEVEL SECURITY;

-- A contact appears once per campaign; dedupes re-imports and makes claiming a row
-- for dialing safe.
CREATE UNIQUE INDEX IF NOT EXISTS idx_campaign_contact_unique
ON campaign_contacts (campaign_id, phone_number);

CREATE INDEX IF NOT EXISTS idx_campaign_contacts_pending
ON campaign_contacts (campaign_id, status);
