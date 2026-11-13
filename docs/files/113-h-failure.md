# 第 113/208 课：分布式故障模型

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-failure` |
| 英文名 | Failure models |
| 领域 / 阶段 | H / 第 3 阶段 |
| 子类 | 理论与故障模型 |
| 难度 | 进阶 |
| 前置节点 | `d-tcp`、`f-model` |
| 标签 | `Failure models`、`Distributed` |
| 参考资料 | [MIT 6.5840 — Distributed Systems](https://pdos.csail.mit.edu/6.5840/)（MIT 6.5840，英文文档，进阶，免费；2026-11-13 该入口返回 200，无跳转。`schedule.html` 与 `notes/l01.txt`、`notes/l-rpc.txt`、`notes/l-raft.txt`、`notes/l-bft.txt` 同批核对为 200，正文按需引用） |
| 摘要 | 跨机器系统不存在共享时钟和瞬时通信；超时只能说明等待预算耗尽，不能证明远端没有执行。 |

> **边界说明**：本课中的「内容后台」「文章标题 `Weekly digest`」「阅读计数 `article:01:views`」都是**教学假设**，用来让超时、重试、分区和故障检测变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `FailureEngine`，包含 `Clock`、`Replica`、`ProxyPolicy`、`ClientObservation`、`ServerObservation`，**不是 Redis，不是 redis-py，不是网络服务，不是 Raft，也不是 PBFT**。它没有 TCP、没有真实 socket、没有共识协议、没有跨机器共享物理时钟；`Clock` 只是仿真调度器，让实验可以重复，真实部署里的两台机器并不会共享这样一个时钟。官方材料已于 **2026-11-13** 用 curl 核对：`https://pdos.csail.mit.edu/6.5840/` 与 `schedule.html` 均为 200；`notes/l01.txt`（2026 Lecture 1，10886 字节）、`notes/l-rpc.txt`（2026 Lecture 2）、`notes/l-raft.txt`（2026 Lecture 6）、`notes/l-bft.txt`（讲义标题仍写 2024 Lecture 22，但 Spring 2026 日程的 LEC 21 链到这个文件）均为 200 且无跳转；`notes/65840-pbft.pdf`、`papers/castro-practicalbft.pdf`、`papers/bft-faq.txt` 均为 200。本课只引用这些核对过的页面。教学实验里的超时、耗时和状态都来自 `/workspace/atlas/lessons/practice113/results.json`，是教学虚拟时钟上的数字，不是生产 RTT，也不是任何系统在真实网络上的基准。

## 导读

### 一个直觉：超时不是「没发生」，只是「我不知道」

内容后台（教学假设）发布一篇稿件，客户端把标题改成 `Weekly digest (revised)`，等待预算 `timeout_s=0.05`。面板上出现 `timeout`，看起来像这次修改失败了。节点把这件事说得很硬：跨机器系统不存在共享时钟和瞬时通信；超时只能说明等待预算耗尽，不能证明远端没有执行。客户端没有收到回复，可能是请求没送到，可能是服务端已经提交而回复丢了，也可能是请求和回复都在路上、只是比预算慢。

本课要盯住的不是「这次调用返回了什么」，而是：返回 `timeout` 之后，服务端的存储可能处于哪几种状态；盲目重试会把哪种操作做两次；探测不到一台副本时，把它当崩溃处理会换来什么。对内容后台来说，这三件事分别对应标题修改、阅读计数和故障切换。

### 接上前置

前置的 [第 49 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/49.html) `d-tcp` 与 [第 83 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/83.html) `f-model` 已经建立了传输与 RPC 失败语义的基本词汇。本课把问题推到跨机器：一次本地函数调用返回，只说明本进程拿到了一个结果；一次远端调用超时，只说明本进程的等待预算用完了。节点把「为什么」写成建立复制、重试和容错设计的前提，原因就在这里：复制和重试都是在信息不完整的情况下做决定。

### 一次 SET 走进教学引擎之后

教学引擎的 `client_call(replica_id, op, timeout_s)` 从 `t0=clock.now()` 开始，先算出 `deadline=t0+timeout_s`。代理策略先决定请求能不能出门：`partitioned=true` 或 `drop_request=true` 时，请求不会到达副本，客户端等到 `deadline` 返回 `timeout`，并标记 `applied_unknown=true`。若 `delay_request_s` 把请求推到 `deadline` 之后，客户端会在 `t0` 到 `deadline` 之间先超时，请求被放进 `pending_requests`，等 `pump_pending()` 在更晚的时刻送达。

请求按时到达时，副本执行 `SET`。如果 `drop_reply=true`，回复被丢掉，客户端仍然等到 `deadline` 返回 `timeout`，但服务端已经改过值。`partitioned=true` 走的是前面那条短路：请求在代理处就被拦住，服务端没有执行，所以本课分区实验里 `r1` 的标题保持不变。如果 `delay_reply_s` 把回复推到 `deadline` 之后，客户端同样先超时，回复被放进 `late_replies`，在更晚的时刻到达。只有回复在 `deadline` 之前到达，`client_call` 才返回 `status="ok"`，并更新 `last_heard[replica_id]`。

