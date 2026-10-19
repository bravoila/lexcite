# Repository Audit: LexCite against `docs/PRD.md`

**Audit date:** 2026-10-18  
**Audited revision:** `54ae375` (clean worktree)  
**Scope:** First-party application, tests, scripts, deployment/configuration, static UI, and PRD/docs. Excluded `node_modules`, caches, generated `.archify` artifacts, and data/legal-source binaries. No application code was changed and no dependencies were installed.

## 1. Executive Summary

The implementation substantially covers the current 1.4.0 P0/P1 requirements. The executed Python, UI, browser, and syntax checks passed. No Critical or High implementation defect was confirmed in the audit. Two Medium integration/state defects remain actionable:

1. **Index cancellation can lose a race with vector publication or final completion.** A cancellation can mark the job interrupted after the worker’s last cancellation check; the worker can then publish vectors and overwrite the terminal status with `completed`.
2. **A lost research-submission response can lead the browser to submit a duplicate paid research job.** The UI generates a new `request_id` for each submit and only tries to recover active jobs; completed jobs are absent from that lookup.

The PRD matrix also identifies a **partial Origin allowlist requirement**: without `LEXCITE_PUBLIC_URL`, the fallback allowlist is derived from the request Host. This is a requirements gap; the audit did not establish a browser-exploitable CSRF path because writes also require the custom `X-Lexcite` header and no CORS allowance was found.

No separate application build, lint, or type-check command is defined. Real vendor/Ollama inference, lawyer-reviewed accuracy evaluation, and Windows startup remain unverified. The PRD’s §15 implementation record is dated 2026-10-19, one day after this audit date; its assertions were treated as documentation, not proof. The audit’s test results below are from commands executed during this audit.

## 2. Repository Audit Coverage

| Area | Inspected |
|---|---|
| Product requirements | Complete `docs/PRD.md` (including §15 and Appendix A), with relevant architecture and verification documentation |
| Runtime/application | `app/` API, auth, persistence, ingestion, indexing, retrieval, model/search integrations, research agent, and task orchestration |
| User interface | `static/` browser application and API call/recovery flows |
| Validation and deployment | Python/Node manifests, tests, scripts, Docker/Compose/startup configuration |
| Exclusions | Third-party dependencies, caches, generated artifacts, SQLite corpus contents, binary legal/source documents |

The PRD names an older baseline (`f2f002c`); the audited worktree was at `54ae375`. Several PRD section references point to historical v0.5 text that is not present in the repository. Those requirements are marked **UNVERIFIED**, not inferred. The detailed §14 issue IDs are qualified by section because identifiers such as `R6-S8` are reused in different parts of the PRD.

## 3. Critical / High / Medium / Low Findings

### Medium

#### AUD-001 — Index cancellation can be overwritten by completion

- **Severity:** Medium
- **Confidence:** High
- **File and lines:** `app/indexing.py:148-167,192-193,252-259`
- **Description / impact:** `publish()` checks cancellation before entering its database transaction, but it does not recheck cancellation or confirm that the job remains active inside the transaction. `cancel()` immediately writes `interrupted`. If cancellation lands after the worker’s check, `publish()` can still insert embedding rows and update counts; after the final batch, `build()` unconditionally writes `completed`. The reported state can therefore contradict the cancellation and indexed data can be published after cancellation.
- **Evidence / verification:** Static interleaving of `publish()`, `cancel()`, and the unconditional completion update. Existing `tests/test_prd_regressions.py:340-364` exercises cancellation before the worker enters its body, not cancellation during publication or between the final publish and completion.
- **Recommended fix:** Serialize cancellation and publication/completion using the same lock or transactional state check. Do not publish or transition to `completed` unless the job is still active; add deterministic barrier-based tests for cancellation immediately before publish, during publish, and after the final publish.

#### AUD-002 — Browser retry can duplicate completed research

