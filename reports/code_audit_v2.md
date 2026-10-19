# Independent Second-Pass Audit

**Date:** 2026-10-18  
**Repository revision:** `54ae375`  
**Compared report:** `reports/code_audit.md`  
**PRD:** `docs/PRD.md`

This pass independently challenged the previous findings using separate bug-reproduction, call-graph, PRD-compliance, and end-to-end workflow investigations. No application code was changed; no paid/external model calls were made. The new report is the only requested artifact.

## 1. Confirmed / Refuted / Unverified Findings

### Previously reported findings

| Finding | Result | Independent evidence and qualification |
|---|---|---|
| **AUD-001: index cancellation can be overwritten** | **CONFIRMED and reproduced** — Medium, high confidence | A deterministic local barrier paused indexing after its cancellation check but before publication. Cancellation wrote `interrupted`; after release, publication wrote a ready vector chunk and final completion overwrote the status to `completed`. Evidence: `app/indexing.py:148-167,192-193,252-259`. The existing regression `tests/test_prd_regressions.py:340-364` cancels before the worker body enters and misses this interleaving. No provider was called; embeddings were stubbed. |
| **AUD-002: browser retry can duplicate research after a lost response** | **CONFIRMED and reproduced** — Medium, high confidence | In a JSDOM/API fixture, the first accepted submission's response was dropped; active-job recovery returned no completed job; a second submission generated a different request ID. Two distinct completed jobs produced four messages. Evidence: `static/app.js:187-189`; `app/jobs.py:48-52,84-97`; async API `app/main.py:270-272`. Existing same-ID tests (`tests/test_agent.py:416-440`; `tests/test_prd_regressions.py:289-318`) do not test the UI's ambiguous-response retry. |
| **AUD-003: Origin fallback depends on Host** | **CONFIRMED requirements gap; exploitability unverified** — Low | With `LEXCITE_PUBLIC_URL` unset, an authenticated synthetic write whose `Origin` matched a chosen `Host` was accepted. `app/main.py:70-77` uses `request.base_url` for the fallback; `tests/test_security.py:218-228` checks only the configured origin and ordinary same-host fallback. This does not establish a browser CSRF exploit: the custom `X-Lexcite` header, SameSite policy, and absence of CORS remain mitigations. |
| **AUD-004: unused imports/helpers** | **PARTIALLY CONFIRMED; prior inventory incomplete** | The first report correctly flagged unused imports and `query_laws`/`embedding_fingerprint`, but deeper module-qualified analysis found additional unreachable helpers and revised the complete list; see §3. No deletion safety was runtime-tested. |

### Newly discovered

| Finding | Result | Evidence |
|---|---|---|
| **AUD2-001: restore reports success when required backup members are absent** | **CONFIRMED and reproduced** — Medium, high confidence | A valid empty ZIP passed to `scripts/restore.py:14-20` printed success while creating neither the database nor key. It conditionally copies each file but does not require them before announcing restoration. A missing database can cause the application to initialize a fresh workspace rather than restore user data. A DB-only archive can also omit the encryption key; `app/llm.py:182-192` creates a replacement key, so stored encrypted settings may become unreadable. No backup/restore tests were found. |
| **AUD2-002: law review may report success after concurrent deletion** | **SUSPECTED race** — Medium, medium confidence | `app/main.py:468-482` checks the law separately, then updates without checking affected rows and returns success; delete is separate at `app/main.py:485-490`. Deletion between the check and update could make the review response/audit claim success for a missing law. No deterministic reproduction or race test was provided. |
| **AUD2-003: failed backup may leave an unusable final archive path** | **SUSPECTED failure-path defect** — Low, medium confidence | `scripts/backup.py:27-32` writes directly to the destination and does not remove a partial archive if ZIP writing fails; the existing-path check at `:18-19` can prevent retrying to that path. No fault-injection reproduction was performed. |

**Refuted:** No prior AUD-001/002/003 issue was refuted. No Critical-severity defect was established. The CSRF exploit interpretation of AUD-003 remains unverified and is not asserted here.

## 2. Newly Discovered Defects

### AUD2-001 — Restore can produce false success

