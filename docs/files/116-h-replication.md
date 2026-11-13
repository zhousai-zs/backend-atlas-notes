# 第 116/208 课：复制状态机

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-replication` |
| 英文名 | Replicated state machine |
| 领域 / 阶段 | H / 第 4 阶段 |
| 子类 | 复制与共识 |
| 难度 | 深入 |
| 前置节点 | [`h-consistency`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html)、[`g-wal`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/105.html) |
| 标签 | `Replicated state machine`、`Distributed` |
| 参考资料 | [Raft — In Search of an Understandable Consensus Algorithm](https://raft.github.io/raft.pdf)（Ongaro & Ousterhout，英文文档，深入，免费；2026-11-13 该地址返回 200，无跳转，567533 字节，Content-Type 为 `application/pdf`。题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页） |
| 摘要 | 将确定性命令按同一顺序应用到多个状态机，可使副本在相同日志前缀上得到相同状态。 |

> **边界说明**：本课里的「内容后台」「文章标题 `Weekly digest`」「键 `article:01:title`」都是**教学假设**，用来让三台服务器、一条命令日志和用户可见结果变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `RsmEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是完整 Raft，不是 Paxos，也不是 Jepsen**。它是单进程仿真；有指定 leader `s1`，没有选举，任期恒为 `1`，选举是下一课 `h-raft-election` 的内容；没有 RequestVote、没有日志冲突覆盖、没有成员变更、没有 InstallSnapshot RPC。它没有 TCP，没有真实丢包；复制延迟按单向建模，follower 收到 AppendEntries 的同一瞬间，leader 就更新 `match_index`，这不是真实 RTT。`Clock` 是虚拟时钟。三台服务器是 `s1`、`s2`、`s3`，多数派为 `2`，`s2` 延迟 `0.08`，`s3` 延迟 `0.16`。非空初始 KV 被记成 `index=0` 的快照，这是教学便利（Raft 日志从 index 1 起），以免崩溃恢复丢掉从未写入日志的初始键；`encoded_size` 用教学结构的 `repr` 字节数，不是 Raft 磁盘格式。官方材料已于 **2026-11-13** 用 curl 核对：`https://raft.github.io/raft.pdf` 返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`。教学实验里的时间与字节都来自 `/workspace/atlas/lessons/practice116/results.json`，是教学虚拟时钟与教学编码上的数字，不是生产 RTT，也不是任何磁盘格式或网络帧的基准。

## 导读

### 一个直觉：同一条命令，为什么要在三台机器上再执行一遍

内容后台（教学假设）要把一篇已发布文章的标题从 `Weekly digest` 改成 `Weekly digest (revised)`，同时把阅读数从 `0` 递增两次。单机做法很直接：改内存里的 KV，写回磁盘，返回用户。机器一旦宕机，用户就得等它重启；如果磁盘坏了，数据可能一起消失。

复制状态机换了一个角度：不只保存「当前状态」，还把「让状态变成现在这样的命令」按顺序记进日志，再把同一串命令按同一顺序交给多台服务器各自执行。只要命令是确定性的，初始状态相同，命令序列相同，每台服务器最终就会算出相同的状态和相同的输出序列。日志里有 `SET title=Weekly digest`、`SET views=0`、`SET title=Weekly digest (revised)`、`INCR views`、`INCR views` 这五条命令，`r1` 和 `r2` 从头重放，`applied_count` 都是 `5`，标题都是 `Weekly digest (revised)`，`views` 都是 `2`，校验和都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`。

这个想法看起来只是「多存一份」，真正的难点在顺序和提交：副本什么时候算真的拥有了这条命令，什么时候可以执行它，执行之后又怎样在崩溃后恢复到同一个状态。本课把这三件事拆开，用一台单进程教学引擎和一条五条命令的日志走一遍。

### 接上前置

前置的 [第 114 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html) `h-consistency` 把一致性落到带调用时间、返回时间的操作历史上：线性一致要求存在一个尊重实时顺序、又符合寄存器单线程语义的全序。复制状态机关注的是另一层：多台服务器上的状态机如何从同一条日志得到同一份状态。它不自动等于线性一致；本课的实验只讨论日志前缀上的状态一致性，不讨论读操作是否满足线性一致。

前置的 [第 105 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/105.html) `g-wal` 讲的是预写日志：先把要做的修改持久化，再改内存状态，崩溃后靠日志重放恢复。复制状态机把这条思路扩展到多台服务器，但多了一个关键区别：日志写下去不等于可以执行，条目还要经过提交。崩溃恢复时，`RsmEngine` 会把已提交条目重放到状态机，未提交条目留在日志里但不应用。

### 一次 SET 走进 RsmEngine 之后

