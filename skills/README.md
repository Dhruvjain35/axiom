# Agent Skill contribution — `implementing-crash-safe-work-queues`

This directory holds an Agent Skill written for the
[cockroachlabs/cockroachdb-skills](https://github.com/cockroachlabs/cockroachdb-skills)
repository. It is laid out to mirror that repo exactly, so contributing it is a copy of one
directory and nothing else.

```
skills/cockroachdb-application-development/implementing-crash-safe-work-queues/
├── SKILL.md                              390 lines
└── references/validation-queries.md
```

## What it is

The crash-safe queue pattern AXIOM proves, written as a skill for someone building a queue
on CockroachDB who has never heard of AXIOM. Twelve steps, each one a decision with the
reasoning attached:

- Complete work with a state transition, never a `DELETE` — no tombstones at the queue head
- A **partial** claim index on non-terminal states, so finished work leaves the index
- An **explicit computed `shard` column** rather than `USING HASH`, so a worker can be
  pinned to a shard subset the way a Kafka consumer group is
- `STORING` the claim predicate's columns, and why that is what makes `SKIP LOCKED` viable
- Claim as one statement with a compare-and-swap on the fence
- A **fencing token, not a lease**, as the correctness mechanism
- One `available_at` column doing earliest-run-time *and* lease expiry — which deletes the
  reaper, and the reaper is a hotspot
- A **`GENERATED STORED` idempotency key**, so it cannot be minted at call time
- A unique partial index enforcing at most one in-flight external call per step
- Receipt committed *before* the call, call outside every transaction, with the crash-window
  table that follows from that ordering
- A request fingerprint, because the same key with a different body is not a retry
- Validation that asserts on query **plans**, since every property here degrades silently

It cites CockroachDB's own hotspot guidance, links to official docs rather than restating
them, and says "effectively-once, not exactly-once" in the text.

## It passes the upstream validator

Verified by downloading `scripts/validate-spec.py` from the skills repo at `main` and
running it against a copy of this directory staged inside a mirror of the upstream tree:

```
$ python validate-spec.py upstream_sim/skills/cockroachdb-application-development/implementing-crash-safe-work-queues/ --strict
✓ All validations passed!
```

Zero errors, zero warnings, in `--strict` mode — where warnings are errors. `name` is 35
characters of the 64 allowed and matches its directory; `description` is 850 of the 1024
allowed, third person, two sentences, with "Use when" triggers; `SKILL.md` is 390 lines
against a 500-line recommendation; the only subdirectory is `references/`, which is on the
allowed list.

Run inside *this* repo instead, the validator emits one warning — a broken relative link to
`../designing-application-transactions/SKILL.md`, a sibling skill that exists upstream and
not here. That link resolves once the directory is in place upstream, which is why the
verification above was done against a staged mirror.

## Opening the PR is the operator's call

Not done, deliberately. `CONTRIBUTING.md` asks contributors to **propose the skill in an
issue first** and agree scope with the maintainers before sending a PR, and that is a
conversation with a human, not a command to run. The steps, in their order:

```bash
# 1. Propose, using .github/ISSUE_TEMPLATE/new-skill.yml
#    Domain: cockroachdb-application-development
#    Name:   implementing-crash-safe-work-queues

# 2. After alignment:
git clone https://github.com/cockroachlabs/cockroachdb-skills
cd cockroachdb-skills
git checkout -b add-skill/application-development/implementing-crash-safe-work-queues

cp -R <axiom>/skills/cockroachdb-application-development/implementing-crash-safe-work-queues \
      skills/cockroachdb-application-development/

pip install -r scripts/requirements.txt
python scripts/validate-spec.py skills/cockroachdb-application-development/implementing-crash-safe-work-queues/ --strict

# 3. PR using .github/PULL_REQUEST_TEMPLATE.md, referencing the proposal issue.
```

**Domain choice.** `cockroachdb-application-development` over
`cockroachdb-query-and-schema-design`, because although the load-bearing decisions are DDL,
the skill's subject is an application protocol — claim, prepare, dispatch, settle, recover —
and it sits directly alongside the existing `designing-application-transactions` and
`benchmarking-transaction-patterns`. A maintainer may reasonably move it; the file needs no
change if they do, beyond the one relative link in its second paragraph.

## Status in the submission

The hackathon asks for at least two of the four CockroachDB tools. AXIOM uses three of them
in the running system — Distributed Vector Indexing, the Cloud Managed MCP Server, and the
`ccloud` CLI — all verified live. The Agent Skills repo is the fourth, and it is the one
whose "use" is this file rather than a code path. **The skill is written and validated; no
PR has been opened**, and the submission says exactly that rather than claiming four of four.
