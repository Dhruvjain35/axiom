-- =====================================================================================
-- AXIOM :: 002_audit_role.sql — the read-only identity the Audit Agent runs as.
--
-- The Audit Agent answers natural-language questions ("was any order ever refunded
-- twice?") by generating SQL and running it against the LIVE database. That is only a
-- defensible design if the identity it runs as physically cannot write. An LLM that can
-- be talked into `UPDATE axiom_action_attempt SET attempt_state = 'SUCCEEDED'` would
-- falsify the exact audit trail it exists to read.
--
-- So the containment is layered, and the layers are independent:
--
--   1. THIS FILE — a role with SELECT and nothing else. Enforced by the database, so it
--      holds even if every line of Python above it is wrong.
--   2. axiom/audit_mcp.py's statement validator — SELECT/WITH only, one statement, no
--      DML/DDL keywords, LIMIT injected. Enforced before the statement is sent.
--   3. default_transaction_read_only on the role — every transaction this login opens
--      starts READ ONLY, so even a statement that slipped past (2) aborts at the server.
--
-- Any one of the three would be sufficient on a good day. All three exist because the
-- thing being protected is the record of what money moved.
--
-- Apply:
--   cockroach sql --insecure --host localhost:26257 -d axiom -f db/002_audit_role.sql
--
-- On CockroachDB Cloud the same grants apply to the SQL user backing the Managed MCP
-- Server's service account; see axiom/audit_mcp.py's module docstring for the ccloud
-- commands that mint it.
-- =====================================================================================

-- The privilege bundle. A ROLE rather than a USER so the grants can be attached to any
-- number of login identities (local `axiom_audit`, a Cloud service account, a human
-- on-call reader) without ever being re-derived — a second hand-written copy of a
-- privilege set is how a "read-only" account quietly acquires INSERT.
CREATE ROLE IF NOT EXISTS axiom_auditor;

-- --------------------------------------------------------------------- axiom database
GRANT CONNECT ON DATABASE axiom TO axiom_auditor;
GRANT USAGE ON SCHEMA axiom.public TO axiom_auditor;
GRANT SELECT ON ALL TABLES IN SCHEMA axiom.public TO axiom_auditor;

-- Tables created after this file runs must inherit the same grant, or the audit agent
-- goes blind on exactly the new table somebody added in a hurry.
ALTER DEFAULT PRIVILEGES FOR ALL ROLES IN SCHEMA axiom.public
    GRANT SELECT ON TABLES TO axiom_auditor;

-- ------------------------------------------------------------------ provider database
-- The provider is a SEPARATE database with no shared transaction (db/003_provider.sql
-- explains why that separation is the whole experiment). The audit agent needs to read
-- it anyway: "was any order ever refunded twice?" is a question about the EXTERNAL
-- ledger, and being able to join the agent's belief against the world's record in one
-- query is the point of putting both on one cluster.
GRANT CONNECT ON DATABASE provider TO axiom_auditor;
GRANT USAGE ON SCHEMA provider.public TO axiom_auditor;
GRANT SELECT ON ALL TABLES IN SCHEMA provider.public TO axiom_auditor;

ALTER DEFAULT PRIVILEGES FOR ALL ROLES IN SCHEMA provider.public
    GRANT SELECT ON TABLES TO axiom_auditor;

-- ------------------------------------------------------------------------ the login
-- Local development identity. On an insecure single-node cluster this connects with no
-- password, which is fine because the cluster is not reachable off the laptop; in Cloud
-- the equivalent identity is a service account whose API key is passed to the Managed
-- MCP Server as a bearer token and never lands in this repo.
CREATE USER IF NOT EXISTS axiom_audit;
GRANT axiom_auditor TO axiom_audit;

-- Layer 3. Every transaction this login opens is READ ONLY before it executes anything,
-- including a statement the Python validator failed to recognise as a write.
ALTER ROLE axiom_audit SET default_transaction_read_only = true;

-- CockroachDB gives every role membership in `public`; make sure that membership is not
-- quietly carrying a write grant on a table someone created with a permissive default.
REVOKE ALL ON DATABASE axiom FROM public;
REVOKE ALL ON DATABASE provider FROM public;

-- --------------------------------------------------------------------- verification
-- Run these after applying. The first must list SELECT and nothing else; the second must
-- fail with "user axiom_audit does not have INSERT privilege".
--
--   SHOW GRANTS ON TABLE axiom.public.axiom_action_attempt FOR axiom_auditor;
--   -- as axiom_audit:
--   INSERT INTO axiom.public.axiom_tenant (id, slug, display_name)
--        VALUES (gen_random_uuid(), 'x', 'x');
