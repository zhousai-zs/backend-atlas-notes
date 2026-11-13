# 第 118/208 课：Raft 日志一致性与提交

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-raft-log` |
| 英文名 | Raft log replication |
| 领域 / 阶段 | H / 第 4 阶段 |
| 子类 | 复制与共识 |
| 难度 | 深入 |
| 前置节点 | [`h-raft-election`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/117.html) |
| 标签 | `Raft log replication`、`Distributed` |
| 参考资料 | [Raft — Log Replication](https://raft.github.io/raft.pdf)（英文文档，深入，免费；2026-11-13 该地址返回 200，无跳转，567533 字节，Content-Type 为 `application/pdf`。题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页）；[etcd — Raft implementation](https://github.com/etcd-io/raft)（英文文档，进阶，免费；2026-11-13 该地址返回 200，无跳转，最终 URL 仍是 `https://github.com/etcd-io/raft`，页面 Content-Type 为 `text/html; charset=utf-8`，HTML 约 378059 字节，`<title>` 为 GitHub - etcd-io/raft: Raft library for maintaining a replicated state machine · GitHub；同日读取 `https://raw.githubusercontent.com/etcd-io/raft/main/README.md` 返回 200，11604 字节） |
| 摘要 | Raft 通过前缀匹配修复冲突日志，并限制 leader 提交旧任期条目的方式以保持安全。 |

> **边界说明**：本课里的「内容后台」「文章标题 `Weekly digest` / `Weekly digest (draft)` / `Weekly digest (revised)` / `Weekly digest (hotfix)`」「键 `article:01:title`」都是**教学假设**，用来让日志前缀、冲突和提交位置变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `RaftLogEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是完整 Raft，不是 Paxos，也不是 Jepsen**。它是单进程邮箱仿真，没有 TCP，没有真实丢包。没有快照传输（论文第 7 节），没有成员变更（论文第 6 节）。`pause()` 是全知冻结，`isolate()` 是全知分区；真实异步网络没有这种信号。持久化是教学用 dict，在 RPC 回复前 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。实验里每台服务器使用固定的 `election_timeout_s`，用来让时间线可重复；论文则是每次从 150–300ms 这类区间重新随机抽样。领导人用 `nextIndex` 与 `matchIndex` 判断复制进度，提交时不偷看其他节点的内存日志。本课最多对每个 follower 在途一条 AppendEntries，没有流水线。follower 在一致性检查通过后，把 `commitIndex` 设为 `min(leaderCommit, 本地最后一条日志的 index)`，包括 `entries` 为空的心跳；Figure 2 的措辞是 index of last new entry，但 §5.3 写明领导人会在心跳里携带 `commitIndex`，好让其他服务器最终知道。`unpause()` 会从当前虚拟时刻重新设置选举计时器，避免暂停期间已经到期的 deadline 在恢复后立刻触发一次多余选举。Figure 8 教学实验用 `plant_log` / `force_leader` 放入论文 Figure 8(c) 的日志构型，再按协议复制，不是从空集群经完整故障序列推演到该构型；`force_leader` 没有走真实投票，所以误区实验里 `s5` 随后也能以 term 4 当选（跟随者在 term 4 的 `votedFor` 仍为空）。`allow_commit_old_terms` 只出现在误区对照实验里，不是 Raft 规则。当已应用的条目被截断时，引擎会记录 `unsafe_applied_overwrite` 并做教学回滚；Raft 本身从不会回滚已应用的条目，因为当前任期提交限制让这种情况不可达。本课所有时间都来自 `/workspace/atlas/lessons/practice118/results.json`，是**教学虚拟时钟上的数字**，不是生产 RTT 或线上超时配置。参数是 `rpc_delay_s=0.01`、`heartbeat_interval_s=0.05`、`election_timeout` 区间 `0.15–0.30`，三节点多数派为 `2`，五节点多数派为 `3`；catch-up 实验的 `entry_delay_s=0.001` 是教学带宽模型：每条日志给该次 AppendEntries 单向延迟加 `0.001` 秒，不是测到的磁盘、网卡或生产 RTT。

## 导读

### 上一课留下的缺口

上一课的 `RaftElectionEngine` 解决了选主：在固定超时、隔离和暂停的配合下，`pause_leader` 实验里 `s1` 可以在 `0.17` 以 `votes=['s1','s2']` 当选 term 1 的 leader，`s2` 可以在 `0.52` 以 `votes=['s2','s3']` 当选 term 2 的 leader。它也能解释为什么落后节点不能靠抬高 term 赢得选举。但那一课的心跳不携带完整追赶：`log_freshness` 里新 leader `s2` 当选后，落后的 `s3` 标题仍然是空，`s3` 的日志也没有因为 `s2` 当选就自动补上。

这正是本课要补的三件事。leader 如何发现 follower 的日志从哪里开始不一致；发现之后如何删除冲突后缀并补上自己的条目；以及哪些条目可以因为「多数派已经有了」而被提交。只复制成功路径不够，网络恢复、旧 leader 带着未提交尾部回来、以及 Figure 8 那种旧任期条目已经在多数派上的情形，才是日志复制真正要处理的边界。

### 接上前置

前置的 [第 117 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/117.html) `h-raft-election` 讲的是任期、持久化投票、多数派和日志新旧比较。它留下了一个重要结论：新 leader 的日志至少要和投票者一样新，因此以往 term 里已提交的条目，在新 leader 当选时就应该在它的日志里。本课要把这个结论落到 `prevLogIndex` / `prevLogTerm` 的一致性检查、`nextIndex` / `matchIndex` 的复制进度，以及「当前任期提交限制」上。选主解决谁发号施令，日志复制解决命令如何按同一前缀落到多数派，并解释为什么已提交的命令不会在切主后消失。

### 教学场景（教学假设）

假设内容后台用三台服务器 `s1`、`s2`、`s3` 保存 `article:01:title`，初值是 `Weekly digest`。一次未完成的编辑留下未提交草稿 `Weekly digest (draft)`，新 leader 随后写入并提交修订 `Weekly digest (revised)`。五节点实验里再引入一条冲突的 `Weekly digest (hotfix)`，它由另一个任期写在同一个 index 上，用来观察旧任期条目为什么不能被副本计数直接提交。三节点实验的 `rpc_delay_s=0.01`、`heartbeat_interval_s=0.05`，选举超时分别是 `s1=0.15`、`s2=0.22`、`s3=0.28`，多数派为 `2`；五节点实验的多数派为 `3`。这些数字都是教学虚拟时钟上的值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **prevLogIndex 与 prevLogTerm**：leader 用这两个字段声明「我要求你在我前面这个位置上已经有一条同 term 的条目」，follower 用前缀匹配接受或拒绝，冲突后缀从第一个不同 term 的 index 开始删除。
2. **matchIndex 与 commitIndex**：leader 用每个 follower 的 `matchIndex` 计算多数派进度，只有多数派都覆盖到 N，且 N 的 term 等于当前任期，才把 `commitIndex` 推到 N。
3. **当前任期提交限制**：旧任期条目即使已经在多数派上，也不能只靠副本计数提交；必须先提交一条当前任期的条目，再让更早的条目因为 Log Matching Property 被间接提交。

## 学习目标

- 能解释 `prevLogIndex` 与 `prevLogTerm` 的作用及适用边界，说明为什么只比较 index 或日志长度不足以发现同 index 不同 term 的冲突。
- 能独立完成实践：注入旧 leader 未提交的尾部，恢复网络，验证新 leader 通过前缀匹配覆盖冲突日志，并保留已提交前缀。
- 能用日志图解释提交规则，并证明已提交命令不会在切主后消失；能说明仅按副本计数提交旧任期条目为什么不安全。

## 为什么后端要懂

公众号内容后台（教学假设）修改已发布标题时，编辑看到「保存成功」并不等于这条命令已经安全。如果旧 leader 在分区一侧追加了 `Weekly digest (draft)`，只复制到少数派就被暂停，客户端可能已经在旧 leader 上看到成功；网络恢复后，新 leader 若按「副本数够了就提交」处理旧任期条目，这条草稿可能先被当成已提交、再被另一个任期覆盖。用户看到标题从 `Weekly digest` 变成 `Weekly digest (draft)`，过一会儿又变成 `Weekly digest (revised)` 或 `Weekly digest (hotfix)`，中间那次「已提交」的承诺就失效了。日志复制的安全性不是抽象的共识练习，它决定了发布、审核、缓存失效和审计记录能不能依赖「已提交」这个词。

日志修复还决定了故障恢复的时间。一个 follower 在发布高峰期间断线，回来后 leader 需要从某个 `nextIndex` 开始补日志。如果一次只发一条，200 条积压就要 200 次 AppendEntries；如果批量发送，追赶会快很多，但 leader 也要为消息大小、内存和恢复时延做取舍。理解 `matchIndex`、`commitIndex` 和 `nextIndex` 的关系，才能在「已经提交」和「只是写到了某个 follower」之间画出正确边界。

## 核心知识点

### 1. prevLogIndex 与 prevLogTerm

Ongaro 与 Ousterhout 在 Figure 2 里列出 AppendEntries 的参数：`prevLogIndex`、`prevLogTerm`、`entries[]`、`leaderCommit`。`entries[]` 可以为空，这时就是心跳；也可以一次携带多条以提高效率。接收者的规则是：若日志在 `prevLogIndex` 处没有 term 等于 `prevLogTerm` 的条目，则回复 `false`（§5.3）；若已有条目与新条目冲突（同一 index、不同 term），删除该条目及其后所有条目；追加尚未拥有的新条目；若 `leaderCommit > commitIndex`，把 `commitIndex` 设为 `min(leaderCommit, index of last new entry)`。

这里的关键不是「比较长度」，而是「比较位置和 term」。日志是一个前缀结构：如果两个日志里有 index 与 term 都相同的条目，则它们保存同一条命令，并且这两份日志在该条目之前的所有前缀也完全相同，这就是 Log Matching Property。因此 leader 不需要逐条比对整个日志，只要让 follower 检查 `prevLogIndex` 上的 `prevLogTerm`，就能确认这一整段前缀。一个 index 上可能有不同 term 的不同命令，只看 index 会把冲突当成一致；只看日志长度，也会漏掉「长度相同但最后一个 term 不同」的情况。

leader 的易失状态在 Figure 2 里写得很具体：`nextIndex[]` 初始化为领导人最后一条日志的 index + 1；`matchIndex[]` 初始化为 `0`，单调增加。领导人规则是：若 `last log index ≥ nextIndex`，向该 follower 发送从 `nextIndex` 起的条目；成功则更新 `nextIndex` 与 `matchIndex`；若因日志不一致失败，则递减 `nextIndex` 并重试。领导人自己从不覆盖或删除自己日志里的条目，这是 Leader Append-Only Property。发现冲突后，领导人通过强制 follower 的日志与自己一致来处理：找到双方一致的最后一条，删除 follower 在那之后的条目，再把自己后面的条目发给 follower。

Figure 7 把新 leader 上任时的几种落后形态画在一起：follower 可能缺条目（a–b）、可能有额外未提交条目（c–d）、或两者都有（e–f）。新领导人把 `nextIndex` 初始化为自己最后一条之后（图中为 `11`）。教学实验里的两次拒绝，正好对应「缺条目」和「同 index 不同 term」两种情形。

先在 `0.38` 看旧 leader 的未提交尾部：

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]  commit=1  (term 1 leader)
s2  [1:t1:Weekly digest]                                commit=1
s3  []                                                  commit=0
```

