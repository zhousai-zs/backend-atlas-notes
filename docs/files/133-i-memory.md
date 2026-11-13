# 第 133/208 课：堆内存与分配器分析

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-memory` |
| 英文名 | Heap profiling |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 剖析与诊断 |
| 难度 | 深入 |
| 前置节点 | [`i-perf`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html)、[`b-memory`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/28.html) |
| 标签 | `Heap profiling`、`Performance` |
| 参考资料 | [TCMalloc · Heap sampling](https://github.com/google/tcmalloc/blob/master/docs/sampling.md)（Google TCMalloc 仓库文档，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，251433 字节，`Content-Type` 为 `text/html; charset=utf-8`，响应头 `Date` 为 `Fri, 13 Nov 2026 18:03:36 GMT`（北京时间 2026-11-14 02:03:36），`<title>` 为 tcmalloc/docs/sampling.md at master · google/tcmalloc · GitHub。正文原文在 [https://raw.githubusercontent.com/google/tcmalloc/master/docs/sampling.md](https://raw.githubusercontent.com/google/tcmalloc/master/docs/sampling.md)，HTTP 200，无跳转，13630 字节，`Content-Type` 为 `text/plain; charset=utf-8`，`Date` 为 `Fri, 13 Nov 2026 18:03:44 GMT`（北京时间 02:03:44），一级标题为 `# How sampling in TCMalloc works.`，与节点资源标题「TCMalloc · Heap sampling」并不相同。该文件最近一次提交为 `411000090dcf025acc83ebbc4664d841a5ed4bba`，作者栏写 Chris Kennelly CA，说明是 Remove stale proxy object references from sampling documentation，提交时间 `2026-09-09T15:50:58Z`（北京时间 2026-09-09 23:50:58），由 Copybara-Service 在 `2026-09-09T15:51:43Z`（北京时间 23:51:43）提交。仓库 [google/tcmalloc](https://github.com/google/tcmalloc) 默认分支是 `master`，许可证为 Apache-2.0；`LICENSE` 原文 HTTP 200、11360 字节。GitHub API 给出的创建时间为 `2019-08-23T14:02:59Z`（北京时间 2019-08-23 22:02:59），最近一次 push 为 `2026-11-13T17:31:57Z`（北京时间 2026-11-14 01:31:57），`updated_at` 为 `2026-11-13T07:27:43Z`（北京时间 2026-11-13 15:27:43）。sampling.md 定义了堆剖析（遍历仍在堆上的采样对象）、分配剖析（`StartAllocationProfiling` 之后一段时间内的采样分配）和寿命剖析（已观察到的寿命与右删失的部分观察），并区分请求大小与按 size class 取整后的实际分配大小；默认采样间隔是期望每 2 MiB 一次，这是统计期望而不是「每 2 MiB 里恰好有一个采样字节」。它提到 RSS 可能因先前分配用过同一页、或大页把未使用的页也撑成驻留而不准确。该页**没有**把节点里的「in-use 与 allocated bytes」「arena 与碎片」写成独立术语对。本课跟随阅读了它点名的 [malloc_extension.h](https://raw.githubusercontent.com/google/tcmalloc/master/tcmalloc/malloc_extension.h)（HTTP 200，39492 字节，`Date` 为 `Fri, 13 Nov 2026 18:04:06 GMT`，北京时间 02:04:06，版权行 Copyright 2019 The TCMalloc Authors）：`ProfileType::kHeap` 写的是当前堆用量的近似，`kAllocations` 是剖析会话期间的分配样本，`Sample` 里有 `requested_size` 与 `allocated_size`，`generic.bytes_in_use_by_app` 注释为 Bytes currently in use by application，并与 `generic.physical_memory_used` 并列。arena 未分配 / 不可用字节以及 realized fragmentation，写在同一仓库的 [docs/stats.md](https://raw.githubusercontent.com/google/tcmalloc/master/docs/stats.md)（HTTP 200，52620 字节，`Date` 为 `Fri, 13 Nov 2026 18:04:04 GMT`，北京时间 02:04:04，一级标题为 `# Understanding Malloc Stats`），**不在**节点给出的 sampling.md 正文。本课按 stats.md 做对照，并标明出处。没有把未打开的 ACM / ASPLOS 论文算进结论。） |
| 摘要 | 区分泄漏、保留容量、碎片和高分配率；RSS 不等同仍被业务使用的堆对象大小。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」都是**教学假设**，用来让大量小对象、heap profile、in-use 与累计分配、释放后的 RSS 以及持续负载下的平台变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在某台机器上调用了宿主 `tcmalloc`、`jemalloc`、`perf` 或 `g++`。配套代码是 Python 3 标准库写的教学引擎 `HeapEngine`，**不是 Redis，不是真实的剖析器或分配器产品，不是网络服务，不是 Google TCMalloc / jemalloc，也不是宿主机上的 g++ / clang**。它不读取 `/proc/pid/pagemap` 或 `smaps`，不链入官方库，也不把文档里的示例 MiB 计数原样写进 `results.json`。教学 size class 表是对照 sampling.md 里「离散档位」这一做法的类比，不是 `size_classes.cc` 的拷贝。采样间隔 2 MiB 只作为官方默认值的边界说明，本课的教学剖析记录每一次分配，以便用准确计数指出分配来源。`30.00` 是教学虚拟时钟上跑完 `2000` 次请求的时长，不是墙钟三十秒，也不是生产 RTT。`generic.bytes_in_use_by_app` 等属性名来自被 sampling.md 点名的 `malloc_extension.h`；arena 与 realized fragmentation 的定义来自跟随阅读的 `docs/stats.md`，不是 sampling.md 正文。本课所有数字都来自 `/workspace/atlas/lessons/practice133/results.json`，是**教学虚拟时钟上的数字**。

