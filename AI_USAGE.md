# AI Usage Disclosure

AI tools are allowed. We want to know **how** you used them, because using them well is a
skill. Honest answers are never held against you. Discovering undisclosed use is.

## Tools used
- **Claude Code** (Anthropic), run as an autonomous coding agent in a cloud session.
- TODO(YOU): list any other AI tools you used (e.g. if another assistant helped you write the spec/prompt you gave Claude Code), or delete this line.

## What I used them for
- I wrote a detailed task prompt: the phases, the tech choices, the CLI, the full spec as a
  checklist, how to resolve each ambiguity, the test plan, and the verification steps. Claude
  Code then did the implementation work end to end:
  - read the original repo and the data generator;
  - wrote all of the code in `solution/` (normalisers, schema/DDL, ingest, `derive.sql`, report
    queries, CLI);
  - wrote all of the tests, including the independent reference implementation in
    `solution/tests/reference_impl.py`;
  - ran the pipeline on generated data (small and large), measured timings and collected the
    seed-42 numbers;
  - drafted `SUBMISSION.md`, `solution/README.md` and this file, and made the git commits.
- In short: **the code, tests and documentation in this repo were written by the AI**, working
  from my specification.

## What I wrote or decided myself
- The specification and decisions given to the agent up front: Python + SQLite, which stretch
  goals to do (SQL aggregation, production note), the CLI shape, the PII approach (keyed
  HMAC for matching + encryption, key from `AU_PII_KEY` or a gitignored key file), doing
  linking at report time for incremental equivalence, and the resolution of each ambiguity listed
  in the decisions table (e.g. `invalid_<field>`, first-column-wins, tie-breaks by `event_id`,
  earliest `call.initiated`).
- Requiring an independent reference implementation and byte-for-byte cross-checks on several
  seeds, because the graders diff exact output on an unseen seed.
- TODO(YOU): add anything you personally changed, reviewed in depth, or decided differently after reading the code, and remove anything above that you did not actually decide.

## Where the AI was wrong or unhelpful
- **Performance of the first report design.** The first version derived everything through
  nested SQL views. On `--scale small` it was instant, but on `--scale large` the report hadn't
  finished after more than 2 minutes, because every view re-evaluated the event→lead linking. This
  was caught by timing the large dataset. It was rewritten as staged, indexed TEMP tables
  (`derive.sql`), which brought it to ~8 s with byte-identical output.
- **Locked database in tests.** Some storage tests opened raw `sqlite3` connections that left an
  implicit transaction open after an expected failure, so later tests hit "database is
  locked". Caught by the test run; fixed by using autocommit connections that are always closed.
- **A blind spot in the cross-check.** The reference implementation agreed with the pipeline on
  every seed at the first attempt, which is suspicious on its own. A deliberate mutation
  test (changing "earliest `call.initiated`" to "latest") was *not* caught, because the generator
  never emits two initiated events for one call. A hand-written edge-case drop was added to the
  cross-check, and now that mutation fails it.
- TODO(YOU): add any example where you yourself spotted or rejected something the AI produced (or delete this line).

## How I verified AI-generated code
- `pytest` (153 tests), including byte-for-byte comparison against an independent
  implementation on seeds 1, 7, 42, 123, plus mutation checks that the comparison catches
  planted bugs.
- Manual runs: fresh clone → venv → `pip install` → generate → ingest drop 1 → ingest drop 2 →
  report → `pytest`, re-ingest to confirm nothing changes, and reading each report file against
  the output contract.
- TODO(YOU): describe how you reviewed the code yourself (e.g. read every module line by line, traced one lead and one override through the DB, re-ran tests). The interview includes a live walkthrough and a live change.
