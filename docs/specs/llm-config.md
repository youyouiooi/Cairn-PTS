# LLM 配置池（多配置 / 多协议 / 自定义请求头 / 自动故障转移）

本文档描述本 fork 在服务端与 dispatcher 之间新增的 **LLM 配置池** 协议。
整体设计背景见 [`../DESIGN.md`](../DESIGN.md) §9.5，浏览器实现见
`cairn/src/cairn/server/static/index.html`（设置页）。

## 1. 数据模型

SQLite 表 `llm_configs`（旧单行表 `llm_config` 保留，启动时一次性迁移为
`name='default'`）：

| 字段 | 说明 |
|------|------|
| `id` | 自增主键 |
| `name` | 展示名，唯一（唯一索引 `llm_configs_name_uniq`），≤64 字符 |
| `enabled` | 0/1；dispatcher 只看到 enabled 的配置 |
| `protocol` | `anthropic` \| `openai_responses` \| `openai_chat` \| `gemini` |
| `base_url` | http(s) URL，禁止 credentials/query/fragment，末尾 `/` 自动去掉 |
| `model` / `small_fast_model` | 模型名，≤256 |
| `api_key` | 明文存储（DB 文件 0600），列表接口永不返回 |
| `headers` | 自定义请求头 JSON 对象；名称 RFC 7230 token，值禁控制字符，≤20 个 / 单值 ≤1024 / 总长 ≤8192 |
| `session_header` | 可选：**会话请求头名**（例如 `x-opencode-session`）。配置只填头名，值由 dispatcher 每次任务自动生成为当前会话 id；≤64 字符、RFC 7230 token、不能与保留头同名、不能同时出现在 `headers` 里 |
| `priority` | 升序决定 dispatcher 的尝试顺序；reorder 接口重排为 0,10,20… |
| `updated_at` | UTC ISO 时间 |

## 2. HTTP 接口

鉴权沿用全局中间件（除 `/healthz` 与静态资源外全部要认证）：

- **session 通道**（浏览器）：除 `runtime` 外的全部接口；Bearer 调用返回 403
  `API configuration can only be changed from a logged-in session`。
- **token 通道**（dispatcher Bearer）：仅 `GET /llm-configs/runtime`；session 调用
  返回 403 `Runtime configuration is only available to the dispatcher`。

### `GET /llm-configs`

返回全部配置（含 disabled），按 `priority, id` 升序。响应：

```json
{"configs": [{
  "id": 1, "name": "ark", "enabled": true, "protocol": "anthropic",
  "base_url": "https://ark.example/api/coding", "model": "m",
  "small_fast_model": "m-small", "headers": {"X-Tenant": "acme"},
  "priority": 0, "updated_at": "2026-01-01T00:00:00Z",
  "has_api_key": true, "api_key_hint": "sk-…abcd"
}]}
```

### `POST /llm-configs`

Body：`LlmConfigCreate` = `{name, enabled=true, protocol, base_url, model,
small_fast_model, headers, session_header, api_key}`。`enabled=true` 时 `base_url` / `model` /
`api_key` 必填（422）。`priority = max+10`。成功 201，返回单条 view。
重名 409 `LLM config name already exists`；最多 50 条，超出 400
`too many LLM configurations (max 50)`。

### `PUT /llm-configs/{id}`

Body：`LlmConfigUpdate` = `{name, enabled, protocol, base_url, model,
small_fast_model, headers, session_header, api_key|null}`，是对可变字段的**整体替换**。

- `api_key` 为 `null` = 保留已存 key；为 `""` = 清空；其它 = 替换。
- `base_url` 或 `protocol` 改变且 `api_key=null` → 400
  `base_url/protocol changed; re-enter the API key`（防止把旧 key 发给新主机）。
- 合并后 `enabled=true` 但 key 为空 → 400 `API key is required when enabled`。
- 不存在 → 404 `LLM config not found`；重名 → 409。

### `DELETE /llm-configs/{id}`

成功 204；不存在 404。允许删除全部配置（dispatcher 回落到 `dispatch.yaml` env）。

### `POST /llm-configs/reorder`

Body：`{"ids": [3, 1, 2]}`。必须恰好覆盖库中全部 id（否则 400
`ids must cover all configurations`），按数组顺序写 `priority=0,10,20…`。
返回更新后的列表（同 `GET /llm-configs`）。

### `POST /llm-configs/test`

草稿探测，不落库。Body：`LlmConfigTestRequest` = `{id|null, protocol, base_url,
model, small_fast_model, headers, session_header, api_key|null, timeout_seconds=10 (1..30)}`。

- 传 `id` 且 `api_key` 为空/`null` 时读取已存配置复用 key，但仅当
  `base_url + protocol` 与已存一致；否则（含 id 不存在）400 `API key required`。
- 成功/失败都返回 200：`{ok, status_code, latency_ms, error, response_preview}`；
  非 2xx 时 preview 截前 512 字符，错误文本中的 key 被替换为 `***`。

各协议的探测请求（自定义 headers 与内置头同名时自定义优先）：