教学引擎维护三台服务器，每台有 `log`、`kv`、`commit_index`、`last_applied`。`s1` 是指定 leader。客户端写只发给 `s1`：`append_on_leader()` 在 leader 日志末尾追加一条命令，并给两个 follower 各发一条 AppendEntries；`wait_for_commit()` 推进虚拟时钟，直到多数派拥有这条日志。多数派为 `2`，所以 `s1` 加上 `s2` 就够了，不必等 `s3`。

日志条目带 `index`、`term`、`command`；`term` 在本课恒为 `1`。`commit_index` 是已知已提交的最高日志 index，`last_applied` 是已应用到状态机的最高日志 index，两者初值都是 `0`，都单调递增。`inspect()` 把「哪些服务器拥有这条日志」「是否已提交」「哪些服务器已应用」分开列出，正好对应本课要区分的三个状态。

### 教学场景（教学假设）

假设内容后台用 `article:01:title` 保存已发布文章的标题，`article:01:views` 保存阅读数，`article:01:published_at` 保存发布时间，`article:01:token` 保存一个用于演示随机输入的令牌。初值是 `Weekly digest` 与 `views=0`；一次编辑写成 `Weekly digest (revised)`，另一侧的冲突写写成 `Weekly digest (hotfix)`。引擎参数是 `local_step_s=0.01`、`replicate_delay_s=0.08`、`slow_follower_delay_s=0.16`，多数派为 `2`。这些数字都是虚拟时钟上的教学值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **命令确定性**：状态机按日志顺序执行命令，只有命令在每台副本上产生相同效果，重放才会得到相同状态；随机数和当前时间若在应用时临时读取、又没有写进日志，就会让副本分叉。
2. **日志提交与应用**：已复制、已提交、已应用是三个不同状态；条目被多数派复制后才提交，leader 决定何时可以应用，follower 要等新的 `commitIndex` 才能应用。
3. **快照与重放**：日志不能无限增长；快照把某一提交点之前的全部状态写成一份，之后只保留后缀日志，重放「快照 + 后缀」应与重放完整日志得到同一状态。

## 学习目标

- 能解释命令确定性及适用边界，说明随机数与当前时间为什么必须作为已记录的输入进入日志。
- 能区分已复制、已提交、已应用，并用日志索引和 `commit_index`、`last_applied` 说明副本之间的差异。
- 能独立完成实践：为内存 KV 增加命令日志，重放到两个副本比较校验和，并完成快照截断与重放对比。

## 为什么后端要懂

公众号内容后台（教学假设）修改已发布标题后，编辑会立刻看到保存结果，线上读流量可能落到不同副本。如果保存接口只在本地写内存就返回，主副本崩溃后这次修改可能丢失；如果消息已经复制到另一台机器，但那台机器还没收到「可以应用」的通知，它读到的仍是旧标题。把「已复制」当成「已提交」、把「已提交」当成「已应用」，会让团队对故障后的可见状态做出错误承诺。

复制状态机的价值在于把「恢复」变成可计算的事情：命令按固定顺序写进日志，提交点明确，恢复时从快照和已提交日志重放。这样，进程重启、单机磁盘故障甚至整机替换后，状态都可以重建到同一个校验和。代价是网络复制与日志持久化的成本；本课会把这两个数字都算出来。

## 核心知识点

### 1. 命令确定性

Ongaro 与 Ousterhout 在第 2 节把复制状态机写成：一组服务器上的状态机计算同一份状态的相同副本，即使部分服务器宕机也能继续工作；它通常用复制日志实现。每台服务器保存一份包含一系列命令的日志，并由自己的状态机按顺序执行；各日志包含相同顺序的相同命令，因此各状态机处理同一命令序列。因为状态机是确定性的（Since the state machines are deterministic），每一台都会算出相同的状态和相同的输出序列。

确定性在这里是可检查的条件：相同的初始状态，加上相同的命令序列，必须得到相同的结果。`SET` 写固定值、`INCR` 在当前状态上自增，都只依赖状态机自己的状态和命令参数，所以五条命令重放到两个全新副本 `r1`、`r2` 时，两边 `applied_count=5`，标题都是 `Weekly digest (revised)`，`views` 都是 `2`，校验和相同，都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`。`log_len=5`，`log_bytes=458`，`checksums_equal=true`。校验和是 64 位十六进制 SHA-256，对规范化编码后的 KV 计算；`458` 是教学结构 `repr` 的字节数，不是 Raft 磁盘格式。

反例来自「应用时才读取的外部输入」。在同样的三命令前缀之后再追加两条命令：`SET_NOW published_at` 与 `SET_RAND token`。`r1` 从时钟 `0.00` 起、每条命令推进 `0.01`，使用 `Random(1)`；`r2` 从 `0.10` 起，使用 `Random(2)`。重放后 `r1` 的 `published_at=0.04`、`token=tok_144272510`，校验和 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`；`r2` 的 `published_at=0.14`、`token=tok_926756583`，校验和 `40ea46a038ca8934eea3b9a868ca4e92e9dd9ad77c4554a2f89f30d68452389c`。`time_differs=true`，`token_differs=true`，`checksums_equal=false`。命令文本相同，日志顺序相同，状态却不同，原因就是两个未记录的输入在应用时才被读取。

