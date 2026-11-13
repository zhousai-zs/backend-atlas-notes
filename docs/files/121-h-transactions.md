# 第 121/208 课：分布式事务与补偿

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-transactions` |
| 英文名 | Distributed transactions |
| 领域 / 阶段 | H / 第 4 阶段 |
| 子类 | 分布式业务机制 |
| 难度 | 深入 |
| 前置节点 | [`h-failure`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/113.html)、[`g-transaction`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/99.html) |
| 标签 | `Distributed transactions`、`Distributed` |
| 参考资料 | [PostgreSQL — Two-Phase Transactions](https://www.postgresql.org/docs/current/two-phase.html)（PostgreSQL 官方文档，英文，深入，免费；2026-11-14 该地址返回 200，无跳转，最终 URL 仍是 `https://www.postgresql.org/docs/current/two-phase.html`，11575 字节，Content-Type 为 `text/html; charset=utf-8`。`<title>` 为 PostgreSQL: Documentation: 18: 67.4. Two-Phase Transactions；页面 `og:url` 为 `https://www.postgresql.org/docs/18/two-phase.html`，页眉指向 PostgreSQL 18.7 Documentation，copyright 为 The PostgreSQL Global Development Group，1996–2026。该页正文很短，只概述 2PC 并由外部事务管理器使用，点名 `PREPARE TRANSACTION`、`COMMIT PREPARED`、`ROLLBACK PREPARED`、`pg_twophase` 与 `pg_prepared_xacts`。同日继续打开这四个被点名的官方页面，均返回 200、无跳转： [PREPARE TRANSACTION](https://www.postgresql.org/docs/current/sql-prepare-transaction.html) 20035 字节，`<title>` 为 PostgreSQL: Documentation: 18: PREPARE TRANSACTION； [COMMIT PREPARED](https://www.postgresql.org/docs/current/sql-commit-prepared.html) 15616 字节； [ROLLBACK PREPARED](https://www.postgresql.org/docs/current/sql-rollback-prepared.html) 15831 字节； [pg_prepared_xacts](https://www.postgresql.org/docs/current/view-pg-prepared-xacts.html) 17376 字节，`<title>` 为 PostgreSQL: Documentation: 18: 53.17. pg_prepared_xacts） |
| 摘要 | 跨资源的原子提交需要协调；2PC 的准备状态和协调者故障影响可用性，Saga 则以业务补偿替代原子回滚。 |

> **边界说明**：本课里的「内容后台」「推荐位库存 `promo:weekly-digest`」「订单 `order-1` / `order-s1`」「文章键 `article:01:title`」「标题 `Weekly digest` / `Weekly digest (revised)`」都是**教学假设**，用来让两阶段提交、协调器崩溃和业务补偿变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `DistTxnEngine`，**不是 Redis，不是 redis-py，不是网络服务，不是真实的事务管理器，不是 PostgreSQL，不是 X/Open XA，也不是 TCC 产品**。它是单进程邮箱仿真，没有 TCP，没有真实丢包。持久化是教学用 dict，参与者在回复 PREPARE / COMMIT / ROLLBACK 之前 flush，协调器在登记事务和写入决策之前 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。`crash_coordinator()` 是全知崩溃：发往或来自协调器的在途消息被丢弃，易失状态被清空，持久日志保留。教学协调器在恢复时若发现事务已登记、但还没有决策日志，就按 **presumed abort** 回滚所有参与者；这是本课事务管理器的策略。PostgreSQL 作为参与者**不会**自动结束已 PREPARE 的事务，必须由外部管理器随后发出 `COMMIT PREPARED` 或 `ROLLBACK PREPARED`。教学引擎也不实现 `pg_twophase` 目录、checkpoint、`VACUUM` 与事务号回卷，只把「准备态占用资源」落实为库存预留；文档里「准备态持锁过久会妨碍 VACUUM，极端情况下数据库会为防止事务号回卷而关机」这一条，会在源码阅读里对照，不在数字里假装测到。Saga 的补偿是账本上的一条新业务操作，不是 `ROLLBACK PREPARED`，也不是数据库撤销准备态。本课所有时间都来自 `/workspace/atlas/lessons/practice121/results.json`，是**教学虚拟时钟上的数字**，不是生产 RTT。参数 `rpc_delay_s=0.01`。

## 导读

### 上一课留下的缺口

上一课的 `ShardMigrationEngine` 解决的是「不同的键由哪一组节点服务」：带版本的路由表、迁移期间的双写或变更日志，以及一个短的冻结窗口。`dual_write` 和 `change_log` 都能在写压测后交出 `4060` 个键、版本全对；只改路由或拷完存量不追增量，则会丢掉已确认的写。那一课的边界停在**同一个分片 KV** 里搬数据，没有回答另一件同样常见的事：一笔业务要同时改**两个服务**的状态——下一条订单、扣一份推荐位库存——怎样让它们要么一起生效，要么一起不作数。

