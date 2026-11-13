# 第 126/208 课：分布式历史与故障测试

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-verification` |
| 英文名 | Jepsen testing |
| 领域 / 阶段 | H / 第 5 阶段 |
| 子类 | 可靠性与演进 |
| 难度 | 专家 |
| 前置节点 | [`h-consistency`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html)、[`h-raft-log`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html)、[`g-recovery`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/111.html) |
| 标签 | `Jepsen testing`、`Distributed` |
| 参考资料 | [Jepsen — Fault injection testing](https://github.com/jepsen-io/jepsen)（Jepsen 仓库，英文，专家，免费；2026-11-14 该地址返回 HTTP 200，无跳转，最终 URL 仍是 `https://github.com/jepsen-io/jepsen`，294864 字节，`Content-Type` 为 `text/html; charset=utf-8`。本次抓取响应头 `Date` 为 `Fri, 13 Nov 2026 17:17:12 GMT`（北京时间 2026-11-14 01:17:12）。页面没有 `Last-Modified`。`<title>` 与 `og:title` 为 GitHub - jepsen-io/jepsen: A framework for distributed systems verification, with fault injection，`og:description` 为 A framework for distributed systems verification, with fault injection - jepsen-io/jepsen。仓库所有者为 jepsen-io，这些页面没有署个人作者。同日打开的仓库元数据 `https://api.github.com/repos/jepsen-io/jepsen` 返回 200，6652 字节，`Last-Modified` 为 `Thu, 12 Nov 2026 22:13:07 GMT`（北京时间 2026-11-13 06:13:07），`pushed_at` 为 `2026-11-06T23:52:13Z`（北京时间 2026-11-07 07:52:13），`updated_at` 与上述 `Last-Modified` 一致，默认分支 `main`，`license` 字段为 `null`。仓库根目录 `LICENSE` 与 `license` 同日返回 404。同日读取的 [`jepsen/project.clj`](https://raw.githubusercontent.com/jepsen-io/jepsen/main/jepsen/project.clj) 返回 200、3294 字节，写明 `:description` 为 Distributed systems testing framework.，`:url` 为 `https://jepsen.io`，`:license` 为 Eclipse Public License（`http://www.eclipse.org/legal/epl-v10.html`），版本 `0.3.16-SNAPSHOT`。页面展示的 README 原文同日在 [https://raw.githubusercontent.com/jepsen-io/jepsen/main/README.md](https://raw.githubusercontent.com/jepsen-io/jepsen/main/README.md) 返回 200，无跳转，13500 字节；其中 *Design Overview* 把一次测试写成：在控制节点上对 DB 节点部署系统，由进程执行操作并把每次操作的开始与结束记进 *history*，同时由 *nemesis* 注入故障，最后用 *checker* 分析历史是否说得通。README 点名的 [tutorial 目录](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/tutorial/index.md) 同日 200、754 字节；本课实际读过并对照的是其中与节点三条主线直接相关的 [03 Writing a Client](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/tutorial/03-client.md)（200，17111 字节）、[04 Checking Correctness](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/tutorial/04-checker.md)（200，7703 字节）和 [05 Introducing Faults](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/tutorial/05-nemesis.md)（200，7278 字节），以及概述命名空间的 [What's Here](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/whats-here.md)（200，9127 字节）。教程把完成记录定义为 `:ok`（成功）、`:fail`（确定没发生）或 `:info`（不确定），并说明超时或抛出异常应变成 `:info`；checker 配合模型（教程里用 Knossos 的线性化寄存器）判断历史是否合法，nemesis 负责在生成器调度下注入故障，失败时可打开 `store/latest/history.txt` 查看反例） |
| 摘要 | 通过生成并发操作历史、注入故障并检查模型，寻找成功路径测试难以暴露的一致性错误。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题家族 `Weekly digest` / `Weekly digest (revised)` / `Weekly digest (hotfix)`」「节点 `n1` / `n2` / `n3`」都是**教学假设**，用来让 invoke / ok / fail / info 历史、超时的不确定结果、故障注入和最小反例变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `VerificationEngine`，**不是 Redis，不是真实的 Jepsen 产品或 Clojure 库，不是网络服务，不是 Knossos 或 Elle，也不是 etcd**。它是单进程离散事件仿真，没有 SSH，没有 TCP，也没有 WAN。持久化是教学用 dict flush，不是 `fsync`。`Clock` 是虚拟时钟。`local_s=0.05`、`quorum_s=0.10` 和 `timeout_s=0.20` 都是教学延迟，不是生产 RTT。历史条目用 JSON 字符串 `invoke` / `ok` / `fail` / `info` 记录，对应教程里的 `:invoke` / `:ok` / `:fail` / `:info`，并不是把 Clojure 关键字搬进了本课引擎。检查器是针对单一寄存器的教学枚举，不是 Knossos，也不是 Elle。本课所有时间都来自 `/workspace/atlas/lessons/practice126/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `GeoKVEngine` 解决的是跨机房的恢复目标：同步与异步复制怎样决定写延迟和丢失窗口，断网时哪个机房可写，主备切换 runbook 的 RTO / RPO，以及备份是否真的能恢复并校验。那一课的边界停在**事先定义的切换与恢复步骤**，没有回答另一件事：系统口头承诺了某种一致性之后，怎样留下一份独立于实现的证据，去抓住那些只在并发和故障下才出现、成功路径测试很难碰到的错误。

一份切换 runbook 可以量出「新主库何时可写」，但不能代替「把并发读写的调用与返回记下来，注入故障，再用模型去核对」。节点把摘要写成：通过生成并发操作历史、注入故障并检查模型，寻找成功路径测试难以暴露的一致性错误。

### 接上前置

前置 [第 114 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/114.html) `h-consistency` 已经说明：线性一致性要求操作看起来在调用与返回之间某一点原子生效，并尊重实时先后；那一课构造过不可线性化的 `SET` / `GET` 历史，但还没有给完成记录分成确定成功、确定失败和不确定，也没有把故障注入写进同一条历史。前置 [第 118 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/118.html) `h-raft-log` 留下的是法定人数与旧领导者不能在分区后继续充当权威。本课用三节点教学 KV 把「多数派写入、本地读」和「多数派读」对照起来，看一种读法会不会在分区后读到已经完成的写入之前的标题。前置 [第 111 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/111.html) `g-recovery` 演练的是备份能否恢复并校验；本课的「验证」对象换成了操作历史：没有记下来的历史，就没有可复现的反例，也就没有独立证据。

Jepsen 教程把一次测试写成：客户端把 invoke 变成 ok / fail / info，nemesis 往集群里注入故障，checker 拿模型和历史做判断。本课的引擎和实验是这条结构的教学对照，不是把 Jepsen 或 etcd 搬到本课目录里跑一遍。

### 教学场景（教学假设）

假设内容后台把文章 `article:01:title` 放在三个教学节点 `n1`、`n2`、`n3` 上，一开始三边已经对齐为 `Weekly digest`。三个客户端进程分别连到这三个节点：进程 0 连 `n1`，进程 1 连 `n2`，进程 2 连 `n3`。多数派写入和多数派读取花费 `quorum_s=0.10`，本地读取花费 `local_s=0.05`，客户端超时是 `timeout_s=0.20`，nemesis 每一步花费 `0.05`。写入先试图应用到可达的同伴，够多数才记 `ok`；多数未达成或应答在路上丢失时，等到超时并记 `info`，而不是直接记 `fail`。所有时间都是教学虚拟时钟上的值，不是线上往返时延，也不是生产 RTT。

### 本课三条主线

1. **历史记录**：每次操作都要记下 invoke，以及后来的 ok、fail 或 info。超时属于不确定结果，不能写成确定失败。
2. **checker 模型**：用单一寄存器的线性化模型去核对整条历史。确定失败的操作不生效；确定成功的必须在调用与返回之间生效；info 则两种可能性都要试。
3. **nemesis 与最小反例**：由 nemesis 注入分区或丢失应答，得到一条可以手工检查的失败历史，并把能复现它的种子保存下来。

## 学习目标

- 能解释历史记录及适用边界：invoke 只表示尝试开始，完成必须是 ok、fail 或 info 之一；超时和「应答丢了但集群里可能已经写上」都属于 info。没有历史，就不能判断模型，也不能复现反例。
- 独立完成实践：给三节点 KV 记录 invoke/ok/fail/info 历史，手工检查一个失败案例。能指出哪一次已完成的写入和哪一次读出的旧标题互相矛盾，并能用同一颗种子再次得到同一条历史。
- 验收上要做到：超时使用不确定结果而非直接失败；复现并保存反例种子。

## 为什么后端要懂

节点给出的理由是：**让一致性承诺有独立证据。** 实现自己声明「写入成功之后再读一定能看到新标题」，并不足够。成功路径上的几次读写往往都是健康集群、顺序执行，暴露不了分区后的陈旧读，也暴露不了「客户端以为写失败、集群里其实已经写上」这种窗口。把并发操作记成历史、用故障注入把窗口打开、再用独立于实现的模型去核对，才是可以把承诺证伪的办法。测试仍然不能穷尽状态空间，它能发现错误，不能代替证明——但没有历史和种子，连发现了什么都无法复查。

## 核心知识点

### 1. 历史记录

一条教学历史是按虚拟时钟排列的事件流。客户端先写下 `invoke`，表示开始尝试；完成时再写 `ok`（确定发生了）、`fail`（确定没发生）或 `info`（不确定）。教程原文把这三种完成类型说成：`:ok` if the operation succeeded, `:fail` if it didn't take place, or `:info` if we're not sure；抛出异常也会被当成 `:info`。本课用同样的三分法，只是写成 JSON 字符串。

健康的多数派路径里，没有故障，四次操作都有成对的 invoke 与 ok，历史校验和为 `7e716acb17f550d2`：

| 时刻 | 类型 | 进程 | 操作 | 标题 |
|---|---|---|---|---|
| `0.00`–`0.10` | invoke / ok | 0 | write | `Weekly digest (revised)` |
| `0.20`–`0.30` | invoke / ok | 1 | read | `Weekly digest (revised)` |
| `0.40`–`0.50` | invoke / ok | 1 | write | `Weekly digest (hotfix)` |
| `0.60`–`0.70` | invoke / ok | 2 | read | `Weekly digest (hotfix)` |

两次写入的确认时延都是 `0.10`，两次读取也是 `0.10`，结束于 `0.70`。检查器认为这条历史合法，见证线性化的最终值是 `Weekly digest (hotfix)`。这是成功路径：它说明记录格式和模型可以在无故障时对上，但不能单独当作一致性证据。

超时必须记成 info。第二条实验里，nemesis 在 `0.15` 对 `n1` 开始丢弃应答（`0.20` 记一条 nemesis 的 info），随后进程 0 在 `0.20` 写入 `Weekly digest (hotfix)`。这次写入实际应用到了 `n1`、`n2`、`n3`，但应答丢失，客户端在 `0.40` 等到 `timeout_s=0.20` 后记下 `info`，原因是 `timeout_lost_reply`。之后进程 1 在 `0.50` 发起多数派读，`0.60` 读到 `Weekly digest (hotfix)`。把这次超时保持为 info 时，检查器认为历史合法：不确定的写入可以被解释为「已经生效」。若错误地把它改标为 `fail`，检查器会假定这次写入没发生，于是无法解释后来读到的 hotfix，判定变为不合法。这就是验收要求的那句话：超时使用不确定结果而非直接失败。

### 2. checker 模型

检查器使用的模型是单一寄存器：一次生效的写把标题换成新值，一次生效的读必须读到当前标题。它只看操作的 `f` 与 `value`，并不知道节点内部怎样复制。确定失败的操作被排除；确定成功的必须出现在某个尊重实时先后的线性化里；info 则「发生了」和「没发生」两种子集都要枚举。这是教学枚举，不是 Knossos，也不是 Elle，只适用于本课这种很短的历史。

把模型和历史对上，才能抓住成功路径看不到的错误。上一小节里，同一条超时历史在「记 info」时合法、在「误标 fail」时不合法，差的不是集群状态，而是完成类型有没有把不确定性留给模型。检查器给出的误标失败原因是：`can't read Weekly digest (hotfix) from register Weekly digest (revised)`，因为 `0.10` 完成的 revised 写入若后面没有 hotfix，就解释不了 `0.50` 才开始的那次读。

