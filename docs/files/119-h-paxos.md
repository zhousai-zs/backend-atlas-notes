# 第 119/208 课：Paxos 与多数派交集

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-paxos` |
| 英文名 | Paxos |
| 领域 / 阶段 | H / 第 5 阶段 |
| 子类 | 复制与共识 |
| 难度 | 专家 |
| 前置节点 | [`h-raft-log`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html) |
| 标签 | `Paxos`、`Distributed` |
| 参考资料 | [Lamport — Paxos Made Simple 论文](https://lamport.azurewebsites.net/pubs/paxos-simple.pdf)（英文论文，专家，免费；2026-11-14 该地址返回 200，无跳转，95019 字节，Content-Type 为 `application/pdf`。题名 Paxos Made Simple；作者 Leslie Lamport；文内标注 01 Nov 2001；pdfinfo 记录 Creator 为 TeX output 2001.11.01:1449，CreationDate 为 Wed Nov 7 08:48:16 2001 CST，共 14 页，文内正文页码至第 11 页） |
| 摘要 | Paxos 的 prepare/accept 两阶段使新提案继承已被选中值所需的约束；Multi-Paxos 可复用稳定领导关系。 |

> **边界说明**：本课里的「内容后台」「文章标题 `Weekly digest` / `Weekly digest (draft)` / `Weekly digest (revised)` / `Weekly digest (hotfix)`」「键 `article:01:title`」都是**教学假设**，用来让提案编号、promise、accepted value 和多数派交集变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `PaxosEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是完整 Paxos，不是完整 Multi-Paxos，不是 Raft，也不是 Jepsen**。它是单进程邮箱仿真，没有 TCP，没有真实丢包或重复投递。论文假定消息可以丢失、重复、任意延迟，且不被篡改；教学引擎默认把每条 RPC 在虚拟时钟上延迟 `rpc_delay_s=0.01` 后送达，丢包只在 `crash()` / `isolate()` 这类全知信号里发生。`pause()` 是全知冻结，`isolate()` 是全知分区；真实异步网络没有这种信号。持久化是教学用 dict，acceptor 在回复 prepare / accept 之前 flush，proposer 在分配提案编号时 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。本课核心实现的是论文 §2 的单决议（single-decree）共识算法，也就是 The Part-Time Parliament 里的 synod 算法：一次实例选出一个值。论文 §3 用「一串互相独立的 Paxos 实例」实现状态机，并说明稳定领导人可以用同一个提案编号、一条较短的 Phase 1 消息覆盖许多实例，之后新命令只需 Phase 2；教学引擎里的 Multi-Paxos 对照只实现了这一条「复用 Phase 1」的对比，**没有选主、没有 no-op 填洞、没有 α 超前窗口、没有成员变更**。学习者是全知观察者，用当前多数派是否接受了同一提案来判断值是否被选中，没有实现 §2.3 里「区分的 learner」那套通知协议。教学引擎在忽略过期 prepare 或拒绝过期 accept 时会回一条 nack，这是论文 §2.2 写明的性能优化，不影响正确性。当 acceptor 收到它并未 promise 过的 accept（论文允许 Phase 2 发给与 Phase 1 不同的集合）时，若 `promised_n < n`，引擎会把 `promised_n` 提升到 `n`，避免随后再接受更小的编号；论文正文只要求记住「已响应的最高 prepare 编号」和「已接受的最高提案」，这一条是教学实现对 P1a 的补充，会在源码阅读里再次标明。本课所有时间都来自 `/workspace/atlas/lessons/practice119/results.json`，是**教学虚拟时钟上的数字**，不是生产 RTT 或线上超时配置。参数是 `rpc_delay_s=0.01`，三台 acceptor 的多数派为 `2`。两个 proposer 的提案编号取自不相交集合：`p1` 使用奇数 `1,3,5,…`，`p2` 使用偶数 `2,4,6,…`，用来落实论文 §2.5 的「不同 proposer 从不相交集合中取号，且永不复用」。

## 导读

### 上一课留下的缺口

上一课的 `RaftLogEngine` 已经把日志复制说清楚：`overwrite_uncommitted_tail` 里，旧 leader `s1` 带着未提交的 `Weekly digest (draft)` 被暂停后，新 leader `s2` 在 `0.56` 提交 term 3 的 `Weekly digest (revised)`，`0.65` 只从 index 2 截断草稿，已提交的 index 1 还在，`0.87` 三台日志完全相同。`current_term_commit_restriction` 又说明，旧任期条目即使已经在多数派上，也不能只靠副本计数提交。

这些结论依赖 Raft 的两项设计：新 leader 在选举时日志至少和投票者一样新，所以上任时已提交前缀已经在它的日志里；领导人的日志只追加，冲突由 follower 截断后缀来对齐。换句话说，Raft 把「已被选中的值不能被另一个值替换」藏进了选主限制和日志匹配。上一课没有把这条安全约束从「谁是 leader」里拆出来，单独观察：一个新的提案发起者，在没有先当选、也不拥有完整日志的情况下，凭什么不能把自己想写的 `Weekly digest (revised)` 变成被选中的值。

这正是本课要补的三件事。提案编号如何排序、如何保证不相交且不可复用；prepare / accept 两阶段如何让新提案继承「已被选中值」所需的约束；以及多数派交集为什么能保证：只要一个值已经被选中，任意新的 prepare 多数派都会看见它。只看成功路径不够，两个 proposer 交错执行、只被少数派接受的值、以及双方不断抬高编号却选不出值的活锁，才是这条安全证明真正要处理的边界。

### 接上前置

前置的 [第 118 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html) `h-raft-log` 讲的是前缀匹配、`matchIndex` / `commitIndex`，以及当前任期提交限制。它留下了一个重要结论：已提交的命令在切主后仍然存在。本课要把这个结论还原到更早、也更一般的安全证明思路上：Paxos 的 P2c。新 proposer 完成 Phase 1 之后，若 prepare 多数派里已经有人接受过提案，它必须发出其中编号最高的那个值，而不能用自己想提的值。选主和稳定领导人解决的是进度；两阶段和多数派交集解决的是「至多一个值被选中」。Raft 的 leader 模型可以看成这条思路的一种工程折叠，本课先把折叠打开。

