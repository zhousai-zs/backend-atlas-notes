# 第 112/208 课：存储性能、放大与尾延迟

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-storageperf` |
| 英文名 | Storage Performance |
| 领域 / 阶段 | G / 第 5 阶段 |
| 子类 | 存储引擎 |
| 难度 | 专家 |
| 前置节点 | `g-rocksdb`、`g-bloom`、`a-math` |
| 标签 | `Storage Performance`、`G`、`Systems` |
| 参考资料 | [存储性能、放大与尾延迟：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Benchmarking-tools)（github.com，文档；2026-11-13 该 URL 返回 301，跳转到 [meta/rocksdb wiki：Benchmarking tools](https://github.com/meta/rocksdb/wiki/Benchmarking-tools) 返回 200。同批核对 Write Stalls、RocksDB Tuning Guide、Statistics、Compaction Stats and DB Status、WAL Performance、Performance Benchmarks、How to ask a performance-related question、Leveled Compaction、Setup Options and Basic Tuning、Memory usage in RocksDB，均为 facebook 入口 301 后 200；`tools/db_bench_tool.cc`、`include/rocksdb/statistics.h` 的 blob 入口同样 301 到 meta 后 200；raw.githubusercontent.com 上的 `version.h` / `options.h` / `advanced_options.h` 直接 200。`wiki/Storage-Performance`、`wiki/db_bench`、`wiki/Write-Stall`、`wiki/Amplification` 当日会跳到 wiki 首页，本课不引用。源码以 main 为准，当日最新正式版 v11.13.2（2026-11-04），main 的 `version.h` 为计划中的 11.15.0） |
| 摘要 | 存储性能受访问分布、缓存状态、压实、同步策略和设备队列共同影响，必须测量稳态与故障恢复阶段。 |

> **边界说明**：本课中的「内容后台」「文章标题」「阅读请求」都是**教学假设**，用来让预热、写停顿、放大和尾延迟变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎，模块包括 `StoragePerfEngine` / `Options` / `WriteOptions` / `ReadOptions` / `Device` / `LRUCache`（统称 `StoragePerfEngine`），**不是 RocksDB，不是 Redis，也不是网络服务**。延迟来自虚拟设备队列（读 80µs、写 40µs、fsync 1000µs，另按 256 字节加一点服务时间），不是某块真实 SSD 的基准数字，也不是 `db_bench` 在生产硬件上的结果。教学 LSM 只有 L0/L1 两层，压实会合并全部 L0 与 L1；写停顿的触发阈值已缩小（L0 slowdown=4、stop=8，pending 24KiB/48KiB），以便短跑就能碰到官方 wiki 里那类日志。它没有多线程后台池、没有 RateLimiter、没有 Bloom 过滤（第 108 课的那种）、也没有真正的 `db_bench`。`WriteOptions.sync` / `disableWAL` / `no_slowdown` 是对官方选项的教学对应，不是对 `db/db_impl` 的逐行移植。官方文档和头文件已于 **2026-11-13** 用 curl 核对（见下文）。文中的官方机制只写到核对过的页面和源码能够支撑的范围；教学实验里的字节数、延迟、命中率和键数量都来自 `/workspace/atlas/lessons/practice112/results.json`，是这台教学机器上的实测。

## 导读

### 一个直觉：缓存里跑得快，不等于存储很快

内容后台要把标题和计数落到本地 KV。压测面板如果只展示「刚写进去的 80 篇，立刻再读，p99 是 0」，很容易让人以为引擎已经够快。节点把这件事说得很硬：存储性能受访问分布、缓存状态、压实、同步策略和设备队列共同影响，必须测量稳态与故障恢复阶段。只用内存命中或短跑结果来宣传存储能力，会把还没碰到设备、还没碰到压实、还没碰到 fsync 的数字，当成长期可交付的能力。

本课要盯住的不是「这一秒的 QPS」，而是：数据相对缓存有多大、预热之后随机读还快不快、后台压实或 fsync 会不会把 p99 拉高、以及你为了把 QPS 做上去时，有没有把 WAL 或 sync 悄悄关掉。

### 接上前置

第 107 课把 RocksDB 当成嵌入式内核来用：写入走 WAL 和 memtable，刷盘形成 SST，读路径穿过块缓存。第 108 课说明，没有过滤时，不存在的键也会变成一次次文件探查。第 16 课的百分位数则是本课读延迟时要反复用的尺子：p99 看的是排序后靠近尾部的那一次，不是平均值。本课把这三件事放在同一条负载里：持续混合读写，看缓存是否还热、压实和 fsync 是否正好打在尾部，以及数据规模和内存的比例有没有写进报告。

### 一次混合读写走进教学引擎之后

一次 `put` 会先向后台捐出一小段设备时间（刷盘或压实），再检查写停顿：L0 文件数或待压实字节超过阈值时，写入会被放慢或停住。然后按 `WriteOptions` 决定要不要写 WAL、要不要 `fsync`。记录进 memtable 之后，若 memtable 满了，就变成 immutable，等待刷成 L0。教学模型里，触发压实的那次写入会把压实的设备时间记在自己的延迟上——这是简化，不是官方「压实在后台线程、写入只竞争磁盘队列」的完整形态，但足以观察「有压实的窗口，写 p99 更高」。

一次 `get` 先查 memtable 和 immutable。未命中时，从新到旧检查可能覆盖该键的 SST：先读教学用的索引块，键不在文件里就看下一份；在的话再读数据块。索引和数据都可以进块缓存。不存在、但落在文件键范围内的查询，必须把所有重叠的 L0 都问一遍，这就是读放大。

### 教学场景（教学假设）

假设内容后台用嵌入式 KV 保存已发布文章的标题，键为 `article:0001` 这种形式，值是固定 128 字节、以 `Weekly digest` 开头的缓冲区。读请求按随机键来，写请求是新文章或覆盖。下面的数字都是这个假设里跑出来的，不是线上容量规划。

### 本课三条主线

1. **预热与稳态**：短跑和已经放进缓存的工作集，不能代表数据大于缓存时的随机读。
2. **写停顿和后台 IO**：刷盘或压实跟不上时，引擎会放慢甚至停写；压实窗口里的写 p99 会明显高于只有 fsync 的窗口。
3. **读写空间放大与 p99**：重叠的 L0 会抬高空间放大和未命中读的 p99；压实用更多写入换回更瘦的树。优化时，持久性契约不能偷偷改变。

## 学习目标

- 能解释预热与稳态：报告里要写清数据规模相对内存（块缓存）的比例，并区分短跑、已预热的工作集，以及数据大于缓存时的随机读。
- 能独立完成：对教学 KV（对应节点里的 RocksDB KV 实践）执行持续混合负载，标注压实和 fsync 与尾延迟的相关性；比较时保持相同的 WAL / sync 契约。

## 为什么后端要懂

节点写得很直接：避免只用内存命中或短跑结果宣传存储能力。内容后台如果按「刚写入还能在 memtable 里读到」去对外承诺读延迟，一旦工作集不再热、压实开始重写 SST、或者每条写都要 `fsync`，p99 会完全是另一个数量级。故障恢复之后缓存是冷的，也必须单独测，不能拿恢复前的稳态数字直接套用。把持久性悄悄放宽换来的 QPS，更不能写进同一张对比表。

## 核心知识点

### 1. 预热与稳态

先看一组对照。800 篇 `Weekly digest` 的逻辑大小是 112000 字节，块缓存只有 16384 字节，**数据相对缓存的比例是 6.8359**。这是验收标准要求写进报告的那一项：不写比例，读者不知道你是在缓存里跑，还是已经超出内存。

80 篇的短跑里，数据还留在 memtable，200 次随机读的 p50 / p99 / 平均值全部是 0µs，缓存未命中 0 次。它很快，是因为几乎没走设备。把 800 篇刷盘并压实、清空缓存之后，200 次冷随机读的 p50 和 p99 都是 88µs，p999 和最大值是 172µs，平均值 83.14µs，命中率只有 0.5275（每次查询会查索引块和数据块，计数会多于 200）。再把 40 篇能放进缓存的工作集预热一遍，随后 200 次读的命中率是 1.0，p99 回到 0。可是预热并不能改变「整库比缓存大」这件事：对全部 800 键再做 200 次随机读，命中率只有 0.5725，p99 仍是 88µs，平均值 75.24µs。

所以稳态不是「已经跑了一会儿」，而是：访问分布、缓存能否罩住工作集、以及 LSM 的形状，都已经不再靠开局的特殊条件。官方 `db_bench` 用 `--benchmarks=fillseq,readrandom` 和 `--use_existing_db` 把填充与回读拆开，也可以在基准名后面加 `[W10]` 做预热、加 `[X3]` 重复测量；这些是工具用法，本身不会自动消除压实或停顿造成的尾部。故障恢复刚打开库时，块缓存是空的，应视为又一次冷启动，而不是沿用恢复前的 p99。

### 2. 写停顿和后台 IO

RocksDB 的 [Write Stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls) 写明：当 flush 或 compaction 跟不上写入时，引擎会放慢甚至停写，否则空间放大和读放大会一起恶化。触发条件包括 immutable memtable 过多、L0 文件数达到 `level0_slowdown_writes_trigger` / `level0_stop_writes_trigger`，以及待压实字节达到 `soft_pending_compaction_bytes_limit` / `hard_pending_compaction_bytes_limit`。slowdown 时写入线程通常先睡约 1ms；stop 时会一直等到条件解除。`no_slowdown=true` 的写入在停顿时直接返回 `Status::Incomplete()`。默认阈值在 2026-11-13 的 `advanced_options.h` 里是 slowdown 20、stop 36、soft 64GB、hard 256GB、`max_write_buffer_number` 为 2。本课教学阈值缩小为 slowdown 4、stop 8，才能在 120 次写入里看到同样形态的日志。

120 次写入、自动压实关闭时：刷盘 15 次，压实只在 stop 时发生了 1 次，出现 56 次 slowdown、1 次 stop，累计停顿 56400µs。日志样本是 `Stalling writes because we have 4 level-0 files`，与官方 wiki 的句式一致。写 p50 是 104µs，p99 是 1104µs，平均值 521.5µs。同样 120 次写入、后台额度加大并打开自动压实时：压实 7 次，停顿 0 次，写 p50 是 44µs，p99 是 648µs，平均值 75.03µs。p99 从 1104 降到 648，不是「设备变快了」，而是 L0 不再堆到触发阈值。

持续混合负载把这件事推到窗口上。预填 400 篇后再做 400 次操作（约 80% 随机读、20% 写），数据 67200 字节、缓存 16384 字节，比例 4.1016。WAL 且每条 `sync=true` 时，混合阶段写 p50 是 1048µs、p99 是 3260µs，发生 83 次 fsync（80 次混合写加 3 条用于崩溃检查的脏写）。只含 fsync、没有压实的窗口，写 p99 是 1108µs；同一段负载里出现压实的窗口，写 p99 升到 3332µs。关掉每条 fsync 之后，安静窗口的写 p99 只有 104µs，压实窗口仍有 2328µs。相关性是直接标在窗口上的：fsync 把基线从几十微秒抬到约 1ms，压实再在这条基线上面叠两三毫秒。教学压实跑在触发它的那次写入里，所以本课读 p99 没有同样被拉高；真实引擎里后台压实会占用设备队列，读路径上的 cache miss 也可能排在后面，这是模型边界，不要反过来推到生产。

### 3. 读写空间放大与 p99

[Tuning Guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide) 把三种放大定义为逻辑请求和硬件请求之间的倍数：写放大是「写到存储的字节 / 写进数据库的字节」，空间放大是盘上 SST 相对活数据的倍数，读放大是一次查询实际碰到了多少文件或块。200 个键覆盖写 6 轮、且每一轮都刷成一份与其它 L0 完全重叠的 SST 之后：逻辑写入 168000 字节，盘上 SST 也是 168000，活数据只有 28000，**空间放大 6.0**；SST 写放大 1.0（还没压实），若把 WAL 算进存储写入则是 2.0。对落在键范围内、但从未写入的 80 个空档键，且不把索引送进缓存时，每次查询必须咨询全部 6 份 L0，读放大 6.0，p50 / p99 / 平均值都是 504µs（6 次索引读）。压实之后只剩一份 28000 字节的 L1，空间放大回到 1.0，SST 写放大升到 1.1667（多写出了那份新 SST），空档键的读放大降到 1.0，p99 降到 84µs。压实用一次额外的顺序写，换掉了「不存在的键也要问 6 份文件」的尾部。

验收的另一条是：优化前后，持久性契约不能偷偷改变。同样 300 次混合操作，基线是 WAL + 每条 sync、16KiB 缓存，数据/缓存比例 3.9307，虚拟 QPS 3818.54，读平均 38.43µs，命中率 0.7812，崩溃后丢失 0 条。公平优化只把缓存加到 256KiB，比例降到 0.2457，契约仍是 `wal-sync-on-every-write`，命中率 0.8354，读平均 28.92µs，崩溃仍丢失 0 条。不公平的「优化」是 `disableWAL`：虚拟 QPS 跳到 19127.77，但契约变成 `memory-only-until-flush`，同一套崩溃检查会丢掉 4 个还在 memtable 里的键。节点的误区就是这句话：关闭 WAL 后更高的 QPS，不能与原来的持久化配置直接等价比较。

## 常见误区

- 把 memtable 或块缓存里的短跑 p99，当成稳态能力。本课 80 篇短跑 p99 是 0，800 篇、数据是缓存 6.8359 倍时的随机读 p99 是 88µs。
- 只跑 `fillseq` / `fillrandom` 的前几秒，或在工作集完全能放进缓存时宣传吞吐。官方工具把填充、预热和回读分开，就是为了避免这种混淆。
- 关闭 WAL 或把 `sync` 关掉以后，拿更高 QPS 和原来的配置比快。混合负载里 `disableWAL` 的虚拟 QPS 是 11088.93，sync WAL 是 3250.55，但崩溃时前者丢掉 3 个脏键，后者丢掉 0 个。
- 看到平均延迟下降，就认为尾部也改善了。异步 WAL 的写平均值是 181.7µs，p99 仍有 2256µs，因为压实窗口并没有消失。
- 只盯用户写入的带宽，不看压实在设备上写了多少。空间放大从 6.0 降到 1.0 的同时，SST 写放大从 1.0 升到 1.1667，这是同一件事的两面。

## 设计权衡

节点的权衡是：缓存、批量与同步频率各有成本，应保留一致的正确性条件。更大的块缓存能把命中率从 0.7812 提到 0.8354，但要占用进程内存，而且压实一旦换掉 SST，刚预热的块也会失效。批量写入或降低 `sync` 频率能减少 fsync，写 p50 会从 1048µs 掉到 44µs，可是确认返回时数据未必已经落盘，本课异步 WAL 在崩溃时丢掉了 3 个脏键。让压实更积极可以压住 L0、避免 1104µs 那种停顿 p99，但压实本身就是写放大，也会在窗口里把写 p99 抬到 3ms 以上。无论选哪一边，对比实验必须把 `disableWAL` 和 `sync` 固定住；改了契约，比的就不再是同一件事。

## 动手实践：对RocksDB KV执行持续混合负载，标注压实和fsync与尾延迟相关性

节点要求对 RocksDB KV 做持续混合负载。本课用教学 `StoragePerfEngine` 在同一组契约下完成这件事，并同时覆盖两条验收：报告里出现数据规模相对内存的比例；优化前后持久性契约没有偷偷改变。

### 0. 文件结构

```
practice112/
  engine.py             # StoragePerfEngine 与虚拟设备队列
  app.py                # 五组实验
  test_storageperf.py   # 验收断言
  test.sh
  results.json          # 本次实测