模型也有适用边界。它检查的是「看起来像不像一个寄存器」，并不证明实现在所有调度下都线性一致，更不会去穷尽状态空间。一次合法的结果只对送进去的那条历史负责。

### 3. nemesis 与最小反例

nemesis 是专门注入故障的进程，它自己的完成也记为 info。要得到一条能用手检查的失败案例，本课让它在 `0.15` 把 `n3` 分区出去（`0.20` 完成），此时三节点都已经有 `Weekly digest (revised)`。进程 0 随即在 `0.20` 写入 `Weekly digest (hotfix)`，`0.30` 以多数派成功返回，只应用到 `n1` 和 `n2`。进程 2 在 `0.35` 向已经孤立的 `n3` 做本地读，`0.40` 返回 `Weekly digest (revised)`。

这条历史的校验和是 `5702df2c35c4e2f5`，可以按时间逐条核对：

| 时刻 | 类型 | 谁 | 做了什么 |
|---|---|---|---|
| `0.00`–`0.10` | invoke / ok | 进程 0 | 多数派写入 `Weekly digest (revised)`，应用到 n1/n2/n3 |
| `0.15`–`0.20` | invoke / info | nemesis | `start_partition`，把 n3 隔离 |
| `0.20`–`0.30` | invoke / ok | 进程 0 | 多数派写入 `Weekly digest (hotfix)`，只应用到 n1/n2 |
| `0.35`–`0.40` | invoke / ok | 进程 2 | 在 n3 本地读到 `Weekly digest (revised)` |