分片迁移处理的是一份数据的位置和版本；跨服务事务处理的是两份独立资源的提交决策。上一课的 `route_version` 不能替订单服务向库存服务投票，库存服务也不会因为路由表切换就自动预留或释放一份库存。本课要补的就是这个协调：2PC 的 prepare / commit、协调器在各个阶段崩溃后怎么恢复，以及当业务不愿意承担准备态占用时，Saga 怎样用补偿代替原子回滚。

### 接上前置

前置之一 [第 99 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/99.html) `g-transaction` 讲的是单机事务：原子性、持久性、隔离和重试边界。那里的 safe 路径用一个事务把「先扣 alice、再给 bob 入账」包起来，崩溃后两边一起回滚，总额仍是 200。这个回滚能成立，是因为两边的账在**同一个数据库事务**里。本课的订单和库存是两个服务、两份持久状态，没有共享的 `ROLLBACK`。要把 99 课的原子性跨出去，就要么引入一个协调器做两阶段提交，要么接受每一步先本地提交，失败时用一笔新的业务操作去补偿。

前置之二 [第 113 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/113.html) `h-failure` 留下的结论是：超时只说明等待预算耗尽，不能证明远端没有执行；崩溃是停住不回答，不是一个全局的失败通知。协调器正好会在这种模型里出事。它可能在发出 PREPARE 之前崩溃，也可能在双方都已准备、甚至已经把 COMMIT 写入日志之后崩溃。参与者看不到「协调器没了」这条广播，只能继续持有准备态，直到协调器恢复并补发决策。113 课的「我不知道」在这里变成具体的占用：一份库存被预留下去，另一笔订单却还看不见。

### 教学场景（教学假设）

假设内容后台把一篇已发布文章 `article:01:title`（初值 `Weekly digest`）投放到推荐位。推荐位是一种有限库存，SKU 为 `promo:weekly-digest`，初始库存 `5`。一次投放要同时做两件事：订单服务记下 `order-1`，库存服务扣 1 个推荐位。两个服务没有共享事务，由一个教学协调器来协调。默认路径走 2PC；对照路径把每一步先本地提交，失败时再补偿。还有一条路径会把标题改成 `Weekly digest (revised)` 并视为已经推送给关注者，用来观察不可逆步骤。压测对照里则连续做 10 笔投放。`rpc_delay_s=0.01`，所有时间都是教学虚拟时钟上的值，不是线上超时，也不是生产 RTT。

### 本课三条主线

1. **2PC prepare/commit**：参与者先进入准备态并投票，协调器再做提交或中止的决策。准备成功之后，业务读取还看不到订单，但库存已经被占用；能发出的下一步只有 commit 或 rollback。
2. **协调者恢复**：协调器在发出 PREPARE 之前、只准备好一方、双方都已准备但尚未落盘决策、决策已落盘、以及只通知了一方之后崩溃，恢复时依据的是持久日志里有没有决策：没有就 presumed abort，有 COMMIT 就必须补齐提交。
3. **Saga 补偿与不可逆操作**：每一步先本地提交；失败时按相反顺序做补偿。补偿是账本上的新操作，不是数据库回滚。已经推送给关注者的标题改写，补偿无法撤回。

## 学习目标

- 能解释 2PC 的 prepare / commit 及适用边界：准备态为什么必须短，准备之后资源被谁占用，以及协调器不在时参与者为什么不能自行结束。
- 能独立完成实践：模拟订单与库存两个服务，在各阶段杀死协调器并恢复，观察库存、订单可见性和决策日志。
- 能说明补偿不是数据库回滚，并能列出 prepare 之后资源占用的风险。

## 为什么后端要懂

公众号内容后台（教学假设）要给一篇稿子买推荐位时，订单和库存往往已经拆成两个服务。如果库存先扣、订单还没写成协调器就宕了，推荐位会被空占用，别的投放进不来，编辑却查不到这笔订单。如果订单先写成再去扣库存，库存不足时又会留下一笔无法履约的单。把两边放进同一个单机事务，在服务拆开之后已经做不到；直接「先写一边，失败了再改回来」，又会把补偿和回滚混为一谈，遇到已经推送给关注者的标题，更是改不回去。

后端要能讲清失败语义：2PC 用准备态换原子性，代价是协调器和锁；Saga 用补偿换可用性，代价是短暂的不一致，以及必须承认有些步骤不可逆。对账不是「最终一致性」的反面，而是它能收敛的前提。选哪一条路，取决于业务能不能接受准备态占用，以及补偿是不是真的可逆。

## 核心知识点

### 1. 2PC prepare/commit

两阶段提交把一次跨资源的事务分成投票和决策。协调器先向每个参与者发 PREPARE，参与者把本地改动做成**可提交、也可回滚**的准备态，再投票。全部投是，协调器把 COMMIT 写入自己的持久日志，再通知各方提交；有任何人投否，或协调器在尚未记录决策时决定放弃，就发 ROLLBACK。

