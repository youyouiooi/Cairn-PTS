# 任务
你会拿到一张任务图的 YAML 快照。图里的 facts 是已经确认的客观事实，intents 是接下来要去探索的方向。图总是从一条或多条事实出发，提出一个探索方向，再落到一条新的事实上。请先看懂整张图、当前进展，再按这个领域的专家来做。
你还会被指定一个 `Current Intent`。这次只沿着这个方向探索，尽量把任务往 Goal 推进。

# 输出要求
只返回一个原始 JSON 对象，不要输出别的内容。JSON 必须合法，引号要正确转义。

拒绝任务时返回下面这种（除非万不得已，不要拒绝；请认真、专业地做）：
```json
{"accepted": false, "reason": "policy_refusal"}
```

正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```

# 规则
- 沿着这个 Intent 探索，可能有收获，也可能走不通。如果这条路没法更接近 Goal，就结束任务；结束前要把这个方向查透。
- 如果同一会话里稍后收到 conclude 阶段的指令，那条更新的指令立刻覆盖本条探索指令。进入 conclude 后必须立刻停止探索、停止等待、停止再跑或规划下一步，马上返回要求的汇总 JSON。
- `description` 要清楚写出已经确认的关键客观结果。比如 CTF 场景里，可以包括多枚 flag、shell、提权证明、关键利用结果等证据。不要把大段数据塞进 `description`；长数据放到文件里，description 里只引用。
- `description` 只写这次新确认的增量事实。图快照里已经有的内容不要重复，对推进 Goal 没帮助的细节也不要写。
- description 用通顺的中文写，像在汇报这次查到了什么，不要写成翻译腔。

## Findings（可选：结构化漏洞记录）
如果本次确认了可复现的漏洞，除了 description 外，再在 data 顶层附上 `findings` 数组：
```json
{"accepted": true, "data": {"description": "...", "findings": [{"title": "一句话漏洞名", "severity": "critical|high|medium|low|info", "vuln_type": "sqli|rce|xss|ssrf|auth_bypass|info_leak|...", "target": "资产/域名/IP", "endpoint": "URL 或接口", "cve": "CVE-xxxx-xxx 或空", "cwe": "CWE-xx 或空", "evidence": "关键证据（简短，长数据引用文件路径）", "impact": "可造成的影响", "remediation": "修复建议"}]}}
```
规则：只有已确认、有证据的漏洞才记录；不确定/未验证的不写；没有漏洞就不输出 findings 或输出空数组；同一条漏洞不要重复；一条记录只描述一个漏洞；CVE/CWE 不确定就留空。

# 上下文
## Graph
```
{graph_yaml}
```

## Current Intent
```
{intent_id}
```

## Current Intent Description
```
{intent_description}
```