`0.38` 暂停 `s1` 并恢复网络后，`s3` 在 `0.46` 成为 term 2 的 candidate（空日志），`s2` 在 `0.50` 成为 term 3 的 candidate（`last_log_index=1`、`last_log_term=1`），`0.52` 以 `votes=['s2','s3']` 当选。新 leader `s2` 的 `last_log_index=1`，所以给 `s1` 和 `s3` 的 `nextIndex` 都初始化为 `2`。`s3` 的日志是空的，`0.53` 它拒绝 `s2` 的 AppendEntries：`prevLogIndex=1`、`prevLogTerm=1`，但 `s3` 的 `last_log_index=0`。`s2` 在 `0.54` 把 `s3` 的 `nextIndex` 从 `2` 减到 `1` 并重试，这次从 index 1 开始发，`s3` 才接受。

第二次拒绝发生在旧 leader 回来时。`0.57` 恢复 `s1`，它此时仍带着 index 2 的 term 1 草稿。`s2` 给 `s1` 发 `prevLogIndex=2`、`prevLogTerm=3`，`s1` 在 `0.63` 拒绝，因为它在 index 2 上的 term 是 `1`，不是 `3`。`s2` 在 `0.64` 把 `s1` 的 `nextIndex` 从 `3` 减到 `2`，从 index 2 重发。`0.65`，`s1` 发生 `log_truncated`：从 index 2 截断，`overwritten` 是 `[{index:2,term:1,value:Weekly digest (draft)}]`，换上 term 3 的 `Weekly digest (revised)`。这就是前缀匹配修复冲突日志的过程：已提交的 index 1 没有被碰，冲突的 index 2 被删除并替换。

