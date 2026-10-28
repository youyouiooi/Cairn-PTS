# Cairn 0.2.1 设计与实现全解

本文基于 `Cairn-0.2.1` 源码通读，说明它到底在解什么问题、图协议怎么长、Dispatcher 如何调度、三类任务如何执行、结果如何写回。后半对应本仓库的 ARM macOS / 网站鉴权 / 安全加固改动。

---

## 1. 它在解什么问题

Cairn 把一类「起点明确、终点明确、路径未知」的问题建模成**有向无环事实图**上的搜索：

- **Origin**：已知起点（目标、环境、题面）
- **Goal**：成功条件（拿到 flag、拿到 shell、证完命题）
- **Path**：未知，需要边探索边长图

渗透测试只是它验证过的第一个域。系统刻意不做「角色分工」和「固定工作流」。没有 Planner / Scanner / Exploiter 这种预定义 Agent。所有执行单元都是同一类 Worker，只是被 Dispatcher 临时派成三种任务。

三个原语：

| 原语 | 是什么 | 不是什么 |
|------|--------|----------|
| **Fact** | 已确认的客观结论，图上的节点，只增不改 | 不是任务状态，不是「当前信念」的可变槽 |
| **Intent** | 从若干 Fact 出发的探索方向，图上的（超）边 | 不是已经在执行的进程；认领是边上的租约 |
| **Hint** | 图外旁注：人的判断、策略、纠偏 | 不参与因果，不改 Fact |

协调方式是 **stigmergy（信息素 / 共享黑板）**：Worker 之间不直接通信。谁都可以读整张图，谁都可以在图上留下新的石头（Fact）和路标（Intent）。下一个 Worker 只根据图的当前形状决定下一步。

每个 Worker 的循环是 OODA：Observe 整图 → Orient 当前态势 → Decide 新 Intent 或 Complete → Act 探索并写回 Fact。

服务端**不做推理**。它只保证图一致、租约互斥、超时回收。所有智能都在 Worker CLI（Claude Code / Codex / Pi）里。

---

## 2. 四层架构

```
浏览器 ──HTTP──► Cairn Server (FastAPI + SQLite)
                      ▲
                      │ 协议 API（读图 / claim / heartbeat / conclude / complete）
                      │
                 Dispatcher
           调度 · 选 Worker · 管进程 · 唯一写回者
                      │
          ┌───────────┴───────────┐
          │  项目执行环境          │
          │  原版：每项目一个 Docker │
          │  本仓库 Darwin：本机进程 │
          │    Worker CLI × N      │
          └───────────────────────┘
```

职责边界：

1. **Server**：真相源。Project / Fact / Intent / Hint / `project.reason` lease。提供 Web UI 和协议 API。
2. **Dispatcher**：唯一的协议写入者和控制面。Agent **不** claim、**不** heartbeat、**不** 调 Cairn API。Agent 只吃 prompt、吐 JSON。
3. **项目执行环境**：原版是 `cairn-dispatch-{project_id}` 容器（Kali + 工具链 + claude/codex/pi）。本仓库在 Apple Silicon 上默认改成本机 workspace。
4. **Worker driver**：把「健康检查 / 启动 / session / 收尾」翻译成具体 CLI 命令。

原版明确：**只支持一个 Dispatcher 连一台 Server**。本地的并发计数、不健康窗口、容器清理、bootstrap 去重都不跨进程。

---

## 3. 数据模型与协议

### 3.1 存储

SQLite，WAL，外键打开。默认路径 `~/.local/share/cairn/cairn.db`。

| 表 | 作用 |
|----|------|
| `projects` | 项目元信息 + 项目级 reason lease 四元组 |
| `facts` | `(id, project_id)` 复合主键。`origin` / `goal` 是创建时写入的特殊 Fact |
| `intents` | 边。`to_fact_id` 空表示未结论 |
| `intent_sources` | Intent 的 `from` 多源（超边） |
| `hints` | 图外提示 |
| `settings` | 全局 `intent_timeout` / `reason_timeout`（秒，≥5） |
| `counters` / `scoped_counters` | 生成 `proj_001` / `f001` / `i001` / `h001` |

