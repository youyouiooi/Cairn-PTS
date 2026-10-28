# 任务
你会拿到一张任务图的 YAML 快照。图里的 facts 是已经确认的客观事实，intents 是接下来要去探索的方向。图总是从一条或多条事实出发，提出一个探索方向，再落到一条新的事实上。请先看懂整张图、当前进展，再按这个领域的专家来做。
注意：这里不是继续做任务，也不用等未完成的命令。你只需要把目前已经确认、对达成 Goal 最有帮助的关键事实汇总出来。
这是 conclude 阶段。它会覆盖同一会话里更早那条“继续做、继续探索、解决 Goal、等待命令结果或再执行动作”的指令。

# 输出要求
只返回一个原始 JSON 对象，不要输出别的内容。JSON 必须合法，引号要正确转义。

拒绝任务时返回：
```json
{"accepted": false, "reason": "policy_refusal"}
```

正常返回示例：
```json
{"accepted": true, "data": {"description": "..."}}
```

# 规则
- 立刻停下来，现在就给出 JSON。不要继续做任务。
- 不要再跑命令、再调工具、再检查别的东西、再等未完成的命令，也不要再去拿新信息。
- 只根据这条 conclude 提示之前已经确认的信息来写。还没确认的，不要等，也不要写进去。
- 这份 JSON 汇总就是本阶段的最终输出。输出后停止。
- `description` 必须是已经确认的客观事实结论。不要写计划、猜测或解释性空话。不要把大段数据塞进 `description`；长数据放到文件里，description 里只引用。
- `description` 只写这次新确认的增量事实。图快照里已经有的内容不要重复，对推进 Goal 没帮助的细节也不要写。
- description 用通顺的中文写，像在交代已经核实的结果，不要写成翻译腔。

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