### 教学场景（教学假设）

假设内容后台用三台 acceptor `a1`、`a2`、`a3` 为键 `article:01:title` 做一次单决议：选出一个标题。两个 proposer `p1`、`p2` 分别想写入 `Weekly digest (draft)` 和 `Weekly digest (revised)`。三台的多数派为 `2`，`rpc_delay_s=0.01`。提案编号由不相交集合分配：`p1` 用 `1,3,5,…`，`p2` 用 `2,4,6,…`。主实践里，`p1` 先让 `{a1,a2}` 接受草稿，使该提案被选中；随后 `p2` 向 `{a2,a3}` 发起更高编号的提案，观察它必须继承哪个值。对照实验再引入只被 `a1` 接受、尚未选中的草稿，以及 `Weekly digest` 与 `Weekly digest (revised)` 作为连续两条命令、分别落在两个 Paxos 实例上的情形。这些数字都是教学虚拟时钟上的值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **proposal number**：提案由编号和值组成，不同提案必须有不同编号；不同 proposer 从不相交集合取号，用过的编号不能再发。编号更高只表示更新一轮两阶段，不表示可以改写已被选中的值。
2. **promise 与 accepted value**：Phase 1 的 prepare 换来「不再接受更小编号」的 promise，以及该 acceptor 已经接受的最高编号提案；Phase 2 用继承得到的值，或在没有任何 accepted value 时用 proposer 自己选的值，去请求 accept。
3. **多数派交集**：任意两个多数派至少有一个公共 acceptor。若某提案已被一个多数派接受，因而被选中，则新提案的 prepare 多数派一定与之相交，从而看见并继承该值。

## 学习目标

- 能解释 proposal number 的作用及适用边界，说明为什么编号必须唯一、必须来自不相交集合、不能复用，以及为什么更高的编号不能用来改写已被选中的值。
- 能独立完成实践：手工演算两个 proposer 交错执行 prepare / accept，并在每一步列出 acceptor 的持久状态（已 promise 的编号、已接受的编号和值）。
- 能指出新 proposer 必须继承哪个值，并能区分活锁与安全性：交错可能选不出任何值，但只要安全性质还在，就不会选出两个不同的值。

## 为什么后端要懂

公众号内容后台（教学假设）修改已发布标题时，两个协调者可能同时想把 `article:01:title` 写成不同的稿。`p1` 想发布 `Weekly digest (draft)`，`p2` 想发布 `Weekly digest (revised)`。如果草稿已经被多数派接受，因而被选中，新来的 `p2` 即使拿出更高的提案编号，也必须把这个已选中的草稿继承下来，而不能把自己的修订当成「更新的命令」直接覆盖。否则编辑会在一个时刻看到草稿已生效，另一个时刻又看到修订生效，审核、缓存失效和审计记录都无法依赖「已选中」这个词。

反过来，如果两个协调者不停地用更高编号去做 Phase 1，谁的 Phase 2 都会被对方的 promise 挡住，标题可能长时间选不出来。这是活锁，不是安全性被破坏：系统卡住了，但没有宣布两个互相冲突的标题都已生效。要恢复进度，需要选出一个 distinguished proposer，只让它继续提案。理解这两点，才能在「为什么要有领导」和「没有领导是否会写坏数据」之间画出正确边界，也才能在阅读 etcd、Chubby 或各种「Paxos 家族」实现时，分清哪些是论文 §2 的安全证明，哪些是为了进度和吞吐做的优化。

## 核心知识点

### 1. proposal number

Lamport 在 §2.1 给出共识的安全要求：只有被提出过的值可以被选中；只选中一个值；进程不会在值尚未被选中时得知它已被选中。§2.2 接着说明，单个 acceptor 会单点故障，所以要用多个 acceptor，并在一个多数派接受某提案时宣布该提案（以及它的值）被选中。为了让 acceptor 可以接受不止一个提案，每个提案由一个编号和一个值组成，且不同提案必须有不同编号。

编号的适用边界要从论文里逐条落实。第一，编号是全序的自然数，用来对提案做归纳；P2 说：若编号为 `m`、值为 `v` 的提案被选中，则之后每一个被选中的更高编号提案也必须具有值 `v`。因此「编号更大」只是「更新的一轮提案」，不是「可以换一个值」。第二，§2.5 要求不同 proposer 从不相交的编号集合中取号，并且每个 proposer 在稳定存储里记住自己用过的最大编号，下一次 Phase 1 必须使用比它更大的编号。教学引擎里 `p1` 使用 `1,3,5,…`，`p2` 使用 `2,4,6,…`，分配成功会先 flush 再发送 prepare。第三，acceptor 只响应严格大于自己已响应过的最高 prepare 编号的请求；更小的 prepare 会被忽略。P1a 又规定：acceptor 可以接受编号为 `n` 的提案，当且仅当它还没有响应过编号大于 `n` 的 prepare。把这三条放在一起，提案编号既是排序键，也是「我保证不再接受更旧提案」的边界。

实践里的 `proposal_number_bounds` 把这三条变成可观察的时间线。`0.02`，`p2` 用属于自己集合的 `n=2` 完成 Phase 1，三台 `promised=2`：

```text
a1  promised=2  accepted_n=None  accepted_v=None
a2  promised=2  accepted_n=None  accepted_v=None
a3  promised=2  accepted_n=None  accepted_v=None
```