Fact 没有更新接口。状态变化靠追加新 Fact（例如「shell 已断开」是一条新事实，不改旧的「已拿到 shell」）。

### 3.2 项目状态

```
active ──stop──► stopped ──start──► active
   │                                  ▲
   └──complete──► completed ──reopen──┘
```

- `active`：允许探索写（intent / heartbeat / conclude / complete / reason lease）
- `stopped`：硬停止。Server 立刻清空所有未结论 Intent 的 `worker`，并清空 `project.reason`。Dispatcher 看到后取消本地任务、停执行环境。
- `completed`：不能用 status 接口改回去，必须 `reopen`。`reopen` 删除指向 `goal` 的完成边，把纠错说明写成新 Fact，再连一条 `description=external_feedback` 的已结论 Intent。

Hint 在三种状态下都能写。标题随时可改。

### 3.3 Intent 租约

| 状态 | `to` | `worker` | 含义 |
|------|------|----------|------|
| 未认领 | null | null | 谁都可以 heartbeat 认领 |
| 进行中 | null | 有值 | 持有者续约；他人 409 |
| 心跳超时 | null | 被清空 | 按 `intent_timeout` 回收，可被别人认领 |
| 已结论 | 新 fact id | 产出者，永久保留 | 不再参与超时 |

`heartbeat` 同时承担 **claim** 和 **续约**。`conclude` 是原子操作：插入 Fact + 把 Intent 的 `to` 指向它。`release` 只清 `worker`，保留 `last_heartbeat_at`。

`from` 不能包含 `goal`。创建时 `worker` 只能是 `null` 或等于 `creator`。

### 3.4 项目级 reason lease

这不是图，不进 YAML 导出。字段挂在 `project.reason`：`worker / trigger / started_at / last_heartbeat_at`。同一项目同一时刻最多一个。超时按 `reason_timeout` 回收。`stopped` / `completed` 时 Server 直接清空。

用途：把「正在对整图做一次 reason」变成可观察、可互斥的协调状态，避免两个 reason 同时往图上扔 Intent。

### 3.5 Dispatcher 实际调用的接口

调度读：

- `GET /projects`（摘要 + 计数 + reason）
- `GET /projects/{id}`（完整图，结构化，用于判断）
- `GET /projects/{id}/export?format=yaml`（**只**用于塞进 prompt）
- `GET /settings`（启动时校验 timeout > interval）

写回：

| 动作 | 接口 |
|------|------|
| 创建保留 bootstrap intent | `POST /intents` `{from:["origin"], description:"bootstrap", creator:"dispatcher.bootstrap"}` |
| claim / 续约 intent | `POST /intents/{id}/heartbeat` |
| 释放 intent | `POST /intents/{id}/release` |
| 落 Fact 并结论 intent | `POST /intents/{id}/conclude` |
| claim reason | `POST /reason/claim` |
| 续约 reason | `POST /reason/heartbeat` |
| 释放 reason | `POST /reason/release` |
| 创建普通 intent | `POST /intents` `worker=null` |
| 完成项目 | `POST /complete`（插入 `to=goal` 的已结论边，项目变 completed） |

读接口在返回前会跑 `expire_workers` / `expire_reason_leases`，所以 Dispatcher 看到的是清理后的当前态。

---

## 4. 三类任务：契约与写回

任务类型是 Dispatcher 的事，Server 不区分。bootstrap 只是消费者约定的保留 Intent。

### 4.1 Bootstrap — 「先直接做一遍」

**何时**：项目仍是初始态。判定：

```
facts 恰好是 {origin, goal}
且 intents 为空，或全部是保留 bootstrap intent
保留约定：description="bootstrap" ∧ creator="dispatcher.bootstrap" ∧ from=["origin"] ∧ to=null
```

