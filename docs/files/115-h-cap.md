# 第 115/208 课：CAP 与分区下的选择

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-cap` |
| 英文名 | CAP theorem |
| 领域 / 阶段 | H / 第 3 阶段 |
| 子类 | 理论与故障模型 |
| 难度 | 进阶 |
| 前置节点 | [`h-consistency`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html) |
| 标签 | `CAP theorem`、`Distributed` |
| 参考资料 | [Gilbert & Lynch · Perspectives on the CAP Theorem](https://dspace.mit.edu/entities/publication/dfcb6512-af44-4fc9-8e8b-1a8f28f1b53a)（Gilbert & Lynch，英文论文，进阶，免费；2026-11-13 该条目页返回 200，无跳转，564296 字节。题名 Perspectives on the CAP Theorem，作者 Gilbert, Seth 与 Lynch, Nancy A.，出版 2012-02，出版方 Institute of Electrical and Electronics Engineers，DOI 10.1109/MC.2011.389。页内标注的 PDF 地址当日返回 429；正文按 CSAIL 公开副本阅读，见「源码与文档阅读」） |
| 摘要 | 在异步网络出现分区时，无法同时保证线性一致性与每个非故障节点对每个请求都响应的可用性。 |

> **边界说明**：本课里的「内容后台」「文章标题 `Weekly digest`」「键 `article:01:title`」都是**教学假设**，用来让两个副本、两次写和用户可见结果变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `CapEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是 Raft，不是 Paxos，也不是 Jepsen**。它没有 TCP，没有真实 socket，没有真实丢包；`Clock` 是虚拟时钟，让实验可以重复。`isolate()` 是一个全知的、显式的分区开关，真实异步网络没有这样的预言机。官方材料已于 **2026-11-13** 用 curl 核对：dspace 条目页 `https://dspace.mit.edu/entities/publication/dfcb6512-af44-4fc9-8e8b-1a8f28f1b53a` 返回 200、无跳转、564296 字节；该页标注的 PDF 地址 `https://dspace.mit.edu/bitstreams/8b57ab12-0c57-413d-9502-0f60ec6104ae/download` 当日返回 429（Web Firewall，服务过载），不能写成已经从 dspace 下到了 PDF；`https://doi.org/10.1109/MC.2011.389` 经一次跳转后 IEEE 页面返回 500、0 字节，不能当作已读来源。教学实验里的时间和标题都来自 `/workspace/atlas/lessons/practice115/results.json`，是教学虚拟时钟上的数字，不是生产 RTT，也不是任何系统在真实网络上的基准。

## 导读

### 一个直觉：分区发生时，编辑保存到底该成功还是该失败

内容后台（教学假设）要把已发布文章的标题从 `Weekly digest` 改成 `Weekly digest (revised)`。系统有两台副本，`r1` 和 `r2`。如果两台副本之间的通信断了，`r1` 上的写还能不能返回 `ok`？如果返回 `ok`，另一侧又在同一时间把标题改成 `Weekly digest (hotfix)`，用户最终会看到哪个标题？

CAP 定理把这个问题逼到一个很窄的条件里：异步网络出现分区时，不能既保证线性一致性，又保证每个非故障节点对每个请求都做完并给出符合对象语义的响应。本课不把「三选二」当成架构口号，而是把两台副本真的隔开，分别执行「拒绝写」和「接受冲突写」两种策略，看用户究竟观察到什么。

### 接上前置

前置的 [第 114 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html) `h-consistency` 已经把一致性落到带调用时间、返回时间的操作历史上：线性一致要求存在一个尊重实时顺序、又符合寄存器单线程语义的全序。CAP 讨论的「一致性」在本定理的语境里取原子性：每个操作在请求与响应之间存在单一瞬间，看起来在该瞬间发生；等价于在客户端看来，所有操作就像由一台集中式服务器执行。这与第 114 课的线性一致性是同一类安全性。第 114 课给出的不可线性化历史，正好可以用来判断本课「接受冲突写」路径为什么不满足 CAP 里的一致性。

### 一次 SET/GET 走进 CapEngine 之后

教学引擎维护两台副本 `r1`、`r2`，一个键的读写寄存器，以及两个写策略。`require_peer` 要求两边都应用这次写才返回 `ok`，等不到对端就拒绝；`local_first` 先在本地副本应用并返回，另一台副本稍后异步抄写。分区由 `isolate()` 显式打开，副本本身并不崩溃。`GET` 只读被指定的那台副本，返回的是该副本本地已提交的值。

