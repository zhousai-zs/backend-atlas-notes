# 第 109/208 课：存储复制、确认与故障切换

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-replication` |
| 英文名 | Storage Replication |
| 领域 / 阶段 | G / 第 4 阶段 |
| 子类 | 数据生命周期 |
| 难度 | 深入 |
| 前置节点 | `g-persistence`、`g-wal` |
| 标签 | `Storage Replication`、`G`、`Systems` |
| 参考资料 | [存储复制、确认与故障切换：参考文档 / 原始资料](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)（redis.io，文档；2026-11-12 该 URL 返回 200，无跳转。同期打开的 [WAIT](https://redis.io/docs/latest/commands/wait/)、[WAITAOF](https://redis.io/docs/latest/commands/waitaof/)、[Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/) 同样返回 200。旧地址 `https://redis.io/topics/replication` 返回 200，但最终跳到 `https://redis.io/docs/latest/develop/` 文档首页，不是复制页；`https://redis.io/docs/latest/topics/sentinel/` 返回 404。这两处本课不引用） |
| 摘要 | 复制把更新传播到其他节点，客户端写确认、传播顺序和切换规则决定故障时的数据与一致性边界。 |

> **边界说明**：本课中的「内容后台」「文章标题」「未发布稿」都是**教学假设**，用来让复制延迟、写确认和故障切换变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `ReplicationEngine`（`Node` / `Clock`），**不是 Redis，不是 redis-py，也不是网络服务**。它没有 TCP 协议、PSYNC/SYNC 握手、RDB/AOF 文件、diskless 传输、Sentinel 进程或 Redis Cluster 槽位。教学复制 ID 是短字符串；教学 offset 按命令计数，不是 Redis 复制流的字节偏移。故障切换是教学函数，按已应用 offset 挑选副本，或按测试指定副本提升，不是 Sentinel 的法定人数选举，也不是 Cluster 故障切换。官方文档已于 **2026-11-12** 用 curl 核对（见下文）。文中的官方机制只写到核对过的页面和源码能够支撑的范围；教学实验里的耗时、丢失条数和标题都来自 `/workspace/atlas/lessons/practice109/results.json`，不是生产 Redis 的基准数字。

## 导读

### 一个直觉：复制不是「多一份」，而是「多一份可能落后、也可能被提升的数据」

内容后台发布一篇修订标题时，写请求先在主库上成功，客户端收到 `ok`；随后主库把这次更新发给副本。问题从这里开始分叉：客户端看到的 `ok` 到底承诺了什么？是主库内存里有了，是主库磁盘里有了，还是至少一个副本也收到了？如果下一秒主库宕机，副本里有没有这条修订？如果副本落后，读请求落到它身上会看到什么？如果故障切换选中了一个落后的副本，已经确认的写会不会消失？

节点把这条机制概括为：复制把更新传播到其他节点，客户端写确认、传播顺序和切换规则决定故障时的数据与一致性边界。这里最容易犯的错，是把「有 3 个副本」直接理解成「3 份一致的数据」或者「多数派已经确认」。副本数量只说明有多少份拷贝；写确认策略才说明客户端返回时数据到了哪里；故障切换规则才说明哪一份拷贝会被提升。三件事必须分开看。

### 接上前置

第 103 课的 `g-persistence` 讲的是单节点持久化：数据在进程退出或机器重启后还在不在。第 105 课的 `g-wal` 讲的是单节点上的顺序与重放：一次写入先进入日志，按顺序应用，崩溃后可以恢复。复制把这两个问题扩展到多节点：主库本地持久化成功，不等于副本已经收到；副本收到了，不等于副本已经把它写进自己的持久存储；客户端收到 `ok`，也不等于故障切换后这条写一定还在。

所以本课要补的不是「再讲一遍 WAL」，而是三个新边界：**传播是异步的，确认是有范围的，切换是有选择规则的**。把这三件事接起来，才能回答「故障时到底会丢什么」。

### 教学场景（教学假设）

教学数据只有几个键：`article:01:title`，旧值 `Weekly digest`，新值 `Weekly digest (revised)`；另有一个未发布稿的值 `unpublished draft`，以及另一篇文章的标题 `article:02:title = Product update`。默认拓扑是 1 个教学主库加 3 个副本 `r1` / `r2` / `r3`，除非某组实验另说。教学 offset 按命令计数：seed 写入旧标题占用 offset 1，随后「发布修订标题」这次写就是 offset 2。这个 offset 不是 Redis 复制流的字节偏移，教学复制 ID 也不是 Redis 的 40 位十六进制 ID。

这些标题和未发布稿都是教学假设。真实内容后台还会有审核、版本、缓存和分片；本课只保留能观察「主库已写、副本未到、客户端已确认、切换后恢复出什么」的最小字段。

### 本课三条主线

1. **同步与异步复制**：主库什么时候把写发给副本，客户端什么时候收到确认，`WAIT` 和 `min-replicas-to-write` 各自改变了哪一段。
2. **复制延迟和读陈旧**：副本的 applied offset 落后主库时，读副本会看到旧值；延迟按 offset 和按时间分别怎么观察。
3. **故障切换与已确认写丢失**：客户端已经收到 `ok` 的写，在主库故障、副本被提升之后还在不在；切换规则如何决定丢多少。

## 学习目标

- 能解释同步与异步复制：默认异步复制下客户端确认发生在哪里，`WAIT` 如何把某一次写的确认扩展到副本，以及为什么 `WAIT` 仍然不是强一致承诺。
- 能独立完成：在教学主从上注入延迟和宕机，记录确认写与恢复数据的差异；能说明每组实验里「允许丢失」的写是哪一条，而不是用「开了三个副本就不会丢」代替结论；能区分副本数与参与共识节点数，知道 3 个副本只是 leader-follower 的拷贝数，默认写确认只在主库完成，Sentinel 的 quorum 和多数派是 Sentinel 进程之间的判断，本教学引擎没有共识组。

## 为什么后端要懂

内容后台的发布接口通常这样写：编辑点「发布」，服务端把 `article:01:title` 改成新标题，返回成功，页面提示已发布。如果这次写只在主库上完成，而副本还在传旧标题，那么主库故障后按副本提升，读者可能重新看到旧标题。更隐蔽的情况是：客户端已经收到成功，运营也以为发布完成，但故障切换后数据回退，事后只能从日志里补。复制课要解决的就是这种「成功到底意味着什么」的问题。

另一个常见误判是把副本数当成一致性保证。3 个副本都在线，不代表这次写已经到达 3 个副本；默认异步复制下，主库返回成功时可能一个副本都还没应用。即使打开了 `min-replicas-to-write`，它检查的也只是「最近有 N 个副本 ping 过」，并不等待这一次写到达副本，官方明确称它是 best-effort 的数据安全机制，仍有丢失窗口。再往上，Sentinel 的 quorum 是 Sentinel 进程对「主库不可达」的判断，真正切换还需要多数派授权；它和「3 个 Redis 副本」不是同一层的东西。