**为什么存在**：有些题可能一轮就做完。若跳过事实图直接 complete，关键证据会丢失。bootstrap 强制先落一条 Fact，再 complete。

**流程**：

```
若无保留 intent → 创建（不认领）
heartbeat claim
健康检查
渲染 bootstrap.md（origin / goal / hints，不读整图 YAML）
主阶段 timeout
  ├─ 合法 JSON：fact + complete → conclude 写 Fact → complete(from=[新fact])
  ├─ 超时 / 解析失败 → 同一 session 跑 bootstrap_conclude（只允许 fact）
  ├─ accepted:false / 非0退出 → release，不写图
  └─ 被 stopped/deleted 取消 → release，禁止进入 conclude
两阶段都失败 → release，下轮仍当新项目
```

主阶段不允许只返回 fact。conclude 阶段不允许 complete。主阶段 complete 写回失败时，Fact 仍保留，交给后续 reason。

### 4.2 Explore — 「认领一条 Intent，产出一个 Fact」

**何时**：非初始态，存在可认领的普通未结论 Intent（`to=null ∧ worker=null`，且不是 bootstrap intent）。实现里选 **created_at 最新** 的一条。

**流程**：

```
选 Worker → heartbeat claim 成功后才 submit 线程
ensure 执行环境
健康检查
把 YAML 快照写进执行环境，prompt 里给文件路径（避免超长命令行）
主阶段
  ├─ 合法 {description} → conclude
  ├─ 超时 / 解析失败 + driver 支持 session → explore_conclude（同一 session，只总结）
  └─ 命令失败 / rejected / 无 session → release，不写图
```

即使没打出漏洞，也必须返回客观结论（「测了什么、没发现什么」）。不允许把拒答文案当成 description。

### 4.3 Reason — 「读整图：做完了吗？下一步探索什么？」

**何时**（全部满足）：

- 项目 active、非初始态
- 当前没有未认领 Intent（有人正在 explore 也可以 reason，但不能有「还没人领的」Intent）
- `project.reason` 为空（单项目最多一个 reason）
- 出现「新态势」

**新态势**（`_reason_trigger`）：

| 条件 | trigger |
|------|---------|
| 尚无 checkpoint | `"initial"` |
| Fact 数量增加 | `facts:n->m` |
| Hint 数量增加 | `hints:n->m` |
| 从「有 open intent」变成「没有」 | `open_intents:n->0` |

**故意不算新态势的**：Intent 总数增加（那通常是上一轮 reason 刚创建的）、explore 失败/掉心跳/release。否则会 reason → 建 intent → 立刻再 reason 的死循环。

启动时：若项目已经有 open intent，先建一条基线 checkpoint，避免把「启动前已有的图」当成新态势，同时不吞掉启动后第一次新增的 Fact/Hint。

**输出**：

| JSON | Dispatcher |
|------|------------|
| `data.complete {from, description}` | `POST /complete` |
| `data.intents[]` 或兼容的单个 `intent` | 逐条 `POST /intents`（最多 `tasks.reason.max_intents`） |
| `data: {}` | 不写图（noop） |
| 无 open intent 时必须 complete 或 intents | 否则校验失败，本轮作废 |

reason **没有 conclude 二阶段**。超时、非法 JSON、rejected、非 0 退出：不写图。`finally` 里一定 `reason/release`。

reason 成功后，用**派发时**的 fact/hint/open 计数更新 checkpoint，不是用 reason 自己新建 intent 之后的计数。这样「刚创建的 intent」不会立刻再触发 reason。

---

## 5. 调度循环：每一拍做什么

入口：`cairn dispatch --config dispatch.yaml` → `DispatcherLoop.run()`。

启动：