- **Severity:** Medium
- **Confidence:** High
- **File and lines:** `static/app.js:187-189`; `app/jobs.py:48-52,84-97`
- **Description / impact:** Each browser submit creates a fresh `request_id`. If the server accepts and completes a job but the POST response is lost, the UI’s catch path searches only active jobs. `jobs.active()` returns only `running`/`cancel_requested`; a completed job is not recovered. A later user retry therefore gets a new request ID and can run the same model work again and append a duplicate message, including repeat provider cost.
- **Evidence / verification:** The code path is confirmed statically. Existing idempotency tests (`tests/test_agent.py:426-445`; `tests/test_prd_regressions.py:289-318`) resubmit the same request ID and verify deduplication, but do not model a lost response followed by a fresh browser submission.
- **Recommended fix:** Retain the request ID and payload across submission failure/retry. Provide terminal-job lookup/replay by `(case_id, request_id)` or otherwise resolve the original request before allowing a new one. Add a browser/API regression where completion succeeds but the initial response is lost.

### Low

#### AUD-003 — Origin fallback depends on the Host header

- **Severity:** Low (PRD compliance gap; no exploit confirmed)
- **Confidence:** High for the implementation gap; low for exploitability
- **File and lines:** `app/main.py:70-77`; `tests/test_security.py:218-230`
- **Description / impact:** If `LEXCITE_PUBLIC_URL` is unset, the middleware constructs its allowed Origin from `request.base_url`, which is derived from the request Host. This does not meet the literal P1-6 requirement that the allowlist not depend on Host. Existing tests show configured-origin rejection and same-Host fallback, not Host-independent behavior.
- **Evidence / verification:** Direct code inspection. Writes also require `X-Lexcite: 1`, and no CORS allowance was found, so this audit does not claim a confirmed browser CSRF bypass.
- **Recommended fix:** Use a fixed configured public origin for deployments that accept writes, or derive the allowed origin from a trusted, explicit server setting rather than the request Host. Add a test that varies Host while keeping the allowed Origin fixed.

#### AUD-004 — Unused imports and unreferenced compatibility/helpers

- **Severity:** Low
- **Confidence:** High for listed unused imports and internal unreachability; Medium for public API safety
- **File and lines:** `app/ingest.py:1,9`; `app/llm.py:5,447-450`; `app/retrieval_services.py:9`; `app/main.py:9,11,13-14`; `app/research_agent/retrieval.py:3,101-102`
- **Description / impact:** Static reference checks found imports that are unused in their defining module (`datetime as dt` and `httpx` in `ingest.py`; `hashlib` in `llm.py`; `field_validator` in `retrieval_services.py`; `re`, `threading`, `Path`, and `urlsplit` in `main.py`; `json` in research retrieval). `query_laws()` has no repository caller; `embedding_fingerprint()` is a compatibility wrapper with no repository caller. These add maintenance noise, but no runtime failure was established.
- **Evidence / verification:** First-party source reference search, with framework routes and dynamic entry points considered. No automated dead-code analyzer is configured.
- **Recommended fix:** Remove only the confirmed unused imports. Before deleting `query_laws()` or `embedding_fingerprint()`, confirm they are not supported external import APIs; otherwise retain and document their compatibility purpose.

**No Critical or High findings were confirmed.** The historical high-severity PRD findings were checked against the current implementation and regression assertions; see the traceability matrix. The index race is a newly verified Medium defect, despite PRD P1-15 being described as fixed.

## 4. PRD Traceability Matrix

Status meanings: **VERIFIED** = implementation and relevant evidence support the requirement; **PARTIAL** = a material gap remains; **MISSING** = required capability/evidence is absent; **UNVERIFIED** = acceptance details or execution evidence are insufficient. Test references indicate assertions; separately executed commands are listed in §7.

### Core P0/P1 requirements