- **Severity / confidence:** Medium / High.
- **Location:** `scripts/restore.py:14-20`; related key behavior `app/llm.py:182-192`.
- **Trigger / impact:** Operator supplies an archive missing `lexcite.sqlite3` and/or `secret.key`. Restore prints “Restored” despite not restoring the required member(s); subsequent startup may create a new empty workspace. If the DB exists but the encryption key does not, a new key can strand previously encrypted settings.
- **Verification:** Local deterministic check with an empty ZIP reproduced the success message and absence of both expected files. No external service used.
- **Recommendation:** Validate required archive members before writing; verify the DB with SQLite integrity checks; require/validate the key when encrypted settings exist; extract to a temporary location and only report success after full validation. Add missing-member, corrupt-ZIP, DB-integrity, and missing-key tests.

### AUD2-002 — Law review/delete race

- **Severity / confidence:** Medium / Medium.
- **Location:** `app/main.py:468-490`.
- **Trigger / impact:** Concurrent deletion after review's existence check but before its update can yield a successful review response for a nonexistent law.
- **Verification:** Static interleaving only; not reproduced.
- **Recommendation:** Make existence/update/audit atomic and require one affected row; add a barrier-controlled race test.

### AUD2-003 — Partial backup archive may block retry

- **Severity / confidence:** Low / Medium.
- **Location:** `scripts/backup.py:18-19,27-32`.
- **Trigger / impact:** Archive creation fails after opening the final path; a partial file can remain and cause a later invocation to refuse that destination.
- **Verification:** Static only; no injected ZIP/disk failure.
- **Recommendation:** Write to a temporary path, validate the completed archive, then atomically replace the final destination; clean up only the specific temporary file on failure.

## 3. Deep Dead-Code Inventory

Call-graph analysis considered application entry points, package exports, FastAPI decorators, framework callbacks, executor callbacks, scripts, UI wiring, and test-only references. These are repository-local reachability conclusions; unknown external consumers cannot be excluded.

### Functions with no production caller

| Symbol | Classification | Evidence / removal qualification |
|---|---|---|
| `app/llm.py:447` — `embedding_fingerprint` | Confirmed unreferenced in application | No first-party production caller; active fingerprinting calls `retrieval_services.fingerprint` from `app/indexing.py:80,100,120`. Its compatibility comment leaves external API compatibility uncertain. |
| `app/llm.py:453` — `embed` | Confirmed unreferenced in application | Active embedding calls `retrieval_services.embed` from `app/research_agent/retrieval.py:284`, `app/indexing.py:175`, and `app/main.py:547`. Same name does not make the wrapper reachable. |
| `app/research_agent/retrieval.py:101` — `query_laws` | Confirmed unreferenced | No executable caller; active ranking invokes `article_bindings` and constructs targets directly. Keep `law_mentions`/`article_bindings`, which are live. |
| `app/research_agent/retrieval.py:341` — `reindex` | Confirmed unreferenced | This wrapper has no caller. The live HTTP handler is `app/main.py:556-557`, which calls `indexing.start(manual=True)`. |
| `app/research_agent/retrieval.py:182` — `cosine` | Test-only reference | Only executable reference is `tests/test_integrity.py:75`; live dense retrieval computes dot products at retrieval lines 286-294. Removing requires replacing the obsolete helper test with active-path coverage. |

### Other unused artifacts

- **Unused constant:** `app/retrieval_services.py:14` `INSTRUCTION`; no reads/imports. Active query-instruction support is separately implemented through `Service.query_instruction` and `embed` at `retrieval_services.py:25,160,210-211`; do not remove that feature based on the unused constant.
- **Suspected obsolete CSS:** `.composer-bottom` (`static/style.css:1,5,36`), `.spinner` (`:1`), and `@keyframes spin` (`:1`) have no first-party HTML/JS class references. Confirm visually before cleanup.
- **Suspected legacy database artifact:** `vectors` table is created in `app/db.py:84-87`, but active code uses `embedding_states`/`embedding_chunks` (`app/indexing.py:21-28,106-108,164-166`). `legacy_vectors_ignored=True` is returned (`db.py:79,96`). Existing DB records/foreign-key behavior and backup compatibility mean this is not permission to drop the table; no data migration recommendation is made without separate compatibility review.

### Unused import bindings

