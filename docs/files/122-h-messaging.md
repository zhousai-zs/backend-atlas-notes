# 第 122/208 课：消息投递与幂等消费

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-messaging` |
| 英文名 | Message delivery |
| 领域 / 阶段 | H / 第 3 阶段 |
| 子类 | 分布式业务机制 |
| 难度 | 进阶 |
| 前置节点 | [`h-transactions`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/121.html) |
| 标签 | `Message delivery`、`Distributed` |
| 参考资料 | [Apache Kafka — Message Delivery Semantics](https://kafka.apache.org/documentation/#semantics)（Apache Kafka 官方文档，英文，进阶，免费；2026-11-14 该地址返回 HTTP 200，**没有 HTTP 3xx 跳转**，`Content-Length` / 实收 20078 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Sun, 08 Nov 2026 16:53:55 GMT`。`<title>` 为 Documentation Redirect \| Apache Kafka，页面元数据 `og:url` 为 `https://kafka.apache.org/documentation/`，`description` 为 Redirecting…。这是一份带锚点映射的 JavaScript 跳转页，不是「Message Delivery Semantics」正文。页内脚本把当前文档版本记为 `t="44"`，并把 `"#semantics"` 映射到 `"design/design/#message-delivery-semantics"`。同日继续打开实际正文 [Design \| Apache Kafka](https://kafka.apache.org/44/design/design/#message-delivery-semantics)，返回 HTTP 200，无跳转，`Content-Length` / 实收 148030 字节，`Content-Type` 为 `text/html`，`Last-Modified` 同样为 `Sun, 08 Nov 2026 16:53:55 GMT`。`<title>` 为 Design \| Apache Kafka，`og:url` 为 `https://kafka.apache.org/44/design/design/`，页眉面包屑为 AK 4.4.X，`article:modified_time` / `itemprop=dateModified` 为 `2026-10-26T16:02:25+00:00`（北京时间 2026-10-27 00:02:25）。正文里与本课直接相关的是 *Message Delivery Semantics*、*Using Transactions*、*Consumer Position*，以及 *The Producer* 中按 key 分区的说明） |
| 摘要 | 至少一次投递允许重复；可靠消费必须把业务状态和去重标记纳入同一可恢复边界。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「结算入账 `settlement:s1` / `+30`」「创作者钱包初始余额 `100`」「主题 `wallet-credits`」都是**教学假设**，用来让 ack、重投、outbox、去重表、分区顺序和毒消息变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `MessageDeliveryEngine`，**不是 Redis，不是真实的消息队列或 broker，不是网络服务，不是 Apache Kafka，也不是 Kafka 客户端 / Kafka Streams / 事务生产者**。它是单进程里的分区日志仿真，没有 TCP，没有 ISR，没有 broker 复制，没有 `transactional.id`，没有 `isolation.level`，也没有真实丢包。持久化是教学用 dict，在追加日志、提交消费位置、写入钱包与去重标记、以及把毒消息写入教学死信日志之前 flush，不是磁盘，也没有 `fsync`。`Clock` 是虚拟时钟。`crash_consumer()` 会丢掉易失的消费位置，恢复时从已提交的位置重新加载；这是本课消费者的策略。分区函数是教学用的 `sum(key.encode()) % n_partitions`，不是 Kafka 的 murmur2。幂等生产只用 `(producer_id, seq)` 去重，不是 Kafka 0.11 之后的 PID / epoch 协议。本课所有时间都来自 `/workspace/atlas/lessons/practice122/results.json`，是**教学虚拟时钟上的数字**，不是生产 RTT。参数 `rpc_delay_s=0.01`。文档里「等待消息提交可能大约需要 10 ms」是官方文档的数量级说明，不会被写进本课的实验数字。

## 导读

### 上一课留下的缺口

上一课的 `DistTxnEngine` 解决的是「一笔业务要同时改两个服务」：2PC 用准备态换原子提交，Saga 用补偿换可用性，并对账把丢掉的补偿补回来。成功的 2PC 里，`0.04` 双方都已准备，可用库存少 1、订单仍不可见；`0.08` 双方提交后，`order-1` 可见，库存为 `4`。那一课的边界停在**同步协调两个参与者**，没有回答另一件同样常见的事：业务先在本地提交，再把「请给创作者钱包入账 30」交给一条消息。消息可能丢，也可能被投递两次。

跨服务事务处理的是两份资源的同一笔决策；消息投递处理的是「发出去之后，对端会不会做、会不会多做」。上一课的协调器决策日志不能替消费者记住「这笔结算是不是已经入过账」，钱包也不会因为协调器提交成功就自动幂等。本课要补的就是这个投递与消费的边界：ack 与重投、把业务状态和去重标记放进同一可恢复边界、以及分区能保证什么顺序、不能保证什么顺序。

### 接上前置

前置 [第 121 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/121.html) `h-transactions` 留下的结论是：没有共享事务时，要么引入协调器做两阶段提交，要么接受每一步先本地提交、失败时用一笔新的业务操作去补偿。补偿不是数据库回滚；超时也不能证明对端没有执行。把这条结论搬到消息上，就会看到对称的两种风险。如果先提交消费位置再处理，崩溃会把消息弄丢，这是 at-most-once。如果先处理再提交位置，崩溃会让同一条消息再来一次，这是 at-least-once。121 课里「我不知道对面做没做」在这里变成具体的重投：钱包已经加了 30，ack 还没发出去，消费者挂了，这条入账会再被拉一次。

本课的可靠消费，其实是把 121 课「同一可恢复边界」这句话换了一个落点：不是协调器的决策日志，而是钱包余额和去重标记必须一起 flush。文档把 Kafka 内部的 exactly-once 写成「把消费位置和输出写进同一笔事务」；写到 Kafka 以外的系统时，需要那些系统配合。outbox 是这句话的反向操作：本地业务和待发送的消息先一起落盘，再异步发出去。

### 教学场景（教学假设）

假设内容后台把一篇已发布文章 `article:01:title`（标题 `Weekly digest`）的结算入账发给创作者钱包。钱包初始余额 `100`，一笔结算记为 `settlement:s1`，金额 `+30`，消息写到教学主题 `wallet-credits`。默认路径是：消费者先处理、再 ack；对照路径会在业务提交之后、ack 之前把消费者杀死，观察重投是否再次增加余额。还有一条路径先 ack 再处理，用来看消息丢失。生产者侧对照 outbox 与「提交后再发送」。分区对照把 `wallet:alice` 和 `wallet:bob` 打到两个分区。毒消息对照则在两条正常入账中间插入一条无法处理的记录。`rpc_delay_s=0.01`，所有时间都是教学虚拟时钟上的值，不是线上超时，也不是生产 RTT。

### 本课三条主线

1. **ack 与重投**：先处理再保存消费位置，对应至少一次；先保存位置再处理，对应至多一次。业务提交后、ack 前崩溃，重投一定会再发生一次处理。
2. **outbox 与去重表**：本地提交和待发消息一起 flush，崩溃后还能补发；去重标记必须和余额写在同一可恢复边界里，否则至少一次投递会把 30 加成 60。
3. **顺序与分区**：同一个 key 进入同一个分区，分区内有序；跨分区没有全局顺序。毒消息若不隔离，会挡住分区里排在后面的正常消息。

## 学习目标

- 能解释 ack 与重投及适用边界：至少一次为什么允许重复，至多一次为什么可能丢消息，以及「处理成功」为什么不等于「中间件不会再投」。
- 能独立完成实践：在业务提交后 ack 前故障，验证重投不重复增加余额。
- 能区分 exactly-once 处理范围与外部副作用，并能验证毒消息隔离。

## 为什么后端要懂

公众号内容后台（教学假设）按篇结算时，入账往往已经不和发布接口走同一笔同步事务，而是写成一条消息。如果这条消息在钱包已经加了 30、ack 还没发出时消费者宕了，中间件会再投一次。余额是支付语义：加两次就是多付。库存扣减、任务调度也是同一类问题——节点把「为什么要懂」写成：避免支付、库存或任务调度重复生效。

后端要能讲清失败语义。消息中间件给出的至少一次、至多一次、恰好一次，都有明确的范围。Kafka 的文档写过：许多系统声称 exactly-once，但细读会发现这些声明在生产者或消费者会失败、有多个消费进程、或磁盘上的数据可能丢失时并不成立；Kafka 默认提供的是至少一次，把消费位置和输出一起提交才能在 Kafka 话题之间取得 exactly-once，写到外部系统则需要那些系统配合。把中间件的承诺直接扩大到一次 HTTP 推送、一次短信或一次银行调用，重投时副作用会再执行一遍。去重键也不是记下来就永远有效，保留窗口过短，回放仍会再入一笔账。

## 核心知识点

### 1. ack 与重投

官方文档把投递语义分成三条：at-most-once（消息可能丢失，但不会被重投）、at-least-once（消息不会丢，但可能被重投）、exactly-once（每条消息只被处理一次）。它还把问题拆成发布时的持久性，以及消费时如何更新位置。

消费侧有两种典型排法。一种是先保存位置、再处理：消费者在保存位置之后、写出处理结果之前崩溃，接任者会从已经保存的位置继续，前面几条可能没被处理——这是 at-most-once。另一种是先处理、再保存位置：消费者在处理之后、保存位置之前崩溃，接任者会再收到已经处理过的那几条——这是 at-least-once。Kafka 把消费位置做成每个分区一个整数（下一条要读的 offset），ack 因此很便宜；这也意味着 ack 只是「我把位置往前挪了」，不是「对端业务已经不可撤销地完成，并且不会再见到这条消息」。

本课主实践走第二条路。教学引擎里，`poll` 只推进易失位置，`ack` 才把 `committed` 写成 `offset+1` 并 flush；`crash_consumer()` 丢掉易失位置，`recover_consumer()` 从 `committed` 重新加载。一条 `settlement:s1`、金额 `+30` 的入账被写入分区 0 的 offset `0` 之后：

```text
t=0.00  余额 100，日志为空
t=0.01  生产者写入一条消息，log_length=1
t=0.02  消费者处理成功，余额 130，尚未 ack
t=0.02  业务提交后、ack 前消费者崩溃（committed 仍是 0）
t=0.03  恢复后同一条消息被重投并再次处理
t=0.04  第二次处理后才 ack，committed_offset=1
```

不启用去重时，第二次处理会再加 30，`balance_after_redelivery=160`，`double_credited=true`，`rpc_count=4`，`flush_count=4`。这就是至少一次投递允许重复：日志里仍然只有一条记录（`log_length=1`），重复发生在消费端。对照路径把顺序改成先 ack 再处理，然后立刻崩溃：`committed_offset=1`，恢复后 `repoll=None`，余额停在 `100`，`message_lost=true`，`credit_missing=true`，结束于 `0.04`。先保存位置，换来的是消息丢失，不是「更安全」。

文档还写过生产侧的对称问题：生产者在网络错误时无法区分消息是在提交之前还是之后失败的；0.11.0.0 之前只能重发，因而至少一次；之后可以用幂等生产者，靠 broker 分配的生产者 ID 和序号避免日志里出现重复条目。教学引擎用 `(producer_id, seq)` 做了一个对照：同一次请求被重发时，关闭幂等会得到 `naive_log_length=2`，打开后是 `idempotent_log_length=1`，两条路径都在 `0.02` 结束。这只说明「重复发送不必写成两条日志」，不能把它读成「下游业务自动 exactly-once」。它也不是 Kafka 的 PID / epoch 协议。

### 2. outbox 与去重表

至少一次投递本身没有错，可靠消费靠的是：业务状态和去重标记必须纳入同一可恢复边界。主实践的另一条路径把 `idempotency_key=settlement:s1` 和余额一起 flush。时间线与上面相同，到 `0.02` 余额也是 `130`，去重表里已经有 `settlement:s1`；崩溃之后重投再次进入 `apply`，这次 `second_duplicate=true`、`second_applied=false`，`balance_after_redelivery=130`，随后在 `0.04` ack。`rpc_count` 仍是 `4`，`flush_count` 变成 `3`——重复那一次没有再写钱包。`exactly_once_wallet=true`。实践题要验证的，就是这个结果：重投发生了，余额没有从 130 变成 160。

这和文档给外部系统的建议是同一句话的两种写法。文档说：写到外部系统时，难点是让消费位置和实际输出保持一致；经典做法是对位置和输出做两阶段提交，更简单、也更普遍的做法是让消费者把 offset 存在输出所在的同一个地方。去重表是「输出这边」的实现：余额和键一起落盘，重投只是再看见一次已经生效的键。它没有把 Kafka 的 offset 存进钱包，但满足同一条约束——可恢复状态里必须能回答「这笔结算是不是已经入过账」。

outbox 是这句话的反向操作，用在生产者。业务先本地加 30，再通知下游。如果「提交成功之后再发送」，教学对照在 flush 之后、`produce` 之前崩溃：余额已经是 `130`，`log_length=0`，`message_lost=true`，`t_end_lost=0.00`（这次崩溃发生在下一次 RPC 之前，虚拟时钟没有往前走）。改成 outbox 之后，余额和待发消息 `ob-1` 在同一次 flush 里提交，`pending_after_commit=1`；即便此时进程停住，恢复后仍能 `drain_outbox()`。实际排出在 `0.01`，`log_length=1`，`outbox_survives_crash_before_send=true`。本地提交和消息发出不再要求恰好落在同一个瞬间，但要求它们先进入同一个可恢复位置。

去重键不能无限期假装还在。权衡实验把保留窗口设为 `0.05`：第一次处理在 `0.02` 把余额写到 `130`，ack 在 `0.03`；等到 `0.09` 再 rewind 回放，第二次 `apply` 发生在 `0.10`，键已经过期，余额变成 `160`，`t_replay=0.11`。把窗口留到 `1.0` 秒，同样等 `0.06` 再回放，`duplicate_on_replay=true`，余额仍是 `130`。幂等键需要保留窗口，窗口的长度是业务能容忍的回放范围，不是「记过一次就永远安全」。

### 3. 顺序与分区

文档把 topic 写成一组全序分区：在同一个消费组里，每个分区同一时刻只被一个消费者消费，位置就是一个整数。生产者可以按 key 做语义分区——例如用 user id 作 key，同一用户的数据会进入同一分区，消费者才能做局部性假设。本课的教学哈希是 `sum(key.encode()) % n_partitions`，不是 murmur2；在 `n_partitions=2` 时，`wallet:alice` 落到分区 `1`，`wallet:bob` 落到分区 `0`。

实验先向 Alice 写入 `+30`、再写入 `+10`，然后向 Bob 写入 `+5`。消费时故意在 Alice 的第二条之前插入 Bob 的那一条，得到的处理顺序是：

```text
A offset=0 amount=30  余额 130
B offset=0 amount=5   余额 135
A offset=1 amount=10  余额 145
```

`alice_amounts_in_order=true`：同一分区里 Alice 的 30 一定在 10 之前。`bob_applied_between_alice=true`：跨分区没有全局顺序，Bob 的 5 可以插在 Alice 两条消息中间。最终余额 `145`，结束于 `0.09`。需要「同一钱包的入账按产生顺序生效」时，必须让这些消息共享分区键；需要「全站严格总序」时，分区做不到，也不要从至少一次投递里推导出总序。

分区还有一个操作后果：队头阻塞。三条消息依次是 `good1`（+30）、一条 `op=poison` 的坏消息、`good2`（+10）。先成功处理 `good1`，余额到 `130`，ack 之后 `committed=1`。若不隔离毒消息，消费者会在 offset `1` 上反复 `poll` / 拒绝 / 回退位置：实验连续尝试了 5 次（`attempt` 从 1 到 5），`dlq_count=0`，后面那条 +10 始终进不来，`final_balance=130`，`head_of_line_blocked=true`，结束于 `0.10`，`rpc_count=10`。把 `max_retries=3` 用尽后移入教学死信日志并 ack，毒消息在 `0.08` 被隔离（`attempts=3`，`offset=1`），随后 `good2` 被处理，余额到 `140`，`dlq_count=1`，`later_message_applied=true`，结束于 `0.11`，`rpc_count=11`。隔离不是「这条坏消息变得可以处理」，而是「不要让它永远占住分区的位置」。

## 常见误区

节点给出的误区是：**把消息中间件承诺扩大到所有外部系统**。`side_effect_pitfall` 仍然使用去重表：重投后钱包余额停在 `130`，`wallet_exactly_once=true`，`second_was_duplicate=true`。但每一次 `apply` 都发起一次未纳入同一可恢复边界的外部推送，`push_attempts=2`，`external_side_effect_duplicated=true`，结束于 `0.04`。钱包侧的 exactly-once 在这里是成立的；那次推送不成立。文档的细读条件与这句话直接对应：exactly-once delivery for other destination systems generally requires cooperation with such systems；默认提供的是至少一次。中间件没有、也不能替一次 HTTP 调用、一次短信或一次银行扣款做决定。

第二个误区是把「日志里只有一条」当成「业务只生效一次」。幂等生产者可以把一次重发收成 `idempotent_log_length=1`，主实践的至少一次路径里 `log_length` 也始终是 `1`。没有去重时，消费端还是把余额加成了 `160`。重复发生在哪一层，要单独验证。

第三个误区是把 ack 理解成业务提交本身。ack 提交的是消费位置。主实践在 `0.02` 余额已经是 `130`，但 `committed` 仍是 `0`；先 ack 再处理的对照里，`committed_offset=1`，余额却还是 `100`。位置和输出若不在同一边界里，这两个数字可以各说各的。

## 设计权衡

节点给出的权衡是：**事务消息增加协调成本，幂等键需要保留窗口**。连续完成 10 笔本地提交并发布消息时，两条路径的教学数字是：

| 路径 | 笔数 | `elapsed_s` | `rpc_count` | `flush_count` | 发布条数 |
|---|---:|---:|---:|---:|---:|
| 提交后再发送 | 10 | 0.10 | 10 | 20 | 10 |
| outbox（本地提交与待发消息同一次 flush，再排出） | 10 | 0.10 | 10 | 30 | 10 |

两条路径都在虚拟时钟 `0.10` 结束，RPC 次数都是 `10`，因为本课只有 `_rpc()` 推进时钟，flush 不推进。协调成本首先表现在持久化次数：outbox 每笔要写业务、追加日志、再标记已发送，`flush_count=30`；提交后立刻发送只需业务 flush 加日志 flush，`flush_count=20`，`transactional_path_more_flushes=true`。少掉的那 10 次 flush 不是免费的：同一组对照里，提交后、发送前崩溃的那一笔，余额已经增加，日志仍是空的，`crash_demo_lost=true`。outbox 用多出来的协调换来「崩溃后还能补发」，`outbox_publishes_all=true`。这些时间是虚拟时钟上、每次 RPC 单向 `0.01` 秒的结果，不是生产延迟，只说明方向：把消息纳入可恢复边界会增加协调，省掉这笔协调就要接受丢失窗口。

第二笔权衡是保留窗口。`dedup_retain_s=0.05` 时，第一次入账在 `0.02`，ack 在 `0.03`，等到 `0.09` rewind，`0.10` 再次处理时键已过期，余额从 `130` 变成 `160`，`short_window_replays_after_expire=true`。窗口留到 `1.0` 秒，同样间隔下回放会被识别为重复，余额仍是 `130`，`long_window_holds=true`。窗口太短，修复缺陷后的合法回放会再入一笔账；窗口太长，去重表会一直占着空间。窗口长度必须按「可能 rewind 多远」来选，不能按一次演示里没有回放来假设。

## 动手实践：在业务提交后 ack 前故障，验证重投不重复增加余额

配套代码在 `/workspace/atlas/lessons/practice122/`，包括 `engine.py`、`app.py`、`test_messaging.py`，用 Python 3 标准库实现，不依赖 Redis、消息中间件或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice122
bash test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写出 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒，不是生产 RTT。`rpc_delay_s=0.01`。钱包初始余额 `100`，默认入账 `30`，键为 `article:01:title`，标题为 `Weekly digest`。

**1. 主实践：业务提交后、ack 前崩溃。** 时间线已在核心知识点第 1、2 节给出。不启用去重：`0.02` 余额到 `130`，崩溃后重投在 `0.03` 再次入账，`0.04` 才 ack，最终 `160`，`double_credited=true`。启用去重并把键与余额一起 flush：时间线相同，重投时 `second_duplicate=true`，最终 `130`，`exactly_once_wallet=true`，`committed_offset=1`，`log_length=1`。要验证的不是「消息没有重投」，而是「重投了，余额没有加第二次」。

**2. 先 ack 再处理会丢消息。** `ack_then_crash` 在处理之前把位置提交到 `1`，随后崩溃。恢复后 `repoll=None`，余额仍是 `100`，`message_lost=true`，`credit_missing=true`，结束于 `0.04`。这是文档中的 at-most-once 排法，不能拿来当作「更可靠的 ack」。

**3. outbox 对照「提交后再发送」。** 后者在本地余额到 `130` 之后、`produce` 之前崩溃，`log_length=0`，消息丢失。outbox 把 `ob-1` 和余额一起提交，`pending_after_commit=1`，`0.01` 排出后 `log_length=1`。`outbox_survives_crash_before_send=true`。

**4. 分区内有序、跨分区无总序；毒消息必须隔离。** `wallet:alice` 在分区 `1` 上按 `30` 再 `10` 的顺序被处理；`wallet:bob` 的 `5` 插在这两条之间，最终余额 `145`，结束于 `0.09`。毒消息不隔离时，offset `1` 被连续拒绝 5 次，后面的 +10 无法处理，余额停在 `130`，`head_of_line_blocked=true`。重试 3 次后写入教学死信日志并 ack，`good2` 生效，余额 `140`，`dlq_count=1`，`later_message_applied=true`。

**5. 外部副作用与权衡数字。** 去重保护了钱包（`130`），未纳入同一边界的推送被执行了 2 次。10 笔发布：outbox `flush_count=30`，提交后发送 `flush_count=20`，两边 `elapsed_s` 都是 `0.10`、`rpc_count` 都是 `10`；短窗口回放后余额 `160`，长窗口仍是 `130`。幂等生产对照：关闭时日志 2 条，打开时 1 条。

**6. 核对验收字段。** `practice_redelivery_does_not_double_credit=true`，`acceptance_exactly_once_scope_vs_external=true`，`acceptance_poison_isolated=true`，`pitfall_middleware_promise_is_not_universal=true`，`tradeoff_txn_cost_and_retain_window=true`。这些字段都在 `results.json` 顶层，测试脚本以 `ALL_CHECKS_PASSED` 结束。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Apache Kafka — Message Delivery Semantics 中与 Message delivery 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://kafka.apache.org/documentation/#semantics](https://kafka.apache.org/documentation/#semantics) 返回 200，**没有 HTTP 3xx 跳转**，20078 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Sun, 08 Nov 2026 16:53:55 GMT`，`<title>` 为 Documentation Redirect \| Apache Kafka。这是锚点映射用的 JavaScript 跳转页，正文是 Redirecting…，不能当作 Semantics 一节来引用。页内脚本把文档版本记为 `44`，映射 `"#semantics" → "design/design/#message-delivery-semantics"`。
- 同日打开实际正文 [https://kafka.apache.org/44/design/design/#message-delivery-semantics](https://kafka.apache.org/44/design/design/#message-delivery-semantics)，返回 200，无跳转，148030 字节，`<title>` 为 Design \| Apache Kafka，`og:url` 为 `https://kafka.apache.org/44/design/design/`，面包屑为 AK 4.4.X，`article:modified_time` 为 `2026-10-26T16:02:25+00:00`（北京时间 2026-10-27 00:02:25）。真正读到并对照实验的是 *Message Delivery Semantics*、*Using Transactions*、*Consumer Position*，以及 *The Producer* 里按 key 分区的段落。

论文式的规范在这里就是这份官方设计文档。教学实验按以下方式使用它，并记下差异。

- **三条语义**：文档给出 at-most-once、at-least-once、exactly-once，并区分发布持久性与消费位置更新。教学实验用「先 ack 再处理然后崩溃」对照至多一次（余额 `100`，消息丢失），用「先处理再 ack、ack 前崩溃」对照至少一次（无去重时余额 `160`）。
- **消费位置**：文档对比「broker 为每条消息保存已投递 / 已确认状态」与 Kafka「每个分区一个 offset，ack 很便宜，也允许 rewind」。教学引擎的 `committed[p]` 是下一条要读的整数；`rewind` 是为了演示保留窗口，不是生产里的日常操作。教学引擎没有 broker 侧的单条 ack 协议，也没有 ISR。
- **生产侧去重与事务**：文档写明 0.11.0.0 起可用幂等生产者（PID + 序号）避免日志重复，也可用事务把多分区写入做成原子，并把消费 offset 当作事务的一部分写回。教学引擎只有 `(producer_id, seq)` 的对照，没有 `transactional.id`，没有 `read_committed`，没有内部 offset 话题，也没有跨分区原子写。
- **exactly-once 的范围**：文档写过，许多 exactly-once 声明经不起失败场景的细读；Kafka 在 Streams、以及事务生产者加 `read_committed` 消费者、并且读写都落在 Kafka 话题上时，可以提供 exactly-once；写到其他目标系统则需要那些系统配合，否则默认是至少一次。教学实验把「配合」落实为钱包与去重表的同一次 flush，并用一次未协调的外部推送说明范围：钱包 `130`，推送 2 次。本课不实现 Kafka 事务，也不声称教学引擎提供了 Kafka 意义上的 exactly-once。
- **把位置存在输出旁边**：文档建议让消费者把 offset 存在输出所在的同一处，举例是 Kafka Connect 往 HDFS 写数据时同时写下 offset。教学去重表没有存储 Kafka offset，只存储业务幂等键；outbox 则是生产者侧的对称物，文档这一节并未展开 outbox 这个名称。
- **分区与顺序**：文档说按 key 哈希到分区，同一 key（例如 user id）进入同一分区，从而允许局部性假设；topic 是一组全序分区，不是全局全序。教学实验用 `sum(key.encode()) % n` 得到 Alice 分区 `1`、Bob 分区 `0`，并验证分区内有序、跨分区可交错。哈希函数与 Kafka murmur2 不同，不能把分区编号当作 Kafka 里的真实编号。
- **毒消息**：官方这一节没有规定死信队列的实现。教学里的 DLQ、`max_retries=3` 和队头阻塞是本课的验收对照，不应读成 Kafka 文档的行为。
- **时间与网络**：单进程仿真，`rpc_delay_s=0.01` 是教学延迟，不是生产 RTT。文档提到等待提交「大约 10 ms」只作为文档原文保留，不进入 `results.json`。`crash_consumer()` 是全知崩溃：易失位置被清空，已 flush 的余额、去重表、outbox 和日志保留。flush 不推进虚拟时钟，所以 10 笔 outbox 与 10 笔「提交后发送」的 `elapsed_s` 同为 `0.10`，协调成本要看 `flush_count`（30 对 20），不要把这两个耗时读成生产里的协调开销对比。

## 自测题

**题 1.** 为什么至少一次投递允许重复，又怎样验证「业务提交后、ack 前故障，重投不会重复增加余额」？请用无去重和有去重两条路径的时间线说明。

<details>
<summary>答案</summary>

至少一次的排法是：先处理，再保存消费位置。处理已经生效、位置还没保存时崩溃，恢复后会从旧位置再次 `poll` 到同一条消息。这不是实现缺陷，而是这种 ack 顺序在故障下的语义。

无去重路径把这一点暴露成重复入账。`0.01` 日志里写下一条 `+30`，`0.02` 余额从 `100` 变成 `130`，此时 `committed` 仍是 `0`。崩溃之后重投在 `0.03` 再次处理，余额变成 `160`，`0.04` 才 ack。`log_length` 始终是 `1`，重复发生在消费端，`double_credited=true`。

有去重路径并不阻止重投，而是把 `settlement:s1` 和余额放进同一次 flush。时间线相同，到 `0.02` 余额同样是 `130`，但去重表已经有这个键；`0.03` 的第二次 `apply` 得到 `second_duplicate=true`、`second_applied=false`，余额仍是 `130`，随后在 `0.04` ack，`exactly_once_wallet=true`。要验证的是「重投发生了，余额没有从 130 变成 160」，不是「消息没有再来」。如果先 ack 再处理，对照实验会走向另一面：位置已经是 `1`，恢复后 `repoll=None`，余额停在 `100`，消息丢失。

</details>

**题 2.** 怎样区分 exactly-once 处理范围与外部副作用？若把消息中间件的承诺扩大到所有外部系统，实验里会看到什么？

<details>
<summary>答案</summary>

范围要问两件事：哪一份状态和去重标记（或消费位置）写在同一可恢复边界里，以及边界之外还做了什么。文档的写法是：在 Kafka 话题之间，可以把 offset 和输出放进同一笔事务；写到其他系统则需要那些系统配合，否则默认仍是至少一次。

`side_effect_pitfall` 把配合只做在钱包上：重投后余额停在 `130`，`wallet_exactly_once=true`，第二次 `apply` 确实是重复。但同一次处理还调用了未纳入该边界的外部推送，`push_attempts=2`，`external_side_effect_duplicated=true`，结束于 `0.04`。钱包没有多加，推送加了一次。若把「中间件承诺了 exactly-once」理解成「所有下游都只会执行一次」，就会在已经去重成功的消费路径上漏掉第二次副作用。要让推送也恰好一次，必须把它和余额、去重标记放进同一个可恢复边界，或让对端同样按幂等键去重；中间件不会自动完成这件事。

</details>

**题 3.** 毒消息不隔离时为什么会挡住后续消息？事务消息的协调成本和幂等键的保留窗口，分别在哪些数字上体现？

<details>
<summary>答案</summary>

分区内必须按 offset 往前消费。毒消息占据 offset `1` 时，如果不 ack、也不把它移走，消费者就只能反复读这一条。不隔离的对照在该 offset 上连续拒绝 5 次，`committed` 停在 `1`，后面的 `good2`（+10）始终进不来，余额停在已经处理过的 `130`，`head_of_line_blocked=true`，`dlq_count=0`，结束于 `0.10`。隔离之后，重试 3 次就把它写入教学死信日志并 ack，`isolated_t=0.08`，随后 `good2` 生效，余额 `140`，`later_message_applied=true`。隔离换来的是分区能够继续，不是把坏消息变成好消息。

协调成本在本课的 10 笔对照里首先是 flush 次数：outbox 路径 `flush_count=30`，提交后发送是 `20`，`transactional_path_more_flushes=true`。两边的 `elapsed_s` 都是 `0.10`、`rpc_count` 都是 `10`，因为教学时钟只在 RPC 时前进；不能把这两个耗时读成生产里的快慢对比。少 flush 的那条路径在崩溃对照里会丢消息（余额已改、`log_length=0`）。保留窗口则体现在回放结果：`dedup_retain_s=0.05` 时，`0.02` 写入的键在 `0.10` 的回放中已经过期，余额变成 `160`；窗口 `1.0` 秒时同样间隔下仍识别为重复，余额保持 `130`。窗口太短，合法回放会再入账；窗口太长，去重记录要留得更久。

</details>

## 推荐资源

- [Apache Kafka — Message Delivery Semantics](https://kafka.apache.org/documentation/#semantics)（Apache Kafka 官方文档，英文，进阶，免费）：用于核对 ack 与重投、消费位置、以及 exactly-once 对外部系统需要配合这一条，并将结论落到本节点的重投与去重实验。2026-11-14 该地址返回 200，无 HTTP 跳转，但页面本身是 JavaScript 跳转页，20078 字节，`<title>` 为 Documentation Redirect \| Apache Kafka。同日实际阅读的正文是 [Design（AK 4.4）](https://kafka.apache.org/44/design/design/#message-delivery-semantics)，200，无跳转，148030 字节，`<title>` 为 Design \| Apache Kafka，`dateModified` 为 `2026-10-26T16:02:25+00:00`。文档讨论的是 Kafka 日志、ISR、幂等生产者和事务；outbox、去重表保留窗口和毒消息隔离是本课的对照，不是该页规定的实现。

## 本课小结

- 至少一次投递允许重复。先处理再 ack，业务提交后、ack 前崩溃，同一条 `+30` 会被再处理一次。不去重时余额从 `100` 经 `130` 变成 `160`；把 `settlement:s1` 与余额一起 flush 之后，重投被识别为重复，余额停在 `130`，`exactly_once_wallet=true`。
- 先 ack 再处理是至多一次：位置已经提交到 `1`，崩溃后消息不再出现，余额停在 `100`，`message_lost=true`。ack 提交的是消费位置，不是业务本身。
- 可靠消费必须把业务状态和去重标记纳入同一可恢复边界。outbox 把本地提交和待发消息一起 flush，崩溃后仍能在 `0.01` 排出；提交后再发送、发送前崩溃，余额已是 `130`，日志却是空的。
- exactly-once 有范围。去重可以保护钱包（`130`），未纳入同一边界的外部推送仍会被执行 2 次。不要把中间件的承诺扩大到所有外部系统。
- 同一 key 进入同一分区，分区内有序；跨分区没有全局顺序。Alice 的 `30` 在 `10` 之前，Bob 的 `5` 可以插在中间。毒消息不隔离会挡住后续的 `good2`；重试 3 次后写入教学死信日志并 ack，余额才能从 `130` 走到 `140`。
- 事务消息更贵，幂等键需要保留窗口。10 笔发布，outbox 的 `flush_count` 是 `30`，提交后发送是 `20`，两边的教学耗时都是 `0.10` 秒。窗口 `0.05` 秒后回放会把余额加成 `160`，窗口 `1.0` 秒则仍识别为重复。这些是教学虚拟时钟上的数字，不是生产 RTT。
- `MessageDeliveryEngine` 不是 Kafka，也不是真实 broker。它没有 ISR、没有 `transactional.id`、没有 murmur2，分区函数和幂等生产都是教学对照；毒消息隔离和 outbox 也不是文档这一节规定的实现。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到官方文档里「先处理再保存位置 / 先保存位置再处理」以及「写到外部系统需要配合」这两段，不要把「日志里只有一条」当成「业务只生效一次」，也不要把「中间件声称 exactly-once」理解成「所有副作用都恰好一次」。