| Requirement ID | Requirement | Implementation | Test evidence | Status | Gap |
|---|---|---|---|---|---|
| P0-1 | Shared-password policy: anonymous failures do not lock out correct-password users; proxy trust/recovery are explicit | `app/auth.py:71-120`; `app/main.py:158-180`; deployment docs | `tests/test_auth.py:26-103`; `scripts/test_deployment.py:173-208` | VERIFIED | §15 explicitly supersedes per-account lockout; app has no individual accounts |
| P0-2 | Bind stored/environment keys to endpoint; omitted/null preserves only same endpoint, empty clears | `app/llm.py:113-136,162-205`; retrieval/search settings modules | `tests/test_model_wire.py:52-99` | VERIFIED | No gap found |
| P0-3 | Preserve repair-round queries during query deduplication/truncation | `app/research_agent/runner.py:279-295` | `tests/test_agent.py:118-140` | VERIFIED | No gap found |
| P0-4 | Bind each exact article number to the nearest named law | `app/research_agent/retrieval.py:44-102,234-270`; `verification.py:7-45` | `tests/test_prd_regressions.py:37-102` | VERIFIED | No gap found in covered punctuation/mixed-law cases |
| P0-5 | Normalize law/article identity consistently | `app/ingest.py:44-71`; retrieval and verification modules | `tests/test_prd_regressions.py:103-110`; `tests/test_import_quality.py:374-380`; `tests/test_integrity.py:109-145` | VERIFIED | No material gap found |
| P0-6 | New evidence invalidates old conclusions before failed reanalysis can return them | `app/research_agent/runner.py:232-246,302-320` | `tests/test_agent.py:46-68` | VERIFIED | No gap found |
| P1-1 | Cookie Secure behavior and warning match effective configuration | `app/main.py:33-40,152-180` | `tests/test_security.py:11-42` | VERIFIED | Includes `"0"` configuration case |
| P1-2 | Prevent SSRF in model/search/retrieval endpoints, including stored settings | `app/models.py:12-36,120-160`; settings loaders in `llm.py`, `retrieval_services.py`, `websearch.py` | `tests/test_security.py:51-120`; deployment checks | VERIFIED | DNS validation/connect timing remains documented |
| P1-3 | Bound unauthenticated and upload bodies before buffering; avoid echoing submitted inputs | `app/main.py:52-106,139-150,418-439` | `tests/test_security.py:135-170` | VERIFIED | No gap found in tested 16 KB login/setup and 20 MB extract caps |
| P1-4 | Restrict data/DB/WAL/SHM/secret-key permissions | `app/db.py:20-61`; `app/llm.py:182-193` | `tests/test_security.py:176-198` | VERIFIED | No gap found |
| P1-5 | Do not print setup code; document protected file retrieval | `app/auth.py:36-68`; README/deployment/start guides | `tests/test_security.py:199-217`; `tests/browser_agent.cjs:32` | VERIFIED | No gap found in inspected guides |
| P1-6 | Origin allowlist must not depend on Host | `app/main.py:70-77` | `tests/test_security.py:218-230` | PARTIAL | Unconfigured fallback uses `request.base_url`; see AUD-003 |
| P1-7 | Restrict rendered links to HTTP(S) and escape server values | `static/app.js:18,79-83,105,133` and renderers | `tests/browser_sources.cjs:28`; `tests/ui_reliability.cjs:156-185` | VERIFIED | No tested render path gap found |
| P1-8 | Remove chapter headings from article bodies and preserve consistent heading paths | `app/ingest.py:23-42,100-140`; `app/indexing.py:62-69` | `tests/test_import_quality.py:14-38,227-250` | VERIFIED | No gap found in covered cases |
| P1-9 | Avoid fake articles from cross-reference lines; surface warnings in imports | `app/ingest.py:73-153,160-197` | `tests/test_import_quality.py:40-60,219-258` | VERIFIED | No gap found in covered cases |
| P1-10 | Audit SHA256 against submitted source and explain corrupt snapshots | `app/indexing.py:16-20`; `app/main.py:385-401` | `tests/test_import_quality.py:67-90,382-387` | VERIFIED | No material gap found |
| P1-11 | Extract PDF/DOCX without silent loss or event-loop blocking; bound extraction | `app/ingest.py:200-293`; `app/main.py:418-439` | `tests/test_import_quality.py:121-158,291-360`; `tests/test_prd_regressions.py:270-287` | VERIFIED | PDF character cap is not a decode-time memory sandbox |
| P1-12 | Apply jurisdiction/date/review freshness to online-source blocking | `app/research_agent/authorities.py:224-239` | `tests/test_source_resolution.py:89-105`; `tests/test_core_research.py:243-255` | VERIFIED | No material gap found |
| P1-13 | Reuse duplicates and resolve concurrent import identity safely | `app/ingest.py:160-197` | `tests/test_import_quality.py:160-180,263-289` | VERIFIED | No gap found |
| P1-14 | Strip Markdown syntax before splitting without changing TXT source | `app/ingest.py:211-239` | `tests/test_import_quality.py:182-198,364-380` | VERIFIED | No gap found in covered forms |
| P1-15 | Index cancellation reaches a consistent terminal state | `app/indexing.py:148-167,192-220,252-259` | `tests/test_prd_regressions.py:340-364` | PARTIAL | Test covers cancellation only before worker entry; publish/completion race remains (AUD-001) |
| P1-16 | Avoid a database read for each streaming cancellation check | `app/jobs.py:137-160`; `app/llm.py:217-260` | `tests/test_prd_regressions.py:320-339`; `tests/test_model_wire.py:157-195` | VERIFIED | No gap found |
| P1-17 | Push filtering into SQL and bound/cache retrieval work | `app/research_agent/retrieval.py:157-230`; `app/indexing.py:99-117` | `tests/test_prd_regressions.py:113-168` | VERIFIED | Performance assertion exists; measured by current test run, see §7 |
| P1-18 | Retry eligible 429/5xx with bounded time/request budget and cancellation | `app/llm.py:217-390,404-445`; `runner.py:80-110` | `tests/test_model_wire.py:102-195`; `tests/test_prd_regressions.py:366-412` | VERIFIED | No gap found in tested behavior |
| P1-19 | Stop when retrieval yields no new evidence without discarding verified results | `app/research_agent/runner.py:302-320` | `tests/test_agent.py:227-250` | VERIFIED | No material gap found |
| P1-20 | Server, not model, decides whether a missing law is locally unavailable | `app/research_agent/runner.py:139-150`; retrieval availability checks | `tests/test_prd_regressions.py:194-205` | VERIFIED | No gap found |

