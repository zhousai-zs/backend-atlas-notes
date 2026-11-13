# 第 136/208 课：NUMA 与内存局部性

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-numa` |
| 英文名 | NUMA locality |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 硬件与IO优化 |
| 难度 | 深入 |
| 前置节点 | [`i-benchmark`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/129.html) |
| 标签 | `NUMA locality`、`Performance` |
| 参考资料 | [Linux man-pages — numa(7)](https://man7.org/linux/man-pages/man7/numa.7.html)（Linux 手册页，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，13644 字节，`Content-Type` 为 `text/html`，`Last-Modified` 为 `Sun, 08 Nov 2026 07:37:16 GMT`（北京时间 2026-11-08 15:37:16），响应头 `Date` 为 `Fri, 13 Nov 2026 18:30:25 GMT`（北京时间 2026-11-14 02:30:25）。`<title>` 为 numa(7) - Linux manual page，页内 `h1` 为 numa(7) — Linux manual page，页脚为 Linux man-pages 6.19、日期 2026-02-08，HTML 渲染标注 2026-09-09、Michael Kerrisk。NAME 行是 numa - overview of Non-Uniform Memory Architecture，正文第一句则写成 Non-Uniform Memory Access (NUMA)。该页把 NUMA 定义为内存被分成多个 memory node 的多处理器系统：The access time of a memory node depends on the relative locations of the accessing CPU and the accessed node。通常每个 CPU 都有一块本地内存节点，访问它比访问「另一个 CPU 的本地节点」更快。它列出 get_mempolicy(2)、mbind(2)、migrate_pages(2)、move_pages(2)、set_mempolicy(2)，并建议应用走 libnuma；还描述了 `/proc/pid/numa_maps`（自 Linux 2.6.14）。这些接口仅在内核以 `CONFIG_NUMA` 编译时可用。该页**没有**出现 first touch 这个短语，也没有给出带宽或延迟数字，也没有展开 MPOL_* 模式。SEE ALSO 指向的 [set_mempolicy(2)](https://man7.org/linux/man-pages/man2/set_mempolicy.2.html) 在同日返回 HTTP 200，无跳转，19642 字节，同一 `Last-Modified`，`Date` 为 `Fri, 13 Nov 2026 18:31:48 GMT`（北京时间 02:31:48），`<title>` 为 set_mempolicy(2) - Linux manual page，页脚同为 man-pages 6.19、2026-02-08。该页写明：策略只在为线程分配新页时生效；For anonymous memory this is when the page is first touched by the thread。MPOL_DEFAULT 回退到系统默认策略，而系统默认是 local allocation——allocate memory on the node of the CPU that triggered the allocation。同日还打开了它点名的 [mbind(2)](https://man7.org/linux/man-pages/man2/mbind.2.html)（200，24441 字节，`Date` 18:31:49 GMT，北京时间 02:31:49，man-pages 6.19、2026-02-08）、[get_mempolicy(2)](https://man7.org/linux/man-pages/man2/get_mempolicy.2.html)（200，14532 字节，`Date` 18:31:55 GMT，北京时间 02:31:55）、[numa(3)](https://man7.org/linux/man-pages/man3/numa.3.html)（200，53296 字节，`Date` 18:31:51 GMT，北京时间 02:31:51，`<title>` 为 numa(3) - Linux manual page；该页属于 numactl 项目，HTML 注明于 2026-08-04 取自上游 Git，当时仓库最近一次提交为 2026-08-03，页脚为 SuSE Labs、December 2007，渲染日期 2026-09-09、Michael Kerrisk。正文写：On a NUMA architecture some memory areas have different latency or bandwidth than others；默认策略是 local allocation；`numa_run_on_node()` 使任务只在指定节点上运行，They will not migrate to CPUs of other nodes until the node affinity is reset）以及 [numactl(8)](https://man7.org/linux/man-pages/man8/numactl.8.html)（200，19865 字节，`Date` 18:31:54 GMT，北京时间 02:31:54，`<title>` 为 numactl(8) - Linux manual page，同属 numactl 项目，页脚 SuSE Labs、Mar 2004，相同的上游取用与渲染日期。它把 `--cpunodebind` / `--physcpubind` 与 `--membind` / `--localalloc` 分开；`--touch` 会提前触页，默认则是 when an applications maps and accesses a page 才应用策略）。本课没有把未打开的 cpuset(7) 当成结论依据。） |
| 摘要 | 多插槽机器的远程内存访问成本更高，线程绑定与首次触页位置会影响服务稳定性。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」「教学双节点拓扑（节点 0 的 CPU 为 `0..3`，节点 1 的 CPU 为 `4..7`）」「本地访问 `100` 纳秒、远程访问 `200` 纳秒」都是**教学假设**，用来让 NUMA node、首次触页和绑核与内存策略变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在本机用 `numactl`、`perf`、`g++` 或 `/sys/devices/system/node` 测到了双插槽带宽。配套代码是 Python 3 标准库写的教学引擎 `NumaEngine`，**不是 Redis，不是真实的剖析器或 NUMA 产品，不是网络服务，不是 libnuma / numactl，也不是宿主 STREAM 或内核的 NUMA 平衡器**。本次运行记录的宿主 `uname -r` 为 `6.12.113+`，`lscpu` 显示 **1** 个 NUMA 节点、**1** 个插槽、8 个 CPU，且本机没有 `numactl`；这些只用来声明「实验不是在这台单节点机器上采集的」。教学拓扑是声明值，带宽和延迟全部由引擎的虚拟时钟算出，没有把 `/sys/devices/system/node` 里的 distance 或 meminfo 写进 `results.json`。numa(7) 正文没有 first touch 这个词，也没有 100 / 200 纳秒或 640 / 320 MB/s 这些数字；「页在被线程第一次触达时才按策略分配」来自它指向的 set_mempolicy(2)。两线程带宽相加是教学假设，不是测到的内存控制器上限。`30.00` 是教学虚拟时钟上的三十秒，不是墙钟 STREAM，也不是生产 RTT。本课所有数字都来自 `/workspace/atlas/lessons/practice136/results.json`。

## 导读

### 上一课留下的缺口

上一课的 `EbpfEngine` 已经能在教学口径下统计 `recvfrom`、`sendto` 和 `futex` 的延迟分布，看见网络和锁上的等待。那一课的边界停在**系统调用事件**，没有回答另一件事：同样的取标题代码，搬到更大的多插槽机器上，扩展为什么会变差——线程是不是在访问另一块内存节点。

节点把摘要写成：多插槽机器的远程内存访问成本更高，线程绑定与首次触页位置会影响服务稳定性。只加线程、不看线程和页分别落在哪个 node，会把远程访问当成「代码变慢了」。

### 接上前置

前置 [第 129 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/129.html) `i-benchmark` 留下的结论是：微基准要写清运行环境；跟随的 Reducing Variance 已经把亲和性和 NUMA 列为方差来源。本课不再展开 `DoNotOptimize` 或重复采样，只用到「环境必须写进记录」这一条：比较跨节点与同节点的内存密集基准时，先报告拓扑和线程分布，再记录带宽和延迟。

官方 numa(7) 把 NUMA 写成「访问时间取决于 CPU 和被访问节点的相对位置」。首次触页、默认的 local allocation，以及「绑 CPU 不等于绑内存」，写在它指向的 set_mempolicy(2)、numa(3) 和 numactl(8) 里。本课用教学引擎复现这几条口径，而不是在本机的单节点系统上运行 `numactl --hardware`。

### 教学场景（教学假设）

假设内容后台的教学 KV `teachkv` 在一份 `256` MiB 的标题缓存上做内存密集扫描，键为 `article:01:title`，标题为 `Weekly digest`。教学机器有两个内存节点：节点 0 上是 CPU `0..3`，节点 1 上是 CPU `4..7`。一次访问按 `64` 字节计，本地花费教学虚拟时钟上的 `100` 纳秒，远程花费 `200` 纳秒，窗口都是 `30.00`。比较同节点运行和跨节点运行，并对照「只把工作线程钉到节点 0」与「先设定内存策略再首次触页」。另外用两组双线程：都放在节点 0，或一个在 0、一个在 1，但页都在节点 0 上首次触达。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT。

### 本课三条主线

1. **NUMA node**：内存按节点划分，访问时间取决于 CPU 和被访问节点是不是同一处。适用边界是：在单节点机器上测不到这条差异；到了多插槽机器，远程访问会把同样的扫描变成更低的带宽和更高的延迟。
2. **first touch**：匿名页的策略在线程第一次触达该页时才生效，默认是在触发分配的那颗 CPU 的本地节点上分配。适用边界是：malloc 或 mmap 本身通常还没有把页定在某个节点上；先碰页的那个线程，决定了后来的局部性。
3. **绑核与内存策略**：把线程钉在某颗 CPU 上，并不会把已经分配的页搬过来；要同时约束内存策略，并且在首次触页之前完成。适用边界是：绑定提高局部性，但会缩小可调度的 CPU 集合。

## 学习目标

- 能解释 NUMA node 及适用边界：本地访问快于远程访问；这条差异只在多节点拓扑上出现，并且取决于页实际所在的节点，而不只取决于「现在有几条线程」。
- 独立完成节点实践：比较跨节点与同节点运行内存密集基准，记录带宽及延迟。
- 能说明固定线程不等于内存已本地化，并在结果里报告拓扑与线程分布；绑定能提高局部性，也可能降低调度灵活性。

## 为什么后端要懂

节点对「为什么」写得很干脆：解释同样代码在大机器上扩展变差。放到本课的教学内容后台：同一段扫描 `Weekly digest` 缓存的代码，工作线程和页都在节点 0 时，教学带宽是 `640.00` MB/s，平均延迟 `100.00` 纳秒；把线程放到节点 1、页仍在节点 0，带宽降到 `320.00` MB/s，延迟升到 `200.00` 纳秒。两条线程都放在节点 0、页也在节点 0 时，带宽是 `1280.00` MB/s；若只加线程、一个放在 0、一个放在 1，而首次触页仍全部发生在节点 0，带宽只有 `960.00` MB/s，平均延迟 `150.00` 纳秒。看起来是「加了线程却没有线性扩展」，根因是有一半访问在走远程节点，而不是代码本身变慢。

## 核心知识点

### 1. NUMA node

numa(7) 的定义是：NUMA 指内存被划分成多个 memory node 的多处理器系统，某个节点的访问时间取决于发起访问的 CPU 和被访问节点的相对位置。这和访问时间对所有 CPU 都相同的对称多处理器形成对照。通常每个 CPU 都有一块本地内存节点，访问它可以快于访问另一个 CPU 的本地节点。numa(3) 补了一句：在 NUMA 架构上，有些内存区域的延迟或带宽与其他区域不同。

本课用教学双节点拓扑把这句话变成可记录的基准：本地 `100` 纳秒 / 行，远程 `200` 纳秒 / 行。`30.00` 秒的窗口里，单线程同节点带宽是 `640.00` MB/s（这里的 MB 按 \(10^6\) 字节计），跨节点是 `320.00` MB/s。适用边界很窄：宿主这次记录的是 1 个 NUMA 节点、1 个插槽，在那台机器上跑同样的扫描，不会出现这组 2 倍差异。要把「大机器上扩展变差」说清楚，必须先声明拓扑，再比较同节点与跨节点，而不是直接把单节点上的微基准外推。

numa(7) 没有给出 100 / 200 纳秒，也没有给出距离矩阵。这两个延迟是教学安排。官方文档要求的是「访问时间与相对位置有关」，不是某一组墙钟数字。

### 2. first touch

节点写的 first touch，并不出现在 numa(7) 正文里。它指向的 set_mempolicy(2) 写明：线程的默认策略只管比 mbind(2) 更宽的那些地址范围，而且 The policy is applied only when a new page is allocated for the thread. For anonymous memory this is when the page is first touched by the thread。系统默认策略是 local allocation：在触发这次分配的那颗 CPU 所属节点上分配；若本地节点没有空闲内存，再尝试 nearby 节点。numa(3) 也把任务和地址范围的默认策略写成 local allocation。numactl(8) 的 `--touch` 则从反面说明：默认并不提前触页，策略在进程映射并访问该页时才应用。

因而「谁先碰这页」决定了页落在哪个节点。本课的跨节点实验正是：初始化线程在 CPU 0（节点 0）上首次触达整块 `256` MiB 缓存，然后把工作线程放到 CPU 4（节点 1）。策略在第一次触页时已经生效，页留在节点 0，工作线程只能远程访问，带宽 `320.00` MB/s。适用边界是：只看分配调用、不看第一次写（或第一次读造成的缺页），会误以为内存已经在本节点。已经分配的页，不会因为后来换了线程所在 CPU 就自动搬迁；那是 migrate_pages(2) / move_pages(2) 的工作，本课不把迁移当成默认路径。

### 3. 绑核与内存策略

numactl(8) 把两类选项分开：`--cpunodebind` / `--physcpubind` 限制命令在哪些节点或哪些 CPU 上执行；`--membind` / `--localalloc` / `--preferred` 限制或偏好从哪里分配内存。只做前一类，并不等于做了后一类。set_mempolicy(2) 的 MPOL_BIND 是严格的内存约束：页只能从 nodemask 里的节点分配。numa(3) 的 `numa_bind()` 则是同时做 `numa_run_on_node_mask()` 和 `numa_set_membind()`；若只想钉在节点内的某几颗 CPU 上，文档建议配合 `sched_setaffinity(2)`。

本课用这个区分做验收。把工作线程钉到 CPU 0，但页已经在 CPU 4 上被首次触达：线程在节点 0，页在节点 1，访问仍是远程，带宽仍是 `320.00` MB/s。固定线程不等于内存已本地化。若在首次触页之前就设定 `--cpunodebind=0 --membind=0`（教学上的 MPOL_BIND 节点 0），再由 CPU 0 触页，访问回到本地，带宽 `640.00` MB/s。

绑定的代价也写在官方文档里：`numa_run_on_node()` 使任务不会迁移到其他节点的 CPU，直到亲和性被重置。本课钉在 CPU 0 时，可调度 CPU 从 8 个降到 1 个。这就是节点写的权衡：绑定提高局部性，可能降低调度灵活性。

## 常见误区

节点写出的误区是：只调线程数不观察拓扑。两组双线程把这件事写得很清楚。两条线程都在节点 0、页也在节点 0 时，教学带宽是 `1280.00` MB/s，延迟仍是 `100.00` 纳秒。若只是把线程加到 2，一个放在 CPU 0、一个放在 CPU 4，而首次触页全部发生在节点 0，带宽只有 `960.00` MB/s，平均延迟变成 `150.00` 纳秒——一半本地、一半远程。相对单线程的 `640.00`，看起来「加了线程有变快」，但远没有同节点双线程的 `1280.00`。不报告拓扑和线程分布，就只会继续加线程，或把责任推给「大机器上的代码」。验收要求把教学双节点拓扑、每条线程所在的 CPU / 节点、以及页所在的节点写进结果。

## 设计权衡

节点写出的权衡是：绑定提高局部性，可能降低调度灵活性。`--cpunodebind=0 --membind=0` 并在触页前生效时，访问回到本地，单线程带宽从跨节点的 `320.00` 回到 `640.00`。同一绑定把可运行 CPU 限制在节点 0，本课钉到单核时只剩 CPU 0。另一侧，不绑定则保留 8 个 CPU 的调度余地，但默认 local allocation 会把页留在「第一次触达」的那个节点，后续线程一旦跑到另一边，就会变成远程访问。选择绑定前，要同时写出：线程在哪、页在哪、还剩下哪些 CPU 可调度；不能只比较带宽。

## 动手实践：比较跨节点与同节点运行内存密集基准，记录带宽及延迟

配套代码在 [`/workspace/atlas/lessons/practice136/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice136.tar.gz)。`engine.py` 是 `NumaEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_numa.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，比较跨节点与同节点运行内存密集基准，记录带宽及延迟；跑的是教学引擎，不是宿主 `numactl`，也没有用 `/sys/devices/system/node` 里的数字当作带宽或延迟。

请先自己根据本地 `100` 纳秒、远程 `200` 纳秒、行宽 `64` 字节和窗口 `30.00` 秒，算出单线程同节点与跨节点的带宽，确认同节点是 `640.00` MB/s、`100.00` 纳秒，跨节点是 `320.00` MB/s、`200.00` 纳秒。然后再核对另外三组：只把线程钉到 CPU 0、页却在节点 1 上首次触达时，带宽仍是 `320.00` MB/s；先 MPOL_BIND 再触页，回到 `640.00` MB/s；双线程同节点是 `1280.00` MB/s，忽略拓扑的双线程是 `960.00` MB/s、平均延迟 `150.00` 纳秒。结果里应同时看到教学拓扑（2 个节点）和宿主记录（1 个节点，未调用 `numactl`）。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_pinned_thread_not_local_memory`、`acceptance_reports_topology_and_placement`、`pitfall_threads_without_topology`、`tradeoff_bind_locality_vs_flexibility`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_numa_product`、`not_host_numactl`、`not_host_sysfs_node`、`not_host_perf`、`not_host_gxx`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Linux man-pages — numa(7) 中与 NUMA locality 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://man7.org/linux/man-pages/man7/numa.7.html](https://man7.org/linux/man-pages/man7/numa.7.html) 返回 200、无跳转、13644 字节，页脚为 man-pages 6.19、2026-02-08，`<title>` 为 numa(7) - Linux manual page。NAME 使用 Architecture，正文使用 Access。本课跟随打开了它点名的 set_mempolicy(2)（19642 字节，含 first touch 与 local allocation）、mbind(2)（24441 字节）、get_mempolicy(2)（14532 字节）、numa(3)（53296 字节，numactl 项目，上游取用 2026-08-04）和 numactl(8)（19865 字节，页脚 Mar 2004）。没有把未打开的 cpuset(7) 算进结论。

教学实验按以下方式使用它，并记下差异。

- **NUMA node**：定义来自 numa(7) 正文。本课用声明的双节点拓扑和 `100` / `200` 纳秒把「相对位置影响访问时间」变成可比较的带宽；这两个纳秒和 `640.00` / `320.00` MB/s 都不在手册里。宿主实际是 1 个节点，实验数字不是从该宿主的 sysfs 读出的。
- **first touch**：短语和「匿名页在首次被线程触达时分配」来自 set_mempolicy(2)，不是 numa(7) 正文。默认 local allocation 同时出现在 set_mempolicy(2) 与 numa(3)。本课的跨节点和「只钉线程」两组，都是这一句的对照，不是 `/proc/pid/numa_maps` 的一次真实扫描。numa(7) 对 numa_maps 的字段说明没有进入 `results.json`。
- **绑核与内存策略**：`--cpunodebind` 与 `--membind` 的区分来自 numactl(8)，`numa_run_on_node()` 不再迁移到其他节点来自 numa(3)，MPOL_BIND 来自 set_mempolicy(2)。本课没有调用这些系统调用或命令行工具，也没有演示 migrate_pages(2)。
- **时间含义**：手册没有给出生产环境下的 QPI / 互联延迟。本课全部是教学虚拟时钟上的值，不是生产 RTT，也不是本机跑了三十秒 STREAM 的墙钟结果。两线程带宽相加是教学假设。

## 自测题

**题 1.** 什么是 NUMA node？在本课的教学双节点机器上，同节点运行和跨节点运行的带宽、延迟各是多少？为什么必须先报告拓扑和线程分布？

<details>
<summary>答案</summary>

numa(7) 把 NUMA 写成：内存被分成多个 memory node 的多处理器系统，访问时间取决于 CPU 和被访问节点的相对位置。本地节点通常快于另一个 CPU 的本地节点。

教学实验里，工作线程在 CPU 0、页也在节点 0 时，带宽 `640.00` MB/s，延迟 `100.00` 纳秒。线程改到 CPU 4、页仍在节点 0 时，带宽 `320.00` MB/s，延迟 `200.00` 纳秒。这两个纳秒是教学安排，不是手册或宿主 sysfs 里的数字。

宿主本次记录只有 1 个 NUMA 节点。若不先写出「教学拓扑是 2 个节点、线程在哪颗 CPU、页在哪个节点」，就无法判断这组差异是不是远程访问，也无法避免把单节点机器上的结果外推到多插槽机器。这正是验收要求「报告拓扑与线程分布」的原因。

</details>

**题 2.** 为什么「固定线程不等于内存已本地化」？请用本课「只钉线程」和「先绑定内存再首次触页」两组数字说明。first touch 的官方表述在哪一页？

<details>
<summary>答案</summary>

匿名页的内存策略在首次被线程触达时才生效。set_mempolicy(2) 的原句是：The policy is applied only when a new page is allocated for the thread. For anonymous memory this is when the page is first touched by the thread。系统默认是 local allocation，页会落在当时那颗 CPU 的节点上。numa(7) 正文没有这个短语。

本课中，页在 CPU 4（节点 1）上被首次触达后，再把工作线程钉到 CPU 0：线程已经固定，页却仍在节点 1，访问是远程，带宽仍是 `320.00` MB/s。若在触页之前设定 MPOL_BIND 节点 0，并由 CPU 0 做首次触达，访问回到本地，带宽 `640.00` MB/s。钉核只约束以后在哪运行，并不搬迁已经分配的页。numactl(8) 也把 `--physcpubind` 和 `--membind` 分成两个选项。

</details>

**题 3.** 为什么不能只调线程数、不观察拓扑？绑定局部性的收益和代价分别是什么？哪些数字只是教学安排？

<details>
<summary>答案</summary>

两组双线程：都在节点 0 且页也在节点 0 时，带宽 `1280.00` MB/s，延迟 `100.00` 纳秒。只加一条线程却把它放到 CPU 4，而首次触页仍全部在节点 0 时，带宽只有 `960.00` MB/s，平均延迟 `150.00` 纳秒。加线程看起来有收益，但一半访问是远程的，扩展明显差于同节点的两条线程。这就是「只调线程数不观察拓扑」。

绑定（CPU 与内存策略都在触页前生效）能把单线程从跨节点的 `320.00` 拉回 `640.00`，这是局部性的收益。代价是可调度 CPU 减少：`numa_run_on_node()` 写明在重置亲和性之前不会迁移到其他节点；本课钉在 CPU 0 时，可运行集合从 8 个降到 1 个。

`100` / `200` 纳秒、由此算出的带宽、以及两线程带宽相加，都是教学安排，不是 numa(7) 的示例，也不是宿主测量或生产 RTT。

</details>

## 推荐资源

- [Linux man-pages — numa(7)](https://man7.org/linux/man-pages/man7/numa.7.html)（Linux man-pages 6.19，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，13644 字节，页脚日期 2026-02-08，`<title>` 为 numa(7) - Linux manual page。用于核对 memory node 与「访问时间取决于相对位置」。该页没有定义 first touch，也没有给出本课的带宽数字；教学计数是对照，不是宿主采集。
- 跟随阅读：[set_mempolicy(2)](https://man7.org/linux/man-pages/man2/set_mempolicy.2.html)（同一套 man-pages 6.19，2026-02-08；19642 字节）给出匿名页的 first touch 与默认 local allocation；[numa(3)](https://man7.org/linux/man-pages/man3/numa.3.html) 与 [numactl(8)](https://man7.org/linux/man-pages/man8/numactl.8.html)（numactl 项目，2026-08-04 取自上游）用于核对默认策略、绑核与绑内存的区分，以及绑定后不再向其他节点迁移。

## 本课小结

- 多插槽机器的远程内存访问成本更高。线程绑定与首次触页位置会影响服务稳定性。同样的代码在大机器上扩展变差，往往先要问页在哪个 node。
- 同节点单线程教学带宽是 `640.00` MB/s、延迟 `100.00` 纳秒；跨节点是 `320.00` MB/s、`200.00` 纳秒。双线程同节点是 `1280.00` MB/s；忽略拓扑时只有 `960.00` MB/s。
- 固定线程不等于内存已本地化。页在节点 1 上被首次触达后，再把线程钉到节点 0，带宽仍是 `320.00` MB/s。要先设定内存策略再触页。first touch 的官方表述在 set_mempolicy(2)，不在 numa(7) 正文。
- 绑定能提高局部性，但会缩小可调度的 CPU 集合。先报告拓扑与线程分布，再谈线程数。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 `numactl` 或 `/sys/devices/system/node` 的测量。`NumaEngine` 不是 Redis，也不是 libnuma。宿主本次记录为 1 个 NUMA 节点。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「访问时间取决于相对位置」「匿名页在首次触达时按策略分配」「绑 CPU 和绑内存是两件事」这几段，不要只加线程，也不要把钉核当成内存已经本地化。
