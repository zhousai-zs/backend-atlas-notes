# 第 135/208 课：eBPF 可观测性

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `i-ebpf` |
| 英文名 | eBPF tracing |
| 领域 / 阶段 | I / 第 4 阶段 |
| 子类 | 剖析与诊断 |
| 难度 | 深入 |
| 前置节点 | [`i-perf`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html) |
| 标签 | `eBPF tracing`、`Performance` |
| 参考资料 | [bpftrace — Language documentation](https://bpftrace.org/docs/release_024/language)（bpftrace 语言文档，英文，深入，免费；2026-11-14 打开该地址返回 HTTP 200，无跳转，263075 字节，`Content-Type` 为 `text/html; charset=utf-8`，`Last-Modified` 为 `Thu, 12 Nov 2026 08:03:09 GMT`（北京时间 2026-11-12 16:03:09），响应头 `Date` 为 `Fri, 13 Nov 2026 18:24:39 GMT`（北京时间 2026-11-14 02:24:39）。`<title>` 为 The bpftrace Language (0.24) \| bpftrace，页内 `h1` 为 The bpftrace Language (0.24)，与节点给出的资源标题「bpftrace — Language documentation」并不字面相同。页面由 Docusaurus v3.6.3 生成，无单独作者署名。该页把 bpftrace 语言写成受 dtrace 所用 D 语言启发，程序由 preamble 与若干 action block 组成；探针类型包括 `kprobe` / `kretprobe`（对内核函数的动态插桩）和 `tracepoint`（编译进内核的静态钩子）。它写明：The advantage of tracepoints is that they generally provide a more stable interface than kprobes do, they do not depend on the existence of a kernel function。Map 以 `@` 开头，底层是 BPF map，在 bpftrace 进程生命周期内存在，可从 action block 和用户态访问。`perf_rb_pages`（默认 64，须为 2 的幂）一节写：If you’re getting a lot of dropped events bpftrace may not be processing events in the ring buffer fast enough。未提前声明的 map 若写满，会导致 lost map update events。该页提到 verifier 的句子只有一处：BPF supports while loops as long as the verifier can prove they’re bounded and fit within the instruction limit。它没有定义 CAP_BPF，也没有在本页给出 `hist()` 的函数签名；页内链到同版本 [Standard Library](https://bpftrace.org/docs/release_024/stdlib)（2026-11-14 返回 HTTP 200，无跳转，194601 字节，同一 `Last-Modified`，`Date` 为 `Fri, 13 Nov 2026 18:25:41 GMT`，北京时间 02:25:41，`<title>` 为 bpftrace Standard Library (0.24) \| bpftrace，`h1` 为 bpftrace Standard Library (0.24)）。stdlib 把 `hist(int64 n[, int k])` 定义为 Create a log2 histogram of n using \(2^k\) buckets per power of 2，0 ≤ k ≤ 5，默认 k = 0；`printf()` 标明为 async，The formatting of the string happens in user space。tracepoint 小节还指向内核文档 [Using the Linux Kernel Tracepoints](https://www.kernel.org/doc/html/latest/trace/tracepoints.html)（HTTP 200，无跳转，18889 字节，`Last-Modified` 为 `Mon, 09 Nov 2026 00:39:11 GMT`，北京时间 2026-11-09 08:39:11，`Date` 为 `Fri, 13 Nov 2026 18:25:42 GMT`，北京时间 02:25:42，`<title>` 为 Using the Linux Kernel Tracepoints — The Linux Kernel documentation，页内 `h1` 为 The Linux Kernel 与 Using the Linux Kernel Tracepoints，作者 Mathieu Desnoyers，页头版本 **7.4.0-rc2**）。该页说明 tracepoint 关闭时仍有 tiny time penalty。本课没有把未打开的 docs.ebpf.io `BPF_PROG_TYPE_RAW_TRACEPOINT` 页（语言文档在 rawtracepoint 处标为 Read More）当成结论依据。） |
| 摘要 | eBPF 可在受验证的内核执行环境中处理事件并把聚合结果传给用户态，需考虑内核特性与采样开销。 |

> **边界说明**：本课里的「内容后台」「文章键 `article:01:title`」「标题 `Weekly digest`」「教学 KV 进程 `teachkv`」「教学内核 `6.8.0-teach` / `6.10.0-teach`」「内部函数 `do_recv_fastpath`」「声明权限 `CAP_BPF, CAP_PERFMON`」都是**教学假设**，用来让 kprobe 与 tracepoint、BPF map 上的 `hist()`、验证器拒绝，以及 ring buffer 丢事件变得可观察，不代表腾讯/微信或任何企业的内部实现，也不代表在本机加载了 BPF 程序或调用了宿主 `bpftrace` / BCC / `perf` / `g++`。配套代码是 Python 3 标准库写的教学引擎 `EbpfEngine`，**不是 Redis，不是真实的剖析器或 eBPF 产品，不是网络服务，不是 Linux bpftrace 或 BCC，也不是宿主机上的内核探针**。本次运行记录的宿主 `uname -r` 为 `6.12.113+`、`uid` 为 `1000`，仅用于声明「没有向该内核加载程序」；教学内核版本是声明值 `6.8.0-teach`，不是 `uname` 的结果。语言文档没有给出 CAP_BPF 定义，权限声明是教学验收要求，不是从该页抄来的内核接口。`hist()` 的签名在跟随阅读的 stdlib 页，不在语言文档正文。验证器在语言文档里只出现在「while 必须被证明有界」那一句，本课对越界栈的拒绝依据的是同一文档 Errors 节写明的 512 字节 BPF 栈上限。`30.00` 是教学虚拟时钟上的三十秒，直方图里的微秒是教学虚拟时钟值，不是生产 RTT，也不是宿主 `CLOCK_BOOTTIME` 的 `nsecs`。`perf_rb_pages` 的默认 64 来自官方文档；用户态 1100 条 / 秒的排空速率和每事件 1 / 8 微秒开销是教学安排，文档没有给出这两个数字。本课所有数字都来自 `/workspace/atlas/lessons/practice135/results.json`。

## 导读

### 上一课留下的缺口

上一课的 `LockEngine` 已经说明：吞吐可能被临界区串行部分卡住，CPU 不高也可能有严重锁等待。那一课停在教学互斥的 hold / wait，没有把「系统调用、网络和调度的真实等待」接到一条可验证的采集链路上，也没有回答：这些等待要在内核里聚合再交给用户态，还是逐条打印出来。

节点把摘要写成：eBPF 可在受验证的内核执行环境中处理事件并把聚合结果传给用户态，需考虑内核特性与采样开销。能看见事件，不等于采集没有代价，也不等于探针在下一个内核里还能挂上。

### 接上前置

前置 [第 131 课](https://zhousai-zs.github.io/backend-atlas-notes/lessons/131.html) `i-perf` 留下的结论是：采样频率升高会丢样，热点份额会被扰动；`perf record` 按频率或周期取栈，并不在每次系统调用上都停留。若要统计服务路径上每一次 `recvfrom` / `sendto` / `futex` 的延迟分布，需要另一类挂载：把程序附到事件上，在内核里更新 map，退出时再把聚合结果打印出来。

第 132 课的火焰图区分了 on-CPU 与 off-CPU，第 134 课又把锁等待写成了阻塞或忙等。本课不再展开那些图，只用到「真实等待会落在系统调用和调度上」这一条：用教学版的 bpftrace 程序，给 `teachkv` 取 `Weekly digest` 的路径画一张 syscall 延迟直方图。

官方语言文档把探针分成 kprobe（动态，依赖内核函数是否存在）和 tracepoint（静态，接口通常更稳），把 map 写成 BPF map，把丢事件写在 ring buffer 来不及处理时。本课用教学引擎复现这几条口径，而不是在本机执行 `bpftrace` 或 BCC。

### 教学场景（教学假设）

假设内容后台的教学 KV `teachkv` 在 `30.00` 的虚拟时钟窗口里处理 `30000` 次取标题请求，键为 `article:01:title`，标题为 `Weekly digest`，吞吐 `1000.00` 次 / 秒。每次请求都有一次 `recvfrom` 和一次 `sendto`；其中 `6000` 次还会进入 `futex`，对应调度与锁上的等待。recv 的延迟按教学分布落在 `8`、`32`、`256` 微秒，send 固定 `4` 微秒，futex 固定 `128` 微秒。采集程序按 bpftrace 的写法，在 `sys_enter_*` 把 `@start[tid] = nsecs` 记进 map，在 `sys_exit_*` 里对 `(nsecs - @start[tid]) / 1000` 做 `hist()`。对照两种用户态出口：map 聚合后在退出时打印，以及每次退出都 `printf()`。另外声明教学内核 `6.8.0-teach` 与权限 `CAP_BPF, CAP_PERFMON`，并在 `6.10.0-teach` 上对比 kprobe 与 tracepoint 能否仍然挂上。所有时间都是教学虚拟时钟上的值，不是线上探测间隔，也不是生产 RTT。

### 本课三条主线

1. **kprobe / tracepoint**：kprobe 是对内核函数的动态插桩，覆盖可以很深，但依赖那个函数名是否还在；tracepoint 是编译进内核的静态钩子，接口通常更稳，但不能随意加新点。适用边界是：观察稳定的系统调用时优先 tracepoint；深入某个内部路径才用 kprobe，并准备好内核升级后挂不上。
2. **BPF map**：map 在受验证的内核执行环境里更新，退出时再把聚合结果交给用户态。适用边界是：直方图、计数这类聚合应该放在 map 里，而不是把每一条事件都送出内核。
3. **验证器与事件丢失**：程序必须让验证器证明循环有界、栈不越过 512 字节；即使用户态只做打印，ring buffer 来不及处理也会丢事件。适用边界是：不要用逐事件打印去撑满采集链路，也不要把跟踪程序当成零开销。

## 学习目标

- 能解释 kprobe / tracepoint 及适用边界：动态探针覆盖深，但符号稳定性不如静态 tracepoint；挂载前要声明内核版本和权限。
- 独立完成节点实践：使用 BCC 或 bpftrace 的口径，统计服务 syscall 延迟直方图。本课跑的是教学引擎对 bpftrace `hist()` 的对照，不是宿主上的 BCC 或 bpftrace。
- 能避免逐事件打印压垮采集链路，并说明跟踪不是零开销；验证器会拒绝无界循环和越栈程序。

## 为什么后端要懂

节点对「为什么」写得很干脆：观察系统调用、网络和调度的真实等待。放到本课的教学内容后台：取一次 `Weekly digest`，等待可能发生在 `recvfrom`（网络），也可能发生在 `futex`（调度与锁）。`30.00` 秒里共有 `66000` 次系统调用退出。用 map 上的 `hist()` 聚合时，recv 的三档是 `[8, 16)` 的 `27000` 次、`[32, 64)` 的 `2700` 次、`[256, 512)` 的 `300` 次，send 全在 `[4, 8)` 的 `30000` 次，futex 全在 `[128, 256)` 的 `6000` 次，丢事件为 `0`。若改成每条退出都 `printf()`，同一条链路丢掉 `33000` 次（`0.5000`），直方图被拦腰砍断，futex 只剩下 `3000` 次。看不见分布，就不能判断慢在网络还是慢在锁等待；用打印把采集链路压垮，得到的分布本身也已经不完整。

## 核心知识点

### 1. kprobe / tracepoint

语言文档把 `kprobe` 写成对内核函数的动态插桩：指定的函数每次执行，挂上的 BPF 程序就会跑；它可以挂在函数入口，也可以加偏移挂到指令中间。`kretprobe` 在返回时触发，默认拿不到入口参数，常见写法是在入口把参数放进以 `tid` 为键的 map，返回时再取。`tracepoint` 则是内核源码里定义、编译进内核二进制的静态钩子。文档原句是：Unlike kprobes, new tracepoints cannot be added without modifying the kernel。优点也写得很清楚：它们通常提供比 kprobe 更稳定的接口，并不依赖某个内核函数是否还存在。官方例子正是 `tracepoint:syscalls:sys_enter_openat` 这类系统调用跟踪点。

适用边界跟着这条区别走。本课在教学内核 `6.8.0-teach` 上，`tracepoint:syscalls:sys_enter_recvfrom` 等六个系统调用跟踪点，以及 `kprobe:__x64_sys_recvfrom` 和更深的 `kprobe:do_recv_fastpath`（教学内部函数）都能挂上。换到教学内核 `6.10.0-teach` 后，同样的 tracepoint 仍然成功，`__x64_sys_recvfrom` 也还在，但 `do_recv_fastpath` 已不存在；`missing_probes` 默认是 `error`，整个附加失败。这就是节点写的权衡：内核探针覆盖深，但符号稳定性不如 tracepoint。跟随阅读的内核 Tracepoints 页还补了一句：跟踪点关闭时并非绝对为零，仍有 tiny time penalty。

语言文档没有把 CAP_BPF 写成挂载条件。本课按节点验收，显式声明教学内核 `6.8.0-teach` 和权限 `CAP_BPF, CAP_PERFMON`，并同时记录：宿主 `6.12.113+` 上没有加载 BPF，也没有调用 bpftrace 或 BCC。

### 2. BPF map

语言文档区分两类变量：以 `$` 开头的 scratch 变量活在 BPF 栈上，出了词法块就不能用；以 `@` 开头的 map 变量底层是 BPF map，在 bpftrace 进程期间一直存在，可以从所有 action block 和用户态访问。显式声明可以选定 `BPF_MAP_TYPE_HASH`、`BPF_MAP_TYPE_PERCPU_HASH` 等类型并给出 max entries；未声明的 map 使用 `max_map_keys`（默认 4096），写满会丢失更新。文档给的延迟测量模板，正是 `@start[tid] = nsecs`，再在返回探针里相减。

`hist()` 本身定义在跟随阅读的 Standard Library：对 n 做 log2 直方图，每两个相邻 2 的幂之间再分成 \(2^k\) 个桶，k 默认 0。本课 k = 0，因此 `8` 微秒落入 `[8, 16)`，`32` 落入 `[32, 64)`，`256` 落入 `[256, 512)`，`4` 落入 `[4, 8)`，`128` 落入 `[128, 256)`。这 `66000` 次更新都在教学中的内核执行环境里完成，用户态在退出时只拿到 5 个桶（`userspace_records` 为 `5`），丢事件为 `0`，`lost_map_updates` 为 `0`。这正对应节点摘要里「在受验证的环境中处理事件，再把聚合结果传给用户态」。

适用边界是：要看延迟分布，就在 map 里做 `hist()` / `count()`，不要把原始事件当作默认出口。map 不是无限的，键太多仍然会丢更新。本课没有启用文档里标为实验性的 map 声明语法作为默认路径。

### 3. 验证器与事件丢失

语言文档对 verifier 的直接表述只有：while 循环必须被证明有界，并且放得进指令上限。Errors 节另外写明 BPF 栈上限是 512 字节。本课用同一口径做教学检查：统计直方图的程序（栈 `64` 字节、无无界循环）被接受；带 `while` 且无法证明有界的程序被拒绝；栈用到 `600` 字节的程序因超过 512 被拒绝。语言文档没有展开验证器的全部规则，本课也不补造未在文档出现的指令条数上限。

事件丢失是另一件事。`printf()` 在 stdlib 里是 async，格式化发生在用户态，事件要先进入 per-CPU perf ring buffer。`perf_rb_pages` 默认 64：If you’re getting a lot of dropped events bpftrace may not be processing events in the ring buffer fast enough。本课按这条条件做对照：同样 `66000` 次退出，逐条 `printf()` 时教学排空只有 `1100` 条 / 秒，`30.00` 秒只记下 `33000` 次，丢掉 `33000` 次（`0.5000`）；三个直方图的计数都被砍成一半，用户态却要处理 `33000` 条记录。把打印当采集，链路会先被自己压垮。

节点的验收是：声明内核版本和权限，并避免逐事件打印压垮采集链路。map 聚合满足后一条；逐条打印用来展示失败模式，不是推荐做法。

## 常见误区

节点写出的误区是：把跟踪程序当作完全无开销。官方语言文档没有把 kprobe 或 tracepoint 写成零代价；相反，`max_probes` 一节警告增加探针会消耗内存、拉长启动，并可能带来高额性能开销，甚至让系统卡死。内核 Tracepoints 页写的是关闭时仍有 tiny time penalty。本课的教学开销是：map 路径每事件 `1` 微秒，`66000` 次合计 `66000` 微秒；`printf` 路径每事件 `8` 微秒，合计 `528000` 微秒。两个数字都不是零，后者更明显。这两个微秒值是教学安排，不是宿主上测到的探针开销，也不是文档给出的官方基准。跟踪能看到真实等待，但跟踪本身也是负载的一部分；开销大小随探针类型、是否逐条输出、map 更新频率而变，不能预先假设为「可以忽略」。文档在 fentry / fexit 处写过 near zero overhead，那是相对 kprobe 的 trampoline 路径，不是「任何跟踪都接近于零」，本课没有把 fentry 当作实践默认。

## 设计权衡

节点写出的权衡是：内核探针覆盖深，但符号稳定性不如 tracepoint。`kprobe:do_recv_fastpath` 可以跟进教学内核里一条更内部的接收路径，这是覆盖深度；到了 `6.10.0-teach`，这个符号不在了，附加按默认 `missing_probes=error` 失败，而 `tracepoint:syscalls:sys_enter_recvfrom` 这一组仍然成功。选择 kprobe 时，要同时写下内核版本，并准备函数更名、编译裁剪或 inline 之后的退路。选择 tracepoint 时，稳定的是已经存在的钩子和 `args` 里的字段，不能自己在不改内核的前提下再加一个点。deep coverage 和稳定接口不是同一件事，需要按问题选，而不是按「能不能挂上这个函数」选。

## 动手实践：使用 BCC 或 bpftrace 统计服务 syscall 延迟直方图

配套代码在 [`/workspace/atlas/lessons/practice135/`](https://zhousai-zs.github.io/backend-atlas-notes/files/practice135.tar.gz)。`engine.py` 是 `EbpfEngine`，`app.py` 跑完整组实验并写出 `results.json`，`test_ebpf.py` / `test.sh` 负责核对。在该目录执行：

```bash
bash test.sh
```

应打印 `ALL_CHECKS_PASSED`。下面的数字全部来自这次运行写出的 `results.json`，都是教学虚拟时钟上的值。实践按节点的表述，使用 BCC 或 bpftrace 的口径统计服务 syscall 延迟直方图；跑的是教学引擎，不是宿主 bpftrace，也不是宿主 BCC。

请先自己根据 `30.00` 秒、`30000` 次请求，以及 recv `8` / `32` / `256` 微秒、send `4` 微秒、futex `128` 微秒的计数，画出 k = 0 的 log2 直方图，确认 recv 三个桶是 `27000`、`2700`、`300`，send 是 `30000`，futex 是 `6000`，map 路径丢事件为 `0`、用户态只有 `5` 条记录。然后再核对另外三组：同样的 `66000` 次退出若改为逐条 `printf()`，会丢掉 `33000` 次（`0.5000`），直方图计数减半；声明的教学内核是 `6.8.0-teach`、权限是 `CAP_BPF, CAP_PERFMON`，宿主 `6.12.113+` 上 `host_bpf_program_loaded` 为假；无界 `while` 和 `600` 字节栈的程序会被拒绝；到 `6.10.0-teach` 时 `kprobe:do_recv_fastpath` 缺失，tracepoint 仍能挂上。

验收与误区、权衡对应的标志在 `results.json` 里均为真：`acceptance_declared_kernel_and_privileges`、`acceptance_avoids_per_event_print`、`pitfall_not_zero_overhead`、`tradeoff_kprobe_less_stable_than_tracepoint`。引擎同时标明 `not_redis`、`not_a_network_service`、`not_a_real_profiler_product`、`not_a_real_ebpf_product`、`not_host_bpf`、`not_host_bpftrace`、`not_host_bcc`、`not_host_perf`、`not_host_gxx`、`times_are_teaching_virtual_clock`。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 bpftrace — Language documentation 中与 eBPF tracing 相关的定义、示例或实现，记录与教学实验的差异。2026-11-14 的实际打开情况如下。

- 节点给出的 [https://bpftrace.org/docs/release_024/language](https://bpftrace.org/docs/release_024/language) 返回 200、无跳转、263075 字节，`<title>` 为 The bpftrace Language (0.24) | bpftrace，与节点资源标题不完全一致。本课跟随打开了它点名的 Standard Library（194601 字节，`hist()` / `printf()` 定义在这里）以及 tracepoint 小节列出的内核 Tracepoints 页（18889 字节，作者 Mathieu Desnoyers，内核文档 7.4.0-rc2）。没有把 rawtracepoint 处的 Read More 目标页算进结论。

教学实验按以下方式使用它，并记下差异。

- **kprobe / tracepoint**：定义和稳定性对照来自语言文档正文，官方例子用的是 `tcp_reset`、`vfs_open`、`sys_enter_openat`。本课的服务路径改用 `recvfrom` / `sendto` / `futex`，以及教学内部函数 `do_recv_fastpath`；两个教学内核版本是声明值，不是一次真实的内核升级实验。
- **BPF map 与直方图**：map 的 `@` 语法、`@start[tid]` 模板、写满丢失，来自语言文档；`hist()` 的 log2 规则来自 stdlib，不在语言页正文。本课的桶计数是教学延迟按 k = 0 归桶的结果，不是文档示例里 `hist(retval)` 那组 `vfs_read` 字节数，也不是 xfsdist 工具的输出。
- **验证器与事件丢失**：verifier 的官方用语只覆盖有界 while 与指令上限；512 字节栈来自 Errors。本课没有实现完整的内核验证器，也没有读取 verifier log（`log_size` 默认 1000000 只在文档里出现，未进入实验数字）。丢事件的条件来自 `perf_rb_pages`；`1100` 条 / 秒的排空和 `0.5000` 的丢失比例是教学模型，文档没有给出这两个数。`printf` 的「用户态格式化、async」来自 stdlib。
- **时间与权限**：stdlib 的 `nsecs` 默认是 `CLOCK_BOOTTIME`。本课全部改用教学虚拟时钟，不读取宿主时钟。权限是教学声明，语言文档未定义 CAP_BPF。宿主 `uname -r` = `6.12.113+` 只作为「未在该内核上加载程序」的记录。

## 自测题

**题 1.** kprobe 和 tracepoint 各回答什么问题？在本课的两个教学内核上，怎样用附加结果说明「覆盖深」和「接口稳」不是同一件事？为什么验收要求先声明内核版本和权限？

<details>
<summary>答案</summary>

kprobe 回答的是：能不能在某个内核函数（或函数内偏移）上动态插桩。它覆盖可以很深，但依赖那个符号还在。tracepoint 回答的是：能不能挂到内核已经编好的静态钩子上。语言文档写明，它通常比 kprobe 更稳定，且不依赖某个函数是否存在；新的 tracepoint 则必须改内核才能增加。

教学内核 `6.8.0-teach` 上，六个 `syscalls:` tracepoint 和 `kprobe:__x64_sys_recvfrom`、`kprobe:do_recv_fastpath` 都能挂上。到了 `6.10.0-teach`，tracepoint 仍成功，但 `do_recv_fastpath` 缺失，默认 `missing_probes=error` 让附加失败。深探针看到了内部路径，却在符号变化时先断掉。

声明内核版本，是为了让「这次挂上的是哪一组符号」可核对；声明权限，是节点验收要求。语言文档没有定义 CAP_BPF。本课的声明是教学值，并且写明宿主 `6.12.113+` 上没有加载 BPF，也没有运行 bpftrace 或 BCC。不能把教学声明写成一次真实的特权探测。

</details>

**题 2.** 为什么不要用逐事件打印去统计 syscall 延迟直方图？请用本课 map 聚合与 `printf` 两条路径的记录数、丢事件和直方图说明。官方文档把丢事件写在哪？

<details>
<summary>答案</summary>

要看的是分布，不是每一行日志。map 上的 `hist()` 在教学内核里更新 `66000` 次，退出时用户态只拿到 5 个桶，丢事件为 `0`。recv 仍是 `27000` / `2700` / `300`，send `30000`，futex `6000`，网络等待和 `futex` 等待还分得开。

同样的事件若每条都 `printf()`，格式化发生在用户态，要经过 perf ring buffer。教学排空只有 `1100` 条 / 秒，`30.00` 秒记下 `33000` 次，丢掉 `33000` 次（`0.5000`），三个直方图都被砍半，futex 只剩 `3000`。用户态还要处理 `33000` 条记录。采集链路被自己压垮，分布也不再完整。

语言文档把丢事件写在 `perf_rb_pages`（默认 64）：bpftrace 若不能足够快地处理 ring buffer 里的事件，就会出现大量 dropped events。增大页数能多排队，但更占内存。stdlib 还写明 `printf()` 是 async，格式化在用户态。这正是节点说的「避免逐事件打印压垮采集链路」。

</details>

**题 3.** 为什么不能把跟踪程序当作完全无开销？验证器在本课拒绝了什么？这些结论分别依据官方文档的哪几句，哪些数字只是教学安排？

<details>
<summary>答案</summary>

跟踪是加在原路径上的程序。语言文档在 `max_probes` 处警告探针增多可能带来高额开销甚至让系统卡死；内核 Tracepoints 页写明关闭时仍有 tiny time penalty。本课 map 路径的教学开销是 `66000` 微秒，`printf` 路径是 `528000` 微秒，都不是零。后两个微秒数和 `1100` 条 / 秒的排空是教学安排，不是宿主测量，也不是文档给出的基准。fentry 的 near zero overhead 只相对于 kprobe 的 trampoline，不能推广成「跟踪免费」。

验证器拒绝了两种教学程序：无法证明有界的 `while`，以及栈用到 `600` 字节、超过官方 512 字节上限的程序。直方图程序被接受。语言文档对 verifier 的直接句子只是「while 必须被证明有界并放进指令上限」；512 来自 Errors。本课没有实现完整验证器，也没有编造指令条数上限。

</details>

## 推荐资源

- [bpftrace — Language documentation](https://bpftrace.org/docs/release_024/language)（bpftrace 0.24 语言文档，英文，深入，免费）：节点给出的官方出处。2026-11-14 返回 200，263075 字节，`<title>` 为 The bpftrace Language (0.24) | bpftrace。用于核对 kprobe / tracepoint、BPF map、`missing_probes` 与 `perf_rb_pages` 下的丢事件。该页描述的是真实 bpftrace 语言，本课的教学计数是对照，不是宿主采集。`hist()` 不在本页，而在它指向的 [Standard Library](https://bpftrace.org/docs/release_024/stdlib)（194601 字节，同一天 200）。
- 跟随阅读：[Using the Linux Kernel Tracepoints](https://www.kernel.org/doc/html/latest/trace/tracepoints.html)（内核文档 7.4.0-rc2，英文，深入，免费；作者 Mathieu Desnoyers）。语言文档的 tracepoint 小节列出了这一页。2026-11-14 返回 200，18889 字节。用于核对静态跟踪点以及「关闭时仍有微小时间代价」，不是本课直方图数字的来源。

## 本课小结

- eBPF 可在受验证的内核执行环境中处理事件，再把聚合结果传给用户态。内核特性和采集开销必须一起考虑。跟踪不是零开销。
- 观察系统调用、网络和调度的真实等待时，本课在 `30.00` 秒里看到 `66000` 次退出：recv 的 `hist()` 是 `[8, 16)` `27000`、`[32, 64)` `2700`、`[256, 512)` `300`，send `[4, 8)` `30000`，futex `[128, 256)` `6000`。map 路径丢事件为 `0`，用户态只有 5 个桶。
- 同样的事件若逐条 `printf()`，丢掉 `33000` 次（`0.5000`），直方图被砍半。不要用打印压垮采集链路。
- kprobe 覆盖深，但在教学内核 `6.10.0-teach` 上 `do_recv_fastpath` 缺失；tracepoint 仍然可挂。先声明内核版本和权限。本课声明的是 `6.8.0-teach` 与 `CAP_BPF, CAP_PERFMON`，并写明没有在宿主 `6.12.113+` 上加载 BPF。
- 验证器按文档拒绝无界循环和超过 512 字节的栈。这些是教学虚拟时钟上的数字，不是生产 RTT，也不是宿主 bpftrace 的输出。`EbpfEngine` 不是 Redis，也不是 BCC 或真实的 eBPF 产品。

如果有一句答不上来，先回到 `results.json` 里对应的字段，再回到文档里「tracepoint 通常比 kprobe 更稳定」「map 把聚合留在内核」「ring buffer 来不及处理就会丢事件」这几段，不要把跟踪当成免费的，也不要用逐行打印去代替直方图。