读路径也会受影响。编辑在后台刷新列表，如果读请求被路由到落后副本，可能看到旧标题；读者刚看到发布成功，再刷新又回到旧值。这不是缓存问题，而是复制延迟和读陈旧。理解 offset、ack 和 lag，才能在「读主库」「读副本」「等副本确认」之间做出有依据的选择。

## 核心知识点

### 1. 同步与异步复制

Redis 默认使用异步复制。连接正常时，主库把写命令、过期和淘汰的效果以命令流发给副本；副本定期向主库异步确认自己已经处理到的数据量。主库平时不等待副本，但它知道各副本处理到哪里，因此可以支持可选的同步复制。客户端可以用 `WAIT` 请求某些写入的同步复制：`WAIT numreplicas timeout` 会等待至少 `numreplicas` 个副本确认到这次写产生的复制 offset，`timeout` 以毫秒计，`0` 表示一直等。返回值是实际确认的副本数，客户端必须检查它是否大于等于要求的数量；超时和成功都会返回这个数字。

这里要把「异步」和「没有传播」分开。异步复制不等于主库不发送，而是客户端不等待副本确认。教学引擎里，`set(..., wait_replicas=0)` 会在主库上写入、递增 offset，并把命令放进副本的待应用队列；如果没有暂停或附加延迟，教学实现会在返回前顺手应用队列，但返回的 `waited` 仍是 0，因为客户端没有等待副本确认。实验 `async_lag_stale_read` 里，异步 SET 返回 `ok`、`waited=0`，主库 offset 已经是 2；这并不表示副本一定收到了 offset 2，暂停的 `r1` 就仍停在 offset 1。

同步复制的边界由确认策略决定，而不是由副本数量决定。`WAIT` 只保证「在其他 Redis 实例中有指定数量的已确认副本」，并不会把一组 Redis 变成具有强一致性的 CP 系统。官方文档明确写到：已确认的写在故障切换时仍可能丢失，取决于持久化配置；`WAIT` 只是把丢失概率降到某些难以触发的故障模式。`WAITAOF` 从 Redis 7.2 起可用，等待本地和/或副本把 AOF fsync 完成，官方同样写明它不会让 Redis 变成强一致存储；除非等待集群所有成员把写 fsync 到磁盘，故障切换或重启时仍可能丢数据。本课实验没有实现 `WAITAOF`，只在文档阅读里提及。

复制本身是 leader-follower（主从）结构。高可用是 Redis Sentinel 或 Redis Cluster 提供的额外层，不是复制自带的。复制有三条路径：连接正常时主库发送命令流；断线后尝试部分重同步；部分重同步不行再做全量同步，主库先做快照，再继续发流。主库侧的复制是非阻塞的；副本在初次同步时可以使用旧数据集回答查询（如果配置允许），也可以配置成复制流中断时对客户端返回错误；初次同步之后，删除旧数据集并加载新数据集的窗口会阻塞。副本默认只读（`replica-read-only`，自 Redis 2.6），这也是为什么「读副本」通常需要显式路由。

复制 ID 加 offset 用来标识一份精确的数据集版本。提升后的副本会使用新的复制 ID，因为旧主库可能仍然可用，不能继续用旧 ID 混淆两段历史。过期由主库合成 `DEL` 发给副本，副本不用自己的时钟决定过期；`INFO replication` 和 `ROLE` 可以查看复制状态和 offset。这些官方机制在本课里只作为阅读边界，教学引擎用短字符串 `replid` 和按命令计数的 `offset` 做近似，不能把教学字段当成 Redis 的字节偏移。

同步复制改善的是持久性边界，但代价是延迟。实验 `sync_latency` 用 20 次写比较了几种确认策略：`async_fast` 不等待副本，总耗时 `9.975099237635732e-05` 秒，约 `200499.2584388598` ops/s，平均每次写 `4.987549618817866e-06` 秒；`wait1_fast` 等待 1 个副本，三副本都能立刻确认，`min_waited=3`、`max_waited=3`，总耗时 `0.00011532899225130677` 秒，约 `173416.93194040185` ops/s，平均 `5.766449612565339e-06` 秒。这些数字来自教学引擎，不是生产 Redis 的基准；它们说明的是「即使副本很快，WAIT 也多了确认往返和检查」。

当副本变慢，同步复制的代价会直接体现出来。`wait1_all_slow_50ms` 让三个副本各延迟 0.05 秒，`WAIT 1` 的总耗时是 `1.0131437960080802` 秒，约 `19.740534442201223` ops/s，平均每次 `0.05065718980040401` 秒，`min_waited=3`、`max_waited=3`。`wait1_one_fast_two_200ms` 让 `r1` 不延迟、`r2` 和 `r3` 延迟 0.20 秒，`WAIT 1` 跟最快的副本走，总耗时只有 `0.00011922299745492637` 秒，约 `167752.87005815492` ops/s，平均 `5.961149872746318e-06` 秒，`min_waited=1`、`max_waited=1`。`wait3_one_slow_50ms` 让 `r3` 延迟 0.05 秒，`WAIT 3` 被最慢的副本拖住，总耗时 `1.0109846529958304` 秒，约 `19.78269397140145` ops/s，平均 `0.05054923264979152` 秒，`min_waited=3`、`max_waited=3`。同样的慢副本，`WAIT 1` 可以绕开，`WAIT 3` 绕不开；要求越多副本确认，越受最慢副本影响。

`WAIT` 的返回值也必须单独看。实验 `wait_timeout` 里，只有 `r1` 能确认，`WAIT 2` 的超时设为 100 毫秒，结果是 `waited=1`、`demanded=2`，耗时 `0.100113414999214` 秒，而主库已经是新标题。客户端如果只看「命令返回了」而不检查返回值，就会把只到达 1 个副本的写当成到达 2 个副本。`WAIT` 在副本实例上不能使用，这也是它属于主库确认语义的一个旁证。

### 2. 复制延迟和读陈旧

复制延迟可以按 offset 看，也可以按时间看。按 offset 看，主库当前 offset 是 2，副本 applied 是 1，`lag_offset=1`，说明副本还差一条命令。按时间看，副本最后一次 ack 距今多久，就是 `lag_s`；官方 `min-replicas-max-lag` 用的也是「延迟不超过 M 秒的已连接副本」这个口径。教学引擎的 `Node` 同时记录 `applied`、`last_ack_offset` 和 `last_ack_at`，`advance` 会推进冻结时钟，让暂停或断链副本的 lag 增长；`info()` 会给出每个副本的 `lag_offset` 和 `lag_s`。