### §14.1 Security/deployment additions

| Requirement ID | Requirement | Implementation / test evidence | Status | Gap |
|---|---|---|---|---|
| §14.1 S1/S2 | Prevent proxy loopback exemption and anonymous lockout under shared-password policy | `auth.py:71-120`, `main.py:158-180`; `test_auth.py:26-103`; `scripts/test_deployment.py:173-208` | VERIFIED | §15’s correct-password-priority policy supersedes individual-account lockout |
| §14.1 S3 | Disable implicit Uvicorn proxy-header parsing | `Dockerfile:16-18`; `scripts/test_deployment.py:203-208` | VERIFIED | None found |
| §14.1 S4 | Make `LEXCITE_TRUST_PROXY` discoverable | `.env.example:11`; `compose.yaml:17` | PARTIAL | Variable is present; no dedicated assertion validates guide discoverability |
| §14.1 S5 | Bind environment keys to endpoint | `llm.py:131-136,195-205`; `test_model_wire.py:67-84` | VERIFIED | Also covered by P0-2 |
| §14.1 S6 | Do not validate unused disabled reranker endpoint; allow documented startup | `retrieval_services.py:17-34,56-77`; `test_security.py:86-94`; `scripts/test_deployment.py:131-136` | VERIFIED | Real model weights not exercised |
| §14.1 S7 | Punctuation does not bypass law/article binding | `retrieval.py:44-102,234-270`; `test_prd_regressions.py:37-60` | VERIFIED | Also covered by P0-4 |
| §14.1 S8 | Bound login/setup bodies and do not echo input | `main.py:62-106`; `test_security.py:148-174` | VERIFIED | None found |
| §14.1 S9/S10 | Nonblocking global throttling and bounded attempt-address state | `auth.py:11-15,81-120`; `test_auth.py:78-100` | VERIFIED | None found |
| §14.1 S11 | Reject CGNAT including IPv4-mapped IPv6 | `models.py:120-143`; `test_security.py:51-57` | VERIFIED | None found in covered cases |
| §14.1 S12 | Revalidate stored model/retrieval/search settings | `llm.py:113-129`; `retrieval_services.py:56-77`; `websearch.py:10-24`; `test_security.py:96-120` | VERIFIED | None found |
| §14.1 S13 | Escape server-provided frontend values | `static/app.js:18,79-83,105,133`; `tests/ui_reliability.cjs:156-185` | VERIFIED | Tested render paths covered |
| §14.1 S14 | Accept specifically documented local service names/ports | `models.py:9-10,120-143`; `docs/DEPLOYMENT.md:114`; `test_security.py:67-73` | VERIFIED | None found |

