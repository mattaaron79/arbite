---
id: tic-8a53
title: 'scenario LC1 breaks on the first of every month: the closed archive folder
  is not normalised'
status: closed
type: bug
tier: medium
domain: io
epic: test-determinism
priority: 2
tags:
- tests
- examples
- dates
- close
assignee: omp.qwen3.8-next-flash.006
depends_on: []
blocked_by: null
created: '2026-10-09T21:41:18'
updated: '2026-10-09T21:46:09'
closed: '2026-10-09T21:46:09'
---

## Description
Scenario LC1 (`arbite close`, .arbite/planning/interaction-examples.md:1003-1020) fails on every machine since 2026-10-01. The transcript pins one path segment that is a function of the wall clock:

    closed tic-cf9f (claude.opus.001) -> .arbite/closed/2026-09/tic-cf9f.md

and a close today moves the ticket into `closed/2026-10/`, so the byte-for-byte comparison in tests/examples.py::_assert_text fails on that single line while every claim-release fact LC1 is actually about matches exactly. It is not a product regression: the file sink archives closed tickets in a folder named after the close month, and that behaviour is pinned where it belongs, in tests/test_file_sink.py:285 (`expected closed/2026-03/`). The transcript is what is over-specified -- and it will break again on the first of every month.

Fix: teach `normalise()` (tests/examples.py:324-341) to fold the archive-month segment -- `closed/<YYYY-MM>/` on both sides of the comparison, alongside the existing `ID_RE`, `DIGEST_RE` and timestamp rules. Keep it narrow: the bucket (`closed/`) and the rest of the path must still match literally, so a close that files the ticket in the wrong bucket, or in `shelved/`, or not at all, still fails the scenario. Do not weaken it by dropping the path from the transcript, and do not change LC1's frozen block to a placeholder -- the document stays readable as a real transcript.

Then keep the harness honest about what it folds: `normalise`'s docstring (:325-328) and the module docstring's explanation of the placeholders (:6-14, which enumerates ids, times, dates, the project root) must name the archive-month rule, and so must the preamble of `.arbite/planning/interaction-examples.md` wherever it tells a reader what gets normalised. A reader of the normative document needs to know that the month in that line is not asserted.

Acceptance:
- `python -m pytest -q tests/test_lifecycle_examples.py::test_LC1_close_releases_claims` passes today, and passes when the clock is faked forward -- prove it with `faketime` if available, otherwise by running the suite with `TZ` variations plus a direct check that `normalise("... .arbite/closed/2026-09/tic-cf9f.md")` and `normalise("... .arbite/closed/2031-01/tic-cf9f.md")` are equal while `normalise("... .arbite/shelved/tic-cf9f.md")` differs. Report what you ran.
- Every other scenario still passes: `python -m pytest -q tests/test_*_examples.py tests/test_examples.py`.
- A regression guard for the fold: a small unit test in whichever module tests the harness (`tests/test_examples.py`) asserting that the archive month is folded while the bucket name, the ticket id and a digest are still distinguished. Do not add a test that merely re-asserts the regex on one literal string with no contrast case.
- No product code changes. The scenario state builder (tests/lifecycle_state.py) and the file sink are correct as they are.

## Notes
- 2026-10-09T21:45:04 omp.qwen3.8-next-flash.006: normalise() now folds the closed-archive month: ARCHIVE_MONTH_RE matches only closed/<YYYY-MM>/ and rewrites it to closed/YYYY-MM/, so the bucket, the ticket id, digests and the rest of the path stay literal (a filing into shelved/, the wrong bucket, or an unmoved ticket still fails the scenario). Documented in normalise's docstring, tests/examples.py's module docstring, and the Normalisation preamble of interaction-examples.md; LC1's frozen block untouched, no product code changed. Verifier observes: LC1 passes today and under TZ extremes (Pacific/Kiritimati, Etc/GMT+12); tests/test_examples.py::test_normalisation_folds_the_closed_archive_month_and_nothing_else pins closed/2026-09 == closed/2031-01 while shelved/, open/2026-09/, att-91bd and a digest each still contrast; tests/test_*_examples.py + tests/test_examples.py: 178 passed. faketime not installed (command -v empty), so the ticket's fallback route was taken: direct normalise contrasts + TZ variations.

- 2026-10-09T21:46:08 omp.qwen3.8-next-flash.001: verified by the parent: 'pytest tests/test_lifecycle_examples.py tests/test_examples.py' is 31 passed (LC1 included, in a shell that exports COLORTERM); direct contrast shows normalise folds closed/2026-09/ and closed/2031-01/ to the same string while shelved/, open/2026-09/, att-91bd.md and a digest all stay distinct, so a close filing into the wrong bucket still fails the scenario; the new guard test carries those contrasts; the normative document's LC1 block is untouched and only its normalisation preamble changed. Observable to a verifier: LC1 passes in any month, and 'arbite close' still moves the ticket into closed/<this month>/.

- 2026-10-09T21:46:09 system: Submitted; closed (review disabled).