`0.03`，`p1` 用更小的 `n=1` 把 prepare 发到三台，三台都忽略，`prepare_ignored_count=3`。`0.06`，`p1` 改用自己集合里更大的 `n=3`，三台 `promised=3`。`0.07`，`p2` 仍用已经过期的 `n=2` 发 accept，三台拒绝，`promised_n=3 > 2`，`accept_rejected_count=3`。同一时刻 `0.08`，`p1` 试图再次使用已经用过的 `n=3`，引擎拒绝，错误是 `p1 cannot reuse or go backwards on instance 1: requested n=3, already used 3`；再试图使用属于 `p2` 集合的 `n=4`，同样被拒绝，错误是 `p1 must choose n from its disjoint set (n ≡ 1 mod 2), got n=4`。`0.10`，`p1` 用合法的 `n=3` 完成 Phase 2，三台接受 `Weekly digest (draft)`，该值被选中；`p2` 原先打算写的 `Weekly digest (hotfix)` 从未被选中，`hotfix_never_chosen=true`。

编号用错的后果很具体：过期编号会被忽略或拒绝，复用编号会破坏「不同提案不同编号」这条前提，跨集合取号则会让两个 proposer 在同一编号上相撞。教学引擎在后两种情况直接拒绝发请求，是为了让边界可检查；真实实现里，稳定存储里的「已用最大编号」承担同一责任。

### 2. promise 与 accepted value

只靠编号还不能选出值。§2.2 从 P2 逐步加强到 P2c：proposer 要发出编号为 `n` 的提案，必须先找到一个多数派 `S`，使得要么 `S` 中还没有人接受过小于 `n` 的提案，要么它将要发出的值，就是 `S` 中已接受的、编号小于 `n` 的那些提案里编号最高的那个值。它无法预测未来的 accept，所以改为控制未来：先请求多数派 promise 不再接受小于 `n` 的提案，并汇报各自已经接受的最高编号提案。这就是 Phase 1 的 prepare / promise。若多数派都汇报「没有 accepted value」，proposer 可以选自己的值；否则必须继承那个最高编号的 accepted value。然后进入 Phase 2，发出 accept 请求。

论文把两阶段写得很短。Phase 1a：proposer 选择编号 `n`，向一个多数派发送 prepare。Phase 1b：acceptor 若发现 `n` 大于自己已经响应过的任何 prepare，就 promise 不再接受小于 `n` 的提案，并带上自己已接受的最高编号提案。Phase 2a：proposer 收到多数派响应后，对同一个 `n` 发出 accept，值为继承结果或自己选的值。Phase 2b：acceptor 接受该提案，除非它已经响应过编号大于 `n` 的 prepare。acceptor 可以忽略任何请求而不破坏安全性；实现上通常忽略已经过期的 prepare，并最好通知 proposer 放弃该提案。

主实践 `interleave_two_proposers` 把这两阶段展开成可逐步核对的持久状态。`0.00` 三台都是空的：

```text
a1  promised=0  accepted_n=None  accepted_v=None
a2  promised=0  accepted_n=None  accepted_v=None
a3  promised=0  accepted_n=None  accepted_v=None
```

`0.02`，`p1` 完成 `n=1` 的 Phase 1，意图值是 `Weekly digest (draft)`，三台 promise，没有人汇报 accepted value，因此 `inherited=false`，`issued_value` 就是草稿本身：

```text
a1  promised=1  accepted_n=None  accepted_v=None
a2  promised=1  accepted_n=None  accepted_v=None
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.04`，`p1` 只向 `{a1,a2}` 发 Phase 2，这两台接受 `n=1` 的草稿，提案被选中，`chosen_majority=['a1','a2']`；`a3` 没有接受：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.06` 是本课最关键的一步。`p2` 想写 `Weekly digest (revised)`，向 `{a2,a3}` 发起 `n=2` 的 Phase 1。`a2` 的 promise 里带着 `accepted_n=1`、`accepted_v=Weekly digest (draft)`，`a3` 没有 accepted value。prepare 多数派是 `['a2','a3']`，其中最高编号的 accepted value 来自 `a2`，于是 `inherited=true`，`inherit_from_n=1`，`inherit_from_acceptor=a2`，`issued_value` 变成草稿，而不是 `p2` 自己的修订：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=1  accepted_v=Weekly digest (draft)
a3  promised=2  accepted_n=None  accepted_v=None
```

