# 第 177/208 课：持久状态与任务恢复

> 序号 177/208 · id `l-durable` · 领域 L · 阶段 4 · 难度 深入 · 子类 上下文与持久运行
> 前置：[`l-memory`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/176.html)、[`h-messaging`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/122.html)
> 节点摘要：持久执行要把任务身份、已完成步骤和恢复位置保存在可靠存储中，同时明确哪些代码可能重放；检查点不能自动让外部写文件或发请求具备恰好一次语义。

## 导读

第 176 课区分了会话、任务状态和长期事实。本课把任务状态推进成可崩溃、可恢复的执行：检索日志、写报告、发布索引，并在三个崩溃点后仍然只有一份有效产物。正式节点同时把 `h-messaging` 列为前置，是因为恢复与消息投递一样，要处理重复与竞态，而不是只把聊天历史存下来。

教学假设：一个后台诊断任务要生成报告并写入结果索引。这是为讲解崩溃恢复和租约设计的场景，不代表腾讯或微信的内部工作流引擎。实践跑在单机 SQLite 上，`is_live_distributed_cluster=false`，不能据此声称具备多机容错。

## 学习目标

1. 说明任务 ID、租约、重放边界和外部副作用控制及其适用边界。
2. 独立完成实践：在至少三个崩溃点恢复后，报告和索引都只有一行。
3. 用负例确认：两个 worker 不能提交不同结果；版本不兼容或权限被撤销时必须暂停或拒绝。

## 为什么后端要懂

节点给出的理由是「让后台 Agent 能够可靠恢复，而不是在失败后重复整段工具调用或错误宣称任务已完成」。如果把“聊天记录还在”当成检查点，进程一死，写文件和建索引可能做一半、再做一遍，或被第二个 worker 写成另一份结果。

## 核心知识点

### 1. 任务身份、checkpoint 与执行租约

**直觉。** 恢复的对象是同一个 `task_id`，不是“再开一个很像的任务”。同一时刻只应有一个合法的执行者。

**原理。** 实践里每个任务有 `task_id`、`version`、`permission`、当前 `step` 和 `lease_owner`。获取租约时，若租约仍有效且属于别人，会抛出 `LeaseHeld`。崩溃后的恢复会先清掉已死亡进程的租约（教学里显式清空；真实系统应靠租约到期），再由重启后的 worker 重新获取。

### 2. 状态保存、事件日志和恢复入口

**直觉。** 恢复入口必须能回答：当前停在哪一步、已经留下了什么。

**原理。** `events` 表按顺序记录 `lease_acquired`、`retrieve_logs`、`write_report`、`publish_index` 和 `crash`。三个崩溃点恢复后的事件分别是：

- 崩溃在 `retrieve_logs`：crash 发生在读取完成前，恢复后补跑检索、写报告和索引。
- 崩溃在 `write_report`：检索已完成，恢复后只补写报告和索引。
- 崩溃在 `publish_index`：报告已在，恢复后只补索引。

三次恢复结束后都是 `reports=1`、`index=1`、`state=completed`。

### 3. 重放边界：哪些函数可以再跑，副作用如何控制

**直觉。** 纯读取可以重放；外部写入必须幂等或先查询再写。

**原理。** 实践明确列出策略：

- `retrieve_logs`：纯读取，可以重放；仅当仍停在该步时才推进指针。
- `write_report`：以 `task_id` 为唯一键；相同正文是幂等重放，不同正文被拒绝。
- `publish_index`：同样按 `task_id` 唯一；指针由已存在的 `request_id` 推导，且必须先有报告。

崩溃钩子打在 *插入之前*，所以“写入后但状态未提交”这一教学点，在本实现里表现为：若插入已成功，恢复走幂等分支；若插入尚未发生，恢复会写恰好一次。不要把这套单机唯一键说成框架 checkpointer 自带的 exactly-once。