## 导读

### 上一课留下的缺口

上一课的 `FlameEngine` 解决的是：完整的调用栈怎样折成火焰图，短连接和长连接把 CPU 花在 syscall 还是序列化上。那一课的边界停在**CPU 样本的宽度和差分**，没有回答另一件事：请求路径上如果新出了大量小对象，内存是漏了、只是分配器还留着，还是分配率本身很高；RSS 涨了，是不是就等于业务对象还在用。

节点把摘要写成：区分泄漏、保留容量、碎片和高分配率；RSS 不等同仍被业务使用的堆对象大小。只看见进程的驻留集变大，不能据此断言发生了泄漏。

### 接上前置

前置 [第 131 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html) `i-perf` 留下的结论是：先确认符号和展开，再读热点。本课假定分配栈是完整的，用教学版的 heap profile / allocation profile 去找「谁在请求路径上分配」。另一则前置是 [第 28 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/28.html) `b-memory`。它讨论的是 `new` 与 `malloc` 的分工、以及用 `monotonic_buffer_resource` 一类内存资源减少批次请求的分配次数。那一课已经写过：池化能少分配，也可能把高峰内存留下来。本课不再展开对齐和 pmr 接口，只用到「释放存储和对象不再被业务使用不是一回事」：对象已经 `free`，分配器仍可把字节留在 size class 的空闲链里，RSS 不必立刻下降。

官方 sampling.md 把堆剖析写成遍历仍在堆上的采样对象，把分配剖析写成对一段时间窗口内分配的采样，并把请求大小与实际分配大小分开。`malloc_extension.h` 用 `kHeap` 近似当前用量，用 `kAllocations` 记录会话期间的分配，用 `bytes_in_use_by_app` 表示应用当前占用的字节，它不必等于物理驻留。本课用教学引擎复现这些对照，而不是在本机链接 TCMalloc。

### 教学场景（教学假设）

