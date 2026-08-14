-- AXIOM :: 005 — make the vector space explicit, and cache what Bedrock returns.
--
--     cockroach sql --url "$DATABASE_URL" -f db/005_embedding_space.sql
--
-- Apply order is 001 -> 003 -> 002 -> 004 -> 005. This one only touches the axiom
-- database and takes no dependency on the provider or the audit role, so it is safe to
-- re-run: every statement is IF NOT EXISTS or a no-op second time.
--
-- WHY
-- ---
-- axiom_memory.embedding_model was declared NOT NULL DEFAULT 'amazon.titan-embed-text-v2:0'
-- and no insert path ever set it. Every row on the demo cluster therefore claimed to hold
-- a Titan V2 embedding while holding a blake2b sketch from the AXIOM_OFFLINE stand-in.
-- Nothing computed a wrong answer — both sides of every cosine comparison were the same
-- stand-in — but a table that misdescribes its own contents is exactly the failure this
-- project exists to argue against, and it becomes a real one the moment the two spaces
-- coexist. axiom/memory.py now writes embeddings.MODEL_ID explicitly and preflight gate
-- 17 asserts the corpus holds exactly one space, and that it is the space the running
-- process would produce.
--
-- The default is dropped rather than corrected. A default here is a way to be wrong
-- quietly: any future insert path that forgets the column would inherit a claim about
-- provenance it never checked. NOT NULL with no default makes forgetting it an error.

SET database = axiom;

ALTER TABLE axiom_memory ALTER COLUMN embedding_model DROP DEFAULT;

-- ---------------------------------------------------------------- the durable cache
--
-- Bedrock sits on the request path of every recall, and this deployment is judged
-- unattended from Aug 19 to Sep 15. Two things follow. Repeat embeddings should not be
-- repeat charges — the demo embeds the same handful of situation descriptions for four
-- weeks — and, more importantly, a Bedrock outage should not empty the recall panel a
-- judge is looking at. The seeded query vectors live here, so the demonstration path
-- keeps answering out of CockroachDB when Bedrock does not answer at all.
--
-- Keyed on (model, sha256) rather than on the text: the key has to change when the
-- vector space changes, or an offline row would be served to an online query. `chars` is
-- kept for cost accounting — it is the only place the size of what was sent is recorded.
CREATE TABLE IF NOT EXISTS axiom_embedding_cache (
    model        STRING        NOT NULL,
    text_sha256  STRING        NOT NULL,
    chars        INT4          NOT NULL,
    embedding    VECTOR(1024)  NOT NULL,
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT pk_embedding_cache PRIMARY KEY (model, text_sha256)
);

-- No vector index on this table, deliberately. Nothing ever searches it by similarity —
-- it is a key-value store that happens to hold vectors, and a C-SPANN index on it would
-- cost writes to serve a query nobody issues.

COMMENT ON TABLE axiom_embedding_cache IS
  'Vectors already paid for. Keyed by (model, sha256) so a change of embedder cannot '
  'serve a vector from the wrong space. Also the reason a Bedrock outage degrades the '
  'demo rather than breaking it.';

COMMENT ON COLUMN axiom_memory.embedding_model IS
  'The vector space this row belongs to, written explicitly from embeddings.MODEL_ID. '
  'Never defaulted: a default here is a claim about provenance that nothing checked.';
