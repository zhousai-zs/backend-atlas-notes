# 第 117/208 课：Raft 任期与选主

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-raft-election` |
| 英文名 | Raft election |
| 领域 / 阶段 | H / 第 4 阶段 |
| 子类 | 复制与共识 |
| 难度 | 深入 |
| 前置节点 | [`h-replication`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/116.html) |
| 标签 | `Raft election`、`Distributed` |
| 参考资料 | [Raft — 选主与安全性](https://raft.github.io/raft.pdf)（英文文档，深入，免费；2026-11-13 该地址返回 200，无跳转，567533 字节，Content-Type 为 `application/pdf`。题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页）；[Raft — Visualization and resources](https://raft.github.io/)（英文文档，进阶，免费；2026-11-13 该地址返回 200，无跳转，37512 字节，Content-Type 为 `text/html; charset=utf-8`；页面 `<title>` 为 Raft Consensus Algorithm，页内有一段 Raft Visualization，称可在浏览器里与 Raft 集群交互，并点名可视化实现 RaftScope 与 The Secret Lives of Data） |
| 摘要 | Raft 以任期、投票限制与随机选举超时选择领导者；少数派不能形成新的有效多数。 |

> **边界说明**：本课里的「内容后台」「文章标题 `Weekly digest` / `Weekly digest (revised)`」「键 `article:01:title`」都是**教学假设**，用来让三台服务器的状态、任期和投票日志变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `RaftElectionEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是完整 Raft，不是 Paxos，也不是 Jepsen**。它是单进程邮箱仿真，没有 TCP，没有真实丢包。它不是完整 Raft：没有快照传输（论文第 7 节），没有成员变更（论文第 6 节）；心跳里的 AppendEntries 不携带日志条目，本课也不做 `nextIndex` 回退追赶，所以 `log_freshness` 实验里新 leader `s2` 当选后，落后的 `s3` 标题仍是空，这不是论文里的完整复制，日志一致性是下一课 `h-raft-log` 的内容。`pause()` 是全知冻结（类似暂停进程），`isolate()` 是全知分区；真实异步网络没有这种信号。持久化是教学用 dict，在 RPC 回复前 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。实验里每台服务器使用固定的 `election_timeout_s`，每次重置用同一长度，用来让时间线可重复；论文则是每次从 150–300ms 这类区间重新随机抽样。提交判断会查看 follower 日志里是否已有该条目（教学上的全知 `matchIndex`），不是只靠 AppendEntries 回复里的 `matchIndex` 字段。拒绝投票（包括 `log_not_up_to_date`）不会重置选举计时器；只有收到当前 leader 的 AppendEntries，或把票投出去，才重置。这是按 Figure 2 的 Followers 规则实现的，用来避免落后候选者靠不断抬高 term 打断别人的计时器。本课所有时间都来自 `/workspace/atlas/lessons/practice117/results.json`，是**教学虚拟时钟上的数字**，不是生产 RTT，也不是线上超时配置。

## 导读

### 一个直觉：谁来决定下一条命令写到哪里

上一课的 `RsmEngine` 有一个指定的 leader `s1`，没有选举，任期恒为 `1`。这足以演示日志复制、提交、应用和快照，但它回避了一个现实问题：`s1` 停机之后，谁来接替；如果两台机器都认为自己应该接替，同一段时间里会不会出现两个 leader。

内容后台（教学假设）要把 `article:01:title` 从 `Weekly digest` 改成 `Weekly digest (revised)`。客户端只知道把写请求发给「当前的 leader」，它不关心这个 leader 是 `s1` 还是 `s2`。如果 leader 在写入过程中暂停，跟随者必须在某个时刻发现「当前 leader 没有消息了」，然后推举一个候选者，拿到集群多数派的投票，再开始发送心跳。Raft 用任期（term）、持久化投票和随机选举超时把这件事拆成可检查的规则。

### 接上前置

前置的 [第 116 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/116.html) `h-replication` 讲的是复制状态机：命令按同一顺序进入日志，多数派复制后提交，提交后再应用。那一课有一处刻意简化：leader `s1` 是固定的，任期恒为 `1`，没有 RequestVote，也没有日志冲突覆盖。本课补上选主这一层，解释一个 term 里为什么最多只能选出一个拿到多数派的 leader，以及双节点集群为什么难以容忍单点故障。日志本身的新旧比较会出现在本课的投票规则里，但完整的日志冲突处理与追赶仍留给下一课。

### 教学场景（教学假设）

假设内容后台用三台服务器 `s1`、`s2`、`s3` 保存 `article:01:title`，初值是 `Weekly digest`，一次编辑写成 `Weekly digest (revised)`。主实验的参数是 `rpc_delay_s=0.01`、`heartbeat_interval_s=0.05`，引擎的 `election_timeout` 区间是 `0.15–0.30`，三台服务器的选举超时分别是 `s1=0.15`、`s2=0.22`、`s3=0.28`，三节点多数派为 `2`。两节点对照实验的多数派也是 `2`。这些数字都是教学虚拟时钟上的值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **term 与持久化投票**：任期是逻辑时钟，投票必须在回复 RPC 前落到稳定存储；重启后不能在同一任期再投一次。
2. **多数派与日志新旧规则**：候选者必须拿到整个集群的多数派，而且日志不能比投票者旧；这两条规则共同限制谁能当选。
3. **心跳与选举抖动**：leader 用心跳维持权威，follower 用选举超时发起选举；随机超时减少 split vote，短超时则把检测速度和误选举风险绑在一起。

## 学习目标

- 能解释 term 与持久化投票的作用及适用边界，说明 `currentTerm`、`votedFor` 为什么要在回复 RPC 前写入稳定存储。
- 能解释每任期最多一个多数派 leader 的原因，并说明双节点集群为何难以容忍单点故障。
- 能独立完成实践：运行三节点教学 Raft，暂停 leader 后观察 term、投票日志和领导者变化。

## 为什么后端要懂