`pump_pending()` 会把 `pending_requests` 和 `late_replies` 按 `at` 排序，推进虚拟时钟，投递请求或回复。因此「客户端已经超时」和「服务端后来才提交」可以同时成立；这不是引擎的漏洞，而是本课要展示的故障模型。

### 教学场景（教学假设）

假设内容后台用两个 KV 副本 `r1`、`r2` 保存已发布文章的标题，键为 `article:01:title`，初始值是 `Weekly digest`；阅读计数键为 `article:01:views`。一次标题修订写成 `Weekly digest (revised)`，一次分区另一侧的应急修改写成 `Weekly digest (hotfix)`。教学参数是 `timeout_s=0.05`、`delay_s=0.08`、`suspect_s=0.05`。这些数字是虚拟时钟上的教学值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **崩溃与拜占庭故障**：崩溃是停住不回答，拜占庭是继续回答但可能给出错误值；本课只在崩溃模型里做容错，不声称覆盖恶意副本。
2. **网络分区与消息延迟**：消息可以丢、可以晚到；超时只说明预算耗尽，客户端无法从超时本身判断远端是否执行。
3. **部分失败与故障检测**：故障是局部的，观察也是局部的；基于心跳的怀疑可能误报，把怀疑当崩溃会带来安全性或可用性的取舍。

## 学习目标

- 能解释崩溃与拜占庭故障及适用边界。
- 能独立完成：用代理延迟或丢弃一个 KV 副本的请求，记录客户端与服务端的不同观察。

## 为什么后端要懂

公众号内容后台（教学假设）会修改已发布标题、累加阅读计数、在副本之间切换读流量。标题修改超时之后，如果后台直接重试，`SET` 写同一个值通常看不出问题；换成阅读计数 `INCR`，重试就会把一次阅读算成两次。另一台副本一次心跳没回，后台如果立刻判定它崩溃并提升新副本，而旧副本其实只是网络断了，两侧就可能各自接受一个不同的标题。

节点要求你建立复制、重试和容错设计的前提，说的就是这些决定：超时之后到底能不能重试、重试要不要带幂等键、怀疑一台副本时是该等待还是该切换。这些判断不能靠「调用返回失败就是没成功」这句话，因为跨机器系统里没有这样一个全局的失败通知。

## 核心知识点

### 1. 崩溃与拜占庭故障

崩溃故障也叫 fail-stop：副本停止工作，不再产生新的回复。它可能丢掉还没持久化的内存状态，也可能在重启后需要恢复，但它不会在停止之后发出一个错误的值。本课先给 `r1`、`r2` 都写入 `Weekly digest (revised)`，再让 `r1` 崩溃。对 `r1` 的 `GET` 返回 `status=timeout`、`elapsed_s=0.05`、`applied_unknown=true`；对 `r2` 的 `GET` 返回 `status=ok`、`value=Weekly digest (revised)`、`elapsed_s=0.0`。服务端视角里，`r1` 的 `value` 仍是 `Weekly digest (revised)`，`applied_count=0`，`crashed=true`，`byzantine=false`。它没有撒谎，只是不再回答。

拜占庭故障是另一类模型：副本继续运行，可能因为缺陷、没有遵守协议，或被攻击者控制，返回错误的值。教学引擎用一个明确的教学桩来展示边界：`r1` 的存储里仍是 `Weekly digest (revised)`，但它对 `GET` 在 `elapsed_s=0.0` 返回 `Weekly digest`；`r2` 返回 `Weekly digest (revised)`。此时 `clients_see_different_values=true`，`timeout_does_not_detect_the_lie=true`。拜占庭副本没有超时，它比超时更早地给了一个看起来成功的回复，所以基于等待预算的故障检测根本抓不到它。

MIT 6.5840 的材料把这两种模型的边界写得很清楚。`notes/l-bft.txt` 说明课程此前一直假设 fail-stop；拜占庭是 buggy、不遵守协议、或被攻击者控制的副本。`notes/l01.txt` 里 MapReduce 假设 fail-stop，错误计算被归为「too bad」。`notes/l-raft.txt` 指出复制适合单个副本的 fail-stop，不一定能对付缺陷或误操作。教学引擎按节点的权衡只限定在崩溃故障：`teaching_limited_to_crash_failures=true`，`does_not_claim_to_cover_malicious_replicas=true`。它不实现 PBFT，也不把「复制 + 故障切换」说成能解决撒谎的副本。

这个边界对设计很重要。崩溃模型下，超时和连接失败可以作为「这台副本可能停了」的证据；拜占庭模型下，一个快速的、格式正确的回答也可能是假的，需要额外的交叉校验或拜占庭容错协议。本课后面的分区、重试和检测，全部建立在崩溃模型上，不能直接搬到恶意副本的场景。

### 2. 网络分区与消息延迟

节点把这一条写成：跨机器系统不存在共享时钟和瞬时通信；超时只能说明等待预算耗尽，不能证明远端没有执行。教学引擎用 `timeout_s=0.05` 和 `delay_s=0.08` 让「预算先到、消息后到」稳定发生。四条路径的客户端观察和服务端观察如下。