Static AST name-load inspection identified unused bindings: `app/ingest.py:1,9` (`datetime as dt`, `httpx`); `app/llm.py:5` (`hashlib`); `app/main.py:9,11,13-14,22-23` (`re`, `threading`, `Path`, `urlsplit`, `retrieval`, `research_agent`); `app/research_agent/retrieval.py:3` (`json`); `app/retrieval_services.py:9` (`field_validator`). `main.py`'s `context as case_state` is used. These are low-risk cleanup candidates, not dead modules. No cleanup was performed.

### Considered reachable; not dead

- `app/research_agent/__init__.py` exports `answer`, called through `jobs`.
- FastAPI endpoints/middleware, `TextHTML` callbacks in `app/ingest.py:313,319,325`, and `PinnedHTTPSConnection.connect` are framework/transport entry points.
- `app/main.py:259-272` sync and async research routes both converge on `jobs.start`; the sync route is not dead because the UI uses async mode.
- `SettingsInput.embedding_model` is consumed for legacy migration (`app/models.py:108`; `app/retrieval_services.py:132-142`).
- `CoverageAudit` remains a local result contract (`app/research_agent/runner.py:385,402`), not an obsolete model stage.
- `tests/browser_fixture_app.py` exports the ASGI app loaded by Uvicorn; optional reranker routes are rooted at the service Docker entry point.
- No whole first-party module or alternate complete research pipeline was confirmed dead.

Static-analysis limitations: `vulture`, `ruff`, `pyflakes`, and `pylint` were unavailable; nothing was installed. Analysis used AST parsing, repository-wide reference searches, and conservative call-graph checks. It did not prove absence of external imports or operational consumers.

## 4. PRD Implementation, Test, and Behavior Matrix

**Status key:** VERIFIED = implementation and substantive test assertions agree; PARTIAL = implementation or tests have a material gap; MISSING = required capability/acceptance not present; UNVERIFIED = detailed acceptance or execution evidence unavailable. Sections §1–14 and Appendix A contain historical findings; §15 current policy governs conflicts. §7–9 refer to absent v0.5 text, so unprovided acceptance criteria are not inferred.

### Core P0/P1 requirements

