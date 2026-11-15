# 第 194/208 课：长任务服务与 Agent Worker

> 序号 194/208 · id `m-jobs` · 领域 M · 阶段 4 · 难度 深入 · 子类 工具基础设施
> 前置：[`l-durable`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/177.html)、[`h-messaging`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/122.html)、[`g-transaction`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/99.html)
> 节点摘要：将任务接收、状态存储与执行 worker 分离，使用租约、重试与幂等结果管理长任务。

## 导读

诊断类任务往往长于一次 HTTP 请求。本课把提交、状态和 worker 拆开：任务有稳定的 `job_id` 和状态机，worker 靠租约认领，副作用按 `job_id` 做成幂等。教学假设：这是 SQLite 上的教学任务服务，**不是** Temporal 集群。`is_live_model=false`。本机已有 Python 3.13.5 与 sqlite 3.46.1，无需宣称缺少数据库引擎。

## 学习目标

1. 说明 job ID 与状态机的边界：重投或换 worker 不得变成「新任务」。
2. 独立完成实践：提交异步诊断任务，杀死 worker 后由另一个接管并保留同一任务 ID。
3. 验收：租约到期不会产生双重副作用；用户能查询失败和取消状态。

## 为什么后端要懂

若把「队列又投递了一次」当成恰好一次，诊断脚本可能会被跑两遍（重启、发单、写库）。租约解决「谁在跑」，幂等键解决「效果只落一次」。

## 核心知识点

### 1. job_id 稳定，状态机可查询

**直觉。** 用户跟踪的是任务，不是某次进程。

**原理。** 状态为 `queued → running → succeeded|failed|cancelled`。提交时分配 `job_id`。用户可随时 `get(job_id)`。实践中一个 `always_fail` 任务进入 `failed` 且 `error=injected_failure`；另一个在被认领前取消，查询为 `cancelled`。取消已达终态的任务会返回 `already_terminal`。

**边界。** 这不是 Temporal 的 Workflow Id + Run Id + 事件历史。[Temporal Workflow Execution](https://docs.temporal.io/workflow-execution) 用可重放的历史恢复执行；本课用租约加幂等表模拟「同一任务被另一 worker 继续」，没有工作流引擎。

### 2. 租约到期不等于可以再做一次副作用

**直觉。** 第二个 worker 可以接管运行权，但不能再插入一条副作用。

**原理。** worker 认领时写入 `lease_owner` 与 `lease_until`。测试先让 `w1` 认领并进入 `running`，再用 SIGKILL 硬杀掉，使它无法调用 `finish`。等待租约过期后，`w2` 以同一 `job_id` 认领并成功。副作用表 `effects(job_id PRIMARY KEY)` 保证插入最多一次：本次 `effect_count=1`，第二次接管得到的是 `already_applied` 或直接跳过。作为对照，没有唯一键的「naive redeliver」会把计数加到 2——那正是「队列重投即恰好一次」的反例，不是本服务的默认路径。

## 常见误区

- 队列重投即认为任务自动恰好一次。
- 杀死 worker 后生成新的任务 ID，导致用户查询中断。
- 只保存成功态，不提供 failed/cancelled 查询。
- 把本课的 SQLite 服务写成已经跑过 Temporal。

## 设计权衡

持久编排（如 Temporal）可靠，但增加平台与运维成本。小规模诊断可以用租约加幂等表；一旦跨进程事务和计时器变复杂，再评估工作流引擎。

## 动手实践

实践包在 `practice194/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice194 "$tmp_dir/"
cd "$tmp_dir/practice194"
bash test.sh
```

本次实测：w1 被 SIGKILL 后，w2 以同一 job_id 完成任务，副作用恰好一次；取消与失败均可查询。sqlite 3.46.1。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Temporal — Workflow Execution](https://docs.temporal.io/workflow-execution)：HTTP 200，`Date` 2026-11-15 16:41:27 GMT（Asia/Shanghai 2026-11-16 00:41:27），`Last-Modified` 2026-11-14 09:47:16 GMT（Asia/Shanghai 2026-11-14 17:47:16）。文档中的 Workflow Id / Run Id 与事件历史未在本课运行；差异已写入 `not_verified_on_this_machine`。

## 自测题

**1. worker 被杀死后，用户应该查询哪个 ID？**

答：原来的 `job_id`。本次接管前后 ID 不变，状态从 running 到 succeeded。

**2. 为什么租约到期仍不会出现两次副作用？**

答：运行权可以转移，效果表以 `job_id` 为唯一键。本次 `effects` 行数保持为 1。没有唯一键的重投会变成 2。

**3. 用户怎样知道任务被取消或失败？**

答：查询同一 `job_id` 的状态机。本次取消态与 `error=injected_failure` 的失败态都可直接读出。

## 推荐资源

- [Temporal — Workflow Execution](https://docs.temporal.io/workflow-execution)（英文，文档，深入，免费）：对照正式工作流模型与本课的租约设计。

## 本课小结

长任务要有稳定的 ID、可查询的状态、可过期的租约和按任务幂等的副作用。下一课是第 195 课 m-ops（可验证的智能运维 Agent）。
