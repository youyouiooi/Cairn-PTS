# Cairn-PTS

**Cairn-PTS** is a fork of [Cairn](https://github.com/oritera/Cairn) (0.2.1) — a general-purpose AI state-space search engine whose first validated use case is autonomous penetration testing. It keeps the original blackboard / Fact-Intent-Hint engine, and adds:

- **website login** plus a dispatcher **API token** (session cookie + CSRF, bearer token for the dispatcher)
- loopback bind, CSRF, security headers, and hashed secrets at rest
- a **multi-provider LLM pool** in the UI: ordered configurations, four protocols, custom request headers, automatic failover on a single point of failure
- a first-class **findings** (vulnerability) entity: table, REST API, project dashboard, Markdown export
- a Windows / Docker Desktop deployment path (Kali `linux/amd64` worker container)

**Current version: [`0.3.0`](CHANGELOG.md)** — initial public release of the fork; upstream baseline [Cairn](https://github.com/oritera/Cairn) `0.2.1`. The full list of changes against the original is in [`CHANGELOG.md`](CHANGELOG.md).

> [!WARNING]
> **For learning, security research and authorized lab testing only — do not use it against real networks.** Cairn-PTS runs autonomous offensive tooling. Do not use it against production systems, the public Internet, or any target you do not own or have explicit written authorization to test. See [Disclaimer](#disclaimer).

Read `docs/DESIGN.md` for the full design and scheduler walkthrough.

## What changed from upstream 0.2.1

| Area | Upstream | This fork |
|------|----------|-----------|
| Worker runtime | `linux/amd64` Kali container, `network_mode: host` | Darwin defaults to **local processes**; Docker is optional and uses `bridge` |
| Web UI | No auth | Session cookie + CSRF |
| Dispatcher | Open HTTP to the server | `Authorization: Bearer` |
| Listen address | Compose published `0.0.0.0:8000` | `127.0.0.1:8000` |
| Healthcheck | API keys in `argv` | Keys stay in the process environment |
| LLM config | single config, env in `dispatch.yaml` | ordered pool in the UI, multi-protocol, custom headers, per-session header values, automatic failover |
| Vulnerability data | free-form `fact.description` only | first-class `findings` table, REST API, project dashboard, Markdown export |

The search algorithm is unchanged: bootstrap → explore → reason, stigmergy through the graph, one Dispatcher.

## Prerequisites

- Python ≥ 3.12 (or Docker ≥ 24 / Docker Desktop, see below)
- At least one worker CLI on `PATH`, depending on `dispatch.yaml`:
  - `claude` (`npm i -g @anthropic-ai/claude-code`)
  - `codex` (`npm i -g @openai/codex`)
  - `pi` (`npm i -g @mariozechner/pi-coding-agent`)
- `curl`, `git`

Docker is **not** required for the local-process executor on macOS / Linux. On
Windows the local executor is not usable (`/bin/sh`, `os.killpg`, `/tmp` are
Unix-specific), so use the Docker path described at the bottom of this file.

## Run locally (macOS / Linux)

```bash
cd /path/to/Cairn-PTS
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./cairn
# or: uv sync --project cairn

# Optional: pin your own password/token before first start
export CAIRN_AUTH_USERNAME=admin
export CAIRN_AUTH_PASSWORD=admin
export CAIRN_API_TOKEN="cairn_$(openssl rand -hex 24)"

# Terminal 1
python -m cairn.cli serve --host 127.0.0.1 --port 8000

# If you did not set env vars, first start prints a generated password and token
# and writes ~/.local/share/cairn/credentials (mode 0600)

# Put the model API key in the environment, not in dispatch.yaml:
export ANTHROPIC_AUTH_TOKEN='your-model-key'

# Terminal 2
python -m cairn.cli dispatch --config dispatch.yaml
```

Open `http://127.0.0.1:8000`, sign in, create a project. The dispatcher claims work and runs `claude` / `codex` / `pi` as host processes under `~/.local/share/cairn/workspaces/`.

If `python -m cairn.cli` is not on PATH after install, use:

```bash
cairn serve --host 127.0.0.1 --port 8000
cairn dispatch --config dispatch.yaml
```

## Auth model

- Browser: `POST /auth/login` → HttpOnly `cairn_session` + readable `cairn_csrf`. Mutating calls need `X-CSRF-Token` and a matching Origin.
- Dispatcher: `Authorization: Bearer <token>`. Token hash is stored in SQLite; plaintext lives in env or `~/.local/share/cairn/credentials`.
- Login is rate-limited (5 / 15 min / IP). `X-Forwarded-For` is ignored unless `CAIRN_TRUST_PROXY=1`.
- `/docs` and every protocol route require auth. `/healthz` and `/static/*` stay public.

Change the login password in Server Settings after signing in. Other sessions are signed out. A UI change survives restart even if `CAIRN_AUTH_PASSWORD` is set; force an env reset with `CAIRN_AUTH_PASSWORD_RESET=1`.

Rotate the API token by setting `CAIRN_API_TOKEN` and restarting the server.

Auth regression tests:

```bash
cd cairn
python -m unittest tests.test_auth -v
```

## LLM configuration pool (multi-provider, automatic failover)

Server Settings manages an ordered **pool** of LLM configurations instead of the
single config env override in `dispatch.yaml`:

- add / edit / delete / reorder any number of configurations; each has `name`,
  `enabled`, `protocol`, `base_url`, `model`, `small_fast_model`, an API key and
  optional **custom request headers**
- protocols: `anthropic`, `openai_responses`, `openai_chat`, `gemini`
- custom headers (for example `X-Tenant` or `x-api-key`) are sent by the
  connection test and by the worker adapters, so gateway/tenant headers work
  without touching YAML
- an optional **session header name** (for example `x-opencode-session`) for
  gateways that route or cache by session id: the configuration only stores the
  header *name*, and the dispatcher fills the value with the current session id
  on every request (the agent session UUID for `claudecode`; a per-process id for
  `codex`/`pi`; a `healthcheck-<worker>` id for health checks). Values never reach
  the database, the logs or the browser UI.
- when a worker health check fails, the dispatcher cools that configuration down
  (30s → 60s → … → 300s) and rotates the worker to the next compatible
  configuration; if every configuration is cooling down it retries the one that
  recovers first, then sticks to it again once healthy
- if the pool is empty or every configuration is disabled, the dispatcher keeps
  using the env from `dispatch.yaml`

Worker compatibility: `claudecode` only understands `anthropic`; `codex` only
`openai_responses`; `pi` supports all four protocols; `mock` ignores the pool.

The browser UI is the only writer (session channel). The dispatcher reads
`GET /llm-configs/runtime` with its bearer token (plaintext keys included). A
legacy single-row `llm_config` is migrated once into the new `llm_configs` table
at startup; the old table is kept for rollback. See
[`docs/specs/llm-config.md`](docs/specs/llm-config.md) for the API and failover
details.

## Structured findings (vulnerability dashboard)

Upstream Cairn stores everything as free text (`fact.description`, `intent.description`,
`hint.content`) and has no notion of a vulnerability, so the graph cannot answer
"how many critical holes did we find" or produce a report. This fork adds a
first-class `findings` entity:

- server: `findings` table (scoped ids `v001`, severity/status/type/target/endpoint/
  CVE/CWE/evidence/impact/remediation, optional link to a `fact`) plus REST endpoints
  `GET/POST /projects/{id}/findings`, `PATCH/DELETE /projects/{id}/findings/{fid}` and
  `GET /projects/{id}/findings/export` (Markdown)
- agents may add an optional top-level `findings` array to their normal output JSON;
  the dispatcher validates it (unknown severity → `info`, status forced to `open`, text
  truncated, fingerprint deduplicated, max 50 per task) and writes it back after the
  normal graph write; failures never fail the task
- idempotent upsert by fingerprint (title + target + endpoint + type), so repeated
  reports merge instead of piling up; human-set status (`confirmed`/`fixed`/
  `false_positive`) is never overwritten by agents
- UI: a "漏洞 / Findings" tab per project with severity counters, filters, inline status
  edits, fact-node jump, manual CRUD, Markdown export, and a `finding_count` badge in
  the project list
- old prompts / workers that emit no `findings` behave exactly as before

See [`docs/specs/findings.md`](docs/specs/findings.md) for the schema, API and
dispatcher contract.

## Docker (optional)

`docker-compose.yaml` starts the server and the dispatcher from one image
(`cairn-app`, built by the top-level `Dockerfile`):

```bash
cp .env.example .env        # set a real password and API token
docker compose up --build
```

- `cairn-server` binds `127.0.0.1:8000`, persists SQLite under `./datas/cairn/`
  and reads `CAIRN_AUTH_*` / `CAIRN_API_TOKEN` from the sibling `.env`
- `cairn-dispatcher` mounts the Docker socket (it creates one worker container
  per project) and `dispatch.yaml`; it reaches the server at
  `http://cairn-server:8000` via `CAIRN_SERVER`

The default login is `admin` / `admin` — change it in Server Settings after the
first sign-in.

### Windows / Docker Desktop

This is the deployment path used for Cairn-PTS. `dispatch.yaml` points the
dispatcher at the official Kali worker image:

```yaml
container:
  image: ghcr.io/oritera/cairn-worker-container:latest
  platform: linux/amd64
  network_mode: bridge
```

The dispatcher creates one `cairn-dispatch-<project_id>` container per project
(`sleep infinity` + `docker exec`) and passes the graph snapshot into it as a
file. If Docker Desktop rejects the socket mount, use `//var/run/docker.sock`
instead of `/var/run/docker.sock`. The upstream worker image is `linux/amd64`
only — do not set `platform: linux/arm64` on it.

## Disclaimer

> [!CAUTION]
> **For learning, security research and authorized testing only. DO NOT use Cairn-PTS against real networks, production systems, Internet-facing services, or any target you do not own or have explicit written authorization to test.** Unauthorized scanning, penetration testing or exploitation may be a criminal offence.

Cairn-PTS is an AI-driven autonomous penetration-testing **research** project. It is
distributed for study, experimentation and authorized security testing in isolated
lab environments only. It is not a commercial product: no warranty, no support and
no fitness for any particular purpose are provided.

Rules of use:

- **Authorization first.** Only run it against systems you own or for which you hold
  explicit written authorization from the owner (a signed engagement / rules of
  engagement, an in-scope bug-bounty or SRC program, or a CTF environment). "It was
  on the Internet" is never authorization.
- **Do not target the real Internet.** Do not point it at public IPs, third-party
  infrastructure, cloud accounts, customer environments or production networks. The
  worker image ships offensive tooling and an autonomous agent that may run
  destructive commands, exfiltrate data, or open outbound callbacks.
- **Isolate it.** The local executor runs worker CLIs (`claude`, `codex`, `pi`) with
  your user privileges and can read/write files reachable by your account, spawn
  outbound traffic and OOB callbacks (reverse shells, DNS/HTTP callbacks). Keep it in
  a dedicated VM or isolated network, never on a machine with valuable credentials.
- **Comply with the law.** You are solely responsible for compliance with applicable
  laws and regulations — for example the PRC Cybersecurity Law, Criminal Law
  articles 285/286 and Data Security Law, and equivalent laws in your jurisdiction —
  as well as the terms of service of any platform, model provider or scope provider.
- **Untrusted output.** Findings, model output and generated prompts are
  automation output, not verified facts. Verify anything manually before relying on
  it or reporting it. The maintainers are not responsible for damage, data loss,
  service disruption or legal consequences caused by use or misuse.
- **Protect secrets.** Never commit real API keys, tokens, credentials or customer
  data. Model-provider terms apply to your own accounts and keys.
- **No liability.** To the maximum extent permitted by law, the maintainers and
  contributors accept no liability for any direct, indirect, incidental or
  consequential loss arising from use of this software. It is provided "AS IS",
  without warranty of any kind.
- **Report software security issues privately** (do not open a public issue with
  exploit details).

### 中文免责声明

本项目仅供学习、安全研究以及在**获得明确书面授权**的隔离实验环境内使用。**严禁**将本项目用于真实网络、生产系统、公网资产，或任何未经授权的目标；未经授权对他人系统进行扫描、渗透或利用可能构成违法犯罪，使用者须自行承担全部法律责任。使用者应自行遵守所在地法律法规（如《中华人民共和国网络安全法》《刑法》第二百八十五条、第二百八十六条、《数据安全法》等）及相关平台规则。本项目按“现状”提供，不提供任何明示或暗示的担保；维护者不对因使用或滥用本项目造成的任何直接或间接损失负责。请勿在包含敏感凭据或生产数据的主机上运行本项目。

## License

GNU AGPLv3, same as Cairn 0.2.1.
