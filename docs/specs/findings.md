# 结构化漏洞看板（findings）

> 本 fork 新增功能。Cairn 上游只有 Fact / Intent / Hint 三个通用原语，漏洞信息只能以自由文本存在 fact.description 里，无法统计、筛选和导出报告。
> 本功能新增一个**一等的漏洞实体**：服务端 `findings` 表 + REST API，dispatcher 把 worker 输出中可选的 `findings[]` 结构化写回，Web UI 提供项目级漏洞看板。
> 对于不输出 `findings[]` 的旧 prompt / mock worker，行为完全不变（纯增量）。

## 1. 数据模型

表：`findings`（`cairn/src/cairn/server/db.py` SCHEMA，`CREATE TABLE IF NOT EXISTS`，随 `db.configure()` 自动建立）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | TEXT | 项目内自增编号，形如 `v001`（`scoped_counters` kind=`finding`，prefix=`v`） |
| `project_id` | TEXT | `REFERENCES projects(id) ON DELETE CASCADE` |
| `title` | TEXT | 漏洞标题（必填，≤300 字符，指纹组成部分） |
| `severity` | TEXT | `critical` / `high` / `medium` / `low` / `info`，默认 `info` |
| `status` | TEXT | `open` / `confirmed` / `fixed` / `false_positive`，默认 `open` |
| `vuln_type` | TEXT | 漏洞类型（SQL 注入 / XSS / 反序列化……，≤512） |
| `target` | TEXT | 资产/目标（域名、IP、URL 前缀，≤512） |
| `endpoint` | TEXT | 入口路径/接口（≤512） |
| `cve` / `cwe` | TEXT | CVE / CWE 编号（≤512） |
| `evidence` | TEXT | 证据/PoC（≤4000，多行） |
| `impact` | TEXT | 影响（≤4000） |
| `remediation` | TEXT | 修复建议（≤4000） |
| `fact_id` | TEXT | 关联事实（可空，UI 可一键定位图中的 fact 节点） |
| `fingerprint` | TEXT | 去重指纹：`sha256(normalize(title) \x1f normalize(target) \x1f normalize(endpoint) \x1f normalize(vuln_type))[:32]` |
| `created_at` / `updated_at` | TEXT | UTC ISO-8601 |

索引：`findings_project_fingerprint_uniq(project_id, fingerprint)`（幂等 upsert 依据）、`findings_project_severity_idx(project_id, severity)`。

约束：单项目最多 `MAX_FINDINGS_PER_PROJECT = 500` 条；单次请求最多 `MAX_FINDINGS_PER_REQUEST = 100` 条。

## 2. 严重度 / 状态约定

- 严重度排序：`critical` > `high` > `medium` > `low` > `info`（API 列表默认按严重度再按 `created_at` 排序）。
- 状态语义：`open` 新增未确认；`confirmed` 人工确认；`fixed` 已修复；`false_positive` 误报。
- **人工状态保护**：agent 写回只会把新记录设为 `open`；已存在的记录若 `status != open`（人工改过），agent 的 upsert 不覆盖状态；`confirmed/fixed/false_positive` 不会被 agent 降级。

## 3. 指纹与幂等 upsert

agent 可能在多轮探索里重复报告同一漏洞。服务端按 `fingerprint` 做幂等 upsert：

- 指纹不存在 → 新建（返回 `created += 1`）；
- 指纹已存在 → 更新该记录（返回 `updated += 1`），保留 `id`/`created_at`，刷新 `updated_at` 与提供的字段；
- 归一化：`strip` + 小写 + 连续空白折叠，因此 `" SQL Injection in LOGIN form "` 与 `"sql injection in login form"` 视为同一条。

## 4. REST API

鉴权与其他业务端点一致（全局中间件：session cookie 或 Bearer token 均可；非 GET 走 CSRF）。

### `GET /projects/{project_id}/findings`

响应 `200`：

```json
{
  "findings": [
    {
      "id": "v001", "project_id": "proj_006",
      "title": "SQL Injection in login form", "severity": "critical", "status": "confirmed",
      "vuln_type": "SQL Injection", "target": "https://shop.example", "endpoint": "/login",
      "cve": "CVE-2024-0001", "cwe": "CWE-89",
      "evidence": "...", "impact": "...", "remediation": "...",
      "fact_id": "f003", "created_at": "...", "updated_at": "..."
    }
  ],
  "summary": {"total": 1, "critical": 1, "high": 0, "medium": 0, "low": 0, "info": 0,
              "open": 0, "confirmed": 1, "fixed": 0, "false_positive": 0}
}
```

项目不存在 → `404 {"detail": "Project not found"}`。

### `POST /projects/{project_id}/findings`

dispatcher（Bearer）与 UI（session）共用。请求体：