公众号内容后台（教学假设）修改已发布标题后，编辑会立刻看到保存结果。如果保存请求落在旧 leader 上，而集群已经在另一侧选出了新 leader，旧 leader 可能仍然接受请求、仍然发送心跳，只是它发出的 AppendEntries 过不了网络分区。用户看到「保存成功」，另一侧的读流量却可能读到旧标题。把「leader 还在发心跳」当成「全局只有一个 leader」，会让故障切换、读写路由和监控告警都建立在错误前提上。

选主还决定可用性边界。三台服务器可以容忍一台故障，因为剩下两台仍能形成多数派 `2`；两台服务器的多数派是 `2`，暂停一台之后另一台只有自己的 `1` 票，选不出新 leader。这个结论不是实现细节，而是「必须获得整个集群多数派投票」的直接结果。理解这条边界，才能在部署拓扑、超时配置和故障演练中做出正确承诺。

## 核心知识点

### 1. term 与持久化投票

Ongaro 与 Ousterhout 在第 5.1 节把任期写成 Raft 的逻辑时钟。Raft 把时间划分为任意长度的 term，term 用连续整数编号。每个 term 以一次选举开始；若候选者获胜，就在该 term 余下时间担任 leader；若出现 split vote，该 term 会在没有 leader 的情况下结束，随后开始新的 term 和新的选举。Raft ensures that there is at most one leader in a given term。不同服务器可能在不同时刻观察到 term 之间的转换，因此任期是逻辑时钟，不是各机器完全一致的真实时间。候选者或 leader 发现自己的 term 过期时，会立即退回 follower；收到带陈旧 term 的请求时，会拒绝该请求。

Figure 2 的 Persistent state 有三项：`currentTerm` 是本服务器见过的最新任期，首次启动为 `0`，单调递增；`votedFor` 是当前任期把票投给了哪个 `candidateId`，若没有则为 `null`；`log[]` 是日志条目。括号里写明：Updated on stable storage before responding to RPCs。这条要求的意思是，服务器不能先回复「我把票投给你了」，再慢慢写磁盘；一旦回复，重启后就只能看到同一个 `votedFor`，否则同一任期可能投出两票。

RequestVote 接收者的规则同样在 Figure 2：若请求的 `term < currentTerm`，回复 `false`；若 `votedFor` 为 `null` 或就是该 `candidateId`，且候选者日志至少与接收者一样新（at least as up-to-date），则给票（§5.2、§5.4）。All Servers 规则是：若 RPC 的 term `T > currentTerm`，则令 `currentTerm = T`，转为 follower。Followers 规则是：若选举超时内既没有收到当前 leader 的 AppendEntries，也没有把票投给某个 candidate，则转为 candidate。Candidates 规则是：转为 candidate 时开始选举，递增 `currentTerm`、给自己投票、重置选举计时器、向其他服务器发 RequestVote；得到多数派投票则成为 leader；若收到新 leader 的 AppendEntries 则转为 follower；若选举超时则开始新一轮选举。每个服务器在同一 term 最多给一个 candidate 投票，按先到先得处理；§5.4 又加了日志新旧这条限制。

教学实验的第一条时间线来自 `pause_leader`。三台服务器在 `0.00` 都是 follower，`term=0`，选举截止时间分别是 `s1=0.15`、`s2=0.22`、`s3=0.28`。`0.15`，`s1` 超时，成为 `term 1` 的 candidate，`voted_for=s1`。`0.16`，`s2` 与 `s3` 都给 `s1` 投票，`durable_voted_for=s1`。`0.17`，`s1` 成为 leader，`votes=['s1','s2']`，`vote_count=2`，`majority=2`；`s3` 的票也在 `0.16` 打下，但多数派在 `s2` 的回复先到达时就已经够了。`0.20` 与 `0.30` 时三台都在 `term 1`，`s1` 为 leader，`s2`/`s3` 为 follower 且 `voted_for=s1`。`0.30` 暂停 `s1`（当时仍是 `term 1` 的 leader）。`0.50`，`s2` 超时，成为 `term 2` 的 candidate。`0.51`，`s3` 给 `s2` 投票。`0.52`，`s2` 成为 `term 2` 的 leader，`votes=['s2','s3']`。`0.53`，`s3` 也回到 `term 2` 的 follower，`leader_id=s2`。`0.70` 时活着的 leader 是 `s2`（`term 2`）；被暂停的 `s1` 仍自称 `term 1` 的 leader。`leaders_by_term` 为 `term 1 → s1`，`term 2 → s2`。

这条时间线把「任期递增」和「投票持久化」分开看。任期递增决定了 `s2` 的选举属于 `term 2`，不是把 `term 1` 的票重新数一遍；投票持久化决定了 `s2` 与 `s3` 在 `term 1` 给过 `s1` 之后，不会在同一个 term 里再给另一个 candidate 投票。`pause_leader` 里 `s1` 的暂停发生在 `0.30`，此时 `s1` 仍是 `term 1` 的 leader；这正好说明「暂停」与「任期变化」不是同一件事，被暂停的进程不会自动知道别处已经进入了更高的 term。

`persistent_vote` 把持久化的作用单独拆出来。持久化开启时，`s2` 在 `0.16` 把票给 `s1` 且 `durable_voted_for=s1`；崩溃再重启后，`current_term=1`、`voted_for=s1`、`durable_voted_for=s1`；`0.18` 拒绝 `s3` 在同一 term 的拉票，`reason=already_voted`，`durable_voted_for` 仍是 `s1`。不持久化 `votedFor` 的对照里，重启后 `voted_for` 与 `durable_voted_for` 都是 `null`，`0.18` 又把票给了 `s3`。`persisted_rejects_second_vote=true`，`not_persisted_grants_second_vote=true`。