检查器判定不合法，原因是：`can't read Weekly digest (revised) from register Weekly digest (hotfix): write of Weekly digest (hotfix) completed at 0.3 before read invoked at 0.35`。hotfix 的写入在 `0.30` 已经完成，读在 `0.35` 才开始，实时上写在读之前，读却回到了旧标题。这就是成功路径测试很难碰到、但手工可以一眼看完的最小反例。同一时间表若改成多数派读，孤立的 `n3` 凑不齐多数，读取在 `0.55` 记为 `info`（`timeout_no_majority`，时延 `0.20`），不再返回一个看起来成功的旧标题，检查器认为合法。

只靠手工构造还不够，验收要求把反例种子保存下来并能复现。本课在本地读模式下从种子 `1262026` 起连续试了 30 个种子，其中 21 个合法、9 个不合法；第一颗失败种子是 `1262027`，历史校验和 `e7647d6a1f6253a9`，原因是 `Weekly digest (west)` 的写入在 `0.35` 完成之后，`0.85` 才开始的本地读仍读到 `Weekly digest (revised)`。用同一颗种子再跑一遍，校验和与判定都相同；换一颗相邻种子 `1262028`，校验和变成 `901375405fe769b0`，与反例不同。没有保存种子，就无法证明这次失败还能再看见。

