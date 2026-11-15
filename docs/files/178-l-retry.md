# 第 178/208 课：工具重试、超时与取消

> 序号 178/208 · id `l-retry` · 领域 L · 阶段 3 · 难度 进阶 · 子类 上下文与持久运行
> 前置：[`l-durable`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/177.html)、[`f-deadline`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/86.html)、[`f-idempotency`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/88.html)
> 节点摘要：工具恢复应先分类错误，再决定重试、改参数、查询结果、请求人工或终止；超时只说明等待预算耗尽，不证明远端没有副作用。

## 导读

第 177 课解决崩溃后如何恢复一次任务；第 86 课与第 88 课已经介绍截止时间和幂等。本课把这些原则放进 Agent 的工具循环：一次 `submit_report` 可能已经写入成功，但响应超时了——这时绝不能再写一遍。

教学假设：Agent 调用一个“提交报告”的远程接口。这是为讲解超时后查询与错误分类设计的场景，不代表腾讯或微信的内部 RPC。实践的远端是进程内模拟存储，`is_live_remote_http=false`。

## 学习目标

1. 说明瞬时、永久、参数、授权与“结果未知”等错误分类及其适用边界。
2. 独立完成实践：提交已写入但响应超时后，按 `request_id` 查询，不重复写入。
3. 用负例确认：权限和参数错误不被原样自动重试；预算耗尽与取消后不启动新尝试；日志能区分三类结果。

## 为什么后端要懂

节点给出的理由是「把传统 RPC 失败语义应用到 Agent 循环，减少重复操作、无效消耗和故障级联」。模型层、工具执行器和 HTTP 客户端如果各自重试 3 次，请求会被放大；取消若被当成“已经回滚”，账本里可能已经有一笔写。

## 核心知识点

### 1. 先分类，再决定是否重试

**直觉。** 不是所有失败都该再试一次，更不是所有失败都该换个参数再试。

**原理。** 实践把结果分成：

- `not_executed`：根本没有发出去（授权失败、参数错误、已经取消、预算耗尽）。
- `remote_failed`：远端明确拒绝或失败（本次 401、400 不重试；429 可重试后成功）。
- `submit_result_unknown`：已经发出，但没有得到结论（写入后超时）。
- `succeeded`：确认成功，或超时后靠查询确认已经写入。

401 / 400 / 缺参数 / 无写权限都是 `attempts=0` 或 `1`，不会循环。429 则在第二次成功，`attempts=2`。

### 2. 每步 deadline 与全任务预算

**直觉。** 单次等待可以短，但所有尝试加在一起不能超过总预算；预算为 0 时连第一次也不发。