这里的适用边界要说清楚。教学引擎的持久化是一个 dict，在 RPC 回复前 flush，不是磁盘，也没有 `fsync`；它只模拟「回复前必须先落盘」这个顺序约束。论文要求的是稳定存储，真实系统要考虑磁盘缓存、掉电和文件系统语义，不能把教学 dict 的行为直接当成生产持久化保证。持久化投票能阻止同一台服务器在同一个 term 投两次，但它不能阻止不同 term 的旧 leader 在自己那一侧继续自称 leader；后者要靠 term 比较、多数派和网络恢复后的退位来处理。

### 2. 多数派与日志新旧规则

Raft 的选举以整个集群的多数派为单位。三节点集群的多数派是 `2`，两节点集群的多数派也是 `2`。候选者若获得整个集群多数派在同一 term 的投票则获胜。多数派规则保证某一 term 最多一个候选者获胜，这就是 Figure 3 的 Election Safety：at most one leader can be elected in a given term（§5.2）。

为什么一个 term 里不可能有两个都拿到多数派的 leader，可以从投票限制直接推出来。假设同一 term 有两个候选者都拿到多数派，那么两个多数派集合必然有交集；交集里的服务器在同一 term 最多投一票，不可能同时把票投给两个候选者。所以两个多数派不可能同时成立。实验里的 `split_vote` 展示了另一种结果：`term 2` 里 `s2` 与 `s3` 都把票投给了自己，`0.51` 互相以 `already_voted` 拒绝对方，`term 2` 没有任何 leader（`leaders_in_term_2=[]`）；`at_most_one_leader_per_term=true` 说的不是「每个 term 一定有 leader」，而是「每个 term 最多一个 leader」。

§5.4.1 又给投票加了日志条件。A candidate must contact a majority of the cluster in order to be elected。RequestVote 携带候选者日志信息；若投票者自己的日志更新，则拒绝投票。比较两条日志新旧的方法是：比较最后一条的 index 与 term；若最后一条的 term 不同，则 term 更大的更新；若最后一条 term 相同，则更长的那份更新。这条规则保证以往 term 中已提交的条目，在每个新 leader 当选的那一刻就已在它的日志里。

`log_freshness` 把这条规则放进内容后台场景。`0.20` 隔离 `s3`，`s1` 追加 `SET article:01:title = Weekly digest (revised)`；`0.22` 该条目在 `s1`、`s2` 上提交（`holders=2`，`commit_index=1`）。`0.30` 暂停 `s1` 并恢复 `s3` 的网络。此时 `s3` 日志仍是 `last_log_index=0`、`last_log_term=0`，标题为空；`s2` 是 `index=1`、`term=1`，标题为 `Weekly digest (revised)`。`0.46`，`s3` 成为 `term 2` 的 candidate（空日志）。`0.47`，`s2` 拒绝，`reason=log_not_up_to_date`。`0.68`，`s2` 成为 `term 3` 的 candidate，`last_log_index=1`、`last_log_term=1`。`0.69`，`s3` 给票。`0.70`，`s2` 成为 `term 3` 的 leader。结束后 `s2` 标题仍是 `Weekly digest (revised)`，`s3` 标题仍为空；本课心跳不带日志，也不做追赶，所以落后的 `s3` 不会因为 `s2` 当选就自动补上这条记录。日志一致性是下一课 `h-raft-log` 的内容。

这条实验有两个容易混淆的点。`s3` 在 `0.46` 成为 candidate 时日志是空的，`s2` 在 `0.47` 拒绝它的理由不是「term 太小」，而是 `log_not_up_to_date`；即使 `s3` 的 term 更高，只要日志最后一条的 term/index 更旧，`s2` 也不会把票给它。另一个点是 `s2` 在 `0.68` 成为 `term 3` 的 candidate 时，`last_log_index=1`、`last_log_term=1`，`s3` 在 `0.69` 给它投票，是因为 `s2` 的日志至少和 `s3` 一样新；这里的「新」按最后一条的 term 与 index 比较，不是按谁先发起选举。

双节点集群的边界也由多数派定义直接给出。`two_node` 实验里 `n=2`、`majority=2`。`0.17`，`s1` 成为 `term 1` 的 leader，`votes=['s1','s2']`。暂停 `s1` 后，活着的 `s2` 在 `0.50`、`0.72`、`0.94`、`1.16` 四次成为 candidate，任期从 `2` 到 `5`，每次只有自己那一票；`new_leaders_after_pause=[]`，`live_leader_after_pause=[]`，`cannot_tolerate_single_failure=true`。`1.20` 结束时 `s1` 仍是被暂停的 `term 1` leader，`s2` 是 `term 5` 的 candidate，`voted_for=s2`。两节点的多数派是 `2`，一台暂停后活着的那一台只有 `1` 票，少数派不能形成新的有效多数，所以选不出新 leader。这不是论文里的原句，而是从「必须获得整个集群多数派的投票」推出来的；论文举的典型例子是五台集群，可容忍两台故障。

### 3. 心跳与选举抖动

第 5.2 节把心跳和选举连在一起。服务器启动时是 follower；只要收到来自 leader 或 candidate 的合法 RPC，就保持 follower。Leader 周期性发送心跳（不含日志条目的 AppendEntries）以维持权威。若 follower 在一段称为 election timeout 的时间内没有通信，则假定没有可用 leader 并开始选举：递增 `currentTerm`，转为 candidate，给自己投票，并行向其他服务器发 RequestVote。候选者持续到三种结果之一：(a) 获胜，(b) 另一台服务器确立为 leader，(c) 一段时间后仍无胜者。候选者若获得整个集群多数派在同一 term 的投票则获胜。获胜后发送心跳以确立权威并阻止新的选举。

候选者收到声称是 leader 的 AppendEntries 时，要按 term 判断：若其 term 至少与自己的 `currentTerm` 一样大，则承认该 leader 并退回 follower；若其 term 更小，则拒绝并继续做 candidate。如果许多 follower 同时成为 candidate，票可能被分掉，无人获得多数派；此时每个 candidate 超时后递增 term 再发起 RequestVote。若无额外措施，split vote 可能无限重复。Raft 使用随机选举超时，使 split vote 少见并很快被化解；超时从固定区间随机选取（例如 150–300ms），这样多数情况下只有一台先超时，它赢得选举并在其他服务器超时前发出心跳。同一机制也用于处理已经发生的 split vote：每个 candidate 在选举开始时重新随机化超时。