纠正方式不是让两台机器「对齐时钟」，而是把外部输入变成命令的一部分：leader 只采样一次，把具体值写进日志，变成 `SET published_at=0.04` 与 `SET token=tok_144272510`。两边从不同时钟和不同 RNG 重放后，校验和仍相同，都是 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`，`acceptance_random_and_time_must_be_logged_inputs=true`。论文把确定性当成复制状态机的前提，没有逐步演示「随机数 / 当前时间若不写入命令就会分叉」；`SET_NOW` / `SET_RAND` 是本课的教学实验，不是论文里的例子。

命令确定性的适用边界也要说清楚。它要求状态机的转移只由「当前状态 + 命令」决定；命令里出现进程号、内存地址、未排序的哈希遍历顺序、外部服务返回值，都可能破坏这个条件。把随机数和时间记进日志，只是把不确定输入变成确定输入的一种做法；日志本身并不负责让不确定命令变确定。

### 2. 日志提交与应用

论文第 5.3 节把提交写得很具体：leader 把客户端命令追加到自己的日志，再并行发出 AppendEntries 复制该条目。条目被安全复制之后，leader 把它应用到自己的状态机，并把执行结果返回给客户端。由 leader 决定何时可以安全地把日志条目应用到状态机，这样的条目称为 committed。Raft 保证已提交条目是持久的，并且最终会被所有可用的状态机执行。一条日志在创建它的 leader 已把它复制到多数派服务器上时即被提交，这也会提交该 leader 日志里所有更早的条目。leader 跟踪它已知已提交的最高 index，并把它放进后续 AppendEntries，好让其他服务器最终知道。follower 得知某条已提交后，按日志顺序把它应用到本地状态机。

Figure 2 给出两个索引：`commitIndex` 是已知已提交的最高日志 index，初值为 `0`，单调递增；`lastApplied` 是已应用到状态机的最高日志 index，初值为 `0`，单调递增。All Servers 规则是：若 `commitIndex > lastApplied`，则递增 `lastApplied`，并把 `log[lastApplied]` 应用到状态机。

这三个状态不能混在一起：

- **已复制**：该条目已经出现在某台服务器的日志里，但还没有证据表明它被多数派保存。
- **已提交**：leader 已知该条目被多数派保存，`commit_index` 至少推进到该 index；提交代表不会再丢，并且最终会被所有可用状态机执行。
- **已应用**：状态机已经按顺序执行到该 index，`last_applied` 至少到达该 index；用户读到的新值来自这一步。

教学实验把这三个状态按虚拟时钟上的时间点拆开。三台服务器初值 `title=Weekly digest`、`views=0`，记在 `index=0` 的快照里。客户端在 `0.00` 对 `s1` 发出 `SET` 修订标题，`0.01` 这条日志只追加在 `s1`：`replicated_on=[s1]`，`committed=false`，`applied_on=[]`，三台标题都仍是 `Weekly digest`。`0.09`，`s2` 收到副本，多数派已具备该条目：`replicated_on=[s1,s2]`，`committed=true`，`s1` 已应用，`title=Weekly digest (revised)`；`s2` 已复制但 `last_applied=0`、`commit_index=0`，标题仍是 `Weekly digest`；`s3` 还没有这条日志。`0.10` 返回客户端，`latency=0.10`，状态与 `0.09` 相同，`applied_on` 仍只是 `[s1]`。

后面是传播与追平。`0.17` 第一条 AppendEntries 到达 `s3`：`replicated_on=[s1,s2,s3]`，但 `applied_on` 仍是 `[s1]`，`s3` 标题仍是 `Weekly digest`。`0.18`，`s2` 收到携带新 `commitIndex` 的心跳并应用，`applied_on=[s1,s2]`。`0.26`，`s3` 同样应用，三台都是修订标题。随后 `GET s1` 从 `0.26` 到 `0.28` 读到修订标题，`GET s3` 从 `0.28` 到 `0.30` 也读到修订标题。`acceptance_distinguish_replicated_committed_applied=true`。这里 `0.09` 的 `committed=true` 是「leader 已知多数派拥有」，不是「三台都已经执行」；`s2` 要等 `0.18` 的心跳，`s3` 要等 `0.26` 的那次应用。

提交与应用的分离在崩溃恢复里更明显。提交并应用后（`ret_s=0.10`，`commit_index=1`，`last_applied=1`，校验和 `3fbe21a8932982cad4a7d74fa20f37852ff0f168de708f3119ecd5a37232d956`），把 `s1` 宕机，再从快照 `0` 加已提交日志恢复：`reapplied=1`，标题仍是 `Weekly digest (revised)`，`views` 仍是 `0`，校验和与崩溃前相同。若只追加到 `s1`、尚未形成多数派（`0.01`，`replicated_on=[s1]`，`committed=false`，`applied_on=[]`），崩溃后恢复：未提交条目仍在日志（`last_log_index=1`），但不应用（`last_applied=0`，`reapplied=0`），标题仍是 `Weekly digest`，`views` 仍是 `0`。日志持久化只保证「这条命令还在这里」，提交才决定「它可以被执行」。

### 3. 快照与重放

论文第 7 节说，正常运行中日志会增长，实际系统里它不能无限变长；日志越长，越占空间，重放也越久。Snapshotting 是最简单的压缩方式：把当前系统状态整个写到稳定存储上的快照，然后丢弃截至该点的全部日志。每台服务器独立做快照，只覆盖已提交条目。快照元数据包括 last included index（快照替换的最后一条日志，也就是状态机当时已应用的最后一条）和 last included term。

教学实验在第 3 条命令之后做快照。快照的 `last_included_index=3`，`last_included_term=1`，里面 `title=Weekly digest (revised)`、`views=0`，校验和 `3fbe21a8932982cad4a7d74fa20f37852ff0f168de708f3119ecd5a37232d956`，`snapshot_bytes=211`。后缀是 `index` 为 `4` 和 `5` 的两条 `INCR`，`suffix_bytes=154`。完整五条日志重放，与「快照 + 后缀」只再应用 `2` 条，得到同一状态：标题 `Weekly digest (revised)`、`views=2`，校验和都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`，与第一条主线的确定性重放相同。`full_log_bytes=458`，`snapshot_plus_suffix_bytes=365`，少 `93` 字节。