每条操作记成一条 `RecordedOp`：`inv_s` 是调用时间，`ret_s` 是返回时间；`status` 取 `ok` 或 `rejected`，`note` 记录 `committed_on_both`、`applied_locally`、`peer_unreachable`、`peer_ack_after_wait_s` 等。`cap_available(ops)` 检查 CAP 意义上的可用性，`sla_got_a_reply(ops)` 只检查有没有回复。两个函数分开，是为了避免把「接口返回了一个状态码」误当成定理里的可用性。

### 教学场景（教学假设）

假设内容后台用键 `article:01:title` 保存已发布文章的标题，初值是 `Weekly digest`；一次编辑写成 `Weekly digest (revised)`，另一侧的冲突写写成 `Weekly digest (hotfix)`。引擎参数是 `local_step_s=0.01`、`replicate_delay_s=0.08`、`wait_s=0.05`。无分区的 `require_peer` 路径在 `results.json` 里记录的 `wait_s` 是 `0.20`，这样 `0.08` 的复制延迟能落在等待窗口内；分区与「延迟看起来像分区」的实验使用 `0.05`。这些数字都是虚拟时钟上的教学值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **分区条件**：分区容忍是对底层通信不可靠的陈述，不是可以随手关掉的功能开关；异步网络里，足够长的延迟在实践中也可以看成丢失。
2. **CAP 中可用性的严格定义**：可用不是「回了一个错误码」或「探针很快有响应」，而是到达非故障节点的请求把这次读或写做完，并返回符合原子寄存器语义的结果。
3. **PACELC 的延迟视角**：有分区时在可用性与一致性之间权衡；没有分区时，系统仍在延迟与一致性之间权衡，这一支不由 CAP 直接决定。

## 学习目标

- 能解释分区条件及适用边界。
- 能独立完成实践：把两个副本隔离，分别实现拒绝写与接受冲突写的策略。

## 为什么后端要懂

公众号内容后台（教学假设）修改已发布标题后，编辑会立刻看到保存结果，预览页和线上读流量可能落到不同副本。如果保存接口在分区期间仍然返回 `ok`，但两台副本各自接受了不同的标题，用户就会在不同页面看到不同标题，而且网络恢复后也未必自动合并。反过来，如果接口拒绝这次保存，用户至少不会以为自己已经改成功，但编辑在分区期间就写不进去。

CAP 要回答的正是这笔交易在哪一侧付出代价。它不告诉你业务应该选哪边，也不保证无分区时快路径的延迟。它只说明：当分区真的发生，且请求到达的是没有故障的节点时，不能同时要求这次读写按原子寄存器语义完成，又要求每个请求都得到服务。把业务 SLA 的「接口有响应」当成 CAP 可用性，会让团队在分区时误以为自己还满足定理，实际上只是把冲突推迟到用户可见的地方。

## 核心知识点

### 1. 分区条件

Gilbert 与 Lynch 把分区容忍写成对底层系统的陈述，而不是可以任意关掉的功能开关：服务器之间的通信不可靠，服务器可能被分成彼此无法通信的若干组；消息可能被延迟，有时永远丢失。足够长的延迟在实践中也可以看成丢失。定理的陈述是：在会发生通信故障的网络中，任何 web 服务都不可能实现一个对每个请求都保证响应的原子读写共享内存。

论文的证明概要很具体。把服务器分成 `{p1}` 与 `{p2,...,pn}`。客户向 `p2` 发读请求。`p2` 无法区分「先前有一次对 `p1` 的写 `v1` 且 `p1` 已返回 ok」和「先前有一次对 `p1` 的写 `v2` 且 `p1` 已返回 ok」。`p2` 要么最终返回，并冒返回错误值的风险；要么永不返回。二者必居其一，不能既返回正确值又保证响应。

论文还给出一个更接近异步网络的推论：若通信是异步的，进程事先没有消息投递时延的上界，同样的困境即使在没有消息丢失、即没有真实分区的执行里也可能出现。若 `p1` 到 `p2` 的延迟大到让 `p2` 以为发生了分区，它可能返回错误值。因此，不可能在「没有分区时保证一致性，只在发生分区时才返回错误答案」的同时，还保证可用性。