`split_vote` 实验把「固定超时导致同时参选」和「下一次超时错开」拆开。`s2` 与 `s3` 的超时同为 `0.22`。`0.30` 暂停 `s1` 后，`0.50` 两台同时成为 `term 2` 的 candidate，`0.51` 互相以 `already_voted` 拒绝对方，`term 2` 没有 leader。随后让 `s2` 的下一次超时更短，`0.69`，`s2` 成为 `term 3` 的 candidate，`0.70`，`s3` 给票，`0.71`，`s2` 成为 `term 3` 的 leader。`leaders_by_term` 为 `term 1 → s1`，`term 3 → s2`。这里 `term 2` 被浪费掉了，但 `at_most_one_leader_per_term=true` 仍然成立：`term 2` 没有 leader，并不等于出现两个 leader。

教学引擎与论文在这里有一个必须说明的差异。引擎里每台服务器使用固定的 `election_timeout_s`，每次重置用同一长度，这样时间线可重复；论文则是每次从 150–300ms 这类区间重新随机抽样。所以实验里 `s2` 与 `s3` 会同时超时，需要显式把 `s2` 的下一次超时改短，才能观察 split vote 被化解。不要把「教学里固定超时」理解成「Raft 不随机」；随机化是论文用来减少 split vote 的机制，本课只是把它换成了可复现的实验参数。

`timeout_tradeoff` 把超时的两难量化。暂停发生在 `0.30`。follower 超时 `0.15` 时，新 leader 在 `0.45` 产生，`detection_s=0.15`；超时 `0.30` 时，新 leader 在 `0.60` 产生，`detection_s=0.30`；`faster_detection=true`。这是故障确实发生时短超时的好处。对照「leader 仍活着」：`rpc_delay_s=0.08`、`heartbeat_interval_s=0.10` 时，超时 `0.12` 会在 `0.24` 就出现第一次误选举（`s2` 成为 `term 2` 的 candidate），共 `5` 次 `spurious_candidates`，`spurious_election=true`；同一延迟下超时 `0.30` 则 `spurious_candidates=[]`，`spurious_election=false`。短超时更快发现故障，也更容易在 leader 仍活着、只是心跳偏慢时误启动选举。

第 5.6 节把时间参数的选择写成不等式。safety must not depend on timing；availability 不可避免地依赖 timing。选主是 Raft 里 timing 最关键的部分。要选出并维持稳定 leader，须满足 `broadcastTime ≪ electionTimeout ≪ MTBF`。broadcast time 应比 election timeout 低一个数量级，以便 leader 可靠发送心跳、阻止 follower 开始选举；结合随机超时，该不等式也让 split vote 不太可能。election timeout 应比 MTBF 低几个数量级。leader 崩溃后，系统大约在一个 election timeout 内不可用。Raft 的 RPC 通常要求接收方把信息持久化到稳定存储，broadcast time 可能在 `0.5ms` 到 `20ms`，因此 election timeout 很可能在 `10ms` 到 `500ms`。这些毫秒数是论文对真实系统的估计，不要把它们和本课虚拟时钟上的 `0.15`、`0.30` 写成同一回事。

引擎还有一条按 Figure 2 实现的细节：拒绝投票（包括 `log_not_up_to_date`）不会重置选举计时器；只有收到当前 leader 的 AppendEntries，或把票投出去，才重置。这样做是为了避免落后候选者靠不断抬高 term 打断别人的计时器。论文的 Followers 规则把「没有收到当前 leader 的 AppendEntries」和「没有把票投给某个 candidate」并列为超时转 candidate 的条件；本课把「拒绝投票不重置计时器」作为实现这条规则的一种具体做法。

## 常见误区

节点写出的误区是：误以为 leader 心跳能阻止所有双主观察。`dual_leader_observation` 用「隔离仍在工作的 `s1`」而不是「暂停 `s1`」构造反例。`0.30` 隔离 `s1`，`0.50`，`s2` 成为 `term 2` 的 candidate，`0.52` 成为 leader（`votes=['s2','s3']`）。`0.70` 观察：`s1` 仍是 `term 1` 的 leader 且 `paused=false`，`s2` 是 `term 2` 的 leader，`two_leaders_observed=true`，`terms_during=[1,2]`。期间丢掉 `20` 条 AppendEntries（心跳），最早一条在 `0.32` 由 `s1` 发给 `s2`、`term=1`，说明旧 leader 还在发心跳，但过不了分区。`0.70` 恢复网络后，`0.73`，`s1` 退回 follower，term 变成 `2`，`leaders_after_heal=['s2']`。

这件事要分两层看。心跳没有阻止「同时看到两个 leader」这件事；它只是在连通的多数派里维持权威。旧 leader 在分区一侧仍然自称 leader，直到看到更大的 term。观察者若同时能看到分区两侧，就会看到 `s1` 的 `term 1` 和 `s2` 的 `term 2`，但这两个 leader 不属于同一个 term，Election Safety 并没有被破坏。把「任意时刻全局只有一个 leader」当成 Raft 的无条件保证，是误读；Raft 保证的是给定 term 至多一个 leader，并且多数派一侧能选出新 leader。恢复网络后，`s1` 看到 `s2` 的更高 term，才在 `0.73` 退回 follower。

另一个相关误读是把「持久化投票」当成「不会出现双主观察」。`persistent_vote` 里 `s2` 在 `0.16` 把票给 `s1`，重启后在 `0.18` 拒绝 `s3` 的同一 term 拉票（`reason=already_voted`），这确实阻止了同一 term 的重复投票；但 `dual_leader_observation` 里的两个 leader 属于 `term 1` 和 `term 2`，持久化投票并不能让被隔离的旧 leader 立刻退位。持久化投票解决的是「同一 term 不投两次」，term 比较和网络恢复解决的是「旧 leader 何时让位」。