| ID | Implementation existence | Test coverage | Behavioral assessment | Status / remaining gap |
|---|---|---|---|---|
| P0-1 | `auth.py:71-120`, `main.py:158-180`, proxy configuration | `test_auth.py:26-108`; deployment checks defined | Matches §15 shared-password/correct-password-priority policy; not the superseded per-account lock criterion | VERIFIED; actual target nginx/container path not run in this second pass |
| P0-2 | `llm.py:113-136,162-205`; retrieval/search settings | `test_model_wire.py:52-99` | Stored/environment key binding and null/empty semantics covered | VERIFIED |
| P0-3 | `runner.py:279-295` | `test_agent.py:118-140` | Repair queries survive query cap | VERIFIED |
| P0-4 | `research_agent/retrieval.py:44-102,234-270`; verification | `test_prd_regressions.py:37-102` | Punctuation/mixed-law/article binding regressions exercised | VERIFIED |
| P0-5 | `ingest.py:44-71`; retrieval/verification normalization | `test_prd_regressions.py:103-110`; import/integrity tests | Supported numeral/suffix forms normalized | VERIFIED for covered forms |
| P0-6 | `runner.py:232-246,302-320` | `test_agent.py:46-68` | New evidence invalidates old approvals before failed reanalysis returns | VERIFIED |
| P1-1 | `main.py:33-40,152-180` | `test_security.py:11-42` | Effective cookie setting/warning includes `0` case | VERIFIED |
| P1-2 | `models.py:12-36,120-160`; all settings loaders revalidate | `test_security.py:51-120` | SSRF address checks and unsafe stored values covered; DNS rebind window remains | VERIFIED with documented boundary |
| P1-3 | `main.py:52-106,139-150` | `test_security.py:126-170` | Authentication, bounded body reads, non-echo validation errors covered | VERIFIED |
| P1-4 | `db.py:20-61`; `llm.py:182-193` | `test_security.py:176-198` | Data/key permissions including existing key correction covered | VERIFIED |
| P1-5 | `auth.py:36-68`; operator guides | `test_security.py:199-217`; deployment script | No code leak and file guidance supported; runtime file mode/deletion assertions reside in deployment checks | PARTIAL: deployment checks not run independently here |
| P1-6 | `main.py:70-77` | `test_security.py:218-230` | Configured URL is fixed; no-config fallback uses Host-derived `request.base_url` | PARTIAL; AUD-003 |
| P1-7 | `static/app.js` escaping/safe URL renderers | browser/UI security tests | Tested unsafe links and rendered content are handled safely | VERIFIED for covered render paths |
| P1-8 | `ingest.py:23-42,100-140`; `indexing.py:62-69` | `test_import_quality.py:14-38,227-250` | Shared heading detection and body protection covered | VERIFIED for tested patterns |
| P1-9 | `ingest.py:73-153,160-197` | `test_import_quality.py:40-60,219-258` | Citation rejection/rescue warnings returned on import | VERIFIED for tested patterns |
| P1-10 | snapshot/hash routes and UI | `test_import_quality.py:67-90,382-387` | Recompute/missing/corrupt snapshot behavior covered | PARTIAL: no direct unauthenticated hash-check test found |
| P1-11 | `ingest.py:200-300`; threaded extraction route | import and PRD regression tests | Tested PDF/DOCX structure and extraction limits work; PDF output cap is not a memory sandbox | VERIFIED with disclosed decompression-memory limitation |
| P1-12 | `authorities.py:224-246` | source/core research tests | Review freshness/jurisdiction/date guards covered with fixtures | VERIFIED |
| P1-13 | `ingest.py:160-197` | `test_import_quality.py:160-180,263-289` | Duplicate and concurrent import reuse covered | VERIFIED |
| P1-14 | `ingest.py:211-239` | `test_import_quality.py:182-198,364-380` | Markdown and TXT handling covered | VERIFIED |
| P1-15 | `indexing.py:148-220,252-259` | `test_prd_regressions.py:340-364` | Queued/pre-worker cancellation works; cancellation during publication can still publish and overwrite terminal status | PARTIAL; AUD-001 |
| P1-16 | `jobs.py` event cancellation; `llm.py` checks | `test_prd_regressions.py:320-339`; model-wire tests | Hot cancellation check avoids DB access in tested streaming loop | VERIFIED |
| P1-17 | SQL filtering and retrieval caches | `test_prd_regressions.py:113-168` | Cache invalidation/reuse and 5k performance threshold exercised by full suite | VERIFIED; no baseline read-volume comparison |
| P1-18 | `llm.py` retries/budgets; runner | model-wire and PRD regression tests | Retry-After, request budget, cancellation, and response bounds covered with stubs | VERIFIED; no provider behavior claimed |
| P1-19 | `runner.py:302-320` | No direct assertion identified for identical retrieve evidence triggering early stop | Implementation comparison exists; test suite checks adjacent repair paths but not this claim directly | PARTIAL: add focused regression |
| P1-20 | `runner.py:139-150` | `test_prd_regressions.py:194-205` | Server availability check prevents false missing-authority gap | VERIFIED |

### §14 additions and engineering criteria