教学引擎把这条推论做成 `delay_looks_like_partition`。场景里 `actual_partition=false`，但 `replicate_delay_s=0.08` 大于 `wait_s=0.05`。`SET` 从 `0.0` 调用，到 `0.05` 返回 `rejected`，`note=peer_ack_after_wait_s`；迟来的确认本应在 `0.09` 到达，但值没有被应用。之后 `GET r1` 从 `0.09` 到 `0.11`、`GET r2` 从 `0.11` 到 `0.13`，两边仍然都是 `Weekly digest`。`rejected_despite_no_partition=true`，`value_not_applied_after_late_ack=true`。没有真实分区，单靠有限等待也无法可靠区分「分区」和「只是慢」；这正是分区条件在异步网络里的适用边界。真实系统没有 `isolate()` 这样的全知开关，它只能在超时之后做判断，而判断本身可能出错。

### 2. CAP 中可用性的严格定义

Gilbert 与 Lynch 把可用性写成：每个请求最终都收到一个响应。他们特意说明，为了 CAP，哪怕只要求「最终有响应」也足以构成问题；在多数真实系统里，足够晚的响应和永远不返回差不多糟，但定理本身并不以延迟上界来定义可用性。

落到原子读写共享内存上，这个响应必须是这次读或写完成后的结果：读要返回符合寄存器语义的值，写要真的生效。一个 `rejected` 状态说明这次写没有发生；它虽然是一个网络层回复，却不是定理意义上的「请求被服务」。SLA 探针很快拿到一个状态码，也只能说明 `sla_got_a_reply=true`，不能说明 `cap_available=true`。节点摘要里的「每个非故障节点对每个请求都响应的可用性」，要按这个严格含义理解。

分区 + 拒绝写的实验把这条定义摆出来。`isolate()` 之后，把 `Weekly digest (revised)` 写到 `r1`：`SET` 从 `0.0` 调用，到 `0.05` 返回，`status=rejected`，`note=peer_unreachable`，两边仍是 `Weekly digest`。再把 `Weekly digest (hotfix)` 写到 `r2`：`SET` 从 `0.06` 到 `0.11`，同样 `rejected`。随后 `GET r1` 从 `0.11` 到 `0.13` 读到 `Weekly digest`，`GET r2` 从 `0.13` 到 `0.15` 也读到 `Weekly digest`。`conflict=false`，`cap_available_for_sets=false`，`sla_got_a_reply_for_sets=true`，`pitfall_sla_is_not_cap_availability=true`。两台副本都没有崩溃，`replicas_were_not_failed=true`；它们只是无法互相通信。`linearizable_no_successful_conflicting_writes=true`：没有一次成功的冲突写，历史没有被两个不同的成功值撕开。用户可见结果是两次写都被拒绝，标题仍是 `Weekly digest`。这就是分区时放弃这次写，从而避免冲突、保住线性一致性，代价正是牺牲可用性。

同一分区下换 `local_first`，得到另一种用户可见结果。`SET` 修订标题到 `r1`，从 `0.0` 到 `0.02` 返回 `ok`，此时 `r1` 是 `Weekly digest (revised)`、`r2` 是 `Weekly digest`。`SET` hotfix 到 `r2`，从 `0.02` 到 `0.04` 返回 `ok`，此时 `r1` 是 `Weekly digest (revised)`、`r2` 是 `Weekly digest (hotfix)`。`GET r1` 从 `0.04` 到 `0.06` 读到修订标题，`GET r2` 从 `0.06` 到 `0.08` 读到 hotfix。`heal_s=0.08` 之后冲突仍在：`GET r1` 从 `0.08` 到 `0.10` 仍是 `Weekly digest (revised)`，`GET r2` 从 `0.10` 到 `0.12` 仍是 `Weekly digest (hotfix)`，`conflict_after_heal=true`。这一次 `cap_available_for_sets=true`：两次写都做完了，都返回了 `ok`；但 `linearizable=false`，原因是同一个寄存器上出现了两个成功的不同写，后面的读分别返回了这两个不同的值。用户可见结果是两次写都成功，两侧标题不同，愈合后冲突仍可见。引擎愈合后不自动合并冲突，这一点必须说清楚，否则会把「网络恢复」误当成「数据自动恢复一致」。

两侧对照就是 CAP 的选择：拒绝写保住一致性、放弃这次写的可用性；接受冲突写保住写的可用性、放弃线性一致性。业务允许陈旧多久、允许冲突由谁解决、停写范围多大，决定了该选哪边；定理本身不替业务做这个决定。