```

在 `practice112` 目录执行 `./test.sh`，应看到 `ALL_CHECKS_PASSED`，并刷新 `results.json`。

### 1. 准备数据与目标操作

标题统一为 `Weekly digest`，值补齐到 128 字节，键为 `article:0001` 起。短跑用 80 篇且留在 memtable；稳态用 800 篇、16KiB 缓存。混合负载先预填 400 篇并压实，再跑 400 次操作（每 5 次里 4 次随机读、1 次写）。放大实验把 200 个键覆盖写 6 轮，每轮刷成一份重叠的 L0。崩溃检查在混合阶段结束后再写 3 个仍留在 memtable 里的脏键（契约实验里不公平路径会丢掉 4 个未刷盘键）。

### 2. 要比较的几条路径

- 短跑（memtable）对照冷随机读、已预热的 40 键工作集、以及整库随机读。
- 关闭自动压实、让 L0 堆到教学 slowdown/stop，对照加大后台额度并自动压实。
- 同一组混合负载：WAL + 每条 sync、WAL 但不每条 sync、`disableWAL`。按窗口标注 fsync 次数、压实用时和写 p99。
- 6 份重叠 L0 对照压实之后的 1 份 L1：空间放大、写放大、空档键的读放大和 p99。
- 公平优化（只加大缓存，契约不变）对照不公平优化（关掉 WAL）。

### 3. 运行并解读（真实输出）

```
$ ./test.sh
ALL_CHECKS_PASSED
```

下面的数字全部来自这次运行写入的 `results.json`，是虚拟时钟上的教学测量，不是 SSD 厂商标称，也不是生产 SLA。

### 4. 逐组对照真实数字

**预热与稳态（`warmup_vs_steady`）**

- 800 篇，逻辑 112000 字节，缓存 16384 字节，比例 6.8359。
- 短跑 80 篇仍在 memtable：读 p99 0µs，未命中 0。
- 冷随机 200 次：p99 88µs，p999 172µs，平均值 83.14µs，命中率 0.5275。
- 40 键工作集预热后 200 次：命中率 1.0，p99 0µs。
- 整库随机 200 次：命中率 0.5725，p99 88µs，平均值 75.24µs。短跑不能代表稳态。

**写停顿（`write_stall_and_bg_io`）**

- 教学阈值 L0 slowdown=4、stop=8；官方默认 20 / 36。
- 慢路径 120 次写：slowdown 56、stop 1，停顿 56400µs，写 p99 1104µs。样本：`Stalling writes because we have 4 level-0 files`。
- 快路径：停顿 0，压实 7 次，写 p99 648µs。

**混合负载与窗口标注（`mixed_load_compaction_fsync`）**

- 预填 400，混合 400，数据/缓存 = 4.1016。
- sync WAL：写 p50 1048µs、p99 3260µs，虚拟 QPS 3250.55，fsync 83 次，崩溃丢失 0。仅 fsync 窗口写 p99 1108µs，压实窗口 3332µs。
- 异步 WAL：写 p50 44µs、p99 2256µs，QPS 10069.48，fsync 0，崩溃丢失 3。安静窗口写 p99 104µs，压实窗口 2328µs。
- `disableWAL`：写 p50 0µs、p99 2212µs，QPS 11088.93，崩溃丢失 3，契约已变，不能与 sync 路径等价比较。

**放大与 p99（`amplification_and_p99`）**

- 200 键覆盖 6 轮后：6 份 L0，空间放大 6.0，SST 写放大 1.0，存储写放大 2.0。空档键读放大 6.0，p99 504µs。
- 压实后：空间放大 1.0，SST 写放大 1.1667，读放大 1.0，空档键 p99 84µs。

**持久性契约（`persistence_contract`）**

- 基线 WAL+sync、16KiB 缓存：比例 3.9307，QPS 3818.54，读平均 38.43µs，命中率 0.7812，丢失 0。
- 公平加大缓存到 256KiB：比例 0.2457，契约不变，QPS 3932.88，读平均 28.92µs，命中率 0.8354，丢失 0。
- `disableWAL`：QPS 19127.77，丢失 4，契约改为 `memory-only-until-flush`。

### 5. 对照验收标准

```
# 验收 1：报告包含数据规模相对内存比例
# 稳态 800 篇：logical_live_bytes=112000，cache_bytes=16384，
#   data_to_cache_ratio=6.8359。
# 混合负载预填 400 篇后：logical_live_bytes=67200，cache_bytes=16384，
#   data_to_cache_ratio=4.1016。
# 契约对照：基线比例 3.9307（16KiB 缓存），公平加大缓存后 0.2457（256KiB）。
# 不写这个比例，就无法判断数字是不是内存命中。

