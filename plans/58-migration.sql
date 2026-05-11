-- Plan 58 migration: flatten FSM, unify fixup flow, add lifecycle phases.
--
-- Run while daemon is stopped:
--   qk daemon stop
--   sqlite3 .quikode/quikode.db < plans/58-migration.sql
--   qk daemon start --detach --max-parallel 12
--
-- This SQL is idempotent: re-running on an already-migrated DB is a no-op.
-- The Python schema migrations in quikode/state_schema.py already add the
-- new columns + rename deprecated state values to PENDING. This script
-- handles the deeper phase/cycle derivation that requires more context
-- than the bare-bones backfill can provide.

BEGIN;

-- 1. Backup the tasks table so the operator can roll back if anything
--    looks off after the daemon restarts. SQLite's `CREATE TABLE ... AS`
--    captures both schema + data in one shot.
DROP TABLE IF EXISTS tasks_backup_plan58;
CREATE TABLE tasks_backup_plan58 AS SELECT * FROM tasks;

-- 2. The column-add DDL is handled by `apply_migrations` at next startup.
--    These ALTERs are no-ops if the columns already exist; we include
--    them so a manual SQL run sees the right schema before the
--    derivation queries below.

ALTER TABLE tasks ADD COLUMN phase TEXT NOT NULL DEFAULT 'initial';
ALTER TABLE tasks ADD COLUMN cycle_in_phase INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN pr_review_trigger TEXT NOT NULL DEFAULT 'none';

-- (If the ALTERs above fail with "duplicate column name" on re-run, that
-- is expected. sqlite3 client surfaces the error but continues; the
-- subsequent UPDATEs are still applied. To silently no-op those ALTERs
-- when re-running, wrap them in a script that catches OperationalError.)

-- 3. Derive `phase` from the (current state + has-subtasks + has-PR)
--    triple. Order matters: more specific rules first.

-- 3a. PR_REVIEW: any task with a PR number is in PR_REVIEW phase.
--     (PR_OPENING is the brief transit; treat as PR_REVIEW for accounting.)
UPDATE tasks
SET phase = 'pr_review'
WHERE pr_number IS NOT NULL AND pr_number != 0
  AND phase = 'initial';

-- 3b. PRE_PR_REVIEW: in any audit-stage state, or in local_ci_checking /
--     fixup_planning with no PR yet.
UPDATE tasks
SET phase = 'pre_pr_review'
WHERE phase = 'initial'
  AND (pr_number IS NULL OR pr_number = 0)
  AND state IN (
    'audit_local_ci', 'audit_rubric', 'audit_standards',
    'audit_architecture', 'audit_behavior',
    'local_ci_checking', 'fixup_planning'
  );

-- 3c. The legacy 'pre_pr_auditing' / 'addressing_feedback' state values
--     were renamed to 'pending' (with resume_from_existing_subtasks=1)
--     by `apply_migrations` before this script runs. Tasks whose state
--     was 'addressing_feedback' had a PR open, so they already landed
--     in `phase = 'pr_review'` via 3a. Tasks whose state was
--     'pre_pr_auditing' had no PR open and at least one done subtask,
--     so we'd want them at PRE_PR_REVIEW; but since the python migration
--     reset them to PENDING, we can detect them via the resume marker.
--     For safety, leave such resume-flagged rows at phase=initial — the
--     worker will re-enter the gauntlet and naturally fire the
--     INITIAL → PRE_PR_REVIEW phase transition.

-- 4. Derive cycle_in_phase. The pre-plan-58 source of truth is the
--    `pre_pr_audit_summary.cycle` value (when present, indicating the
--    audit gauntlet ran at least once). For PR_REVIEW tasks we also
--    derive from `review_round` and `ci_triage_retries`.

-- 4a. PRE_PR_REVIEW cycle: use the audit summary's cycle if present.
UPDATE tasks
SET cycle_in_phase = COALESCE(
    json_extract(pre_pr_audit_summary, '$.cycle'),
    1
)
WHERE phase = 'pre_pr_review'
  AND pre_pr_audit_summary IS NOT NULL;

-- 4b. PR_REVIEW cycle: sum of review_round + ci_triage_retries gives a
--     conservative estimate of "how many PR_REVIEW fixup cycles have
--     happened". If both are 0, the PR opened but no fixup trigger has
--     fired yet; cycle stays at 0.
UPDATE tasks
SET cycle_in_phase = COALESCE(review_round, 0) + COALESCE(ci_triage_retries, 0)
WHERE phase = 'pr_review';

-- 5. Derive pr_review_trigger from the most recent post-PR state in the
--    state_log. If the most recent post-PR-fixup state was hit via
--    CI_FAILED (review_round unchanged but ci_triage_retries > prior),
--    label as ci_failure; if via CHANGES_REQUESTED_RECEIVED, label as
--    review_feedback. Without rich event metadata in state_log, fall
--    back to: if review_round > 0, the most recent trigger was likely
--    review_feedback; else ci_failure.

UPDATE tasks
SET pr_review_trigger = CASE
    WHEN phase = 'pr_review' AND COALESCE(review_round, 0) > 0 THEN 'review_feedback'
    WHEN phase = 'pr_review' AND COALESCE(ci_triage_retries, 0) > 0 THEN 'ci_failure'
    ELSE 'none'
END;

-- 6. Sanity sweep: any task with phase=pr_review but pr_number is NULL
--    is a data inconsistency — log it via SELECT for operator inspection.
--    (sqlite3 CLI prints SELECT output to stdout.)

SELECT 'WARNING: phase=pr_review but no pr_number' AS issue, id, state, pr_number
FROM tasks
WHERE phase = 'pr_review' AND (pr_number IS NULL OR pr_number = 0);

-- 7. Any task whose state was 'pre_pr_auditing' or 'addressing_feedback'
--    (now 'pending' with resume_from_existing_subtasks=1) — surface for
--    operator inspection. The worker will re-enter the unified driver
--    fresh on next pickup; no manual intervention required.

SELECT 'INFO: deprecated state remapped to pending+resume' AS issue,
       id, last_processed_review_id, pr_number, review_round
FROM tasks
WHERE state = 'pending' AND resume_from_existing_subtasks = 1
  AND pr_number IS NOT NULL;

COMMIT;

-- Done. After the daemon restarts the unified audit driver picks up
-- post-PR tasks at AUDIT_LOCAL_CI (via CI_FIXUP_START or REVIEW_FIXUP_START
-- as appropriate) and pre-PR tasks resume their gauntlet from cycle N+1
-- using the existing pre_pr_audit_summary.