这里的 `93` 字节只说明教学编码下快照加后缀比完整日志短，不代表生产环境一定省这么多；论文把快照作为最简单的压缩方式，快照本身仍要写稳定存储，快照之后还要管理保留点。`encoded_size` 算的是教学结构 `repr` 的字节数，`snapshot_bytes=211` 与 `suffix_bytes=154` 都不对应 Raft 的磁盘格式或网络帧大小。

本课还有一处教学便利：非空初始 KV 被记成 `index=0` 的快照，`last_included_index=0`、`last_included_term=0`，里面是 `title=Weekly digest`、`views=0`。Raft 日志从 index 1 开始，如果没有这层初始快照，崩溃恢复只重放日志就会丢掉从未写入日志的初始键。这个设计只服务于本课实验，不是论文规定的日志布局。

## 常见误区

节点写出的误区是：认为有复制线程就有共识。教学引擎用 `NaiveReplicator` 构造了反例：两个节点 `n1`、`n2`，没有 leader，没有多数派提交，`commit_index` 保持 `0`。`n1` 在 `0.01` 本地写入修订标题并立即应用，`n2` 在 `0.02` 本地写入 `Weekly digest (hotfix)` 并立即应用；两边都在 `index=1` 上各写了一条不同命令。复制线程在 `0.09` 把 `n1` 的条目拷到 `n2`、在 `0.10` 把 `n2` 的条目拷到 `n1`，两次都发生同 index 冲突，校验和仍然不同：`n1` 是 `9f29a794456f3da0846ca72f65faa563e1ba8139edffa83b2b4c7278ef1c369b`，标题仍是修订；`n2` 是 `8e9c11a9754146bf1e642b94bb9c64a99bec57811ac6be5e3a040ee2cbb4f442`，标题仍是 hotfix。`has_replication_thread=true`，`has_consensus=false`，`logs_conflict=true`，`pitfall_replication_thread_is_not_consensus=true`。

复制线程只负责「把字节搬到别处」，它不决定同一个 index 上哪条命令有资格留下，也不决定何时可以应用。没有这个决定权，两个节点都可以在本地先执行、先返回，冲突发生在日志层，用户看到的就是两台机器标题不同。对照 `RsmEngine`：两次写都经同一个 leader，修订标题在 `0.00`–`0.10` 提交为 `index=1`，hotfix 在 `0.10`–`0.20` 提交为 `index=2`；等到 `0.36`，三台都应用完，标题都是 `Weekly digest (hotfix)`，校验和都是 `afbdc4581e50f0756c54fddc243b06ac127d426f6bbd2b6393158f5f3c429af4`。同样的两条写，在一个日志顺序下收敛，在「各自先写再互拷」下分叉。