## 设计权衡

节点给出的权衡是：短超时更快检测故障，也更容易误选举。`timeout_tradeoff` 的数字把两边都摆出来了：故障发生在 `0.30` 时，超时 `0.15` 让新 leader 在 `0.45` 产生，检测耗时 `0.15`；超时 `0.30` 让新 leader 在 `0.60` 产生，检测耗时 `0.30`。但 leader 仍活着、只是心跳偏慢时，`rpc_delay_s=0.08`、`heartbeat_interval_s=0.10`、超时 `0.12` 会在 `0.24` 就出现第一次误选举，共 `5` 次 `spurious_candidates`；同一延迟下超时 `0.30` 没有误选举。`tradeoff_short_timeout_faster_but_easier_to_mis_elect=true`。

第二笔权衡是持久化的成本与防止重复投票的收益。投票在 RPC 回复前写入稳定存储，会增加一次写盘延迟；换来的是重启后仍记得 `votedFor`，同一 term 不会投出第二票。`persistent_vote` 里持久化开启时 `s2` 在 `0.18` 拒绝 `s3`（`already_voted`），不持久化时 `0.18` 又把票给了 `s3`。这个对照只说明顺序约束的价值，不代表教学 dict 的 flush 等同于真实磁盘持久化。

第三笔权衡是副本数与容错。三节点多数派为 `2`，可以容忍一台故障；两节点多数派为 `2`，暂停一台后另一台只有 `1` 票，`two_node` 里 `s2` 在 `0.50`、`0.72`、`0.94`、`1.16` 四次参选都没有选出新 leader。论文举的典型例子是五台集群，可容忍两台故障。副本数一变，多数派、可用性边界和选举所需的最少票数都要重新算，不能把三节点的数字直接搬到两节点或五节点。

## 动手实践：运行三节点教学 Raft，暂停 leader 后观察 term 和投票日志

配套代码在 `/workspace/atlas/lessons/practice117/`，包括 `engine.py`、`app.py`、`test_election.py`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice117
bash test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT，也不是线上超时配置。实验里 `rpc_delay_s=0.01`、`heartbeat_interval_s=0.05`，三节点多数派为 `2`，两节点多数派为 `2`。

**1. 主实践 `pause_leader`。** 三台超时分别是 `s1=0.15`、`s2=0.22`、`s3=0.28`。`0.00` 三台都是 follower，`term=0`，选举截止分别是 `0.15` / `0.22` / `0.28`。`0.15`，`s1` 超时，成为 `term 1` 的 candidate，`voted_for=s1`。`0.16`，`s2` 与 `s3` 都给 `s1` 投票，`durable_voted_for=s1`。`0.17`，`s1` 成为 leader，`votes=['s1','s2']`，`vote_count=2`，`majority=2`；`s3` 的票也在 `0.16` 打下，但多数派在 `s2` 的回复先到达时就已经够了。`0.20` 与 `0.30` 时三台都在 `term 1`，`s1` 为 leader，`s2`/`s3` 为 follower 且 `voted_for=s1`。`0.30` 暂停 `s1`（当时仍是 `term 1` 的 leader）。`0.50`，`s2` 超时，成为 `term 2` 的 candidate。`0.51`，`s3` 给 `s2` 投票。`0.52`，`s2` 成为 `term 2` 的 leader，`votes=['s2','s3']`。`0.53`，`s3` 也回到 `term 2` 的 follower，`leader_id=s2`。`0.70` 时活着的 leader 是 `s2`（`term 2`）；被暂停的 `s1` 仍自称 `term 1` 的 leader。`leaders_by_term` 为 `term 1 → s1`，`term 2 → s2`。

**2. `persistent_vote`。** 持久化开启时：`s2` 在 `0.16` 把票给 `s1` 且 `durable_voted_for=s1`；崩溃再重启后 `current_term=1`、`voted_for=s1`、`durable_voted_for=s1`；`0.18` 拒绝 `s3` 在同一 term 的拉票，`reason=already_voted`，`durable_voted_for` 仍是 `s1`。不持久化 `votedFor` 的对照：重启后 `voted_for` 与 `durable_voted_for` 都是 `null`，`0.18` 又把票给了 `s3`。`persisted_rejects_second_vote=true`，`not_persisted_grants_second_vote=true`。

**3. `split_vote`。** `s2` 与 `s3` 的超时同为 `0.22`。`0.30` 暂停 `s1` 后，`0.50` 两台同时成为 `term 2` 的 candidate，`0.51` 互相以 `already_voted` 拒绝对方，`term 2` 没有 leader（`leaders_in_term_2=[]`）。随后让 `s2` 的下一次超时更短，`0.69`，`s2` 成为 `term 3` 的 candidate，`0.70`，`s3` 给票，`0.71`，`s2` 成为 `term 3` 的 leader。`0.90` 观察时 `s2` 仍是 `term 3` 的 leader。`leaders_by_term` 为 `term 1 → s1`，`term 3 → s2`。`at_most_one_leader_per_term=true`。

**4. `log_freshness`。** `0.20` 隔离 `s3`，`s1` 追加 `SET article:01:title = Weekly digest (revised)`；`0.22` 该条目在 `s1`、`s2` 上提交（`holders=2`，`commit_index=1`）。`0.30` 暂停 `s1` 并恢复 `s3` 的网络。`s3` 日志仍是 `last_log_index=0`、`last_log_term=0`，标题为空；`s2` 是 `index=1`、`term=1`，标题为 `Weekly digest (revised)`。`0.46`，`s3` 成为 `term 2` 的 candidate（空日志）。`0.47`，`s2` 拒绝，`reason=log_not_up_to_date`。`0.68`，`s2` 成为 `term 3` 的 candidate，`last_log_index=1`、`last_log_term=1`。`0.69`，`s3` 给票。`0.70`，`s2` 成为 `term 3` 的 leader。结束后 `s2` 标题仍是 `Weekly digest (revised)`，`s3` 标题仍为空；本课心跳不带日志，也不做追赶。