### 3. PACELC 的延迟视角

节点把「PACELC 的延迟视角」列为第三条要点。PACELC 的表述不在 Gilbert & Lynch 这篇 Perspectives 论文里，本课按同一期 IEEE Computer（2012 年 2 月）中 Daniel Abadi 的文章核对：if there is a partition (P), how does the system trade off availability and consistency (A and C); else (E), when the system is running normally in the absence of partitions, how does the system trade off latency (L) and consistency (C)? 读音是 pass-elk。

无分区的两条路径把 else 分支的数字算了出来。`require_peer` 要求两边都提交才返回：`SET` 从 `0.0` 调用，到 `0.10` 返回 `ok`，`latency=0.10`；返回时 `r1`、`r2` 都是 `Weekly digest (revised)`。随后 `GET r1` 从 `0.10` 到 `0.12`、`GET r2` 从 `0.12` 到 `0.14`，都读到修订标题。`cap_available=true`，`conflict=false`。这一块在 `results.json` 里记录的 `wait_s` 是 `0.20`，因为 `0.08` 的复制延迟要落在等待窗口内。

`local_first` 先在本地确认：`SET` 从 `0.0` 调用，到 `0.02` 返回 `ok`，`latency=0.02`；返回时 `r1` 是 `Weekly digest (revised)`、`r2` 仍是 `Weekly digest`。一次打到 `r2` 的 `GET` 从 `0.02` 到 `0.04` 返回旧标题 `Weekly digest`。异步抄写在 `0.09` 生效，之后两边都是修订标题；再一次 `GET r2` 从 `0.09` 到 `0.11` 返回 `Weekly digest (revised)`。`cap_available=true`，`conflict=false`，但写返回之后到抄写生效之前，读 `r2` 会看到旧标题。

两条路径都没有分区，延迟分别是 `0.10` 与 `0.02`，两次 `SET` 都是 `ok`，`acceptance_cap_does_not_decide_latency_without_partition=true`。这是 PACELC 在 else 分支上的延迟与一致性权衡，不是 CAP 在无分区时给出的强制选择。CAP 只在分区条件成立时谈可用性与一致性；把 `0.10` 对 `0.02` 的差别说成「CAP 三选二的结果」，是把两个分支混在一起。

Gilbert 与 Lynch 第 4.1 节以 Chubby 为例：在网络可靠、没有分区时，服务可以保证强一致性并推进；分区时服务会不可用。无分区时还可以把常见路径做快，但这是工程优化，不是 CAP 在无分区时规定的延迟。这段例子正好说明，为什么无分区的快慢要交给 PACELC 的 else 分支来讨论，而不是继续用 CAP 的口号解释。

## 常见误区

节点写出的误区是：把业务 SLA 的可用性等同 CAP 定义。分区 + 拒绝写的实验里，两次 `SET` 都在 `wait_s` 之后返回了 `rejected`，SLA 探针确实拿到了回复，`sla_got_a_reply_for_sets=true`；但 `cap_available_for_sets=false`，`pitfall_sla_is_not_cap_availability=true`。回复存在，不等于请求被完成。把「接口没有超时」「监控探针有响应」当成 CAP 可用性，会在分区时得出错误结论：以为系统还满足定理，实际上写已经被放弃。

另一个相关误读是把 CAP 当成日常架构口号，认为任何系统都必须「三选二」，或者认为无分区时 CAP 也在决定延迟。无分区的 `0.10` 与 `0.02` 两条路径都可用、都一致到各自承诺的程度，差别来自 PACELC 的 else 分支。CAP 不直接决定这个延迟，也不要求你在无分区时牺牲其中一项。

## 设计权衡

节点给出的权衡是：选择必须结合业务允许的陈旧、冲突与停写范围。`tradeoff_choice_depends_on_staleness_conflict_and_write_outage=true`。

拒绝写在分区时把写停掉，用户看到 `rejected`，标题保持 `Weekly digest`；好处是没有成功写入的冲突值，`linearizable_no_successful_conflicting_writes=true`，恢复后不需要合并两个都成功过的标题；代价是分区期间编辑完全写不进去，停写范围就是所有走 `require_peer` 的写。