另一个相关误读是把「已复制」当成「已应用」。`0.17` 时 `s3` 已经有这条日志，`replicated_on=[s1,s2,s3]`，但 `applied_on` 仍是 `[s1]`，`s3` 标题仍是 `Weekly digest`；要等到 `0.26` 才应用。读请求如果路由到 `s3`，在 `0.17` 到 `0.26` 之间看到旧标题并不奇怪。把复制完成误当成读请求一定能看到新值，会让缓存、读路由和监控都做出错误判断。

## 设计权衡

节点给出的权衡是：日志复制增加网络与持久化成本，换取故障后的状态恢复。`tradeoff_log_replication_costs_network_and_persist_for_recovery=true`。

五条命令的日志是 `458` 字节，向两个 follower 各拷一份，`network_copy_bytes=916`。这 `916` 字节是教学结构 `repr` 长度的简单相加，不是生产环境的网络帧大小，也不含协议头、重试和确认包。换来的是已提交状态可以在崩溃后从快照加日志恢复：提交并应用后宕机，`reapplied=1`，标题仍是修订标题，校验和与崩溃前相同；只追加、未提交就宕机，未提交条目留在日志里但不应用，标题仍是 `Weekly digest`。如果没有日志，这两次恢复都无从谈起。

快照把「重放多久」这笔成本压下来。完整日志 `458` 字节，快照加后缀 `365` 字节，少 `93` 字节；重放时只需再应用 `2` 条，而不是 `5` 条。代价是快照本身要写稳定存储，快照之后要管理保留点，快照传输和安装也要额外的机制。本课引擎没有 InstallSnapshot RPC，只有本地 `take_snapshot()`，所以这里只能讨论存储与重放长度的对比，不能把结论扩展成完整的快照传输方案。

还有一笔账不在节点摘要里，却直接影响选择：多数派为 `2` 意味着 `s1` 加 `s2` 就能提交，`s3` 的 `0.16` 延迟不挡提交，但会让 `s3` 长时间停留在旧状态。副本数或多数派一变，提交所需的最少副本、读到的旧值窗口和网络字节数都要重新算，不能把这组数字直接搬到别的拓扑。

## 动手实践：为内存 KV 增加命令日志，重放到两个副本比较校验和

配套代码在 `/workspace/atlas/lessons/practice116/`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice116
./test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写入 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，字节数是教学结构的 `repr` 长度，不是生产 RTT、磁盘格式或网络帧大小。

**1. 确定性重放。** 同一条五条命令的日志重放到两个全新副本 `r1`、`r2`。命令依次是 `SET title=Weekly digest`、`SET views=0`、`SET title=Weekly digest (revised)`、`INCR views`、`INCR views`。两边 `applied_count=5`，标题都是 `Weekly digest (revised)`，`views` 都是 `2`，校验和相同，都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`。`log_len=5`，`log_bytes=458`，`checksums_equal=true`。

**2. 验收 1：未记录的随机数与当前时间。** 在同一条三命令前缀后再加 `SET_NOW published_at` 与 `SET_RAND token`。`r1` 从时钟 `0.00` 起、每条命令推进 `0.01`，用 `Random(1)`；`r2` 从 `0.10` 起，用 `Random(2)`。`r1` 的 `published_at=0.04`、`token=tok_144272510`，校验和 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`；`r2` 的 `published_at=0.14`、`token=tok_926756583`，校验和 `40ea46a038ca8934eea3b9a868ca4e92e9dd9ad77c4554a2f89f30d68452389c`。`time_differs=true`，`token_differs=true`，`checksums_equal=false`。纠正方式：leader 采样一次，把具体值写入命令（`SET published_at=0.04`、`SET token=tok_144272510`）。两边从不同时钟和不同 RNG 重放后校验和仍相同，都是 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`。`acceptance_random_and_time_must_be_logged_inputs=true`。

**3. 验收 2：已复制 / 已提交 / 已应用。** 三台服务器，初值 `title=Weekly digest`、`views=0`，记在 `index=0` 的快照里。客户端在 `0.00` 对 `s1` 发出 `SET` 修订标题。`0.01` 只追加在 `s1`：`replicated_on=[s1]`，`committed=false`，`applied_on=[]`，三台标题都仍是 `Weekly digest`。`0.09` `s2` 收到副本，多数派已具备该条目：`replicated_on=[s1,s2]`，`committed=true`，`s1` 已应用（`title=Weekly digest (revised)`），`s2` 已复制但 `last_applied=0`、`commit_index=0`，标题仍是 `Weekly digest`；`s3` 还没有这条日志。`0.10` 返回客户端，`latency=0.10`，状态与 `0.09` 相同，`applied_on` 仍只是 `[s1]`。`0.17` 第一条 AppendEntries 到达 `s3`：`replicated_on=[s1,s2,s3]`，但 `applied_on` 仍是 `[s1]`，`s3` 标题仍是 `Weekly digest`。`0.18` `s2` 收到携带新 `commitIndex` 的心跳并应用，`applied_on=[s1,s2]`。`0.26` `s3` 同样应用，三台都是修订标题。随后 `GET s1` 从 `0.26` 到 `0.28` 读到修订标题，`GET s3` 从 `0.28` 到 `0.30` 也读到修订标题。`acceptance_distinguish_replicated_committed_applied=true`。

**4. 崩溃恢复。** 提交并应用后（`ret_s=0.10`，`commit_index=1`，`last_applied=1`，校验和 `3fbe21a8932982cad4a7d74fa20f37852ff0f168de708f3119ecd5a37232d956`）把 `s1` 宕机，再从快照 `0` 加已提交日志恢复：`reapplied=1`，标题仍是 `Weekly digest (revised)`，`views` 仍是 `0`，校验和与崩溃前相同。若只追加到 `s1`、尚未形成多数派（`0.01`，`replicated_on=[s1]`，`committed=false`，`applied_on=[]`），崩溃后恢复：未提交条目仍在日志（`last_log_index=1`），但不应用（`last_applied=0`，`reapplied=0`），标题仍是 `Weekly digest`，`views` 仍是 `0`。

**5. 快照与重放。** 在第 3 条命令之后做快照，`last_included_index=3`，`last_included_term=1`，快照里 `title=Weekly digest (revised)`、`views=0`，快照校验和 `3fbe21a8932982cad4a7d74fa20f37852ff0f168de708f3119ecd5a37232d956`，`snapshot_bytes=211`。后缀是 `index` 为 `4` 和 `5` 的两条 `INCR`，`suffix_bytes=154`。完整五条日志重放，与「快照 + 后缀」只再应用 `2` 条得到同一状态：标题修订、`views=2`，校验和都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`（与实验 1 相同）。`full_log_bytes=458`，`snapshot_plus_suffix_bytes=365`，少 `93` 字节。

