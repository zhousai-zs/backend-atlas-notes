# 第 174/208 课：Agent Harness 的组成

> 序号 174/208 · id `l-harness` · 领域 L · 阶段 3 · 难度 进阶 · 子类 Agent执行模型
> 前置：[`l-tools`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/170.html)
> 节点摘要：Harness 把会调用模型的脚本，推进成可以诊断、恢复和验收的后台任务系统；system prompt 或一层工具包装都不是完整的 Harness。

## 导读

第 170 课说明 Tool Calling 只是模型提出结构化操作意图的接口，真正的执行器仍是宿主。本课把执行器周围的东西补齐：任务状态机、授权、预算、事件日志、检查点，以及“不要相信模型自报成功”。

教学假设：一个后台要让 Agent 写诊断报告文件。这是为讲解 Harness 组成而设计的场景，不代表腾讯或微信的内部实现。实践里的策略是脚本化规则，`scripted_policy_is_llm=false`，不调用外部模型，也不把规则命中写成“模型已经学会写报告”。

## 学习目标

1. 说明任务状态机 `ready / running / waiting / failed / cancelled / completed` 及其适用边界。
2. 独立完成实践：把每次状态变化记到事件表，并用同一授权层约束所有工具。
3. 用负例确认：模型宣称成功但文件缺失时不得标 completed；预算耗尽和取消后不得再启动新工具。

## 为什么后端要懂

节点给出的理由是「把会调用模型的脚本推进为可以诊断、恢复和验收的后台任务系统，并理解框架外围仍需自己负责什么」。演示脚本通常在一次成功打印后结束；后台任务还要回答：现在停在哪、为什么停、重启后能不能继续、已经写过的文件会不会再写一遍、模型说“写好了”到底有没有文件。

## 核心知识点

### 1. 六态状态机

**直觉。** 任务不是“聊到哪儿算哪儿”，而是一份有明确状态的后台工作。

**原理。** 实践把状态放在 SQLite 的 `tasks` 表：`ready` 可被调度，`running` 正在执行，`waiting` 等待外部输入或恢复点，`failed` 验收或授权失败，`cancelled` 被明确取消，`completed` 只有在独立验收通过后才能进入。本次六个状态都被走到：正常路径 `completed`，缺文件 `failed`，预算耗尽 `failed` 且 `stop_reason=budget_exhausted`，取消 `cancelled`，暂停任务先进入 `waiting` 再恢复完成。

**边界。** 状态机约束的是宿主任务，不是模型内部的“思考步骤”。把聊天轮次当成任务状态，会在进程重启后丢失“已经写过哪份文件”。

### 2. 适配层、工具注册与执行器分离

**直觉。** 模型（或脚本化策略）只许提出“调用哪个工具、参数是什么”；校验、授权和真正执行发生在另一层。