假设内容后台有一个教学 KV：请求打来，就按键 `article:01:title` 取出标题 `Weekly digest`。这一课在处理路径上为每个请求解析出 `64` 个小字段：每个字段请求 `24` 字节，教学 size class 实际给出 `32` 字节；另外为标题准备 `64` 字节的序列化缓冲。剖析在教学虚拟时钟上连续处理 `2000` 次请求，窗口是 `30.00`。对照包括：请求结束时把对象还给分配器、每条请求泄漏 `8` 个字段、以及用对象池复用字段槽。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT，更不是宿主 heap profiler 的墙钟结果。

### 本课三条主线

1. **in-use 与 allocated bytes**：in-use（官方对照是 `kHeap` / `bytes_in_use_by_app`）是此刻仍被业务占用的字节；allocated bytes（官方对照是 `kAllocations` 会话里的累计分配）是这段时间分配过多少。适用边界是：空闲时的 heap 快照会漏掉已经释放的短命对象，要找高分配率的来源，得看分配剖析或负载进行中的堆快照。请求大小和实际分配大小也不是一回事，差额是内部碎片。
2. **arena 与碎片**：教学堆按块向「操作系统」要内存；块里尚未交给业务的部分记为 arena 未分配。realized fragmentation 看的是**峰值**处实际驻留相对当时 in-use 的开销。适用边界是：峰值过后把对象还给缓存，开销占活对象的比例会变大，但不等于又多要了一块内存。
3. **对象生命周期**：寿命剖析区分已经释放的观察值和会话结束时仍活着的右删失值。短命对象会推高分配率，却不一定抬高空闲时的 in-use；泄漏则让删失样本和 in-use 一起增长。

## 学习目标

- 能解释 in-use 与 allocated bytes 及适用边界：前者是当前仍被业务使用的堆对象大小，后者是一段窗口里分配过的字节；RSS 和物理驻留包含缓存与 arena，不等于 in-use。解释图形或报告前，要分清看的是堆快照还是分配剖析。
- 独立完成节点实践：给请求路径加入大量小对象，使用 heap profile 找出分配来源，并用分配剖析核对该来源在整段负载里的累计权重。
- 能说明为什么释放后 RSS 可能不降，以及怎样用持续负载验证稳定平台，而不是只凭 RSS 上升判断泄漏。

## 为什么后端要懂

节点对「为什么」写得很干脆：处理服务内存增长与分配热点。放到本课的教学内容后台：每次取出 `Weekly digest` 都会走过 `64` 个小字段。`2000` 次请求里，分配剖析统计到 `128000` 次 `alloc_field`，累计分配 `4096000` 字节，占会话分配的 `0.9697`，这是分配来源。但若在全部请求结束再拍一张 heap 快照，in-use 是 `0`，来源会消失。真正的泄漏会让 in-use 随着持续负载从 `256` 涨到 `512000`；只把对象还给空闲链时，in-use 回到 `0`，RSS 却仍是 `4096`。不会区分这三种情况，就不能判断该修泄漏、该归还缓存，还是该减少热路径上的小对象分配。

## 核心知识点

### 1. in-use 与 allocated bytes

节点里的「in-use 与 allocated bytes」是一组教学用语。官方 sampling.md 用的是堆剖析和分配剖析两个过程；`malloc_extension.h` 把前者写成 `kHeap`（当前堆用量的近似），把后者写成 `kAllocations`（从 `StartAllocationProfiling` 到 `Stop` 的分配样本），并把 `generic.bytes_in_use_by_app` 注释为应用当前占用的字节。本课按这一对照使用这两个词，而不把它们写成 sampling.md 正文里现成的标题。

同一条请求进行中，教学 heap 快照给出 in-use `2112`：`alloc_field` 占用 `2048`（`64` 个对象，请求合计 `1536`，内部碎片 `512`），`serialize_title` 占用 `64`。来源就是 `alloc_field`，份额 `0.9697`。请求结束并归还之后，同一张 `kHeap` 快照是空的。整段 `30.00` 的分配剖析则记下 `130000` 次分配、累计 allocated bytes `4224000`、请求合计 `3200000`，来源仍是 `alloc_field` 的 `128000` 次、`4096000` 字节。短命的高分配率，要靠分配剖析或负载中的堆快照才能看见。