### §14.2 Import/parsing additions

| Requirement ID | Requirement | Implementation / test evidence | Status | Gap |
|---|---|---|---|---|
| R6-P1/P8 | Normalize BOM/zero-width and Unicode line separators in parse-only copy | `ingest.py:23-30,91-99`; `test_import_quality.py:201-218` | VERIFIED | Original-source snapshot remains distinct |
| R6-P2 | Do not fabricate articles from wrapped citations | `ingest.py:73-120`; `test_import_quality.py:219-225` | VERIFIED | None found in tested pattern |
| R6-P3/P4 | Recognize valid structural headings without deleting body fragments | `ingest.py:32-42,130-140`; `test_import_quality.py:227-242` | VERIFIED | None found in tested cases |
| R6-P5/P12 | Normalize full-width/spaced numerals and reject invalid suffix forms | `ingest.py:12-16,44-71`; `test_import_quality.py:201-205,374-380` | VERIFIED | None found in tested cases |
| R6-P6 | Share heading analysis between parsing and indexing | `ingest.py:32-42,138-140`; `indexing.py:62-69`; `test_import_quality.py:227-250` | VERIFIED | None found |
| R6-P7 | Return/audit import ambiguity warnings | `ingest.py:160-197`; `test_import_quality.py:244-258` | VERIFIED | None found |
| R6-P9 | Recursively extract DOCX structures and deduplicate merged cells | `ingest.py:250-293`; `test_import_quality.py:291-327` | VERIFIED | Fixture coverage is not exhaustive of all DOCX producers |
| R6-P10 | Strip nested Markdown markers while preserving TXT | `ingest.py:211-239`; `test_import_quality.py:364-380` | VERIFIED | None found in tested forms |
| R6-P11 | Detect duplicates before parsing and handle concurrent imports atomically | `ingest.py:160-197`; `test_import_quality.py:263-289` | VERIFIED | None found |
| R6-P13 | Bound PDF extracted output | `ingest.py:20,238-250`; `test_import_quality.py:353-361` | VERIFIED | Output cap does not isolate decompression memory use |

### §14.3 Research-engine/frontend additions