`0.08`，`p2` 即使在 `start_accept` 时传入修订，引擎也忽略这个自身的值，`ignored_own_value=true`，发出的是继承来的草稿。`{a2,a3}` 接受 `n=2` 的草稿后，被选中的值仍然是 `Weekly digest (draft)`，`revised_never_chosen=true`：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=2  accepted_v=Weekly digest (draft)
a3  promised=2  accepted_n=2  accepted_v=Weekly digest (draft)
```

同一时刻 crash 再 restart `a2`，它的持久状态仍是 `promised=2`、`accepted_n=2`、`accepted_v=Weekly digest (draft)`，`restart_preserved=true`。这对应论文 §2.1 和 §2.5：agent 可能停机再恢复，若值已经在停机前被选中，acceptor 必须记住 promise 与 accepted 提案，否则安全证明会断。教学引擎用 dict 在回复前 flush 来模拟这条要求，不是磁盘。

### 3. 多数派交集

§2.2 一开始就点明，为什么「足够大的集合」要取多数派：任意两个多数派至少有一个公共 acceptor。若再要求一个 acceptor 对同一轮冲突只能留下与安全证明相容的 accepted value，被一个多数派选中的值就无法从另一个多数派眼前消失。P2c 正是把这句话写成可执行的检查：新提案的 prepare 多数派 `S` 必须与先前选中该值的多数派 `C` 相交，相交的那一台会汇报已经接受的提案，新 proposer 只能继承。

主实践把交集算得很具体。`0.04` 的选中多数派是 `{a1,a2}`，`0.06` 的 prepare 多数派是 `{a2,a3}`，交集是 `{a2}`，正是汇报 `n=1` 草稿的那一台。没有 `a2` 这一票相交，`p2` 就会以为自己可以写修订，已选中的草稿就会被另一个值替换。多数派大小在三台里是 `2`，不是「全部到齐」；论文也允许 Phase 2 发给与 Phase 1 不同的集合，主实践里 `p1` 的 Phase 1 发给三台、Phase 2 只发给 `{a1,a2}`，就是为了让后来的交集清晰可见。

对照实验 `partial_accept_not_chosen` 说明，交集约束只在「值已经被选中」时成为必然。`0.04`，`p1` 只让 `a1` 接受 `n=1` 的草稿，未形成多数派，`chosen=None`，`not_chosen_after_partial=true`：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=1  accepted_n=None  accepted_v=None
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.06`，`p2` 向 `{a2,a3}` 做 Phase 1，这个多数派与唯一接受草稿的 `a1` 不相交，`intersection_with_a1=[]`，`p2_inherited=false`，它因此可以发出自己的 `Weekly digest (revised)`。`0.08`，`{a2,a3}` 接受修订，修订被选中，`draft_never_chosen=true`，`safety_ok=true`：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=2  accepted_v=Weekly digest (revised)
a3  promised=2  accepted_n=2  accepted_v=Weekly digest (revised)
```

`a1` 上仍留着未被选中的草稿，这并不违反「只选中一个值」：草稿从未获得多数派，修订才是被选中的值。把这两段实验对照着看：部分接受不是选中；只有当某个提案已被多数派接受，任意新的 prepare 多数派才会与之相交，从而被迫继承该值。

## 常见误区

节点给出的误区是：把 Paxos 的实例数量等同存储数据副本数量。`pitfall_instances_are_not_replicas` 用同一组三台 acceptor 连续跑两个单决议实例来观察这一点。论文 §3 写得很清楚：实现复制状态机的方法，是运行一串互相独立的 Paxos 共识实例，第 `i` 个实例选出第 `i` 条状态机命令。实例数量随命令序列增长，与 acceptor 的数量、也就是数据副本的数量，不是一回事。

`0.04`，实例 1 选中第 1 条命令 `SET article:01:title = Weekly digest`，三台的持久状态都是 `promised=1`、`accepted_n=1`、`accepted_v=Weekly digest`。`0.08`，实例 2 选中第 2 条命令 `SET article:01:title = Weekly digest (revised)`，三台在实例 2 上的持久状态都是 `promised=1`、`accepted_n=1`、`accepted_v=Weekly digest (revised)`。注意这里的 `n=1` 是实例 2 里 `p1` 的第一个提案编号，与实例 1 的 `n=1` 互不相干；提案编号是按实例分配的。最终 `acceptor_count=3`，`instance_count=2`，`command_count=2`，`instances_equal_replicas=false`，`instances_equal_commands=true`，`each_acceptor_holds_all_instances=true`，`wrong_model_rejected=true`。若按「有 3 个副本就该有 3 个 Paxos 实例」来理解，会期待 `instance_count=3`；实验里再来第 3 条命令，才会出现实例 3，而它仍然跑在这 3 个 acceptor 上，不会再复制出一套新的集群。把实例数当成副本数，会把「命令日志有多长」和「这份日志存了几份」混成一个数字，成员变更、追赶和容量规划都会从错误的模型出发。

第二个误区是把「已经有一台接受」当成「值已被选中」。`partial_accept_not_chosen` 里，`0.04` 的 `a1` 已经接受了草稿，但 `chosen` 仍是 `None`。`0.08` 另一个多数派选出了修订，草稿永远没有被选中。若在 `0.04` 就向客户端宣布草稿生效，随后就会看到一个「已发布」的标题被另一个值替换。选中的定义是「某个提案被一个多数派接受」，不是「至少有一个人写下了它」。

第三个误区是把更高的 proposal number 当成改写已选中值的许可证。主实践里 `p2` 的 `n=2` 确实比 `p1` 的 `n=1` 大，意图值也确实是 `Weekly digest (revised)`，但 `0.06` 它必须继承草稿，`0.08` 发出去的还是草稿，`ignored_own_value=true`。编号用来排序和获取 promise，不用来覆盖 P2。

第四个误区是把活锁当成安全性失败。两个 proposer 互相抢更高编号时，可能很久都选不出值，表面上看像协议「坏了」。下一节的对照会说明：这只是进度没有保证，安全性质仍然成立。

## 设计权衡

节点给出的权衡是：Paxos 家族实现选择多，阅读时明确论文模型和具体优化。论文 §2 的核心是单决议两阶段算法，本身并不包含稳定领导；§3 把一串实例排成状态机，并观察到：新领导人用同一个提案编号，向其他服务器发一条较短的 Phase 1 消息，就可以覆盖许多尚未选中的实例，之后在这些实例上提出新命令只需执行 Phase 2。「既然故障和重新选主应当是稀有事件，执行一条状态机命令的有效代价就是只跑 Phase 2。」教学对照 `tradeoff_paper_vs_multipaxos` 只实现了这一条，明确标注 `not_full_multipaxos=true`，不要把它读成完整的 Multi-Paxos、Fast Paxos 或其他变体。

实验里每次 RPC 单向延迟仍是教学用的 `0.01` 秒，不是生产 RTT。同样由一个 proposer 连续提出命令时，单决议路径每个实例都走满 Phase 1 加 Phase 2；Multi-Paxos 教学对照先对全部实例做一次范围 prepare，再对每条命令只做 Phase 2：

| 模式 | 命令数 | `elapsed_s` | `rpc_count` | 每条命令的阶段 |
|---|---:|---:|---:|---|
| `single_decree` | 5 | 0.20 | 60 | 2 |
| `multipaxos_teaching_contrast` | 5 | 0.12 | 36 | 先 2，其后 1 |
| `single_decree` | 20 | 0.80 | 240 | 2 |
| `multipaxos_teaching_contrast` | 20 | 0.42 | 126 | 先 2，其后 1 |

20 条命令时，单决议要用 `0.80` 秒、`240` 次单向 RPC；复用 Phase 1 之后只要 `0.42` 秒、`126` 次 RPC，少了 `114` 次 RPC、`0.38` 秒，`multipaxos_fewer_rpcs_for_same_commands=true`，`stable_leader_skips_phase1=true`。范围 prepare 自己花费 `0.02` 秒、`6` 次 RPC（向三台各发一条 prepare，再收回三条 promise），之后每条命令是 `6` 次 Phase 2 RPC。这些数字只说明「稳定领导把大多数命令的代价从两阶段降到一阶段」这个方向，不是生产容量规划，也没有把选主、填洞和流水线算进去。

第二笔权衡是进度与安全性的分离。§2.4 构造了两个 proposer 轮流完成更高编号 Phase 1、对方 Phase 2 被忽略的场景，并指出：要保证进度，必须选出一个 distinguished proposer；若它能与多数派通信，并用一个足够大的编号，就可以选出一个值。Fischer、Lynch 与 Patterson 的结果又说明，可靠地选出这样一个 proposer，必须使用随机或真实时间（例如超时）。但「选主成功与否都不影响安全性」。活锁实验把这句活写成数字：交错期间一个值都没选中，安全性质却一直成立；一旦只让 `p1` 继续，值立刻被选中。用选主换进度，是工程上几乎不可避免的选择，不是安全证明的一部分。Raft 把这个选择做成了默认形态；阅读具体实现时，要先问它还保存着哪些 §2 的约束，再问它为进度做了哪些 §3 或更后面的优化。

第三笔权衡是 nack 与忽略。论文允许 acceptor 默默忽略任何请求而不破坏安全性，同时建议：若因为已经看到更高编号的 prepare 而忽略，最好通知 proposer 放弃。教学引擎选择发送 `prepare_nack` / `accept_nack`，好让时间线上的拒绝可观察。这是性能与可观测性上的实现选择，不是安全条件。

## 动手实践：手工演算两个 proposer 交错执行，列出 acceptor 持久状态

配套代码在 `/workspace/atlas/lessons/practice119/`，包括 `engine.py`、`app.py`、`test_paxos.py`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice119
bash test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT，也不是线上超时配置。`rpc_delay_s=0.01`，三台 acceptor 的多数派为 `2`。`p1` 的编号集合是 `1,3,5,…`，`p2` 的编号集合是 `2,4,6,…`。每一步都列出三台的持久状态：`promised` 是已响应的最高 prepare 编号，`accepted_n` / `accepted_v` 是已接受的最高编号提案及其值。

**1. 主实践 `interleave_two_proposers`。** `0.00` 三台 acceptor 的持久状态都为空，尚未选中任何值。

```text
a1  promised=0  accepted_n=None  accepted_v=None
a2  promised=0  accepted_n=None  accepted_v=None
a3  promised=0  accepted_n=None  accepted_v=None
```

`0.00`，`p1` 向 `{a1,a2,a3}` 发送 `n=1` 的 prepare，意图值是 `Weekly digest (draft)`。`0.02` Phase 1 完成，三台 `promised=1`，没有 accepted value，`inherited=false`，`issued_value` 为草稿，`promises=['a1','a2','a3']`。

```text
a1  promised=1  accepted_n=None  accepted_v=None
a2  promised=1  accepted_n=None  accepted_v=None
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.02`，`p1` 只向 `{a1,a2}` 发送 `n=1` 的 accept。`0.04` 这两台接受草稿，提案被选中，`chosen_majority=['a1','a2']`，`chosen_after_p1=Weekly digest (draft)`；`a3` 仍无 accepted value。已选中的是草稿，不是修订。

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.04`，`p2` 向 `{a2,a3}` 发送 `n=2` 的 prepare，意图值是 `Weekly digest (revised)`。`0.06` Phase 1 完成，`promises=['a2','a3']`。这个 prepare 多数派与选中多数派 `{a1,a2}` 交于 `{a2}`：`a2` 汇报 `accepted_n=1`、`accepted_v=Weekly digest (draft)`，`a3` 没有已接受提案。于是 `p2_inherited=true`，`p2_inherited_value=Weekly digest (draft)`，`inherit_from_n=1`，`inherit_from_acceptor=a2`。`p2` 必须继承草稿，不能发出修订。

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=1  accepted_v=Weekly digest (draft)
a3  promised=2  accepted_n=None  accepted_v=None
```

`0.06`，`p2` 向 `{a2,a3}` 发送 accept，调用时仍传入修订，但引擎忽略自身的值，`ignored_own_value=true`，实际发出的是草稿。`0.08`，`a2` 和 `a3` 接受 `n=2` 的草稿，被选中的值仍然是 `Weekly digest (draft)`，`revised_never_chosen=true`，`safety_ok=true`。`a1` 仍保留 `n=1` 的草稿；更高编号的提案带的是同一个值。

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=2  accepted_v=Weekly digest (draft)
a3  promised=2  accepted_n=2  accepted_v=Weekly digest (draft)
```