**6. 误区：复制线程不等于共识。** `NaiveReplicator` 没有 leader、没有多数派提交（`commit_index` 保持 `0`）。`n1` 在 `0.01` 本地写入修订标题并立即应用，`n2` 在 `0.02` 本地写入 hotfix 并立即应用；两边都在 `index=1` 上各写了一条不同命令。复制线程在 `0.09` 把 `n1` 的条目拷到 `n2`、在 `0.10` 把 `n2` 的条目拷到 `n1`，两次都发生同 index 冲突，校验和仍然不同（`n1`：`9f29a794456f3da0846ca72f65faa563e1ba8139edffa83b2b4c7278ef1c369b`，标题仍是修订；`n2`：`8e9c11a9754146bf1e642b94bb9c64a99bec57811ac6be5e3a040ee2cbb4f442`，标题仍是 hotfix）。`has_replication_thread=true`，`has_consensus=false`，`logs_conflict=true`。对照 `RsmEngine`：两次写都经同一个 leader，修订标题在 `0.00`–`0.10` 提交为 `index=1`，hotfix 在 `0.10`–`0.20` 提交为 `index=2`；等到 `0.36`，三台都应用完，标题都是 hotfix，校验和都是 `afbdc4581e50f0756c54fddc243b06ac127d426f6bbd2b6393158f5f3c429af4`。`pitfall_replication_thread_is_not_consensus=true`。

**7. 核对验收字段。** `acceptance_random_and_time_must_be_logged_inputs=true`，`acceptance_distinguish_replicated_committed_applied=true`，`pitfall_replication_thread_is_not_consensus=true`，`tradeoff_log_replication_costs_network_and_persist_for_recovery=true`。四个字段都能在 `results.json` 顶层找到，实验脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Raft — In Search of an Understandable Consensus Algorithm 中与 Replicated state machine 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 的实际打开情况如下。