| 路径 | 客户端观察 | 服务端观察 |
|---|---|---|
| `drop_reply` | `status=timeout`，`elapsed_s=0.05`，`applied_unknown=true` | `value=Weekly digest (revised)`，`applied_count=1`，`title_committed=true` |
| `drop_request` | `status=timeout`，`elapsed_s=0.05`，`applied_unknown=true` | `value=Weekly digest`，`applied_count=0`，`title_committed=false` |
| `delay_reply` | `status=timeout`，`elapsed_s=0.05`，`applied_unknown=true` | 请求已提交为 `Weekly digest (revised)`，late reply 在 `at=0.08` 到达，`value=Weekly digest (revised)` |
| `delay_request` | 先在 `elapsed_s=0.05` 超时，`applied_unknown=true` | 请求在 `at=0.08` 才送达并提交，`value=Weekly digest (revised)`，`title_committed=true` |

`drop_reply` 和 `drop_request` 的客户端字段完全一样：`status=timeout`、`elapsed_s=0.05`、`value=null`、`applied_unknown=true`。结果是 `client_timeout_observations_identical=true`，而服务端 `server_observations_differ=true`：前者已经提交了修订标题，后者连请求都没收到。于是 `cannot_distinguish_drop_reply_from_drop_request=true`。客户端手里没有足够信息去区分这两种世界，这正是 `timeout_is_not_proof_of_rollback=true` 的含义，也是 `timeout_only_means_budget_exhausted=true` 的含义。

`delay_reply` 更直接：请求在预算内到达并提交，回复要等到 `at=0.08`，超过 `deadline=0.05`，所以客户端超时；`delay_reply_commits_before_client_gives_up=true`，但 `delay_reply_arrives_late=true`。`delay_request` 则展示了另一种更麻烦的情况：客户端在 `0.05` 已经放弃，请求在 `0.08` 才送达并提交，`delay_request_commits_after_timeout=true`、`delay_request_delivered_late=true`。所以超时之后不仅可能「已经提交」，还可能「将在超时之后提交」。`cannot_distinguish_delay_from_loss_at_timeout=true` 说的是同一件事：在超时那一刻，延迟和丢失对客户端看起来一样。MIT 6.5840 的 `notes/l-rpc.txt` 也指出，客户端看不到回复时，并不知道服务端是否见到过请求。

超时带来的不确定性会直接传到重试策略上。教学引擎对 `article:01:views` 做 `INCR`，先让第一次回复丢失，再比较三条路径。

| 路径 | 第一次 | 第二次 | 最终 `views` | `applied_count` | 结论 |
|---|---|---|---|---|---|
| 盲重试 | `status=timeout`，`elapsed_s=0.05` | `status=ok`，`value=2`，`elapsed_s=0.0` | 2 | 2 | `safety_violation=true` |
| 不重试 | `status=timeout`，`elapsed_s=0.05` | 无 | 1 | 1 | `safety_violation=false`，`availability_reduced=true`，`client_confirmation=timeout` |
| 幂等键 `publish-01` 重试 | `status=timeout`，`elapsed_s=0.05` | `status=ok`，`value=1`，`elapsed_s=0.0` | 1 | 1 | 安全性与确认都保住 |

盲重试的第一次 `INCR` 已经在服务端执行，只是回复丢了；客户端不知道，于是第二次又执行一次，`views=2`、`applied_count=2`。客户端最终拿到了 `status=ok` 和 `value=2`，但阅读计数多了一次，这是一次安全性违反，不是一次成功的恢复。不重试的路径让 `views=1`，服务端只执行一次，安全性没有破坏；代价是客户端只拿到 `timeout`，`availability_reduced=true`，后续流程无法确认这次计数到底有没有生效。幂等重试把 `publish-01` 作为键交给服务端，第二次到达时服务端直接返回第一次的结果 `value=1`，没有再执行 `INCR`，所以 `views=1`、`applied_count=1`，第二次仍然是 `status=ok`。这条路径不是靠「猜第一次有没有成功」，而是让服务端记住这个键对应的结果。

这里的操作类型很关键。本课用 `INCR` 而不是 `SET`，因为重复执行 `INCR` 会留下可见的差异；`SET` 写同一个标题时，重复执行可能只让 `applied_count` 增加，而值看起来没变，容易掩盖重试问题。幂等键也不是一句注释：服务端必须把键和结果一起保存，并在重复请求到达时返回同一个结果；如果保存键和保存副作用不是原子的，崩溃窗口里仍然可能重复。教学引擎用一个内存字典演示这件事，不代表生产实现已经替你处理好。

### 3. 部分失败与故障检测

分布式系统里的故障是部分失败：一台副本停了，另一台还在回答；一个客户端超时，另一个客户端可能正好成功。观察也是局部的，`client_call` 返回的是本客户端这一次等待的结果，`server_view` 返回的才是某个副本本地的状态。教学引擎用 `heartbeat_reachable()` 和 `suspects(suspect_s)` 做一个最简故障检测器：能到达的副本会更新 `last_heard`，当前时间减去 `last_heard` 达到 `suspect_s=0.05` 的副本会被列为怀疑对象。它不是独立的周期心跳线程：`client_call` 成功时会更新 `last_heard`，超时路径会调用 `heartbeat_reachable()`，所以怀疑列表是在这些调用之后观察到的。