## 常见误区

第一个误区是节点写明的那句：**随机杀进程不记录操作历史。** 对照路径里 nemesis 在 `0.25` 杀掉 `n2`、在 `0.35` 杀掉 `n3`，操作员只留下两条「killed n2 / killed n3」的笔记，把历史清空。结果是 `history_recorded=false`，检查器没有跑，也没有反例种子。进程是倒下了，但没有 invoke/ok/fail/info 可核对，失败无法被定位，也无法被复现。

第二个误区是把超时写成 `fail`。上面那次已经应用到三个节点、只是应答丢失的 hotfix 写入，若被标成确定失败，检查器会把后来读到的 hotfix 判成错误。看起来「测试更严了」，其实是在用错误的完成类型制造误报，也会在相反的窗口里漏掉真正的不确定性。

第三个误区是看见一次合法结果就宣布「已经线性一致」。健康路径是合法的，30 个随机种子里也有 21 个合法，这只说明这些历史没找到反例，不是证明。

## 设计权衡

节点给出的权衡是：**状态空间无法穷尽，测试能发现错误不能替代证明。** 本课的检查器只枚举送进去的短历史；随机狩猎只覆盖 30 个种子，远远谈不上穷尽。它在本地读加分区的设置下找出了 9 条反例，并保存了种子 `1262027`，这是「能发现错误」。同一套多数派读在完全相同的手工时间表上不再返回陈旧的 ok 读，说明一种实现选择可以避开这条已知反例，但那同样只对测过的历史负责。要把承诺变成证明，需要的是论证或模型检验；把承诺变成可复查的证据，需要的是历史、故障注入、检查器和种子。二者不是替代关系。本课把这一条落实为 `tradeoff_testing_is_not_proof`：窗口里 21 次合法、9 次不合法，总数是 30，而不是一个声称已经穷尽的数字。