`0.57` 时刻的日志图能同时看到「已提交前缀」和「未提交冲突后缀」：

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]    commit=1  (paused, term 1 leader)
s2  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2  (term 3 leader)
s3  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2
```

这条时间线也说明了 `prevLogIndex` 的适用边界。它只能证明「`prevLogIndex` 及其之前的前缀一致」，不能证明 follower 已经拥有 leader 后面的条目；后者要靠 `entries[]` 和后续的 `matchIndex` 更新。`prevLogIndex=0` 表示空前缀，任何 follower 都能通过这一检查，所以新 leader 可以从头开始补。论文允许一次 AppendEntries 携带多条日志，心跳时 `entries[]` 为空；本课的教学引擎每个 follower 最多在途一条 AppendEntries，失败时基本按「递减 `nextIndex` 再重试」走，没有实现论文之外的冲突 term 提示优化。

### 2. matchIndex 与 commitIndex

`matchIndex` 是 leader 对每个 follower 复制进度的记录，`commitIndex` 是 leader 认为已经安全提交的前缀末尾。Figure 2 的领导人规则给出提交条件：若存在 `N > commitIndex`，多数派的 `matchIndex[i] ≥ N`，并且 `log[N].term == currentTerm`，则把 `commitIndex` 设为 `N`（§5.3、§5.4）。这里的「多数派」包含 leader 自己，因为 leader 持有 `N`；`matchIndex[i] ≥ N` 又因为 Log Matching Property 意味着 follower i 拥有 N 之前的全部条目。于是「多数派覆盖到 N」不是一句模糊的「副本数够了」，而是一个可以逐项核对的前缀条件。

`matchIndex` 单调增加，但单个 follower 的 `matchIndex` 高，不等于全局 `commitIndex` 就能前进。leader 要数多数派，还要检查 `log[N].term` 是否等于当前任期。教学引擎的 `holders_for(index)` 就是「领导人自己加 `matchIndex >= index` 的 follower 数」，`try_commit` 从最后一条往前找第一个满足多数派且 term 检查通过的 N。它只读 leader 维护的 `matchIndex`，不偷看其他节点的内存日志。

先看三节点实验里第一次提交。`0.22`，`s1` 把 index 1 的 `Weekly digest` 复制到 `s2`，`holders=2`，`matchIndex={s2:1,s3:0}`，`commit_index=1`。`s3` 当时被隔离，所以它的 `matchIndex` 还是 `0`。`0.30` 的日志图是：

```text
s1  [1:t1:Weekly digest]  commit=1  (term 1 leader)
s2  [1:t1:Weekly digest]  commit=1
s3  []                    commit=0  (isolated)
```

`0.30` 再隔离 `s2`，`s1` 追加 index 2 的 `Weekly digest (draft)`。到 `0.38`，只有 `s1` 自己有这条草稿，`holders=1`，不够三节点多数派 `2`，所以 `s1` 的 `commitIndex` 仍然是 `1`。这就是「旧 leader 未提交尾部」：它已经写进了 leader 的日志，也可能返回给客户端某种本地成功，但没有达到提交条件。

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]  commit=1
s2  [1:t1:Weekly digest]                                commit=1
s3  []                                                  commit=0
```

新 leader `s2` 在 `0.52` 上任时只带 index 1，`commitIndex=1`。它追加自己的当前任期条目 index 2 term 3 后，`0.56` 得到 `holders=2`、`matchIndex={s1:0,s3:2}`、`entry_term=3`，于是把 `commitIndex` 从 `1` 推到 `2`。`0.57`，`s2` 和 `s3` 都应用了 `Weekly digest (revised)`；`s1` 仍被暂停，保留 index 1 的已提交前缀和 index 2 的未提交草稿。之后 `s1` 恢复，`0.65` 只截断 index 2，index 1 没有被删除。

leader 提交后还要让 follower 知道。AppendEntries 携带 `leaderCommit`，follower 在一致性检查通过后把 `commitIndex` 设为 `min(leaderCommit, 本地最后一条日志的 index)`，然后应用。这个动作也发生在 `entries` 为空的心跳里。Figure 2 的措辞是 index of last new entry，但 §5.3 明确说领导人会在心跳里携带 `commitIndex`，好让其他服务器最终知道。教学引擎按后一种行为实现：即使心跳没有新条目，只要 `leaderCommit` 大于本地 `commitIndex`，follower 也会把提交位置推进到本地日志允许的范围。这个 `min` 很重要，因为 follower 可能还没收到 leader 后面的条目，不能直接把 `commitIndex` 设到 `leaderCommit`。

### 3. 当前任期提交限制

§5.3 说，一条由当前领导人创建的日志条目，在被复制到多数派之后即被提交；这也提交该领导人日志里所有更早的条目，包括前任领导人创建的条目。细节与安全性在 §5.4。§5.4.2 / Figure 8 又加了关键限制：领导人不能用更早任期的日志条目来判断提交。论文的原句是：

> Raft never commits log entries from previous terms by counting replicas. Only log entries from the leader’s current term are committed by counting replicas; once an entry from the current term has been committed in this way, then all prior entries are committed indirectly because of the Log Matching Property.

为什么不能按副本计数提交旧任期条目，Figure 8 的时间序列给出了反例。旧任期条目即使已经在多数派上，仍可能被覆盖。(a) S1 部分复制了 index 2；(b) S1 崩溃，S5 以 S3、S4 和自己的票当选 term 3 领导人，并在 index 2 接受一条不同的条目；(c) S5 崩溃，S1 重启并当选，继续复制，此时 term 2 的条目已在多数派上，但并未提交；(d) 若 S1 再次崩溃，S5 可能以 S2、S3、S4 的票当选并覆盖该条目；(e) 若 S1 在崩溃前把一条当前任期的条目复制到多数派，则该条目提交，S5 无法再当选，此前的条目也一并提交。差别就在最后一步：旧任期条目本身没有给「未来的 leader 一定包含它」提供足够约束，当前任期条目的提交才把整段前缀锁住。

教学实验用 `plant_log` / `force_leader` 放入 Figure 8(c) 的日志构型，再按协议复制，不是从空集群经完整故障序列推演到该构型。五节点多数派为 `3`。index 1 term 1 的 `Weekly digest` 在五台上且已知提交（`commit_index=1`）；`s1`、`s2`、`s3` 在 index 2 持有 term 2 的 `Weekly digest (draft)`；`s4` 只有 index 1；`s5` 在 index 2 持有冲突的 term 3 `Weekly digest (hotfix)`，并且已 crash。`force_leader` 让 `s1` 成为 term 4 领导人。

`0.20` 的复制结果如下：

```text
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1  (term 4 leader)
s2  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s3  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s4  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  (down)
    matchIndex={s2:2,s3:2,s4:2,s5:0}, holders_for(2)=4, commit_index=1
```

旧任期条目 term 2 的草稿已经到 `s1`、`s2`、`s3`、`s4` 四台，含领导人自己在内是五台中的四台，超过了多数派 `3`，但 `commit_index` 仍然是 `1`，`commits_before_current_term` 是空列表。原因不是副本数不够，而是 index 2 的 `term=2` 不等于当前任期 `4`。这就是「仅按副本计数提交任意旧任期条目」会踩的坑：四份副本看起来比三份多数派更安全，实际上仍然不能证明未来 leader 一定保留它。

`s1` 随后追加 index 3 term 4 的 `Weekly digest (revised)`。`0.24` 提交 `commit_index=3`（从 previous `1` 直接跳到 `3`），`entry_term=4`，`holders=3`，`current_term_rule=true`，`matchIndex={s2:3,s3:3,s4:2,s5:0}`。index 2 的草稿没有被单独计数提交，而是因为 index 3 的当前任期条目提交而被间接提交。`0.40` 的日志图是：

```text
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3  (leader)
s2  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s3  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s4  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]                                (down)
```

随后重启 `s5` 并暂停 `s1`，让 `s5` 尝试选举。`s5` 在 `0.56`、`0.71`、`0.88` 三次被 `s2`、`s3`、`s4` 以 `reason=log_not_up_to_date` 拒票（对应 term 4、5、7），`s5_elected_after_majority_has_term4=false`。`0.90`，活着的 `s2`、`s3`、`s4` 任期到了 `7`，但日志与标题仍是 `Weekly digest (revised)`，`commit=3`；`s5` 仍是 candidate，日志仍是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，因 restart 后 volatile 状态清零，`title` 为 `None`、`commit=0`。多数派上已提交的 `Weekly digest (revised)` 没有消失，`old_term_not_committed_by_counting=true`，`current_term_commit_also_commits_prior=true`。

