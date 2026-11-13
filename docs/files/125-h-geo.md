# 第 125/208 课：多机房与灾难恢复

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-geo` |
| 英文名 | Multi-region recovery |
| 领域 / 阶段 | H / 第 5 阶段 |
| 子类 | 可靠性与演进 |
| 难度 | 专家 |
| 前置节点 | [`h-raft-log`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html)、[`h-shard`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/120.html) |
| 标签 | `Multi-region recovery`、`Distributed` |
| 参考资料 | [Google SRE — Reliable Product Launches](https://sre.google/sre-book/reliable-product-launches/)（Google SRE 书籍第 27 章，英文，专家，免费；2026-11-14 该地址返回 HTTP 200，无跳转，最终 URL 仍是 `https://sre.google/sre-book/reliable-product-launches/`，92971 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Thu, 08 Oct 2026 04:48:00 GMT`（北京时间 2026-10-08 12:48:00）。本次抓取响应头 `Date` 为 `Fri, 13 Nov 2026 17:08:21 GMT`（北京时间 2026-11-14 01:08:21）。`<title>` 为 Google SRE - Deployment Strategies for Product Launches，与章名不完全一致：页内 `h2.chapter-title` 为 Chapter 27 - Reliable Product Launches at Scale，正文标题为 Reliable Product Launches at Scale。作者为 Rhandeev Singh 与 Sebastian Kirsch，并有 Vivek Rau 参与，由 Betsy Beyer 编辑。页脚版权为 Copyright © 2017 Google, Inc. Published by O'Reilly Media, Inc.，许可为 [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/)。canonical 为 `https://sre.google/sre-book/reliable-product-launches/`。该章是发布清单（launch checklist）章节，正文与本课直接相关的是 *The Launch Checklist* 中「是否存储持久数据 / 必须实现备份」、*Client Behavior* 里把「把服务迁到另一机房、从备份恢复」列为需要文档化的人工流程、*Capacity Planning* 中「若 3 套复制部署才能服务 100% 峰值则应保留 4 或 5 套」、以及 *Failure Modes* 中对单机、机房中断和网络故障的检查。该章没有定义 RPO、RTO、fencing，也没有定义同步与异步复制。章内点名的清单正文是附录 E，同日打开 [https://sre.google/sre-book/launch-checklist/](https://sre.google/sre-book/launch-checklist/) 返回 HTTP 200，无跳转，40937 字节，同样的 `Last-Modified`，`<title>` 为 Google SRE - Google checklist: SRE pre launch checklist，正文标题为 Appendix E - Launch Coordination Checklist，版权同为 2017 Google, Inc. / O'Reilly / CC BY-NC-ND 4.0，没有单独作者署名。附录中与本课相关的条目是 Machines and datacenters（含 N+2 redundancy）、System reliability and failover（What happens when: Machine dies, rack fails, or cluster goes offline; Network fails between two datacenters）以及 Data backup/restore, disaster recovery。附录同样没有定义 RPO、RTO、fencing 或同步与异步复制） |
| 摘要 | 跨机房设计需定义 RPO、RTO、写入主权和切换流程；异地复制不自动保证零丢失。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题家族 `Weekly digest` / `Weekly digest (revised)` / `Weekly digest (hotfix)`」「机房 `dc-east` / `dc-west`」都是**教学假设**，用来让同步与异步复制的写延迟、丢失窗口、断网时的写入主权、主备切换 runbook 和备份恢复演练变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `GeoKVEngine`，**不是 Redis，不是真实的多机房产品，不是网络服务，不是 Spanner，也不是 Google 生产环境里的复制、流量调度或备份系统**。它是单进程离散事件仿真，没有 TCP，没有 WAN，也没有磁盘。持久化是教学用 dict flush，不是 `fsync`。`Clock` 是虚拟时钟。`local_write_s=0.05`、`cross_dc_s=0.20` 和 `runbook_step_s=0.05` 都是教学延迟，不是生产 RTT。RPO、RTO、fencing、同步与异步复制这些名字，以及测到的窗口，来自本课教学模型和节点用语；第 27 章与附录 E 要求实现备份、写清迁机房和恢复步骤、检查机房中断与机房间网络故障，但并不定义这些公式。本课所有时间都来自 `/workspace/atlas/lessons/practice125/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `CircuitIsolateEngine` 解决的是「同一个前端同时依赖两个下游」：错误窗口填满后打开熔断、半开探测必须有界，独立连接池才能在注入慢依赖后让另一个下游不受拖累，降级结果还要带上新鲜度。那一课的边界停在**一次调用链里的隔离与降级**，没有回答另一件更大的故障：整机房不可用、两个机房之间的网络断开，或者主库所在的机房掉线之后，数据会丢多少、要多久才能重新可写、断网期间到底哪个机房允许写入。

熔断可以挡住对一个坏依赖的继续调用，但不能替跨机房系统决定「谁还拥有写入主权」；降级缓存也回答不了「已确认写入会不会在切换后消失」。节点把摘要写成：跨机房设计需定义 RPO、RTO、写入主权和切换流程；异地复制不自动保证零丢失。

### 接上前置

前置 [第 118 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html) `h-raft-log` 留下的结论是：日志复制与提交要以法定人数为准，旧领导者在分区之后不能继续当成可写的权威。本课把这条结论搬到两个教学机房上：切换必须先给旧主库加上 fencing（并把它移出副本集），再提升新主库；只提升备库、不剥夺旧主库的写入权，就会出现双主分叉。前置 [第 120 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/120.html) `h-shard` 给出的是单集群里的分片与在线迁移。本课不再增加分片，而是在已经对好的两份标题副本上，观察跨机房复制方式和切换流程怎样决定丢失窗口。

第 27 章把「把服务迁到新机房」和「从备份恢复」列为必须写进文档、任何一个同事在紧急情况下都能执行的人工流程；附录 E 则要求事先回答机房掉线、两个机房之间网络断开时会发生什么，以及是否做了 Data backup/restore, disaster recovery。本课的 runbook 和备份演练就是把这两项清单落到可测量的步骤上，而不是复现 Google 的发布流程。

### 教学场景（教学假设）

假设内容后台把文章 `article:01:title` 放在两个教学机房：`dc-east` 是主库，`dc-west` 是备库，两边在 `t=0.00` 已经对齐为 `Weekly digest`（版本 1），纪元为 1。本地确认写入花费 `local_write_s=0.05`，跨机房复制花费 `cross_dc_s=0.20`，runbook 每一步花费 `runbook_step_s=0.05`。异步路径在本地确认后即可应答，复制在应答后再走 `0.20`；同步路径要等备库也应用成功才应答，时延是 `0.25`。主库崩溃后，runbook 依次做检测、封锁旧主库、把它移出副本集、提升备库，并测量 RTO（故障到新主库可写）和 RPO（已确认但新主库没有的窗口）。所有时间都是教学虚拟时钟上的值，不是线上往返时延，也不是生产 RTT。

### 本课三条主线

1. **同步与异步复制**：异步写得快，但已确认的标题不一定已经到达备库；同步写得慢，但在本课模型里崩溃后 RPO 为 0。适用边界取决于能否接受潜在丢失，以及能否接受每次写入都跨机房等待。
2. **fencing 与切换**：切换是一套有序步骤，不是「备库起来就算完成」。封锁旧主库并把它移出副本集之后，新主库才独占写入权；只提升、不封锁，两个机房会写入不同的标题。
3. **备份恢复演练**：有一份备份对象不等于已经恢复。必须先恢复再校验校验和与标题；从未跑过恢复的备份，在仓库被清空后校验会失败。

## 学习目标

- 能解释同步与异步复制及适用边界：异步确认只代表本地已落盘（教学 dict flush），同步确认代表所需副本已经应用；跨地域同步增加写延迟，异步复制增加潜在 RPO。
- 独立完成实践：为教学 KV 写主备切换 runbook，测量恢复时间与丢失窗口。能说出 RTO、RPO 是怎样从虚拟时钟上算出来的，以及切换后旧主库为什么不能再写。
- 验收上要能明确断网时哪个机房可写，并且从备份恢复后校验数据。

## 为什么后端要懂

节点给出的理由是：**让重大故障下的恢复目标可执行**。机房故障不是「再试一次调用」能解决的。如果事先没有定义谁可写、能丢多少、多久必须恢复，以及切换和恢复的步骤是否真的跑通，异地复制只会给人一种「另一边应该有数据」的错觉。本课用同一组标题在两种复制方式和四种断网策略下把这些目标写成可核对的数字，就是为了让「可执行」不再停留在口头上。

## 核心知识点

### 1. 同步与异步复制

两种路径从同一起点出发：`t=0.00` 两侧都是 `Weekly digest`。随后写入 `Weekly digest (revised)`，再写入 `Weekly digest (hotfix)`，然后让 `dc-east` 崩溃，跑同一份 runbook。

异步路径里，revised 在 `t=0.30` 开始写，`0.35` 应答，时延 `0.05`；hotfix 在 `0.40` 开始，`0.45` 应答，时延同样是 `0.05`。hotfix 的复制本应在 `0.65` 到达西侧，但东侧在 `0.60` 崩溃，教学引擎会丢弃来自已崩溃主库的在途复制。西侧因此只保住了 `0.35` 已到达的 revised。runbook 从 `0.60` 走到 `0.80` 才让西侧可写，RTO 为 `0.20`，RPO 为 `0.10`（最后失去的 hotfix 应答时刻 `0.45` 减去最后保住的 revised 应答时刻 `0.35`），丢失 1 条：版本 3 的 `Weekly digest (hotfix)`。新主库上的标题是 `Weekly digest (revised)`。

同步路径里，每次写入都要等 `0.05 + 0.20 = 0.25`。revised 在 `0.30` 开始、`0.55` 应答；hotfix 在 `0.55` 开始、`0.80` 应答。东侧在 `0.85` 崩溃时，hotfix 已经在西侧落稳。runbook 从 `0.85` 走到 `1.05`，RTO 同样是 `0.20`，RPO 为 `0`，丢失 0 条，新主库标题是 `Weekly digest (hotfix)`。

这就是适用边界：异步把 2 次写入的累计时间压到 `0.45`，但切换后会丢掉已经应答的 hotfix；同步把同两次写入拉到 `0.80`，崩溃后标题还在。异地复制本身并不自动等于零丢失，丢失与否取决于应答时复制是否已经完成。

### 2. fencing 与切换

runbook 不是「把备库改成 primary」这一步，而是四步，每步 `0.05`：

| 步骤 | 异步路径时刻 | 同步路径时刻 | 当时可写机房 |
|---|---|---|---|
| `detect_primary_failure` | `0.65` | `0.90` | 无 |
| `fence_old_primary` | `0.70` | `0.95` | 无 |
| `drop_old_primary_from_replica_set` | `0.75` | `1.00` | 无 |
| `promote_standby` | `0.80` | `1.05` | `dc-west` |

RTO 就是 `t_writable - t_fail`，两条路径都是 `0.20`。提升后纪元变成 2。随后一次切换后写入 `Weekly digest (after-failover)`，西侧都能应答：异步在 `0.80` 开始、`0.85` 应答，时延 `0.05`；同步因为旧主库已被移出副本集，不再等待已经倒下的东侧，时延同样是 `0.05`（`1.05` 到 `1.10`），版本为 4。这是切换完成后的暂时单机房持久化，不是「同步永远要付 `0.25`」。旧东侧此时已崩溃，再写会被拒绝，原因是 `dc_down`（引擎先判断机房是否存活，再判断是否被封锁）。要看到纯粹的 `fenced` 拒绝，需要旧主库还活着的断网场景。

断网时哪个机房可写，必须按策略说清楚，不能假设「主库所在的一边一定能写完」：

| 策略 | 可发起写入的机房 | 能完成确认的机房 | 东侧结果 | 西侧结果 | 两边标题 |
|---|---|---|---|---|---|
| 异步、不切换 | 只有 `dc-east` | 只有 `dc-east` | `0.25` 开始，`0.30` 应答 `Weekly digest (east)` | `not_primary` | 东 `Weekly digest (east)`，西仍为 `Weekly digest (revised)` |
| 同步、不切换 | 预检仍列出 `dc-east` | 谁都不能确认 | 东侧接受尝试但回滚，原因 `sync_replica_unreachable`，标题仍是 revised | `not_primary` | 两边都还是 `Weekly digest (revised)` |
| 错误地双主（不封锁、不提升纪元） | `dc-east` 与 `dc-west` | 两边都能确认 | 东侧写成 `Weekly digest (east)` | 西侧写成 `Weekly digest (west)` | 标题分叉 |
| 先封锁再提升 | 只有 `dc-west` | 只有 `dc-west` | 活着的旧主库被拒绝，原因 `fenced` | `0.45` 开始，`0.50` 应答 `Weekly digest (west)` | 东仍为 revised，西为 west |

「明确断网时哪个机房可写」指的就是这张表。异步且不切换时，只有原主库可写；同步且不切换时，没有机房能得到持久确认；正确的 fencing 把主权交给新主库，并且不让还活着的旧主库继续写。

### 3. 备份恢复演练

备份演练从已经同步的 `Weekly digest (revised)` 开始。`t=0.25` 在东侧拍下一份备份，校验和为 `9c3632257ba5e3fa`。之后写入 `Weekly digest (hotfix)`，再清空东侧仓库。这时标题变成空，备份对象还在，但仓库里已经没有 revised，也没有后来的 hotfix。

验证过的路径在 `t=0.60` 执行恢复，`t=0.65` 做校验：期望与实际的校验和都是 `9c3632257ba5e3fa`，标题都是 `Weekly digest (revised)`，`verify_ok` 为真。备份不会自动带上备份之后才写进去的 hotfix，这是正确的，说明恢复回到的是拍摄点，不是崩溃前的最新值。

对照路径同样拥有这份备份（`has_backup=true`），也清空了仓库，但把恢复这一步跳过，直接校验。`t=0.40` 时实际校验和是 `e3b0c44298fc1c14`（空仓库的教学校验和），实际标题为空，`verify_ok` 为假。有备份但从未验证能恢复，就是这样：备份对象在，数据不在。

## 常见误区

第一个误区是把「已经做了异地复制」当成「零丢失」。异步路径里 hotfix 已在 `0.45` 应答，编辑会以为标题已经安全；东侧在 `0.60` 崩溃后，西侧只有 revised，RPO 是 `0.10`。十条连续写入、应答后立刻崩溃的对照里，这个窗口会放大到 `0.20`，丢掉 4 条已确认标题。复制在路上，不等于已经到达。

第二个误区是提升备库却不封锁旧主库。错误的双主路径里，两个机房都接受写入，标题变成东侧 `Weekly digest (east)`、西侧 `Weekly digest (west)`。先封锁再提升时，活着的东侧得到的是 `fenced`，只有西侧可写。切换流程必须包含 fencing，而不能只改角色名。

第三个误区是节点写明的那句：**有备份但从未验证能恢复。** 对照路径里 `has_backup` 为真、`restore_ran` 为假，校验失败。备份文件在手，不能代替一次真正的恢复加校验。

## 设计权衡

节点给出的权衡是：**跨地域同步增加写延迟，异步复制增加潜在 RPO。** 十条连续标题写入、最后一次应答后立即让主库崩溃，数字如下。

| 方式 | 单次确认时延 | 十条累计 | 崩溃时刻 | RTO | RPO | 丢失条数 | 新主库标题 | 新主库版本 / 旧主库最后确认版本 |
|---|---|---|---|---|---|---|---|---|
| 异步 | `0.05` | `0.50` | `0.50` | `0.20` | `0.20` | 4（`w7`–`w10`，版本 8–11） | `Weekly digest (w6)` | 7 / 11 |
| 同步 | `0.25` | `2.50` | `2.50` | `0.20` | `0` | 0 | `Weekly digest (w10)` | 11 / 11 |

异步把十条写入做完只要 `0.50`，但跨机房复制还要再走 `0.20`，崩溃来得比在途复制更早，西侧只保住到 `w6`。同步每次都等到西侧应用，累计 `2.50`，RPO 为 0。RTO 在两种方式里都是四步 runbook 的 `0.20`，它衡量的是切换流程，不是复制时延。选哪一种，取决于能接受多慢的写入，以及能接受多大的丢失窗口；本课不把其中一边写成唯一正确答案。

## 动手实践：为教学 KV 写主备切换 runbook，测量恢复时间与丢失窗口

配套代码在 [`/workspace/atlas/lessons/practice125/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice125.tar.gz)。`engine.py` 是 `GeoKVEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_geo.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。

主实验按「先写 revised 与 hotfix，再让东侧崩溃，跑 runbook，再打一笔切换后写入」来测量窗口：

| 路径 | 两次写入时延 | 故障时刻 | 新主库可写时刻 | RTO | RPO | 丢失 | 切换后标题（runbook 当时） | 切换后再写 |
|---|---|---|---|---|---|---|---|---|
| 异步 | `0.05` / `0.05` | `0.60` | `0.80` | `0.20` | `0.10` | hotfix 1 条 | `Weekly digest (revised)` | `0.85` 应答，版本 3 |
| 同步 | `0.25` / `0.25` | `0.85` | `1.05` | `0.20` | `0` | 0 | `Weekly digest (hotfix)` | `1.10` 应答，时延 `0.05`，版本 4 |

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_writable_dc_during_partition`、`acceptance_restore_then_verify`、`practice_runbook_measures_rto_and_rpo`、`pitfall_backup_never_verified`、`tradeoff_sync_latency_vs_async_rpo`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_multi_dc_product`、`not_spanner`、`not_google_sre_production`。

实践时请同时看断网四策略和备份对照，不要只盯着 RTO / RPO 两个数字。同步且不切换时预检仍会列出东侧，但确认会失败并回滚——「可写」要以是否确认成功为准。备份对照里，跳过恢复后实际校验和是 `e3b0c44298fc1c14`，与备份里的 `9c3632257ba5e3fa` 对不上。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Google SRE — Reliable Product Launches 中与 Multi-region recovery 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://sre.google/sre-book/reliable-product-launches/](https://sre.google/sre-book/reliable-product-launches/) 返回 200，无跳转，92971 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Thu, 08 Oct 2026 04:48:00 GMT`（北京时间 2026-10-08 12:48:00），响应头 `Date` 为 `Fri, 13 Nov 2026 17:08:21 GMT`（北京时间 2026-11-14 01:08:21）。`<title>` 为 Google SRE - Deployment Strategies for Product Launches，正文标题是 Chapter 27 - Reliable Product Launches at Scale。作者为 Rhandeev Singh 与 Sebastian Kirsch，并有 Vivek Rau 参与，由 Betsy Beyer 编辑，版权 2017 Google, Inc.，由 O'Reilly Media 出版，许可为 CC BY-NC-ND 4.0。这一页是章节正文，主题是大规模发布的清单与发布协调，**不是**一本灾难恢复手册，也没有再把读者送到第 26 章 *Data Integrity*。本课不把第 26 章当成节点指定的官方来源。
- 章内把具体清单指向 Launch Coordination Checklist。同日跟随打开 [https://sre.google/sre-book/launch-checklist/](https://sre.google/sre-book/launch-checklist/) 返回 200，无跳转，40937 字节，同样的 `Last-Modified`。`<title>` 为 Google SRE - Google checklist: SRE pre launch checklist，正文标题是 Appendix E - Launch Coordination Checklist，版权相同，没有单独作者署名。

教学实验按以下方式使用这两页，并记下差异。

- **术语**：第 27 章与附录 E **没有**定义 RPO、RTO、fencing，也没有讨论同步与异步复制。这些名字和测量公式来自本课教学模型与节点用语，不要写成书中的正式接口。清单真正写下的，是实现备份、把「迁到新机房」和「从备份恢复」写成紧急情况下可执行的文档、检查机房中断与两个机房之间的网络故障，以及 Data backup/restore, disaster recovery。
- **备份与迁机房**：章内对持久数据的 action item 是 “Make sure you implement backups.”；在需要由人执行的流程里，明确举了 moving the service to a different data center 和 restoring data from backups，并要求 “Document the process for moving your service to a new datacenter.” 教学 runbook 和 `backup` / `restore` / `verify` 是这两项清单的对照，步骤时长是 `0.05` 的教学节拍，不是 Google 的变更窗口或真实恢复耗时。
- **故障模式与机房间网络**：Failure Modes 一节要求系统检查单机故障、机房中断和网络故障；附录 E 把 “Network fails between two datacenters” 和 “Data backup/restore, disaster recovery” 写成必须事先回答的条目。本课的 `partition`、四种写入主权策略和备份对照，就是把这两个问题变成可观察的标题，而不是在真实 WAN 上注入故障。
- **容量与冗余**：Capacity Planning 写道，若需要三套复制部署才能在峰值服务 100% 流量，应维持四套或五套，以便遮住维护和意外故障；附录 E 也列出 N+2 redundancy。教学引擎只有两个机房、一份主一份备，没有实现 N+2，也没有把 4、5 套部署写进 `results.json`。不要把本课的双机房模型说成书中的容量公式。
- **未实现的章节内容**：发布协调工程师（LCE）流程、域名与配额、架构评审、按机房逐步放量、负载测试，以及 Borg、GSLB 或其他 Google 生产系统，教学引擎都没有做，它们的数字不会进入 `results.json`。
- **网络与时间**：单进程离散事件，所有时延都是教学虚拟时钟上的值，不是生产 RTT。在途的异步复制在主库崩溃时被丢弃，这是本课偏保守的 RPO 记法，不是某种生产复制协议的保证。同步在封锁并移出旧主库之后，只对仍在副本集内的成员等待，切换后的那一次写入时延是 `0.05`，表示暂时降为单机房持久化，直到旧机房重新加入——书中没有规定这个实现。

## 自测题

**题 1.** 两个教学机房之间断网时，哪个机房可以写入？请分别说明异步不切换、同步不切换、错误双主，以及先封锁再提升这四种策略，并指出「预检列出的可写机房」和「真正确认成功」可能不是一回事。

<details>
<summary>答案</summary>

要按策略回答，不能笼统地说「主库所在的机房可写」。

异步且不切换时，只有原主库 `dc-east` 可写：它在 `0.25` 开始写入 `Weekly digest (east)`，`0.30` 应答；西侧因 `not_primary` 被拒绝，标题仍是断网前已经同步过来的 `Weekly digest (revised)`。

同步且不切换时，预检仍会列出 `dc-east`，因为它的角色还是 primary。但东侧无法把写入同步到西侧，原因是 `sync_replica_unreachable`，本地未确认的写入会被回滚，标题仍是 revised；西侧同样是 `not_primary`。这一策略下没有机房能得到持久确认。「可写」如果只看预检名单会误导，验收要以是否确认成功为准。

错误地同时把西侧也当成主库、又不提升纪元、不封锁东侧时，两边都接受写入，东侧变成 `Weekly digest (east)`，西侧变成 `Weekly digest (west)`，标题分叉。这正是切换流程缺少 fencing 的后果。

先封锁东侧再提升西侧时，可写机房只剩 `dc-west`。还活着的旧主库再写，原因是 `fenced`；西侧在 `0.45` 开始、`0.50` 应答 `Weekly digest (west)`。写入主权在这里是被 runbook 明确交出去的，不是断网之后自动出现的。

</details>

**题 2.** 怎样为教学 KV 测量主备切换的恢复时间和丢失窗口？请用 revised / hotfix 这条故事线里的 RTO、RPO，以及十条写入对照里异步多出来的丢失，说明同步和异步的适用边界。

<details>
<summary>答案</summary>

恢复时间 RTO 是 runbook 里 `t_writable - t_fail`。故事线里两种复制都要走检测、封锁、移出副本集、提升这四步，每步 `0.05`，所以异步是 `0.80 - 0.60 = 0.20`，同步是 `1.05 - 0.85 = 0.20`。RTO 衡量的是切换流程，不是跨机房复制时延。

丢失窗口 RPO 是「已确认但新主库没有」的时间宽度。异步路径里 hotfix 在 `0.45` 已经应答，但它的复制要到 `0.65` 才到达西侧，东侧在 `0.60` 崩溃，在途复制被丢弃，西侧只保住 `0.35` 应答的 revised，RPO 为 `0.10`，丢失 1 条 hotfix。同步路径里 hotfix 在 `0.80` 应答时西侧已经有副本，`0.85` 崩溃后 RPO 为 `0`，丢失 0 条，新主库标题是 `Weekly digest (hotfix)`。

把窗口放到十条连续写入、最后一次应答后立刻崩溃的对照里，差异更清楚。异步单次时延 `0.05`、累计 `0.50`，但 RPO 变成 `0.20`（正好等于 `cross_dc_s`），西侧只保住 `Weekly digest (w6)`（版本 7），丢掉版本 8–11 共 4 条。同步单次时延 `0.25`、累计 `2.50`，RPO 为 0，新主库是 `Weekly digest (w10)`（版本 11）。跨地域同步增加写延迟，异步复制增加潜在 RPO；异地复制不自动保证零丢失。切换之后新主库必须能写：西侧在异步 `0.85`、同步 `1.10` 都确认了 `Weekly digest (after-failover)`，同步这一笔时延是 `0.05`，因为旧主库已不在副本集里。

</details>

**题 3.** 为什么从备份恢复之后还要校验数据？若只保留备份对象、从未执行恢复，校验会看到什么？这份备份里有没有后来写入的 hotfix？

<details>
<summary>答案</summary>

验收要求的是「从备份恢复后校验数据」，不是「系统里有一份备份记录」。验证路径在 `t=0.25` 拍摄 revised，校验和为 `9c3632257ba5e3fa`；后来写入的 hotfix 不在这份备份里。清空仓库后标题变为空，在 `t=0.60` 恢复、`t=0.65` 校验，期望与实际的校验和一致，标题回到 `Weekly digest (revised)`，`verify_ok` 为真。校验通过，才说明恢复真的把拍摄点的数据带了回来。

对照路径同样有这份备份（`has_backup=true`），同样清空了仓库，但 `restore_ran=false`。直接校验时，实际校验和是空仓库的 `e3b0c44298fc1c14`，实际标题为空，`verify_ok` 为假。这就是「有备份但从未验证能恢复」：备份对象不能代替恢复步骤。

备份也不会包含拍摄之后才确认的 hotfix。恢复成功后看到的是 revised，不是 hotfix。若把「有备份」理解成「能回到崩溃前的最新标题」，就会在演练里得到错误的预期。节点把这一条写成误区，正是因为备份只有在被恢复并校验之后，才是可执行的恢复目标。

</details>

## 推荐资源

- [Google SRE — Reliable Product Launches](https://sre.google/sre-book/reliable-product-launches/)（Google SRE 第 27 章，英文，专家，免费）：用于核对本课涉及的备份、迁机房文档化和机房 / 网络故障检查，并将结论落到本节点的复制方式、切换 runbook 与备份演练。2026-11-14 该地址返回 200，无跳转，92971 字节，`<title>` 为 Google SRE - Deployment Strategies for Product Launches，正文标题为 Chapter 27 - Reliable Product Launches at Scale，`Last-Modified` 为 `Thu, 08 Oct 2026 04:48:00 GMT`，作者为 Rhandeev Singh 与 Sebastian Kirsch，并有 Vivek Rau 参与，由 Betsy Beyer 编辑。该章是发布清单，并不定义 RPO、RTO、fencing 或同步与异步复制。同日跟随打开的 [Appendix E - Launch Coordination Checklist](https://sre.google/sre-book/launch-checklist/) 返回 200，无跳转，40937 字节，`<title>` 为 Google SRE - Google checklist: SRE pre launch checklist，列出了 N+2 redundancy、两个机房之间网络故障，以及 Data backup/restore, disaster recovery，同样没有给出这些公式。本课的教学窗口、fencing 记号和双机房模型是对照，不是书中的生产实现；N+2、按机房放量和 LCE 流程本课没有实现。

## 本课小结

- 跨机房设计要先定义 RPO、RTO、写入主权和切换流程。异地复制不自动保证零丢失：异步故事线里已确认的 hotfix 在切换后消失，RPO 为 `0.10`；同步故事线 RPO 为 `0`，但两次写入要付 `0.25` 的确认时延。
- 切换 runbook 按检测、封锁旧主库、移出副本集、提升备库四步执行，RTO 为 `0.20`。切换后西侧可写，崩溃的东侧再写会得到 `dc_down`；旧主库仍活着时，正确封锁给出的拒绝原因是 `fenced`。
- 断网时哪个机房可写取决于策略。异步不切换时只有东侧确认成功；同步不切换时谁都不能确认；错误双主让标题分叉为 `Weekly digest (east)` 与 `Weekly digest (west)`；先封锁再提升则只有西侧可写。
- 十条写入的权衡把这句话变成数字：异步累计 `0.50`、RPO `0.20`、丢掉 4 条；同步累计 `2.50`、RPO `0`。跨地域同步增加写延迟，异步复制增加潜在 RPO。
- 有备份不等于能恢复。拍摄点校验和 `9c3632257ba5e3fa` 在恢复后校验通过，标题回到 `Weekly digest (revised)`；跳过恢复则实际校验和是 `e3b0c44298fc1c14`，校验失败。备份也不包含拍摄之后才写入的 hotfix。
- 这些是教学虚拟时钟上的数字，不是生产 RTT。`GeoKVEngine` 不是 Redis，也不是真实的多机房产品或 Google 的生产实现。第 27 章与附录 E 没有定义 RPO / RTO / fencing / 同步与异步复制；教学模型用来落实清单里的备份、迁机房文档和机房故障检查，恢复目标仍然要靠演练才可执行。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「必须实现备份」「把迁到新机房和从备份恢复写成可执行步骤」以及「两个机房之间网络断开时会发生什么」这几项，不要把「已经复制到异地」当成「零丢失」，也不要把「手里有一份备份」当成「已经验证能恢复」。