接受冲突写让两次写都返回 `ok`，用户看到两侧标题不同，`r1` 是 `Weekly digest (revised)`、`r2` 是 `Weekly digest (hotfix)`；愈合后冲突仍在，`conflict_after_heal=true`，引擎不自动合并。好处是分区期间写仍然可用，`cap_available_for_sets=true`；代价是 `linearizable=false`，以及业务必须自己定义冲突解决规则：哪一侧赢、是否需要人工合并、用户在这段时间看到哪个标题。

无分区的选择则是另一笔账。`require_peer` 用 `0.10` 的写延迟换「返回时两边都已是修订标题」；`local_first` 用 `0.02` 的写延迟换「返回后短时间内读 `r2` 仍可能看到旧标题」。这不是 CAP 的分区分支，而是 PACELC 的 else 分支：在没有分区时，仍然要在延迟与一致性之间选。两台副本、提交要求两边都写入，相当于法定人数为 2；副本数或法定人数一变，这些数字和结论都要重新算，不能直接搬到别的拓扑。

## 动手实践：把两个副本隔离，分别实现拒绝写与接受冲突写

配套代码在 `/workspace/atlas/lessons/practice115/`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice115
./test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写入 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT，也不是基准测试。

**1. 先看无分区的两条确认路径。** `no_partition_wait_both` 使用 `require_peer`：`SET` 从 `0.0` 到 `0.10` 返回 `ok`，`latency=0.10`，返回时 `r1`、`r2` 都是 `Weekly digest (revised)`；`GET r1` 从 `0.10` 到 `0.12`、`GET r2` 从 `0.12` 到 `0.14`，都读到修订标题。`no_partition_local_ack` 使用 `local_first`：`SET` 从 `0.0` 到 `0.02` 返回 `ok`，`latency=0.02`，返回时 `r1` 是修订标题、`r2` 是 `Weekly digest`；`GET r2` 从 `0.02` 到 `0.04` 读到旧标题；异步抄写在 `0.09` 生效，`GET r2` 从 `0.09` 到 `0.11` 读到修订标题。`no_partition_latency` 汇总出 `0.10` 与 `0.02` 两个延迟，`both_sets_ok=true`，`acceptance_cap_does_not_decide_latency_without_partition=true`。

**2. 分区 + 拒绝写。** `partition_refuse_write` 先 `isolate()`。`SET` 修订标题到 `r1`，从 `0.0` 到 `0.05`，`status=rejected`，`note=peer_unreachable`，两边仍是 `Weekly digest`。`SET` hotfix 到 `r2`，从 `0.06` 到 `0.11`，同样 `rejected`。`GET r1` 从 `0.11` 到 `0.13`、`GET r2` 从 `0.13` 到 `0.15`，都读到 `Weekly digest`。`conflict=false`，`cap_available_for_sets=false`，`sla_got_a_reply_for_sets=true`，`pitfall_sla_is_not_cap_availability=true`，`linearizable_no_successful_conflicting_writes=true`，`replicas_were_not_failed=true`。用户可见：两次写都被拒绝，标题仍是 `Weekly digest`。

**3. 分区 + 接受冲突写。** `partition_accept_conflict` 使用 `local_first` 并 `isolate()`。`SET` 修订标题到 `r1`，从 `0.0` 到 `0.02` 返回 `ok`，此时 `r1=Weekly digest (revised)`、`r2=Weekly digest`；`SET` hotfix 到 `r2`，从 `0.02` 到 `0.04` 返回 `ok`，此时 `r1=Weekly digest (revised)`、`r2=Weekly digest (hotfix)`。`GET r1` 从 `0.04` 到 `0.06` 读到修订标题，`GET r2` 从 `0.06` 到 `0.08` 读到 hotfix。`heal_s=0.08` 后冲突仍在：`GET r1` 从 `0.08` 到 `0.10` 仍是修订标题，`GET r2` 从 `0.10` 到 `0.12` 仍是 hotfix；`conflict_after_heal=true`。`linearizable=false`，`cap_available_for_sets=true`。用户可见：两次写都成功，两侧标题不同，愈合后冲突仍可见。`acceptance_show_both_strategies_user_visible=true` 由这两条分区路径共同满足。