这里有两个容易混淆的边界。第一，`holders_for(2)=4` 已经超过多数派，但提交条件还包括 term 检查，所以「超过多数派」不是充分条件。第二，`s5` 的日志里也有 index 2，但它的 term 是 `3`，和多数派在 index 2 上的 term `2` 不同；多数派在 `0.24` 之后最后一条已经是 index 3 term 4，而 `s5` 的最后一条还是 index 2 term 3，所以日志新旧比较会拒绝它。当前任期提交限制和 117 课的选举限制一起，才保证了已提交命令在切主后仍然存在。

## 常见误区

节点写出的误区是：仅按副本计数提交任意旧任期条目。`pitfall_commit_old_term_by_counting` 把同一条 Figure 8(c) 构型交给 `allow_commit_old_terms=true` 的错误规则，直接观察后果。`0.02`，`s1` 就按副本计数把 `commit_index` 提到 `2`，`entry_term=2`，`holders=3`，`current_term_rule=false`。`0.20`，`s1`、`s2`、`s3`、`s4` 的标题都变成 `Weekly digest (draft)`，`commit=2`。这时从表面看，草稿已经被多数派「提交」并应用。

接着 crash `s1`、restart `s5`。`0.37`，`s5` 以 `votes=['s2','s3','s5']` 当选 term 4 领导人。这里要按本课的边界说明理解：`force_leader` 没有走真实投票，跟随者在 term 4 的 `votedFor` 仍为空，所以 `s5` 能在教学实验里当选；这不是在说真实 Raft 会允许一个日志更旧的节点随便当选。`0.40`，`s2`、`s3`、`s4` 都发生 `log_truncated`，从 index 2 覆盖 `Weekly digest (draft)`，换上 term 3 的 `Weekly digest (hotfix)`。`1.00` 的日志图是：

```text
s2  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2  (leader)
s3  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s4  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]   commit=2  (crashed, not serving)
```

活着的 `s2` 到 `s5` 日志都是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，标题都是 `Weekly digest (hotfix)`。刚才被当成已提交的草稿消失了，`applied_draft_then_lost=true`。这就是「仅按副本计数提交任意旧任期条目」的后果：一个已经被应用、被客户端看到的命令，在切主后被另一个任期的日志覆盖。引擎在截断已应用条目时记录了 `unsafe_applied_overwrite` 并做了教学回滚，Raft 本身不会走到这一步，因为当前任期提交限制让「已应用条目被截断」这种情况不可达。

第二个误区是把「同 index」当成「同一条命令」。`prevLogIndex` 只说明位置，`prevLogTerm` 才说明这个位置属于哪个任期。实践一里 `s1` 在 `0.63` 拒绝 `s2` 的请求，不是因为 index 2 不存在，而是因为 index 2 上的 term 是 `1` 而请求要求 `3`。如果 follower 只比较 `prevLogIndex` 或只比较日志长度，它会把 `Weekly digest (draft)` 和 `Weekly digest (revised)` 当成同一条，冲突后缀就不会被删除。

第三个误区是把 follower 的 `commitIndex` 直接设成 `leaderCommit`。follower 可能还没收到 leader 后面的条目，必须取 `min(leaderCommit, 本地最后一条日志的 index)`。教学引擎在一致性检查通过后这样做，包括 `entries` 为空的心跳。否则 follower 会声称提交了自己没有的日志位置，应用时就会跳空。这个细节在论文 Figure 2 的措辞和 §5.3 的心跳说明之间容易读漏。

## 设计权衡

节点给出的权衡是：批量复制提高吞吐，积压日志会增加恢复时延。`batch_tradeoff` 用同一条三节点追赶路径量化了这两面。实验先隔离并暂停 `s3`，让 `s1` 和 `s2` 提交积压，再恢复 `s3` 测量追赶。`entry_delay_s=0.001` 是教学带宽模型：每条日志给该次 AppendEntries 单向延迟加 `0.001` 秒，不是测到的磁盘、网卡或生产 RTT；`rpc_delay_s=0.01` 是教学 RPC 延迟。

| `batch_size` | `backlog` | `catch_up_s` | `rpc_with_entries_to_s3` | `entries_sent_to_s3` | `t_heal` | `t_caught` |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 20 | 0.39 | 20 | 20 | 1.00 | 1.39 |
| 20 | 20 | 0.03 | 1 | 20 | 1.00 | 1.03 |
| 1 | 200 | 3.99 | 200 | 200 | 6.40 | 10.39 |
| 50 | 200 | 0.27 | 4 | 200 | 6.40 | 6.67 |
| 200 | 200 | 0.21 | 1 | 200 | 6.40 | 6.61 |

同样 200 条积压，一条一批要 `3.99` 秒、`200` 次 RPC，200 条一批只要 `0.21` 秒、`1` 次 RPC，`batching_faster_for_same_backlog=true`。批量把多次往返摊到一次 AppendEntries 上，所以在积压较大时优势明显。但「一次发完」也不是没有代价：同样一次发完，20 条积压的 `catch_up_s` 是 `0.03`，200 条积压的 `catch_up_s` 是 `0.21`，`larger_backlog_slower_even_when_fully_batched=true`。积压越大，leader 要发送、follower 要持久化和应用的数据越多，恢复时延仍然会上升。

真实实现还要在批量之外加边界。消息不能无限大，leader 不能为了追赶把一个 follower 的所有积压塞进一个 RPC；etcd 的 README 里 `Config` 示例含 `MaxSizePerMsg: 4096`，说明工业实现会限制单条消息的大小。本课的教学引擎用 `batch_size` 控制每次发送多少条，最多对每个 follower 在途一条 AppendEntries，没有流水线，所以这些数字只说明「批量减少 RPC 次数、积压增加恢复工作量」这个方向，不是生产容量规划。

第二笔权衡是提交延迟与安全性。实践二里，term 2 的草稿在 `0.20` 已经到了五台中的四台，但 `commit_index` 仍是 `1`；直到 `0.24` 当前任期的 index 3 复制到三台，`commit_index` 才从 `1` 跳到 `3`。等待一条当前任期条目会多花一轮复制，但换来的是旧条目被间接提交、未来 leader 无法覆盖已提交前缀。把这段时间省掉，就是误区实验里 `0.02` 的提前提交和 `1.00` 的草稿消失。

第三笔权衡是日志截断与持久化顺序。发现冲突后要删除 follower 的后缀，这要求存储层能按 index 覆盖已有条目；etcd README 明确写到，写入 Index i 的 Entry 时，任何已经持久化的 Index >= i 的条目都必须丢弃。批量复制让追赶更快，但也让一次 Ready 批次里可能包含多条目，持久化和发送的先后顺序必须按实现约定处理，不能把「先发消息、后写日志」当成安全顺序。

## 动手实践：注入旧 leader 未提交尾部，恢复网络后验证覆盖

配套代码在 `/workspace/atlas/lessons/practice118/`，包括 `engine.py`、`app.py`、`test_log.py`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice118
bash test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT，也不是线上超时配置。三节点实验的 `rpc_delay_s=0.01`、`heartbeat_interval_s=0.05`，多数派为 `2`；五节点实验的多数派为 `3`。

**1. 主实践 `overwrite_uncommitted_tail`。** 三台超时分别是 `s1=0.15`、`s2=0.22`、`s3=0.28`。`0.00` 三台都是 follower，`term=0`。`0.15`，`s1` 超时，成为 term 1 的 candidate。`0.17`，`s1` 以 `votes=['s1','s2']`、`vote_count=2`、`majority=2` 当选，`nextIndex` 均为 `1`，`matchIndex` 均为 `0`。`0.20` 三台都是空日志，`commit_index=0`。