# 验收 2：优化前后持久性契约不偷偷改变
# 基线与公平优化的 contract.label 都是 wal-sync-on-every-write，
#   durable_on_ack=true，崩溃丢失 0 个键；这是一次合法对照。
# disableWAL 的 label 是 memory-only-until-flush，durable_on_ack=false，
#   混合负载崩溃丢失 3 个脏键，契约实验丢失 4 个；虚拟 QPS
#   虽从 3250.55 / 3818.54 升到 11088.93 / 19127.77，
#   fair_compare_sync_vs_disableWAL=false。
# 关闭 WAL 后更高的 QPS 不能与原持久化配置直接等价比较。
```

### 6. 自己动手的扩展练习

- 把教学 `level0_slowdown_writes_trigger` 改回 20，看 120 次写入还能不能打出 slowdown，并对照官方默认值为什么要在生产里另外观测 `STALL_MICROS`。
- 给空档键打开 `fill_cache=true`，看读放大是不是只在第一次查询为 6，之后索引命中就把 p99 打回 0；想想这和「稳态随机读」有什么不同。
- 在同步 WAL 的混合负载里，把每条 `sync` 改成每 10 条才 `sync` 一次，记录写 p99、崩溃丢失的键数，以及契约是否已经改变。

### 7. 和生产引擎的对应关系（不要记错边界）

教学设备是单队列虚拟时钟，服务时间是写死的 80/40/1000µs，不是 `iostat` 或 `fio` 的测量。教学压实往往跑在触发它的那次 `put` 里，所以本课是「压实窗口的写 p99 更高」，不要把它读成官方后台压实线程的调度。教学停顿阈值是缩小后的 4/8，官方默认是 20/36 和 64GB/256GB；日志句式对齐 wiki，阈值不对齐默认值。教学没有 Bloom，空档键会探查所有重叠 L0，这更接近「过滤没帮上忙」的读放大，而不是第 108 课里已经调过假阳性的那条路径。`db_bench` 的 `fillsync`、`readwhilewriting`、`readrandomwriterandom`、`--statistics`、`--disable_wal`、`--sync`、`--cache_size` 是核对负载参数的官方工具；本课的混合负载只是同一种问题的教学版本。崩溃恢复只重放已经 `fsync` 的 WAL 前缀和已经刷成的 SST，不是完整的 WAL recovery mode。

## 源码与文档阅读

按节点的 `sourceStudy`：使用 RocksDB `db_bench` 和统计信息定位 write stall、cache miss 与 compaction，并核对负载参数。2026-11-13 实际打开的页面如下。facebook 入口均为 301，跳转到 `meta/rocksdb` 后返回 200；下面保留节点和文档里给出的 facebook URL。当日最新正式发布是 v11.13.2（2026-11-04）；main 上 `version.h` 写的是 11.15.0，注释说明这是计划中的下一个版本号，不要把它当成已发布版本。

- [Benchmarking tools](https://github.com/facebook/rocksdb/wiki/Benchmarking-tools)（节点给出的入口）：`db_bench` 的 `fillseq` / `fillrandom` / `fillsync` / `readrandom` / `readwhilewriting` / `readrandomwriterandom`，以及 `--benchmarks="fillseq,stats" --statistics`、`--use_existing_db`。用它核对本课「先填充再回读」「同步写和不带 sync 的写不是同一个基准」。`tools/db_bench_tool.cc` 里，基准名后的 `[W10]` 是预热次数，`[X3]` 是重复测量次数。
- [Write Stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls)：三类触发条件、LOG 里的 `Stalling writes` / `Stopping writes` 句式、slowdown 约 1ms、`delayed_write_rate`、`no_slowdown` 返回 `Incomplete`。对照本课 56 次 slowdown 与 1104µs 的写 p99。
- [RocksDB Tuning Guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide)：先看统计再调参；写放大等于磁盘写入带宽除以 DB 写入速率；关注 compaction stats 里的 `Stalls` 和统计项 `STALL_MICROS`。三种放大是调压实时要一起看的杠杆。
- [Statistics](https://github.com/facebook/rocksdb/wiki/Statistics)、[Compaction Stats and DB Status](https://github.com/facebook/rocksdb/wiki/Compaction-Stats-and-DB-Status)：`CreateDBStatistics()` / `ToString()`，以及 LOG 里的 LSM 形状、停顿汇总。定位 cache miss 与 compaction 应从这里开始，而不是只看 QPS。
- [advanced_options.h](https://github.com/facebook/rocksdb/blob/main/include/rocksdb/advanced_options.h)：`level0_slowdown_writes_trigger = 20`、`level0_stop_writes_trigger = 36`、`soft_pending_compaction_bytes_limit = 64 * 1073741824ull`、`hard_pending_compaction_bytes_limit = 256 * 1073741824ull`、`max_write_buffer_number = 2`。本课教学阈值是缩小后的值，对比时要写明。
- 同日核对过、正文未展开的页面：[WAL Performance](https://github.com/facebook/rocksdb/wiki/WAL-Performance)、[Performance Benchmarks](https://github.com/facebook/rocksdb/wiki/Performance-Benchmarks)、[How to ask a performance-related question](https://github.com/facebook/rocksdb/wiki/How-to-ask-a-performance-related-question)、[Leveled Compaction](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction)、[Setup Options and Basic Tuning](https://github.com/facebook/rocksdb/wiki/Setup-Options-and-Basic-Tuning)、[Memory usage in RocksDB](https://github.com/facebook/rocksdb/wiki/Memory-usage-in-RocksDB)。`wiki/Storage-Performance`、`wiki/db_bench`、`wiki/Write-Stall`、`wiki/Amplification` 当日会跳到 wiki 首页，不要引用。

## 自测题

**1.** 80 篇标题的短跑读 p99 是 0µs，能不能用来宣传这套存储已经足够快？一份合格的性能报告至少还要写上什么？

<details>
<summary>答案</summary>

不能。短跑时数据还在 memtable 里，根本没走设备。同一课里 800 篇、逻辑 112000 字节、缓存只有 16384 字节（比例 6.8359）时，整库随机读的 p99 是 88µs；能放进缓存的 40 键工作集预热之后 p99 才会回到 0。节点要求必须测量稳态，并且验收标准写明：报告要包含数据规模相对内存的比例。缺了这个比例，别人无法判断你是不是在用内存命中宣传存储能力。

</details>

**2.** 混合负载里把 WAL 关掉之后，虚拟 QPS 从 3250.55 升到 11088.93。能把它和原来的配置放在同一张「优化效果」表里吗？崩溃之后呢？

<details>
<summary>答案</summary>

不能等价比较。节点的误区就是：关闭 WAL 后更高的 QPS 不能与原持久化配置直接等价比较。sync WAL 的契约是 `wal-sync-on-every-write`，崩溃丢失 0 个键；`disableWAL` 的契约是 `memory-only-until-flush`，同一组混合负载结束时留下的 3 个脏键全部丢失。公平对照是只加大缓存、保持 sync 不变：QPS 只从 3818.54 到 3932.88，但读平均从 38.43µs 降到 28.92µs，崩溃仍然丢失 0 个键。

</details>

**3.** 200 个键覆盖写 6 轮后，空间放大是 6.0，空档键的读 p99 是 504µs；压实之后空间放大 1.0，写放大从 1.0 升到 1.1667，读 p99 变成 84µs。为什么写放大升了，读的尾部反而降了？这和写停顿有什么关系？

<details>
<summary>答案</summary>

6 份完全重叠的 L0 让活数据在盘上存了 6 份，所以空间放大是 6；一个从未写入、但落在键范围内的查询必须咨询全部 6 份文件，读放大 6.0，6 次索引读合计 504µs。压实把它们合并成一份 28000 字节的 L1，多一次 SST 写出，于是写放大升到 1.1667，但之后空档键只问 1 份文件，p99 降到 84µs。若这些 L0 继续堆下去，还会碰到本课慢路径里的 56 次 slowdown 和 1104µs 写 p99——Write Stalls 文档把停顿解释为：不把写入降到设备跟得上的速度，空间放大和读放大还会继续恶化。压实是在用写放大，换空间、读尾部和停顿。

</details>

## 推荐资源

- [存储性能、放大与尾延迟：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Benchmarking-tools)（github.com，英文文档，专家，免费）。重点阅读预热与稳态与写停顿和后台 IO，用于核对本节点实验的语义和边界。2026-11-13 该入口 301 到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/Benchmarking-tools) 后返回 200；同一次核对建议打开 [Write Stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls) 和 [RocksDB Tuning Guide](https://github.com/facebook/rocksdb/wiki/RocksDB-Tuning-Guide)。不要使用会跳到 wiki 首页的 `wiki/Storage-Performance`、`wiki/db_bench`、`wiki/Write-Stall`、`wiki/Amplification`。

## 本课小结

- 预热和稳态不是一回事。80 篇短跑的读 p99 是 0µs，是因为还在 memtable；800 篇、数据是缓存的 6.8359 倍时，整库随机读 p99 是 88µs。报告必须写上数据规模相对内存的比例，恢复之后的冷缓存也要单独测。
- 写停顿和后台 IO 会直接出现在尾部。教学慢路径 56 次 L0 slowdown、写 p99 1104µs；混合负载里，仅 fsync 的窗口写 p99 是 1108µs，叠上压实后是 3332µs。异步 WAL 能把安静窗口降到 104µs，但压实窗口仍有 2328µs。
- 放大是同一组杠杆。6 份重叠 L0 的空间放大 6.0、空档键 p99 504µs；压实后空间放大 1.0、写放大 1.1667、p99 84µs。加大缓存是契约不变的公平优化；关掉 WAL 把 QPS 做到 19127.77 却丢掉 4 个键，不能拿来和原来的持久化配置比。
- 学完后可以用三句话自检：短跑 p99 和数据/缓存比例为什么必须一起出现在报告里；fsync 窗口和压实窗口的写 p99 差在哪里，L0 停顿的日志长什么样；空间/写/读放大如何一起变化，以及为什么 `disableWAL` 的 QPS 不能直接对比。三句都能答上来，并且能指出 `practice112/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验字段，再回到官方文档里关于「`db_bench` 的填充、预热与回读」「Write Stalls 的 slowdown/stop 与 `STALL_MICROS`」「写放大等于磁盘写入除以 DB 写入」「`disableWAL` 与 `sync` 不是同一个基准」的那几句，不要只把「要看 p99」这句话背下来。
