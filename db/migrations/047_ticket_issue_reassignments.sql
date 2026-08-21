-- Operator correction of which Odoo record owns a REVA-created GitHub issue
-- (spec 2026-08-20-issue-reassignment-design). REVA's ticket<->issue mapping is
-- otherwise implicit in ticket_issue_runs.issues, and a create-issues run fired
-- from the wrong record leaves no way to fix it: Odoo's handler replaces the
-- record's whole issue set from REVA's union, so moving the reva.github.issue
-- row alone is undone by the next callback.
--
-- One row per moved issue. Absence is the normal case; the runs stay untouched,
-- so deleting a row undoes the move.
-- Mirrors reva/db/models.py::TicketIssueReassignment.
CREATE TABLE IF NOT EXISTS ticket_issue_reassignments (
    id BIGSERIAL PRIMARY KEY,
    odoo_instance_id BIGINT NOT NULL REFERENCES odoo_instances(id),
    repo_full_name TEXT NOT NULL,
    number INTEGER NOT NULL,
    ticket_id BIGINT NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The per-issue direction: "who owns issue N of this repo?"
CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_issue_reassignments
    ON ticket_issue_reassignments (odoo_instance_id, repo_full_name, number);

-- The per-record direction: "what moved ONTO this record?" — needed because a
-- target record may have no ticket_issue_runs row of its own at all.
CREATE INDEX IF NOT EXISTS idx_ticket_issue_reassignments_record
    ON ticket_issue_reassignments (odoo_instance_id, ticket_id, model_name);