- 节点给出的 [Raft 论文 PDF](https://raft.github.io/raft.pdf) 返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`。
- 题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页。
- 这份 PDF 没有单独给出 DOI，本课只使用节点给出的这一份材料。下文只引用它的定义与结论，不把论文没写过的句子说成论文原文。

论文里的定义与结论，本课按以下方式使用。

第 2 节 Replicated state machines：共识算法通常出现在复制状态机的语境中。一组服务器上的状态机计算同一份状态的相同副本，即使部分服务器宕机也能继续工作。复制状态机通常用复制日志实现（Figure 1）。每台服务器保存一份包含一系列命令的日志，并由自己的状态机按顺序执行；各日志包含相同顺序的相同命令，因此各状态机处理同一命令序列。因为状态机是确定性的，每一台都会算出相同的状态和相同的输出序列。保持复制日志一致是共识算法的工作。命令被妥善复制之后，每台服务器的状态机按日志顺序处理它们，再把输出返回给客户端；这些服务器看起来就像一台高度可靠的单一状态机。

第 5.3 节 Log replication：leader 把客户端命令追加到自己的日志，再并行发出 AppendEntries 复制该条目。条目被安全复制之后，leader 把它应用到自己的状态机，并把执行结果返回给客户端。由 leader 决定何时可以安全地把日志条目应用到状态机，这样的条目称为 committed。Raft 保证已提交条目是持久的，并且最终会被所有可用的状态机执行。一条日志在创建它的 leader 已把它复制到多数派服务器上时即被提交，这也会提交该 leader 日志里所有更早的条目。leader 跟踪它已知已提交的最高 index，并把它放进后续 AppendEntries，好让其他服务器最终知道。follower 得知某条已提交后，按日志顺序把它应用到本地状态机。

Figure 2：`commitIndex` 是已知已提交的最高日志 index（初值为 `0`，单调递增）；`lastApplied` 是已应用到状态机的最高日志 index（初值为 `0`，单调递增）。All Servers 规则：若 `commitIndex > lastApplied`，则递增 `lastApplied`，并把 `log[lastApplied]` 应用到状态机。

第 7 节 Log compaction：正常运行中日志会增长，实际系统里它不能无限变长；日志越长，越占空间，重放也越久。Snapshotting 是最简单的压缩方式：把当前系统状态整个写到稳定存储上的快照，然后丢弃截至该点的全部日志。每台服务器独立做快照，只覆盖已提交条目。快照元数据包括 last included index（快照替换的最后一条日志，也就是状态机当时已应用的最后一条）和 last included term。

教学引擎与论文的差异必须逐条记住。`RsmEngine` 是单进程仿真；有指定 leader `s1`，没有选举，任期恒为 `1`，选举是下一课 `h-raft-election` 的内容；不是完整 Raft（没有 RequestVote、没有日志冲突覆盖、没有成员变更、没有 InstallSnapshot RPC），也不是 Paxos / Jepsen / Redis / redis-py / 网络服务；没有 TCP，没有真实丢包；复制延迟按单向建模，follower 收到 AppendEntries 的同一瞬间，leader 就更新 `match_index`，这不是真实 RTT；`Clock` 是虚拟时钟；三台服务器 `s1`/`s2`/`s3`，多数派为 `2`，`s2` 延迟 `0.08`，`s3` 延迟 `0.16`；非空初始 KV 被记成 `index=0` 的快照，这是教学便利（Raft 日志从 index 1 起），以免崩溃恢复丢掉从未写入日志的初始键；`encoded_size` 用教学结构的 `repr` 字节数，不是 Raft 磁盘格式。论文把确定性当成复制状态机的前提，没有逐步演示「随机数 / 当前时间若不写入命令就会分叉」；`SET_NOW` / `SET_RAND` 是本课教学实验，不是论文里的例子。`NaiveReplicator`（复制线程、无多数派提交）是为节点误区构造的反例，不是论文中的算法。`results.json` 里的时间与字节只用于理解定义，不能当成生产基准。

## 自测题

**题 1.** 同一段命令文本，`r1` 从时钟 `0.00` 起用 `Random(1)`，`r2` 从时钟 `0.10` 起用 `Random(2)`，重放后 `checksums_equal=false`。为什么？应该怎样改？

<details>
<summary>答案</summary>

因为命令里包含 `SET_NOW` 与 `SET_RAND`，它们在应用时才读取当前时间和随机数，这两个输入没有写进日志。`r1` 得到 `published_at=0.04`、`token=tok_144272510`，校验和 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`；`r2` 得到 `published_at=0.14`、`token=tok_926756583`，校验和 `40ea46a038ca8934eea3b9a868ca4e92e9dd9ad77c4554a2f89f30d68452389c`。命令序列相同，但状态转移依赖了日志之外的输入，确定性被破坏。改法是 leader 采样一次，把具体值写成 `SET published_at=0.04` 与 `SET token=tok_144272510`；两边从不同时钟和不同 RNG 重放后校验和仍相同，都是 `9fee6f6e88a07050fe66028674b2d232c60b928353f4fc4be6bce3eda5c58346`，`acceptance_random_and_time_must_be_logged_inputs=true`。论文把确定性当成前提，这个 `SET_NOW` / `SET_RAND` 实验是本课的教学构造。

</details>

**题 2.** `0.09` 时 `committed=true`，为什么 `s2` 的标题仍是 `Weekly digest`，而不是已经应用的修订标题？`s2` 什么时候才应用？

<details>
<summary>答案</summary>

`committed=true` 表示 leader 已知该条目被多数派保存，`s1` 和 `s2` 都有这条日志，`replicated_on=[s1,s2]`。但 `s2` 还没有收到新的 `commitIndex`，它的 `commit_index=0`、`last_applied=0`，所以只完成了复制，没有应用；`s1` 已经在 `0.09` 应用，标题是修订标题。`0.18`，`s2` 收到携带新 `commitIndex` 的心跳并应用，`applied_on=[s1,s2]`，此时 `s2` 标题才变成修订标题。`s3` 在 `0.17` 已有日志，但 `applied_on` 仍是 `[s1]`，要到 `0.26` 才应用。已复制、已提交、已应用是三个不同状态，`acceptance_distinguish_replicated_committed_applied=true` 要的正是这个区分。

</details>

**题 3.** `NaiveReplicator` 明明有复制线程，为什么实验仍说 `has_consensus=false`？两次同 index 冲突说明了什么？

<details>
<summary>答案</summary>

复制线程只把条目从 `n1` 拷到 `n2`、从 `n2` 拷到 `n1`，它没有 leader，没有多数派提交，`commit_index` 保持 `0`，也不决定同一个 index 上哪条命令有资格留下。`n1` 在 `0.01` 本地写入修订标题并立即应用，`n2` 在 `0.02` 本地写入 hotfix 并立即应用，两边都在 `index=1` 上写了不同命令。`0.09` 与 `0.10` 的两次拷贝都撞上同 index 冲突，日志里各留一条，状态没有收敛，`n1` 校验和 `9f29a794456f3da0846ca72f65faa563e1ba8139edffa83b2b4c7278ef1c369b`、标题仍是修订，`n2` 校验和 `8e9c11a9754146bf1e642b94bb9c64a99bec57811ac6be5e3a040ee2cbb4f442`、标题仍是 hotfix，`logs_conflict=true`。对照 `RsmEngine`，两次写经同一个 leader 排成一个日志顺序：修订标题在 `0.00`–`0.10` 提交为 `index=1`，hotfix 在 `0.10`–`0.20` 提交为 `index=2`，`0.36` 三台都应用完，标题都是 hotfix，校验和都是 `afbdc4581e50f0756c54fddc243b06ac127d426f6bbd2b6393158f5f3c429af4`。有复制线程不等于有共识，`pitfall_replication_thread_is_not_consensus=true`。

</details>

## 推荐资源

- [Raft — In Search of an Understandable Consensus Algorithm](https://raft.github.io/raft.pdf)（Ongaro & Ousterhout，英文文档，深入，免费）：用于核对命令确定性与日志提交与应用，将结论落实到本节点实验。2026-11-13 该地址返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`；题名 In Search of an Understandable Consensus Algorithm (Extended Version)，作者 Diego Ongaro 与 John Ousterhout，Stanford University，文内标注 This tech report is an extended version of [32]，Published May 20, 2014，共 18 页。

## 本课小结

- 命令确定性要求状态转移只由「当前状态 + 命令」决定。五条命令重放到 `r1`、`r2`，两边 `applied_count=5`、标题 `Weekly digest (revised)`、`views=2`，校验和都是 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`；加入未记录的 `SET_NOW` 与 `SET_RAND` 后，`r1` 得到 `0.04` / `tok_144272510`，`r2` 得到 `0.14` / `tok_926756583`，`checksums_equal=false`。把采样值写进日志，两边才重新一致。
- 已复制、已提交、已应用不是同一件事。`0.01` 只有 `s1` 拥有日志，`committed=false`；`0.09` `s1` 与 `s2` 形成多数派，`committed=true`，但只有 `s1` 应用；`s2` 到 `0.18`、`s3` 到 `0.26` 才应用。崩溃后，已提交条目从快照加日志恢复，`reapplied=1`；未提交条目留在日志但不应用，`last_applied=0`、`reapplied=0`。
- 快照把重放长度压短，代价是额外的存储与快照管理。在第 3 条命令后做快照，`last_included_index=3`，`snapshot_bytes=211`，后缀 `154` 字节，快照加后缀共 `365` 字节，比完整日志 `458` 字节少 `93` 字节；只再应用 `2` 条就得到与完整重放相同的 `8b7f920799fe5ee24437df57b166669f5784a5171520f4830e748618fd53dcae`。
- 有复制线程不等于有共识。`NaiveReplicator` 在 `index=1` 上留下两条不同命令，`n1` 与 `n2` 校验和不同，`has_consensus=false`；`RsmEngine` 用同一 leader 排出 `index=1` 与 `index=2`，`0.36` 三台标题都是 hotfix，校验和都是 `afbdc4581e50f0756c54fddc243b06ac127d426f6bbd2b6393158f5f3c429af4`。日志复制用 `916` 字节的网络副本和持久化成本，换来崩溃后的状态恢复。

如果有一句答不上来，先回到对应的实验字段，再回到论文第 2 节关于确定性状态机与复制日志的几句、第 5.3 节关于 leader 提交与应用的几句，以及 Figure 2 的 `commitIndex` 与 `lastApplied` 规则，不要只把「有日志就能恢复」或「复制了就等于提交」背下来。