## 动手实践：给三节点 KV 记录 invoke/ok/fail/info 历史，手工检查一个失败案例

配套代码在 [`/workspace/atlas/lessons/practice126/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice126.tar.gz)。`engine.py` 是 `VerificationEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_verification.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。

请先手工核对自己会看的那条失败历史：`0.30` 完成的 hotfix 写入只到了 `n1` / `n2`，`0.35` 开始的本地读在 `n3` 上于 `0.40` 返回 `Weekly digest (revised)`，检查器给出的原因与时间戳必须对得上。然后再核对另外两组数字：超时写入在 `0.40` 记为 `info`，后读在 `0.60` 读到 hotfix，info 路径合法、误标 fail 则不合法；反例种子 `1262027` 的校验和 `e7647d6a1f6253a9` 可以复现，相邻种子不同。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_timeout_is_info`、`acceptance_seed_saved`、`practice_history_and_manual_fail_case`、`pitfall_kill_without_history`、`tradeoff_testing_is_not_proof`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_jepsen`、`not_knossos`、`not_elle`、`not_etcd`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Jepsen — Fault injection testing 中与 Jepsen testing 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://github.com/jepsen-io/jepsen](https://github.com/jepsen-io/jepsen) 返回 200，无跳转，294864 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Fri, 13 Nov 2026 17:17:12 GMT`（北京时间 2026-11-14 01:17:12）。页面没有 `Last-Modified`。`<title>` 为 GitHub - jepsen-io/jepsen: A framework for distributed systems verification, with fault injection。仓库所有者为 jepsen-io，页上没有个人作者署名。根目录 `LICENSE` 同日 404；许可以同日读取的 `jepsen/project.clj` 为准，为 Eclipse Public License。
- 该页展示的 README 原文在 [https://raw.githubusercontent.com/jepsen-io/jepsen/main/README.md](https://raw.githubusercontent.com/jepsen-io/jepsen/main/README.md) 返回 200，无跳转，13500 字节。*Design Overview* 给出控制节点、DB 节点、进程与客户端、生成器、history、nemesis 和 checker 这条流水线，并把结果写到 `store/<test-name>/<date>/`。这不是一份单独改换主题的文档，而是节点所给仓库的正文。
- README 点名的教程是跟进阅读的依据，不是另换官方来源。同日打开 [doc/tutorial/index.md](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/tutorial/index.md)（200，754 字节）以及其中的 03 / 04 / 05 章和 [What's Here](https://raw.githubusercontent.com/jepsen-io/jepsen/main/doc/whats-here.md)（200，9127 字节），用来核对历史类型、模型与检查器、以及 nemesis 注入故障的写法。

教学实验按以下方式使用这些文本，并记下差异。

- **术语与完成类型**：教程写明 invoke 表示尝试，完成必须是 `:ok`、`:fail` 或 `:info`；不确定时用 `:info`，异常也会变成 `:info`。本课采用同一组含义，但用 JSON 字符串记录，不要写成「引擎里跑着 Clojure 关键字」。nemesis 的完成在教程里也是 `:info`（例如 `:start` / `:stop`）；本课对应为 `start_partition`、`drop_replies` 等教学故障名，不是 `partition-random-halves` 那个现成 nemesis。
- **模型与检查器**：04 章用 Knossos 的 `CASRegister` 和 `checker/linearizable` 说明「模型负责 step，检查器判断历史是否合法」；失败时可以打开 `store/latest/history.txt` 和线性化示意图。本课的检查器是针对短历史的教学枚举，只覆盖单一寄存器上的读和写，没有 CAS，没有 SVG，也没有 Fressian 存储格式。不要把 `results.json` 里的 `valid` 写成 Knossos 的输出。
- **反例形态**：05 章给出的典型失败是 “can't read 3 from register 4”，原因是 etcd 允许不经共识读取本地副本；加上 quorum 读之后测试通过。本课手工反例是同一类陈旧读：已完成的标题是 `Weekly digest (hotfix)`，本地读却返回 `Weekly digest (revised)`。标题、节点和时间都是教学假设，不是把 etcd v2 API 或 Verschlimmbesserung 客户端搬进本课。教程里的 5 秒超时、约 1 秒的 stagger 和 15 秒 time-limit，也不是本课的 `0.05` / `0.10` / `0.20`。
- **超时与 info**：验收要求的「超时使用不确定结果而非直接失败」，直接对应教程对 `:info` 的定义。本课用「多数派已经写上、应答被丢掉」这条窗口，让误标 `fail` 的对照给出假的不合法；这是教学对照，不是 Jepsen 解释器在抛异常时自动转 info 的原实现。
- **种子与 store/**：Jepsen 把测试、历史和分析写到 `store/`，用最新结果的符号链接方便复查。本课把反例种子 `1262027` 和校验和写进 `results.json`，用同一生成器复现，没有实现 `store/` 目录，也没有 lein 命令行。
- **未实现的仓库内容**：SSH 控制平面、Debian 安装、AWS Marketplace 集群、时钟偏移、文件损坏、Elle 事务检查、性能曲线、以及真实 DB 节点，教学引擎都没有做，它们的数字不会进入 `results.json`。
- **网络与时间**：单进程离散事件，所有时延都是教学虚拟时钟上的值，不是生产 RTT。三节点在同一进程里用分区集合模拟隔离，没有 iptables，也没有真正的网络丢包。

## 自测题

**题 1.** 为什么超时要记成不确定结果，而不能直接记成失败？请用 hotfix 写入应答丢失、随后又读到 hotfix 的那条历史说明。

<details>
<summary>答案</summary>

完成类型要回答的是「这件事在不在系统里发生过」，不是「客户端有没有等到应答」。`0.20` 开始的 hotfix 写入已经应用到 `n1`、`n2`、`n3`，只是 nemesis 让 `n1` 上的应答丢失，客户端在 `0.40` 超时。这时不能断定写入没发生，只能记 `info`，原因是 `timeout_lost_reply`，时延 `0.20`。

之后进程 1 在 `0.50` 发起多数派读，`0.60` 读到 `Weekly digest (hotfix)`。检查器把这次 info 解释为「写入已经生效」，历史合法。若把同一条完成改标为 `fail`，模型里只剩下 `0.10` 完成的 `Weekly digest (revised)`，就无法解释这次读，检查器会报 `can't read Weekly digest (hotfix) from register Weekly digest (revised)`。看起来更严，其实是误报。教程把「不确定」专门留给 `:info`，把 `:fail` 留给确定没发生的操作；超时属于前者。

</details>

**题 2.** 请手工检查那条三节点失败历史：哪一次写入已经完成，哪一次读取返回了旧标题，为什么这违反线性化？同一时间表改成多数派读，完成类型会变成什么？

<details>
<summary>答案</summary>

先按时间把事件摆出来。`0.00` 到 `0.10`，进程 0 把 `Weekly digest (revised)` 写到三个节点。`0.15` 到 `0.20`，nemesis 把 `n3` 分区出去。`0.20` 到 `0.30`，进程 0 多数派写入 `Weekly digest (hotfix)`，只应用到 `n1` 和 `n2`，并且已经 `ok`。`0.35` 到 `0.40`，进程 2 在 `n3` 上本地读，得到 `Weekly digest (revised)`，也是 `ok`。

违反线性化的原因不在「分区」这个事实本身，而在实时先后：hotfix 的写入在 `0.30` 已经完成，读在 `0.35` 才 invoke。读的区间完全落在这次写入返回之后，却读到了更旧的 revised。检查器给出的句子就是 `can't read Weekly digest (revised) from register Weekly digest (hotfix)`，时间戳是 `completed at 0.3 before read invoked at 0.35`。历史校验和 `5702df2c35c4e2f5`，可以对照 `results.json` 逐条核对。

同一时间表若使用多数派读，孤立的 `n3` 凑不齐两个节点，这次读在 `0.55` 记为 `info`，原因 `timeout_no_majority`，时延 `0.20`，不会再给出一个成功的旧标题，检查器认为合法。换读法避开的是「成功返回陈旧值」，不是把分区变没。

</details>

**题 3.** 为什么发现反例之后要保存种子并再跑一遍？若只随机杀进程、不记录历史，会失去什么？30 个种子里有 21 个合法，能否据此宣布系统已经被证明？

<details>
<summary>答案</summary>

没有种子，就无法证明眼前这条失败历史还能再生成一次，也就无法把反例交给别人复查。狩猎从 `1262026` 起试了 30 颗种子，第一颗失败种子是 `1262027`，历史校验和 `e7647d6a1f6253a9`，判定不合法。用同一颗种子再跑，校验和与原因都相同；相邻种子 `1262028` 的校验和是 `901375405fe769b0`，与反例不同。验收要求「复现并保存反例种子」，指的就是这一对数字，而不是只在屏幕上看到一次失败。

若只随机杀进程、不记录 invoke/ok/fail/info，对照路径里只剩下 `0.25` 杀掉 `n2`、`0.35` 杀掉 `n3` 两条笔记，历史被清空，检查器没有跑，也没有种子。故障发生过，但没有可分析的对象。这正是「随机杀进程不记录操作历史」这个误区。

21 次合法、9 次不合法，只说明这个大小为 30 的窗口里测试能发现错误，也能漏过许多种子。状态空间无法穷尽，测试不能替代证明。健康路径那一条合法历史同样如此：它验证的是记录和模型在无故障时能对上，不是三节点 KV 的证明。

</details>

## 推荐资源

- [Jepsen — Fault injection testing](https://github.com/jepsen-io/jepsen)（Jepsen 仓库，英文，专家，免费）：用于核对 history、checker 与 nemesis 这条测试结构，并将结论落到本节点的三节点历史、超时 info 和反例种子实验。2026-11-14 该地址返回 200，无跳转，294864 字节，`<title>` 为 GitHub - jepsen-io/jepsen: A framework for distributed systems verification, with fault injection，页面无 `Last-Modified`，无个人作者署名。同日读取的 README（13500 字节）给出 Design Overview；教程 03 / 04 / 05 章定义了 `:invoke` / `:ok` / `:fail` / `:info`、模型加检查器，以及 nemesis 注入故障后查看 `store/latest/history.txt` 的做法。根目录没有 LICENSE 文件，`jepsen/project.clj` 声明为 Eclipse Public License。本课引擎不是 Jepsen、Knossos 或 Elle；教学时间和标题不要与教程里的 etcd 例子或 5 秒超时混用。SSH 集群、Elle 事务检查和性能图本课没有实现。

## 本课小结

- 一致性承诺需要独立证据：生成并发操作历史，注入故障，再用模型检查，才能抓住成功路径测试难以暴露的错误。
- 历史必须记录 invoke，以及 ok / fail / info。超时是不确定结果：hotfix 写入已应用到三个节点，但在 `0.40` 因丢失应答记为 `info`；后读在 `0.60` 读到 hotfix。保持 info 则合法，误标 fail 则误报。
- 检查器按单一寄存器模型核对短历史。健康的多数派路径四次操作都是 ok，结束于 `0.70`，最终标题是 `Weekly digest (hotfix)`。这只对送入的历史负责。
- 手工失败案例是分区后的陈旧本地读：`0.30` 已完成的 hotfix 写入之后，`0.35` 开始的读在 `n3` 返回 `Weekly digest (revised)`。同一时间表的多数派读变为 `info`，不再返回旧值。失败种子 `1262027`（校验和 `e7647d6a1f6253a9`）可以复现。
- 随机杀进程却不记历史，就既不能检查也不能复现。30 个种子里 21 个合法、9 个不合法，说明测试能发现错误，不能穷尽状态空间，也不能替代证明。
- 这些是教学虚拟时钟上的数字，不是生产 RTT。`VerificationEngine` 不是 Redis，也不是 Jepsen、Knossos 或 etcd。教程定义了完成类型和测试结构；本课用教学枚举和三节点标题来落实它们。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到教程里「完成必须是 ok、fail 或 info」以及「用模型和历史判断是否合法」这两段，不要把超时写成确定失败，也不要把「杀过进程」或「有一次测试通过」当成已经验证过的一致性承诺。