PostgreSQL 18 文档把这套命令写成给**外部事务管理器**用的接口，并说明它遵循 X/Open XA 的模型，但没有实现其中较少使用的部分。`PREPARE TRANSACTION` 的要点有三条。第一，成功之后，当前会话不再持有这笔事务，状态被完整记下，即使随后数据库崩溃，也有很高概率还能提交成功；之后从**任意会话**都可以对同一个事务标识做 `COMMIT PREPARED` 或 `ROLLBACK PREPARED`。第二，对发出 PREPARE 的那个会话来说，这条命令看起来不像提交，倒像回滚：当前事务结束了，准备态的效果暂时不可见，只有真正提交之后才会重新可见。第三，PREPARE 一旦失败，就变成一次 `ROLLBACK`，当前事务被取消。文档明确告诫：准备态只应短暂存在；准备态会继续持有它已经拿到的锁，拖太久会妨碍 `VACUUM` 回收存储，极端情况下数据库会为防止事务号回卷而关机。如果没有外部管理器负责及时关账，应把 `max_prepared_transactions` 设为 `0`，以免准备态被忘掉。

教学引擎按这个形状实现订单和库存，但不假装自己是 PostgreSQL。一次成功的 2PC 时间线如下。初始库存 `5`，没有任何订单。协调器为 `order-1` 登记 `xid-1` 后，先向订单服务发 PREPARE：`0.02` 订单投是，准备态里有这笔订单，但业务读取仍看不到它。再向库存发 PREPARE：`0.04` 库存也投是，可用库存从 `5` 变成 `4`，预约记在 `xid-1` 上。此时：

```text
stock=5  available=4  reservations={xid-1: 1}
prepared_order=xid-1  visible_orders=[]
votes={order:yes, inventory:yes}
```

订单对业务读取仍不可见（对应文档里「PREPARE 之后效果暂时不可见」），库存却已经被占用。`0.04` 协调器把决策 `commit` 写入持久日志，然后先通知订单、再通知库存。`0.06` 订单变为 `committed`，库存仍持有预约；`0.08` 库存扣减完成，`stock=4`，预约清空。最终 `order-1` 可见，双方都不再处于准备态，`consistent=true`，共用了 `8` 次 RPC、`6` 次 flush。

适用边界也在这条时间线上。2PC 能给出「要么两边都有、要么两边都无」的原子性，但要求每个参与者支持准备态，并在准备之后把决策权交给协调器。准备态必须短：文档把它写成预期用法，实验里从双方都 PREPARE 到全部 COMMIT，库存被占用了 `0.04` 秒（`0.04` 到 `0.08`）。若协调器在这期间消失，占用会一直持续到恢复，下面第二节会看到。参与者在准备之后不能自行「当作没发生」——否则协调器一旦已经写下 COMMIT，两边就会分叉。

### 2. 协调者恢复

113 课已经说明，崩溃不会给系统发一封全局通知。2PC 的协调器必须把自己的决策写成持久日志，恢复时只根据日志行事：已经写下 COMMIT，就只能补发 COMMIT；还没有决策，本课的教学管理器按 presumed abort 回滚。PostgreSQL 参与者本身不会做这个判断，它们会一直把准备态留到外部管理器来关账，可用 `pg_prepared_xacts` 查看。教学实验把「杀死协调器并恢复」做到五个阶段，每一次恢复后系统都重新变得一致，`all_2pc_consistent=true`。

| 崩溃点 | 崩溃时的可见状态 | 恢复策略 | 恢复完成时刻 | 最终订单 | 最终库存 |
|---|---|---|---:|---|---:|
| 发出任何 PREPARE 之前 | 库存 `5`，无准备态 | presumed abort，向双方补发 rollback | 0.02 | 无 | 5 |
| 只完成订单一侧 PREPARE | 订单在准备态、仍不可见，库存未占用 | presumed abort，回滚已准备的订单 | 0.04 | 无 | 5 |
| 双方都已 PREPARE，尚未写决策 | `available=4`，预约仍在，订单仍不可见 | presumed abort，回滚双方 | 0.06 | 无 | 5 |
| 决策 `commit` 已落盘，尚未通知 | 同上，但日志里已有 COMMIT | 重发 COMMIT | 0.06 | `order-1` 已提交 | 4 |
| 只通知了订单服务 | `order-1` 已提交，库存仍占用 1 个 | 按已记录的 COMMIT 补通知库存 | 0.08 | `order-1` 已提交 | 4 |

前三行没有决策日志，恢复后库存回到 `5`、没有任何可见订单，准备态被清空。关键是第三行：崩溃发生在 `0.04`，当时预约还在，`during_outage.reservation_held=true`，`order_still_invisible=true`。协调器停着的这段时间里，被预留的那一份库存谁也用不了，业务侧却查不到对应订单。这就是准备态对可用性的影响：不是数据写错了，而是资源被卡住了。