| Requirement ID | Requirement | Implementation / test evidence | Status | Gap |
|---|---|---|---|---|
| R6-E1 | Avoid extracting answers from question/instruction fragments; retain current focus | `context.py:34-72,98-149`; `test_prd_regressions.py:170-185` | VERIFIED | None found in tested cases |
| R6-E2 | Convert corrupt official attachments to source-read failures | `ingest.py:200-209`; `sources.py:139-168`; `authorities.py:238-246`; `test_prd_regressions.py:251-287` | VERIFIED | None found in tested cases |
| R6-E3 | Prevent cross-law article-number matches | `retrieval.py:87-102,234-270`; `test_prd_regressions.py:37-60` | VERIFIED | Also covered by P0-4 |
| R6-E4 | Route sync `/ask` through task locking/idempotency | `main.py:259-270`; `jobs.py:67-160`; `test_prd_regressions.py:289-318` | VERIFIED | Duplicate browser retry with a new ID remains (AUD-002) |
| R6-E5 | Report requested exact provisions omitted by evidence budget | `runner.py:147-181`; `test_prd_regressions.py:208-249` | VERIFIED | None found |
| R6-E6 | Keep startup failures visible until data load succeeds | `static/app.js:56`; `tests/ui_reliability.cjs:40-72` | VERIFIED | None found in tested scenarios |
| R6-E7/E10 | Bound polling/recovery retries and release UI on terminal states | `static/app.js:142-184`; `tests/ui_reliability.cjs:96-154` | VERIFIED | None found in tested scenarios |
| R6-E8 | Lock settings changes against active research/indexing | `main.py:295-305,498-532`; `jobs.py:75-82`; PRD regression tests | VERIFIED | None found |
| R6-E9 | Ignore stale case-load responses | `static/app.js:60-72`; `tests/ui_reliability.cjs:74-94` | VERIFIED | None found in tested scenarios |
| R6-E11 | Use pinned/public-checked fetch path for fixed Civil Code source | `ingest.py:333-350`; `sources.py:18-100`; `test_core_research.py:106-116` | PARTIAL | No specific regression assertion for proxy-environment independence or DNS-rebinding behavior |
| R6-E12 | Bound non-streaming model response and support cancellation | `llm.py:217-260,313-390`; `test_model_wire.py:145-195` | VERIFIED | None found in tested cases |
| R6-E13 | Apply security/cache headers to early errors | `main.py:52-106`; `test_security.py:158-174` | VERIFIED | None found |
| R6-E14 | Document setup-code file rather than logs | README/deployment/start scripts and HTML guides; `tests/browser_agent.cjs:32` | VERIFIED | None found in inspected guides |

### Engineering, deferred requirements, nonfunctional criteria, and release gates

| Requirement ID | Requirement | Evidence | Status | Gap |
|---|---|---|---|---|
| N1 | Remove obsolete CoverageAudit paths while retaining compatibility where needed | `runner.py:80-82,380-402`; workflow/source tests | PARTIAL | No direct regression asserts obsolete path remains absent |
| N2 | Preserve regression coverage for earlier P0 fixes | Auth/model/import/PRD regression suites | VERIFIED | Test execution results in §7 |
| N3 | Mark source timing skipped when no new source is added | `runner.py:232-246`; `test_prd_regressions.py:251-268` | VERIFIED | None found |
| N4 | Empty browser-fixture evidence does not raise `StopIteration` | `tests/browser_fixture_app.py:66-70` | PARTIAL | Fixed code; no located test exercises empty-evidence branch |
| N5 | Reject wrong-stage model JSON instead of silently accepting an empty Draft | `schemas.py:69-76`; `test_prd_regressions.py:188-192` | VERIFIED | None found |
| NFR performance | Reduce retrieval work; 5,000-article cold retrieval under 2 seconds | Retrieval caching/SQL; `test_prd_regressions.py:113-168` | PARTIAL | Timing test ran in full pytest, but no baseline read-volume comparison assertion |
| NFR security | Close security rework and cover regressions | P0/P1 and §14.1 evidence above | PARTIAL | P1-6 Host-derived fallback remains |
| NFR observability | Call-level telemetry; no permanent intermediate index state | `runner.py:80-126`; `indexing.py:197-260` | PARTIAL | P1-15 cancellation race; full P2-2 criteria missing |
| NFR compatibility | Python 3.12, one worker, additive SQLite migrations | `Dockerfile:18`; `jobs.py:19-30`; `db.py` migrations | PARTIAL | No complete migration-compatibility or Windows runtime test |
| NFR documentation | Deployment security, setup instructions, version and upgrade docs | deployment/README/upgrade docs | VERIFIED | None found in inspected paths |
| Release gate 1 | Regression suite passes | Executed commands in §7 | VERIFIED | No legal accuracy inference |
| Release gate 2 | Close all P0/P1 rework with tests | Core matrix above | PARTIAL | P1-6 partial and P1-15 race |
| Release gate 3 | Real vendor/Ollama/weights, reranker composition and target reverse-proxy smoke | Verification docs disclose outstanding real model/target checks | MISSING | No actual vendor/Ollama inference or target unit validation |
| Release gate 4 / P2-1 | Lawyer-labeled legal holdout evaluation and recorded metrics | Evaluation scripts exist; no completed labeled holdout reported | MISSING | No completed legal-personnel evaluation or CI gate |
| Release gate 5 | Negative SSRF, lockout/DoS, key-leak, permissions tests | Security/auth/model tests and deployment assertions | PARTIAL | Assertions exist and general suite passed; target deployment checks not independently run here |
| P2-2 | Call-level telemetry | Some runner telemetry; v0.5 acceptance text absent | UNVERIFIED | Detailed acceptance criteria unavailable |
| P2-3 | Retrieval quality enhancement | Retrieval/evaluation code exists | UNVERIFIED | Detailed v0.5 criteria absent; no quality result establishes acceptance |
| P2-4 | Task recovery | `jobs.py:19-58`; UI recovery; reliability tests | UNVERIFIED | Full v0.5 acceptance absent; lost-response/new-ID gap in AUD-002 |
| P2-5 | Backup/archive verification | `scripts/backup.py`, `scripts/restore.py` | UNVERIFIED | Detailed v0.5 criteria absent |
| P2-6 | Batch import/progress | No complete implementation trace established | UNVERIFIED / deferred | Detailed criteria absent |
| P2-7 | Law-change tracking | No complete implementation trace established | UNVERIFIED / deferred | Detailed criteria absent |
| P2-8 | Jurisdiction hierarchy | Jurisdiction currently represented as string (`models.py:38-57`) | UNVERIFIED / deferred | Detailed criteria absent |
| P2-9 | Review-expiration reminders | Review freshness filtering exists; no reminder workflow established | UNVERIFIED / deferred | Detailed criteria absent |
| P2-10 | Configurable retrieval budget | Settings exist in `retrieval_services.py:39-53` | UNVERIFIED | Detailed criteria absent |
| P3-1/P3-2 | Users/roles and case permissions | Shared workspace/password; deployment docs describe boundary | UNVERIFIED / deferred | Independent milestone; detailed v0.5 criteria absent |
| P3-3/P3-4 | Audit ownership and authentication enhancement | Shared workspace audit/auth implementation | UNVERIFIED / deferred | Independent milestone; detailed v0.5 criteria absent |
| P3-5 | Storage encryption | Password/session and encrypted model secrets exist; no field-level case encryption | UNVERIFIED / deferred | Independent milestone; data-at-rest boundary disclosed |