`Sample.requested_size` 与 `Sample.allocated_size` 是另一对官方字段：本课请求 `24`、实际得到 `32`，差额记入内部碎片。不要把「allocated size」和「allocated bytes（一段窗口的累计分配）」混成同一个意思。官方默认采样间隔是期望每 2 MiB 一次；这段教学负载一共分配 `4224000` 字节，按该间隔估计只有 `2.0142` 个样本，不够用来点名来源。教学引擎因此记录每一次分配，不把 2 MiB 抽样当作实验结果。

### 2. arena 与碎片

这两个名字以及「Bytes in malloc metadata Arena unallocated / unavailable」「realized fragmentation」写在 `docs/stats.md`，不在 sampling.md。stats.md 把「Bytes in use by application」写成应用正在用来放数据的字节，把页堆和各级 cache 里尚未交给业务的字节分开，并提醒 Actual memory used 对 RSS 的贡献可能因尚未缺页或透明大页而偏小或偏大。Arena unallocated 是为了摊还元数据而向操作系统多要、但尚未切出去的块内空隙。

教学堆按 `4096` 字节一块要内存。一次请求的峰值 in-use 是 `2112`，物理块是 `4096`，块里还剩 arena 未分配 `1984`。请求结束后 in-use 回到 `0`，`2112` 留在 size class 空闲链，arena 未分配仍是 `1984`，RSS 仍是 `4096`。realized fragmentation 按峰值处计算：`(4096 - 2112) / 4096 = 48.4375%`。stats.md 写过：峰值过后内存可能留在缓存里等复用，开销相对剩下的活对象会升高，但并不需要再多预备一块内存。所以这个比例要看峰值，不要在空闲时用 RSS / in-use 去吓自己。

泄漏对照里，峰值物理是 `516096`，峰值 in-use 是 `513856`，realized fragmentation 只有 `0.434%`：驻留大体跟着活对象走，问题不在碎片，而在 in-use 本身不断上升。

### 3. 对象生命周期

sampling.md 把寿命剖析分成两类：对象在会话里被释放，寿命是已观察值；会话结束时对象还活着，寿命被右删失。教学短命对象的已观察平均寿命是一次请求的 `15.00` ms，健康负载下没有删失样本。泄漏路径里，每次请求留下 `8` 个字段，会话结束时有 `16000` 个删失样本，平均寿命 `15007.5` ms，同时还有 `114000` 个已观察样本，平均仍是 `15.00` ms。空闲 heap 快照上的来源是 `alloc_field` 的 `512000` 字节，这才是仍被占用的堆。

适用边界是：寿命长、in-use 随负载单调上升，才像泄漏；寿命短、分配剖析很热、空闲 in-use 却回到平台，是高分配率。两者都可能让 RSS 在第一段负载里升高，但不能用同一句话概括。

## 常见误区

节点写出的误区是：仅凭 RSS 上升判断泄漏。健康负载在第一次请求之后，RSS 就停在 `4096`，之后 `2000` 次请求的 in-use 平台都是 `0`，RSS 不再涨。它上升过，是因为分配器留下了 `2112` 字节的空闲链和 `1984` 字节的 arena 空隙，不是因为对象没被释放。泄漏对照里，in-use 从 `256` 一直升到 `512000`，RSS 从 `4096` 升到 `516096`，持续负载验证时平台并不稳定。只看 RSS 曾经变大，会把这两种情况看成同一件事。要先看 in-use 是否随负载回到同一水平，再看 heap 快照里还有没有活着的来源。

## 设计权衡

节点写出的权衡是：池化减少分配，但可能延长驻留与增加清理难度。这与第 28 课对内存资源的权衡是同一方向。字段改走对象池之后，分配器上的 `malloc` 次数从 `130000` 降到 `2064`（池预热 `64` 次，加上每次请求仍要分配的标题缓冲 `2000` 次），分配剖析的来源变成了 `serialize_title`，`alloc_field` 不再出现在会话的分配样本里。池里的字段槽在请求结束后仍被保留，in-use 是 `0`，retained 仍是 `2112`，RSS 仍是 `4096`。分配热点被挪走了，驻留和清理责任却留了下来：要让 RSS 下降，不能只把对象还回池子，还得决定何时清空空闲链并真正交给操作系统。