分区实验先做一次初始 `GET r1`，得到 `status=ok`、`value=Weekly digest`、`elapsed_s=0.0`，`heard_at=0.0`。随后把 `r1` 标记为分区，再对 `r1` 做 `SET`，客户端得到 `status=timeout`、`elapsed_s=0.05`。此时 `r1_crashed=false`，`r1` 的标题仍是 `Weekly digest`，请求根本没有到达；但 `suspects_after_partitioned_write=["r1"]`，`false_suspicion=true`。检测器没有做错什么，它只是根据「多久没听到」判断，而分区和崩溃在这条信息上不可区分。`cannot_distinguish_crash_from_partition=true`。MIT 6.5840 的 `notes/l-raft.txt` 把这件事写成：计算机无法区分「server crashed」和「network broken」；网络分区还可能导致 split brain。

接下来是两条故障切换策略。保守路径不提升 `r2`，客户端继续收到 `timeout`；`r1_title=Weekly digest`、`r2_title=Weekly digest`、`split_brain=false`、`safety_violation=false`、`availability_reduced=true`。它保住了「两个副本标题一致」这个安全条件，代价是写不可用。

激进路径把怀疑当成崩溃，提升 `r2`。同时，分区另一侧还有客户端能直接写 `r1`，教学引擎用 `direct_apply` 模拟这一侧：`r1` 被写成 `Weekly digest (revised)`；客户端这一侧对 `r2` 写入 `Weekly digest (hotfix)`，得到 `status=ok`、`elapsed_s=0.0`。结果是 `r1_title=Weekly digest (revised)`、`r2_title=Weekly digest (hotfix)`、`split_brain=true`、`safety_violation=true`、`availability_reduced=false`。客户端这一侧看起来完全成功，但两个副本已经分叉。这里的 `direct_apply` 是教学装置，用来表示分区另一侧仍有写入口，不是真实客户端实现；真实系统里，如果没有多数派或租约约束，两侧各自接受写入就会产生同样的分裂。

这两条路径对应验收里要区分的东西：安全性违反是两个副本出现不同值、或同一次副作用被执行两次；可用性降低是客户端拿不到确认、写无法推进。保守路径是「安全但不可用」，激进路径是「可用但不安全」，盲重试是「最终有确认但不安全」，不重试是「安全但没有确认」，幂等重试才同时保住两边。故障检测的误报本身不等于系统出错，真正决定后果的是你拿怀疑去做什么。

## 常见误区

- **把超时当成远端事务回滚的证明。** 本课 `drop_reply` 的客户端是 `timeout`、`elapsed_s=0.05`，服务端却已经提交 `Weekly digest (revised)`、`applied_count=1`。超时只说明等待预算耗尽，不说明远端没有执行。
- **看到客户端超时一样，就认为服务端状态一样。** `drop_reply` 和 `drop_request` 的客户端字段完全相同，服务端一个已提交、一个 `applied_count=0`。`cannot_distinguish_drop_reply_from_drop_request=true`。
- **对非幂等操作盲重试。** `INCR` 第一次超时后重试，`views=2`、`applied_count=2`，客户端最终 `ok`，但计数已经错了。客户端的成功不等于操作只发生一次。
- **为了避免重复，干脆不重试，并认为这是免费的。** 不重试时 `views=1`、`safety_violation=false`，但 `availability_reduced=true`、`client_confirmation=timeout`。安全性保住了，客户端却不知道结果。
- **把心跳超时直接当成副本崩溃。** 分区实验里 `r1_crashed=false`、标题仍是 `Weekly digest`，检测器却给出 `suspects_after_partitioned_write=["r1"]` 和 `false_suspicion=true`。怀疑不是事实。
- **认为故障切换只会提高可用性。** 激进路径的 `promoted_write` 是 `status=ok`，看起来服务恢复了，代价是 `r1` 与 `r2` 标题分叉，`split_brain=true`、`safety_violation=true`。
- **认为复制和故障切换能覆盖拜占庭副本。** 拜占庭 `r1` 在 `elapsed_s=0.0` 返回 `Weekly digest`，存储里却是 `Weekly digest (revised)`；`timeout_does_not_detect_the_lie=true`。本课限定在崩溃故障，不声称覆盖恶意副本。
- **把教学 `Clock` 当成生产里的共享物理时钟。** 它只是仿真调度器，用来让 `0.05` 和 `0.08` 的先后关系可重复；真实机器之间没有这样一个共同的 `now()`。

## 设计权衡

超时预算本身是取舍。把 `timeout_s` 设得比 `delay_s=0.08` 短，客户端能更快地放弃，但 `delay_reply` 和 `delay_request` 都会变成超时，误报和不确定性增加；设得更长，晚到的回复有机会被确认，代价是故障检测和用户等待都变慢。本课选 `0.05` 对 `0.08`，是为了让「预算耗尽但消息仍在路上」稳定出现，不是为了给出一个生产建议值。

重试策略在安全性和可用性之间取舍。盲重试让客户端最终拿到 `status=ok`，但 `INCR` 的 `views=2` 说明副作用被执行了两次；不重试让 `views=1`，却只给客户端 `timeout`。幂等重试要求服务端保存键和结果，增加存储与协议复杂度，换来的是第二次请求返回第一次的结果 `value=1`、`applied_count=1`。操作本身是否幂等也很重要：写同一个标题的 `SET` 比 `INCR` 更容易做成幂等，但不能因为值相同就假设副作用只发生了一次。