同一时刻 `0.08` crash 再 restart `a2`，重启后持久状态不变，仍是 `promised=2`、`accepted_n=2`、`accepted_v=Weekly digest (draft)`，`restart_preserved=true`。整段实验共 flush `11` 次，都发生在分配编号或回复 prepare / accept 之前。结论是：Paxos 里「被选中」对应「某个提案已被多数派接受」，它是上一课「已提交」最接近的对应物；新 proposer 面对已被选中的草稿，只能继承它。

**2. 对照 `partial_accept_not_chosen`。** 同样从空状态开始。`p1` 完成 `n=1` 的 Phase 1 后，只让 `a1` 接受草稿。`0.04` 时草稿尚未被选中，`not_chosen_after_partial=true`：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=1  accepted_n=None  accepted_v=None
a3  promised=1  accepted_n=None  accepted_v=None
```

`0.06`，`p2` 向 `{a2,a3}` 完成 Phase 1，与 `a1` 的交集为空，`p2_inherited=false`，`issued_value` 是它自己的 `Weekly digest (revised)`。`0.08`，`{a2,a3}` 接受修订，修订被选中，`draft_never_chosen=true`：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=2  accepted_n=2  accepted_v=Weekly digest (revised)
a3  promised=2  accepted_n=2  accepted_v=Weekly digest (revised)
```