教学对照里，对象已经还给 size class 空闲链之后，直接做一次不刷新缓存的 `ReleaseMemoryToSystem`，RSS 仍是 `4096`，一块内存也没有被卸掉。先清空空闲链再释放，RSS 才从 `4096` 降到 `0`，并记下 `4096` 字节的 unmapped。释放存储、清空缓存、归还操作系统，是三步，不是一步。

## 动手实践：给请求路径加入大量小对象，使用 heap profile 找出分配来源

配套代码在 [`/workspace/atlas/lessons/practice133/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice133.tar.gz)。`engine.py` 是 `HeapEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_heap.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，给请求路径加入大量小对象，使用 heap profile 找出分配来源；跑的是教学引擎，不是宿主 tcmalloc，也不是宿主 g++。

请先自己把一次请求进行中的 heap 快照、全部请求结束后的空闲快照，以及 `30.00` 窗口里的分配剖析记进一张表，确认进行中的来源是 `alloc_field`（`2048` / `0.9697`），空闲快照的 in-use 是 `0`，分配剖析里 `alloc_field` 有 `128000` 次、`4096000` 字节。然后再核对另外三组：健康负载下 in-use 平台为 `0`、RSS 停在 `4096`，不刷新缓存就释放时 RSS 仍是 `4096`，清空后再释放才降到 `0`；泄漏时 in-use 从 `256` 升到 `512000`，空闲 heap 快照仍能看见 `alloc_field`；对象池把分配次数降到 `2064`，但 retained 仍是 `2112`。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_explains_rss_after_free`、`acceptance_steady_load_platform`、`pitfall_rss_rise_is_not_leak`、`tradeoff_pool_retains_resident`、`practice_found_allocation_source`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_allocator_product`、`not_host_tcmalloc`、`not_host_jemalloc`、`not_host_perf`、`not_host_gxx`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 TCMalloc — Heap profiling 中与 Heap profiling 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://github.com/google/tcmalloc/blob/master/docs/sampling.md](https://github.com/google/tcmalloc/blob/master/docs/sampling.md) 返回 200、无跳转、251433 字节，`<title>` 为 tcmalloc/docs/sampling.md at master · google/tcmalloc · GitHub。正文在 raw 地址，13630 字节，一级标题为 How sampling in TCMalloc works.，与资源条目里的「TCMalloc · Heap sampling」不一致。最近一次文档提交者为 Chris Kennelly CA，时间为 2026-09-09 23:50:58（北京时间）。本课跟随打开了该页点名的 `malloc_extension.h`（39492 字节），并阅读了同一 `docs/` 下、但未被 sampling.md 点名的 `stats.md`（52620 字节），用来补 arena 与 realized fragmentation。没有再把未打开的 ASPLOS 2020 论文算进结论。

教学实验按以下方式使用它，并记下差异。

- **in-use 与 allocated bytes**：这两个名字是节点用语。官方对照是 sampling.md 里的堆剖析 / 分配剖析，以及 `malloc_extension.h` 里的 `kHeap`、`kAllocations` 和 `bytes_in_use_by_app`。本课用准确计数复现：负载中 heap 快照找到 `alloc_field`，空闲快照找不到，分配剖析在 `30.00` 里给出 `128000` 次和 `4096000` 字节。文档默认按期望 2 MiB 采样，本课不把 `2.0142` 个估计样本当作来源证据。
- **requested 与 allocated size**：sampling.md 的附录和 `Sample` 结构都区分请求大小与实际分配大小。教学 size class 把 `24` 升到 `32`，内部碎片在单次请求里是 `512`。这张表是教学类比，不是官方 `size_classes.cc` 的拷贝。
- **arena 与碎片**：定义来自 `docs/stats.md`，不在 sampling.md。教学块大小 `4096`、arena 未分配 `1984`、峰值 realized fragmentation `48.4375%`，都是教学模型，不是该页示例里的 MiB 计数。本课不实现 Temeraire、透明大页或 `/proc/pid/pagemap` 驻留探测。
- **对象生命周期**：sampling.md 定义了已观察寿命与右删失。教学短命对象的已观察平均寿命是 `15.00` ms（一次请求的虚拟时长），泄漏对象的删失平均寿命是 `15007.5` ms。文档里的寿命剖析还区分会话开始前就已采样的对象，本课没有做这一截。
- **时间含义**：官方分配剖析是「开始、等待一段时间、再 Stop」。本课的 `30.00` 是教学虚拟时钟上 `2000` 次请求跑完的时长，不是墙钟三十秒，也不是生产 RTT，更不是本机链入 tcmalloc 后的实测。

## 自测题

**题 1.** in-use 和 allocated bytes 各回答什么问题？在本课给请求路径加上 `64` 个小对象之后，怎样用 heap profile 找出分配来源？适用边界在哪里？

<details>
<summary>答案</summary>

in-use 对应官方的 `kHeap` / `bytes_in_use_by_app`，回答「此刻还有哪些对象、多大」。allocated bytes 对应 `kAllocations`，回答「这段窗口里分配过多少」。请求大小和实际分配大小是另一对字段：本课请求 `24`、实际 `32`。

一次请求进行中的教学 heap 快照里，in-use 是 `2112`，来源是 `alloc_field` 的 `2048` 字节（`64` 个对象，份额 `0.9697`），`serialize_title` 只有 `64`。全部请求结束后再拍一张，in-use 是 `0`，来源消失。同一段 `30.00` 的分配剖析记下 `130000` 次分配，其中 `alloc_field` 有 `128000` 次、`4096000` 字节，份额仍是 `0.9697`。所以「找出分配来源」要看负载中的堆快照，或看整段分配剖析，不能只看空闲时的 heap 快照。

适用边界是：heap 快照回答的是当前占用，不代替累计分配率；官方 2 MiB 的默认间隔在这段只有 `4224000` 字节的教学负载里，估计仅 `2.0142` 个样本，不够用来点名来源。RSS 是物理驻留，也不等于 in-use。

</details>

**题 2.** 为什么对象释放之后 RSS 可能不降？怎样用持续负载验证稳定平台？为什么不能只凭 RSS 上升判断泄漏？

<details>
<summary>答案</summary>

健康负载里，每次请求结束都会把对象还给 size class 空闲链。in-use 回到 `0`，但 retained 仍是 `2112`，再加上 arena 未分配的 `1984`，RSS 停在 `4096`。这是分配器留下的保留容量，不是业务对象还活着。此时即使调用一次不刷新缓存的释放接口，RSS 仍是 `4096`，一块也卸不掉；先清空空闲链再释放，RSS 才降到 `0`。

持续负载的验证看的是 in-use 平台。健康路径上 `2000` 次请求每次结束都是 `0`，平台稳定。泄漏路径每次留下 `8` 个字段，in-use 从 `256` 单调升到 `512000`，空闲 heap 快照仍能看见 `alloc_field` 的 `16000` 个对象，这才是泄漏。RSS 在健康路径上也会在第一次请求后升到 `4096`，所以「RSS 上升过」不能单独当作泄漏证据。

</details>

**题 3.** 池化怎样减少分配、又怎样延长驻留？请用本课的分配次数、retained 和 RSS 说明，并指出和内部碎片、realized fragmentation 的差别。

<details>
<summary>答案</summary>

字段改走对象池之后，分配器上的 `malloc` 从 `130000` 次降到 `2064` 次，会话里的分配剖析来源变成 `serialize_title` 的 `2000` 次。池预热和标题缓冲仍然要分配，并不是零分配。请求结束后 in-use 是 `0`，但池和空闲链仍保留 `2112` 字节，RSS 仍是 `4096`。少掉的是分配次数，不是驻留；清理时还要决定何时真正把这些字节还给操作系统。

内部碎片是请求 `24`、实际 `32` 的差额，单次请求合计 `512`，它算在 in-use 里面。realized fragmentation 看峰值处物理相对 in-use 的开销，本课健康路径是 `48.4375%`，来自 `4096` 的块和峰值 in-use `2112`。泄漏时这个比例只有 `0.434%`，因为驻留大体跟着活对象走。池化改变的是分配次数和保留容量，不是把内部碎片算进泄漏。

</details>

## 推荐资源

- [TCMalloc · sampling.md](https://github.com/google/tcmalloc/blob/master/docs/sampling.md)（Google TCMalloc 文档，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，页面 251433 字节，正文 13630 字节，一级标题为 How sampling in TCMalloc works.，最近一次文档提交者为 Chris Kennelly CA，时间为 2026-09-09 23:50:58（北京时间）。用于核对堆剖析、分配剖析、寿命剖析以及请求大小与实际分配大小。本课的教学引擎、虚拟时钟上的 `30.00` 和逐次记录的分配计数，都是对照，不是在宿主上链接 TCMalloc，也不是按默认 2 MiB 间隔抽样后的估计值。
- [malloc_extension.h](https://github.com/google/tcmalloc/blob/master/tcmalloc/malloc_extension.h)（被 sampling.md 点名的头文件，英文，深入，免费）：2026-11-14 raw 地址返回 200，39492 字节，版权行 Copyright 2019 The TCMalloc Authors，许可证 Apache-2.0。该文件给出 `ProfileType::kHeap` / `kAllocations` / `kLifetimes`、`bytes_in_use_by_app` 与 `physical_memory_used`，以及 `requested_size` / `allocated_size`。节点用语「in-use 与 allocated bytes」按这里的对照来读，而不是当作 sampling.md 里的现成标题。
- [Understanding Malloc Stats](https://github.com/google/tcmalloc/blob/master/docs/stats.md)（同一仓库的统计说明，英文，深入，免费）：2026-11-14 raw 地址返回 200，52620 字节。该页定义 Bytes in use by application、metadata Arena unallocated / unavailable，以及峰值处的 realized fragmentation。这些内容不在节点给出的 sampling.md 正文，本课按它解释释放后 RSS 不降和碎片，但不复制页内示例的 MiB 计数。

## 本课小结

- 要区分泄漏、保留容量、碎片和高分配率。RSS 不是仍被业务使用的堆对象大小。
- 给取 `Weekly digest` 的路径加上 `64` 个小对象后，负载中的 heap 快照和 `30.00` 的分配剖析都指向 `alloc_field`（`128000` 次、`4096000` 字节，份额 `0.9697`）。空闲时的 heap 快照 in-use 是 `0`，会把这个来源漏掉。
- 健康负载下 in-use 平台为 `0`，RSS 因空闲链 `2112` 和 arena 未分配 `1984` 停在 `4096`。不刷新缓存就释放，RSS 仍不降；清空后再释放才降到 `0`。泄漏时 in-use 从 `256` 升到 `512000`，持续负载的平台不稳定。
- 池化把分配次数从 `130000` 降到 `2064`，但 retained 仍是 `2112`。内部碎片来自 `24` 升到 `32`；realized fragmentation 要看峰值，本课健康路径是 `48.4375%`。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 tcmalloc 或文档示例里的墙钟计数。`HeapEngine` 不是 Redis，也不是真实的分配器产品。「in-use 与 allocated bytes」是节点用语，官方对照来自 sampling.md 与 `malloc_extension.h`；arena 与 realized fragmentation 的定义来自跟随阅读的 `docs/stats.md`。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「堆剖析看当前占用、分配剖析看一段窗口」「RSS 可能因页和缓存而不准确」以及「峰值处的碎片才是预备量」这几段，不要把空闲快照当成没有分配热点，也不要把 RSS 上升当成泄漏。