2026-11-15 23:52:59（UTC+8）抓取的 [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 与 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)（Last-Modified 均为 2026-11-15 03:43:40，UTC+8）都强调检查点保存的是图状态；外部副作用要另外设计。第 182 课会在官方 LangGraph 里再做一次 interrupt 恢复，本课先用业务表把原则跑通。

### 4. 两个 worker 不能提交不同结果

**直觉。** 租约过期后第二个 worker 可以接手，但不能把已经冻结的报告改写成另一份。

**原理。** 实验中 worker-A 写入 `body-A` 后模拟租约到期，worker-B 试图写入不同正文，得到 `ConflictingReport`；最终库中仍是 `body-A`，索引也只有一行。这是条件更新 / 唯一键，而不是“谁后到谁赢”。

### 5. 版本与权限在恢复时重新检查

**直觉。** 任务创建时合法，不代表崩溃几天后仍能继续。

**原理。** 代码版本期望为 2，任务版本为 1 时，`acquire` 把状态置为 `paused` 并抛出 `IncompatibleVersion`。权限为 `revoked` 时同样暂停，抛出 `PermissionRevoked`。恢复路径不会为了“尽量跑完”而跳过这两道门。

## 常见误区

把保存聊天历史当成任务检查点，会丢失步骤指针和副作用记录。相信框架 checkpointer 能替所有外部服务提供 exactly-once，会在重复写文件或重复建索引时失败。这两条是节点陷阱；本课把重放策略和冲突拒绝写成断言。

## 设计权衡

更细的检查点能减少重做，但增加存储和状态兼容负担。教学系统限制为单机恢复，不能据此声称具备分布式容错。若需要跨主机，要换支持并发的存储，并重新证明租约与冲突处理，而不是把这份 SQLite 文件复制到多机。

## 动手实践

实践包是 `practice177.tar.gz`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice177.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice177"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后：三个崩溃点恢复均为 1 份报告和 1 条索引；冲突写入被拒绝且正文仍为 `body-A`；版本不兼容与权限撤销都进入 `paused`。未验证：解释器被操作系统直接杀死、网络分区下的 SQLite，或多机队列。

## 源码与文档阅读

同日还抓取了 [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)（23:52:57，UTC+8，200）。文档讨论长任务恢复的产品经验；本实践给出可复跑的最小租约与幂等写入，并把未覆盖的分布式边界写在 `results.json`。

## 自测题

**1. 崩溃发生在写报告之前，恢复后为什么不会出现两份报告？**

答：写入以 `task_id` 为唯一键，崩溃钩子清掉后只允许再插入一次；恢复走同一 `request_id`。本次 `t-crash-write` 恢复后 `reports=1`。

**2. 第二个 worker 拿到了租约，为什么仍不能把报告改成另一份正文？**

答：已存在的正文被冻结，不同正文触发 `ConflictingReport`。本次存活正文仍是 `body-A`。接手者可以幂等发布已有报告的索引，不能另写一份结果。

**3. 为什么不兼容的代码版本必须暂停，而不能“尽量接着跑”？**

答：旧检查点里的字段或副作用语义可能已经变了，继续跑会产生无法解释的产物。本次 version=1 的任务在 acquire 时进入 `paused`。

## 推荐资源

- [Persistence · Checkpointer 与 Store](https://docs.langchain.com/oss/python/langgraph/persistence)：核对检查点保存什么、不保存什么。
- [Interrupts · 暂停与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)：对照等待与恢复时的重放规则。
- [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)：阅读长任务恢复的工程经验。

## 本课小结

持久恢复要同时保留步骤指针、租约和副作用幂等键；能重放的是读取和幂等写，不能重放互相冲突的提交；版本和权限必须在恢复时再检查。本课在单机 SQLite 上验证了三个崩溃点和双 worker 冲突，不是分布式容错证明。下一课是第 178 课 l-retry（工具重试、超时与取消）。
