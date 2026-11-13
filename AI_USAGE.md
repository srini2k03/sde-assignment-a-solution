# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude Code** (Anthropic), used as a coding agent.

## What I wrote or decided myself
- **The specification.** I turned the assignment into a precise, phase-by-phase spec and
  checklist covering every rule, the CLI, the output contract and the verification steps.
- **Tech and scope.** Python + SQLite, and two stretch goals: aggregation in SQL and a production
  note for 100k leads/day.
- **Core design.**
  - Store facts at ingest and derive everything else (linking, orphans, buckets) at report
    time. That is what makes incremental and one-shot runs give identical reports.
  - Idempotency by file content hash.
  - An append-only ledger enforced by triggers.
- **PII approach.** A keyed HMAC of the phone for matching, encryption for names, and a key from
  `AU_PII_KEY` or a gitignored key file.
- **Ambiguity calls.** Every resolution in the `SUBMISSION.md` decisions table, e.g.
  `invalid_<field>` for required non-phone fields, first-column-wins, tie-breaks by `event_id`,
  earliest `call.initiated`.
- **Test strategy.** An independent reference implementation checked byte-for-byte against the
  pipeline on several seeds, because grading diffs exact output on an unseen seed.

## What I used them for
Working from my spec, Claude Code:
- implemented the pipeline in `solution/`;
- wrote the tests and the reference implementation;
- ran the pipeline on the small and large datasets;
- drafted the documentation and made the commits.

## Where the AI was wrong or unhelpful
- **Report too slow at scale.** Its first report design used nested SQL views and didn't finish
  on the large dataset. Timing runs caught this, and it was restructured into staged TEMP tables
  (about 8 s, identical output).
- **Gap in the cross-check.** Planted bugs showed the cross-check missed one rule ("earliest
  `call.initiated`"), because the generated data never exercises it. A hand-written edge-case
  test drop was added, and the cross-check now catches it.

## How I verified AI-generated code
- **Tests:** `pytest` (153 tests), including the byte-for-byte comparison against the
  independent implementation on seeds 1, 7, 42 and 123.
- **Fresh-clone run:** clone → install → generate → ingest drop 1 → ingest drop 2 → report →
  `pytest`. Re-ingesting confirmed nothing changes, and each report file was checked against the
  output contract.