**4. 让延迟看起来像分区。** `delay_looks_like_partition` 里 `actual_partition=false`，但 `replicate_delay_s=0.08` 大于 `wait_s=0.05`。`SET` 从 `0.0` 到 `0.05` 返回 `rejected`，`note=peer_ack_after_wait_s`；迟来的确认本应在 `0.09` 到达，但值没有被应用，之后两边仍是 `Weekly digest`。`GET r1` 从 `0.09` 到 `0.11`、`GET r2` 从 `0.11` 到 `0.13` 都是 `Weekly digest`。`rejected_despite_no_partition=true`，`cap_available_for_set=false`，`sla_got_a_reply=true`。这条实验用来说明分区条件的适用边界：异步网络里有限等待并不能可靠区分「分区」和「只是慢」。

**5. 核对验收字段。** `acceptance_cap_does_not_decide_latency_without_partition=true`，`acceptance_show_both_strategies_user_visible=true`，`pitfall_sla_is_not_cap_availability=true`，`tradeoff_choice_depends_on_staleness_conflict_and_write_outage=true`。四个字段都能在 `results.json` 顶层找到，实验脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Gilbert & Lynch — CAP 论文中与 CAP theorem 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 的实际打开情况如下。

- 节点给出的 [dspace 条目页](https://dspace.mit.edu/entities/publication/dfcb6512-af44-4fc9-8e8b-1a8f28f1b53a) 返回 200，无跳转，564296 字节（HTML 条目页）。页面题名 Perspectives on the CAP Theorem，作者 Gilbert, Seth 与 Lynch, Nancy A.，出版 2012-02，出版方 Institute of Electrical and Electronics Engineers，DOI 10.1109/MC.2011.389。条目页可以打开，不等于 PDF 已经拿到。
- 该页标注的 PDF 地址 `https://dspace.mit.edu/bitstreams/8b57ab12-0c57-413d-9502-0f60ec6104ae/download` 当日返回 429（Web Firewall，服务过载），不能写成已经从 dspace 下到了 PDF。
- 为阅读正文，同日从作者单位 CSAIL 的公开副本 `https://groups.csail.mit.edu/tds/papers/Gilbert/Brewer2.pdf` 取得同一篇 2012 年 IEEE Computer 论文：200，无跳转，135973 字节，`application/pdf`。这个地址只作为本次阅读来源说明，不写进节点 resources，也不列为推荐资源的第二条。
- `https://doi.org/10.1109/MC.2011.389` 当日经一次跳转后 IEEE 页面返回 500、0 字节，不能当作已读来源。
- PACELC 的表述不在 Gilbert & Lynch 这篇 Perspectives 论文里。第三条主线的引文按同一期 IEEE Computer（2012 年 2 月）中 Daniel Abadi 的文章核对；本课只把它作为术语核对来源，不把 Abadi 的 PDF 或博客加进推荐资源。

论文里的定义与结论，本课按以下方式使用。一致性取原子性：每个操作在请求与响应之间存在单一瞬间，看起来在该瞬间发生；等价于在客户端看来，所有操作就像由一台集中式服务器执行。可用性是每个请求最终都收到一个响应；为了 CAP，哪怕只要求「最终有响应」也足以构成问题；论文同时说明，在多数真实系统里，足够晚的响应和永远不返回差不多糟，但定理本身并不以延迟上界来定义可用性。分区容忍是对底层系统的陈述：服务器之间通信不可靠，服务器可能被分成彼此无法通信的若干组；消息可能被延迟，有时永远丢失；足够长的延迟在实践中也可以看成丢失。定理陈述为：在会发生通信故障的网络中，任何 web 服务都不可能实现一个对每个请求都保证响应的原子读写共享内存。证明概要、异步网络推论与 Chubby 例子都按前文转述，没有给论文添加它没写过的句子。

教学引擎与论文的差异必须逐条记住。`CapEngine` 是单进程仿真，`isolate()` 是全知的显式分区开关，真实异步网络没有这样的预言机；它没有 TCP，没有真实丢包，不是 Redis / redis-py / 网络服务 / Raft / Paxos / Jepsen；拒绝写是在 `wait_s` 之后返回 `rejected`，不是论文证明里那种「永不返回」；只有两台副本，提交要求两边都写入，相当于法定人数为 2；愈合后不自动合并冲突；`Clock` 是虚拟时钟。dspace 条目页当日可打开，bitstreams 下载返回 429，正文按 CSAIL 公开副本阅读。这些差异决定了实验结果只能用于理解定义与策略，不能当成生产系统的基准或故障注入结论。

## 自测题

**题 1.** 分区时 `SET` 在 `0.05` 返回 `rejected`，SLA 探针也拿到了这个回复。为什么实验仍说 `cap_available_for_sets=false`？

<details>
<summary>答案</summary>

CAP 讨论的是原子读写共享内存，可用性要求请求被服务并返回符合寄存器语义的结果。`rejected` 说明这次写没有发生，两边仍是 `Weekly digest`，所以写没有被完成；它只是一个网络层回复。`sla_got_a_reply_for_sets=true` 只证明有回复，`cap_available_for_sets=false` 才对应定理意义上的可用性。把 SLA 探针的快速响应当成 CAP 可用性，正是节点写出的误区，`pitfall_sla_is_not_cap_availability=true`。

</details>

**题 2.** 无分区时 `require_peer` 的 `SET` 延迟是 `0.10`，`local_first` 是 `0.02`，两次都返回 `ok`。这是 CAP 要求的三选二结果吗？

<details>
<summary>答案</summary>

不是。两条路径都没有分区，CAP 只在分区条件成立时谈可用性与一致性，无分区时它不直接决定这个延迟。`0.10` 对 `0.02` 是 PACELC 的 else 分支：没有分区时，系统仍在延迟与一致性之间权衡。`require_peer` 用更长的写延迟换「返回时两边都是修订标题」，`local_first` 用更短的写延迟换「返回后短时间内读 `r2` 仍可能看到旧标题」。`acceptance_cap_does_not_decide_latency_without_partition=true` 指的就是这一点。

</details>

**题 3.** 分区 + `local_first` 下，两次写分别返回 `ok`，`r1` 是 `Weekly digest (revised)`、`r2` 是 `Weekly digest (hotfix)`；`0.08` 愈合后两边仍是各自的值。用户看到什么？这条历史线性一致吗？

<details>
<summary>答案</summary>

用户看到两次写都成功，随后读 `r1` 得到修订标题、读 `r2` 得到 hotfix；愈合后冲突仍在，`conflict_after_heal=true`，引擎不自动合并。线性一致性不成立，`linearizable=false`：同一个寄存器上出现了两个成功的不同写，后面的读分别返回了这两个不同的值，找不到一个符合寄存器语义的全序。`cap_available_for_sets=true`，说明这次选择保住了写的可用性，代价是放弃了线性一致性，并把冲突解决留给业务。

</details>

## 推荐资源

- [Gilbert & Lynch · Perspectives on the CAP Theorem](https://dspace.mit.edu/entities/publication/dfcb6512-af44-4fc9-8e8b-1a8f28f1b53a)（Gilbert & Lynch，英文论文，进阶，免费）：用于核对分区条件与 CAP 中可用性的严格定义，将结论落实到本节点实验。2026-11-13 条目页返回 200，无跳转，564296 字节；页内 PDF 地址当日返回 429，正文按 CSAIL 公开副本阅读。

## 本课小结

- 分区容忍是对底层通信不可靠的陈述，不是可随手关掉的功能开关。异步网络里，`replicate_delay_s=0.08` 大于 `wait_s=0.05` 时，没有真实分区也会让 `SET` 在 `0.05` 返回 `rejected`、`note=peer_ack_after_wait_s`；迟来的确认本应在 `0.09` 到达，但值没有被应用，`rejected_despite_no_partition=true`。
- CAP 的可用性不是「回了一个错误码」或「探针很快有响应」。分区 + 拒绝写时，两次写分别在 `0.05` 与 `0.11` 返回 `rejected`，SLA 有回复，但 `cap_available_for_sets=false`；拒绝写用停写换来了 `linearizable_no_successful_conflicting_writes=true`。分区 + 接受冲突写时，两次写分别在 `0.02` 与 `0.04` 返回 `ok`，`cap_available_for_sets=true`，但 `linearizable=false`，`0.08` 愈合后冲突仍在。
- 无分区时 CAP 不直接决定延迟。`require_peer` 的 `SET` 在 `0.10` 返回，`local_first` 在 `0.02` 返回，两次都 `ok`；这是 PACELC 的 else 分支，不是 CAP 在无分区时给出的强制选择。选择哪条路径，取决于业务允许的陈旧、冲突与停写范围。

如果有一句答不上来，先回到对应的实验字段，再回到论文里关于原子性、可用性、分区容忍和定理陈述的那几句，以及证明概要里 `{p1}` 与 `{p2,...,pn}` 的分割，不要只把「CAP 三选二」或「SLA 有响应就是可用」背下来。