实验 `async_lag_stale_read` 把这条机制拆得很清楚。先 seed 旧标题 `Weekly digest`，这次写入占用 offset 1；暂停 `r1`；再异步写新标题 `Weekly digest (revised)`，返回 `ok`、`waited=0`、`offset=2`。此时主库是修订版、offset 2，`r2` 和 `r3` 也是修订版、offset 2；`r1` 仍是 `Weekly digest`、applied offset 1，`lag_offset=1`。`stale_before_flush=true`，说明读 `r1` 确实读到了旧值。随后对 `r1` 执行教学 `flush`，这不是 Redis 命令，而是测试辅助函数，用来按需把暂停副本的待应用队列送进去；之后三个副本都变成修订版、offset 2，`caught_up_after_flush=true`。

这组数字回答了两个问题。第一，异步写返回成功时，主库和部分副本可能已经是新值，但落后副本仍可能返回旧值；读副本不是「读一份稍微旧一点但最终一样」这么简单，客户端在延迟窗口内看到的就是旧值。第二，副本不是永远落后；只要复制流恢复或教学 `flush` 把队列应用掉，它就会追上。关键是应用之前的那段时间里，读请求有没有被路由到它身上。

官方文档还给了两个相关的读边界。副本在初次同步时可以使用旧数据集回答查询（如果配置如此），也可以配置成复制流中断时对客户端返回错误；页面写了这两种行为，但没有写出配置项的名字，本课不擅自补 `redis.conf` 键名。教学实验 `link_down_read` 复现了这两种结果：`r1` 在未跟上时断链，默认仍返回旧标题 `Weekly digest`、offset 1；把 `serve_on_link_down` 设为 `false` 后，同一个读返回 `status=link_down`，不再给出旧值。这里要记住的是语义边界：**断开复制流的副本既可以继续用当前数据集回答，也可以拒绝回答；选择哪一种取决于配置，而不是「副本一定安全」。**

复制延迟还会影响 `min-replicas-to-write` 的判定。实验 `replica_count_is_not_consensus` 里，打开 `min-replicas-to-write=2`、`min-replicas-max-lag=2.0` 秒后，三个副本新鲜时写成功；暂停 `r2` 和 `r3`，把教学时钟推进 2.1 秒，只有 `r1` 一个新鲜副本，于是写 `unpublished draft` 被拒绝，`status=noreplicas`，标题仍是 `Weekly digest (revised)`，`fresh_at_reject=1`。这里「新鲜」指的是最近有 ping 且 lag 不超过 2.0 秒，不是「已经收到这次写」。恢复 `r2` 并让 `r2` 重新 ping 后，新鲜副本变成 2 个，写成功，offset 变成 3，标题变成 `unpublished draft`。被拒绝的那次写 offset 是 0，因为它根本没有被主库接受，不能算作一条已经发生的写。

读陈旧和写拒绝是同一件事的两面：副本落后时，读可能旧；主库发现新鲜副本不够时，写可能被拒绝。两者都不能靠「副本数量」自动解决。

### 3. 故障切换与已确认写丢失

客户端收到 `ok` 的写，在故障切换后不一定还在。实验 `async_ack_lost_on_failover` 把三个副本全部暂停，然后异步写新标题，返回 `ok`、`waited=0`、`offset=2`，`client_saw_ok=true`；主库已经是修订版，但三个副本都还是 `Weekly digest`、offset 1。接着宕掉主库，按最高 applied offset 提升副本。三个副本并列在 offset 1，教学函数选中了 `r3`；`new_master_offset=1`，恢复值是 `Weekly digest`，`acknowledged_write_lost=true`，`lost_writes=1`，`allowed_loss=true`。这条丢失的写就是客户端已经确认的 offset 2：它只在主库上，副本没收到，主库故障后没有来源可以恢复。

`WAIT` 可以改善这个边界，但不能消除它。实验 `wait_promote_acking_replica` 暂停 `r2` 和 `r3`，只让 `r1` 能确认；`SET` 加 `WAIT 1`，`waited=1`，`r1` 已有修订版、offset 2。宕掉主库后按最高 offset 提升，`r1` 成为新主库，`new_master_offset=2`，恢复值是 `Weekly digest (revised)`，`lost_writes=0`，`acknowledged_write_lost=false`；`r2` 和 `r3` 随后被全量同步成修订版。这个结果说明：如果切换选中了已经确认这次写的副本，已确认的写可以保住。

但切换不一定选中它。实验 `wait_promote_lagging_replica` 用同样的 `WAIT 1` 让 `r1` 确认，却强制提升落后的 `r2`。切换前 `r1` 已有新标题，`r2` 还是旧标题；提升 `r2` 后，`new_master_offset=1`，恢复值是 `Weekly digest`；`r1` 被新主库全量同步，也被改回 `Weekly digest`。`lost_writes=1`，`allowed_loss=true`。这正对应官方 `WAIT` 页的说法：在 Sentinel 或 Cluster 故障切换场景中，`WAIT` 改善现实世界的数据安全，但只是「更可能（但不保证）」提升到已收到该写的副本，仍是 best-effort；仍然可能丢失已经同步复制到多个副本的写。

这里的「故障切换」是教学函数，不是 Sentinel。教学 `failover(promote=None)` 会在存活副本中按 `(applied, -priority, name)` 选一个，优先已应用 offset 更大的；也可以由测试指定副本。它不投票、不判断 ODOWN、不需要多数派授权，也不处理网络分区。官方 Sentinel 的 quorum 是「就主库不可达达成一致的 Sentinel 进程数」，用于判定 ODOWN；真正执行故障切换还需要 Sentinel 的多数派授权，少数派分区里不会切换。副本选择顺序是：先 `replica-priority`（越低越优先，0 表示永不提升），相同则选复制 offset 更大、收到更多数据的，再相同则选 run ID 字典序更小的。**「3 个 Redis 副本」和「3 个 Sentinel 的多数派/quorum」不是同一回事。**

分区还会制造另一类丢失。官方 Sentinel 页写到：分区下旧主库若仍接受写，分区愈合后这些写会丢失；可以用 `min-replicas-to-write` 限制窗口。教学实验没有模拟完整网络分区，但 `failover` 会把存活旧主库隔离，保留它作为「仍可能接受写、愈合后可能被覆盖」的边界；本课不声称验证了 Sentinel 的分区恢复流程。

主库无持久化空重启是另一种更危险的故障模式。实验 `empty_master_restart` 用 2 个副本，先写入 `Weekly digest (revised)` 和 `Product update`，两边各有 2 个标题。主库的 `persist=False`，宕机后以空数据集重启；仍连接的副本被全量同步清空，`master_keys_after=0`、`r1_keys_after=0`、`r2_keys_after=0`，`replicas_wiped=true`，`lost_titles=2`。这不是「副本还在所以安全」，而是空主库把副本也带空了。官方文档把「主库关闭持久化并自动重启」列为危险故障模式：空的主库会让仍连接的副本全量同步，并把副本数据也清空。