```json
{"findings": [{"title": "...", "severity": "high", "status": "open", "...": "..."}],
 "worker": "claudecode_deepseek-chat"}
```

- `findings` 1..100 条；`title` 必填；其余字段可省略；`worker` 仅记录日志用（可空）。
- 响应 `201`：`{"findings": [更新后的记录], "created": 2, "updated": 1}`。
- `fact_id` 不属于该项目 → `404 {"detail": "Fact f999 not found"}`。
- 超过单项目 500 条 → `400 {"detail": "too many findings for this project (max 500)"}`。
- 校验失败（severity/status 非法、title 为空、字段超长、空列表）→ `422`。

### `PATCH /projects/{project_id}/findings/{finding_id}`

请求体为任意要更新的字段子集（`title/severity/status/vuln_type/target/endpoint/cve/cwe/evidence/impact/remediation/fact_id`）。
响应 `200` Finding；未知 finding → `404`。UI 用它做状态行内修改与编辑。

### `DELETE /projects/{project_id}/findings/{finding_id}`

成功 `204`；未知 finding → `404`。

### `GET /projects/{project_id}/findings/export`

导出 Markdown：`Content-Type: text/markdown; charset=utf-8`、`Content-Disposition: attachment; filename="{project_id}-findings.md"`。
内容：标题 + 生成时间 + 统计表 + 按严重度分组的明细（标题/严重度/状态/类型/目标/入口/CVE/CWE/证据/影响/修复/关联事实/时间）。
路径参数优先于 `/findings/{finding_id}`，因此 export 路由必须先注册。

## 5. dispatcher 采集流程

worker（claude code / codex / pi）的 prompt 允许在原有输出 JSON 中**额外**带一个可选的顶层 `findings` 数组（见 `cairn/src/cairn/dispatcher/prompts/default/*.md`）：

```json
{"accepted": true, "data": {"fact": {"description": "..."}},
 "findings": [{"title": "SQL Injection", "severity": "high", "target": "shop.example",
               "endpoint": "/login", "vuln_type": "SQLi", "evidence": "..."}]}
```

- `contracts.validate_findings()` 容错校验：未知 severity → `info`；status 强制 `open`；文本截断到 512/4000；按指纹去重；单次最多 50 条；坏项跳过不抛异常。
- `contracts.extract_findings()` 从 payload 取 `findings`（兼容 `accepted/data` 包裹），没有则返回 `[]`。
- 各 task 在原有写回（conclude/complete/create_intent）成功之后调用 `tasks/common.best_effort_write_findings()` → `client.create_findings()`；写 findings 失败只记 warning，**不影响任务 outcome**。
- 覆盖阶段：bootstrap 主阶段与 bootstrap_conclude fallback、explore 主阶段与 explore_conclude fallback、reason 的 `complete`/`intents`/noop 输出。

## 6. Web UI

项目详情页右侧新增「漏洞」tab（数量徽章）：

- 严重度统计卡（critical/high/medium/low/info + 总数），可点击筛选；
- 严重度多选 chips、状态下拉、关键字搜索（title/target/endpoint）；
- 表格：严重度徽章、标题、目标/入口、CVE/CWE、状态行内 select（PATCH）、更新时间、操作；
- 行展开 evidence/impact/remediation；
- 「新增漏洞 / 编辑 / 删除」modal（含关联 fact 下拉）；
- 「定位」：跳转图视图并选中/居中对应 fact 节点；
- 「导出 Markdown」：下载 `{project_id}-findings.md`；
- 项目列表显示 `finding_count` 徽章。

## 7. 兼容性与迁移

- 旧库升级：`db.configure()` 执行 `CREATE TABLE IF NOT EXISTS findings` + 索引，**无破坏性迁移**；旧数据（facts/intents/hints/llm_configs）原样保留。
- 旧 prompt / 不输出 findings 的 worker（含 mock）：`extract_findings()` 返回空列表，任务行为不变。
- 本页所有字段/上限常量定义在 `cairn/src/cairn/server/models.py`（`MAX_*`、`normalize_finding_*`、`finding_fingerprint`）与 `cairn/src/cairn/dispatcher/contracts.py`（`validate_findings`）。

## 8. 测试

```powershell
cd cairn
.\.venv\Scripts\python.exe -m unittest tests.test_findings tests.test_findings_dispatcher tests.test_llm_config tests.test_llm_override -v
# 服务端 E2E（临时 DB + 127.0.0.1:8124）
.\.venv\Scripts\python.exe ..\.analysis\e2e_findings.py
```

既有失败豁免：`tests.test_auth.LocalExecutorPathTests.test_write_stays_in_workspace_or_prompt_tmp`（Windows 盘符相关的历史问题，与本功能无关）。