故障切换在安全性和可用性之间取舍。保守路径 `split_brain=false`、`availability_reduced=true`；激进路径 `availability_reduced=false`、`split_brain=true`、`safety_violation=true`。节点的权衡是：教学系统先限定崩溃故障，不声称覆盖恶意副本。这个限定不是省略，而是边界：崩溃模型下可以用超时、心跳、复制和切换来讨论容错；拜占庭模型需要额外的假设和协议，本课不做，也不把已有实验的结论外推过去。

## 动手实践：用代理延迟或丢弃一个 KV 副本的请求，记录客户端与服务端的不同观察

本课不连接 Redis，也不启动真实网络服务；下面的副本、代理、超时和分区都由 `/workspace/atlas/lessons/practice113` 里的教学 `FailureEngine` 模拟。代码是 Python 3 标准库，`Clock` 是虚拟调度器。运行 `./test.sh` 会执行全部实验并写下 `results.json`；本课数字全部来自这份结果，是教学虚拟时钟上的测量，不是生产 RTT 或基准。

### 0. 文件结构

```
practice113/
  engine.py             # FailureEngine、Clock、Replica、ProxyPolicy 与观察结构
  app.py                # 四组实验
  test_failure.py       # 验收断言
  test.sh
  results.json          # 本次实测
```

### 1. 准备数据与目标操作

标题键是 `article:01:title`，初始值 `Weekly digest`，修订值 `Weekly digest (revised)`，应急值 `Weekly digest (hotfix)`；计数键是 `article:01:views`，从 0 开始。教学参数为 `timeout_s=0.05`、`delay_s=0.08`、`suspect_s=0.05`。实验要观察：请求或回复被丢弃时客户端看到什么、服务端实际发生了什么；延迟到预算之外时请求和回复分别在哪里落地；盲重试、不重试和幂等重试对 `views` 的影响；分区之后怀疑对象、保守切换和激进切换的差别；崩溃与拜占庭在客户端观察上的差别。

### 2. 要比较的几条路径

- `drop_reply`、`drop_request`、`delay_reply`、`delay_request` 四条路径，逐条记录客户端与服务端观察。
- 对 `INCR article:01:views` 做盲重试、不重试、带幂等键 `publish-01` 的重试。
- 先 `GET r1`，再分区 `r1` 并尝试 `SET`，记录怀疑列表；然后比较不提升 `r2` 与提升 `r2` 两种策略。
- 崩溃 `r1` 之后分别 `GET r1`、`GET r2`；再对照拜占庭 `r1` 快速返回错误标题的情况。

### 3. 运行并解读（真实输出）

```
$ ./test.sh
ALL_CHECKS_PASSED
```

下面的数字全部来自这次运行写入的 `results.json`。它们是虚拟时钟上的先后关系，不是线上超时统计，也不是网络基准。

### 4. 逐组对照真实数字

**超时与提交（`timeout_vs_commit`）**

- `timeout_s=0.05`，`delay_s=0.08`。
- `drop_reply`：客户端 `status=timeout`、`value=null`、`elapsed_s=0.05`、`applied_unknown=true`；服务端 `value=Weekly digest (revised)`、`applied_count=1`、`title_committed=true`。
- `drop_request`：客户端同样 `status=timeout`、`elapsed_s=0.05`、`applied_unknown=true`；服务端 `value=Weekly digest`、`applied_count=0`、`title_committed=false`。
- `client_timeout_observations_identical=true`、`server_observations_differ=true`、`cannot_distinguish_drop_reply_from_drop_request=true`、`timeout_is_not_proof_of_rollback=true`。
- `delay_reply`：请求已提交为 `Weekly digest (revised)`，客户端仍 `timeout`；late reply 在 `at=0.08` 到达，`value=Weekly digest (revised)`。
- `delay_request`：客户端先在 `elapsed_s=0.05` 超时，请求在 `at=0.08` 才送达并提交，`title_committed=true`。
- `cannot_distinguish_delay_from_loss_at_timeout=true`。

**重试与安全/可用（`retry_safety_vs_availability`）**

- 盲重试 `INCR article:01:views`：第一次 `status=timeout`、`elapsed_s=0.05`；第二次 `status=ok`、`value=2`、`elapsed_s=0.0`；`views=2`、`applied_count=2`、`safety_violation=true`。
- 不重试：`views=1`、`safety_violation=false`、`availability_reduced=true`、`client_confirmation=timeout`。
- 幂等键 `publish-01` 再重试：第一次 `status=timeout`、`elapsed_s=0.05`；第二次 `status=ok`、`value=1`、`elapsed_s=0.0`；`views=1`、`applied_count=1`、`safety_violation=false`。

**分区与故障检测（`partition_and_failure_detection`）**

- 初始 `GET r1` 得到 `Weekly digest`，`status=ok`、`elapsed_s=0.0`、`heard_at=0.0`。
- 分区 `r1` 后，对 `r1` 的 `SET` 超时，`elapsed_s=0.05`；`r1_crashed=false`，`r1` 标题仍是 `Weekly digest`，请求没有到达；`suspects_after_partitioned_write=["r1"]`，`false_suspicion=true`。
- 保守路径：不提升 `r2`，`client_status=timeout`，`r1_title=Weekly digest`、`r2_title=Weekly digest`、`split_brain=false`、`safety_violation=false`、`availability_reduced=true`。
- 激进路径：`direct_apply` 把 `r1` 写成 `Weekly digest (revised)`，客户端一侧提升 `r2` 并写入 `Weekly digest (hotfix)`，`status=ok`、`elapsed_s=0.0`；`split_brain=true`、`safety_violation=true`、`availability_reduced=false`。

