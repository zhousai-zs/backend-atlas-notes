# 第 114/208 课：一致性模型与线性化点

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `h-consistency` |
| 英文名 | Linearizability |
| 领域 / 阶段 | H / 第 3 阶段 |
| 子类 | 理论与故障模型 |
| 难度 | 进阶 |
| 前置节点 | `h-failure`、`g-kv`、`e-atomic` |
| 标签 | `Linearizability`、`Distributed` |
| 参考资料 | [Jepsen — Linearizability](https://jepsen.io/consistency/models/linearizable)（Jepsen，英文文档，进阶，免费；2026-11-13 该页返回 200，无跳转，4588 字节）；[Jepsen — Consistency models](https://jepsen.io/consistency)（Jepsen，英文文档，进阶，免费；同日返回 200，无跳转，4498 字节。其 Models 入口 `/consistency/models/` 先 302 到 `https://jepsen.io/consistency/models`，最终 200，15395 字节；线性化页链出的 Sequential Consistency 页同日 200、无跳转、4620 字节，正文按需引用） |
| 摘要 | 线性一致性要求操作看起来在调用与返回之间某点原子生效，并尊重真实时间先后。 |

> **边界说明**：本课中的「内容后台」「文章标题 `Weekly digest`」「键 `article:01:title`」都是**教学假设**，用来让调用时间、返回时间和读到的标题变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `ConsistencyEngine`，包含 `Clock`、`RecordedOp`、`LockedRegister`、`EventualStore` 以及针对读写寄存器的线性一致性 / 顺序一致性检查器，**不是 Redis，不是 redis-py，不是网络服务，不是 Jepsen/Knossos，也不是 Raft**。它没有 TCP，没有真实 socket，没有跨机器共享的物理时钟；`Clock` 只是仿真 Jepsen 一致性模型页里那个「假想的、完全同步、全局可访问的时钟」，让实验可以重复，真实部署里的两台机器并不会共享这样一个时钟。官方材料已于 **2026-11-13** 用 curl 核对：`https://jepsen.io/consistency/models/linearizable` 与 `https://jepsen.io/consistency` 均为 200、无跳转；线性化页链到的 `https://jepsen.io/consistency/models/sequential` 为 200、无跳转；`https://jepsen.io/consistency/models/` 会落到 `https://jepsen.io/consistency/models`，最终 200。`https://jepsen.io/consistency/models/eventual` 同日返回 404，本课不引用这个地址。教学实验里的时间和标题都来自 `/workspace/atlas/lessons/practice114/results.json`，是教学虚拟时钟上的数字，不是生产 RTT，也不是任何系统在真实网络上的基准。

## 导读

### 一个直觉：写已经返回成功，后来的读不该再看到旧标题

内容后台（教学假设）要把已发布文章的标题从 `Weekly digest` 改成 `Weekly digest (revised)`。编辑侧的 `SET` 返回了 `ok`。若另一次 `GET` 是在这次 `SET` 返回之后才开始的，却仍然读到 `Weekly digest`，这还算不算「这次修改已经生效」？

节点把线性一致性说得很硬：操作看起来在调用与返回之间某点原子生效，并尊重真实时间先后。上面那条历史不尊重真实时间先后：写已经完成，后来才开始的读却返回了更早的值。Jepsen 的 Linearizability 页把同一句话写成：若操作 A 完成之后操作 B 才开始，则 B 必须在逻辑上晚于 A 生效。

本课要盯住的不是「两台副本的内存里此刻是否字面相同」，而是：一组带有调用时间和返回时间的 `SET`/`GET`，能不能找到一个符合寄存器语义、又尊重实时顺序的全序。对内容后台来说，这决定了标题保存成功之后，预览页和故障切换后的读路径到底能不能再看到旧标题。

### 接上前置

前置的 [第 113 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/113.html) `h-failure` 已经说明：超时不能证明远端没有执行，把流量切到另一台副本时，那台副本可能还没见过这次写。[第 104 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/104.html) `g-kv` 给出了单键读写的对象，[第 69 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/69.html) `e-atomic` 讨论了原子与复合不变量。本课把「原子生效」落到可检查的历史上：线性化点必须落在这次调用与返回之间，并且不能把已经结束的写排到后来才开始的读之后。节点把「为什么」写成「为 KV 读写与故障切换定义可测试的语义」，原因就在这里：没有这样一条语义，保存成功、预览和切主就无法对打。

### 一次 SET/GET 走进教学引擎之后

教学引擎把每次操作记成一条 `RecordedOp`：`inv_s` 是调用时间，`ret_s` 是返回时间。若 `ret_s(A) < inv_s(B)`，就说 A 在实时顺序上先于 B，并记一条实时边 `A → B`。两次操作的区间有重叠、谁也不先于谁，就记入 `overlapping_pairs`。检查器在尊重实时边的所有全序里，寻找一条遵守读写寄存器单线程语义的顺序：`GET` 必须返回这条顺序里最近一次 `SET` 的值，若还没有写过，就返回初值。找得到，这条历史就是线性一致的；找不到，就不是。

单机锁实现还会多记一个 `lin_point_s`：持有锁期间对共享槽位做 load 或 store 的那一个时刻。最终一致的教学模型则有两台副本 `r1`、`r2`：`SET` 可以只在主副本 `r1` 上生效就返回，再在 `sync_delay_s=0.08` 之后才把标题抄到 `r2`。客户端若在同步前去读 `r2`，就会得到一条不可线性化的历史。

### 教学场景（教学假设）

假设内容后台用键 `article:01:title` 保存已发布文章的标题，初值是 `Weekly digest`；一次编辑写成 `Weekly digest (revised)`，一条用来对照的非法读值写成 `Weekly digest (hotfix)`。教学参数是 `local_step_s=0.01`、`sync_delay_s=0.08`。这些数字是虚拟时钟上的教学值，不是线上超时配置，也不是生产 RTT。

### 本课三条主线

1. **操作历史与实时顺序**：历史由带调用、返回时间的操作组成；实时顺序只约束「已经结束」对「尚未开始」，重叠的操作没有先后。
2. **线性化点与顺序一致性**：线性化点落在调用与返回之间；顺序一致性只要求与各进程的程序顺序一致，不要求跨进程的实时顺序。
3. **读写寄存器历史检查**：用寄存器语义检查一组 `SET`/`GET` 是否线性一致，并给出与最终一致可区分的历史。

## 学习目标

- 能解释操作历史与实时顺序及适用边界。
- 能独立完成：记录并发 `set`/`get` 的调用返回时间，构造一个不可线性化历史。

## 为什么后端要懂

公众号内容后台（教学假设）会修改已发布标题，然后立刻把预览或线上读流量打到某台副本；上一课里的故障切换，也会把读请求从一台副本转到另一台。如果接口把这次 `SET` 返回 `ok` 理解成「之后开始的读一定看到新标题」，那这条承诺就是线性一致；如果只保证「过一会儿副本会收敛到同一个标题」，那就是更弱的最终一致，编辑保存成功之后的第一次预览仍可能看到旧标题。

节点要求你为 KV 读写与故障切换定义可测试的语义，说的就是这件事：给一组真实发生过的调用和返回，能够判断它有没有打破承诺。不能靠「两台机器上的字符串此刻看起来一样」来代替，因为那既不是定义，也不是一个可在客户端观察到的检查。

## 核心知识点

### 1. 操作历史与实时顺序

Jepsen 的 Consistency models 页把一次操作写成带有调用时间（invocation）和完成时间（completion）的区间，时间来自那个假想的全局时钟，由此得到实时顺序。两个区间若有重叠，这两次操作就是并发的；同一个进程是单线程的，它自己的两次操作不会并发。一组这样的操作构成一条历史。线性一致性是这条历史上的安全性：它声明哪些历史合法，不保证系统在分区下一定还能推进——同一页和线性化页都写明，线性一致在异步网络分区下不能 totally 或 sticky available。

教学引擎在单机锁寄存器上记下了三次操作，用来把这些定义变成一条看得见的时间线。编辑进程的 `set1` 把标题写成 `Weekly digest (revised)`，`inv_s=0.0`，线性化点 `0.01`，`ret_s=0.03`。阅读进程的 `get_overlap` 在 `inv_s=0.02` 开始，此时 `set1` 还没返回，两次操作重叠；它等到锁释放后才进入临界区，`lin_point_s=0.03`，`ret_s=0.04`，读到的是 `Weekly digest (revised)`。另一次 `get_after` 在 `inv_s=0.05` 开始，`lin_point_s=0.06`，`ret_s=0.07`，同样读到修订后的标题。

实时边只有两条：`set1 → get_after` 和 `get_overlap → get_after`。`set1` 与 `get_overlap` 是唯一的重叠对：`set1` 的返回时刻 `0.03` 并不早于 `get_overlap` 的调用时刻 `0.02`，所以实时顺序并不强制这次重叠读必须看到新标题。它之所以看到新标题，是因为锁把线性化点排成了 `set1` 在前、`get_overlap` 在后。检查器给出 `linearizable=true`，并且 `all_lin_points_in_interval=true`。

适用边界在这里已经能看出来。实时顺序约束的是「已经完成的操作」对「尚未开始的操作」；它不把重叠的两次操作排定先后，也不要求你去比较两台机器上未暴露给客户端的内部内存。历史里如果还有调用了但尚未返回的操作，Jepsen 的模型页要求把它视为与调用之后的所有操作并发。本课实验只用完整操作，这条规则没有在检查器里展开。

### 2. 线性化点与顺序一致性

线性化点就是「看起来原子生效」的那个时刻，它必须落在这次调用与返回之间。单机锁实现把这个点说得很具体：持有单机锁期间，对共享槽位做 load（`GET`）或 store（`SET`）的那一下。验收要求指出的，就是这个位置，而不是「拿锁」或「函数返回」本身。

为了让读到旧标题也成为合法情形，实验让阅读进程先进入临界区。`get_old` 的 `inv_s=0.0`，`lin_point_s=0.02`，`ret_s=0.03`，返回 `Weekly digest`；编辑进程的 `set2` 的 `inv_s=0.01`，`lin_point_s=0.03`，`ret_s=0.04`，写入 `Weekly digest (revised)`。两次操作重叠，`lin_point_order=["get_old", "set2"]`，每个线性化点都落在自己的 `[inv_s, ret_s]` 里，历史是线性一致的。读到旧标题在这里合法，正因为 `get_old` 的线性化点仍在 `set2` 之前，而不是因为「读请求开始得早就可以永远忽略已经完成的写」。

把「已经完成的写」忽略掉，得到的是另一条历史。构造出来的 `sc_not_lin` 里，`set_done` 在 `0.02` 返回，另一个进程的 `get_stale` 在 `0.03` 才开始，`0.05` 返回初值 `Weekly digest`。实时边是 `set_done → get_stale`，检查器给出 `linearizable=false`、`linearization=null`。它仍然是顺序一致的：`sequentially_consistent=true`，`sequential_order=["get_stale", "set_done"]`。两个进程各只有一条操作，程序顺序并不强制写在读之前，于是「先读到初值，再执行那次写」在顺序一致性下是合法的。Jepsen 的 Sequential Consistency 页也写明：顺序一致的系统里，一个进程可以读到任意陈旧的状态；它要求的是存在一个与各进程程序顺序一致的全序，而不是跨进程的实时顺序。线性化页把二者的关系写在同一段：当你不需要实时约束、但仍希望所有进程看到同一个全序时，才退到顺序一致性。模型图上的箭头是线性一致蕴含顺序一致，反过来不成立。这条 `sc_not_lin` 就是一个反例。

若把这次陈旧的 `GET` 改成与 `SET` 属于同一进程，程序顺序就会强制写在读之前。`same_process_stale_read` 的 `linearizable` 和 `sequentially_consistent` 都是 `false`。可见「读到旧值」并不自动等于「只违反线性一致」；要先看这次读和那次写是不是同一个进程的程序顺序。

### 3. 读写寄存器历史检查

把对象限定成一个读写寄存器——本课里就是单个键 `article:01:title`——检查就变成三件事，对应 Jepsen 转述的 SingleOrder、RealTime、RVal：存在一个全序，它尊重实时边，并且每一步都像单线程在操作这个寄存器。线性化页强调这是单对象模型；KV 存储里「一个对象」常常是一个 key，跨多个 key 的实时约束要另谈严格可串行化，本课不展开。

检查器在几条短历史上的结论如下。

| 历史 | 线性一致 | 顺序一致 | 说明 |
|---|---|---|---|
| `legal_after_write`：写已返回，后来的读返回 `Weekly digest (revised)` | `true`（`["w", "r"]`） | `true` | 最基本的合法历史 |
| `stale_after_completed_write`：写已返回，后来的读仍返回 `Weekly digest` | `false` | `true`（`["r", "w"]`） | 与 `sc_not_lin` 同类 |
| `overlapping_read_old_legal`：重叠的读返回旧标题 | `true`（`["r", "w"]`） | `true` | 实时边为空，读可以排在写前 |
| `overlapping_read_new_legal`：重叠的读返回新标题 | `true`（`["w", "r"]`） | `true` | 实时边为空，读也可以排在写后 |
| `new_then_old`：两个重叠写入（修订标题与 `Weekly digest (hotfix)`）都在 `0.02` 返回后，后来的两次读分别返回这两个值 | `false` | `true`（`["w2", "r_first", "w3", "r_second"]`） | 两次写都已经结束，寄存器不能再先后露出两个不同的最终值 |
| `never_written_value`：写的是修订标题，后来的读返回从未写入的 `Weekly digest (hotfix)` | `false` | `false` | 任何全序都无法解释这个返回值 |

验收还要求给出线性一致与最终一致的可区分历史。Jepsen 当天的模型图并没有把「最终一致」列成与线性一致并列的那种安全性模型，`/consistency/models/eventual` 返回 404；本课按节点验收，把最终一致按活跃性来检查：若不再有新写入，副本最终收敛到最后一次写入，之后的读都返回该值。

最终路径里，`SET` 在 `0.02` 返回时，`r1` 已经是 `Weekly digest (revised)`，`r2` 仍是 `Weekly digest`。随后一次打到 `r2` 的 `GET` 在 `0.03` 开始、`0.05` 返回旧标题，于是整段历史 `linearizable=false`。同步发生在 `0.09`，两边都变成修订标题；再一次 `GET` 在 `0.10` 开始、`0.12` 返回修订标题，`eventually_converged=true`。这就是「写已成功、同步前的读仍看到旧标题，但之后会收敛」的最终一致历史。

对照路径并不等待 `r2`。`SET` 同样在 `0.02` 返回，返回时两台副本的内容仍然不同；但客户端只读主副本 `r1`，`0.03` 开始、`0.05` 返回的 `GET` 读到 `Weekly digest (revised)`，`linearizable=true`。`histories_are_distinguishable=true`：两条历史在「写返回之后那次读看到了什么」上分开，检查器也给出了相反的结论。

## 常见误区

节点写出的误区是：认为所有副本随时内容相同才叫线性一致。对照路径正好把这句话拆开。`linearizable_primary_read` 里，`SET` 返回的那一刻 `r1` 是 `Weekly digest (revised)`，`r2` 仍是 `Weekly digest`，`replicas_not_identical_at_write_return=true`；客户端历史却是线性一致的，因为后来的读走的是主副本，读到了新标题。`pitfall_replicas_need_not_be_identical=true`。

线性一致约束的是客户端观察到的操作历史，不是副本内部是否在每一时刻保持字节级相同。副本可以短暂不一致，只要客户端没有一条「写已返回、后来的读却看到旧值」的观察。反过来，两台副本在某一瞬间内容相同，也证明不了历史合法：若系统曾经让一次已经完成的写之后的读看到旧值，检查器仍然会给出 `linearizable=false`。把「副本是否相同」当成定义，既会放过真正的违规，也会把一次完全合法的主副本读写误判为失败。

## 设计权衡

节点给出的权衡是：更强读语义通常需要协调或租约前提。教学引擎用两种确认方式对同一条标题修改把这笔账算出来。本地确认的 `SET` 在 `0.02` 返回，此时若允许读任意副本，`eventual_any_replica_get_value` 仍是 `Weekly digest`，`eventual_any_replica_can_be_stale=true`。若 `SET` 等到同步再返回，返回时刻是 `0.10`，那时 `r1` 和 `r2` 都已经是 `Weekly digest (revised)`，之后打到 `r2` 的读也是修订标题，`coordinated_write_is_slower=true`。

另一条路是不把写推迟到 `0.10`，但限制读的位置：只读主副本，或只读持有有效租约的副本。对照路径已经表明，这样可以在 `SET` 于 `0.02` 返回之后仍然给出线性一致的读，代价是分区期间部分副本不能再提供这种读——这和线性化页所说「分区下不能 totally 或 sticky available」是同一笔交易。`stronger_reads_need_coordination_or_lease=true`：要么在写的返回路径上协调，要么在读的路径上用主副本或租约做前提。两者都会牺牲一部分延迟或可用性，换来的是「保存成功之后开始的读必须看到新标题」这条可测试的承诺。

## 动手实践：记录并发 set/get 的调用返回时间，构造一个不可线性化历史

配套代码在 `/workspace/atlas/lessons/practice114/`，用 Python 3 标准库实现，不依赖 Redis 或网络服务。运行验收：

```bash
cd /workspace/atlas/lessons/practice114
./test.sh
```

标准输出应是一行 `ALL_CHECKS_PASSED`，并写入 `results.json`。下面的数字都来自这次运行，时间单位是教学虚拟时钟上的秒。

**1. 记下并发 `SET`/`GET` 的调用与返回时间。** `history_and_realtime` 给出三条操作：`set1` 从 `0.0` 调用到 `0.03` 返回，线性化点 `0.01`；与它重叠的 `get_overlap` 从 `0.02` 到 `0.04`，读到 `Weekly digest (revised)`；`get_after` 从 `0.05` 到 `0.07`，同样读到修订标题。实时边是 `set1 → get_after`、`get_overlap → get_after`，重叠对只有 `set1` 与 `get_overlap`。这是一条线性一致的历史，三次线性化点都落在各自的调用与返回之间。

**2. 指出单机锁的线性化点，并构造一条不可线性化历史。** `lock_impl` 里，`get_old` 在 `0.02` 做 load，返回 `Weekly digest`；`set2` 在 `0.03` 做 store。线性化点就是持有单机锁期间这次 load 或 store，`lin_point_order=["get_old", "set2"]`。然后用两个不同进程构造 `sc_not_lin`：`SET` 于 `0.02` 返回，`GET` 于 `0.03` 开始、`0.05` 返回 `Weekly digest`。这就是验收要求构造的不可线性化历史：`linearizable=false`，但它是顺序一致的，顺序为 `["get_stale", "set_done"]`。同一进程做完写再读到旧值，则线性一致和顺序一致都会失败。

**3. 用检查器核对寄存器历史。** `register_checker` 确认：写后读到新值合法；写已返回仍读到旧值不线性；重叠读到旧值或新值都合法；两个已完成的重叠写入之后，后来的两次读不能分别返回 `Weekly digest (revised)` 和 `Weekly digest (hotfix)`；读到从未写入的值则两种模型都不接受。`legal_cases_linearizable=true`，`illegal_cases_not_linearizable=true`。

**4. 给出线性一致与最终一致的可区分历史。** `lin_vs_eventual` 里，最终路径的 `SET` 在 `0.02` 返回时 `r2` 仍是旧标题，随后打到 `r2` 的读在 `0.05` 返回 `Weekly digest`，`linearizable=false`；`0.09` 同步之后，`0.12` 返回的读是修订标题，`eventually_converged=true`。只读主副本的对照路径在同样的副本落后下，`0.05` 返回修订标题，`linearizable=true`。`acceptance_distinguish_lin_vs_eventual=true`。权衡数字在 `tradeoff_coordination_or_lease`：本地确认返回 `0.02`，任意副本读可能仍是旧标题；等待同步的确认返回 `0.10`，之后任意副本读到修订标题。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Jepsen — Linearizability 中与 Linearizability 相关的定义、示例或实现，记录与教学实验的差异。2026-11-13 实际打开的页面如下。节点 `resources` 数组里的两条入口均返回 200 且无跳转；另外两个地址是这两页链出去、并经过核对的相关页，不把它们写成节点资源的第三条、第四条。

- [Jepsen — Linearizability](https://jepsen.io/consistency/models/linearizable)（节点给出的入口，200，无跳转，4588 字节）：给出线性一致的定义、单对象范围，以及「A 完成之后 B 才开始，则 B 必须晚于 A 生效」。本课的实时边和不可线性化历史都按这句话检查。页内把定义归于 Herlihy 与 Wing 1990 年的论文，并转述为 SingleOrder、RealTime、RVal；教学检查器实现的是这三条在单键读写寄存器、且操作均已完成时的枚举判定，不是论文里的完整证明系统。
- [Jepsen — Consistency models](https://jepsen.io/consistency)（节点给出的入口，200，无跳转，4498 字节）：把一致性模型定义成合法历史的集合，并指向模型图、现象和依赖。用来比较模型强弱，以及理解「可区分历史」是从历史集合里分开的，不是从副本内存快照里分开的。
- [Jepsen — Consistency Models](https://jepsen.io/consistency/models)（由上面两个入口链到，请求 `/consistency/models/` 先 302 到无尾斜线地址 `https://jepsen.io/consistency/models`，最终 200，15395 字节）：补充调用时间、完成时间、并发、未完成操作和历史的表示；并写明线性一致蕴含顺序一致。教学引擎用一条 `RecordedOp` 同时记下 `inv_s` 和 `ret_s`，没有改用 Jepsen 那种把调用和返回拆成两个事件的列表。
- [Jepsen — Sequential Consistency](https://jepsen.io/consistency/models/sequential)（由线性化页链出，200，无跳转，4620 字节）：说明顺序一致只与各进程程序顺序一致，进程可以读到任意陈旧状态。`sc_not_lin` 用这条边界对照线性一致。

与教学实验的主要差异还有这些。教学引擎是单进程仿真，共享的 `Clock` 只为了让实时顺序可重复，并不演示线性一致在分区下无法 totally 或 sticky available。实验只用完整操作，未实现「未完成操作必须视为与调用之后所有操作并发」。检查器是小历史的排列枚举，不是 Knossos，也没有去跑 Jepsen 的测试工具。Jepsen 当天的模型图没有把「最终一致」画成一个安全性节点，本课按验收用同步后的收敛来构造可区分历史，不要把这一点误认成 Jepsen 页上的现成模型条目。

## 自测题

**题 1.** 编辑进程把 `article:01:title` 写成 `Weekly digest (revised)`，在 `0.02` 返回 `ok`。另一个进程的 `GET` 在 `0.03` 开始，在 `0.05` 返回 `Weekly digest`。这条历史是线性一致的吗？是顺序一致的吗？若改成同一个进程先写后读，结论会怎么变？

<details>
<summary>答案</summary>

不是线性一致，但是顺序一致。实时边强制这次 `GET` 必须排在 `SET` 之后，寄存器语义又要求它返回新标题，两者无法同时满足，所以 `linearizable=false`、`linearization=null`。两个进程之间没有程序顺序约束，顺序 `["get_stale", "set_done"]` 仍合法，故 `sequentially_consistent=true`。这正是 `results.json` 里的 `sc_not_lin` 与 `stale_after_completed_write`。若读写属于同一进程，程序顺序也强制写在读前，于是 `same_process_stale_read` 的线性一致和顺序一致都是 `false`。
</details>

**题 2.** 单机锁实现的线性化点在哪里？为什么 `get_old` 返回旧标题 `Weekly digest`，这条历史仍然线性一致？

<details>
<summary>答案</summary>

线性化点是持有单机锁期间，对共享槽位的 load（`GET`）或 store（`SET`），不是拿锁、放锁或函数返回本身。`lock_impl` 里 `get_old` 的线性化点是 `0.02`，`set2` 的线性化点是 `0.03`，都落在各自的调用与返回之间，`lin_point_order=["get_old", "set2"]`。重叠的读可以排在写之前，所以返回旧标题并不违规。若把这次读移到 `set2` 返回之后再开始，再返回旧标题，就变成 `sc_not_lin` 那种不可线性化历史。
</details>

**题 3.** `SET` 在 `0.02` 返回时，`r1` 已是 `Weekly digest (revised)`，`r2` 仍是 `Weekly digest`。这能证明系统不是线性一致的吗？怎样给出与最终一致可区分的两条历史？

<details>
<summary>答案</summary>

不能。线性一致看的是客户端历史，不是「所有副本随时内容相同」。`linearizable_primary_read` 在同样的副本落后下，只读 `r1`，后来的 `GET` 返回修订标题，`linearizable=true`，这正是节点写出的误区要排除的情况。可区分的最终一致历史是：同一时刻允许读 `r2`，这次 `GET` 在 `0.03` 开始、`0.05` 返回旧标题，于是 `linearizable=false`；等到 `0.09` 同步后，`0.12` 返回的读是修订标题，`eventually_converged=true`。两条历史的差别在写返回之后那次读看到了什么，不在「返回瞬间副本是否相同」。
</details>

## 推荐资源

- [Jepsen — Linearizability](https://jepsen.io/consistency/models/linearizable)（Jepsen，英文文档，进阶，免费）：用于核对操作历史与实时顺序、线性化点与顺序一致性，将结论落实到本节点实验。2026-11-13 核对为 200，无跳转。
- [Jepsen — Consistency models](https://jepsen.io/consistency)（Jepsen，英文文档，进阶，免费）：比较模型间强弱关系与可区分历史。2026-11-13 核对为 200，无跳转。

## 本课小结

- 线性一致要求存在一个尊重实时顺序、又符合对象单线程语义的全序；操作看起来在调用与返回之间的线性化点上原子生效。`set1` 于 `0.03` 返回之后，`0.05` 才开始的 `get_after` 必须读到 `Weekly digest (revised)`；与 `set1` 重叠的 `get_overlap` 则没有这条实时约束。
- 单机锁的线性化点是持有锁期间那一次 load 或 store。`get_old` 在 `0.02` 读到旧标题、`set2` 在 `0.03` 写入新标题，仍然线性一致。`SET` 于 `0.02` 返回后，另一进程在 `0.03` 开始的读若仍返回旧标题，则 `linearizable=false`、`sequentially_consistent=true`；同一进程的同样读法两种模型都不接受。
- 线性一致不是「所有副本随时内容相同」。只读主副本时，`SET` 返回瞬间 `r1` 与 `r2` 可以不同，客户端历史仍可以是线性一致；读落后的 `r2` 就会得到与最终一致可区分的那条历史：`0.05` 仍看到旧标题，`0.09` 同步后在 `0.12` 看到新标题。更强的读语义要用协调（`SET` 推迟到 `0.10` 才返回）或主副本/租约前提来换。三句都能答上来，并且能指出 `practice114/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验字段，再回到线性化页关于「A 完成后 B 才开始则 B 必须晚于 A 生效」、顺序一致页关于「进程可以读到陈旧状态」和模型页关于「模型是合法历史的集合」的那几句，不要只把「副本要一致」或「保存成功就能到处读到新标题」背下来。
