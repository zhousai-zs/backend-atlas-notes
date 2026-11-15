# 第 186/208 课：多 Agent 协作与协调

> 序号 186/208 · id `l-multi` · 领域 L · 阶段 5 · 难度 专家 · 子类 权限与结果保障
> 前置：[`l-eval`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/185.html)、[`l-durable`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/177.html)
> 节点摘要：多 Agent 通过任务分解、并行工作和独立复核提升某些任务的处理能力，但也引入协调、预算和状态一致性问题。应先明确每个角色的输入、工具、产物格式、停止条件和写入范围。多个模型重复同一错误并不形成独立证据。

## 导读

第 185 课要求结论可验证、硬失败不能被平均分抵消；第 177 课处理的是可恢复的长期运行状态。本课把同一份 C++ 服务故障材料交给一个单 Agent 基线，以及三个职责受限的工作者（日志、指标、代码），用官方 OpenAI Agents SDK 的 `Agent.as_tool` 做编排者-工作者，而不是声称 Pi 或其他框架已经内置了通用多 Agent 运行时。

教学假设：材料里的租户名、请求号和源码片段是为对照构造的，不代表任何线上事故。实践锁定 `openai-agents==0.24.0`，使用官方 `ScriptedModel`，替换默认 trace 导出器并删除 `OPENAI_API_KEY`（不读取其值），`is_live_model=false`。

## 学习目标

1. 区分可独立子任务与必须串行的依赖，说明编排者、工作者、工具式调用和 handoff 的差别。
2. 独立完成实践：每个结论可追溯到原始证据，重复转述不被当成多份独立证据；工作者超时后主任务有明确降级。
3. 并发任务不写同一文件（或有显式串行提交）；报告对比单 Agent 基线及总工具调用/脚本步数，而不只报告最快一次延迟。

## 为什么后端要懂

并行看起来能缩短等待，但会复制上下文、放大费用，并把「谁写哪个文件、超时怎么办、矛盾结论如何合并」变成必须验收的问题。多数投票不能代替对原始日志的核对。

## 核心知识点

### 1. 工具式子 Agent 与 handoff 不是一回事

**直觉。** 把工作者当成工具调用时，编排者仍持有任务；handoff 则会把对话交给对方。

**原理。** 官方 `Agent.as_tool` 文档写明两点差异：工具式子 Agent 收到的是生成的输入而不是完整历史，并且调用结束后仍由原 Agent 继续。`handoff` 则接管会话。本课三个工作者 `LogsWorker`、`MetricsWorker`、`CodeWorker` 各自只注册自己的读工具，经 `as_tool` 暴露为 `consult_logs` / `consult_metrics` / `consult_code`。编排者按脚本依次调用三次再合并。这是代码控制的编排，不是工作者自行组网。

单 Agent 基线只用 `read_all_materials` 一次读完全部输入，作为对照，而不是「先看多 Agent 里最快的一次」。

### 2. 证据要溯源，转述不是新证据

**直觉。** 同一行日志说两遍，还是一行日志。

**原理。** 证据 schema 为 `{claim, source_path, excerpt, kind}`。日志工作者故意把同一句 `exists not implemented` 转述两次。合并时按 `(source_path, excerpt)` 去重，本次 raw 6 条、unique 5 条、`retellings_dropped=1`。指标里还有一条「CPU 翻倍证明计算热循环是根因」的 *inference*：`cpu_pct` 只从 18 走到 21，并不能证明根因。该条被保留为「有争议的推断」，没有当成对日志/代码结论的独立确认。

### 3. 超时要降级，写冲突要显式拒绝

**直觉。** 一个工作者卡住，主报告不能假装三份材料都齐了；两个工作者写同一个文件也不能靠运气。