**崩溃与拜占庭（`crash_vs_byzantine`）**

- 崩溃：`GET r1` 为 `status=timeout`、`elapsed_s=0.05`；`GET r2` 为 `status=ok`、`value=Weekly digest (revised)`、`elapsed_s=0.0`；`r1` 仓库仍是 `Weekly digest (revised)`，不撒谎。`crash_is_fail_stop=true`。
- 拜占庭：`r1` 在 `elapsed_s=0.0` 回复 `Weekly digest`，但 `r1_store` 仍是 `Weekly digest (revised)`；`r2` 回复 `Weekly digest (revised)`；`clients_see_different_values=true`、`timeout_does_not_detect_the_lie=true`。`byzantine_is_outside_crash_model=true`，`failover_covers_crash_not_byzantine_lie=true`。
- `teaching_limited_to_crash_failures=true`、`does_not_claim_to_cover_malicious_replicas=true`。

### 5. 对照验收标准

```
# 验收 1：解释超时后请求可能已提交
# timeout_s=0.05，delay_s=0.08。
# drop_reply：client status=timeout，elapsed_s=0.05，applied_unknown=true；
#   server value=Weekly digest (revised)，applied_count=1，title_committed=true。
# drop_request：client status=timeout，elapsed_s=0.05，applied_unknown=true；
#   server value=Weekly digest，applied_count=0，title_committed=false。
# client_timeout_observations_identical=true，
# server_observations_differ=true，
# cannot_distinguish_drop_reply_from_drop_request=true，
# timeout_is_not_proof_of_rollback=true。
# delay_reply：请求已提交为 Weekly digest (revised)，客户端仍 timeout；
#   late reply 在 at=0.08 到达，value=Weekly digest (revised)。
# delay_request：客户端先在 0.05 超时，请求在 at=0.08 才送达并提交，
#   title_committed=true。cannot_distinguish_delay_from_loss_at_timeout=true。

# 验收 2：区分安全性违反与可用性降低
# 盲重试 INCR article:01:views：第一次 timeout，第二次 ok 且 value=2，
#   views=2，applied_count=2，safety_violation=true。
# 不重试：views=1，safety_violation=false，availability_reduced=true，
#   client_confirmation=timeout。
# 幂等键 publish-01 再重试：views=1，applied_count=1，
#   第二次 ok 且 value=1，safety_violation=false。
# 分区后保守路径：split_brain=false，safety_violation=false，
#   availability_reduced=true。
# 分区后激进路径：split_brain=true，safety_violation=true，
#   availability_reduced=false。
# 安全性违反看副本是否分叉、副作用是否重复；
# 可用性降低看客户端是否拿不到确认。
# 验收 1 汇总：acceptance_timeout_may_already_be_committed=true，
#   pitfall_timeout_is_not_rollback_proof=true。
# 验收 2 汇总：acceptance_distinguish_safety_vs_availability=true，
#   safety_violation_on_blind_retry=true，
#   availability_reduced_when_not_retrying=true，
#   idempotent_retry_preserves_safety_and_gets_ok=true，
#   conservative_preserves_safety=true，aggressive_violates_safety=true。
```

### 6. 自己动手的扩展练习

- 把 `timeout_s` 改成大于 `delay_s=0.08` 的值，再跑 `delay_reply` 和 `delay_request`，看客户端是否变成 `status=ok`；`drop_reply` 和 `drop_request` 仍然会超时。新数字要自己跑出来，不要拿本课 `results.json` 的数字当成新配置的结果。
- 把 `SET` 改成两个不同的标题再盲重试，观察 `applied_count` 变成 2、最终只留下最后一次的值；再对比 `INCR`，说明「值看起来一样」为什么不能证明副作用只发生一次。
- 把 `suspect_s` 从 `0.05` 调小或调大，观察分区后 `suspects` 的时机变化；想一想更短的怀疑阈值换来了什么，又让哪种误报更常见。
- 给盲重试加上 `idempotency_key`，看 `applied_count` 是否从 2 回到 1，并检查第二次返回的是第一次的结果还是重新计算的结果。

### 7. 和生产系统的对应关系（不要记错边界）

教学引擎没有 TCP、没有真实 socket、没有 Raft、没有 PBFT、没有多数派和租约。`ProxyPolicy` 的 `drop_request`、`drop_reply`、`delay_request_s`、`delay_reply_s`、`partitioned` 是确定性故障注入，不是网络模拟器。`Clock` 是单进程虚拟时钟，不能用来推断生产里的时钟偏移、RTT 分布或超时重传行为。`applied_count` 统计的是单个副本本地执行过的请求数，不是跨副本的提交次数；`direct_apply` 是模拟分区另一侧写入的教学装置，不是客户端 API。幂等键的去重表在内存里，没有持久化和原子性保证；真实系统需要把键、结果和副作用放在同一个持久化边界内考虑。`FailureEngine` 不实现共识，也不评估 PBFT 的性能；它只展示崩溃与拜占庭的边界、超时与提交的分离，以及安全性/可用性在重试和故障切换里的取舍。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 MIT 6.5840 — Distributed Systems 中与 Failure models 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 实际打开的页面如下；它们都在 `pdos.csail.mit.edu/6.5840/` 下，入口与讲义均返回 200 且无跳转。