后两行已经有 COMMIT 日志，恢复必须把事务做完，不能改主意去回滚。尤其是只通知了一方的情形：`0.06` 订单已经是 `committed`，库存仍是 `stock=5` 且 `reservation_held=true`。如果恢复时错误地 abort，就会留下一笔没有库存的订单。教学引擎按日志补发 COMMIT，`0.08` 库存扣到 `4`，两边重新对齐。

「先写决策，再发通知」是这条恢复路径能成立的前提。如果协调器在内存里决定提交、还没 flush 就崩溃，教学策略会把它当成没有决策，走 presumed abort；已经收到 COMMIT 的参与者一旦存在，就会和这条策略冲突。所以决策必须先落盘。PostgreSQL 文档写 PREPARE 把状态完整记下、崩溃后仍有很高概率能提交，说的是**参与者**一侧；协调器自己的决策日志，要由事务管理器保证。

### 3. Saga 补偿与不可逆操作

Saga 把一笔跨服务的业务拆成若干本地事务，每一步先提交，失败时按相反顺序做补偿。它不再要求参与者进入准备态，因此也没有「PREPARE 之后效果不可见、但锁还在」的窗口。代价是：中间步骤已经对业务可见，补偿也不是把历史擦掉。

成功路径很短。`0.01` 库存本地扣 1，`stock=4`；`0.03` 订单本地提交为 `committed`；`0.04` 结束，`rpc_count=2`。没有准备态，也没有第二轮 COMMIT。

失败路径用来回答「补偿是不是数据库回滚」。实验让投放在「发布标题」这一步注入失败：库存和订单已经本地提交，发布没有发生。补偿按相反顺序执行：`0.06` 把 `order-s2` 的状态改成 `cancelled`，`0.08` 再补一条 `compensate_restock`，库存回到 `5`，`t_end=0.09`。库存账本里两条都在：

```text
t=0.01  saga_reserve          qty=1  stock_after=4
t=0.08  compensate_restock    qty=1  stock_after=5  undone=true
```

订单账本同样留下 `saga_create_order` 和 `compensate_cancel_order`。补偿没有把「预留」从历史上删掉，也没有走 `ROLLBACK PREPARED`：`compensation_is_new_operation=true`，`not_a_prepared_rollback=true`，`order_record_still_present=true`。它是一笔新的业务操作，有自己的时间和结果。

不是所有步骤都能这样补。对照实验先走完库存、订单，并把 `article:01:title` 改成 `Weekly digest (revised)`，视为已经推送给关注者；随后业务要求取消。库存可以退回 `5`，订单可以改成 `cancelled`，标题却必须留下：

```text
compensate_publish  irreversible=true  undone=false
title_still=Weekly digest (revised)
```

`publish_not_undone=true`。补偿在这里只能记下「无法撤回一次已对用户可见的推送」，不能假装发布没有发生。不可逆不是实现偷懒，而是业务边界：能补库存，补不回已经看到新标题的关注者。

## 常见误区

节点给出的误区是：**把最终一致性当作永不需要对账**。`pitfall_dropped_compensate` 让库存本地扣减成功（`t=0.01`，`stock=4`），创建订单失败，再把本应发出的 `compensate_restock` 丢掉。对账之前：库存是 `4`，没有任何订单，`stock_short_before_reconcile=true`。如果到此为止、以为「最终总会一致」，这份推荐位会一直少 1 个。对账任务发现一笔 `saga_reserve` 既没有对应的已提交订单，也没有成功的退库记录，补写 `reconcile_restock`，`0.04` 库存回到 `5`，`needed_reconcile=true`。最终一致性描述的是收敛的目标，不是「可以不做核对」的许可证。补偿消息会丢、进程会在补完之前崩溃，没有对账，不一致就停在那里。

第二个误区是把补偿当成数据库回滚。上一节的账本已经说明：回滚会让准备态消失、效果像从未发生（2PC 里 presumed abort 之后，订单不可见、预约清空）；补偿则留下「先扣后还」「先下单再取消」的轨迹，并且对已发布的标题无能为力。

第三个误区是以为「PREPARE 成功就等于下单成功」。主实践里 `0.04` 双方都已投票，可用库存已经少 1，但 `business_visible_orders` 仍是空列表。此时如果把占用当成已售，或把「还看不见订单」当成「库存也没动」，后面的对账会从错误的前提出发。只有协调器的 COMMIT 落地、参与者真正提交之后，订单才可见、库存才完成扣减。

## 设计权衡

节点给出的权衡是：**强原子性增加协调成本，补偿需要业务明确可逆边界**。同样连续完成 10 笔投放（初始库存 `20`，最后都是 10 笔订单、库存 `10`），两种路径的教学数字是：