**原理。** 注入超时后，`inspect_metrics_with_budget` 返回「worker timeout...degraded」，合并报告 `degraded=true`，结论写明只合并了日志和代码证据。工作区按路径登记所有者：`logs` 写入 `logs.json` 后，`metrics` 再写同一路径被 `PermissionError` 拒绝（`double_write_rejected=true`）。正常路径下每个工作者写自己的文件，编排者写 `orchestrator.json`。

对比表同时给出基线与多 Agent 的工具调用次数、脚本步数和本机耗时，并注明：这些耗时是本地脚本墙钟，**不能当成唯一质量信号，也不是真实模型的 token 费用**。本次没有消耗付费 token。

## 常见误区

- 把有串行依赖的步骤硬拆成并行，然后用「多数意见」抹平矛盾。
- 为每个小任务复制完整上下文，却只展示最快一次延迟。
- 把重复转述或同源摘录当成多份独立证据。
- 工作者超时后仍给出看起来完整的高置信结论。

## 设计权衡

并行能缩短互相独立的调查等待，但增加总成本和协调故障面。少量职责明确、产物 schema 统一的协作，通常比无限自组织更容易验收。若任务本身不可分解，单 Agent 基线可能更便宜、也更少写冲突。

## 动手实践

实践包在 `practice186/`，使用 `/workspace/atlas/cmvenv` 中的 `openai-agents==0.24.0`（`test.sh` 默认该解释器）。

```bash
tmp_dir=$(mktemp -d)
cp -a practice186 "$tmp_dir/"
cd "$tmp_dir/practice186"
bash test.sh
```

本次 `retellings_dropped=1`，同文件双写被拒绝，超时路径 `degraded=true`，对比里有工具调用和脚本步数。`is_live_model=false`。

未在本机实测：真实多模型协作的质量或 token 成本；Anthropic 研究系统文章中的生产数字不可直接外推到本任务。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）亲自抓取：

- [How we built our multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)：HTTP 200，`Date` 2026-11-15 16:13:55 GMT（Asia/Shanghai 2026-11-16 00:13:55）。案例中的编排代价不能直接推广。
- [Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)：HTTP 200，`Last-Modified` 2026-11-10 08:04:28 GMT（Asia/Shanghai 2026-11-10 16:04:28），`Date` 同上 00:13:55。用于核对手工编排、Agent-as-tool 与 handoff。
- [Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)：HTTP 200，`Last-Modified` 2026-11-14 19:41:46 GMT（Asia/Shanghai 2026-11-15 03:41:46）。本课没有再跑一套 LangGraph 图，只对照「并行与工具循环是显式状态图」这一原则。

## 自测题

**1. 日志工作者把同一句错误写了两遍，为什么合并后只能算一条证据？**

答：独立证据看的是原始出处和摘录，不是复述次数。本次按 `(source_path, excerpt)` 去重，`retellings_dropped=1`。

**2. 指标工作者超时后，什么样的报告才算合格？**

答：必须标明降级，并且不要假装指标结论已经就绪。本次 `degraded=true`，结论只合并日志和代码。

**3. 为什么对比里必须出现工具调用或步数，而不能只报耗时？**

答：本地脚本耗时不能代表真实模型费用或质量。本次同时报告基线 1 次工具调用与编排者 3 次专家调用，以及脚本步数，并写明没有消耗付费 token。

## 推荐资源

- [Anthropic multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)（英文，研究文章，专家，免费）：编排者-工作者及协调代价。
- [Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/)（英文，文档，进阶，免费）：代码控制、as_tool 与 handoff。
- [LangGraph workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)（英文，文档，进阶，免费）：顺序、路由、并行与工具循环。

## 本课小结

多 Agent 值得做的前提是任务可分解，并且证据、超时、写隔离和成本都能验收。官方 `Agent.as_tool` 让编排者保持任务所有权；转述去重、超时降级和文件所有权是宿主责任，不是模型默契。下一课进入领域 M，第 187 课 m-coding（Coding Agent 辅助 C++ 开发）。