| IDs | Implementation/test/behavior result | Status / gap |
|---|---|---|
| §14.1 S1–S3 | Proxy trust, loopback recovery, Uvicorn proxy-header behavior implemented; auth tests cover spoofed XFF/recovery. | VERIFIED in code/tests; real nginx deployment not independently verified |
| §14.1 S4 | `.env.example` and Compose expose trust setting. | PARTIAL: no dedicated assertion that documentation remains discoverable |
| §14.1 S5–S14 | Endpoint-bound secrets, disabled service validation, matching, request caps, nonblocking throttle, bounded attempts, CGNAT checks, stored-setting revalidation, escaping and service allowlists have implementation/tests. | VERIFIED for tested paths; real service inference not covered |
| R6-P1/P8 | Parse-only normalization handles BOM/zero-width/Unicode separators; tests assert source preservation. | VERIFIED |
| R6-P2–P8 | Citation/title heuristics, headings, shared indexing metadata, warnings and line normalization implemented/tested. | VERIFIED for listed regression fixtures |
| R6-P9/P10 | Recursive DOCX extraction and Markdown normalization implemented with fixtures. | VERIFIED for covered DOCX structures/marker forms, not all producers |
| R6-P11/P12 | Duplicate import handling and article numeral normalization implemented/tested. | VERIFIED for covered cases |
| R6-P13 | Character/stream output checks implemented. | PARTIAL on memory guarantee: decompression occurs before aggregate cap; disclosed, no memory sandbox |
| R6-E1 | Fact extraction/current focus safeguards | VERIFIED with PRD regression |
| R6-E2 | Known attachment parse errors are converted; source caller catches narrow exception types | PARTIAL: unexpected parser exception may abort study; no injected unexpected-exception test |
| R6-E3–E5 | Cross-law binding, sync ask locking, evidence-budget omissions | VERIFIED in code and targeted PRD tests |
| R6-E6/E7/E9/E10 | Boot visibility, finite polling/recovery and stale-case protection | VERIFIED for DOM/HTTP fixtures; no second-pass Chromium execution |
| R6-E8 | Settings use task/model lock | VERIFIED with regression asserting updates rejected during research |
| R6-E11 | Fixed Civil Code source delegates to shared pinned public-address-checking reader | VERIFIED in code; no new live-network assertion |
| R6-E12/E13/E14 | Model response bounds, early response headers, setup-code instructions | VERIFIED for tested local paths; deployment guide claims not all independently executed |
| N1 | Model-driven obsolete coverage stage removed; local `CoverageAudit` result remains live | VERIFIED by code path; distinct from dead helper functions in §3 |
| N2 | Historical regressions present | VERIFIED; full pytest independently passed |
| N3 | Source timing skipped when no source added | VERIFIED by PRD regression test |
| N4 | Fixture uses `next(..., None)` for empty evidence | PARTIAL: no direct empty-evidence branch test found |
| N5 | Wrong-stage schema response rejected | VERIFIED with regression |

### Deferred scope and release gates

| ID | Scope / evidence | Status |
|---|---|---|
| P2-1 / §12 gate 4 | No completed lawyer-labeled holdout evaluation reported | **MISSING release evidence** |
| P2-2 through P2-10 | Deferred; detailed acceptance refers to absent v0.5 content | **UNVERIFIED**, not inferred from feature names |
| P3-1 through P3-5 | Explicit independent/deferred milestone; shared workspace boundary documented | **UNVERIFIED/deferred** |
| §12 gate 3 | Real vendor chat/embedding/reranker, Ollama, weights, target network/certificate checks | **MISSING/unverified** in this pass |
| §12 gate 5 | Negative security test assertions exist, but target deployment run was not completed in this pass | **PARTIAL** |
| NFR compatibility | Single worker and additive migrations exist; Windows and complete old-schema migration compatibility not established | **PARTIAL** |

## 5. Missing Tests and Verification Gaps

1. **Index cancellation:** test during an active publish transaction and after the final publish; assert status and embedding rows agree. Current test covers only pre-worker cancellation.
2. **Research ambiguous response:** lose the POST response after acceptance/completion, retry through the UI, and assert one request ID/job/result/provider invocation.
3. **Origin policy:** with `LEXCITE_PUBLIC_URL` unset, vary `Host` independently of allowed Origin and test expected rejection. Do not describe this alone as a CSRF exploit.
4. **Restore integrity:** reject archives missing DB/key, corrupt ZIP/member data, invalid SQLite DB, and DB/key mismatch; assert no success-shaped response or partial final install.
5. **Backup failure atomicity:** inject write failure and verify retry to the same final destination works without leaving a partial archive.
6. **Law review/delete:** deterministic interleaving test to prove review cannot succeed after deletion.
7. **PRD coverage gaps:** add direct tests for P1-19 early stop, N4 empty evidence, R6-E2 unexpected parser exceptions, and P1-10 unauthenticated hash-check access.
8. **Resource safety:** characterize compressed PDF peak memory or run extraction in a bounded subprocess; a character cap after decompression is not a memory bound.
9. **Deployment:** rerun `scripts/test_deployment.py` with its required `lexcite:deployment-test` image available; verify real reverse proxy/Compose behavior. No paid model calls are needed for these deployment tests.
10. **External/model/legal gates:** real vendor/Ollama inference and legal holdout remain unverified; perform only with explicit approval and appropriate non-production credentials/corpus.

## 6. Coverage and Execution Evidence

### Executed in this second pass