| 路径 | 命令数 | `elapsed_s` | `rpc_count` | 从首次 PREPARE 到双方 COMMIT（均值） |
|---|---:|---:|---:|---:|
| 2PC | 10 | 0.60 | 80 | 0.06 秒（其中库存预约出现在第二次 PREPARE 之后，约 0.02 秒） |
| Saga | 10 | 0.40 | 20 | 0.00（每一步本地提交，不跨服务持有准备态） |

2PC 每笔要准备两方、再提交两方，实验里是 `8` 次 RPC；10 笔就是 `80` 次，耗时 `0.60` 秒。Saga 每笔只打两个服务各一次，`20` 次 RPC，耗时 `0.40` 秒，而且没有跨服务的准备态占用。`saga_fewer_rpcs=true`，`saga_no_prepared_lock_hold=true`。`lock_hold_s_mean=0.06` 量的是从发出第一次 PREPARE 到双方 COMMIT 完成；库存预约从第二次 PREPARE 成功（约 +0.04）持有到提交完成（+0.06），大约 `0.02` 秒。这些时间是虚拟时钟上、每次 RPC 单向 `0.01` 秒的结果，不是生产延迟，只说明方向：原子性把等待和锁集中在协调器与准备态上；补偿把成本换成「可逆怎么定义」和「不一致窗口里对外可见什么」。

可逆边界要事先写清。库存退回、订单取消，在本课里是可逆的补偿；把标题推给关注者，是不可逆的。如果业务把不可逆步骤放在 Saga 的中间，失败时就补不回已经发生的可见效果，只能留下补偿记录并转入人工或对账。2PC 也不是没有边界：它要求双方都支持准备态，并且协调器的决策日志可靠；它换来的是切换完成之前对外不可见，而不是「永远不会卡住」。

第二笔权衡是可用性与占用。文档要求准备态尽量短，因为锁会一直持有。实验 `occupancy` 把库存设为 `1`：第一笔在 `0.04` 双方 PREPARE 之后，`available_stock=0`，订单仍不可见。第二笔订单服务可以准备成功，库存却投否，`second_votes={order:yes, inventory:no}`，决策是 abort，第二笔不会提交。第一笔若此时还不做 commit 或 rollback（例如协调器在双方 PREPARE 之后崩溃、尚未恢复），最后一份库存会一直被占用，新的投放全部被拒。2PC 的原子性，在协调器故障窗口里体现为可用性下降。Saga 没有这个窗口，但会在补偿完成之前把「库存已扣、订单没有」或「订单已成、稍后取消」暴露给业务。

## 动手实践：模拟订单与库存两个服务，在各阶段杀死协调器并恢复

配套代码在 `/workspace/atlas/lessons/practice121/`，包括 `engine.py`、`app.py`、`test_txn.py`，用 Python 3 标准库实现，不依赖 Redis、PostgreSQL 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice121
bash test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT。`rpc_delay_s=0.01`。SKU 为 `promo:weekly-digest`，默认库存 `5`。

**1. 主实践：成功的 2PC，以及五个崩溃点。** 成功路径已在核心知识点第 1 节给出：`0.02` 订单准备，`0.04` 库存准备并占用 1 个名额，`0.04` 决策落盘，`0.06` 订单提交，`0.08` 库存提交，`stock=4`，`order-1` 可见，`consistent=true`。

五个崩溃点的恢复结果见第 2 节的表。这里把「占用」和「决策日志」再对一次现场：

```text
双方 PREPARE 后立刻崩溃（0.04）
  available=4  reservations={xid-1:1}  visible_orders=[]  decision=None
  恢复后 0.06：presumed_abort=true  stock=5  orders=[]  准备态清空

决策 commit 已落盘后立刻崩溃（0.04）
  占用同上，但 durable_decision=commit
  恢复后 0.06：order-1 committed  stock=4  准备态清空

只通知了订单后崩溃（0.06）
  order-1=committed  stock=5  reservation_held=true  durable_decision=commit
  恢复后 0.08：stock=4  预约清除  consistent=true
```

`kill_before_prepare` 和 `kill_after_one_prepare` 同样 presumed abort，分别在 `0.02`、`0.04` 结束，库存保持 `5`。五条路径加上成功路径，全部 `consistent=true`。

**2. 准备态占用 `occupancy`。** 把库存改为 `1`。第一笔在 `0.04` 完成双方 PREPARE：`available_stock=0`，`order_visible=false`，最后一份推荐位被 `xid-1` 预约。第二笔的订单服务投是，库存投否，`second_votes={order:yes, inventory:no}`，决策 abort，第二笔订单不会提交。第一笔此时仍停在准备态，`first_still_prepared=true`。实验在 `0.10` 结束时没有给第一笔发 COMMIT，就是为了把「准备态不结束，新业务就进不来」留在现场。文档把这种风险写成持锁过久；教学引擎把它写成可用库存为零。

