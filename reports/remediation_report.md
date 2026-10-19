# Remediation Report

## Defects fixed

No Critical or High severity issue was established in the audits. The confirmed Medium-severity defects and the tested PRD gaps below have been addressed with focused changes; no application data was used or overwritten.

| Finding | Root cause and remediation |
|---|---|
| AUD-001 — index cancellation/publication race | Cancellation, publication, failure, and completion could update state outside a shared synchronization boundary. `app/indexing.py` now serializes these transitions using `_state_guard` and checks terminal state before publishing. Tests force both orderings so an interrupted job cannot publish rows or later become completed, and successful finalization remains consistent. |
| AUD-002 — duplicate research after an ambiguous response | The browser discarded the request identity when a POST response was lost, while recovery did not reliably find a terminal job by request ID. `static/app.js` now persists the pending payload and request ID, reuses them on recovery/retry, and blocks replacement by a different unresolved payload. `app/jobs.py` resolves a unique request ID to its job, including terminal jobs. |
| AUD-003 — Host-derived Origin fallback | The unconfigured Origin policy derived its allowlist from the request Host. `app/main.py` now uses fixed loopback origins unless an explicit `LEXCITE_PUBLIC_URL` is configured. Deployment guidance and `.env.example` describe the required configuration for non-default origins. This closes the requirements gap; it does not establish that the prior behavior was exploitable as browser CSRF. |
| AUD2-001 / AUD2-003 — unsafe restore and partial backup | Restore could report success without required archive members, and backup creation could leave a partial final-path file. `scripts/backup.py` and `scripts/restore.py` now validate archive members, database integrity, and required encryption material; stage restore into an empty destination; and publish backups only after successful validation. `scripts/archive_utils.py` checks encrypted settings across model, retrieval, and search configurations and verifies the supplied key can decrypt saved tokens. |
| AUD2-002 — law review racing with deletion | Review checked existence separately from the update and could claim success after concurrent deletion. `app/main.py` now checks the update row count and persists the audit record in the same transaction. |
| R6-E2 — unexpected official-source parser exception | Unexpected reader/parser exceptions could abort a research task instead of degrading that source to a read failure. `app/research_agent/authorities.py` logs unexpected failures and reports `read_failed`, allowing already-supported local claims to remain available. |

## Modified files

- Application behavior: `app/indexing.py`, `app/jobs.py`, `app/main.py`, `app/research_agent/authorities.py`, `static/app.js`
- Backup/restore: `scripts/backup.py`, `scripts/restore.py`, new `scripts/archive_utils.py`
- Regression tests: `tests/test_index_cancel_race.py`, `tests/test_research_idempotency.py`, `tests/test_backup_restore.py`, `tests/test_law_review_race.py`, `tests/test_security.py`, `tests/test_prd_regressions.py`
- Test fixture origin configuration: `scripts/test_browser.py`, `scripts/test_ui.py`
- Deployment documentation: `.env.example`, `docs/DEPLOYMENT.md`

No schema migration, deletion of legacy tables, or removal of compatibility APIs was made.

## Regression tests and before/after evidence

- **Indexing:** `tests/test_index_cancel_race.py` deterministically pauses publication/finalization around cancellation. The regression failed against the original implementation in the delegated baseline run; after the fix, both orderings passed, including ten repeated race-test runs.
- **Research idempotency:** `tests/test_research_idempotency.py` simulates an accepted request whose response is lost, verifies terminal recovery by the original request ID, and checks the browser reuses the same ID without duplicate messages or a second mocked provider invocation. The pre-fix lookup returned 404 and the independent audit reproduced duplicate work.
- **Backup/restore:** `tests/test_backup_restore.py` covers empty or database-less archives, missing and invalid keys (including retrieval/search secrets), corrupted ZIP/database, existing destination protection, successful encrypted round-trip, and fault-injected interrupted backup followed by retry. The second audit reproduced false success for an empty archive; the tests now verify rejection without installation or a final partial archive.
- **Law review:** `tests/test_law_review_race.py` deterministically deletes the law between the precheck and update. The baseline returned success; the fixed behavior rejects the update and does not record a successful review.
- **Origin/auth:** `tests/test_security.py` checks Host spoofing, fixed default-origin behavior, and unauthenticated hash-check access.
- **PRD regressions:** `tests/test_prd_regressions.py` adds direct checks for P1-19 early stopping, N4 empty evidence, unexpected parser exceptions at the URL boundary and during acquisition, and preservation of local claims after an official-source read failure.

## Validation results

| Check | Result |
|---|---|
| Full Python suite | `PYTHONPATH=. python -m pytest -q` — **443 passed**, 3 deprecation/user warnings, 43.86s |
| UI fixture and DOM/HTTP suite | `npm run test:ui` — **15 fixture reliability checks and 23 DOM/HTTP checks passed** |
| Chromium browser suites | `scripts/test_browser.py` smoke, agent, hybrid, layout, memory, sources, and core — **7 suites, 82 checks passed** on Chromium 156.0.8078.4 |
| JavaScript syntax | `node --check static/app.js` — passed |
| Python syntax | `python -m py_compile scripts/test_browser.py scripts/test_ui.py scripts/archive_utils.py` — passed |
| Shell syntax | `bash -n start.sh` — passed |
| Whitespace/conflict check | `git diff --check` — passed |

The initial plain `pytest -q` invocation failed during collection because the repository root was not on the import path (`ModuleNotFoundError: app`). Rerunning with `PYTHONPATH=.` completed successfully; this was a test invocation/environment issue, not an application test failure. Chromium first failed at login before the fixture origin was passed to the server process. The test harness was corrected to set `LEXCITE_PUBLIC_URL` before starting the server, after which all seven browser suites passed.

## PRD status updates

- **P1-6 (Origin policy):** fixed the Host-derived fallback; direct Host-variation tests pass. Non-default deployment origins must explicitly set `LEXCITE_PUBLIC_URL`.
- **P1-10 (hash-check access):** unauthenticated access now has direct regression coverage and is rejected.
- **P1-15 (index cancellation):** cancellation/publication transitions are synchronized and the in-flight race has deterministic regression coverage.
- **P1-19 (research early stop):** direct identical-retrieval early-stop regression added and passes.
- **N4 (empty evidence):** direct empty-evidence fixture regression added and passes.
- **R6-E2 (unexpected parser errors):** unexpected official-source parser errors now degrade to `read_failed`; direct error-injection and local-claim-preservation regressions pass.
- **Research recovery/idempotency, restore safety, and law-review/delete consistency:** targeted tests pass for the defects found in the two audits. These cross-cutting fixes do not constitute verification of every unrelated PRD requirement; consult `reports/code_audit.md` and `reports/code_audit_v2.md` for the full requirement matrices and earlier verification limitations.

## Remaining risks and limitations

- Index cancellation cannot interrupt an embedding-provider call already in progress; it takes effect at the next synchronization/checkpoint. No paid provider was called during validation.
- The backup format continues to omit sessions as before. Restore intentionally requires an empty destination; it does not merge an archive into a populated workspace.
- Browser validation used local fixtures and mocked providers. No live model, legal authority, or external paid service was exercised, so external-provider behavior and legal correctness remain outside these results.
- Origin exploitability was not demonstrated; the remediation enforces the PRD's Host-independent policy rather than claiming a confirmed CSRF exploit.
- The Python suite emitted three dependency deprecation/user warnings. They did not fail tests and were not changed as part of this remediation.