- [MIT 6.5840 — Distributed Systems](https://pdos.csail.mit.edu/6.5840/)（节点给出的入口）：课程主页，用来进入日程和讲义。本课只引用这个入口与下面核对过的讲义，不把日程页当成节点 `resources` 数组之外的资源条目。
- [schedule.html](https://pdos.csail.mit.edu/6.5840/schedule.html)：Spring 2026 日程。LEC 21 链到 `notes/l-bft.txt`，可以核对拜占庭讲义在课程里的位置。
- [notes/l01.txt](https://pdos.csail.mit.edu/6.5840/notes/l01.txt)：2026 Lecture 1，10886 字节。MapReduce 假设 fail-stop，错误计算被归为「too bad」；用它对照本课：教学实验只在崩溃（fail-stop）模型里讨论超时和切换，错误计算与撒谎不在这个假设里；即便在崩溃模型下，超时本身仍然不能把崩溃和分区分开。
- [notes/l-rpc.txt](https://pdos.csail.mit.edu/6.5840/notes/l-rpc.txt)：2026 Lecture 2。客户端看不到回复时，并不知道服务端是否见到过请求；用它核对本课 `drop_reply` 与 `drop_request` 的客户端观察为什么相同。
- [notes/l-raft.txt](https://pdos.csail.mit.edu/6.5840/notes/l-raft.txt)：2026 Lecture 6。计算机无法区分「server crashed」和「network broken」，网络分区可能导致 split brain；复制适合单个副本的 fail-stop，不一定能对付缺陷或误操作。用它核对本课 `false_suspicion` 与保守/激进路径。
- [notes/l-bft.txt](https://pdos.csail.mit.edu/6.5840/notes/l-bft.txt)：讲义标题仍写 2024 Lecture 22，但 Spring 2026 日程的 LEC 21 链到这个文件。课程此前一直假设 fail-stop；拜占庭是 buggy、不遵守协议、或被攻击者控制的副本。本课不实现 PBFT。
- [notes/65840-pbft.pdf](https://pdos.csail.mit.edu/6.5840/notes/65840-pbft.pdf)：PBFT 讲义。用于理解拜占庭容错需要的额外假设和代价；本课不实现，也不评估其性能。
- [papers/castro-practicalbft.pdf](https://pdos.csail.mit.edu/6.5840/papers/castro-practicalbft.pdf)：Castro–Liskov 的 PBFT 论文。作为拜占庭模型的原始阅读，不要把它和本课的崩溃模型实验混在一起。
- [papers/bft-faq.txt](https://pdos.csail.mit.edu/6.5840/papers/bft-faq.txt)：BFT FAQ。用于核对拜占庭容错常见问题的表述，避免把「复制」直接说成「能对付撒谎副本」。

与教学实验的差异要写清楚：这些讲义是完整课程语境，教学引擎只抽取了超时、丢弃、延迟、分区、崩溃和拜占庭桩这几件事；它没有实现 Raft 的选举与日志复制，也没有实现 PBFT 的消息轮次和投票。`l01.txt` 里的 MapReduce 用 fail-stop 假设，`l-rpc.txt` 讨论 RPC 语义，`l-raft.txt` 讨论复制与分区，`l-bft.txt` 讨论拜占庭副本；本课把它们当作模型定义和边界的核对来源，而不是把教学引擎当成这些系统的实现。

## 自测题

**1.** 客户端对 `SET article:01:title` 得到 `status=timeout`、`elapsed_s=0.05`，能不能据此认为服务端事务已经回滚？`drop_reply` 和 `drop_request` 的客户端观察为什么无法区分？

<details>
<summary>答案</summary>

不能。超时只说明客户端 `timeout_s=0.05` 的等待预算耗尽，不证明远端没有执行。本课 `drop_reply` 的客户端是 `status=timeout`、`elapsed_s=0.05`、`applied_unknown=true`，服务端却已经提交 `Weekly digest (revised)`、`applied_count=1`、`title_committed=true`；`drop_request` 的客户端字段完全一样，服务端却是 `Weekly digest`、`applied_count=0`、`title_committed=false`。`client_timeout_observations_identical=true`、`server_observations_differ=true`，所以 `cannot_distinguish_drop_reply_from_drop_request=true`。更麻烦的是 `delay_request`：客户端在 `0.05` 超时，请求在 `at=0.08` 才送达并提交，`title_committed=true`，说明超时之后还可能有新的提交。节点的误区正是把超时当作远端事务回滚的证明。

</details>

**2.** 盲重试 `INCR article:01:views` 为什么算安全性违反？不重试不是更安全吗？幂等重试怎样同时保住安全性和可用性？

<details>
<summary>答案</summary>

盲重试的第一次 `INCR` 已经在服务端执行，只是回复丢失；客户端不知道，第二次又执行一次，得到 `views=2`、`applied_count=2`，第二次 `status=ok`、`value=2`。客户端最终有确认，但阅读计数多了一次，这是安全性违反，不是成功的恢复。不重试确实保住了 `views=1`、`safety_violation=false`，但客户端只拿到 `timeout`，`availability_reduced=true`，无法确认这次计数是否生效。幂等键 `publish-01` 让服务端记住第一次的结果：第二次到达时直接返回 `value=1`，不再执行 `INCR`，所以 `views=1`、`applied_count=1`，第二次仍然是 `status=ok`。安全性看副作用是否只发生一次、副本是否分叉；可用性看客户端能否拿到确认。幂等重试两边都保住了，代价是服务端要持久化键和结果，并处理去重与副作用之间的原子性。

</details>

**3.** 崩溃的 `r1` 对 `GET` 是 `timeout`，拜占庭的 `r1` 却在 `elapsed_s=0.0` 返回 `Weekly digest`。这两种情况在故障检测上有什么差别？为什么分区误报会导致脑裂？

<details>
<summary>答案</summary>

崩溃的 `r1` 不再回答，`GET r1` 得到 `status=timeout`、`elapsed_s=0.05`；`r2` 返回真实的 `Weekly digest (revised)`，而 `r1` 的存储仍是 `Weekly digest (revised)`，它没有撒谎。拜占庭的 `r1` 继续运行，在 `elapsed_s=0.0` 返回 `Weekly digest`，但 `r1_store` 仍是 `Weekly digest (revised)`；`r2` 返回修订版，`clients_see_different_values=true`。它没有超时，`timeout_does_not_detect_the_lie=true`，所以基于等待预算的检测抓不到它；本课只在崩溃模型里做容错，`teaching_limited_to_crash_failures=true`。

分区时，检测器看到 `r1` 一段时间没有心跳，就把它列为怀疑对象，但实验里 `r1_crashed=false`、标题仍是 `Weekly digest`，`false_suspicion=true`。如果保守处理，不提升 `r2`，`split_brain=false`、`availability_reduced=true`；如果把怀疑当成崩溃并提升 `r2`，同时分区另一侧还能写 `r1`，就会得到 `r1_title=Weekly digest (revised)`、`r2_title=Weekly digest (hotfix)`、`split_brain=true`、`safety_violation=true`。`l-raft.txt` 说计算机无法区分 crashed 和 network broken，分区可能导致 split brain；故障检测的误报本身不是结论，拿怀疑去做什么才决定安全性和可用性。

</details>

## 推荐资源

- [MIT 6.5840 — Distributed Systems](https://pdos.csail.mit.edu/6.5840/)（MIT 6.5840，英文文档，进阶，免费）。重点阅读崩溃与拜占庭故障的边界、RPC 超时和 Raft 对分区与 fail-stop 的假设，用于核对本节点实验的语义和边界。2026-11-13 该入口返回 200，无跳转；同一次核对建议打开 `schedule.html` 与 `notes/l01.txt`、`notes/l-rpc.txt`、`notes/l-raft.txt`、`notes/l-bft.txt` 讲义。这些讲义是核对路径，不是节点 `resources` 数组里的额外条目。

## 本课小结

- 崩溃是停住不回答，拜占庭是继续回答但可能给出错误值。崩溃实验里 `GET r1` 超时 `elapsed_s=0.05`、`r2` 返回 `Weekly digest (revised)`，`r1` 存储仍是修订版；拜占庭 `r1` 在 `elapsed_s=0.0` 返回 `Weekly digest`，存储却是修订版，`timeout_does_not_detect_the_lie=true`。本课限定在崩溃故障，不声称覆盖恶意副本。
- 超时只说明等待预算耗尽，不证明远端没有执行。`drop_reply` 的客户端 `status=timeout`、`elapsed_s=0.05`，服务端已经提交 `applied_count=1`；`drop_request` 的客户端观察完全相同，服务端 `applied_count=0`。`delay_request` 的请求在 `at=0.08` 才送达并提交。盲重试让 `views=2`、`safety_violation=true`；不重试让 `views=1`、`availability_reduced=true`；幂等键 `publish-01` 让 `views=1`、`applied_count=1`，同时拿到 `status=ok`。
- 故障是部分失败，怀疑不等于崩溃。分区后 `suspects_after_partitioned_write=["r1"]`、`false_suspicion=true`，但 `r1_crashed=false`、标题仍是 `Weekly digest`。保守路径 `split_brain=false`、`availability_reduced=true`；激进路径 `split_brain=true`、`safety_violation=true`、`availability_reduced=false`。两次验收分别要求说清「超时后可能已提交」和「安全性违反与可用性降低的区别」。
- 学完后可以用三句话自检：超时之后请求为什么可能已经提交，`drop_reply` 与 `drop_request` 的客户端为什么看起来一样；盲重试、不重试和幂等重试分别把安全性、可用性放在了哪里；崩溃超时与拜占庭快速撒谎有什么差别，分区误报又怎样走到脑裂。三句都能答上来，并且能指出 `practice113/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验字段，再回到 `l-rpc.txt` 关于「客户端看不到回复时不知道服务端是否见过请求」、`l-raft.txt` 关于「无法区分 crashed 与 network broken」和 `l-bft.txt` 关于「课程此前假设 fail-stop、拜占庭是另一类副本」的那几句，不要只把「超时要重试」或「故障要切换」背下来。