**3. Saga 补偿、不可逆与对账。** 成功路径 `0.04` 结束，库存 `4`，`order-s1` 为 `committed`，`rpc_count=2`。失败路径在发布步骤注入错误：`0.01` 库存扣到 `4`，`0.03` 写下订单，随后补偿在 `0.06` 取消订单、`0.08` 退库，账本同时保留 `saga_reserve` 与 `compensate_restock`。不可逆路径先把标题改成 `Weekly digest (revised)`，再取消：库存退回 `5`，订单为 `cancelled`，标题仍是修订后的值，`publish_not_undone=true`。对账对照里，退库消息被丢掉，对账前库存停在 `4` 且没有订单；`reconcile` 在 `0.04` 补回库存，`needed_reconcile=true`。

**4. 权衡 `tradeoff`。** 10 笔投放：2PC 用 `0.60` 秒、`80` 次 RPC，每笔从第一次 PREPARE 到结束 `0.06` 秒（库存预约大约占后 `0.02` 秒）；Saga 用 `0.40` 秒、`20` 次 RPC，不跨服务持有准备态。两边最后都是 10 笔订单、库存 `10`。注意：成功路径那一次 2PC 把两份 COMMIT 改成了先后发送（为了插入「只通知一方」的崩溃点），结束于 `0.08`，双方 PREPARE 之后库存被占用到 `0.08`，共 `0.04` 秒；权衡实验里两份 COMMIT 一起发出，所以更短。数字都来自同一次 `results.json`，不要混用。