`0.20` 隔离 `s3`，`s1` 本地追加 index 1 term 1 的 `SET article:01:title = Weekly digest`。`0.22` 该条目提交，`holders=2`，`matchIndex={s2:1,s3:0}`，`commit_index=1`。`0.30` 时 `s1` 和 `s2` 的日志是 `[1:t1:Weekly digest]`，标题是 `Weekly digest`，`commit=1`；`s3` 仍为空，`commit=0`。

```text
s1  [1:t1:Weekly digest]  commit=1  (term 1 leader)
s2  [1:t1:Weekly digest]  commit=1
s3  []                    commit=0  (isolated)
```

`0.30` 再隔离 `s2`，`s1` 本地追加 index 2 term 1 的 `SET article:01:title = Weekly digest (draft)`。`0.38` 时 `s1` 的日志是 `[1:t1:Weekly digest, 2:t1:Weekly digest (draft)]`，`commit` 仍为 `1`，因为只有 `1` 份副本，不能提交；`s2` 只有 index 1；`s3` 仍为空。这就是「旧 leader 未提交尾部」。

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]  commit=1
s2  [1:t1:Weekly digest]                                commit=1
s3  []                                                  commit=0
```

`0.38` 暂停 `s1`，并 heal 全部网络。`0.46`，`s3` 成为 term 2 的 candidate（空日志）；这一轮没有形成多数派。`0.50`，`s2` 成为 term 3 的 candidate（`last_log_index=1`、`last_log_term=1`）。`0.52`，`s2` 以 `votes=['s2','s3']` 当选 term 3 领导人，`last_log_index=1`，`nextIndex` 均为 `2`。`0.52` 的日志图里，`s1` 仍是被暂停的 term 1 leader，带着未提交的草稿；`s2` 是 term 3 leader，日志只有 `[1:t1:Weekly digest]`，`commit=1`；`s3` 日志仍空，`commit=0`。同一时刻可观察到两个 leader（`s1` 已暂停），这与第 117 课的双主观察一致，本课重点是随后的日志修复。

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]  commit=1  (paused, term 1 leader)
s2  [1:t1:Weekly digest]                                commit=1  (term 3 leader)
s3  []                                                  commit=0
```

`0.52`，`s2` 追加 index 2 term 3 的 `SET article:01:title = Weekly digest (revised)`。`0.53`，`s3` 拒绝 `s2` 的 AppendEntries：`prevLogIndex=1`、`prevLogTerm=1`，但 `s3` 的 `last_log_index=0`。`0.54`，`s2` 把 `s3` 的 `nextIndex` 从 `2` 减到 `1` 并重试。`0.56`，`s2` 提交 `commit_index=2`，`entry_term=3`，`holders=2`，`matchIndex={s1:0,s3:2}`。`0.57`，`s2` 和 `s3` 的日志是 `[1:t1:Weekly digest, 2:t3:Weekly digest (revised)]`，标题是 `Weekly digest (revised)`，`commit=2`；被暂停的 `s1` 仍是 `[1:t1:Weekly digest, 2:t1:Weekly digest (draft)]`，标题仍是 `Weekly digest`，`commit=1`。已提交的 index 1 在两边都还在。

```text
s1  [1:t1:Weekly digest] [2:t1:Weekly digest (draft)]    commit=1  (paused)
s2  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2  (leader)
s3  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2
```

`0.57` 恢复 `s1`。`0.63`，`s1` 拒绝 `s2` 的 AppendEntries：`prevLogIndex=2`、`prevLogTerm=3`，但 `s1` 在 index 2 上的 term 是 `1`（草稿）。`0.64`，`s2` 把 `s1` 的 `nextIndex` 从 `3` 减到 `2`。`0.65`，`s1` 发生 `log_truncated`：从 index 2 截断，`overwritten=[{index:2,term:1,value:Weekly digest (draft)}]`，换上 term 3 的 `revised`。`0.87`，三台状态一致：`s1` 是 follower term 3 commit 2 title `Weekly digest (revised)`；`s2` 是 leader term 3 commit 2 title `Weekly digest (revised)`；`s3` 是 follower term 3 commit 2 title `Weekly digest (revised)`。三份日志完全相同：

```text
s1  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2  (follower)
s2  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2  (leader)
s3  [1:t1:Weekly digest] [2:t3:Weekly digest (revised)]  commit=2  (follower)
```

`committed_index1_survived=true`，`draft_gone=true`，`logs_identical=true`，`all_titles_revised=true`。已提交的 `Weekly digest` 没有在切主后消失；未提交的 `draft` 被前缀匹配覆盖。

**2. 当前任期提交限制 `current_term_commit_restriction`。** 五节点，`majority=3`。用 `plant_log` 放入 Figure 8(c) 教学构型：index 1 term 1 的 `Weekly digest` 在五台上且已知提交（`commit_index=1`，标题 `Weekly digest`）；`s1`、`s2`、`s3` 在 index 2 持有 term 2 的 `draft`；`s4` 只有 index 1；`s5` 在 index 2 持有冲突的 term 3 `hotfix`，并且已 crash。`0.00` 用 `force_leader` 让 `s1` 成为 term 4 领导人。

`0.20`，`s1` 已把 term 2 的 `draft` 复制到 `s4`，`s4` 日志变为 `[1:t1:Weekly digest, 2:t2:Weekly digest (draft)]`。`matchIndex={s2:2,s3:2,s4:2,s5:0}`，`holders_for(2)=4`（含领导人自己为五台中的四台），但 `commit_index` 仍为 `1`，`commits_before_current_term` 为空列表。旧任期条目即使到了超过多数派的 `4` 个副本，也不会因计数而被提交，标题仍是 `Weekly digest`。`s5` 仍 down，日志为 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`。

```text
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1  (term 4 leader)
s2  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s3  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s4  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  (down)
    holders_for(2)=4, commit_index=1
