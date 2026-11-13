# 第 137/208 课：CPU 缓存与伪共享

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-cache` |
| 英文名 | False sharing |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 硬件与IO优化 |
| 难度 | 深入 |
| 前置节点 | [`i-locks`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/134.html)、[`e-cache`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/71.html) |
| 标签 | `False sharing`、`Performance` |
| 参考资料 | [Linux Kernel — False Sharing](https://www.kernel.org/doc/html/latest/kernel-hacking/false-sharing.html)（Linux 内核文档，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，22125 字节，`Content-Type` 为 `text/html; charset=utf-8`，`Last-Modified` 为 `Mon, 09 Nov 2026 00:37:39 GMT`（北京时间 2026-11-09 08:37:39），响应头 `Date` 为 `Fri, 13 Nov 2026 18:35:53 GMT`（北京时间 2026-11-14 02:35:53）。`<title>` 为 False Sharing — The Linux Kernel documentation，页内 `h1` 为 False Sharing 与 The Linux Kernel，页头版本为 **7.4.0-rc2**。页脚为 The kernel development community，由 Sphinx 5.3.0、Alabaster 0.7.16 生成，源文件 SPDX 为 GPL-2.0，无单独作者署名。配套 rst 源 [false-sharing.rst.txt](https://www.kernel.org/doc/html/latest/_sources/kernel-hacking/false-sharing.rst.txt) 返回 200、9446 字节，`Last-Modified` 为 `Sun, 01 Dec 2024 22:32:43 GMT`（北京时间 2024-12-02 06:32:43），`Date` 为 `Fri, 13 Nov 2026 18:36:11 GMT`（北京时间 02:36:11）。该页把 false sharing 写成与「在多个 CPU 的缓存中维护同一缓存行一致性」有关：refcount 被一个 CPU 频繁改写、name 只在创建时写一次，其他 CPU 读 name 时仍必须一遍遍重新装入整行。有害的伪共享有两个关键因素：被许多 CPU 访问的全局数据，以及并发访问中至少有一次写（write/write 或 write/read）。它写明 cache line is usually 64 bytes or more，并不是把 64 写成所有平台的常量。检测建议在 perf record/report/stat 找到热点后，再用 perf-c2c 和 pahole；示例输出含 Load Local HITM、Load Remote HITM 以及数据在行内的偏移。缓解措施须 balance performance gains with complexity and space consumption：把热点放进独占缓存行会消耗更多内存、缓存行和 TLB。该页没有展开 HITM 这个缩写，把 perf-c2c 的介绍指向 [3] [C2C - False Sharing Detection in Linux Perf](https://joemario.github.io/blog/2016/09/01/c2c-blog/)（2026-11-14 返回 HTTP 200，无跳转，28578 字节，`Last-Modified` 为 `Tue, 11 Apr 2017 13:57:58 GMT`（北京时间 2017-04-11 21:57:58），`Date` 为 `Fri, 13 Nov 2026 18:36:46 GMT`（北京时间 02:36:46）。`<title>` 为 C2C - False Sharing Detection in Linux Perf - My Octopress Blog，页内 `h1` 含 C2C - False Sharing Detection in Linux Perf，作者 Joe Mario，正文时间 `2016-09-01T14:54:55-04:00`（北京时间 2016-09-02 02:54:55）。该文把 HITM 定义为 a load that hit in a modified cacheline；Local HITM 在同一 NUMA 节点，Remote HITM 跨节点，后者最贵。学术定义 [1] 指向 [Wikipedia: False sharing](https://en.wikipedia.org/wiki/False_sharing)（同日 200，108739 字节，`Last-Modified` 为 `Sun, 08 Nov 2026 01:53:32 GMT`，北京时间 2026-11-08 09:53:32，响应头 `Date` 为 `Fri, 13 Nov 2026 15:16:02 GMT`，北京时间 00:16:02，带 `age: 12044` 的缓存命中，`<title>` 为 False sharing - Wikipedia）。本课的定义以内核文档正文为准，没有把 [2] 的 lore 邮件或文中列出的六次内核提交当作实验数字来源。） |
| 摘要 | 不同线程更新同一缓存行内的独立数据也会争夺所有权；结构布局能显著改变一致性流量。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」「每线程请求计数器」「队列 head / tail」「无竞争递增 `5` 纳秒、HITM 递增 `50` 纳秒」都是**教学假设**，用来让缓存行、HITM、以及 padding 与空间开销变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在本机运行了 `perf c2c`、`pahole`、`g++`、cachegrind 或读取了 CPU 性能计数器。配套代码是 Python 3 标准库写的教学引擎 `CacheEngine`，**不是 Redis，不是真实的剖析器或缓存产品，不是网络服务，不是 Linux perf c2c，也不是宿主上的 g++ / cachegrind**。本次运行记录的宿主 `uname -r` 为 `6.12.113+`，`getconf LEVEL1_DCACHE_LINESIZE` 与 sysfs 的 coherency_line_size 均为 `64`，这些只作为宿主事实，**没有**写进行宽结论，也没有调用本机的 `perf`。行宽是教学扫描里、使 HITM 消失的最小 2 的幂次 padding，结果为 `64` 字节；官方用语是 usually 64 bytes or more，扫描本身是粗粒度的（8 / 16 / 32 / 64 / 128），并不声称测到了 48 到 63 之间的精确值。内核文档示例里的 1658231 条记录、博客里的 3402 / 12757 等 HITM 计数都没有进入 `results.json`。HITM 的定义来自文档点名的 c2c 介绍文；HITM 本身不能区分真实共享与伪共享，区分靠同一缓存行上的偏移是否相同。`30.00` 是教学虚拟时钟上的三十秒，不是墙钟 `perf c2c record sleep 3`，也不是生产 RTT。本课所有数字都来自 `/workspace/atlas/lessons/practice137/results.json`。

## 导读

### 上一课留下的缺口

上一课的 `NumaEngine` 已经说明：远程节点上的内存比本地更贵，钉住线程也不等于页已经本地化。那一课的边界停在**节点之间的访问距离**，没有进入同一节点上、更小的那一层：两个线程写的是各自独立的字段，只要它们落在同一缓存行里，也会来回争夺行的所有权。

节点把摘要写成：不同线程更新同一缓存行内的独立数据也会争夺所有权；结构布局能显著改变一致性流量。计数器和队列元数据如果挤在一行里，吞吐会先在一致性流量上崩掉，而不是先在锁或远程内存上崩掉。

### 接上前置

前置 [第 134 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/134.html) `i-locks` 留下的结论是：临界区会把工作串行化，CPU 不高也可能有严重等待。本课不再展开 futex 与分片锁，只用到「独立更新按理不该互相等待」这一条：两个 atomic 计数器没有数据竞争，却仍可能因为同一缓存行而彼此拖累。

另一则前置是 [第 71 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/71.html) `e-cache`。它已经说明缓存按行维护所有权，无数据竞争仍可有伪共享，并且不能把 64 字节无条件写成所有平台的行宽。本课把这几条接到官方内核文档和 perf-c2c 的口径上：用 HITM 与行内偏移区分真实共享和伪共享，并用一次 padding 扫描来测量行宽，而不是事先假定。

官方文档把伪共享放在「多份缓存里同一行的一致性」上，检测推荐 perf-c2c，缓解必须权衡空间消耗。本课用教学引擎复现这几条口径，而不是在本机执行 `perf c2c record`。

### 教学场景（教学假设）

假设内容后台的教学 KV `teachkv` 在处理 `Weekly digest`（键为 `article:01:title`）时，为每个工作线程准备一个 atomic 请求计数器，队列则用 head / tail 两个字段记录生产与消费位置。窗口是教学虚拟时钟上的 `30.00` 秒。无竞争时，一次递增花费 `5` 纳秒；若这次访问命中另一颗 CPU 上已 Modified 的缓存行（HITM），则花费 `50` 纳秒。比较两种布局：两个 8 字节计数器紧挨着，以及在它们之间插入扫描得到的 padding。再用同一套口径对照「两个线程写同一个计数器」（真实共享）和「写相邻但独立的计数器」（伪共享），并对 8 / 16 / 32 / 64 / 128 字节的 padding 做一次扫描。另外统计只隔离写热点、以及给每个字段都 padding 时的工作集。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT。

### 本课三条主线

1. **缓存行**：一致性以行为单位，而不是以字段为单位。适用边界是：两个逻辑上独立的变量只要落在同一行，写其中之一就会让别的 CPU 重新装入整行；行宽通常是 64 字节或更大，但要用测量来解释结果，而不是把 64 当成公理。
2. **HITM**：一次 load 命中了另一份缓存里处于 Modified 状态的行。适用边界是：HITM 只说明「这行在别人手里是脏的」，要靠行内偏移区分真实共享（同一偏移）和伪共享（同一行、不同偏移）。
3. **padding 与空间开销**：把写热点隔到不同缓存行，能减少一致性流量，但会占用更多缓存行。适用边界是：只隔离经常被不同线程写入的字段；每个字段都 padding，会让工作集暴涨，却不一定再带来吞吐收益。

## 学习目标

- 能解释缓存行及适用边界：一致性按行维护所有权；布局会改变 HITM 与吞吐；行宽要用测量来说明，而不是固定写死。
- 独立完成节点实践：对比相邻 atomic 计数器与隔离布局，并用教学版的 perf c2c 口径验证。本课没有在宿主上调用 `perf c2c`。
- 能排除真实共享与伪共享的混淆，并说明为什么不要给每个字段都 padding。

## 为什么后端要懂

节点对「为什么」写得很干脆：避免计数器与队列元数据导致吞吐崩塌。放到本课的教学内容后台：两个线程各自递增自己的请求计数器，按理互不影响。若这两个 8 字节计数器紧挨着，`30.00` 秒里合计只完成 `1200000000` 次递增，吞吐 `40000000.00` 次 / 秒，教学 c2c 记到 Load Local HITM `1200000000`。把它们隔开之后，同样的窗口完成 `12000000000` 次，吞吐 `400000000.00` 次 / 秒，HITM 为 `0`，差了十倍。队列的 head 与 tail 紧挨时，数字完全一样：生产与消费写的是不同字段，却在争夺同一行。八个线程挤在一行里，吞吐是 `160000000.00`；各自独占一行则是 `1600000000.00`。看不见布局，就会把崩掉的吞吐误判成锁、调度或「大机器扩展性」。

## 核心知识点

### 1. 缓存行

官方文档把伪共享放在缓存机制上：同一缓存行的数据可能同时存在于多个 CPU 的缓存里，一致性要维护这行的单一事实。例子是带 refcount 和 name 的结构挤在一行里，refcount 频繁被一个 CPU 改写，name 创建后不再改；其他 CPU 只读 name，却仍必须一遍遍重新装入整行。有害的伪共享要同时满足：数据被许多 CPU 访问，并且并发里至少有一次写。

本课的相邻布局就是这句话的计数器版本：CPU 0 写偏移 `0x0`，CPU 1 写偏移 `0x8`，两个字段逻辑独立，却共享教学地址 `0x1000` 所在的那一行，HITM 为 `1200000000`，吞吐只有 `40000000.00`。文档写 cache line is usually 64 bytes or more。本课没有把 64 当作事先给定的行宽，而是对 8 / 16 / 32 / 64 / 128 字节的 padding 做扫描：前三档仍在同一行，HITM 保持 `1200000000`；到 `64` 时 HITM 降为 `0`，吞吐升到 `400000000.00`，`128` 也是同样结果。于是测得的行宽是 `64` 字节，依据是「使 HITM 消失的最小 2 的幂次 padding」，而不是宿主 sysfs 里的那个 64，也不是把文档里的 usually 抄成常量。扫描是粗粒度的，不能据此断言行宽恰好不能是 48 或 56。

适用边界是：先测量布局改变是否消除了 HITM，再用这个结果解释吞吐；不要在报告里写死「所有机器都是 64 字节，所以我 padding 64」。

### 2. HITM

内核文档的 perf-c2c 示例列出了 Load Local HITM 与 Load Remote HITM，但没有解释缩写。它指向的介绍文写明：HITM stands for a load that hit in a modified cacheline。Local HITM 是命中同一 NUMA 节点上已 Modified 的行，Remote HITM 跨节点，后者更贵。本课的教学拓扑只使用同一节点上的 CPU 0 与 CPU 1（以及 0 到 7），因此 Remote HITM 为 `0`，Local HITM 随布局变化。

单看吞吐，会把真实共享和伪共享混在一起。两个线程写同一个计数器（真实共享）时，吞吐同样是 `40000000.00`，HITM 同样是 `1200000000`；两个线程写相邻但独立的计数器（伪共享）时，这两个数字完全相同。差别只在教学 c2c 的偏移：真实共享两条记录都是 `0x0`；伪共享是 `0x0` 与 `0x8`，同一行、不同字段。这正是文档建议用 perf-c2c 的 in-line offset、再用 pahole 去对成员的原因，也是节点验收写的「排除真实共享与伪共享混淆」。HITM 是「这行在别人手里是脏的」的信号，不是伪共享的充分条件。介绍文把 HITM 直接说成 false sharing 已经发生，本课把这一步标成需要用偏移再确认的差异。

本课没有在宿主上采样，也没有使用文档示例或博客输出里的 HITM 计数。那些是内核 will-it-scale 与作者一次真实运行的样本，不是 `results.json` 的来源。

### 3. padding 与空间开销

文档给出的常见缓解是：把热的全局数据放到独占的缓存行，哪怕只是一个 short；代价是 more consumption of memory, cache line and TLB entries。重组结构、把互相干扰的成员分开，也可能给其他成员带来新的伪共享。编码时要意识到缓存行边界，把大多只读的字段放在一起，把同时写的字段放在一起，并把频繁读与频繁写的字段分到不同行。它同时提醒：并非所有伪共享都值得修，缓解必须权衡复杂度与空间，不要去过度优化冷路径。

本课用工作集把这条权衡写具体。8 个写热点计数器若紧挨着，只需 `64` 字节、1 行，但 8 个线程的吞吐只有 `160000000.00`，HITM 为 `4800000000`。只给这 8 个热点各留一行，变为 `512` 字节、8 行（膨胀 `8.0000` 倍），吞吐升到 `1600000000.00`，HITM 为 `0`。若结构里还有 8 个只读字段：只隔离写热点时，工作集是 `576` 字节、9 行；每个字段都 padding，则是 `1024` 字节、16 行，相对全部挤在一起的 `128` 字节膨胀 `8.0000` 倍，相对「只隔离热点」还要再乘 `1.7778`，而吞吐并不会比只隔离热点更高。这就是节点写的误区：每个字段都 padding 导致工作集暴涨。隔离写热点减少一致性开销，同时增加缓存占用——收益和代价要放在同一张表里看。

## 常见误区

节点写出的误区是：每个字段都 padding 导致工作集暴涨。只读的 name、flag 或配置字段，本来可以和同类只读数据待在一行里；给它们也各留一行，行数从 9 走到 16，字节从 `576` 走到 `1024`，并没有消掉额外的 HITM。文档还指出，把互相干扰的成员拆开之后，新的邻居可能再次伪共享，所以「一律 padding」既贵，也不保证一劳永逸。正确的顺序是：先用（教学）c2c 看到哪几个偏移在争同一行，再只隔离那些写热点，并记下工作集增加了多少。

## 设计权衡

节点写出的权衡是：隔离写热点减少一致性开销，同时增加缓存占用。8 个计数器从 1 行变成 8 行，HITM 从 `4800000000` 降到 `0`，吞吐从 `160000000.00` 升到 `1600000000.00`，这是一致性上的收益；多出去的 7 行和 `8.0000` 倍的热点占用，是缓存与 TLB 上的代价。队列的 head / tail 也是同一笔账：隔开之后吞吐从 `40000000.00` 回到 `400000000.00`，布局从 16 字节变成 80 字节。文档说 Sometimes, lower performance is OK——若这条路径很冷，就不值得为它付行数。选择隔离之前，要把 HITM、吞吐和工作集放在一起看，而不是只宣布「已经对齐到缓存行」。

## 动手实践：对比相邻 atomic 计数器与隔离布局，使用 perf c2c 验证

配套代码在 [`/workspace/atlas/lessons/practice137/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice137.tar.gz)。`engine.py` 是 `CacheEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_cache.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，对比相邻 atomic 计数器与隔离布局，并用 perf c2c 的口径（Local / Remote HITM 与行内偏移）验证；跑的是教学引擎，不是宿主 `perf c2c`，也不是 `pahole`。

请先自己根据无竞争 `5` 纳秒、HITM `50` 纳秒和窗口 `30.00` 秒，算出两线程在相邻布局与隔离布局下的合计吞吐，确认相邻是 `40000000.00` 次 / 秒、Local HITM `1200000000`，隔离是 `400000000.00`、HITM `0`。然后再核对另外三组：真实共享与伪共享的吞吐相同，但偏移分别是两个 `0x0` 与 `0x0` / `0x8`；padding 扫描在 `64` 字节处首次使 HITM 清零，测得行宽 `64`，且该值不是事先假定的；给 8 个写热点全部 padding 会把工作集从 `64` 字节扩到 `512` 字节，再给每个只读字段也 padding 会到 `1024` 字节。宿主记录里 `l1d_line_size` 为 `64`，但标成了「不是结果来源」，`host_perf_c2c_invoked` 为假。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_distinguishes_true_and_false_sharing`、`acceptance_measured_line_not_assumed`、`pitfall_pad_every_field`、`tradeoff_isolation_vs_occupancy`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_cache_product`、`not_host_perf`、`not_host_perf_c2c`、`not_host_pahole`、`not_host_gxx`、`not_host_cachegrind`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 Linux Kernel — False Sharing 中与 False sharing 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://www.kernel.org/doc/html/latest/kernel-hacking/false-sharing.html](https://www.kernel.org/doc/html/latest/kernel-hacking/false-sharing.html) 返回 200、无跳转、22125 字节，页头内核文档版本为 7.4.0-rc2，`<title>` 为 False Sharing — The Linux Kernel documentation。rst 源 9446 字节，`Last-Modified` 为 2024-12-01 22:32:43 GMT（北京时间 2024-12-02 06:32:43）。本课跟随打开了它作为 [3] 列出的 Joe Mario 介绍文（28578 字节，2016-09-01）以及作为学术定义 [1] 列出的 Wikipedia 页（108739 字节，带缓存 Date）。没有把 [2] 的 lore 邮件或文中六次提交的补丁差当作实验依据。

教学实验按以下方式使用它，并记下差异。

- **缓存行**：定义和 refcount / name 的例子来自文档正文。本课改用 `teachkv` 的请求计数器与队列 head / tail，没有复现 `mm_struct.mmap_lock` 或 `mem_cgroup` 那些内核结构。行宽「通常是 64 字节或更大」被用来对照扫描结果，而不是当作输入常量。文档示例中的 perf-c2c 表格没有进入 `results.json`。
- **HITM**：缩写定义来自 [3]，不是内核页正文。本课用教学 Local HITM 计数和行内偏移来区分真实共享与伪共享；Remote HITM 在教学单节点里为 `0`。介绍文把 HITM 直接当成伪共享已经发生的关键信号，本课多写了「还要看偏移」这一步。本课没有调用宿主 perf，也没有使用 `-ag sleep 3` 或博客中的采样参数。
- **padding 与空间开销**：独占一行会多占内存、缓存行和 TLB，这句话来自 Possible Mitigations。本课的 `64` / `512` / `576` / `1024` 字节是教学布局的算术，不是一次 pahole 解码，也不是文档里某个补丁的 sizeof。文档还提到结构随机化会打乱成员的行内位置，本课没有模拟这项可选机制。
- **时间含义**：文档和博客里的 HITM 计数来自真实采样窗口。本课全部是教学虚拟时钟上的值，不是生产 RTT，也不是本机 `perf c2c record` 的墙钟结果。`5` / `50` 纳秒是教学安排，不是计数器测到的周期。

## 自测题

**题 1.** 什么是缓存行上的伪共享？在本课里，相邻 atomic 计数器与隔离布局的吞吐、HITM 各是多少？为什么必须用测量而不是事先写死行宽来解释结果？

<details>
<summary>答案</summary>

伪共享是：不同线程更新同一缓存行里逻辑独立的数据，一致性仍按行维护所有权，其他 CPU 必须重新装入整行。官方例子是 refcount 频繁写、name 只读，读 name 的 CPU 仍要一遍遍装行。

两线程、`30.00` 秒：相邻布局合计吞吐 `40000000.00` 次 / 秒，Load Local HITM `1200000000`，偏移为 `0x0` 与 `0x8`；隔离布局吞吐 `400000000.00`，HITM 为 `0`。队列 head / tail 紧挨或隔开时，数字相同。

文档只说行宽 usually 64 bytes or more。本课对 8 / 16 / 32 / 64 / 128 做扫描，到 `64` 时 HITM 才清零，才把测得行宽记为 `64`。宿主 sysfs 也是 64，但那不是结果来源。扫描是 2 的幂次，不能当成对 48 或 56 的精确测量。验收要求用测量解释结果，而不是固定行宽假设。

</details>

**题 2.** 怎样排除真实共享与伪共享的混淆？为什么只看吞吐或只看 HITM 不够？HITM 的官方定义在哪一页？

<details>
<summary>答案</summary>

真实共享是多个线程写同一位置；伪共享是写同一行里的不同位置。本课里这两种情况合计吞吐都是 `40000000.00`，Local HITM 都是 `1200000000`，Remote HITM 都是 `0`。只比较吞吐或 HITM 总数，二者无法分开。

教学 c2c 给出行内偏移：真实共享两条记录都是 `0x0`；伪共享是 `0x0` 与 `0x8`。这与内核文档建议用 perf-c2c 的 in-line offset、再用 pahole 对齐成员是同一条路。

HITM 的定义不在内核页正文，而在它点名的介绍文：a load that hit in a modified cacheline。该文把 HITM 说成伪共享已经发生的关键；本课将其标成必要但不充分，还必须看偏移是否不同。本课没有在宿主上跑 `perf c2c`。

</details>

**题 3.** 为什么不要给每个字段都 padding？隔离写热点的收益和代价在本课里分别是什么？哪些数字只是教学安排？

<details>
<summary>答案</summary>

8 个写热点挤在一行时，只要 `64` 字节，但八线程吞吐是 `160000000.00`，HITM `4800000000`。只给热点各留一行，工作集变为 `512` 字节（`8.0000` 倍），吞吐 `1600000000.00`，HITM 为 `0`。这是一致性上的收益，也是更多缓存占用的代价。

若还有 8 个只读字段，只隔离热点是 `576` 字节、9 行；每个字段都 padding 则是 `1024` 字节、16 行，相对全部紧凑的 `128` 字节膨胀 `8.0000` 倍，却不再提高吞吐。这就是「每个字段都 padding 导致工作集暴涨」。文档要求缓解必须权衡空间消耗，不要超优化冷路径。

`5` / `50` 纳秒、由此算出的递增次数和 HITM 计数，都是教学安排，不是宿主 perf 采样，也不是文档示例里的墙钟数字。

</details>

## 推荐资源

- [Linux Kernel — False Sharing](https://www.kernel.org/doc/html/latest/kernel-hacking/false-sharing.html)（内核文档 7.4.0-rc2，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，22125 字节，`<title>` 为 False Sharing — The Linux Kernel documentation。用于核对缓存行、有害伪共享的两个条件、perf-c2c 示例字段，以及缓解必须权衡空间。该页描述的是内核里的布局问题，本课的教学计数是对照，不是宿主 `perf c2c` 的输出，也不使用页内示例的 HITM 数字。
- 跟随阅读：[C2C - False Sharing Detection in Linux Perf](https://joemario.github.io/blog/2016/09/01/c2c-blog/)（Joe Mario，2016-09-01，英文，深入，免费）。内核文档把这篇列为 perf-c2c 介绍。2026-11-14 返回 200，28578 字节。用于核对 HITM 的定义（命中 Modified 行的 load）以及 Local / Remote HITM。文中的采样计数不是本课结果。

## 本课小结

- 不同线程更新同一缓存行内的独立数据，也会争夺所有权。结构布局能显著改变一致性流量。计数器与队列元数据挤在一行里，吞吐会崩。
- 两线程相邻布局的教学吞吐是 `40000000.00` 次 / 秒，Local HITM `1200000000`；隔离后是 `400000000.00`，HITM 为 `0`。真实共享的吞吐与 HITM 与相邻布局相同，但偏移都是 `0x0`，不能与伪共享混为一谈。
- 行宽由 padding 扫描测得为 `64` 字节，不是事先假定，也不是宿主 sysfs 的直接拷贝。官方只保证 usually 64 bytes or more。
- 只隔离写热点：8 个计数器从 `64` 字节、1 行变为 `512` 字节、8 行，吞吐从 `160000000.00` 升到 `1600000000.00`。每个字段都 padding 会把 16 个字段撑到 `1024` 字节，工作集暴涨，却不再提高吞吐。
- 这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 `perf c2c` 的输出。`CacheEngine` 不是 Redis，也不是 Linux perf。HITM 的定义来自文档点名的介绍文，内核页正文没有展开这个缩写。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「整行重新装入」「至少有一次写」「用偏移定位成员」以及「缓解必须权衡空间」这几段，不要只看吞吐就把真实共享当成伪共享，也不要把 64 字节或一律 padding 写成所有机器的前提。