最后回到 `min-replicas-to-write`。从 Redis 2.8 起可用 `min-replicas-to-write` 与 `min-replicas-max-lag`：至少 N 个延迟不超过 M 秒的已连接副本时才接受写。因为复制是异步的，它无法确保副本已经收到这一次写，所以始终存在丢失窗口；官方称这是 best-effort 数据安全机制，把无界丢失变成有界丢失，不满足条件时主库以错误拒绝写。它不等于共识。实验里默认 `min-replicas-to-write=0` 时，3 个已连接副本的异步写仍然 `waited=0`、`confirmation=master_only`、`is_consensus=false`，教学引擎的 `consensus_nodes=0`。打开 `min-replicas-to-write=2` 后，它只检查「最近有 N 个副本 ping 过」，并不等待这一次写到达副本。把这个开关理解成「多数派确认」是错的；把它理解成「在主库接受写之前加一道新鲜副本数量的门槛」才准确。

## 常见误区

**「有三个副本，所以三份数据一致。」** 副本数量只说明有三份拷贝。默认异步复制下，写返回成功时副本可能一个都没应用；实验 `async_lag_stale_read` 里 `r1` 就停在 offset 1，主库和另外两个副本在 offset 2。副本之间的一致性取决于复制进度和确认策略，不取决于数量。

**「`WAIT` 成功就等于强一致，切换一定不丢。」** `WAIT` 只保证指定数量的副本确认到了某个 offset，不保证故障切换一定提升到这些副本。实验 `wait_promote_acking_replica` 提升已确认的 `r1` 丢失 0 条，但 `wait_promote_lagging_replica` 强制提升落后的 `r2` 仍丢失 1 条。官方文档明确说 `WAIT` 是 best-effort，仍可能丢失已经同步复制到多个副本的写；`WAITAOF` 也不是强一致承诺。

**「`min-replicas-to-write` 会等这次写到达副本。」** 它检查的是最近有 N 个副本 ping 过且延迟不超过 M 秒，不是这次写的确认。实验里打开 `min-replicas-to-write=2` 后，三个副本新鲜时写成功；暂停两个副本并推进 2.1 秒后，只剩 1 个新鲜副本，写被拒绝。被拒绝的写没有发生；被接受的写也没有等待所有副本应用，只是通过了准入门槛。

**「Sentinel 的 quorum 就是 Redis 副本数。」** quorum 是 Sentinel 进程之间就「主库不可达」达成一致的数量，用于 ODOWN；真正切换还需要多数派授权。3 个 Redis 副本不构成 3 个 Sentinel 的 quorum，也不构成共识组。本教学引擎的 `consensus_nodes=0`，`sentinel_quorum_is_not_replica_count=true`，就是提醒不要把这两层混在一起。

**「主库挂了，副本还有数据，所以空重启也安全。」** 如果主库关闭持久化并自动重启，空主库会让仍连接的副本全量同步，副本数据也会被清空。实验 `empty_master_restart` 里两个副本各有 2 个标题，最后三边都是 0 个键，`lost_titles=2`。副本不是备份，尤其不是在这种危险故障模式下。

**「读副本最多读到稍微旧一点的数据，不会有问题。」** 延迟窗口内读副本就是旧值；实验里 `r1` 返回 `Weekly digest`，而主库已经是 `Weekly digest (revised)`。如果复制流断开，副本还可以配置成直接返回错误；`link_down_read` 里同一个读从返回旧值变成 `status=link_down`。读副本要么接受陈旧，要么显式拒绝，不能假设它总是最新。

**「教学 offset 就是 Redis 的复制流字节偏移。」** 教学 offset 按命令计数，seed 占 offset 1，发布修订占 offset 2；Redis 的复制 offset 是复制流的字节偏移。教学复制 ID 是短字符串，不是 Redis 的复制 ID。这个区别会影响你读 `INFO replication` 和源码时的口径。

## 设计权衡

节点的权衡是：同步复制改善持久性边界，但增加延迟并受慢副本影响。实验 `sync_latency` 把这条权衡量化得很直接。不等待副本时，20 次写总耗时 `9.975099237635732e-05` 秒，约 `200499.2584388598` ops/s，平均每次 `4.987549618817866e-06` 秒。`WAIT 1` 且副本都很快时，总耗时 `0.00011532899225130677` 秒，约 `173416.93194040185` ops/s，平均 `5.766449612565339e-06` 秒；确认数 `min_waited=3`、`max_waited=3`，因为 `WAIT` 返回的是实际确认数，可以大于要求的 1。

慢副本把差距拉开。三个副本各延迟 0.05 秒时，`WAIT 1` 总耗时 `1.0131437960080802` 秒，约 `19.740534442201223` ops/s，平均 `0.05065718980040401` 秒；`WAIT 3` 在一个副本延迟 0.05 秒时总耗时 `1.0109846529958304` 秒，约 `19.78269397140145` ops/s，平均 `0.05054923264979152` 秒。两者都被 0.05 秒的副本拖到约 1 秒，因为 20 次写每次都要等一次确认，`WAIT 3` 还要等最慢的那一个。相反，`WAIT 1` 在「一个快、两个 200 毫秒」的配置下总耗时只有 `0.00011922299745492637` 秒，约 `167752.87005815492` ops/s，平均 `5.961149872746318e-06` 秒，`min_waited=1`、`max_waited=1`：它跟最快的副本走，绕开了慢副本。

这就是确认策略的核心取舍。要求越多副本确认，持久性边界越宽，但写延迟越受慢副本影响，可用性也越差：一个慢副本或一个分区就可能让 `WAIT 3` 长时间等不到。要求越少，写越快，但故障切换时可选的安全副本越少。`min-replicas-to-write` 是另一条曲线：它在准入阶段拒绝写，把无界丢失变成有界丢失，但在副本不足时会牺牲可用性；实验里只剩 1 个新鲜副本时，写直接返回 `noreplicas`，而不是继续接受再等复制。

故障切换的选择规则也是权衡。优先选 offset 更大的副本，能减少已确认写的丢失；但生产环境的 Sentinel 还要先看 `replica-priority`，这可能导致「优先级更高但更落后」的副本被选中，或者优先级为 0 的副本永不提升。教学函数按 `(applied, -priority, name)` 选，是为了让实验可重复，不是 Sentinel 的完整规则。即使选择最靠前的副本，也只能保证它有的数据不丢；它没有的数据仍然没有。