1. 加载并校验 `dispatch.yaml` + prompt 资源
2. 用临时执行环境对每个 Worker 做并行健康检查；**全部失败才拒绝启动**，有一个健康就继续
3. 拉 `GET /settings`：`intent_timeout` 和 `reason_timeout` 必须 **大于** `runtime.interval`，否则心跳还没续上租约就被 Server 清掉。小于 `2 * interval` 只告警

之后死循环，每拍 `sleep(interval)`。`interval` 被刻意复用为：

- 主循环节拍
- bootstrap / explore / reason 的 heartbeat 周期

### 5.1 一拍的固定顺序

```
reap 已完成任务线程
reap 已完成的容器/workspace 清理
GET /projects
初始化缺失的 reason checkpoint
runtime_project_ids ∩ 当前 active（inactive 的踢出 admission 集合）
取消所有非 active 项目上仍在跑的任务（status 缺失视为 deleted）
排队 completed/stopped 的执行环境清理
在配额允许时派发
```

Server 请求失败：这一拍跳过，下一拍重试。`--once` 则直接抛出。

### 5.2 全局派发策略

伪代码（与 `loop.py` `_dispatch_available` 一致）：

```
if 当前运行任务数 >= max_workers: return

active = 所有 status==active 的项目
running = 已被本 Dispatcher 接手（在 runtime_project_ids 里）的 active 项目
idle    = 其余 active 项目

# 项目顺序：id 排序后 round-robin（project_cursor 每拍 +1），避免饿死

while 还能派发:
    dispatched = False
    for p in running:                    # 已运行项目优先
        if try_dispatch(p): dispatched = True; 若打满 max_workers 则 return
    if dispatched: continue              # 本轮有人吃到任务，先别开新项目
    if |runtime ∩ active| >= max_running_projects: return
    for p in idle:                       # 最多开一个新项目
        if try_dispatch(p): break
```

含义：

- **先把已经在跑的项目喂饱**（还有可派发的 bootstrap/explore/reason）
- 只有它们这一轮都派不出任务，才考虑 admission 一个新项目
- 项目一旦被接手，即使暂时没任务，也占着 `max_running_projects`，直到离开 active
- `max_workers` 是全局线程池上限；`max_project_workers` 是单项目任务上限（三类任务合计）

### 5.3 单项目 `try_dispatch`

```
cleanup 还在排队 → skip
本项目运行任务数 >= max_project_workers → skip
GET 完整项目；若已非 active → skip

if 初始态:
    若 project.reason 非空 → skip（bootstrap 与 reason 互斥）
    确保恰好走一条 bootstrap intent
    本地已有 bootstrap 任务或 intent 已被别人 claim → skip
    否则 dispatch_bootstrap
else:
    未认领的普通 intent（排除本地已经在 explore 的 id）
    if 有:
        取 created_at 最新的
        export YAML
        dispatch_explore
    elif project.reason 已被占 → skip
    elif 无新态势 → skip
    else:
        export YAML
        dispatch_reason
```

项目内优先级：**初始态只 bootstrap** → **有未认领 intent 就 explore** → **否则才 reason**。

### 5.4 派发一个任务的公共骨架

```
选 Worker
    过滤 task_types / max_running / 不健康窗口 / 本项目本类型的 rejected 窗口
    余下按 (priority 升序, 当前运行数, random) 排序，取第一个
claim（explore/bootstrap: intent heartbeat；reason: reason/claim）
    403 → 项目已停，info
    409 → 被别人占了，warning
    其它失败 → warning，本轮放弃
submit 到 ThreadPoolExecutor
登记 RunningTask（cancellation 句柄、intent_id、reason 用的计数快照）
runtime_project_ids.add(project_id)
```

真正的健康检查在**任务线程里、claim 之后**做。失败返回 `"unhealthy"`，主循环把该 Worker 放进 5 秒不可选窗口。`"rejected"`（模型 `accepted:false`）放进 (project, task_type, worker) 的 5 秒窗口，避免同一 Worker 对同一项目同一类型立刻重试。

