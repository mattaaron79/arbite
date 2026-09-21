---
id: tic-c38b
title: 'Align the repo''s own config test with the deliberate review: false'
status: closed
type: chore
tier: low
domain: io
epic: null
priority: null
tags:
- tests
- config
assignee: deepseek.code.003
depends_on: []
blocked_by: null
created: '2026-09-21T09:30:21'
updated: '2026-09-21T09:33:20'
closed: '2026-09-21T09:33:20'
---

## Description
## Outcome and scope
`.arbite/project.yaml` now deliberately commits `review: false`, so `arbite submit` closes
a ticket directly instead of parking it in `review/` and the implementer commits on main.
That invalidates `tests/test_config.py::test_the_repos_own_config_takes_the_default`, which
asserts this repo's config omits the `review:` key and therefore reads as `true`.

Update that single test so it asserts the deliberate state, with a docstring that says the
value is an owner decision rather than an absent key: the committed config carries
`review: false`, `load_config` reports `{'sink': 'file', 'review': False}`, and
`review_enabled(repo_root)` is `False` (and keep the assertion that the flag is read as an
explicit boolean, not inferred). Rename it if the old name no longer describes it. Leave the
surrounding default-behaviour tests (absent key, no config file, malformed value) untouched.

## Acceptance criteria
- `python3 -m pytest -q` is fully green: 557 passed, 3 skipped, 0 failed (the failure this
  ticket removes is the only one).
- No other test, doc or docstring is changed to accommodate the flip.
- The test states, in one line, that `review: false` is the repo owner's deliberate choice.

## Boundaries
Test-only change. Do not edit `.arbite/project.yaml`, the review/submit code path, or any
coordination source under `src/arbite/coordination/`.

## Notes
- 2026-09-21T09:33:18 deepseek.code.003: Updated tests/test_config.py::test_the_repos_own_config_takes_the_default (renamed to test_the_repos_own_config_carries_the_owner_chosen_review_false, since the old name claimed a default it no longer takes) to assert the committed state instead of an absent key: load_config(repo_root) == {'sink': 'file', 'review': False}, config['review'] is False (explicit boolean read as written, not inferred/falsey), review_enabled(repo_root) is False. Docstring states in one line that review: false is the repo owner's deliberate choice. Neighbouring default-behaviour tests (absent key, no config file, malformed value), the review/status-vocabulary test, and all other files are untouched. Commands and results: 'python3 -m pytest -q tests/test_config.py' -> 23 passed in 0.05s (was 1 failed, 22 passed); 'python3 -m pytest -q' -> 558 passed, 3 skipped, 0 failed in 142.07s (was 557 passed, 3 skipped, 1 failed). Integration/QA observation: nothing in the product behaviour changes -- this is a test-only alignment with the already-committed .arbite/project.yaml (review: false). What QA can observe is the suite itself: running pytest from the repo root is now fully green, and the suite now pins that this repo's config file explicitly carries review: false rather than merely omitting the key, so a future edit that drops or flips that key in .arbite/project.yaml fails the test instead of passing it. arbite doctor remains exit 0. Deliberately not touched: the module docstring of tests/test_config.py still says 'No command reads the flag (it gates a future arbite submit)' and the '# --- the default: absent reads as true ---' section header now heads one test that is about an explicit override -- both are out of this ticket's stated scope (one test, no other doc/docstring changes) and are worth a follow-up chore.

- 2026-09-21T09:33:20 system: Submitted; closed (review disabled).