持久化配置会改变结论。主库关闭持久化并自动重启，会把空数据集同步给副本，副本越多，被清空的范围越大。这个权衡不是「副本多就好」，而是「副本、确认、持久化和切换规则必须一起设计」。

## 动手实践：在测试 Redis 主从注入延迟和宕机，记录确认写与恢复数据差异

本课不启动 Redis，也不连接真实主从；下面的「主从」由 `/workspace/atlas/lessons/practice109` 里的教学 `ReplicationEngine` 模拟。代码用 Python 3.13.5 标准库写成，不链接 Redis，不走网络。运行 `./test.sh` 会执行全部实验并写下 `results.json`。本次已跑通，输出 `ALL_CHECKS_PASSED`。下面的数字都来自这份结果，没有改写。

### 0. 文件结构

- `engine.py`：教学 `ReplicationEngine`、`Node`、`Clock`。主库写入、offset 递增、命令入队、副本 ack、`WAIT` 计数、暂停/断链、全量同步、空主库重启、教学故障切换。
- `app.py`：九组实验。延迟与读陈旧、异步确认写丢失、`WAIT` 后提升已确认副本、`WAIT` 后强制提升落后副本、`WAIT` 超时、副本数不是共识、空主库重启、断链读、同步延迟。
- `test_replication.py` / `test.sh`：验收并写出 `results.json`。
- `results.json`：本课所有教学数字的来源。

### 1. 准备数据与目标操作

教学键是 `article:01:title`，旧值 `Weekly digest`，新值 `Weekly digest (revised)`；另有 `unpublished draft` 和 `article:02:title = Product update`。默认 1 个教学主库加 3 个副本。`seed` 先写入旧标题并让所有副本跟上，占用 offset 1；目标操作是发布修订标题，占用 offset 2。实验要观察：异步写返回后各节点的值和 offset；暂停或断链后读副本看到什么；主库宕机后按什么规则提升、恢复出哪个值；`WAIT` 成功或超时后丢失多少条；`min-replicas-to-write` 拒绝写时标题有没有变；空主库重启会不会清空副本。

### 2. 要比较的几条路径

1. 暂停 `r1`，异步写新标题，比较主库、`r2`/`r3` 和 `r1` 的值与 offset；再 `flush` `r1` 看是否追上。
2. 暂停全部副本，异步写返回 `ok`，宕掉主库，按最高 offset 提升，看恢复值和丢失条数。
3. 暂停 `r2`/`r3`，`SET` 加 `WAIT 1`，提升已确认的 `r1`，看是否丢失。
4. 同样的 `WAIT 1`，但强制提升落后的 `r2`，看 `r1` 是否被改回旧值、丢失多少条。
5. 只让 `r1` 能确认，`WAIT 2` 超时 100 毫秒，看返回值和耗时。
6. 默认 `min-replicas-to-write=0` 时三副本的异步写确认；打开 `min-replicas-to-write=2`、`max-lag=2.0` 后，暂停两个副本并推进 2.1 秒，看写是否被拒绝、标题是否不变，再恢复 `r2` 看写是否成功。
7. 主库无持久化，两个副本各有 2 个标题；主库宕机后空数据集重启，看副本是否被清空。
8. `r1` 未跟上就断链，默认读旧值；配置成断链返回错误后，看 `status`。
9. 20 次写下比较 `async_fast`、`wait1_fast`、`wait1_all_slow_50ms`、`wait1_one_fast_two_200ms`、`wait3_one_slow_50ms` 的耗时和确认数。

### 3. 运行并解读（真实输出）

`./test.sh` 输出 `ALL_CHECKS_PASSED`。下面是从 `results.json` 摘出的关键字段，数值保持原样：

| 场景 | 关键结果 |
|---|---|
| `async_lag_stale_read` | 写 `ok`、`waited=0`、`offset=2`；`r1` 值 `Weekly digest`、offset 1、`lag_offset=1`；`stale_before_flush=true`，`caught_up_after_flush=true` |
| `async_ack_lost_on_failover` | 写 `ok`、`waited=0`；提升 `r3`，`new_master_offset=1`；恢复值 `Weekly digest`，`lost_writes=1`，`allowed_loss=true` |
| `wait_promote_acking_replica` | `waited=1`；提升 `r1`，`new_master_offset=2`；恢复值 `Weekly digest (revised)`，`lost_writes=0` |
| `wait_promote_lagging_replica` | `waited=1`；强制提升 `r2`，`new_master_offset=1`；恢复值 `Weekly digest`，`lost_writes=1` |
| `wait_timeout` | `waited=1`、`demanded=2`、`elapsed_s=0.100113414999214`；主库已是新标题 |
| `replica_count_is_not_consensus` | 三副本异步写 `waited=0`、`confirmation=master_only`、`is_consensus=false`、`consensus_nodes=0` |
| `empty_master_restart` | 三边 keys 都是 0，`replicas_wiped=true`，`lost_titles=2` |
| `link_down_read` | 默认读旧值 `Weekly digest`；断链返回错误时 `status=link_down` |

### 4. 逐组对照真实数字

