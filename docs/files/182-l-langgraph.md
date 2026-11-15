# 第 182/208 课：LangGraph 状态图实践

> 序号 182/208 · id `l-langgraph` · 领域 L · 阶段 4 · 难度 深入 · 子类 框架源码与实践
> 前置：[`l-durable`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/177.html)、[`l-human`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/180.html)
> 节点摘要：LangGraph 是显式状态和图执行运行时；节点返回状态更新，边或条件路由决定下一步，checkpointer 保存恢复位置，但不会自动解决外部副作用的恰好一次执行。

## 导读

第 177 课用手写租约和幂等键恢复任务，第 180 课把批准做成服务端状态。本课用锁定版本的官方 LangGraph 把“读日志 → 假设 → 查指标 → 验证 → 输出”画成一张可恢复的图，并在验证节点用 `interrupt` 等待人工决定。

教学假设：一个后台排障图根据固定证据形成假设，再请人确认后写入索引。这是为讲解状态契约、持久 checkpointer 和节点重放设计的场景，不代表腾讯或微信的内部编排系统。实践锁定 `langgraph==1.2.22` 与 `langgraph-checkpoint-sqlite==3.1.3`，使用**文件** `SqliteSaver`，**不用** `MemorySaver`。节点是确定性函数，`is_live_model=false`。

## 学习目标

1. 说明 State schema、节点更新、边与条件路由及其适用边界。
2. 独立完成实践：从持久化状态解释每次路由和最终停止原因；同一 `thread_id` 在进程重启后恢复到正确任务。
3. 用负例确认：授权错误不被自动重试；interrupt 恢复不重复业务副作用；并行更新同一字段时显式合并或显式拒绝。

## 为什么后端要懂

节点给出的理由是「将长任务中的状态、分支和等待位置显式化，减少隐藏在聊天循环里的恢复逻辑」。只画一张漂亮的图却不定义状态契约，或把任意非幂等 IO 塞进节点，恢复时就会重复写。把内存 checkpointer 用于“进程重启仍能恢复”的验收，重启后状态会直接消失。

## 核心知识点

### 1. 状态、节点、边与条件路由

**直觉。** 图上的每一步都是对同一份状态的更新；下一步由边或条件函数决定，而不是由模型临时决定“我想去哪”。

**原理。** 2026-11-15 23:52:59（UTC+8）抓取的 [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)（Last-Modified 2026-11-15 03:41:58，UTC+8）把运行时描述为状态图。实践的路径是 `START → read_logs → hypothesize → fetch_metrics`，然后按指标分流：`AUTH_DENIED` 走向 `abort`，否则走向 `verify`；`verify` 之后仅当 `verdict==confirmed` 才进入 `output`，人工拒绝则直接 `END`。

一次成功恢复后，`route_log` 为 `read_logs → hypothesize → fetch_metrics → verify → output`，`stop_reason=completed`，`next=[]`。授权失败路径的 `route_log` 为 `read_logs → hypothesize → fetch_metrics → abort`，`stop_reason=auth_error_not_retried`，`fetch_metrics` 只出现一次。

### 2. 并行更新同一字段：相等则合并，冲突则拒绝

**直觉。** 两个节点同时写同一个键，必须事先规定规则，不能靠“后到的覆盖”。

**原理。** 状态字段 `title` 使用显式 reducer `exclusive_str`：空值让位，相等则合并为同一值，冲突抛出 `ValueError: conflicting updates of field: 'A' vs 'B'`。相同值的并行更新得到 `title=A`。这是节点要求的“明确合并或显式拒绝”，不是默认 last-writer-wins。

### 3. 文件 checkpointer 与 thread 隔离

**直觉。** 恢复键是 `thread_id`，存储必须能在进程退出后还在。

**原理。** 同日 [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)（Last-Modified 2026-11-15 03:43:40，UTC+8）说明 checkpointer 保存恢复位置，store 承担长期数据。实践用 `sqlite3.connect(path, check_same_thread=False)` 构造 `SqliteSaver`，并调用 `setup()`。`incident-1` 完成后，`incident-B` 仍停在 `next=['verify']`，其 `request_id` 是 `req-B`，不会与 `req-1` 混淆。节点陷阱写明：不要用内存 checkpointer 做进程重启验收，本课遵守这一条。

### 4. interrupt、Command 与节点重放

**直觉。** 暂停不是把进程卡住，而是把状态写进 checkpointer；恢复时，官方运行时会从该节点的开头再跑一遍。

**原理。** 同日 [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)（Last-Modified 同样是 2026-11-15 03:43:40，UTC+8）写着：`interrupt()` 第一次抛出可恢复中断；用 `Command(resume=...)` 恢复时，图从该节点起点重放，因此“interrupt 之前的副作用必须幂等”。实践把人工确认放在副作用之前：第一次 `invoke` 返回 `__interrupt__`，`get_state` 显示 `next=['verify']`、`interrupted=true`、索引表仍为 0 行。关闭数据库连接后重新打开同一文件，状态仍停在 `verify` 且 `request_id=req-1`，再 `Command(resume="approve")`，索引表只有 1 行。再次对已完成线程 `invoke`，行数仍是 1。`Command(resume="reject")` 的线程 `stop_reason=human_rejected`，不会写入索引。