**5. `two_node`。** `n=2`，`majority=2`。`0.17`，`s1` 成为 `term 1` 的 leader，`votes=['s1','s2']`。暂停 `s1` 后，活着的 `s2` 在 `0.50` / `0.72` / `0.94` / `1.16` 四次成为 candidate，任期 `2` 到 `5`，每次只有自己那一票；`new_leaders_after_pause=[]`，`live_leader_after_pause=[]`。`cannot_tolerate_single_failure=true`。`1.20` 结束时 `s1` 仍是被暂停的 `term 1` leader，`s2` 是 `term 5` 的 candidate，`voted_for=s2`。

**6. 误区 `dual_leader_observation`。** `0.30` 隔离仍在工作的 `s1`（不是暂停）。`0.50`，`s2` 成为 `term 2` 的 candidate，`0.52` 成为 leader（`votes=['s2','s3']`）。`0.70` 观察：`s1` 仍是 `term 1` 的 leader 且 `paused=false`，`s2` 是 `term 2` 的 leader，`two_leaders_observed=true`，`terms_during=[1,2]`。期间丢掉 `20` 条 AppendEntries（心跳），最早一条在 `0.32` 由 `s1` 发给 `s2`、`term=1`，说明旧 leader 还在发心跳，但过不了分区。`0.70` 恢复网络后，`0.73`，`s1` 退回 follower，term 变成 `2`，`leaders_after_heal=['s2']`。

**7. 权衡 `timeout_tradeoff`。** 暂停发生在 `0.30`。follower 超时 `0.15` 时，新 leader 在 `0.45` 产生，`detection_s=0.15`；超时 `0.30` 时，新 leader 在 `0.60` 产生，`detection_s=0.30`；`faster_detection=true`。对照「leader 仍活着」：`rpc_delay_s=0.08`、`heartbeat_interval_s=0.10` 时，超时 `0.12` 会在 `0.24` 就出现第一次误选举（`s2` 成为 `term 2` 的 candidate），共 `5` 次 `spurious_candidates`，`spurious_election=true`；同一延迟下超时 `0.30` 则 `spurious_candidates=[]`，`spurious_election=false`。短超时更快发现故障，也更容易在 leader 仍活着、只是心跳偏慢时误启动选举。

**8. 核对验收字段。** `acceptance_at_most_one_majority_leader_per_term=true`，`acceptance_two_node_cannot_tolerate_single_failure=true`，`pitfall_heartbeat_does_not_prevent_dual_leader_observation=true`，`tradeoff_short_timeout_faster_but_easier_to_mis_elect=true`。四个字段都能在 `results.json` 顶层找到，实验脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Raft — 选主与安全性中与 Raft election 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 的实际打开情况如下。