1. **复制延迟和读陈旧。** 暂停 `r1` 后异步写，`set` 返回 `ok`、`waited=0`、`offset=2`，耗时 `5.280991899780929e-06` 秒。主库和 `r2`/`r3` 都是 `Weekly digest (revised)`、offset 2；`r1` 仍是 `Weekly digest`、applied offset 1，`lag_offset=1`。`stale_before_flush=true`，`caught_up_after_flush=true`。seed 占用 offset 1，这次写是 offset 2；`flush` 之后三个副本都到 offset 2。
2. **异步已确认写丢失。** 暂停全部副本，异步 `SET` 返回 `ok`（客户端已收到确认），主库已是新标题，三个副本仍是旧标题、offset 1。宕掉主库后按最高 offset 提升，三个副本并列，教学函数选中 `r3`；`new_master_offset=1`，恢复值是 `Weekly digest`，`acknowledged_write_lost=true`，`lost_writes=1`，`allowed_loss=true`。
3. **`WAIT` 后提升已确认副本。** 暂停 `r2`/`r3`，`SET` 加 `WAIT 1`、超时 1000 毫秒，`waited=1`。提升最高 offset 的 `r1`，`new_master_offset=2`，恢复值 `Weekly digest (revised)`，`lost_writes=0`，`acknowledged_write_lost=false`；`r2` 和 `r3` 被全量同步成修订版。
4. **`WAIT` 后强制提升落后副本。** 同样 `WAIT 1` 成功（`r1` 已有新标题，`r2` 仍是旧标题），但强制提升 `r2`。新主库恢复值 `Weekly digest`，`new_master_offset=1`；`r1` 被全量同步后也改回 `Weekly digest`，`lost_writes=1`，`allowed_loss=true`。这对应官方「best-effort，仍可能丢失已同步复制的写」。
5. **`WAIT` 超时。** 只让 `r1` 能确认，`WAIT 2`、超时 100 毫秒，`waited=1`、`demanded=2`，`client_must_check=true`，耗时 `0.100113414999214` 秒；主库已经是新标题。客户端必须检查返回值，不能把超时返回当成成功。
6. **三副本不是共识。** 默认 `min-replicas-to-write=0` 时，3 个已连接副本的异步写仍然 `waited=0`、`confirmation=master_only`、`is_consensus=false`，本引擎 `consensus_nodes=0`，`sentinel_quorum_is_not_replica_count=true`。打开 `min-replicas-to-write=2`、`max-lag=2.0` 秒后，三副本新鲜时写成功；暂停 `r2`/`r3` 并推进 2.1 秒后只有 1 个新鲜副本，写 `unpublished draft` 被拒绝，`status=noreplicas`，标题仍是 `Weekly digest (revised)`；恢复 `r2` 并让它重新 ping 后写成功，标题变成 `unpublished draft`，offset 变成 3。这个开关只检查「最近有 N 个副本 ping 过」，并不等待这一次写到达副本，官方称仍有丢失窗口。
7. **主库无持久化空重启。** 2 个副本已有 2 个标题（含 `Weekly digest (revised)` 与 `Product update`），主库宕机后空数据集重启，仍连接的副本被全量同步清空，`replicas_wiped=true`，`lost_titles=2`，三边 keys 都是 0。这是官方文档里的危险故障模式。
8. **断链后的读。** `r1` 未跟上就断链，默认仍返回旧标题 `Weekly digest`、offset 1；若配置为链路断开时返回错误，`status=link_down`，不再返回旧值。官方复制页写了「可继续用旧数据集回答」或「复制流中断时返回错误」，但该页没有写出配置项的名字，本课不擅自写出 `redis.conf` 键名。
9. **同步延迟。** 各 20 次写：`async_fast` 的 `elapsed_s=9.975099237635732e-05`、`ops_per_s=200499.2584388598`、`mean_write_s=4.987549618817866e-06`、`min_waited=0`、`max_waited=0`；`wait1_fast` 的 `elapsed_s=0.00011532899225130677`、`ops_per_s=173416.93194040185`、`mean_write_s=5.766449612565339e-06`、`min_waited=3`、`max_waited=3`；`wait1_all_slow_50ms` 的 `elapsed_s=1.0131437960080802`、`ops_per_s=19.740534442201223`、`mean_write_s=0.05065718980040401`、`min_waited=3`、`max_waited=3`；`wait1_one_fast_two_200ms` 的 `elapsed_s=0.00011922299745492637`、`ops_per_s=167752.87005815492`、`mean_write_s=5.961149872746318e-06`、`min_waited=1`、`max_waited=1`；`wait3_one_slow_50ms` 的 `elapsed_s=1.0109846529958304`、`ops_per_s=19.78269397140145`、`mean_write_s=0.05054923264979152`、`min_waited=3`、`max_waited=3`。这些数字支撑「同步复制改善持久性边界，但增加延迟并受慢副本影响」。

### 5. 对照验收标准

```text
# 验收 1：能区分副本数与参与共识节点数
# 3 个副本只是 leader-follower 的拷贝数；
# 默认写确认只在主库完成：waited=0、confirmation=master_only、is_consensus=false；
# Sentinel 的 quorum/多数派是 Sentinel 进程之间的，不是这 3 个 Redis 副本；
# 本教学引擎没有共识组：consensus_nodes=0。
# sentinel_quorum_is_not_replica_count=true。

# 验收 2：故障测试记录允许丢失的写
# 异步确认后切换：lost_writes=1，allowed_loss=true；
# WAIT 且提升已确认副本：lost_writes=0；
# WAIT 但提升落后副本：lost_writes=1，allowed_loss=true；
# 空主库重启：lost_titles=2，replicas_wiped=true。
# 「允许丢失」是测试记录，不是「开了三副本就不会丢」。
```

### 6. 自己动手的扩展练习

- 把 `wait_promote_lagging_replica` 里的强制提升从 `r2` 改成 `r3`，观察 `r1` 和另一个副本被全量同步后的值；再对比「按最高 offset 提升」的结果。
- 把 `min-replicas-to-write` 从 2 改成 3，暂停一个副本并推进超过 `max-lag`，观察写是否被拒绝；再恢复副本，看被拒绝的写有没有留下 offset。
- 把 `wait_timeout` 的超时从 100 毫秒改成 0，观察 `WAIT 2` 在只有一个副本能确认时会不会一直等；不要在生产代码里无条件使用 `timeout=0`。
- 在 `async_lag_stale_read` 里让 `r1` 断链而不是暂停，比较默认读旧值和 `serve_on_link_down=false` 两种结果。
- 给 `sync_latency` 增加一组「两个副本 50 毫秒、一个副本 200 毫秒」的 `WAIT 2`，观察它跟第二快还是最慢的副本走。

### 7. 和生产引擎的对应关系（不要记错边界）

- 教学 `ReplicationEngine` 是 Python 3 标准库模型，不是 Redis。没有 TCP、PSYNC/SYNC、RDB/AOF、diskless 传输、Sentinel 进程或 Cluster 槽位；教学复制 ID 是短字符串，教学 offset 按命令计数，不是 Redis 复制流的字节偏移。
- 教学 `WAIT` 用 `_wait_for` 循环应用到期命令并统计 `last_ack_offset >= offset` 的副本数；Redis 的 `waitCommand` 用客户端的 `c->woff`，并由 `replicationCountAcksByOffset` 统计确认 offset。两者语义相近，但实现和计数口径不同。
- 教学 `failover` 按 `(applied, -priority, name)` 选副本或按测试指定副本，不是 Sentinel 的法定人数选举，也不是 Cluster 故障切换；教学 `restart_empty` 模拟主库关闭持久化后空重启导致副本被全量同步清空，不是 Redis 的启动流程。
- 教学 `min-replicas-to-write` 用 `fresh_replicas()` 检查连接、存活和 `lag_s <= min_replicas_max_lag`；官方 `min-replicas-to-write` / `min-replicas-max-lag` 的语义是「至少 N 个延迟不超过 M 秒的已连接副本时才接受写」，仍是 best-effort，不等待这一次写。
- 教学断链读用 `serve_on_link_down` 表示两种配置行为；官方复制页写了「可继续用旧数据集回答」或「复制流中断时返回错误」，但没有在该页写出配置项名字，本课不补键名。

## 源码与文档阅读