| protocol | 方法/路径 | body 摘要 | 鉴权头 |
|---|---|---|---|
| `anthropic` | `POST {base_url}/v1/messages` | `{"model","max_tokens":10,"messages":[{"role":"user","content":"ping"}]}` | `Authorization: Bearer <key>` + `anthropic-version: 2023-06-01` |
| `openai_responses` | `POST {base_url}/responses` | `{"model","input":[{"role":"user","content":"ping"}],"max_output_tokens":16,"stream":false}` | `Authorization: Bearer <key>` |
| `openai_chat` | `POST {base_url}/chat/completions` | `{"model","messages":[{"role":"user","content":"ping"}],"max_tokens":10,"stream":false}` | `Authorization: Bearer <key>` |
| `gemini` | `POST {base_url}/models/{model}:generateContent` | `{"contents":[{"parts":[{"text":"ping"}]}],"generationConfig":{"maxOutputTokens":10}}` | `x-goog-api-key: <key>` |

请求参数：`timeout=(3.05, timeout_seconds)`、`allow_redirects=False`。
该接口会让服务器向任意 http(s) 地址发 POST，仅 session 管理员可用。

若请求带 `session_header`，探测请求会先按大小写不敏感移除静态 `headers` 中的同名键，
再注入 `<session_header>: cairn-config-test`（固定测试值；真实值由 dispatcher 按会话生成）。
这同时兼容历史脏数据（旧版把 session 头写进固定 headers 的情况）。

### `GET /llm-configs/runtime`

仅 Bearer。返回 **仅 enabled** 的配置，按 `priority, id` 升序，含明文
`api_key`、`headers` 与 `session_header`：

```json
{"configs": [{"id": 1, "name": "ark", "enabled": true, "protocol": "anthropic",
  "base_url": "https://…", "model": "m", "small_fast_model": "",
  "headers": {}, "session_header": "", "priority": 0, "updated_at": "…", "api_key": "sk-…"}]}
```

空池返回 `{"configs": []}`；dispatcher 保持 `dispatch.yaml` 的 worker env 不变。

## 3. dispatcher 行为

- 每拍刷新池（`GET /llm-configs/runtime`，仅指纹变化时重建 `self.config`）；
  `--startup-healthcheck-only` 启动模式也会先刷新一次池再探测。
- Worker 与协议的兼容矩阵：`claudecode`→`anthropic`；`codex`→`openai_responses`；
  `pi`→四种全部；`mock`→无。无兼容配置的 worker 保持 `dispatch.yaml` env。
- env 落点：
  - claudecode：`ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` / `ANTHROPIC_AUTH_TOKEN` /
    `ANTHROPIC_SMALL_FAST_MODEL`；自定义头 → `ANTHROPIC_CUSTOM_HEADERS` +
    内部 `CAIRN_LLM_HTTP_HEADERS`（JSON，健康检查 curl 用）
  - codex：`CODEX_BASE_URL` / `CODEX_MODEL` / `OPENAI_API_KEY`；自定义头 →
    `-c model_providers.cairn.http_headers={...}`（TOML inline table）+ 健康检查 curl
  - pi：`PI_BASE_URL` / `PI_MODEL` / `PI_API_KEY` / `PI_PROVIDER_API`
    （`anthropic-messages` / `openai-responses` / `openai-completions` /
    `google-generative-ai`）；自定义头写进临时 `models.json` 的 provider `headers`
- sticky + 冷却：健康检查失败 → 该配置 `cooldown = min(300, 30 * 2**(failures-1))` 秒，
  worker 切到下一个兼容配置；全部冷却时选最早到期者重试；任务成功后清失败计数。
- **会话请求头动态注入**（`session_header` 非空时）：配置只存头名，dispatcher 在每次
  请求时用**当前会话 id** 填充值，用于网关按 session 的提示缓存 / 粘性路由：
  - claudecode：`execute`/`conclude` 使用预生成的 Claude session UUID（同一任务多轮稳定），
    通过 `env ANTHROPIC_CUSTOM_HEADERS="…静态头…\n<name>: <session id>"` 前缀注入；
    健康检查用 `healthcheck-<worker.name>`。
  - codex：`execute` 用 `f"{worker.name}-{uuid4().hex[:12]}"`（每进程一个），`conclude`
    复用真实 session；健康检查用 `healthcheck-<worker.name>`；合并进
    `-c model_providers.cairn.http_headers={...}` 的 TOML inline table。
  - pi：值作为 shell 位置参数传入 models.json 生成脚本，写进 provider `headers`
    （脚本文本不含值）；execute 无 session 时随机，conclude 复用；健康检查用
    `healthcheck-<worker.name>`。
  - `session_header` 为空时所有命令与旧版完全一致（零变化）；值只存在于运行时
    env/argv，不落库、不打印日志、不返回给浏览器。
- 启动健康检查逐候选重试：任一 worker 的任一候选通过即可启动；所有 worker 的
  所有候选都失败才 `RuntimeError` 拒绝启动。
- 日志：`llm config applied configs=… applied_workers=… skipped_workers=…`、
  `llm config rotated worker=… from=… to=… cooldown=…s reason=unhealthy`；
  不打印 api_key。

## 4. 测试

```bash
cd cairn
uv run python -m unittest tests.test_llm_config tests.test_llm_override -v
```

服务端测试覆盖 CRUD/排序/通道鉴权/掩码/探测（mock `requests.post`，不真发网络）、
`session_header` 校验与四协议探测注入、旧库 ALTER 迁移；
dispatcher 测试覆盖协议兼容矩阵、候选选择、冷却退避、env 覆盖、自定义头与会话头动态注入。