与主实践唯一的差别，就是 `p1` 有没有让草稿获得多数派。没有多数派，就没有「任意新多数派都会相交」的保证，另一个值仍然可以被选中，而且这并不违反安全性。

**3. 提案编号边界 `proposal_number_bounds`。** `0.02`，`p2` 用 `n=2` 完成 Phase 1，三台 `promised=2`。`0.03`，`p1` 的 `n=1` prepare 被三台忽略，`prepare_ignored_count=3`。`0.06`，`p1` 改用 `n=3`，三台 `promised=3`。`0.07`，`p2` 的 `n=2` accept 被三台拒绝，`promised_n=3`，`accept_rejected_count=3`。`0.08`，复用 `n=3` 和跨集合使用 `n=4` 都被引擎拒绝。`0.10`，`p1` 用合法的 `n=3` 完成 Phase 2，三台接受草稿，`chosen=Weekly digest (draft)`，`hotfix_never_chosen=true`。过期编号进不了 Phase 2，不相交集合和不可复用从源头上避免了「两个提案共用一个编号」。

**4. 活锁与安全性 `livelock_vs_safety`。** 两个 proposer 按论文 §2.4 的方式交错抬高编号，谁也不先完成 Phase 2。`0.02`，`p1` 完成 `n=1` 的 Phase 1；`0.04`，`p2` 完成更高的 `n=2` 的 Phase 1，三台 `promised=2`；`0.05` 三台拒绝 `p1` 的 `n=1` accept，`0.06` `p1` 收到三份 nack。`0.08`，`p1` 改用 `n=3` 完成 Phase 1；`0.09` 三台拒绝 `p2` 的 `n=2` accept。`0.12`，`p2` 改用 `n=4` 完成 Phase 1；`0.13` 三台拒绝 `p1` 的 `n=3` accept。`0.16`，`p1` 改用 `n=5` 完成 Phase 1；`0.17` 三台拒绝 `p2` 的 `n=4` accept。到 `0.18`，`t_livelock_end=0.18`，`accept_rejected_during_livelock=12`（四轮拒绝，每轮三台），`chosen_during_livelock=None`，`livelock_no_value_chosen=true`，但 `safety_during_livelock=true`：三台始终没有接受任何提案，更没有选出两个不同的值。

```text
a1  promised=5  accepted_n=None  accepted_v=None
a2  promised=5  accepted_n=None  accepted_v=None
a3  promised=5  accepted_n=None  accepted_v=None
```

这时停止交错，只让 `p1` 作为 distinguished proposer 继续。它已经持有 `n=5` 的 Phase 1 多数派，直接发出 Phase 2。`0.20` 三台接受 `n=5` 的 `Weekly digest (draft)`，该值被选中，`chosen_after_distinguished=Weekly digest (draft)`，`election_is_for_progress_not_safety=true`：

```text
a1  promised=5  accepted_n=5  accepted_v=Weekly digest (draft)
a2  promised=5  accepted_n=5  accepted_v=Weekly digest (draft)
a3  promised=5  accepted_n=5  accepted_v=Weekly digest (draft)
```

活锁可以一直进行下去，安全性不会因此放松；选出一个 distinguished proposer，是为了让某个值最终被选中，不是为了修补一条已经被破坏的安全性质。论文还指出，可靠选主必须借助随机或超时，因为 FLP 限制仍然在；教学实验没有实现选主，只是停止其中一方的交错，来观察「只让一个人提案」对进度的效果。

**5. 误区对照与 Multi-Paxos 权衡。** `pitfall_instances_are_not_replicas` 已在常见误区里给出数字：3 个 acceptor、2 个实例、2 条命令，`instances_equal_replicas=false`。`tradeoff_paper_vs_multipaxos` 已在设计权衡里给出表格：20 条命令时，单决议 `0.80` 秒、`240` 次 RPC，教学 Multi-Paxos 对照 `0.42` 秒、`126` 次 RPC，`rpc_saved_20=114`，`elapsed_saved_20_s=0.38`。两种模式最终都选出了全部命令，`all_chosen=true`，`safety_ok=true`。