## 5. Confirmed and Suspected Bugs

| Finding | Classification | Severity / confidence | Trigger and impact | Location / evidence |
|---|---|---|---|---|
| AUD-001 | Confirmed | Medium / High | Cancel after worker check but before publication/completion; vectors may publish and job may become `completed` despite cancellation | `app/indexing.py:148-167,192-193,252-259`; existing test only covers pre-worker cancellation (`tests/test_prd_regressions.py:340-364`) |
| AUD-002 | Confirmed integration edge case | Medium / High | Completed POST response lost; active lookup misses terminal job; new browser submit generates new ID and may repeat paid work/save duplicate answer | `static/app.js:187-189`; `app/jobs.py:48-52,84-97`; same-ID tests do not cover lost response |
| AUD-003 | Confirmed requirements gap; exploitability unconfirmed | Low / High code confidence | `LEXCITE_PUBLIC_URL` unset; Origin allowlist derived from request Host instead of fixed trusted config | `app/main.py:70-77`; `tests/test_security.py:218-230` |

No other confirmed functional bug met the evidence threshold in the delegated backend/integration review. Do not treat historical §14 findings marked fixed in the current implementation as open defects merely because the PRD preserves their earlier descriptions.

## 6. Confirmed and Suspected Dead Code

**Confirmed (static, low impact):** Unused imports listed in AUD-004. These can be removed without changing behavior, subject to ordinary focused regression checks.

**Suspected / internal unreachability only:**

| Symbol | Location | Evidence | Qualification |
|---|---|---|---|
| `query_laws` | `app/research_agent/retrieval.py:101-102` | No source, script, test, or UI caller found; only historical PRD mention | Could be an external import API; confirm compatibility before deletion |
| `embedding_fingerprint` | `app/llm.py:447-450` | No in-repository caller; described as compatibility wrapper | External callers may depend on it; retain unless API compatibility is explicitly ended |