- 节点给出的 [Raft 论文 PDF](https://raft.github.io/raft.pdf) 返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`。
- 题名 In Search of an Understandable Consensus Algorithm (Extended Version)；作者 Diego Ongaro 与 John Ousterhout，Stanford University；文内标注 This tech report is an extended version of [32]，Published May 20, 2014；共 18 页。这份 PDF 没有单独给出 DOI，本课只使用节点给出的这一份材料。
- 节点给出的 [Raft — Visualization and resources](https://raft.github.io/) 返回 200，无跳转，37512 字节，`Content-Type` 为 `text/html; charset=utf-8`；页面 `<title>` 为 Raft Consensus Algorithm。页内有一段 Raft Visualization，称可在浏览器里与 Raft 集群交互，并点名可视化实现 RaftScope（文内写 still pretty rough around the edges），另提到 The Secret Lives of Data 是另一种更引导、少交互的可视化。本课不描述页面上的按钮或操作步骤，只把它当作观察投票与日志冲突场景的辅助材料。

论文里的定义与结论，本课按以下方式使用。

第 5.1 节：典型集群是五台，可容忍两台故障。任意时刻每台服务器处于 leader、follower 或 candidate 三态之一。正常运行时恰好一个 leader，其余为 follower。Raft 把时间划分为任意长度的 term，term 用连续整数编号。每个 term 以一次选举开始；若候选者获胜，则在该 term 余下时间担任 leader。若出现 split vote，该 term 将在没有 leader 的情况下结束，随后开始新的 term 和新的选举。Raft ensures that there is at most one leader in a given term。Different servers may observe the transitions between terms at different times。Terms act as a logical clock。若候选者或 leader 发现自己的 term 过期，立即退回 follower；若收到带陈旧 term 的请求，则拒绝该请求。

第 5.2 节：用心跳触发选举。服务器启动时是 follower。只要收到来自 leader 或 candidate 的合法 RPC，就保持 follower。Leader 周期性发送心跳（不含日志条目的 AppendEntries）以维持权威。若 follower 在一段称为 election timeout 的时间内没有通信，则假定没有可用 leader 并开始选举：递增 `currentTerm`，转为 candidate，给自己投票，并行向其他服务器发 RequestVote。候选者持续到三种结果之一：(a) 获胜，(b) 另一台服务器确立为 leader，(c) 一段时间后仍无胜者。候选者若获得整个集群多数派在同一 term 的投票则获胜。Each server will vote for at most one candidate in a given term, on a first-come-first-served basis（§5.4 另加一条投票限制）。多数派规则保证某一 term 最多一个候选者获胜（Election Safety）。获胜后发送心跳以确立权威并阻止新的选举。若候选者收到声称是 leader 的 AppendEntries：若其 term 至少与自己的 `currentTerm` 一样大，则承认该 leader 并退回 follower；若其 term 更小，则拒绝并继续做 candidate。若许多 follower 同时成为 candidate，票可能被分掉，无人获得多数派；此时每个 candidate 超时后递增 term 再发起 RequestVote。若无额外措施，split vote 可能无限重复。Raft 使用随机选举超时，使 split vote 少见并很快被化解；超时从固定区间随机选取（例如 150–300ms），这样多数情况下只有一台先超时，它赢得选举并在其他服务器超时前发出心跳。同一机制也用于处理已经发生的 split vote：每个 candidate 在选举开始时重新随机化超时。

Figure 2 的 Persistent state：`currentTerm`（本服务器见过的最新任期，首次启动为 `0`，单调递增）、`votedFor`（当前任期把票投给了哪个 `candidateId`，若无则为 `null`）、`log[]`。括号说明：Updated on stable storage before responding to RPCs。RequestVote 接收者规则：若 `term < currentTerm` 则回复 `false`；若 `votedFor` 为 `null` 或就是该 `candidateId`，且候选者日志至少与接收者一样新（at least as up-to-date），则给票（§5.2、§5.4）。All Servers：若 RPC 的 term `T > currentTerm`，则 set `currentTerm = T`，转为 follower。Followers：若选举超时内既没有收到当前 leader 的 AppendEntries，也没有把票投给某个 candidate，则转为 candidate。Candidates：转为 candidate 时开始选举：递增 `currentTerm`、给自己投票、重置选举计时器、向其他服务器发 RequestVote；得到多数派投票则成为 leader；若收到新 leader 的 AppendEntries 则转为 follower；若选举超时则开始新一轮选举。

Figure 3 的 Election Safety：at most one leader can be elected in a given term（§5.2）。

第 5.4.1 节：Raft 保证以往 term 中已提交的条目，在每个新 leader 当选的那一刻就已在它的日志里。A candidate must contact a majority of the cluster in order to be elected。RequestVote 携带候选者日志信息，若投票者自己的日志更新，则拒绝投票。比较两条日志新旧：比较最后一条的 index 与 term；若最后一条的 term 不同，则 term 更大的更新；若最后一条 term 相同，则更长的那份更新。

第 5.6 节：safety must not depend on timing；availability 不可避免地依赖 timing。选主是 Raft 里 timing 最关键的部分。要选出并维持稳定 leader，须满足 `broadcastTime ≪ electionTimeout ≪ MTBF`。broadcast time 应比 election timeout 低一个数量级，以便 leader 可靠发送心跳、阻止 follower 开始选举；结合随机超时，该不等式也让 split vote 不太可能。election timeout 应比 MTBF 低几个数量级。leader 崩溃后，系统大约在一个 election timeout 内不可用。Raft 的 RPC 通常要求接收方把信息持久化到稳定存储，broadcast time 可能在 `0.5ms` 到 `20ms`，因此 election timeout 很可能在 `10ms` 到 `500ms`。这些毫秒数是论文对真实系统的估计，不要把它们和本课虚拟时钟上的 `0.15`、`0.30` 写成同一回事。

教学引擎与论文的差异必须逐条记住。`RaftElectionEngine` 是 Python 3 标准库写的单进程邮箱仿真，不是 Redis / redis-py / 网络服务 / 完整 Raft / Paxos / Jepsen；没有 TCP，没有真实丢包。它不是完整 Raft：没有快照传输（第 7 节）、没有成员变更（第 6 节）；心跳里的 AppendEntries 不携带日志条目，本课也不做 `nextIndex` 回退追赶，所以 `log_freshness` 里新 leader `s2` 当选后，落后的 `s3` 标题仍是空，这不是论文里的完整复制，日志一致性是下一课 `h-raft-log` 的内容。`pause()` 是全知冻结（类似暂停进程），`isolate()` 是全知分区；真实异步网络没有这种信号。持久化是教学 dict，在 RPC 回复前 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。实验里每台服务器使用固定的 `election_timeout_s`，每次重置用同一长度，用来让时间线可重复；论文则是每次从 150–300ms 这类区间重新随机抽样。提交判断会查看 follower 日志里是否已有该条目（教学上的全知 `matchIndex`），不是只靠 AppendEntries 回复里的 `matchIndex` 字段。拒绝投票（包括 `log_not_up_to_date`）不会重置选举计时器；只有收到当前 leader 的 AppendEntries，或把票投出去，才重置。这是按 Figure 2 的 Followers 规则实现的，用来避免落后候选者靠不断抬高 term 打断别人的计时器。`results.json` 里的时间只用于理解定义，不能当成生产基准。

## 自测题

**题 1.** 为什么同一个 term 里不可能有两个都拿到多数派的 leader？结合 `split_vote` 与 `pause_leader` 的数字说明。

<details>
<summary>答案</summary>

假设同一 term 有两个候选者都拿到多数派，那么两个多数派集合必然有交集；交集里的服务器在同一 term 最多投一票，不可能同时把票投给两个候选者，所以两个多数派不能同时成立。论文在 Figure 3 把它写成 Election Safety：at most one leader can be elected in a given term。`pause_leader` 里 `term 1` 只有 `s1` 当选，`0.17` 的 `votes=['s1','s2']`、`vote_count=2`、`majority=2`；`0.52` 的 `term 2` 只有 `s2` 当选，`votes=['s2','s3']`，`leaders_by_term` 为 `term 1 → s1`、`term 2 → s2`。`split_vote` 里 `0.50` 两台同时成为 `term 2` 的 candidate，`0.51` 互相以 `already_voted` 拒绝对方，`term 2` 没有 leader（`leaders_in_term_2=[]`）；`0.69` 才在 `term 3` 由 `s2` 当选。`at_most_one_leader_per_term=true` 的意思是每个 term 至多一个 leader，不是每个 term 一定有 leader。

</details>

**题 2.** 两节点集群为什么难以容忍单点故障？请用多数派和 `two_node` 的实验数字说明。

<details>
<summary>答案</summary>

两节点的多数派是 `2`。暂停 `s1` 后，活着的那一台 `s2` 只有自己的一票，计数为 `1`，形不成多数派 `2`，所以选不出新 leader。`two_node` 里 `0.17`，`s1` 成为 `term 1` 的 leader，`votes=['s1','s2']`；暂停 `s1` 后，`s2` 在 `0.50`、`0.72`、`0.94`、`1.16` 四次成为 candidate，任期从 `2` 到 `5`，每次只有自己那一票；`new_leaders_after_pause=[]`，`live_leader_after_pause=[]`，`cannot_tolerate_single_failure=true`。`1.20` 结束时 `s1` 仍是被暂停的 `term 1` leader，`s2` 是 `term 5` 的 candidate，`voted_for=s2`。这个结论不是论文原句，而是从「必须获得整个集群多数派的投票」推出来的；论文举的典型例子是五台集群，可容忍两台故障。

</details>

**题 3.** 把 `s1` 网络隔离而不是暂停，`0.70` 时为什么能看到两个 leader？心跳和持久化投票各自没有阻止什么？

<details>
<summary>答案</summary>

`0.30` 隔离的是仍在工作的 `s1`，不是暂停它。`s1` 仍在分区一侧发送心跳，`0.50` 时 `s2` 超时成为 `term 2` 的 candidate，`0.52` 成为 leader（`votes=['s2','s3']`）。`0.70` 观察：`s1` 仍是 `term 1` 的 leader 且 `paused=false`，`s2` 是 `term 2` 的 leader，`two_leaders_observed=true`，`terms_during=[1,2]`。期间丢掉 `20` 条 AppendEntries（心跳），最早一条在 `0.32` 由 `s1` 发给 `s2`、`term=1`，说明旧 leader 还在发心跳，但过不了分区。心跳只是在连通的多数派里维持权威，它没有阻止「同时看到两个 leader」；两个 leader 属于不同 term，Election Safety 仍然成立。持久化投票解决的是同一 term 不投两次：`persistent_vote` 里 `s2` 在 `0.16` 把票给 `s1`，重启后在 `0.18` 拒绝 `s3` 的同一 term 拉票（`reason=already_voted`），但它不能让 `term 1` 的旧 leader 立刻退位。`0.70` 恢复网络后，`0.73`，`s1` 看到更高的 term，退回 follower，`term` 变成 `2`，`leaders_after_heal=['s2']`。

</details>

## 推荐资源

- [Raft — 选主与安全性](https://raft.github.io/raft.pdf)（Ongaro & Ousterhout，英文文档，深入，免费）：用于核对 term 与持久化投票、多数派与日志新旧规则，将结论落实到本节点实验。2026-11-13 该地址返回 200，无跳转，567533 字节，`Content-Type` 为 `application/pdf`；题名 In Search of an Understandable Consensus Algorithm (Extended Version)，作者 Diego Ongaro 与 John Ousterhout，Stanford University，文内标注 This tech report is an extended version of [32]，Published May 20, 2014，共 18 页。
- [Raft — Visualization and resources](https://raft.github.io/)（英文文档，进阶，免费）：用公开可视化演算投票与日志冲突场景。2026-11-13 该地址返回 200，无跳转，37512 字节，`Content-Type` 为 `text/html; charset=utf-8`；页面 `<title>` 为 Raft Consensus Algorithm，页内有一段 Raft Visualization，称可在浏览器里与 Raft 集群交互，并点名可视化实现 RaftScope（文内写 still pretty rough around the edges），另提到 The Secret Lives of Data 是另一种更引导、少交互的可视化。

## 本课小结

- 任期是逻辑时钟。`pause_leader` 里 `0.15` 时 `s1` 超时成为 `term 1` 的 candidate，`0.16` 拿到 `s2` 与 `s3` 的票，`0.17` 以 `votes=['s1','s2']`、`vote_count=2`、`majority=2` 当选；`0.30` 暂停 `s1` 后，`0.50` `s2` 成为 `term 2` 的 candidate，`0.52` 以 `votes=['s2','s3']` 当选。`persistent_vote` 里持久化开启时 `s2` 在 `0.18` 拒绝 `s3` 的同一 term 拉票（`reason=already_voted`），不持久化时又把票给了 `s3`。
- 多数派规则与日志新旧规则共同限制谁能当选。三节点多数派为 `2`，两节点多数派也是 `2`；`two_node` 里 `s2` 在 `0.50`、`0.72`、`0.94`、`1.16` 四次参选都只有自己一票，`new_leaders_after_pause=[]`。`log_freshness` 里 `0.46` 的 `s3` 空日志参选，`0.47` 被 `s2` 以 `log_not_up_to_date` 拒绝；`0.68` 的 `s2` 带 `last_log_index=1`、`last_log_term=1` 参选，`0.70` 当选 `term 3` 的 leader。一个 term 至多一个 leader，`at_most_one_leader_per_term=true`。
- 心跳维持权威，但不阻止所有双主观察。`dual_leader_observation` 里 `0.30` 隔离 `s1`，`0.52` `s2` 当选 `term 2` 的 leader；`0.70` 同时看到 `s1`（`term 1`）与 `s2`（`term 2`），期间丢掉 `20` 条心跳，最早一条在 `0.32`；`0.73` 恢复后 `s1` 退回 follower。短超时更快发现故障，也更容易误选举：超时 `0.15` 在 `0.45` 选出新 leader，超时 `0.30` 在 `0.60` 选出；但 leader 仍活着时，超时 `0.12` 在 `0.24` 就出现第一次误选举，共 `5` 次 `spurious_candidates`。
- 这些时间都是教学虚拟时钟上的数字，不是生产 RTT 或线上超时配置。教学引擎 `RaftElectionEngine` 不是完整 Raft；它没有快照传输、没有成员变更、没有日志条目心跳与 `nextIndex` 追赶，日志一致性留给下一课 `h-raft-log`。

如果有一句答不上来，先回到对应的实验字段，再回到论文第 5.1 节关于 term 与三态的几句、第 5.2 节关于心跳与随机超时的几句、Figure 2 的持久化状态与 RequestVote 规则，以及第 5.4.1 节的日志新旧比较，不要只把「心跳在就没有双主」或「两节点也能切换」背下来。