### 5. 授权错误不自动重试

**直觉。** 可重试的读错误和不可重试的授权错误必须走不同的边。

**原理。** `auth_ok=False` 时，`fetch_metrics` 写下 `AUTH_DENIED` 并路由到 `abort`，不会折返再打一次。这与第 178 课的分类一致。节点实践还提到“注入一次可重试读取错误”；本课把可重试读取留在第 178 课的 429 路径，本图用确定性授权失败证明“不会自动重试”这一条，不在图里再叠加一层不稳定的瞬时读失败。

### 6. 框架状态不是业务事务

**直觉。** checkpointer 记得“停在 verify”，业务索引表记得“有没有写过”。两者要一起设计。

**原理。** 索引写入放在单独的 `index.sqlite` 里，主键是 `request_id`。即便 `verify` 被重放，相同 `request_id` 的第二次插入是空操作。不要与 checkpointer 共用同一个连接：共享连接会在框架后台线程提交时与业务 `commit()` 打架。也不要指望只靠 checkpointer 就得到 exactly-once。

## 常见误区

只画图而不定义状态契约。误以为每个节点都可以包含任意非幂等 IO。将内存 checkpointer 用于需要进程重启恢复的验收。这三条是节点陷阱，实践分别用 `route_log` / `stop_reason`、interrupt 之后的幂等写入，以及文件 SqliteSaver 来对应。

## 设计权衡

显式图有利于可视化和恢复，但增加状态模型与迁移负担。短小的一次性脚本可能不需要持久图运行时。本课先用确定性节点验证更新和恢复，再把不确定的模型调用留给以后的有限边界；这次没有把聊天模型放进节点。

## 动手实践

实践包是 `practice182.tar.gz`，需要 `langgraph==1.2.22` 与 `langgraph-checkpoint-sqlite==3.1.3`：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice182.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice182"
bash test.sh
```

本次机器环境为 CPython 3.13.5。独立复跑后：暂停与进程重启后的 `next` 都是 `['verify']`；恢复后 `stop_reason=completed` 且索引 1 行；人工拒绝不写索引；授权路径不重试 `fetch_metrics`；`incident-B` 仍停在自己的 verify；并行冲突抛出明确错误，相同值合并为 `A`；`used_memory_saver=false`。未验证：节点内的直播聊天模型、多机 Postgres checkpointer、LangGraph Platform 部署。

## 源码与文档阅读

Overview / Persistence / Interrupts 三页均在 2026-11-15 23:52:59（UTC+8）抓取为 200。节点资源里的 [thinking-in-langgraph](https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph) 同日重定向到 [thinking-in-graph-api](https://docs.langchain.com/oss/python/langgraph/thinking-in-graph-api)（Last-Modified 2026-11-15 03:54:57，UTC+8），阅读以最终地址为准。官方 `interrupt` 文档明确写了节点会从开头重放，这是本课把副作用放在 resume 之后、并用业务主键做幂等的直接原因。

## 自测题

**1. 为什么进程重启验收不能使用 MemorySaver？**

答：内存检查点随进程一起消失，重启后无法 `get_state` 到原来的 `thread_id`。本次关闭连接再打开同一 SQLite 文件，`incident-1` 仍停在 `verify`，`request_id` 仍是 `req-1`。

**2. 用 Command(resume="approve") 恢复时，为什么 verify 里的写索引必须是幂等的？**

答：官方运行时会从该节点起点重放，`interrupt()` 之后才会拿到 resume 值并执行写入。若写入不是按 `request_id` 去重，重放就会插入第二行。本次恢复后以及再次 invoke 后都只有 1 行。

**3. 两个并行节点分别写入 `title="A"` 和 `title="B"` 时，为什么要抛错而不是任选一个？**

答：没有声明的 last-writer-wins 会让最终状态无法从输入解释。本次 reducer 显式拒绝冲突，并在相同值时合并为 `A`，满足“明确合并或显式拒绝”。

## 推荐资源

- [LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)：核对状态图运行时。
- [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)：核对 checkpointer 与 store，以及为何本课使用文件 SqliteSaver。
- [Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)：核对暂停、`Command(resume=...)` 与节点重放规则。

## 本课小结

显式状态图要把路由和停止原因写进可持久化的状态；同一 `thread_id` 才能在文件 checkpointer 上重启恢复；授权失败走 abort 而不是回边重试；interrupt 恢复必须搭配幂等副作用；并行字段更新要有明确的合并或拒绝。本课用 LangGraph 1.2.22 和文件 SqliteSaver 验证了这些点，没有使用 MemorySaver，也没有调用直播模型。下一课是第 183 课 l-pi，不在本批发布范围内。