### 5.5 任务线程内部时序

```
HeartbeatLease.start()          # 后台线程每 interval 续约
ensure_running(project)         # docker 容器或本地 workspace
healthcheck                     # 失败 → 释租约，return unhealthy
render prompt + 写 graph 快照
driver.prepare_session()        # claude/mock 预生成 uuid；codex 事后从 stderr 抽
exec 主阶段（外层 communicate timeout = 任务 timeout + 15s 宽限）
    容器内还会再套一层 GNU timeout -k 5s
解析 JSON（从 stdout 抽第一个对象，兼容 markdown fence）
按任务契约校验
写回 Server
finally: lease.stop()；reason 额外 release_reason
```

心跳失败（403/409 立即；其它失败超过 `2*interval` 宽限）会 `process.kill()`。项目被 stop/delete 时主循环调 `cancellation.cancel(status)`，同样杀进程，且 **禁止进入 conclude**。

### 5.6 执行环境生命周期

原版 Docker：

- 名字 `cairn-dispatch-{project_id}`
- `sleep infinity` 常驻，任务用 `docker exec`
- completed：按 `completed_action` `stop` 或 `remove`
- stopped：只 stop，保留现场
- deleted：设计上应当 orphan remove；源码实现了 `cleanup_orphan`，主循环未挂上（已知缺口）
- cleanup 走独立线程池，不堵调度拍

本仓库 Darwin 默认 local：每个项目一个目录，进程直接跑在宿主机。见第 8 节。

---

## 6. Worker 与 prompt

`dispatch.yaml` 只声明「用哪个 driver、跑哪些任务、并发、环境变量」。命令形状在 driver 代码里。

| type | 健康检查 | 主阶段 | session | conclude |
|------|----------|--------|---------|----------|
| claudecode | curl Anthropic `/v1/messages` | `claude --session-id --dangerously-skip-permissions -p` | 预生成 UUID | `claude -r {session}` |
| codex | curl `{CODEX_BASE_URL}/responses` | `codex exec --dangerously-bypass-approvals-and-sandbox` + 自定义 provider | stderr `session id:` | `codex exec resume` |
| pi | `pi --mode json --no-tools` | 写临时 `models.json` 再 exec pi | stdout JSON 事件 | 同 session |
| mock | 内嵌 python 抽样 | 按 prompt.phase 抽 outcome | 预生成 | 同 |

并发模型：一个 Worker = 一个 LLM 配额单元。不支持「多个 Worker 共用一把 key」。用 `workers[].max_running` 卡住。

Prompt 是包装内 markdown，按 `runtime.prompt_group` 选目录。占位符是直接字符串替换，不是模板引擎。默认组：

- `bootstrap.md` / `bootstrap_conclude.md`：`{origin}{goal}{hints}`
- `reason.md`：`{graph_yaml}{fact_ids}{open_intents}{max_intents}`
- `explore.md` / `explore_conclude.md`：`{graph_yaml}{intent_id}{intent_description}`