按节点的 `sourceStudy`：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。2026-11-12 用 curl 打开的主文档是 [Redis replication](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)，返回 200，无跳转。页面支撑了本课的核心语义：Redis 默认异步复制；副本定期向主库异步确认已处理的数据量；主库平时不等待副本，但知道各副本处理到哪里，从而允许可选的同步复制；客户端可用 `WAIT` 请求某些写入的同步复制；`WAIT` 只能确保在其他 Redis 实例中有指定数量的已确认副本，并不会把一组 Redis 变成具有强一致性的 CP 系统；已确认的写在故障切换时仍可能丢失，取决于持久化配置；`WAIT` 会把丢失概率降到某些难以触发的故障模式。页面还写到 leader-follower 结构、Sentinel/Cluster 是额外的高可用层、命令流/部分重同步/全量同步三条路径、主库侧复制非阻塞、副本初次同步时可用旧数据集回答或配置成链路中断时返回错误、初次同步加载新数据集窗口会阻塞、`min-replicas-to-write` 与 `min-replicas-max-lag` 的 best-effort 语义、副本默认只读、主库关闭持久化并自动重启的危险模式、复制 ID 加 offset 标识数据集版本、提升后的副本使用新复制 ID、过期由主库合成 `DEL`、以及 `INFO replication` / `ROLE` 可查看状态和 offset。

