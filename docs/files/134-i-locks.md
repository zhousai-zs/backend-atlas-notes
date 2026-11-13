# 第 134/208 课：锁竞争与调度延迟

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-locks` |
| 英文名 | Lock contention |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 剖析与诊断 |
| 难度 | 深入 |
| 前置节点 | [`i-flame`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/132.html)、[`e-lock`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/66.html) |
| 标签 | `Lock contention`、`Performance` |
| 参考资料 | [Linux Kernel — Lock statistics](https://www.kernel.org/doc/html/latest/locking/lockstat.html)（Linux 内核文档，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，24629 字节，`Content-Type` 为 `text/html; charset=utf-8`，`Last-Modified` 为 `Mon, 09 Nov 2026 00:37:43 GMT`（北京时间 2026-11-09 08:37:43），响应头 `Date` 为 `Fri, 13 Nov 2026 18:12:10 GMT`（北京时间 2026-11-14 02:12:10）。`<title>` 为 Lock Statistics — The Linux Kernel documentation，页内 `h1` 为 Lock Statistics 与 The Linux Kernel，页头版本为 **7.4.0-rc2**。页脚为 © The kernel development community，由 Sphinx 5.3.0 生成。配套 rst 源 [lockstat.rst.txt](https://www.kernel.org/doc/html/latest/_sources/locking/lockstat.rst.txt) 返回 200、11819 字节，`Last-Modified` 为 `Wed, 07 Aug 2024 14:39:34 GMT`（北京时间 2024-08-07 22:39:34），无单独作者署名。该页定义 contentions（不得不等待的加锁次数）、acquisitions（取到锁的次数），以及 wait time / hold time 的 min / max / total / avg；时间值的整数部分以微秒为单位。输出通过 `CONFIG_LOCK_STAT` 和 `/proc/lock_stat`（文中示例版本 0.4）采集，对象是**内核锁类**，不是用户态 mutex 剖析器。它没有把 futex 或用户态自旋写成术语。同一 locking 手册目录里列出了 [Lock types and their rules](https://www.kernel.org/doc/html/latest/locking/locktypes.html)（HTTP 200，39120 字节，同一 `Last-Modified` 与 `Date` 18:12:55 GMT，北京时间 02:12:55，`<title>` 为 Lock types and their rules — The Linux Kernel documentation，版本同为 7.4.0-rc2），其中把内核锁分成 Sleeping locks（如 mutex）与 Spinning locks（如 `raw_spinlock_t`）。节点同时给出的 [Linux man-pages — futex(2)](https://man7.org/linux/man-pages/man2/futex.2.html) 在 2026-11-14 返回 HTTP 200，无跳转，34072 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Sun, 08 Nov 2026 07:37:16 GMT`（北京时间 2026-11-08 15:37:16），响应头 `Date` 为 `Fri, 13 Nov 2026 18:12:11 GMT`（北京时间 02:12:11）。`<title>` 为 futex(2) - Linux manual page，页内 `h1` 为 futex(2) — Linux manual page，页脚为 Linux man-pages 6.19、日期 2026-02-14，HTML 渲染标注 2026-09-09、Michael Kerrisk。该页把 futex 定义为 fast user-space locking：多数同步留在用户态，只有「很可能要阻塞较长时间」时才调用 `futex()`；无竞争时用原子指令，内核并不跟踪锁状态；有竞争时用 `FUTEX_WAIT` 阻塞、`FUTEX_WAKE` 唤醒。页中**没有**出现 spin 这个词。它指向的 [futex(7)](https://man7.org/linux/man-pages/man7/futex.7.html) 返回 200、11901 字节，同一 `Last-Modified`，`Date` 为 `Fri, 13 Nov 2026 18:12:54 GMT`（北京时间 02:12:54），`<title>` 为 futex(7) - Linux manual page，页脚日期 2026-02-17，同一 HTML 渲染日期。该页写明：无竞争时 futex 操作完全在用户态进行，内核只在有竞争时介入。本课没有把未打开的 PI-futex 专题文当成结论依据。） |
| 摘要 | 吞吐可能受临界区串行部分和等待调度限制；CPU 不高也可能存在严重竞争。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」都是**教学假设**，用来让全局锁与分片锁、持有时间与等待时间、futex 式阻塞与忙等，以及一致快照时的锁顺序变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在某台机器上调用了宿主 `perf`、`g++`、`/proc/lock_stat` 或 `futex` 系统调用。配套代码是 Python 3 标准库写的教学引擎 `LockEngine`，**不是 Redis，不是真实的剖析器或锁产品，不是网络服务，不是 Linux 的 lock_stat 或 pthread mutex，也不是宿主机上的 g++ / clang**。它不读取 `/proc/lock_stat`，不发出真实的 `FUTEX_WAIT`，也不把文档示例里 `mmap_sem` 那组微秒计数写进 `results.json`。lockstat 页统计的是内核锁类；本课用同一套「持有 / 等待 / contentions / acquisitions」口径，对照多线程教学 KV 的用户态锁。futex(2) 与 futex(7) 说明无竞争走用户态、有竞争才阻塞；本课的「自旋 / 忙等」是教学等待策略，用来对比阻塞等待会不会把 CPU 利用率抬高，这两个词并不出现在 lockstat.html 或 futex(2) 正文里。内核的 Spinning locks 与 Sleeping locks 写在跟随阅读的 Lock types 页，那是内核锁类别，不是用户态自适应互斥的自旋次数。`30.00` 是教学虚拟时钟上的三十秒，不是墙钟三十秒，也不是生产 RTT。本课所有数字都来自 `/workspace/atlas/lessons/practice134/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `HeapEngine` 解决的是：RSS 涨了，究竟是泄漏、保留容量，还是分配率本身很高。那一课的边界停在**堆占用和分配来源**，没有回答另一件事：多线程 KV 在取 `Weekly digest` 时，吞吐是卡在临界区里的工作，还是卡在等锁、等被调度；CPU 看起来不高，是不是就已经没有竞争了。

节点把摘要写成：吞吐可能受临界区串行部分和等待调度限制；CPU 不高也可能存在严重竞争。只看见利用率低，不能据此断言锁是空闲的。

### 接上前置

前置 [第 132 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/132.html) `i-flame` 留下的结论是：on-CPU 与 off-CPU 是不同事件，宽塔首先要问采样事件是什么。等锁如果走阻塞，时间会落在 off-CPU；如果走忙等，时间仍在 on-CPU，CPU 会被抬高。本课把这件事落到锁的持有时间和等待时间上。

另一则前置是 [第 66 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/66.html) `e-lock`。它已经说明互斥锁把临界区串行化，粗锁易验证、分片能提高并行度，也增加死锁与维护风险。本课不再展开 `lock_guard` 的写法，只用到「同一契约、固定锁顺序」：对比全局锁和分片锁在不同线程数下的吞吐与等待，并用一次一致快照检查锁顺序。

官方 lockstat 把一次加锁画成：取锁、若有竞争则进入 wait、acquired 之后进入 hold、再释放。futex(2) 则说明无竞争时不必进内核。本课用教学引擎复现这两组口径，而不是在本机打开 `/proc/lock_stat`。

### 教学场景（教学假设）

假设内容后台有一个多线程教学 KV：工作线程按键取出标题 `Weekly digest`，键形如 `article:01:title` 到 `article:16:title`。临界区里做一次取值和序列化，教学虚拟时钟上固定为 `20` 微秒；锁外还有 `10` 微秒的请求收尾。比较一把全局锁和 `8` 把按键分片的锁，线程数取 `1`、`2`、`4`、`8`，窗口都是 `30.00`。等待策略对照 futex 式阻塞和忙等自旋。另外用一次按分片顺序 `0..7` 取得的一致快照，以及一组反向加锁的死锁演示、一组乐观重试的「无锁」路径。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT。

### 本课三条主线

1. **锁持有与等待时间**：hold time 是临界区里的工作，wait time 是为了进入临界区付出的等待。适用边界是：吞吐可能被串行的 hold 卡住，即使 wait 已经很长；CPU 利用率要用「等待时是否还在跑」一起读，不能单看繁忙程度。
2. **futex 与自旋**：无竞争时同步留在用户态；有竞争时，futex 式等待会让出 CPU，忙等则把等待也记成 CPU。适用边界是：忙等可能略掉调度唤醒，却会虚增利用率，不能用来证明「机器还有余量」。
3. **分片锁与快照**：分片让不同键并行，一致快照却要按固定顺序拿齐全部分片。适用边界是：细锁提高吞吐，也增加锁顺序和死锁的管理成本。

## 学习目标

- 能解释锁持有与等待时间及适用边界：hold 是锁内工作，wait 是锁等待；全局锁的吞吐会被临界区串行部分限制，CPU 不高也可能有大量 contentions。
- 独立完成节点实践：对比全局锁和分片锁，在 `1` / `2` / `4` / `8` 个线程下记录吞吐与等待时间。
- 能区分锁内工作与锁等待，并说明为什么不要用忙等去虚增 CPU 利用率；细锁需要固定顺序，无锁也并不必然更快。

## 为什么后端要懂

节点对「为什么」写得很干脆：优化多线程 KV 的扩展性。放到本课的教学内容后台：一把全局锁保护取出 `Weekly digest` 时，1 个线程吞吐是 `33333.33` 次 / 秒；加到 `8` 个线程，吞吐只到 `50000.00`，平均等待却从 `0` 升到 `130.00` 微秒，contentions 是 `1499999` 次。此时若用 futex 式阻塞，CPU 利用率只有 `0.1875`——竞争已经很严重，CPU 并不高。换成 `8` 个分片后，同样 8 个线程吞吐到 `266666.67`，等待为 `0`。不会读 hold 和 wait，就会在「再加线程」或「再加机器」上走弯路。

## 核心知识点

### 1. 锁持有与等待时间

lockstat 把 contentions 写成「不得不等待的加锁次数」，acquisitions 写成「取到锁的次数」，并给 wait time 与 hold time 各一组 min / max / total / avg，单位是微秒。本课按这套口径记账，但不读取宿主的 `/proc/lock_stat`。

教学临界区是 `20` 微秒的取值和序列化，与线程数无关：所有全局锁实验里 holdtime-avg 都是 `20.00` 微秒。变的是等待。1 个线程时 contentions 为 `0`，wait 为 `0`，`30.00` 秒里完成 `1000000` 次，吞吐 `33333.33`。2 个线程起，临界区被占满，完成次数都是 `1500000`，吞吐停在 `50000.00`：2、4、8 个线程不再提高吞吐。waittime-avg 则是 `10.00`、`50.00`、`130.00` 微秒，8 个线程时 waittime-total 为 `194999520` 微秒，contentions 为 `1499999`。锁内工作还是那 `20` 微秒，多出来的是排队，加上「下一个可运行线程被调度进来」所占用的窗口。节点说的「受临界区串行部分和等待调度限制」，指的就是这件事。

适用边界是：hold 和 wait 要分开读。把它们加在一起，看不出是临界区太长，还是只是人多。文档示例里的内核锁数字没有进入 `results.json`。

### 2. futex 与自旋

futex(2) 的定义是 fast user-space locking：多数操作留在用户态，只有很可能要阻塞较长时间才进 `futex()`。无竞争时用原子指令取锁，内核并不维护锁状态；有竞争时 `FUTEX_WAIT` 阻塞，释放方 `FUTEX_WAKE`。futex(7) 补了一句：无竞争时操作完全在用户态，内核只仲裁有竞争的情况。两页都没有把「自旋」写成术语。本课把忙等当作一种教学等待策略：等待期间线程仍占着 CPU，用来对照「避免通过忙等虚增 CPU 利用率」。

8 个线程抢同一把全局锁时，futex 式阻塞的吞吐是 `50000.00`，CPU 利用率是 `0.1875`，有用工作占已用 CPU 的 `1.0`——忙的都是临界区，等待是阻塞的。改成忙等后，waittime-avg 仍是 `130.00`，吞吐仍是 `50000.00`，CPU 利用率却升到 `1.0`，有用工作只占已用 CPU 的 `0.1875`。多烧的都是空转。分片且无竞争时，两种等待策略都是吞吐 `266666.67`、CPU `1.0`，因为根本没有等待可转。忙等并不能把已经被串行部分卡住的全局锁「转」出更高吞吐，却会让利用率看起来很满。

内核文档 Lock types 页里的 Spinning locks（如 `raw_spinlock_t`）是另一类：那是内核里关抢占的自旋锁，不是本课用户态忙等的计数对象。本课不模拟关中断或 PREEMPT_RT。

### 3. 分片锁与快照

16 个键、8 个分片时，8 个线程按键行走，始终落在不同分片上，contentions 为 `0`。吞吐随线程数近似线性：`33333.33`、`66666.67`、`133333.33`、`266666.67`。这是分片把串行临界区拆开之后的收益。

要做一份一致快照，却必须按固定顺序拿齐 `0..7`。教学快照在窗口中点发起，先等 `20` 微秒才拿到 0 号分片，再持有全部分片 `200` 微秒，结束于 `15000220` 微秒。这段时间工作线程被挡住，完成次数从 `8000000` 降到 `7999944`，吞吐 `266664.8`，并记下 9 次 contentions，最长等待 `190` 微秒。收益还在，但快照把自己变成了新的竞争点。

若两个快照辅助路径一个按 `0、1` 取锁，另一个按 `1、0` 取锁，教学引擎会在等待图里检出环，`deadlock_detected` 为真。两边都改成升序后，环消失。这与第 66 课「多锁必须有全局固定顺序」是同一条边界，也对应节点写的权衡：细粒度锁增加管理成本与死锁风险。

## 常见误区

节点写出的误区是：认为无锁必然更快。同一条取标题路径，若不用互斥，而用乐观重试（每次花 `20` 微秒拷贝，发现版本变了就立刻再来），1 个线程时吞吐仍是 `33333.33`，没有重试。8 个线程时，尝试 `11294120` 次，重试 `9882355` 次（`0.875`），吞吐只有 `47058.83`，还低于全局锁的 `50000.00`，更远低于分片锁的 `266666.67`；CPU 利用率是 `1.0`，有用工作只占 `0.1765`。无锁省掉的是互斥，不是冲突；冲突变成重试之后，等待以忙等的形式出现，吞吐不必更好。无锁结构有自己的适用边界，不能把它理解成「只要把锁拿掉就一定更快」。

## 设计权衡

节点写出的权衡是：细粒度锁增加管理成本与死锁风险。分片把 8 线程吞吐从 `50000.00` 拉到 `266666.67`，等待从 `130.00` 微秒降到 `0`，这是并行度的收益。代价是：一致快照要按 `0..7` 的顺序拿齐全部锁，会引入新的等待（本课是 `20` 微秒再加 `200` 微秒的持有）；锁顺序一旦对反，就会死锁。粗锁更容易论证正确性，也更容易把临界区变成整段服务的串行部分。选择粒度时，要把吞吐、等待、快照方式和锁顺序放在同一张表里看，而不是只比较「锁的把数」。

## 动手实践：对比全局锁和分片锁，在不同线程数下记录吞吐与等待时间

配套代码在 [`/workspace/atlas/lessons/practice134/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice134.tar.gz)。`engine.py` 是 `LockEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_locks.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，对比全局锁和分片锁，在不同线程数下记录吞吐与等待时间；跑的是教学引擎，不是宿主 lock_stat，也不是宿主 `futex`。

请先自己把 1 / 2 / 4 / 8 个线程下，全局锁与 8 分片锁的吞吐、holdtime-avg、waittime-avg 和 CPU 利用率记进一张表，确认全局锁在 2 个线程之后吞吐停在 `50000.00`，等待从 `10.00` 升到 `130.00` 微秒，而分片锁在 8 个线程达到 `266666.67`、等待为 `0`。然后再核对另外三组：同样 8 线程全局锁，futex 式阻塞的 CPU 是 `0.1875`，忙等是 `1.0`，吞吐却同为 `50000.00`；中点快照按 `0..7` 取锁，完成 `7999944` 次，快照自身等待 `20` 微秒；乐观重试在 8 线程下吞吐 `47058.83`，重试率 `0.875`，并不比加锁更快。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_distinguishes_hold_from_wait`、`acceptance_avoids_busy_wait_cpu`、`pitfall_lockfree_not_always_faster`、`tradeoff_fine_locks_need_order`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_lock_product`、`not_host_perf`、`not_host_gxx`、`not_host_lock_stat`、`not_host_futex`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Linux Kernel — Lock statistics 中与 Lock contention 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://www.kernel.org/doc/html/latest/locking/lockstat.html](https://www.kernel.org/doc/html/latest/locking/lockstat.html) 返回 200、无跳转、24629 字节，页头内核文档版本为 7.4.0-rc2，`<title>` 为 Lock Statistics — The Linux Kernel documentation。rst 源 11819 字节，`Last-Modified` 为 2024-08-07。本课跟随打开了同一手册目录中的 Lock types 页（39120 字节），以及节点同时列出的 futex(2)（34072 字节，man-pages 6.19，页脚 2026-02-14）和它指向的 futex(7)（11901 字节，页脚 2026-02-17）。没有再把未打开的 Lightweight PI-futexes 等专题文算进结论。

教学实验按以下方式使用它，并记下差异。

- **锁持有与等待时间**：lockstat 给出 hold / wait 的 min、max、total、avg，以及 contentions / acquisitions，时间单位是微秒。本课用同一口径统计教学 KV，1 线程等待为 `0`，8 线程全局锁 waittime-avg 为 `130.00` 微秒、holdtime-avg 仍为 `20.00` 微秒。文档里 `/proc/lock_stat` 的示例行（如 `&mm->mmap_sem`）没有进入 `results.json`。本课没有启用 `CONFIG_LOCK_STAT`，也不区分 con-bounces / acq-bounces 这类跨 CPU bounce。
- **futex 与自旋**：定义来自 futex(2) / futex(7) 的「无竞争留在用户态、有竞争再阻塞」，不是 lockstat 正文。忙等是教学对照，用来显示同样 `50000.00` 的吞吐下 CPU 从 `0.1875` 升到 `1.0`。内核 Spinning locks 的定义在 Lock types 页，本课不把那一套 raw_spinlock 规则搬到用户态。
- **分片锁与快照**：分片与锁顺序来自本课对第 66 课的衔接，以及教学快照实验；lockstat 跟踪的是竞争点（contention points），不是 KV 快照。本课的快照等待 `20` 微秒、持有 `200` 微秒，是教学安排，不是文档里的示例。
- **时间含义**：lockstat 示例是某次内核采集的微秒计数，futex 页不给出生产环境下的调度延迟数字。本课全部是教学虚拟时钟上的值，不是生产 RTT，也不是本机跑了三十秒多线程程序的墙钟结果。教学模型在解锁的同一时刻把锁交给下一个等待者，没有再叠加一笔未在文档里出现的唤醒延迟。

## 自测题

**题 1.** 锁的 hold time 和 wait time 各回答什么问题？在本课的全局锁实验里，怎样用它们区分锁内工作与锁等待？为什么 8 个线程时 CPU 只有 `0.1875`，仍然算严重竞争？

<details>
<summary>答案</summary>

hold time 是持有锁、在临界区里做事的时间；wait time 是为了进入临界区而等待的时间。lockstat 还配合 contentions（不得不等的次数）和 acquisitions（取到锁的次数）一起读。

教学临界区固定是 `20` 微秒。1 个线程时等待为 `0`，吞吐 `33333.33`。2 个线程起吞吐就停在 `50000.00`（`1500000` 次 / `30.00` 秒），holdtime-avg 仍是 `20.00` 微秒，waittime-avg 则从 `10.00` 升到 4 线程的 `50.00`、8 线程的 `130.00` 微秒，contentions 为 `1499999`。锁内工作没有变长，变长的是排队。

8 个线程用 futex 式阻塞时，等待的线程不占 CPU，利用率只有 `0.1875`，但几乎每一次加锁都在等，平均要等 `130.00` 微秒。这正是节点说的：CPU 不高也可能存在严重竞争。吞吐这时已经被串行的 `20` 微秒临界区卡住，再加线程只会把 wait 拉长。

</details>

**题 2.** 为什么不要用忙等去虚增 CPU 利用率？请用 8 个线程、同一把全局锁下，futex 式阻塞与忙等的吞吐和 CPU 说明。官方文档有没有把这种用户态忙等叫作 spin？

<details>
<summary>答案</summary>

futex(2) 写的是：多数同步留在用户态，只有很可能要阻塞较长时间才进内核。本课的 futex 式路径在 8 线程全局锁下吞吐 `50000.00`，CPU `0.1875`，有用工作占已用 CPU 的 `1.0`。改成忙等后，waittime-avg 仍是 `130.00` 微秒，吞吐仍是 `50000.00`，CPU 却升到 `1.0`，有用份额降到 `0.1875`。多出来的利用率是空转，不是更多的 `Weekly digest` 取值。

lockstat.html 和 futex(2) 都没有用 spin 来命名这种用户态忙等。futex(7) 只区分无竞争的用户态路径和有竞争时的内核仲裁。内核 Lock types 页里的 Spinning locks 是另一类内核锁。所以本课把忙等标成教学策略，用来落实「不要通过忙等虚增 CPU 利用率」，而不是声称手册把用户态 mutex 定义成了自旋锁。

</details>

**题 3.** 分片锁为什么能提高吞吐，又为什么不能认为「无锁必然更快」或「越细越好」？请用 8 线程的吞吐、一致快照和死锁对照说明。

<details>
<summary>答案</summary>

8 个分片让 8 个线程落在不同键上，contentions 为 `0`，吞吐从全局锁的 `50000.00` 升到 `266666.67`，等待为 `0`。这是细锁的收益。

一致快照必须按 `0..7` 拿齐全部分片，自身先等 `20` 微秒，再持有 `200` 微秒，完成次数降到 `7999944`。若两个路径锁顺序相反，教学引擎会检出死锁；都改成升序后环消失。细锁把正确性从一把锁的契约，变成一组锁的顺序约定。

同一路径改成乐观重试后，8 线程吞吐只有 `47058.83`，重试率 `0.875`，既低于分片锁，也低于全局锁，CPU 却是满的。无锁没有消除冲突，只是把等待变成了重试。所以既不能认为无锁必然更快，也不能只看分片数、不看快照方式和锁顺序。

</details>

## 推荐资源

- [Linux Kernel — Lock statistics](https://www.kernel.org/doc/html/latest/locking/lockstat.html)（内核文档，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，24629 字节，文档版本 7.4.0-rc2，`<title>` 为 Lock Statistics — The Linux Kernel documentation。用于核对 hold / wait 时间、contentions 与 acquisitions。该页描述的是内核 `/proc/lock_stat`，本课的教学计数是对照，不是宿主采集，也不使用页内示例的微秒数字。
- [futex(2)](https://man7.org/linux/man-pages/man2/futex.2.html)（Linux man-pages 6.19，英文，进阶，免费）：节点同时列出的手册页。2026-11-14 返回 200，34072 字节，页脚日期 2026-02-14，HTML 渲染 2026-09-09、Michael Kerrisk。用于核对无竞争走用户态、有竞争再 `FUTEX_WAIT` / `FUTEX_WAKE`。页中不定义 spin；本课的忙等是教学对照。跟随阅读的 [futex(7)](https://man7.org/linux/man-pages/man7/futex.7.html)（11901 字节，页脚 2026-02-17）写明内核只仲裁有竞争的情况。

## 本课小结

- 吞吐可能受临界区串行部分和等待调度限制。CPU 不高也可能存在严重竞争。hold 是锁内工作，wait 是锁等待，二者要分开读。
- 全局锁在 2 个线程之后吞吐停在 `50000.00`，8 个线程的 waittime-avg 是 `130.00` 微秒，holdtime-avg 仍是 `20.00` 微秒，futex 式等待下 CPU 只有 `0.1875`。8 个分片后吞吐到 `266666.67`，等待为 `0`。
- 同样的等待时间，忙等会把 CPU 抬到 `1.0`，有用份额只剩 `0.1875`，吞吐并不增加。不要用忙等虚增利用率。futex / 忙等的对照来自 futex 手册的「无竞争留在用户态」，spin 一词并不出自 lockstat 正文。
- 一致快照要按固定顺序取齐分片，本课中点快照等待 `20` 微秒、持有 `200` 微秒；反向取锁会死锁。乐观重试在 8 线程下吞吐 `47058.83`、重试率 `0.875`，并不比加锁更快。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 `/proc/lock_stat` 或文档示例里的墙钟计数。`LockEngine` 不是 Redis，也不是 Linux lock_stat 或真实 futex。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「contentions 是不得不等的加锁」「hold / wait 以微秒计」以及「无竞争留在用户态、有竞争再阻塞」这几段，不要把低 CPU 当成没有竞争，也不要把去掉锁当成必然更快。
