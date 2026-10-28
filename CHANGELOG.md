# Changelog

All notable changes to **Cairn-PTS** are documented in this file.

Cairn-PTS is a fork of [Cairn](https://github.com/oritera/Cairn) `0.2.1`
(AGPL-3.0). The core search engine — the Fact / Intent / Hint blackboard,
`bootstrap → explore → reason`, stigmergy scheduling, one dispatcher per server —
is unchanged. This file records what the fork adds, changes and fixes on top of
that baseline.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Per-session request headers for LLM gateways.** Each LLM configuration can now
  specify a *session header name* (`session_header`, for example
  `x-opencode-session`) in addition to fixed custom headers. The dispatcher fills
  the value on every request with the current session id — the agent session UUID
  for `claudecode`, a per-process id for `codex`/`pi`, and a
  `healthcheck-<worker>` id for health checks — so gateways that cache or route by
  session id work correctly. Same session stays stable across its turns, different
  sessions get different values; values are never written to the database, logs or
  browser responses. Configurations with an empty `session_header` behave exactly
  as before.
- Server: `session_header` field on create/update/test/list/runtime payloads,
  validation (RFC 7230 token, ≤64 chars, reserved header names rejected, must not
  duplicate a static header name), probe injection with a fixed test value, and a
  startup `ALTER TABLE` migration for existing databases.
- UI: "会话请求头名（可选）" field in the LLM configuration form plus a
  `session: <name>` badge on configured rows.

### Changed

- Findings dashboard: severity is now unmistakable at a glance — `critical` and
  `high` badges are solid red / solid orange with white text, `medium` keeps a pale
  amber badge, `low` is sky and `info` is slate. Severity statistics cards carry a
  matching colored top accent, and every table row gets a severity-colored left
  stripe, so high-risk findings no longer blur into medium ones in a long list.

## [0.3.0] - 2026-10-28

Initial public release of Cairn-PTS. Package version (`cairn/pyproject.toml`) is
`0.3.0`; upstream baseline is Cairn `0.2.1`.

Focus of this release: make the stack deployable and operable outside the original
ARM-macOS environment, replace the single env-var LLM configuration with an ordered
failover pool, and give penetration-testing projects a structured vulnerability
dataset with a dashboard.

### Added

**Authentication and hardening**

- Web login: `POST /auth/login` issues an HttpOnly `cairn_session` cookie plus a
  CSRF token; mutating requests require a matching `Origin` (or `Referer`) and
  `X-CSRF-Token`, validated against the session row.
- Dispatcher authentication with a bearer API token (`Authorization: Bearer`,
  SHA-256 hash at rest); the token is only accepted through a dedicated header and
  never via a cookie.
- Password hashing with PBKDF2-HMAC-SHA256, first-start credential generation to
  `~/.local/share/cairn/credentials` (mode `0600`), and a UI password-change flow
  that signs out other sessions.
- Login rate limiting (5 attempts / 15 min / IP); `X-Forwarded-For` is only trusted
  when `CAIRN_TRUST_PROXY=1`.
- Security headers (CSP, HSTS behind TLS proxy, frame-ancestors, no-store on the
  SPA) and `0600` on the SQLite database; `/docs`, `/redoc` and the protocol routes
  now require authentication, `/healthz` and `/static/*` stay public.
- Committed `tests/test_auth.py` covering login, CSRF, bearer auth, rate limiting,
  password change and session expiry.

**Multi-provider LLM configuration pool with automatic failover**

- New `llm_configs` table and browser UI (Server Settings → "多 LLM 配置") to
  add, edit, duplicate, reorder, enable/disable and delete any number of LLM
  configurations.
- Per configuration: `name`, `enabled`, `protocol`, `base_url`, `model`,
  `small_fast_model`, API key and arbitrary **custom request headers**.
- Four protocols: `anthropic` (`/v1/messages`), `openai_responses` (`/responses`),
  `openai_chat` (`/chat/completions`) and `gemini`
  (`/models/{model}:generateContent` with `x-goog-api-key`).
- Custom headers are applied by the connection test as well as by every worker
  adapter (`ANTHROPIC_CUSTOM_HEADERS` for `claudecode`, `http_headers` in the
  `codex` provider TOML, the provider `headers` object in `models.json` for `pi`),
  with validation against header-injection.
- Ordered failover: when a worker health check fails, the dispatcher cools that
  configuration down (`30s → 60s → 120s → 240s → 300s`) and rotates the worker to
  the next protocol-compatible configuration; if every configuration is cooling
  down it retries the one that recovers first, and sticks to the active one while
  healthy. Successful runs reset the failure counter.
- Startup health checks retry each worker candidate-by-candidate and refuse to
  start only when every candidate of every worker fails.
- New REST API: `GET/POST /llm-configs`, `PUT/DELETE /llm-configs/{id}`,
  `POST /llm-configs/reorder`, `POST /llm-configs/test`,
  `POST /llm-configs/{id}/test` (session channel) and
  `GET /llm-configs/runtime` (bearer channel, plaintext keys for the dispatcher).
- One-time, idempotent migration of the legacy single-row `llm_config` table into
  `llm_configs` (kept for rollback); empty pool falls back to the environment in
  `dispatch.yaml`.
- New `tests/test_llm_config.py`, `tests/test_llm_override.py`; end-to-end script
  covers the pool API and the dispatcher rotation logic.

**Structured findings (vulnerability dashboard)**

- New `findings` table: scoped ids (`v001`), title, severity
  (`critical/high/medium/low/info`), status (`open/confirmed/fixed/false_positive`),
  vuln type, target, endpoint, CVE, CWE, evidence, impact, remediation, optional
  link to a `fact`, timestamps, and a unique fingerprint per project.
- REST API: `GET/POST /projects/{id}/findings`,
  `PATCH/DELETE /projects/{id}/findings/{fid}`,
  `GET /projects/{id}/findings/export` (Markdown, `text/markdown` with
  `Content-Disposition`). `ProjectSummary` now exposes `finding_count`.
- Idempotent upsert by fingerprint (title + target + endpoint + type): repeated
  reports merge instead of piling up, and a human-set status is never overwritten
  by an agent.
- Dispatcher collection: agents may append an optional top-level `findings` array
  to their normal output JSON; it is validated (unknown severity → `info`, status
  forced to `open`, text truncated, fingerprint deduplicated, max 50 per task) and
  written back after the normal graph write. Failures never fail the task.
- Default prompts document the optional `findings` output block for bootstrap,
  explore and reason (and their conclude fallbacks).
- UI: per-project "漏洞 / Findings" tab with severity counters, severity chips,
  status filter, search, inline status edits, row expansion, manual CRUD, fact-node
  jump, Markdown export, and a `finding_count` badge in the project list.
- New `tests/test_findings.py`, `tests/test_findings_dispatcher.py`; end-to-end
  script covers CRUD, upsert, validation, export, cascade delete and the dispatcher
  write-back path.

**Deployment and documentation**

- Windows / Docker Desktop deployment path: dispatcher bind-mounts the Docker
  socket, creates one Kali `linux/amd64` worker container per project, and passes
  the graph snapshot into it as a file; `dispatch.yaml` uses
  `network_mode: bridge`.
- Docker Compose runs server and dispatcher from one `cairn-app` image, binds the
  server to `127.0.0.1:8000`, persists SQLite under `./datas/cairn/`, and reads
  credentials from `.env`.
- `docs/DESIGN.md` §9.1–§9.6 documents the executor, auth model, hardening, LLM
  pool and findings design; new `docs/specs/llm-config.md` and
  `docs/specs/findings.md` document the APIs.
- New end-to-end scripts and a publishable `.gitignore` were added for the public
  release.

### Changed

- Default executor resolution: `auto` keeps host processes on macOS (Darwin) and
  uses Docker elsewhere (Windows/Linux), instead of always launching a Kali
  container; `local` and `docker` remain explicit choices.
- Health checks take API keys from the process environment instead of `argv`, and
  custom LLM headers are redacted in logs.
- Default server bind address changed from `0.0.0.0:8000` to `127.0.0.1:8000`;
  Docker/Compose examples follow the loopback bind.
- UI settings modal now manages the LLM pool instead of a single config form; the
  project page gained the findings tab; the project list shows finding counts.
- `dispatch.yaml` and `Dockerfile`/`docker-compose.yaml` were adapted to the
  Windows / Docker Desktop workflow (Kali worker image, amd64 platform).

### Fixed / hardened

- Prevented an injected/malformed custom header from breaking health-check shell
  scripts (headers are shell-quoted and invalid ones are dropped).
- Probe failures no longer raise 500 when the remote closes the connection
  mid-response; error text is redacted and bounded.
- Dispatcher startup health check no longer aborts when only one candidate
  configuration fails; each worker is retried against its remaining candidates.
- Fixed the startup health-check subset bookkeeping bug (worker objects were
  treated as names, causing `TypeError`).
- Form validation and API errors are localized in the UI; failed inline status
  updates roll back instead of showing a stale value.

### Compatibility and migration

- Existing `cairn.db` files upgrade in place on first start: the new
  `llm_configs` and `findings` tables/indexes are created with
  `CREATE ... IF NOT EXISTS`, and the legacy `llm_config` row is copied once.
  There is no schema-migration framework; downgrading to upstream requires the
  old binary and is not supported by this fork.
- Existing projects, facts, intents and hints are untouched; the search engine and
  protocol timing are unchanged, so a running project keeps its graph.
- `dispatch.yaml` remains valid; adding an LLM pool is optional (an empty pool
  keeps using the YAML environment).
- The upstream worker image `ghcr.io/oritera/cairn-worker-container:latest` is
  `linux/amd64` only.

### Known limitations

- No structured asset inventory, no deduplication across projects, and no automatic
  vulnerability import from historical reports (findings are collected from
  workers' `findings` arrays or entered manually).
- Findings are not versioned; status history is limited to `updated_at`.
- The UI has no real-time event stream: the graph/timeline is rebuilt from the
  current graph, so deleted facts and cancelled executions are not shown.
- The pool and findings features are only tested on a single dispatcher; no
  horizontal scaling is supported (upstream constraint).

### Testing

- `cd cairn && python -m unittest tests.test_auth tests.test_findings
  tests.test_findings_dispatcher tests.test_llm_config tests.test_llm_override
  tests.test_dispatch_config -v` → 139 tests, 138 pass.
- One known Windows-only failure in `tests.test_auth`
  (`LocalExecutorPathTests.test_write_stays_in_workspace_or_prompt_tmp`) is
  pre-existing and unrelated to this release: the local executor's `/tmp` handling
  is Unix-specific, which is why Windows users should use the Docker executor.
- End-to-end scripts (not shipped in the repo): LLM pool API + dispatcher
  rotation, findings API + dispatcher write-back, mock dispatcher smoke run.

## Upstream baseline

| Area | Cairn 0.2.1 (upstream) | Cairn-PTS 0.3.0 |
|------|------------------------|-----------------|
| Worker runtime | Kali `linux/amd64` container, `network_mode: host` | Darwin→local processes; Docker optional, `bridge` |
| Web UI access | no authentication | session cookie + CSRF |
| Dispatcher→server | open HTTP | `Authorization: Bearer` token |
| Listen address | `0.0.0.0:8000` in compose | `127.0.0.1:8000` |
| Health checks | API key in `argv` | key in process environment |
| LLM configuration | one config from env in `dispatch.yaml` | ordered pool in the UI, 4 protocols, custom headers, automatic failover |
| Vulnerability data | free-form `fact.description` | first-class `findings` table, REST API, project dashboard, Markdown export |
| Findings collection | — | optional `findings[]` in agent output, dispatcher write-back, fingerprint upsert |
| Docs | DESIGN + protocol specs | plus `docs/specs/llm-config.md`, `docs/specs/findings.md`, release notes |

[0.3.0]: https://github.com/youyouiooi/Cairn-PTS/releases/tag/v0.3.0