**6. 核对验收字段。** `acceptance_new_proposer_inherits_chosen_value=true`，`acceptance_livelock_vs_safety=true`，`pitfall_instances_not_equal_replicas=true`，`tradeoff_stable_leader_saves_phase1=true`。四个字段都能在 `results.json` 顶层找到，实验脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Lamport — Paxos Made Simple 论文中与 Paxos 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [Paxos Made Simple PDF](https://lamport.azurewebsites.net/pubs/paxos-simple.pdf) 返回 200，无跳转（最终 URL 仍是 `https://lamport.azurewebsites.net/pubs/paxos-simple.pdf`），95019 字节，`Content-Type` 为 `application/pdf`。
- 题名 Paxos Made Simple；作者 Leslie Lamport；文内标注 01 Nov 2001。`pdfinfo` 记录 Creator 为 TeX output 2001.11.01:1449，CreationDate 为 Wed Nov 7 08:48:16 2001 CST，Pages 14，PDF 版本 1.2。PDF 共 14 页，其中文内正文页码至第 11 页。本节课主要用 §2.1 The Problem（安全性质与角色）、§2.2 Choosing a Value（P1、P2、P2c 与两阶段，约第 2–6 页）、§2.3 Learning a Chosen Value、§2.4 Progress（活锁与 distinguished proposer，约第 7 页）、§2.5 The Implementation（不相交编号与稳定存储，约第 7–8 页），以及 §3 Implementing a State Machine（多实例与稳定领导复用 Phase 1，约第 8–11 页）。

论文里的定义与结论，本课按以下方式使用。

§2.1 的安全要求：只有被提出过的值可以被选中；只选中一个值；进程不会在值尚未被选中时得知它已被选中。角色分为 proposer、acceptor、learner，一个进程可以扮演多种角色。模型是异步、非拜占庭：进程可以停机再恢复，因此失败重启后仍必须记住某些信息；消息可以延迟、重复、丢失，但不被篡改。

§2.2 的选值规则：值在某个提案被多数派接受时被选中。P1 要求 acceptor 接受它收到的第一个提案，因而必须允许接受多个提案。P2 / P2a / P2b / P2c 逐步把「已被选中的值必须被更高编号提案继承」写成可维护的不变式。Phase 1 用 prepare 换 promise 和已接受的最高编号提案；Phase 2 发出继承得到的值，或在没有 accepted value 时发出 proposer 自选的值。P1a：acceptor 可以接受编号 `n`，当且仅当它还没有响应过编号大于 `n` 的 prepare。优化后，acceptor 只需持久记住已响应的最高 prepare 编号，以及已接受的最高编号提案。

§2.4 明确给出活锁：两个 proposer 轮流完成更高编号的 Phase 1，对方的 Phase 2 被忽略，于是没有任何值被选中。进度依赖 distinguished proposer；选举算法需要随机或真实时间，但选举成败不影响安全性。

§2.5 要求不同 proposer 从不相交集合取号，并在稳定存储中记住自己用过的最大编号；acceptor 必须在真正发出响应之前，先把打算写入的响应记录到稳定存储。

§3 用一串独立实例实现状态机，第 `i` 个实例选出第 `i` 条命令。正常情况下选出一个 leader，作为所有实例的 distinguished proposer。Phase 1 不必在提出值之前完成对值的选择，这是稳定领导可以跳过后续 Phase 1 的关键。新 leader 可用同一个提案编号、一条较短消息执行许多实例的 Phase 1；之后新命令的有效代价主要是 Phase 2。多个自认为是 leader 的服务器同时提案，可能阻止选出值，但不会让第 `i` 条命令出现两个不同的值。

教学实验与论文的差异如下。

- `PaxosEngine` 是单进程邮箱仿真，没有 TCP，默认不会丢失或重复消息，只在 `crash()` / `isolate()` 时丢弃在途消息。论文允许任意延迟、丢失和重复。
- `rpc_delay_s=0.01` 和所有时间戳都是教学虚拟时钟上的数字，不是生产 RTT，也不是论文里的真实时间或超时。
- 持久化是 dict，在回复前 flush，不是磁盘，也没有 `fsync`。`restart()` 保留这份 dict，用来观察 §2.5 的稳定存储要求，不表示做过真正的崩溃恢复测试。
- 核心实现是单决议算法。多实例只用于误区对照和 Multi-Paxos 教学对比；对照没有选主、没有 no-op 填洞、没有 α 超前窗口、没有成员变更，不能当成完整的 §3 实现。
- 提案编号用算术等差集合实现不相交取号，这是 §2.5「从不相交集合中取号」的一种教学实现，不是论文指定的唯一格式。
- 学习者是全知观察者，按当前多数派是否接受同一提案来判断选中；没有实现 §2.3 的区分 learner 或「向所有 learner 广播」。
- 忽略过期请求时发送 nack，是论文写明的性能优化；教学引擎把它做成默认行为，以便拒绝可观察。
- 若 acceptor 收到它并未 promise 过的 accept，且 `promised_n < n`，引擎会把 `promised_n` 提升到 `n`。这是对 P1a 的教学补充，论文正文没有单独写这一步。
- `pause()` 与 `isolate()` 是全知信号。活锁实验中的 distinguished proposer，是测试脚本停止另一方交错后得到的，不是一次真实选举。

## 自测题

**题 1.** 主实践里，新 proposer `p2` 必须继承哪个值？请用 `0.04` 和 `0.06` 的 acceptor 持久状态，以及多数派交集说明原因。

<details>
<summary>答案</summary>

必须继承已经被选中的 `Weekly digest (draft)`，不能发出自己打算写的 `Weekly digest (revised)`。

`0.04`，`p1` 的 `n=1` 提案已被 `{a1,a2}` 接受，因而被选中：

```text
a1  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a2  promised=1  accepted_n=1  accepted_v=Weekly digest (draft)
a3  promised=1  accepted_n=None  accepted_v=None
```

选中多数派是 `{a1,a2}`。`0.06`，`p2` 向 `{a2,a3}` 完成 `n=2` 的 Phase 1，意图值却是修订。prepare 多数派 `{a2,a3}` 与选中多数派交于 `{a2}`，`a2` 汇报 `accepted_n=1`、`accepted_v=Weekly digest (draft)`。于是 `p2_inherited=true`，`inherit_from_n=1`，`inherit_from_acceptor=a2`，`issued_value` 变成草稿。`0.08` 即使 `start_accept` 传入修订，也会被忽略，`ignored_own_value=true`，`{a2,a3}` 接受的仍是草稿，`revised_never_chosen=true`。

这就是 P2c 与多数派交集：已被一个多数派选中的值，一定会出现在任意新的 prepare 多数派里，新 proposer 只能继承它。对照实验里，若草稿只被 `a1` 接受、尚未选中，`p2` 的 `{a2,a3}` 与 `a1` 不相交，就可以发出修订；那是因为草稿从未被选中，并不推翻这条规则。

</details>

**题 2.** 活锁与安全性有什么区别？请用 `livelock_vs_safety` 的时间线和最终状态说明。

<details>
<summary>答案</summary>

活锁是进度失败：协议一直在运行，却选不出任何值。安全性是「至多选出一个值，且只选出被提出过的值」。两者不是一回事。两个 proposer 互相抢更高编号时，可能陷入活锁，但安全性质仍然成立；要恢复进度，需要一个 distinguished proposer，而选主本身是为了进度，不是为了修补安全性。

实验里，`0.02` `p1` 完成 `n=1` 的 Phase 1，`0.04` `p2` 用 `n=2` 抢先完成 Phase 1，`0.05` 三台拒绝 `n=1` 的 accept。随后双方交替抬高编号：`0.09` 拒绝 `n=2`，`0.13` 拒绝 `n=3`，`0.17` 拒绝 `n=4`。到 `0.18`，四轮拒绝共 `12` 次 `accept_rejected`，`chosen_during_livelock=None`，`livelock_no_value_chosen=true`，三台的持久状态是 `promised=5`、`accepted_n=None`、`accepted_v=None`。此时一个值都没选中，更没有选出两个不同的值，`safety_during_livelock=true`。

停止交错后，只让已经持有 `n=5` Phase 1 多数派的 `p1` 发出 Phase 2，`0.20` 三台接受 `Weekly digest (draft)`，`chosen_after_distinguished=Weekly digest (draft)`。论文 §2.4 的原意就是这样：活锁容易构造，进度依赖 distinguished proposer；选举成败不影响安全性。`election_is_for_progress_not_safety=true`。

</details>

**题 3.** 为什么不能把 Paxos 的实例数量当成数据副本数量？用 `pitfall_instances_are_not_replicas` 的数字说明。

<details>
<summary>答案</summary>

Paxos 实例是「第几条状态机命令要在哪一次共识里被选中」，数据副本是「同一条被选中的命令被多少个 acceptor 记住」。前者随命令序列变长，后者由集群成员数决定，两件事不是同一个计数。

实验在 3 个 acceptor 上先后跑了 2 个单决议实例。`0.04`，实例 1 选中 `SET article:01:title = Weekly digest`，三台都接受了这个值。`0.08`，实例 2 选中 `SET article:01:title = Weekly digest (revised)`，仍然是这三台，各自为实例 2 保存一份持久状态。最终 `acceptor_count=3`，`instance_count=2`，`command_count=2`，`instances_equal_replicas=false`，`instances_equal_commands=true`，`each_acceptor_holds_all_instances=true`。若把「3 个副本」理解成「应该有 3 个 Paxos 实例」，就会期待 `instance_count=3`，这个错误模型被 `wrong_model_rejected=true` 拒绝。再来第 3 条命令，出现的是实例 3，不是第 4、第 5 个 acceptor，也不是一套新的集群。

论文 §3 的原句是：用一串互相独立的 Paxos 共识实例实现状态机，第 `i` 个实例选出第 `i` 条命令。阅读实现时，要把「日志有多长」和「日志存了几份」分开看。

</details>

## 推荐资源

- [Lamport — Paxos Made Simple 论文](https://lamport.azurewebsites.net/pubs/paxos-simple.pdf)（Leslie Lamport，英文论文，专家，免费）：用于核对 proposal number 与 promise、accepted value，以及多数派交集和当前提案必须继承的约束，将结论落实到本节点实验。2026-11-14 该地址返回 200，无跳转，95019 字节，`Content-Type` 为 `application/pdf`；题名 Paxos Made Simple，作者 Leslie Lamport，文内标注 01 Nov 2001；pdfinfo 记录 Creator 为 TeX output 2001.11.01:1449，CreationDate 为 Wed Nov 7 08:48:16 2001 CST，共 14 页，文内正文页码至第 11 页。

## 本课小结

- 提案由编号和值组成。编号必须唯一，必须来自不相交集合，不能复用；更高的编号只启动新的一轮两阶段，不能改写已被选中的值。`proposal_number_bounds` 里，`0.03` 三台忽略过期的 `n=1` prepare，`0.07` 三台拒绝过期的 `n=2` accept，`0.08` 复用 `n=3` 和跨集合使用 `n=4` 都被拒绝，`0.10` 合法的 `n=3` 才选出草稿。
- Phase 1 的 promise 带上已接受的最高编号提案，Phase 2 必须发出继承得到的值，或在没有 accepted value 时发出 proposer 自选的值。主实践 `0.04` `{a1,a2}` 接受并选中 `Weekly digest (draft)`；`0.06` `p2` 想写修订，却从 `a2` 继承了 `n=1` 的草稿；`0.08` 发出去的仍是草稿，`ignored_own_value=true`，`revised_never_chosen=true`。`a2` 在 `0.08` 重启后持久状态不变。
- 多数派交集保证：已被选中的值一定出现在任意新的 prepare 多数派里。主实践的交集是 `{a2}`。对照实验里，草稿只被 `a1` 接受、尚未选中时，`p2` 的 `{a2,a3}` 与 `a1` 不相交，可以选出修订；部分接受不是选中。
- 活锁不是安全性失败。交错到 `0.18` 仍无值被选中，`accept_rejected_during_livelock=12`，但 `safety_during_livelock=true`；只让 `p1` 继续后，`0.20` 选出草稿。选主是为了进度。
- Paxos 实例数量不等于数据副本数量。3 个 acceptor 上的 2 个实例选出 2 条命令，`instances_equal_replicas=false`，`instances_equal_commands=true`。
- 稳定领导可以复用 Phase 1，这是论文 §3 的优化，不是 §2 单决议核心。20 条命令时，单决议 `0.80` 秒、`240` 次 RPC，教学 Multi-Paxos 对照 `0.42` 秒、`126` 次 RPC。这些时间是教学虚拟时钟上的数字，不是生产 RTT。
- 教学引擎 `PaxosEngine` 不是完整 Paxos，也不是完整 Multi-Paxos；它没有选主、没有 no-op 填洞、没有成员变更，持久化是 dict，不是磁盘；`pause()` 与 `isolate()` 是全知信号。

如果有一句答不上来，先回到对应的实验字段，再回到论文 §2.2 的 P2c 与两阶段、§2.4 的活锁与 distinguished proposer、§2.5 的不相交编号和稳定存储，以及 §3 的多实例与稳定领导，不要只把「多数派同意了就能改成自己的值」背下来。