- Independent deterministic reproductions of AUD-001 and AUD-002 using local barriers/stubs/JSDOM fixture; no external providers.
- AUD-003 synthetic authenticated HTTP request with configured origin unset; accepted matching Host/Origin.
- Empty-ZIP restore reproduction; success message with no restored DB/key.
- Targeted local pytest investigation reported **221 passed** across six targeted files; separate mocked DOM run reported **15 passed**. These are targeted results, not the full suite.
- Independent PRD pass ran `python -m pytest -q`: **416 passed**.
- Independent PRD pass ran `npm run test:ui`: **15 DOM reliability and 23 DOM/HTTP checks passed**.
- Independent deployment check attempted `python scripts/test_deployment.py`; **blocked before checks** because the required `lexcite:deployment-test` image was unavailable and Docker could not find it.

### Previously executed and independently not repeated

The first audit reported seven Chromium suites / 82 checks passing and JavaScript/Bash syntax checks passing. The second-pass PRD agent did not independently rerun Chromium; the deployment script was blocked. Treat the earlier browser result as first-pass evidence, not second-pass re-verification.

### Coverage inventory / exclusions

First-party app, UI, tests, scripts, deployment configuration and complete PRD were assigned to explicit parallel workstreams. The static dead-code investigator separately analyzed **48 tracked Python files** plus first-party JS/HTML/CSS and deployment entry points. Vendor code, generated artifacts, caches, SQLite corpus contents, and binary legal documents were excluded. Real providers, paid calls, legal accuracy, Windows runtime, production-volume load, and multi-process deployment were not executed. No full runtime coverage instrumentation was collected; this is a documented scope inventory, not a claim of 100% code coverage.

## 7. Comparison with the First Audit

- **Confirmed:** AUD-001, AUD-002, and AUD-003 were independently reproduced/verified. AUD-003 remains a requirements gap without demonstrated browser exploitability.
- **Expanded:** Dead-code list was incomplete. Confirmed unreferenced wrappers include `llm.embed` and `retrieval.reindex`; `retrieval.cosine` is test-only; unused import list also includes `main.py` imports `retrieval`/`research_agent`. Framework callbacks, package exports, active routes, and separate `retrieval_services.embed` remain reachable.
- **New defect:** Restore's success-shaped result for an archive with no DB/key (AUD2-001) was deterministically reproduced. The first audit did not include backup/restore behavior in its defects.
- **Additional suspected cases:** law review/delete race and partial backup output were not reproduced; they remain explicitly suspected, not confirmed.
- **PRD status corrections:** P1-15 is **PARTIAL**, not verified, because the current race survives. P1-19 has an implementation path but lacks direct early-stop test evidence. P1-5 runtime mode/deletion checks depend on deployment tests that could not run. R6-E2 is partial for unexpected parser failures; N4 lacks a direct empty-evidence test.
- **Validation reconciliation:** This pass independently confirms 416 pytest and `npm run test:ui`; deployment checks were blocked. The previous 82 Chromium checks were not rerun independently. §15's 2026-10-19 date is after the audit date 2026-10-18; its future-dated validation statements are not treated as independently established evidence.

## 8. Prioritized Remediation Recommendations

1. **AUD-001 — Make cancellation and index publication/finalization atomic.** Recheck active job state inside the publication transaction and prevent any terminal-state overwrite; cover in-flight and last-batch races.
2. **AUD2-001 — Validate restore archives before success or install.** Require the database and relevant encryption key, validate SQLite integrity and archive members, and avoid partial installation.
3. **AUD-002 — Preserve idempotency across ambiguous browser submissions.** Reuse the original request ID and retrieve terminal job status/result before offering a fresh submission.
4. **AUD-003 — Remove Host-derived origin fallback where strict Host independence is required.** Use an explicit trusted origin policy and test Host/Origin independently.
5. **Add atomicity/failure-path tests** for law-review/delete and backup-write failure; investigate before calling these suspected issues defects.
6. **Close direct-test gaps** for P1-19, N4, R6-E2, and unauthenticated hash-check access.
7. **Review confirmed dead helpers/imports for cleanup** only after checking external compatibility; do not remove persisted `vectors` table or public compatibility wrappers without migration/API review.
8. **Complete release evidence**: rerun isolated deployment checks with the required image, perform approved real-service/Ollama smoke tests, and complete lawyer-labeled evaluation before making production/legal-accuracy claims.

**No application source was changed.** The investigations did not establish Critical severity or complete production/legal validation.