No other obsolete module or unreachable execution path was established with sufficient confidence. Framework routes and UI-wired code were treated as reachable.

## 7. Test / Build / Lint / Type-check Results

| Command | Exit | Result |
|---|---:|---|
| `python -m pytest -q` | 0 | 416 passed in 42.64s |
| `npm run test:ui` | 0 | 15 fixture-only reliability checks and 23 DOM/HTTP checks passed; visual-browser availability was reported false for this command |
| `python scripts/test_browser.py` | 0 | Seven Chromium suites, 82 checks passed: smoke 10, agent 18, hybrid 11, layout 14, memory 11, sources 9, core 9 |
| `node --check static/app.js && bash -n start.sh` | 0 | JavaScript and Bash syntax passed |

Pytest emitted three non-failing deprecation warnings (Starlette/httpx `TestClient`, AnyIO `BlockingPortal`, and `jieba`/`pkg_resources`). The environment used Python 3.12.12 and Node.js 22.22.2. Installed Pydantic was 2.14.1 while `requirements.txt` pins 2.13.5; this drift did not cause the observed failures.

No separate build, lint, or type-check command is defined in `package.json`; the app has no frontend build step. Docker image build, Compose deployment, `scripts/test_deployment.py`, real model/provider calls, and Windows `start.bat` execution were not run in this audit. No dependencies were installed.

## 8. Integration Defects

1. **AUD-002 — research submission/recovery boundary:** UI-generated idempotency key is not retained through an ambiguous POST outcome; server idempotency is effective only when the same key is reused. This crosses `static/app.js`, the `/api/cases/{cid}/research` handler, and `jobs.start()`.
2. **AUD-001 — index worker/cancel boundary:** cancellation updates persisted job state independently of vector publication, while publication and final completion do not atomically verify the cancellation state.
3. **AUD-003 — deployment-origin boundary:** unconfigured origin policy falls back to request Host. The fixed `LEXCITE_PUBLIC_URL` deployment path is tested, but the Host-independent requirement is not met in fallback mode.

No other confirmed API/data transformation, provider interface, or persistence integration defect was established. External service compatibility and production behavior remain unverified, not presumed defective.

## 9. Prioritized Remediation Plan

1. **Fix AUD-001:** make index cancellation terminal and publication-safe with a shared synchronization/transaction boundary; add interleaving tests for cancellation during and after publication.
2. **Fix AUD-002:** persist/reuse the UI request ID after ambiguous submission, expose lookup/replay for terminal jobs, and test lost-response completion without a second model invocation.
3. **Close AUD-003:** use an explicit trusted public origin rather than request Host for the fallback policy; add Host-variation regression coverage and document the required setting.
4. **Complete release acceptance:** run real vendor chat/embedding/reranker and local Ollama workflows, verify target Compose/reverse-proxy deployment, and complete lawyer-reviewed holdout evaluation before claiming production or legal-accuracy readiness.
5. **Clean safe dead imports;** decide whether `query_laws` and `embedding_fingerprint` remain compatibility APIs before removing them.

## 10. Limitations and Unverified Areas

- Real vendor credentials, quotas, billing, model quality, Ollama weights, GPU/CPU performance, and target network/certificate environments were not exercised.
- No lawyer-annotated legal holdout and no measured legal accuracy/hallucination rate were available.
- Docker/Compose and isolated deployment smoke tests were not independently run in this audit. PRD §15 reports some deployment checks, but that section is dated 2026-10-19, after the audit date.
- Windows startup was not run. Only `start.sh` shell syntax was checked.
- DNS validation/connect time-of-check/time-of-use window and PDF decode-time memory exposure remain documented boundaries; output-character limits do not constitute a memory sandbox.
- Detailed P2/P3 acceptance criteria referenced from v0.5 are absent, so those items remain unverified/deferred rather than assumed implemented or missing.
- Findings use static execution-path analysis plus the listed test assertions and executed suites. AUD-001 and AUD-002 were not reproduced against live services; their triggering interleavings are supported by the inspected code and test gaps.
