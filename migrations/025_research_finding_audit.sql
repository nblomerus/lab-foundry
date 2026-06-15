-- 025_research_finding_audit.sql
-- Reconnect the verification spine to the RESEARCH era. Evaluation (Aletheia) audited the market-era
-- `findings` table, which the research loop never writes (it writes `research_findings`), so 1096
-- completed tasks produced 0 audits and the critic never fired. Add the audit columns so Aletheia can
-- score a synthesized finding's groundedness and (on a confident pass) hand it to Momus (critic) via
-- finding.high_signal. Idempotent.
ALTER TABLE public.research_findings ADD COLUMN IF NOT EXISTS audit_score   real;
ALTER TABLE public.research_findings ADD COLUMN IF NOT EXISTS audit_verdict text;
CREATE INDEX IF NOT EXISTS research_findings_unaudited_idx
    ON public.research_findings (direction_claim_id) WHERE audit_verdict IS NULL;