**原理。** 2026-11-15 23:53:00（UTC+8）抓取的 [Addressing cascading failures](https://sre.google/sre-book/addressing-cascading-failures/)（Last-Modified 2026-10-08 12:48:00，UTC+8）讨论重试预算，避免在过载时放大流量。实践用统一的 `budget_s`：预算已耗尽的任务 `submitted=[]`，状态为 `not_executed` / `budget_exhausted`。教学里用 0 秒预算来稳定复跑，生产中应使用真实剩余时间。

### 3. 超时之后必须先查询

**直觉。** 超时不是“没写”，只是“我没等到回答”。

**原理。** 模拟远端先把 `req-timeout` 写入，再返回 timeout。执行器把这次记为 `submit_result_unknown`，然后按同一个 `request_id` 查询，命中后记 `query_hit_after_timeout`，最终 `succeeded`，`wrote_count=1`，`submit_count=1`。不会再插入第二条记录。

### 4. 取消不启动新尝试，也不等于已经回滚

**直觉。** 取消是停止本地再发；远端若已经收到上一次提交，需要靠查询或补偿来确认。

**原理。** 在启动前取消，`submitted=[]`，`not_executed / cancelled_before_start`。这次没有发出去，所以没有远端副作用。若取消发生在 `submit_result_unknown` 之后，正确动作仍是查询而不是假设回滚——本课用“超时后查询”覆盖这一语义，不强行再造一条难以稳定复跑的竞态。

### 5. 模型修参数与执行器自动重试要分工

**直觉。** 参数错误应回到模型或调用方修改，而不是执行器原样连打。

**原理。** 无效参数在发出前就被分类为 `not_executed`，`attempts=0`。429 这类瞬时限流才由执行器在预算内重试。把两层重试叠在一起，正是节点所说的请求放大。

节点还列出了 [Thinking in LangGraph](https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph)。2026-11-15 23:52:59（UTC+8）抓取时该地址已重定向到 [thinking-in-graph-api](https://docs.langchain.com/oss/python/langgraph/thinking-in-graph-api)（Last-Modified 2026-11-15 03:54:57，UTC+8）。本课阅读的是重定向后的页面，用来对照错误分类与恢复，而不是在本实践中运行 LangGraph——那是第 182 课的工作。

## 常见误区

模型层、执行器和 HTTP 客户端各重试 3 次，最终会指数级放大请求。把取消或超时返回当成“业务已经回滚”，会在远端已经写入时再写一次。节点把这两条列为陷阱；本课用 `submit_count=1` 和不可重试的 401/400/参数错误直接对应。

## 设计权衡

重试能提高短暂故障的成功率，也可能加剧过载。对非幂等操作，先按稳定 `request_id` 查询，或交给人工，比盲目重试更可靠。本课只对 429 自动重试，正是这个权衡的最小实现。

## 动手实践

实践包是 `practice178.tar.gz`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice178.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice178"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后：超时后查询成功且只提交一次；auth / param 的 `attempts=0`；401 / 400 的 `attempts=1`；429 经过 2 次尝试后成功；取消与预算耗尽的 `submitted=[]`；日志种类包含 `remote_failed`、`not_executed`、`submit_result_unknown`。未验证：真实的跨区域 HTTP 超时或消息总线。

## 源码与文档阅读

SRE 书该章 Last-Modified 较早（2026-10-08 12:48:00，UTC+8），但仍是同日 200。LangGraph 思维模型文档以重定向后的 URL 为准。实践是进程内模拟，重试等待被缩短，不能把本次 2 次 429 当成某个云厂商的限流配额证明。

## 自测题

**1. 响应超时之后，为什么必须先按 `request_id` 查询，而不是再调用一次写入？**

答：超时只说明本地没等到答案，远端可能已经写入。本次模拟先写后超时，查询命中同一条记录，`submit_count` 仍为 1。

**2. 为什么 401 和缺少参数都不能自动循环重试？**

答：它们不是短暂故障。再发同一请求不会变成合法调用。本次 401 的 `attempts=1`，参数错误的 `attempts=0`（根本未发出）。

**3. 取消返回之后，能不能认为远端已经回滚？**

答：不能。取消只停止本地新尝试。若此前已经处于 `submit_result_unknown`，仍要查询。本次启动前取消的路径没有发出请求，所以写入次数为 0，这不能推广成“所有取消都无副作用”。

## 推荐资源

- [Addressing cascading failures](https://sre.google/sre-book/addressing-cascading-failures/)：核对重试预算与过载放大。
- [Thinking in LangGraph（已重定向到 thinking-in-graph-api）](https://docs.langchain.com/oss/python/langgraph/thinking-in-graph-api)：对照错误分类与恢复思路；原节点 URL 在 2026-11-15 已跳转。

## 本课小结

工具失败要先分类：没发出去、远端明确失败、发出去但结果未知。只有瞬时错误才在总预算内重试；超时后必须查询幂等键；取消不会启动新尝试，也不等于回滚。本课用进程内模拟验证了这些分支，没有打真实远程 HTTP。下一课是第 179 课 l-sandbox（工具沙箱与权限隔离）。