**原理。** 2026-11-15 23:52:58（UTC+8）抓取的 [Running agents](https://openai.github.io/openai-agents-python/running_agents/) 与 [Testing](https://openai.github.io/openai-agents-python/testing/)（Last-Modified 均为 2026-11-10 16:04:28，UTC+8）把 Runner 循环和可替换模型分开：测试里用脚本化模型就能驱动工具循环。本课 Harness 更小，但也保持同样的分离：策略层不能直接 `open()` 写文件，必须经过带 ACL 的执行器。Alice 有读写，Bob 只有读；Bob 的写操作在同一授权层被拒，状态为 `failed`，原因 `unauthorized`。

### 3. 每步 deadline、全任务预算与取消

**直觉。** 每一步可以有自己的截止时间，但整个任务还要有一份总预算；取消是朝着这同一份预算发出的信号。

**原理。** 实践在步数预算用尽时把状态打成 `budget_exhausted`（本次 3 步后停止，耗时约 0.005s，这是本机调度结果，复跑会变），并且不再启动新工具。取消路径进入 `cancelled`，同样不会再开新的写操作。预算数字是教学用的步数上限，不是生产 SLA。

### 4. 事件日志、检查点与副作用幂等键

**直觉。** 每一次状态变化都要能回答：什么时候、因为什么、对应哪一次调用。已经完成的写，重启后不能再写一遍。

**原理。** 事件表记录 `reason`、时间戳和 `call_id`。写报告用任务级幂等：同一份正文再次提交时，文件内容与 mtime 都不变（`restart_idempotent.body_unchanged=true`）。这不是框架“自动 exactly-once”，而是业务自己用同一主键覆盖成相同结果。

同日抓取的 [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)（200，无 Last-Modified）讨论长任务里的状态、事件与恢复；本课实现的是最小核心，不声称复现了该文中的完整产品 Harness。Pi Agent 的 README 同日也返回 200，可作为对照阅读，不是本实践的运行时。

### 5. 上下文投影、输出裁剪与原文存储

**直觉。** 给策略看的是投影，不是整份原始输出；原始输出另外存，必要时再取。

**原理。** 实践会裁剪过长输出并保留 originals，避免“窗口里只剩摘要、磁盘上却找不到原文”。这与第 175 课的指针策略是同一思路，本课先在 Harness 里留下存储位置。

### 6. 用实际文件验收，而不是相信自报成功

**直觉。** “模型说写好了”不是完成条件。

**原理。** 当策略宣称成功但目标文件不存在时，任务保持 `failed`，`stop_reason=acceptance_failed`。这是节点验收的硬条件，也是把 Harness 和聊天脚本分开的那条线。

## 常见误区

把 system prompt、聊天历史或一层工具包装当成完整 Harness，会在重启、取消和验收时暴露缺口。自动重试所有错误、把会话保存等同业务恢复，都会产生难复现的重复副作用。节点把这两条列为陷阱；本课用缺文件失败和幂等重启直接对应它们。

## 设计权衡

自己实现最小核心，便于看清状态、授权和验收各自承担什么。通用框架能减少重复工程，但不能替你定义“文件在不在算完成”、资源权限和外部副作用语义。本课选择最小核心，是为了把这三条变成可复跑的断言，而不是比较各个框架的功能清单。

## 动手实践

实践包是 `practice174.tar.gz`，只需带 sqlite3 的 Python：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice174.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice174"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后：

1. **六态都出现。** `states_used` 含 ready、running、waiting、failed、cancelled、completed。
2. **事件完整。** 每次变迁有原因、时间和 call_id。
3. **重启不重复写。** 已完成报告的正文与 mtime 不变。
4. **自报成功无效。** 缺文件时保持 failed。
5. **授权统一。** Bob 写失败，原因 unauthorized。
6. **预算与取消。** 3 步后 budget_exhausted；取消后不再启动新工具。

未验证：把真实模型接进该 Harness 后的生成质量。`scripted_policy_is_llm=false`。

## 源码与文档阅读

2026-11-15（UTC+8）抓取 Anthropic Effective harnesses（23:52:57，无 Last-Modified）、Agents SDK Running / Testing（23:52:58，Last-Modified 2026-11-10 16:04:28）以及 Pi Agent README（23:52:58，200）。文档说明的是产品级 Harness 或 SDK 循环；本实践是可复跑的最小状态机，策略不是 LLM。阅读时把“框架能跑循环”和“业务完成条件由你定义”分开。

## 自测题

**1. 为什么模型宣称“报告已写入”不能直接把任务标成 completed？**

答：完成条件是独立验收，而不是自报。本次缺文件用例的状态是 `failed`，`stop_reason=acceptance_failed`。

**2. 会话里保存了聊天记录，是否等于任务可以安全恢复？**

答：不等于。聊天记录没有幂等键，也不知道副作用是否已经发生。本次用 SQLite 任务行和文件幂等键保证重启不重复写；这是业务恢复，不是会话回放。

**3. 预算耗尽之后为什么不能再开一次“最后的补救工具”？**

答：再开一次就等于没有预算。本次 3 步后停止，并记录 `budget_exhausted`。取消同样不启动新工具。

## 推荐资源

- [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)：核对长任务中的状态、事件与恢复思路。
- [Pi Agent core · 事件、工具与状态](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)：对照另一套事件与工具组织方式。
- [Running agents](https://openai.github.io/openai-agents-python/running_agents/) 与 [Testing](https://openai.github.io/openai-agents-python/testing/)：核对循环、执行配置，以及不依赖真实模型的测试方式。

## 本课小结

Harness 是带状态机、授权、预算、事件和独立验收的任务系统；策略或模型只能提出意图，完成与否看文件和权限。本课用脚本化策略验证了六态、幂等重启和自报失败，没有调用外部模型。下一课是第 175 课 l-context（上下文工程与压缩）。