**5. 核对验收字段。** `acceptance_compensation_is_not_db_rollback=true`，`acceptance_prepare_occupies_resources=true`，`pitfall_eventual_consistency_needs_reconcile=true`，`tradeoff_atomicity_costs_coordination=true`，`all_2pc_consistent=true`。这些字段都在 `results.json` 顶层，测试脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 PostgreSQL — Two-Phase Transactions 中与 Distributed transactions 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [Two-Phase Transactions](https://www.postgresql.org/docs/current/two-phase.html) 返回 200，无跳转，11575 字节，`Content-Type` 为 `text/html; charset=utf-8`，`<title>` 为 PostgreSQL: Documentation: 18: 67.4. Two-Phase Transactions。`og:url` 为 `https://www.postgresql.org/docs/18/two-phase.html`，页眉为 PostgreSQL 18.7 Documentation，copyright 为 The PostgreSQL Global Development Group。该页只有两段正文，说明 PostgreSQL 支持 2PC，命令是 `PREPARE TRANSACTION`、`COMMIT PREPARED` 和 `ROLLBACK PREPARED`，供外部事务管理器使用；它遵循 X/Open XA 的特性与模型，但没有实现其中较少使用的部分。PREPARE 之后，下一步只能是提交或回滚已准备的事务。准备态预期很短，但外部可用性问题可能让事务在这个状态停留较久。短寿命的准备事务只存在共享内存和 WAL 中，跨越检查点的会记到 `pg_twophase` 目录；当前的准备事务可用 `pg_prepared_xacts` 查看。
- 因为概述页没有展开命令语义，同日按它点名的命令继续打开了四个官方页面，均返回 200、无跳转：[PREPARE TRANSACTION](https://www.postgresql.org/docs/current/sql-prepare-transaction.html) 20035 字节；[COMMIT PREPARED](https://www.postgresql.org/docs/current/sql-commit-prepared.html) 15616 字节；[ROLLBACK PREPARED](https://www.postgresql.org/docs/current/sql-rollback-prepared.html) 15831 字节；[53.17. pg_prepared_xacts](https://www.postgresql.org/docs/current/view-pg-prepared-xacts.html) 17376 字节。
- `PREPARE TRANSACTION` 写明：成功后事务与当前会话脱钩，状态完整落盘，即使崩溃也有很高概率仍能提交；之后任意会话都可提交或回滚。对发出该命令的会话，效果暂时不可见，像一次 `ROLLBACK`；命令失败则确实变成 `ROLLBACK`。它不适合普通应用或交互会话，只应被外部事务管理器用来跨多个数据库或其他事务资源做原子全局事务。不能准备涉及临时表、`WITH HOLD` 游标或 `LISTEN` / `NOTIFY` 的事务。文档用 Caution 强调：不要让准备态拖很久，它继续持锁，会妨碍 `VACUUM`，极端情况下可能导致数据库为防止事务号回卷而关机；若没有外部管理器跟踪并及时关闭准备事务，应把 `max_prepared_transactions` 设为 `0`。
- `COMMIT PREPARED` / `ROLLBACK PREPARED` 必须由原用户或超级用户执行，不必在原会话中，也不能放在一个事务块里；执行后立即完成。`pg_prepared_xacts` 为每个准备中的事务保留一行，在提交或回滚后删除，列包括事务号、全局事务标识 `gid`、准备时间、所有者和数据库。

论文式的规范在这里就是这份官方文档。教学实验按以下方式使用它，并记下差异。

- **角色**：文档中的 PostgreSQL 是参与者，真正做原子全局事务的是外部管理器。`DistTxnEngine` 同时模拟了协调器和两个参与者，协调器不是 XA 实现，也不是 PostgreSQL。
- **准备态可见性**：文档说 PREPARE 之后效果对当前会话不可见，直到 COMMIT。教学引擎里，准备中的订单不进入业务可见的 `orders`，库存只减少 `available_stock`，`stock` 仍要等到 COMMIT 才减。
- **恢复**：PostgreSQL 崩溃重启后仍保留准备事务，等待外部的 `COMMIT PREPARED` 或 `ROLLBACK PREPARED`，自己不会 presumed abort。教学协调器在没有决策日志时会 presumed abort；这是管理器策略，必须在课文里与参与者行为分开。
- **持久化**：文档区分共享内存加 WAL，以及跨越检查点后的 `pg_twophase`。教学引擎只有 dict flush，没有 WAL、检查点、目录或 `fsync`。
- **持锁风险**：文档提到 VACUUM 与事务号回卷。教学引擎只用库存预约来表现「准备态占用资源」，没有实现 VACUUM，也没有事务号。
- **命令限制**：教学引擎没有临时表、会话参数、`LISTEN`、权限模型和 `max_prepared_transactions`。
- **Saga**：文档不讨论 Saga。补偿、不可逆和对账是本课在 2PC 之外的对照，不能读成 PostgreSQL 的行为。
- **网络与时间**：单进程邮箱，`rpc_delay_s=0.01` 是教学延迟，不是生产 RTT。`crash_coordinator()` 是全知崩溃。

## 自测题

**题 1.** 为什么说补偿不是数据库回滚？请用 `compensate` 和 `irreversible` 两组实验的账本与标题说明。

<details>
<summary>答案</summary>

数据库回滚（以及 2PC 的 `ROLLBACK PREPARED`）会结束准备态，让那笔事务的效果像从未发生：可见订单仍是空的，预约被抹掉。补偿是在本地事务已经提交之后，再记一笔方向相反的新业务操作，历史还在，有些效果也补不掉。

失败路径里，发布步骤被注入错误。库存已经在 `0.01` 扣到 `4`，订单已经在 `0.03` 写成；补偿并没有把这两条记录删掉，而是在 `0.06` 把订单改为 `cancelled`，在 `0.08` 再写一条 `compensate_restock` 把库存补回 `5`。账本上 `saga_reserve` 和 `compensate_restock` 同时存在，`compensation_is_new_operation=true`，`not_a_prepared_rollback=true`，`order_record_still_present=true`。

不可逆路径走得更远：标题已经被改成 `Weekly digest (revised)` 并视为已推送给关注者。随后的取消可以把库存退回 `5`、把订单改为 `cancelled`，但 `compensate_publish` 的结果是 `irreversible=true`、`undone=false`，标题仍是 `Weekly digest (revised)`。如果补偿等于回滚，这次发布应当被撤掉。它撤不掉，正说明补偿受业务可逆边界约束，不是准备态上的 undo。

</details>

**题 2.** PREPARE 之后，资源占用有哪些风险？请用成功路径里 `0.04` 的状态、`occupancy` 实验，以及协调器在双方都已准备后崩溃的那段窗口说明。

<details>
<summary>答案</summary>

PREPARE 成功并不等于业务已经完成，但它已经把资源占住。成功路径的 `0.04`：双方都投了是，`available_stock=4`，`reservations={xid-1:1}`，`prepared_order=xid-1`，而 `business_visible_orders` 仍是空的。一份推荐位从可用库存里消失了，编辑却还查不到订单。文档也写过：准备态会继续持有已经拿到的锁，只应短暂存在。

把库存改成 `1` 时，风险更具体。第一笔在 `0.04` 进入准备态后，`available_stock=0`，订单仍不可见。第二笔库存投否，`votes={order:yes, inventory:no}`，整笔 abort，第二笔订单不会生成。第一笔若迟迟不提交、不回滚，最后一份库存会一直被占用，新的投放全部被拒，`first_still_prepared=true`。

协调器在双方都已 PREPARE、尚未写决策时崩溃，会把这个窗口拉长：`0.04` 当时 `reservation_held=true`、`order_still_invisible=true`。恢复之前，被预留的库存既不能卖给别人，也不能当作本笔已经成交。本课的协调器在 `0.06` 按 presumed abort 回滚双方，库存回到 `5`；如果协调器迟迟不恢复，占用会一直持续。PostgreSQL 作为参与者不会自动结束准备态，还要额外面对持锁妨碍 `VACUUM`、极端情况下事务号回卷的风险。所以「列出 prepare 后资源占用的风险」，至少包括：业务可见性滞后、并发事务被拒、协调器故障延长占用，以及真实数据库里持锁过久带来的运维后果。

</details>

**题 3.** 协调器在「双方都已 PREPARE」和「COMMIT 决策已落盘」两个时刻崩溃，恢复后为什么结果不同？若把最终一致性理解成「不必对账」，会在 Saga 路径上看到什么？

<details>
<summary>答案</summary>

区别在持久日志里有没有决策。双方都已 PREPARE、尚未写决策时崩溃，教学协调器恢复时只能 presumed abort：它不能冒充自己曾经决定提交，否则无法与「其实还没决定」的状态区分。实验里这条路径在 `0.06` 回滚双方，库存回到 `5`，没有可见订单。COMMIT 已经落盘后再崩溃，决策不可更改，恢复必须重发 COMMIT：实验里 `0.06` 得到 `order-1` 已提交、库存 `4`。如果这时错误地 abort，就会和可能已经（或即将）按 COMMIT 行事的参与者分叉；「只通知了订单」的对照把这一点表现得更清楚：`0.06` 订单已提交、库存仍占用，恢复只能把库存也提交掉。

Saga 路径上，把最终一致性当成「不必对账」，会把一次丢失的补偿当成还会自己修好。`pitfall_dropped_compensate` 在订单失败后丢掉 `compensate_restock`，对账前库存停在 `4`，又没有已提交订单。系统不会自动回到 `5`，除非跑对账：`needed_reconcile=true`，`0.04` 才把库存补回。最终一致性要有人去核对并修补，不是一种可以省略对账的保证。

</details>

## 推荐资源

- [PostgreSQL — Two-Phase Transactions](https://www.postgresql.org/docs/current/two-phase.html)（PostgreSQL 18.7 文档，英文，深入，免费）：用于核对 2PC 的 prepare / commit、准备态的预期寿命，以及外部事务管理器与参与者的分工，并将结论落到本节点的协调器恢复实验。2026-11-14 该地址返回 200，无跳转，11575 字节，`<title>` 为 PostgreSQL: Documentation: 18: 67.4. Two-Phase Transactions。同日打开的命令页：[PREPARE TRANSACTION](https://www.postgresql.org/docs/current/sql-prepare-transaction.html)、[COMMIT PREPARED](https://www.postgresql.org/docs/current/sql-commit-prepared.html)、[ROLLBACK PREPARED](https://www.postgresql.org/docs/current/sql-rollback-prepared.html)、[pg_prepared_xacts](https://www.postgresql.org/docs/current/view-pg-prepared-xacts.html)。文档不讨论 Saga；补偿与对账是本课的对照实验，不是 PostgreSQL 的行为。

## 本课小结

- 2PC 用 prepare / commit 把跨服务的原子性交给协调器。成功路径里，`0.04` 双方都已准备：可用库存少 1，订单仍不可见；`0.08` 双方提交后，`order-1` 可见，`stock=4`。PREPARE 成功不等于下单成功，但资源已经被占用。
- 协调器恢复看的是决策日志。尚未落盘就崩溃，本课按 presumed abort 回滚，库存回到 `5`；COMMIT 一旦落盘，即使只通知了一方，恢复也必须把另一边补提交。五条崩溃路径加上成功路径全部 `consistent=true`。PostgreSQL 参与者不会自动 abort 准备态，外部管理器必须来关账。
- 准备态占用会挡住新业务。库存为 `1` 时，第一笔准备后 `available_stock=0`，第二笔库存投否，整笔 abort；协调器若在此时消失，最后一份库存会一直被卡住。文档还指出持锁过久会妨碍 VACUUM，极端情况下触发事务号回卷保护。
- 补偿不是数据库回滚。失败路径的账本同时留下 `saga_reserve` 和 `compensate_restock`，订单被改为 `cancelled` 而不是消失。已经推送的 `Weekly digest (revised)` 无法撤回，`publish_not_undone=true`。
- 最终一致性需要对账。丢掉补偿后库存停在 `4` 且没有订单；`reconcile` 之后才回到 `5`。
- 强原子性更贵：10 笔投放，2PC 用 `0.60` 秒、`80` 次 RPC，每笔从第一次 PREPARE 到结束 `0.06` 秒；Saga 用 `0.40` 秒、`20` 次 RPC，不跨服务持有准备态。这些是教学虚拟时钟上的数字，不是生产 RTT。补偿路径必须事先写清哪些步骤可逆。
- `DistTxnEngine` 不是 PostgreSQL，也不是 XA。它没有 `pg_twophase`、没有 WAL，决策恢复策略是教学管理器的 presumed abort；Saga 是对照，不是文档里的命令。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到 `PREPARE TRANSACTION` 的 Caution、以及「PREPARE 之后效果暂时不可见、但锁还在」这句话，不要把「两边都投了是」当成「已经下单成功」，也不要把「最终会一致」理解成「不必对账」。