输出解析：`extract_json_object` 会扫全文和 \`\`\`json fence，再从每个 `{` 处 `raw_decode`。契约层允许两种形态：带 `accepted` 包装，或直接是 data 对象（兼容模型漏包一层）。

---

## 7. 一次完整项目的调度电影

假设新建项目「拿 flag」，hints 里写了优先看 80 端口。`max_project_workers=2`。

```
t0  项目被 admission。初始态 → 创建 i001 bootstrap → claim → 派 bootstrap
t1  bootstrap 主阶段 300s 没打完 → 杀进程 → 同 session conclude
t2  conclude 写出 f001「登录页默认口令」→ 项目不再是初始态（多了一条 fact）
    bootstrap 未 complete。checkpoint 尚未建立（还没有成功的 reason）
t3  无未认领普通 intent，reason 无 lease，无 checkpoint → trigger=initial → 派 reason
t4  reason 提出两条 intent：i002 测默认口令，i003 扫目录。写回后 release reason
    checkpoint 记的是 reason 开始前的计数（open=0，facts=3）
t5  下一拍：有未认领 intent，选最新的 i003 explore；若配额还够，同一拍 while 还会再派 i002
t6  i003 产出 f002。open intent 仍有 i002 在跑 → 不 reason（有未认领才会优先 explore；
    若 i002 已 claim，未认领为空，但 checkpoint 显示仍有 open intent 且 facts 增加
    → trigger=facts:3->4 → 允许再 reason，给正在跑的探索补方向）
t7  全部 intent 结论完，open=0，facts 增加过 → 再 reason
t8  reason 认为 f008 已满足 goal → POST /complete → 项目 completed
t9  Dispatcher 取消该项目任务（若还有），异步 stop/remove 执行环境
```

人可以随时 POST Hint。下一轮读图的 Worker 会看到。人也可以 stop：Server 立刻丢租约，Dispatcher 下一拍 cancel，conclude 不会再落脏 Fact。

---

## 8. 原版在 Apple Silicon 上为什么不好用

1. Worker 镜像 `FROM --platform=amd64 kalilinux/kali-rolling`，二进制全是 `linux_amd64`。在 ARM Mac 上只能 QEMU，又慢又容易踩 `network_mode: host`（Docker Desktop 的 host 网是 Linux VM，不是 Mac 主机）。
2. 控制面本身是纯 Python，ARM 上完全能跑，却被 compose 绑进 amd64 容器。
3. 网站零鉴权。`:8000` 一旦转到局域网，谁都能删项目、改 timeout、读完整渗透图。
4. 健康检查把 API key 写进 argv，`ps` 能看见。
5. compose 把 Docker socket 挂进 dispatcher，等于把宿主机 Docker 根权限交给容器。
6. 默认 `0.0.0.0:8000`，OpenAPI `/docs` 裸奔。

---

## 9. 本仓库改了什么（ARM / 鉴权 / 安全）

目标：在 Apple Silicon 上**不依赖 amd64 Kali 镜像**就能跑通控制面和 Worker；网站必须登录；Dispatcher 用独立 API token；默认只绑回环。

### 9.1 执行器

`runtime.executor`: `local` | `docker` | `auto`

- `auto`：Darwin → `local`，其它 → `docker`
- `local`：每个项目一个目录 `~/.local/share/cairn/workspaces/{id}`，用 `subprocess` 跑 Worker。超时只靠 Python 侧 watchdog（macOS 没有 GNU `timeout`）。不需要 Docker。
- `docker`：保留原行为，但默认 `network_mode=bridge`，可配 `container.platform`。Darwin 上若仍写 `host` 会告警并改成 bridge。

### 9.2 鉴权

双通道：

| 调用方 | 方式 |
|--------|------|
| 浏览器 | `POST /auth/login` → HttpOnly Session Cookie（SameSite=Lax，HTTPS 时 Secure） |
| Dispatcher | `Authorization: Bearer <CAIRN_API_TOKEN>` |

除 `/`、`/static/*`、`/auth/login`、`/healthz` 外全部要认证。Cookie 通道的写操作额外校验 `Origin`/`Referer`，防 CSRF。登录有每 IP 速率限制。密码 PBKDF2-HMAC-SHA256。Token 只存 SHA-256。

首次启动若未设置密码，生成随机管理员口令和 API token，写入 `~/.local/share/cairn/credentials`（模式 0600），并在终端打印一次。

已登录用户可通过 `POST /auth/password`（当前密码 + 新密码，最少 8 位）改密。仅 session 可改，Bearer token 不行。改密后其它 session 失效，当前 session 保留，并标记 `password_source=user`，之后启动不再被 `CAIRN_AUTH_PASSWORD` 覆盖。紧急回滚：`CAIRN_AUTH_PASSWORD_RESET=1`。

### 9.3 其它安全

- 默认 bind `127.0.0.1`
- 安全响应头：CSP、X-Frame-Options、nosniff、Referrer-Policy、Permissions-Policy
- `/docs` `/redoc` `/openapi.json` 同样要登录
- 数据库文件 0600
- 健康检查通过环境变量传 token，不再把 secret 拼进 argv
- compose 端口改 `127.0.0.1:8000:8000`，健康检查走 `/healthz`

Agent CLI 仍然带 `--dangerously-skip-permissions`：这是工具能力，不是网站漏洞。local 模式下 Worker 与你的用户同权，只对**你授权过的目标**使用。

### 9.4 本轮补强

- 登录速率限制默认**不信任** `X-Forwarded-For`。只有显式设置 `CAIRN_TRUST_PROXY=1` 时才按反代头计 IP，避免伪造头绕过 5 次 / 15 分钟限制。
- 本地执行器写文件只允许项目 workspace 或 `/tmp/cairn-prompts`（图快照），禁止 `..` 和任意绝对路径。
- 浏览器登出会停 polling；重复登录不会叠 hashchange / interval。
- 凭证文件和数据库路径可用 `CAIRN_CREDENTIALS_FILE` / `CAIRN_DB_PATH` 覆盖，测试不会踩真实 `~/.local/share/cairn/credentials`。

### 9.5 LLM 配置池（多配置 / 多协议 / 自定义请求头 / 自动故障转移）

LLM 配置从「单条 + dispatch.yaml env」升级为服务端持久化的有序配置池：

- 新表 `llm_configs`：`name / enabled / protocol / base_url / model / small_fast_model / api_key / headers(JSON) / session_header / priority / updated_at`。启动时把旧 `llm_config` 单行一次性迁移成 `name='default'`，旧表保留用于回滚（无迁移框架，迁移函数自身幂等）。`session_header` 列由 `_migrate_llm_config_columns()` 用 `ALTER TABLE ADD COLUMN` 补齐（旧库启动即升级）。
- 协议：`anthropic` / `openai_responses` / `openai_chat` / `gemini`。Worker 兼容性：`claudecode` 仅 `anthropic`，`codex` 仅 `openai_responses`，`pi` 全部四种，`mock` 忽略。
- 自定义请求头在服务端探测、`claudecode`/`codex` 健康检查与 curl、codex `-c model_providers.cairn.http_headers={...}`、pi `models.json` 中都会带上；写入时按 RFC 7230 校验名称、禁止控制字符并限制长度，避免 shell/TOML 注入。
- **会话请求头**（可选 `session_header`）：配置只存头名，dispatcher 在每个请求上用当前会话 id 填值（网关按 session 做提示缓存 / 粘性路由）：`claudecode` 用预生成 session UUID（execute/conclude 稳定），经 `env ANTHROPIC_CUSTOM_HEADERS="…"` 前缀注入；`codex` execute 每进程随机 `worker-<12hex>`、conclude 用真实 session，合并进 http_headers TOML；`pi` 经位置参数写进临时 `models.json` 的 provider `headers`；健康检查统一用 `healthcheck-<worker>`。值为运行时产物，不落库、不进日志、不回显浏览器；字段为空时命令与旧版完全一致。
- 浏览器的 session 通道是唯一写入口；dispatcher 用 Bearer 调 `GET /llm-configs/runtime` 拿有序 + 明文 key 的池，`_refresh_llm_config` 只在指纹变化时重建 `self.config`。
- 故障转移：每个 worker 保持当前配置（sticky）。任务健康检查失败 → 该配置记一次失败并按 30s→60s→120s→240s→300s 指数冷却，worker 轮换到下一个兼容配置（按池顺序，跳过冷却中的）；全部冷却时选最早到期者重试；成功后清该配置失败计数。启动健康检查改为逐候选重试，所有 worker 的所有候选都失败才拒绝启动。
- UI：设置页显示配置列表（新增 / 编辑 / 删除 / 排序 / 启用 / 测试），Key 只显示掩码；`GET /llm-configs` 永不含明文 key。API 细节见 `docs/specs/llm-config.md`。

### 9.6 结构化漏洞看板（findings）

上游只有 Fact / Intent / Hint 三个通用原语，漏洞只能写在 `fact.description` 自由文本里：能画图，但不能统计严重度、不能筛选、不能导出报告。本 fork 增加一个一等的漏洞实体，作为「通用状态空间搜索」之上的渗透领域视图：

- 新表 `findings`（`id` 项目内 scoped `v001`、`project_id` 级联删除、`title/severity/status/vuln_type/target/endpoint/cve/cwe/evidence/impact/remediation/fact_id/created_at/updated_at`，`fingerprint` 唯一索引）。随 `db.configure()` 的 `CREATE TABLE IF NOT EXISTS` 自动建表，旧库升级无破坏性迁移。
- 严重度 `critical/high/medium/low/info`，状态 `open/confirmed/fixed/false_positive`。人工状态受保护：agent upsert 不得把 `confirmed/fixed/false_positive` 改回 `open`。
- 指纹 = `sha256(normalize(title) + normalize(target) + normalize(endpoint) + normalize(vuln_type))`，`POST` 语义是**幂等 upsert**（重复报告合并、`created/updated` 计数返回），单项目上限 500 条。
- REST：`GET/POST /projects/{id}/findings`、`PATCH/DELETE /projects/{id}/findings/{fid}`、`GET /projects/{id}/findings/export`（Markdown 附件）。session 与 Bearer 均可写（人工在 UI 改，agent 经 dispatcher 写）。
- dispatcher 侧：worker 输出 JSON 可带可选的顶层 `findings[]`；`validate_findings()` 容错（未知 severity→info、status 强制 open、文本截断、指纹去重、单任务上限 50），各 phase 在原有写回成功之后 `best_effort_write_findings()` 写回，失败只记 warning，不影响任务 outcome；bootstrap/explore 主阶段与两种 conclude fallback、reason 的 complete/intents 输出都支持。
- UI：项目详情页「漏洞」tab（严重度统计卡 / chips 筛选 / 搜索 / 表格 / 行内状态 / 新增编辑删除 / 一键定位关联 fact 节点 / Markdown 导出），项目列表加 `finding_count` 徽章。
- 旧 prompt / 不输出 findings 的 worker（含 mock）行为完全不变（纯增量）。API 细节见 `docs/specs/findings.md`。

---

## 10. 在这台 ARM Mac 上怎么跑

```bash
cd /Users/kr0n0s/Cairn
source .venv/bin/activate

export CAIRN_AUTH_USERNAME=admin
export CAIRN_AUTH_PASSWORD=admin
export CAIRN_API_TOKEN="cairn_$(openssl rand -hex 24)"

# 终端 1
cairn serve --host 127.0.0.1 --port 8000

# 终端 2：先改 dispatch.yaml 里的模型地址和 key
cairn dispatch --config dispatch.yaml
```

打开 `http://127.0.0.1:8000`，登录后建项目。Dispatcher 用本机 `claude` / `codex` / `pi` 在 `~/.local/share/cairn/workspaces/` 下跑。

不设环境变量时，首次启动会打印随机口令和 token，并写入 `~/.local/share/cairn/credentials`（0600）。

只验证调度、不打真实模型：

```bash
export CAIRN_API_TOKEN=...   # 与 server 同一把
cairn dispatch --config dispatch_mock.yaml
```

鉴权回归：

```bash
cd /Users/kr0n0s/Cairn/cairn
python -m unittest tests.test_auth -v
```

已知仍按上游语义保留的点：

- Agent CLI 带 skip-permissions，local 模式与当前用户同权
- 只支持一个 Dispatcher 连一台 Server
- Docker 路径下 `cleanup_orphan` 已实现，主循环仍未挂上（上游缺口）
