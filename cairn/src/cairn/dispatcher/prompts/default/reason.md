# 任务
你会拿到一张任务图的 YAML 快照。图里的 facts 是已经确认的客观事实，intents 是接下来要去探索的方向。图总是从一条或多条事实出发，提出一个探索方向，再落到一条新的事实上。请先看懂整张图、当前进展，再按这个领域的专家来判断。

你要判断两件事：
1. 现有事实是否已经足以说明 Goal 达成了
2. 如果还没有，现在要不要提出新的探索方向

# 输出要求
只返回一个原始 JSON 对象，不要输出别的内容。JSON 必须合法，引号要正确转义。

拒绝任务时返回下面这种（除非万不得已，不要拒绝；请认真、专业地做）：
```json
{"accepted": false, "reason": "..."}
```

如果 Goal 已经达成，返回：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}}}
```

如果 Goal 还没达成，但现在应该提出新方向，返回：
```json
{"accepted": true, "data": {"intents": [{"from": ["f001"], "description": "..."}, {"from": ["f002", "f003"], "description": "..."}]}}
```

如果 Goal 还没达成，而且现在也不该提新方向，返回：
```json
{"accepted": true, "data": {}}
```

## 规则
- 先判断事实是否已经覆盖 Goal。如果已经覆盖，`data.complete.from` 必须来自 `Valid facts`，`data.complete.description` 要用已经确认的结果，说明为什么足以证明 Goal 达成。
- 如果 Goal 还没达成，想一想卡在哪、有没有走偏，要不要提出一个正确的 Intent 把方向拉回来。
- 看有没有 `Open Intents`（已经提出、还没给出结论的方向）。如果有，对照 hints 和 facts 里的已知线索，判断现有方向是否已经覆盖这些线索、还要不要再提新方向。
- 如果 `Open Intents` 是空的，必须提出新方向。
- 如果未完成的方向已经很多，而当前局面又没有比现有方向更值得做的新路，可以先不提新方向（返回空 data）。
- 提新方向时，最多提出 {max_intents} 条高价值、互不重叠的探索路径。每条都应该能独立并行推进。
- 每条 Intent 写清核心洞察和方向即可，不必写得很细。不要太空、不要堆对推进 Goal 没帮助的细节、也不要写得过死。关键是：每条都是独立、明确、高价值的方向。
- 一条 Intent 可以来自多条事实。
- 不同 Intent 应覆盖不同探索维度，避免重复或大量重叠。
- description 用通顺的中文写，像在给同事交代下一步怎么查，不要写成翻译腔。

## Findings（可选：结构化漏洞记录）
如果本次确认了可复现的漏洞，除了 `complete`/`intents` 外，再在 data 顶层附上 `findings` 数组：
```json
{"accepted": true, "data": {"complete": {"from": ["f001"], "description": "..."}, "findings": [{"title": "一句话漏洞名", "severity": "critical|high|medium|low|info", "vuln_type": "sqli|rce|xss|ssrf|auth_bypass|info_leak|...", "target": "资产/域名/IP", "endpoint": "URL 或接口", "cve": "CVE-xxxx-xxx 或空", "cwe": "CWE-xx 或空", "evidence": "关键证据（简短，长数据引用文件路径）", "impact": "可造成的影响", "remediation": "修复建议"}]}}
```
规则：只有已确认、有证据的漏洞才记录；不确定/未验证的不写；没有漏洞就不输出 findings 或输出空数组；同一条漏洞不要重复；一条记录只描述一个漏洞；CVE/CWE 不确定就留空。

## 上下文
### Graph
```
{graph_yaml}
```

### Valid facts
```
{fact_ids}
```

### Open Intents
```
{open_intents}
```