```

`s1` 追加 index 3 term 4 的 `SET article:01:title = Weekly digest (revised)`。`0.24` 提交 `commit_index=3`（从 previous `1` 直接跳到 `3`），`entry_term=4`，`holders=3`，`current_term_rule=true`，`matchIndex={s2:3,s3:3,s4:2,s5:0}`。index 2 的 `draft` 被间接提交。`0.40`，`s1` 到 `s4` 的日志都是 `[1:t1:Weekly digest, 2:t2:Weekly digest (draft), 3:t4:Weekly digest (revised)]`，`commit=3`，标题 `Weekly digest (revised)`；`s5` 仍 down。

```text
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3  (leader)
s2  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s3  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s4  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)] [3:t4:Weekly digest (revised)]  commit=3
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]                                (down)
```

随后重启 `s5` 并暂停 `s1`，让 `s5` 尝试选举。`s5` 在 `0.56`、`0.71`、`0.88` 三次被 `s2`、`s3`、`s4` 以 `reason=log_not_up_to_date` 拒票（对应 term 4、5、7）。`s5_elected_after_majority_has_term4=false`。`0.90`，活着的 `s2`、`s3`、`s4` 任期到了 `7`，但日志与标题仍是 `Weekly digest (revised)`，`commit=3`；`s5` 仍是 candidate，日志仍是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，因 restart 后 volatile 状态清零，`title` 为 `None`、`commit=0`。多数派上已提交的 `revised` 没有消失。`old_term_not_committed_by_counting=true`，`current_term_commit_also_commits_prior=true`。

**3. 误区对照 `pitfall_commit_old_term_by_counting`。** 同一 Figure 8(c) 构型，`allow_commit_old_terms=true`。`0.02`，`s1` 就按副本计数把 `commit_index` 提到 `2`，`entry_term=2`，`holders=3`，`current_term_rule=false`。`0.20`，`s1` 到 `s4` 的标题都变成 `Weekly digest (draft)`，`commit=2`。然后 crash `s1`、restart `s5`。`0.37`，`s5` 以 `votes=['s2','s3','s5']` 当选 term 4 领导人；按边界说明，`force_leader` 没有写入 term 4 的 `votedFor`，所以这个教学构型里 `s5` 能当选。`0.40`，`s2`、`s3`、`s4` 都发生 `log_truncated`，从 index 2 覆盖 `draft`，换上 term 3 的 `hotfix`。`1.00`，活着的 `s2` 到 `s5` 日志都是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，标题都是 `Weekly digest (hotfix)`。刚才被当成已提交的 `draft` 消失了，`applied_draft_then_lost=true`。引擎在截断已应用条目时做了教学回滚，Raft 本身不会走到这一步。

```text
s2  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s3  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s4  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  commit=2
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]   commit=2  (crashed, not serving)
```

**4. 批量复制与积压恢复 `batch_tradeoff`。** `entry_delay_s=0.001`，`rpc_delay_s=0.01`。隔离并暂停 `s3`，让 `s1` 和 `s2` 先提交积压，再恢复 `s3` 测量追赶。每一行的 `t_heal` 是恢复网络的虚拟时刻，`t_caught` 是 `s3` 追上并提交积压的虚拟时刻，`catch_up_s` 是两者之差。

| `batch_size` | `backlog` | `catch_up_s` | `rpc_with_entries_to_s3` | `entries_sent_to_s3` | `t_heal` | `t_caught` |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 20 | 0.39 | 20 | 20 | 1.00 | 1.39 |
| 20 | 20 | 0.03 | 1 | 20 | 1.00 | 1.03 |
| 1 | 200 | 3.99 | 200 | 200 | 6.40 | 10.39 |
| 50 | 200 | 0.27 | 4 | 200 | 6.40 | 6.67 |
| 200 | 200 | 0.21 | 1 | 200 | 6.40 | 6.61 |

`batching_faster_for_same_backlog=true`：200 条积压，1 条一批用 `3.99` 秒、`200` 次 RPC；200 条一批用 `0.21` 秒、`1` 次 RPC。`larger_backlog_slower_even_when_fully_batched=true`：同样一次发完，20 条积压用 `0.03` 秒，200 条积压用 `0.21` 秒。`entry_delay_s=0.001` 只描述教学带宽模型，不是测到的磁盘、网卡或生产 RTT。

**5. 核对验收字段。** `acceptance_explain_commit_with_log_diagram=true`，`acceptance_committed_command_survives_leader_change=true`，`pitfall_counting_old_term_is_unsafe=true`，`tradeoff_batch_vs_backlog=true`。四个字段都能在 `results.json` 顶层找到，实验脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Raft — Log Replication 中与 Raft log replication 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 的实际打开情况如下。

- 节点给出的 [Raft 论文 PDF](https://raft.github.io/raft.pdf) 返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`。
- 题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页。`pdfinfo` 记录 Title 为 In Search of an Understandable Consensus Algorithm，Author 为 ongardie，ModDate 为 Wed May 21 03:38:48 2014 CST，Pages 18。本节课主要用 Figure 2（第 4 页）、§5.3 Log replication（第 7–8 页）、Figure 7、Figure 8 与 §5.4.2 Committing entries from previous terms（第 8–9 页）。
- 节点给出的 [etcd — Raft implementation](https://github.com/etcd-io/raft) 返回 200，无跳转（最终 URL 仍是 `https://github.com/etcd-io/raft`），页面 Content-Type 为 `text/html; charset=utf-8`，HTML 约 378059 字节，`<title>` 为 GitHub - etcd-io/raft: Raft library for maintaining a replicated state machine · GitHub。
- 同日读取的 [README](https://raw.githubusercontent.com/etcd-io/raft/main/README.md) 返回 200，11604 字节。

论文里的定义与结论，本课按以下方式使用。

Figure 2 的 AppendEntries 参数包括 `prevLogIndex`、`prevLogTerm`、`entries[]`（心跳可为空，也可一次发多条以提高效率）、`leaderCommit`。接收者：若日志在 `prevLogIndex` 处没有 term 等于 `prevLogTerm` 的条目则回复 `false`（§5.3）；若已有条目与新条目冲突（同一 index、不同 term），删除该条目及其后所有条目；追加尚未拥有的新条目；若 `leaderCommit > commitIndex`，把 `commitIndex` 设为 `min(leaderCommit, index of last new entry)`。

Figure 2 的领导人易失状态：`nextIndex[]` 初始化为领导人最后一条日志的 index + 1；`matchIndex[]` 初始化为 `0`，单调增加。领导人规则：若 `last log index ≥ nextIndex`，向该 follower 发送从 `nextIndex` 起的条目；成功则更新 `nextIndex` 与 `matchIndex`；若因日志不一致失败，则递减 `nextIndex` 并重试。提交规则：若存在 `N > commitIndex`，多数派的 `matchIndex[i] ≥ N`，并且 `log[N].term == currentTerm`，则把 `commitIndex` 设为 `N`（§5.3、§5.4）。

Log Matching Property：若两个日志里有 index 与 term 都相同的条目，则它们保存同一条命令；并且这两份日志在该条目之前的所有前缀也完全相同。领导人通过强制 follower 的日志与自己一致来处理不一致：找到双方一致的最后一条，删除 follower 在那之后的条目，再把自己后面的条目发给 follower。领导人自己从不覆盖或删除自己日志里的条目（Leader Append-Only Property）。

Figure 7：新领导人上任时，follower 可能缺条目（a–b）、可能有额外未提交条目（c–d）、或两者都有（e–f）。新领导人把 `nextIndex` 初始化为自己最后一条之后（图中为 `11`）。

§5.3：一条由当前领导人创建的日志条目，在被复制到多数派之后即被提交；这也提交该领导人日志里所有更早的条目，包括前任领导人创建的条目。细节与安全性在 §5.4。

§5.4.2 / Figure 8：领导人不能用更早任期的日志条目来判断提交。Figure 8 的时间序列说明：旧任期条目即使已经在多数派上，仍可能被覆盖。(a) S1 部分复制了 index 2；(b) S1 崩溃，S5 以 S3、S4 和自己的票当选 term 3 领导人，并在 index 2 接受一条不同的条目；(c) S5 崩溃，S1 重启并当选，继续复制，此时 term 2 的条目已在多数派上，但并未提交；(d) 若 S1 再次崩溃，S5 可能以 S2、S3、S4 的票当选并覆盖该条目；(e) 若 S1 在崩溃前把一条当前任期的条目复制到多数派，则该条目提交，S5 无法再当选，此前的条目也一并提交。

etcd README 开头称：Raft is a protocol with which a cluster of nodes can maintain a replicated state machine. The state machine is kept in sync through the use of a replicated log. 并指向同一份 `raft.pdf`。它写明：The library only implements the Raft algorithm; both network and disk IO are left to the user. 使用方必须自己实现传输层和把 Raft 日志与状态持久化的存储层。处理循环的第一步是读 `Node.Ready()`，其中与本课直接相关的边界是：

1. Write Entries, HardState and Snapshot to persistent storage in order, i.e. Entries first, then HardState and Snapshot if they are not empty. ... Note that when writing an Entry with Index i, any previously-persisted entries with Index >= i must be discarded.
2. Send all Messages to the nodes named in the `To` field. It is important that no messages be sent until the latest HardState has been persisted to disk, and all Entries written by any previous Ready batch (Messages may be sent while entries from the same batch are being persisted).

随后调用 `Node.Advance()`。README 里的 `Config` 示例含 `MaxSizePerMsg: 4096`，与本课「批量复制」对应。

教学引擎与论文、etcd 的差异要逐条记住。`RaftLogEngine` 是 Python 3 标准库写的单进程邮箱仿真，不是 Redis / redis-py / 网络服务 / 完整 Raft / Paxos / Jepsen；没有 TCP，没有真实丢包。它不是完整 Raft：没有快照传输（第 7 节）、没有成员变更（第 6 节）。`pause()` 是全知冻结，`isolate()` 是全知分区；真实异步网络没有这种信号。持久化是教学 dict，在 RPC 回复前 flush，不是磁盘，也没有 `fsync`；etcd 则把磁盘 IO 留给使用方，并要求按顺序写 Entries、HardState 和 Snapshot。`Clock` 是虚拟时钟。实验里每台服务器使用固定的 `election_timeout_s`，用来让时间线可重复；论文则是每次从 150–300ms 这类区间重新随机抽样。领导人用 `nextIndex` 与 `matchIndex` 判断复制进度，提交时不偷看其他节点的内存日志。本课最多对每个 follower 在途一条 AppendEntries，没有流水线。follower 在一致性检查通过后，把 `commitIndex` 设为 `min(leaderCommit, 本地最后一条日志的 index)`，包括 `entries` 为空的心跳；Figure 2 的措辞是 index of last new entry，但 §5.3 写明领导人会在心跳里携带 `commitIndex`，好让其他服务器最终知道。`unpause()` 会从当前虚拟时刻重新设置选举计时器。Figure 8 教学实验用 `plant_log` / `force_leader` 放入论文 Figure 8(c) 的日志构型，再按协议复制，不是从空集群经完整故障序列推演到该构型；`force_leader` 没有走真实投票，所以误区实验里 `s5` 随后也能以 term 4 当选。`allow_commit_old_terms` 只出现在误区对照实验里，不是 Raft 规则。当已应用的条目被截断时，引擎会记录 `unsafe_applied_overwrite` 并做教学回滚；Raft 本身从不会回滚已应用的条目，因为当前任期提交限制让这种情况不可达。

## 自测题

**题 1.** 用日志图解释 Raft 的提交规则。为什么实践二里 term 2 的草稿已经在五台中的四台上，`commit_index` 还是 `1`？后来为什么直接跳到 `3`？

<details>
<summary>答案</summary>

Raft 的提交规则是：若存在 `N > commitIndex`，多数派的 `matchIndex[i] ≥ N`，并且 `log[N].term == currentTerm`，则把 `commitIndex` 设为 `N`。三条件要同时满足，副本数只是其中之一。

`0.20` 的日志图：

```text
s1  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1  (term 4 leader)
s2  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s3  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s4  [1:t1:Weekly digest] [2:t2:Weekly digest (draft)]  commit=1
s5  [1:t1:Weekly digest] [2:t3:Weekly digest (hotfix)]  (down)
    matchIndex={s2:2,s3:2,s4:2,s5:0}, holders_for(2)=4, commit_index=1
```

index 2 的 term 是 `2`，当前任期是 `4`，`log[2].term == currentTerm` 不成立，所以即使 `holders_for(2)=4` 超过多数派 `3`，`commit_index` 仍是 `1`，`commits_before_current_term` 是空列表。旧任期条目不能按副本计数提交。

`0.24`，`s1` 追加 index 3 term 4 的 `Weekly digest (revised)`，它到 `s2`、`s3` 后形成多数派 `3`。此时 `entry_term=4`，`current_term_rule=true`，`holders=3`，`matchIndex={s2:3,s3:3,s4:2,s5:0}`，于是 `commit_index` 从 `1` 直接跳到 `3`。index 2 的草稿是因为 index 3 的当前任期条目提交而被间接提交，不是因为它自己有 `4` 个副本。`0.40` 的日志图里 `s1` 到 `s4` 都是 `[1:t1:Weekly digest, 2:t2:Weekly digest (draft), 3:t4:Weekly digest (revised)]`，`commit=3`，标题 `Weekly digest (revised)`。

</details>

**题 2.** 证明已提交的命令不会在切主后消失。请用实践一里 index 1 的 `Weekly digest` 和实践二里 `Weekly digest (revised)` 的数字说明。

<details>
<summary>答案</summary>

实践一里，`0.22`，`s1` 把 index 1 term 1 的 `Weekly digest` 复制到 `s2`，`holders=2`，`matchIndex={s2:1,s3:0}`，`commit_index=1`；这是多数派提交，`s3` 当时被隔离，日志为空。`0.30` 时 `s1` 和 `s2` 都有 `[1:t1:Weekly digest]`，`commit=1`，`s3` 仍为空。`0.52` 新 leader `s2` 上任时，它的日志里已经有 index 1，`commit=1`；这说明新 leader 不是从空日志开始，已提交前缀在选举时就已存在。

随后 `s2` 在 `0.52` 追加 index 2 term 3 的 `Weekly digest (revised)`，`0.56` 提交 `commit_index=2`，`holders=2`，`matchIndex={s1:0,s3:2}`。`0.57`，`s2` 和 `s3` 的日志是 `[1:t1:Weekly digest, 2:t3:Weekly digest (revised)]`，`commit=2`；`s1` 仍被暂停，日志是 `[1:t1:Weekly digest, 2:t1:Weekly digest (draft)]`，`commit=1`。`0.57` 恢复 `s1` 后，`0.63` 它拒绝 `s2` 的 AppendEntries（`prevLogIndex=2`、`prevLogTerm=3`，但本地 index 2 的 term 是 `1`），`0.64` `nextIndex` 从 `3` 减到 `2`，`0.65` 只从 index 2 截断，`overwritten` 是 `[{index:2,term:1,value:Weekly digest (draft)}]`。已提交的 index 1 没有被删除。`0.87` 三台日志完全相同，都是 `[1:t1:Weekly digest, 2:t3:Weekly digest (revised)]`，`commit=2`，标题都是 `Weekly digest (revised)`，`committed_index1_survived=true`，`draft_gone=true`，`logs_identical=true`，`all_titles_revised=true`。

实践二里，`0.24` 提交 index 3 term 4 的 `Weekly digest (revised)`，`commit_index=3`，`holders=3`，`current_term_rule=true`，index 2 被间接提交。`0.40` `s1` 到 `s4` 的 `commit=3`，标题 `Weekly digest (revised)`。之后 `s5` 重启并尝试选举，`0.56`、`0.71`、`0.88` 三次被 `s2`、`s3`、`s4` 以 `reason=log_not_up_to_date` 拒票（对应 term 4、5、7），`s5_elected_after_majority_has_term4=false`。`0.90` 活着的 `s2`、`s3`、`s4` 任期到了 `7`，日志与标题仍是 `Weekly digest (revised)`，`commit=3`；`s5` 仍是 candidate，日志是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，`title` 为 `None`、`commit=0`。多数派上已提交的 `revised` 没有消失，`old_term_not_committed_by_counting=true`，`current_term_commit_also_commits_prior=true`。

两段实验合起来说明：已提交条目在新 leader 当选时已经存在于其日志中（117 课的选举限制），当前任期条目提交后又把更早的前缀一并提交（Log Matching Property），所以切主只会覆盖未提交后缀，不会让已提交命令消失。

</details>

**题 3.** 为什么仅按副本计数提交旧任期条目不安全？用 `allow_commit_old_terms=true` 的对照实验数字说明。

<details>
<summary>答案</summary>

按副本计数只检查「有多少台持有这个 index」，不检查这个 index 属于哪个任期。旧任期条目即使已经在多数派上，也可能被一个拥有更高最后任期日志的新 leader 覆盖。Figure 8 的时间序列说明了这一点，教学实验把 Figure 8(c) 的构型直接放进去观察后果。

`0.02`，`s1` 按副本计数把 term 2 的 `draft` 提交，`commit_index=2`，`entry_term=2`，`holders=3`，`current_term_rule=false`。`0.20`，`s1`、`s2`、`s3`、`s4` 的标题都变成 `Weekly digest (draft)`，`commit=2`，看起来草稿已经安全提交。随后 crash `s1`、restart `s5`，`0.37` `s5` 以 `votes=['s2','s3','s5']` 当选 term 4 领导人。这里要记住边界：`force_leader` 没有走真实投票，跟随者在 term 4 的 `votedFor` 仍为空，所以 `s5` 能在教学实验里当选；真实 Raft 的选举限制仍然会拒绝日志更旧的候选者。

`0.40`，`s2`、`s3`、`s4` 都发生 `log_truncated`，从 index 2 覆盖 `draft`，换上 term 3 的 `hotfix`。`1.00`，活着的 `s2` 到 `s5` 日志都是 `[1:t1:Weekly digest, 2:t3:Weekly digest (hotfix)]`，标题都是 `Weekly digest (hotfix)`。刚才被当成已提交的 `draft` 消失了，`applied_draft_then_lost=true`。这正是「仅按副本计数提交任意旧任期条目」的后果：一个已经被应用、被客户端看到的命令，在切主后被另一个任期的日志覆盖。

Raft 的规则是只按副本计数提交当前任期条目；当前任期条目提交后，更早的条目因为 Log Matching Property 被间接提交。实践二里 `0.20` 的 term 2 草稿已经在五台中的四台上，但 `commit_index` 仍是 `1`；直到 `0.24` 当前任期的 index 3 到三台，`commit_index` 才从 `1` 跳到 `3`。这条限制让「已应用条目被截断」在 Raft 中不可达；教学引擎在误区实验里记录 `unsafe_applied_overwrite` 并做回滚，只是为了让错误规则的后果可观察。

</details>

## 推荐资源

- [Raft — Log Replication](https://raft.github.io/raft.pdf)（Ongaro & Ousterhout，英文文档，深入，免费）：用于核对 `prevLogIndex` 与 `prevLogTerm`、`matchIndex` 与 `commitIndex`，以及 §5.4.2 的当前任期提交限制，将结论落实到本节点实验。2026-11-13 该地址返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`；题名 In Search of an Understandable Consensus Algorithm (Extended Version)，作者 Diego Ongaro 与 John Ousterhout，Stanford University，文内标注 This tech report is an extended version of [32]，Published May 20, 2014，共 18 页。
- [etcd — Raft implementation](https://github.com/etcd-io/raft)（英文文档，进阶，免费）：在工业实现中定位 `Ready`、日志持久化与消息发送边界。2026-11-13 该地址返回 200，无跳转（最终 URL 仍是 `https://github.com/etcd-io/raft`），页面 `Content-Type` 为 `text/html; charset=utf-8`，HTML 约 378059 字节，`<title>` 为 GitHub - etcd-io/raft: Raft library for maintaining a replicated state machine · GitHub；同日读取的 `https://raw.githubusercontent.com/etcd-io/raft/main/README.md` 返回 200，11604 字节。

## 本课小结

- `prevLogIndex` 与 `prevLogTerm` 是前缀匹配检查。实践一里 `0.53` `s3` 因 `last_log_index=0` 拒绝 `prevLogIndex=1`、`prevLogTerm=1`，`0.54` `nextIndex` 从 `2` 减到 `1`；`0.63` `s1` 因本地 index 2 的 term 是 `1` 而拒绝 `prevLogIndex=2`、`prevLogTerm=3`，`0.64` `nextIndex` 从 `3` 减到 `2`，`0.65` 只截断 index 2 的草稿。已提交的 index 1 没有被碰，`0.87` 三台日志完全相同。
- `matchIndex` 与 `commitIndex` 是提交进度。`0.22` `holders=2`、`matchIndex={s2:1,s3:0}`，`commit_index=1`；`0.56` `holders=2`、`matchIndex={s1:0,s3:2}`、`entry_term=3`，`commit_index=2`。follower 在一致性检查通过后把 `commitIndex` 设为 `min(leaderCommit, 本地最后一条日志的 index)`，心跳也携带 `leaderCommit`。
- 当前任期提交限制防止旧任期条目被副本计数误提交。实践二里 `0.20` term 2 草稿在五台中的四台上，`holders_for(2)=4`，`commit_index` 仍是 `1`；`0.24` 当前任期 index 3 到三台后，`commit_index` 从 `1` 跳到 `3`，index 2 被间接提交。`0.90` 已提交的 `Weekly digest (revised)` 仍在多数派，`s5` 三次被 `log_not_up_to_date` 拒票，`s5_elected_after_majority_has_term4=false`。
- 仅按副本计数提交旧任期条目会导致已应用命令被覆盖。误区实验 `0.02` 提前提交 term 2 草稿，`0.20` 四台标题变成 `Weekly digest (draft)`；`0.37` `s5` 当选 term 4 后，`0.40` 三台从 index 2 截断，`1.00` 活着的节点标题都变成 `Weekly digest (hotfix)`，`applied_draft_then_lost=true`。
- 批量复制减少 RPC 次数，但积压会增加恢复时延。200 条积压，1 条一批 `catch_up_s=3.99`、`200` 次 RPC；200 条一批 `catch_up_s=0.21`、`1` 次 RPC。20 条一次发完是 `0.03`，200 条一次发完是 `0.21`。`entry_delay_s=0.001` 是教学带宽模型，不是生产 RTT 或磁盘、网卡测量。
- 这些时间都是教学虚拟时钟上的数字，不是生产 RTT 或线上超时配置。教学引擎 `RaftLogEngine` 不是完整 Raft；它没有快照传输、没有成员变更，持久化是 dict，不是磁盘；`pause()` 与 `isolate()` 是全知信号。Figure 8 教学实验用 `plant_log` / `force_leader` 放入构型，不是从空集群推演完整故障序列。

如果有一句答不上来，先回到对应的实验字段，再回到论文 Figure 2 的 AppendEntries 参数与领导人规则、§5.3 的日志复制与提交、Figure 7 的落后形态，以及 §5.4.2 / Figure 8 的当前任期提交限制，不要只把「多数派有了就能提交」背下来。