[WAIT](https://redis.io/docs/latest/commands/wait/) 返回 200。页面写明 `WAIT numreplicas timeout` 的毫秒超时、`0` 表示一直等、自 Redis 3.0.0 起可用、返回已确认副本数、客户端必须检查返回值是否大于等于要求数量、超时或成功都会返回这个数字；在 Sentinel 或 Cluster 故障切换场景中 `WAIT` 改善现实世界的数据安全，但只是「更可能（但不保证）」提升到已收到该写的副本，仍是 best-effort，仍然可能丢失已经同步复制到多个副本的写。实现上为每个客户端记住写产生时的复制 offset，`WAIT` 检查是否有足够副本的确认 offset 大于等于该值；示例是 `SET foo bar`、`WAIT 1 0` 返回 1，`WAIT 2 1000` 在只有一个副本时 1 秒后返回 1；`WAIT` 不能在副本实例上使用。

[WAITAOF](https://redis.io/docs/latest/commands/waitaof/) 返回 200。它自 Redis 7.2 起可用，等待本地和/或副本把 AOF fsync 完成；官方同样写明它不会让 Redis 变成强一致存储，除非等待集群所有成员把写 fsync 到磁盘，故障切换或重启时仍可能丢数据。本课实验没有实现 `WAITAOF`，这里只作为文档阅读，不编造实验数字。

[Sentinel](https://redis.io/docs/latest/operate/oss_and_stack/management/sentinel/) 返回 200（复制页本身指向 Sentinel/Cluster 了解高可用与故障切换，因此打开核对）。页面写明 Sentinel+Redis 分布式系统不保证已确认的写在故障中保留，因为 Redis 使用异步复制；quorum 是就「主库不可达」达成一致的 Sentinel 进程数，用于判定 ODOWN，真正执行故障切换还需要 Sentinel 的多数派授权，少数派分区里不会切换；副本选择顺序是先 `replica-priority`（越低越优先，0 表示永不提升），相同则选复制 offset 更大、收到更多数据的，再相同则选 run ID 字典序更小的；分区下旧主库若仍接受写，分区愈合后这些写会丢失，可用 `min-replicas-to-write` 限制窗口。这些内容支撑了本课「3 个 Redis 副本不是 3 个 Sentinel 的 quorum」这条边界。

同期打开但**不要当作复制正文引用**：`https://redis.io/topics/replication` 返回 200，但最终跳到 `https://redis.io/docs/latest/develop/`（文档首页），不是复制页；`https://redis.io/docs/latest/topics/sentinel/` 返回 404。本课不引用这两处。`https://redis.io/docs/latest/commands/replicaof/`、`https://redis.io/docs/latest/commands/role/`、`https://redis.io/docs/latest/commands/info/`、`https://redis.io/docs/latest/operate/oss_and_stack/management/scaling/` 均为 200，本课只点到命令名和「Cluster 是额外层」，不展开未核对的 Cluster 槽位细节。

源码阅读以 [Redis 8.4 的 src/replication.c](https://raw.githubusercontent.com/redis/redis/8.4/src/replication.c) 为准，2026-11-12 返回 200；不要写成 unstable 或其他版本。下面按核对过的函数名和大致行号阅读，不编造没打开的函数体：

- `refreshGoodSlavesCount`（约 4461 行）：若配置了 `min slaves`，遍历 `server.slaves`，对 `SLAVE_STATE_ONLINE` 且 `lag = server.unixtime - slave->repl_ack_time` 不超过 `repl_min_slaves_max_lag` 的副本计数，写入 `repl_good_slaves_count`。对照本课 `fresh_replicas()` 和 `min-replicas-to-write` 的「最近 ping 过」语义。
- `checkGoodReplicasStatus`（约 4481 行）：未配置或 good count 大于等于 min 时才认为状态 OK。对照实验里只剩 1 个新鲜副本时写被拒绝。
- 约 4488 行的注释概括同步复制设计：全局 offset、主库发送命令时递增、副本 ping 已处理 offset、`WAIT <num_replicas> <milliseconds_timeout>` 返回已处理的副本数。对照本课 `WAIT 1` / `WAIT 3` 和 `wait_timeout`。
- `replicationRequestAckFromSlaves`、`replicationCountAcksByOffset`（约 4524 行）：统计 `SLAVE_STATE_ONLINE` 且 `repl_ack_off >= offset` 的副本数。对照教学 `_ack_count_unlocked` 的 `last_ack_offset >= offset`。
- `waitCommand`（约 4558 行）：用客户端的 `c->woff`；若当前确认数已够或 `CLIENT_DENY_BLOCKING` 则立刻返回该数字；否则 `blockForReplication`，并 `replicationRequestAckFromSlaves`。副本实例上调用 `WAIT` 会报错。对照本课 `waited` / `demanded` 和超时返回 1 的结果。
- `waitaofCommand`（约 4592 行）、`replicationCountAOFAcksByOffset`、`processClientsWaitingReplicas`：这里只点到名字与「同样按 offset 统计」，不编造没打开的函数体；本课实验没有实现 `WAITAOF`。

本课**没有打开 `sentinel.c`**，因此不声称核对过 Sentinel 的 C 源码；Sentinel 的 quorum、多数派和副本选择只按 2026-11-12 打开的官方页面描述。

## 自测题

**1.** 为什么「3 个副本」不等于「参与共识的 3 个节点」？`min-replicas-to-write=2` 改变了什么，又没有改变什么？

**答案：** 3 个副本只是 leader-follower 结构里的 3 份拷贝。默认异步复制下，写确认只在主库完成；教学实验里 3 个已连接副本的异步写仍然 `waited=0`、`confirmation=master_only`、`is_consensus=false`，本引擎 `consensus_nodes=0`。`min-replicas-to-write=2` 只是在主库接受写之前检查「最近有 N 个副本 ping 过且延迟不超过 M 秒」：三个副本新鲜时写成功，暂停两个并推进 2.1 秒后只剩 1 个新鲜副本，写被拒绝，`status=noreplicas`，标题仍是 `Weekly digest (revised)`。它没有等待这一次写到达副本，也没有让副本参与投票；官方称它是 best-effort 数据安全机制，仍有丢失窗口。Sentinel 的 quorum 是 Sentinel 进程之间就主库不可达达成一致的数量，真正切换还需要多数派授权，和这 3 个 Redis 副本不是同一层。

**2.** 同样 `WAIT 1` 成功，为什么提升已确认的 `r1` 丢失 0 条，强制提升落后的 `r2` 却丢失 1 条？`WAIT` 能保证故障切换不丢写吗？

**答案：** `WAIT 1` 让 `r1` 确认到 offset 2，所以提升 `r1` 时新主库有修订版，`lost_writes=0`，`r2`/`r3` 随后被全量同步成修订版。强制提升 `r2` 时，`r2` 还停在 offset 1、值是 `Weekly digest`；`r1` 虽然已有新标题，也被新主库全量同步改回旧值，`lost_writes=1`，`allowed_loss=true`。`WAIT` 不能保证故障切换不丢写：官方文档明确说它是 best-effort，仍可能丢失已经同步复制到多个副本的写；`WAITAOF` 也不是强一致承诺。`WAIT` 的返回值还必须检查：`wait_timeout` 里 `WAIT 2` 只等到 1 个副本，耗时 `0.100113414999214` 秒，客户端不能把超时返回当成成功。

**3.** 异步写返回 `ok` 后，为什么读 `r1` 仍是旧标题？主库关闭持久化后空数据集重启，为什么副本也会被清空？

**答案：** 异步写只要求主库完成，客户端不等待副本确认。实验 `async_lag_stale_read` 里，seed 占用 offset 1，发布修订是 offset 2；写返回 `ok`、`waited=0` 后，主库和 `r2`/`r3` 是 `Weekly digest (revised)`、offset 2，而暂停的 `r1` 仍是 `Weekly digest`、offset 1，`lag_offset=1`，所以读 `r1` 得到旧值；`flush` `r1` 后它追上 offset 2。空主库重启更危险：实验 `empty_master_restart` 里两个副本各有 2 个标题，主库无持久化宕机后以空数据集重启，仍连接的副本被全量同步清空，`master_keys_after=0`、`r1_keys_after=0`、`r2_keys_after=0`，`lost_titles=2`。官方把「主库关闭持久化并自动重启」列为危险故障模式：空的主库会让仍连接的副本全量同步，并把副本数据也清空。副本不是备份。

## 推荐资源

- [存储复制、确认与故障切换：参考文档 / 原始资料](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)（redis.io，英文文档，深入，免费）。重点阅读同步与异步复制、复制延迟和读陈旧，以及 `WAIT`、`min-replicas-to-write` 和故障切换相关段落，用于核对本节点实验的语义和边界。2026-11-12 该 URL 返回 200，无跳转。

## 本课小结

- 复制不是「多一份相同数据」，而是「多一份可能落后、可能被提升的数据」。Redis 默认异步复制，副本定期确认已处理的数据量；主库平时不等待副本，但知道各副本的进度，因此可以支持 `WAIT` 这种按次的可选同步复制。`WAIT` 只保证指定数量的副本确认到某个 offset，不会把 Redis 变成强一致 CP 系统；`WAITAOF` 也不是强一致承诺。教学引擎 `ReplicationEngine` 用短字符串 `replid` 和按命令计数的 offset 做近似，不是 Redis 的字节偏移。
- 同步复制改善持久性边界，但增加延迟并受慢副本影响。20 次写里，`async_fast` 总耗时 `9.975099237635732e-05` 秒、约 `200499.2584388598` ops/s；`wait1_fast` 总耗时 `0.00011532899225130677` 秒、约 `173416.93194040185` ops/s、`min_waited=3`；`wait1_all_slow_50ms` 总耗时 `1.0131437960080802` 秒、约 `19.740534442201223` ops/s；`wait1_one_fast_two_200ms` 总耗时 `0.00011922299745492637` 秒、约 `167752.87005815492` ops/s、`min_waited=1`；`wait3_one_slow_50ms` 总耗时 `1.0109846529958304` 秒、约 `19.78269397140145` ops/s。`WAIT 1` 可以跟最快副本走，`WAIT 3` 会被最慢副本拖住。
- 复制延迟导致读陈旧。`async_lag_stale_read` 里异步写返回 `ok`、`waited=0`、`offset=2`，主库和 `r2`/`r3` 已是修订版，`r1` 仍是旧标题、offset 1、`lag_offset=1`；`flush` 后追上。断链副本默认可以返回旧值，也可以配置成返回错误；实验 `link_down_read` 的 `status=link_down` 就是后一种。
- 故障切换会暴露已确认写的丢失。异步确认后切换丢失 1 条；`WAIT` 且提升已确认副本丢失 0 条；`WAIT` 但强制提升落后副本仍丢失 1 条；空主库重启丢失 2 个标题，`replicas_wiped=true`。3 个副本不是共识，`min-replicas-to-write` 只检查最近有 N 个副本 ping 过，不等待这次写；Sentinel 的 quorum 和多数派是 Sentinel 进程之间的判断，不是这 3 个 Redis 副本。教学故障切换函数按已应用 offset 挑选或按测试指定副本，不是 Sentinel 选举，也不是 Cluster 故障切换。
- 学完后可以用三句话自检：异步写返回 `ok` 后，为什么读落后副本会旧、切换会丢；`WAIT` 成功为什么仍可能丢，`min-replicas-to-write` 到底检查了什么；空主库重启为什么会把副本一起清空。三句都能答上来，并且能指出 `practice109/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验表，再回到官方文档里关于「默认异步复制」「`WAIT` 只保证指定数量的已确认副本」「已确认写仍可能丢失」「`min-replicas-to-write` 是 best-effort」「空主库重启会清空副本」以及 Sentinel quorum 的那几句，不要只把「多开几个副本就安全了」这句话背下来。

写作笔记时顺便记下三件事：你核对文档的日期和实际打开的页面（包括 `/topics/replication` 跳转到文档首页、`/docs/latest/topics/sentinel/` 返回 404 这两件事），这次实验里每个 `lost_writes` / `lost_titles` 对应哪一条写，以及教学 offset 和 Redis 字节 offset 的区别。这三件事能把本课的复制边界带进后面的高可用与一致性课，而不是只留下一个「副本越多越可靠」的印象。
