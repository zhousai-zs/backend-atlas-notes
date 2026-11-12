# 第 108/208 课：Bloom Filter与读放大

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-bloom` |
| 英文名 | Bloom Filters |
| 领域 / 阶段 | G / 第 4 阶段 |
| 子类 | 存储引擎 |
| 难度 | 深入 |
| 前置节点 | `g-lsm`、`a-hash`、`a-math` |
| 标签 | `Bloom Filters`、`G`、`Systems` |
| 参考资料 | [Bloom Filter与读放大：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter)（github.com，文档；2026-11-12 该 URL 返回 301，跳转到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/RocksDB-Bloom-Filter) 并返回 200。同期打开的 Partitioned-Index-Filters、Prefix-Seek、Rocksdb-BlockBasedTable-Format、Memory-usage-in-RocksDB 同样 facebook 入口 301 后 200；`wiki/Bloom-Filter` 与 `wiki/Filter-Policy` 301 后落到 wiki 首页，页面不存在，本课不引用） |
| 摘要 | Bloom Filter用位数组快速判断元素可能存在，允许假阳性但在正确维护条件下不应产生假阴性。 |

> **边界说明**：本课中的「内容后台」「文章标题」「未发布稿」「删除稿」都是**教学假设**，用来让 LSM 的读路径、过滤器布局和误判率变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学引擎 `BloomFilter` / `SSTFile` / `BloomLSM`，**不是 RocksDB，不是 Redis，也不是一个对外提供网络协议的服务**。教学 SST 是**内存中按 key 排好序的记录列表，再按固定条数切成块**，不是 RocksDB 的 block-based 约 4 KiB 压缩块。教学 Bloom 是标准（非 cache-local）位数组，用 blake2b 得到两个 64 位哈希再做 double hashing，哈希数 `k = round(bits_per_key * ln(2))`；它不是 RocksDB 的 FastLocalBloom（`format_version=5`，探针限制在同一条 CPU cache line），也没有实现 Ribbon filter、真实 block cache、WAL、MANIFEST、列族、快照、prefix extractor 或压缩。实验里的「data block read」是教学计数，不是真实磁盘 syscall。官方文档已于 **2026-11-12** 核对：节点 URL `https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter` 返回 301，跳转到 `https://github.com/meta/rocksdb/wiki/RocksDB-Bloom-Filter` 并返回 200；同期打开的 Partitioned-Index-Filters、Prefix-Seek、Rocksdb-BlockBasedTable-Format、Memory-usage-in-RocksDB 同样 301 后 200。`https://github.com/facebook/rocksdb/wiki/Bloom-Filter` 与 `wiki/Filter-Policy` 301 后落到 wiki 首页，页面不存在，不要引用。文中的官方默认值、公式和机制只写到核对过的页面能够支撑的范围；教学实验里的字节数、耗时、误判次数和 IO 次数都来自 `/workspace/atlas/lessons/practice108/results.json`，不是生产引擎的基准数字。

## 导读

### 一个直觉：过滤器不是「查得更准」，而是「更早放弃」

LSM 的读放大有一个很具体的来源：一个键不存在时，引擎仍要按层、按文件去找。第 106 课已经把 memtable、SST、压实讲清楚了；第 107 课又把块缓存和按列族配置接上。但在这些路径之前，还有一个更便宜的问题可以问：**这个 SST 里到底有没有可能包含这个键？** Bloom Filter 用一段位数组回答这个问题。查询时用同一组哈希探针去查位：只要有一个位置是 0，这个键「一定不在」；如果全部是 1，只能说「可能在」。允许假阳性，正确维护时不应出现假阴性。

节点把这条机制的价值写成：降低 LSM 不存在键查询触发的磁盘读取。内容后台里，读者打开一个已经被删除、从未发布、或链接拼错的标题时，Get 本来会在多个重叠的 SST 里白白读数据块。过滤器先判负，就能把「读数据块」降成「查几个位」。这里要记住一个边界：过滤器说「可能在」只是**许可去读**，不是最终答案；真正的值仍要由数据块和版本判断来确认。

### 接上前置

第 106 课的 LSM 告诉我们，一次 Get 可能先看 memtable，再从 L0 到更底层逐层找；L0 文件之间范围重叠时，一个查询会碰多个文件。`a-hash` 提供哈希函数和位运算：同一个键每次都要得到同一组位置，不同键要尽量分散。`a-math` 提供概率：位数组被占满的比例、哈希数过多或过少、以及误判率随 bits-per-key 变化的曲线，都可以用标准公式估计。

这三门前置在本课汇合成一件事：**用可计算的内存换不可控的读 IO**。但内存不是白来的，哈希也不是白算的。10 bits/key 意味着每个已有键平均占 10 位；4,000 个键就是 40,000 位，也就是 5,000 字节。查询时做 7 次探针；16 bits/key 时位数组变成 8,000 字节，探针变成 11 次。误判率下降，但内存、构建和查询 CPU 都上升。核心知识点第 1 节会把这条曲线和实际数字放在一起看。

### 教学场景（教学假设）

教学 SST 里有 4,000 个已发布标题，键是 `article:0000:title` 到 `article:3999:title`，值在 `Weekly digest`、`Product update`、`Community notes`、`Editor picks`、`Weekly digest (revised)` 之间轮换。每个数据块放 16 条记录，所以这批数据有 250 个数据块。为了观察「不存在的键」，实验构造了 20,000 个形如 `article:NNNN:title~c` 的查询，并让它们落在 `(smallest, largest)` 之内，避免被范围检查直接跳过。另一些实验把 4,000 个键拆成 8 个 L0 文件、每个 500 个键，键交错分布，模拟真实 L0 flush 后范围重叠的样子。

这些都是教学假设。真实内容后台的键设计、分片、缓存和压缩策略不会这么简单；这里的目的是把「过滤器判负」「过滤器误判」「过滤器漏掉墓碑」三种结果分开观察。

### 本课三条主线

1. **位数、哈希数与误判率**：位数组多大、做几次哈希，决定误判率；标准公式能解释实测曲线的形状，但实测值不会和理论值逐位相等。
2. **分层过滤**：每个 SST 都可能有过滤器；L0 多个重叠文件时，过滤器减少的是「查错文件」的读放大。`optimize_filters_for_hits` 还会决定最底层要不要建过滤器。
3. **过滤器内存和查询成本**：full filter、partitioned filter、旧式 per-block filter 的读路径不同；Ribbon、prefix Bloom、cache-local 布局又各自在空间、CPU 和最坏读流量之间做取舍。

## 学习目标

- 能解释位数、哈希数与误判率：`m/n`、`k` 与标准公式 `p ≈ (1 - e^(-k/(m/n)))^k` 的关系，以及为什么提高 bits-per-key 的收益是非线性的。
- 能独立完成：对不同 bits-per-key 测量不存在键读取的 IO 和误判率，并说明实测值来自哪组查询、哪个计数器。结合实验还能分清「过滤器判负」「过滤器判正但数据块没有」「已有键被过滤器错误拒绝」三种情况，并比较 full、partitioned、per-block 布局的内存和查询成本。

## 为什么后端要懂

内容后台的读取并不总是「读一篇存在的文章」。编辑打开一个已被删除的草稿、读者点开一个过期链接、审核系统按标题查一个从未发布的版本，都会产生不存在键的查询。LSM 不会因为键不存在就跳过所有文件：它仍要按范围判断每个 SST 是否可能包含这个键；在 L0 重叠时，一次查询可能碰 8 个文件。没有过滤器，8 次范围检查之后往往就是 8 次数据块读取。过滤器把大部分查询挡在数据块之前，直接决定这类请求的尾延迟和磁盘压力。

但过滤器不是「开了就快」。bits-per-key 设得太低，误判率仍然很高，读放大只是从 8 次降到 6 次；设得太高，过滤器占用内存和构建时间，查询还要多算几次哈希。第 107 课已经说明 block cache、memtable 和过滤器都在抢同一台机器的内存预算；本课要补的是：**过滤器的收益要用「避免了多少次数据块读取」来衡量，而不是只看误判率的小数点**。另外，过滤器只解决「不存在」的读放大；对确实存在的键，最后一层仍然要读数据块，命中型负载不能指望它把这次读也省掉。

## 核心知识点

### 1. 位数、哈希数与误判率

标准 Bloom Filter 有一个 `m` 位的位数组和 `k` 个哈希函数。插入一个键时，算出 `k` 个位置并置 1；查询时算出同一组位置：只要有一个为 0，就返回「一定不在」；全为 1 就返回「可能在」。它不存储键本身，所以不能用「值是否相等」来纠错；它也不保证「全为 1」一定存在。节点把这条语义写成：允许假阳性，但在正确维护条件下不应产生假阴性。

误判率来自位数组被其他键占满。假设哈希分布均匀，插入 `n` 个键后某一位仍为 0 的概率约为 `e^(-kn/m)`，因此误判率为：

```text
p ≈ (1 - e^(-k / (m/n)))^k
```

在给定 `m/n`（也就是 bits-per-key）时，使 `p` 最小的哈希数是 `k ≈ (m/n) * ln(2)`。教学实现取 `k = round(bits_per_key * ln(2))`，所以 3 bits/key 用 2 个探针，5 用 3 个，10 用 7 个，16 用 11 个。RocksDB 的旧式 `LegacyNoLocalityBloomImpl::ChooseNumProbes` 用的是 `bits_per_key * 0.69`，本质上也是这个 `ln(2)` 的近似；新的 FastLocalBloom 有自己的实现，不能把教学公式直接当成它的内部细节。

教学实验用同一份 4,000 键的 SST、250 个数据块，查 20,000 个不存在的键。结果如下，数字来自 `results.json`，没有改动：

| bits/key | k | 理论误判率 | 实测误判率 | 假阳性 | 数据块读取 | 节省读取 | 过滤器字节 | 构建秒 | 查询秒 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0（无过滤器） | 0 | 1.0 | 1.0 | 0 | 20000 | 0 | 0 | 0.000694 | 0.094908 |
| 3 | 2 | 0.23676290005054274 | 0.23975 | 4795 | 4795 | 15205 | 1500 | 0.005532 | 0.061139 |
| 5 | 3 | 0.09184883923294047 | 0.09305 | 1861 | 1861 | 18139 | 2500 | 0.009597 | 0.049402 |
| 10 | 7 | 0.008193722065862417 | 0.00805 | 161 | 161 | 19839 | 5000 | 0.008443 | 0.040175 |
| 16 | 11 | 0.0004587107308146284 | 0.00055 | 11 | 11 | 19989 | 8000 | 0.010129 | 0.045778 |

无过滤器时，每个不存在的键都要读一次数据块，20,000 次查询就是 20,000 次数据块读取，查询耗时 0.094908 秒。3 bits/key 把读取降到 4,795 次，省下 15,205 次，相当于无过滤器效果的 76.025%；5 bits/key 降到 1,861 次，省 18,139 次，效果 90.695%；10 bits/key 降到 161 次，省 19,839 次，效果 99.195%；16 bits/key 降到 11 次，省 19,989 次，效果 99.945%。实测误判率和理论值在同一量级，但不会完全相等，因为哈希、位数组取整和这 20,000 个查询的具体分布都会带来偏差。

这张表最重要的一行不是 16，而是 10 到 16 的差：过滤器从 5,000 字节涨到 8,000 字节，多花 3,000 字节，数据块读取只从 161 降到 11，只多省 150 次。理论误判率从 0.008193722065862417 降到 0.0004587107308146284，看起来降了一个数量级以上，但绝对次数已经很小，继续加位数的边际收益很低。这就是节点权衡里的「更低误判率需要更多内存和构建成本」。构建耗时从 3 bits/key 的 0.005532 秒到 16 bits/key 的 0.010129 秒，中间有测量噪声（例如 5 bits/key 的 0.009597 秒高于 10 bits/key 的 0.008443 秒），不能逐点比较；总体上是位数越多，构建越贵。查询耗时也一样：10 bits/key 是 0.040175 秒，16 bits/key 反而到 0.045778 秒，因为探针从 7 次加到 11 次，而能再省下的数据块读取已经只剩 150 次；教学实现里这些「读取」只是计数，墙钟时间会把额外的哈希成本露出来。

官方文档对这条曲线给了一组更直观的比例：1.5 bits/key（50% 误判）相当于 100 bits/key 效果的 50%；2.9 bits/key（25% 误判）相当于 75%；4.9 bits/key（10% 误判）相当于 90%；9.9 bits/key（1% 误判）相当于 99%；15.5 bits/key（0.1% 误判）相当于 99.9%。这组数字说明的是**收益非线性**：从 50% 降到 25% 只多花 1.4 bits/key，但从 1% 降到 0.1% 要再花 5.6 bits/key。文档还建议：如果纠结要不要开过滤器、而且内存紧张，应该拿 `NewBloomFilterPolicy(3)` 和「不用过滤器」比，而不是拿 10 和「不用」比，因为 10 bits/key 挡住的大部分读取在 3 bits/key 时已经能挡住。

误判不影响最终正确性。10 bits/key 的 161 次假阳性，每一次都跟着一次数据块读取，Get 最终仍返回 `not_found`；这组实验里 `missing_hits=0`，20,000 个不存在的键没有编造出任何标题。反过来，4,000 个已有键在 0、3、5、10、16 各档都是 `hits=4000`、`false_negatives=0`，没有一次被过滤器错误拒绝。过滤器说「可能在」只是让读路径继续走；它说「一定不在」才是跳过。正确维护的过滤器不会对已有键说「一定不在」，这正是本课验收标准里的「对已有键无错误拒绝」。

观测误判率时，口径要写清楚。官方文档给出的点查观测公式是：

```text
(positives - true positives) / (positives - true positives + true negatives)
```

其中 `positives` 是过滤器返回「可能在」的次数，`true positives` 是其中真的存在的次数，`true negatives` 是过滤器正确判负的次数。对应的 RocksDB 统计是 `rocksdb.bloom.filter.useful`（真负）、`rocksdb.bloom.filter.full.positive`（判正）和 `rocksdb.bloom.filter.full.true.positive`（判正且真有）。教学实验用的也是同一思路：10 bits/key 时判正 161 次、真阳 0 次、真阴 19,839 次，所以 161 / (161 + 19,839) = 0.00805。

这里还要避免把一个容易混淆的官方数字搬进本课实验。RocksDB 文档里的 cache-local 变体会把过滤器切成 `s` 片，每片 `m/s` 位；标准配置 `m/s=512`、`m/n=10`，于是 `n/s=51.2`。文档用 `k=6`，把 `n` 换成 `51.2±7.16`、`m=512`，算出 0.43% 与 1.48%，平均 0.95%；同一组参数下，无局部性的标准 Bloom 是 0.84%。本课教学过滤器是标准（非 cache-local）实现，10 bits/key 时 `k=7`，实测是 0.00805；**不要把 0.95% 写成我们实验的数字**。

### 2. 分层过滤

LSM 的一次 Get 先看 memtable，再从 L0 到更底层逐层找。每一层的每个 SST 都有 `smallest` 和 `largest`：如果查询键落在范围外，直接跳过；如果落在范围内，没有过滤器就要读索引、读数据块。L0 的文件之间通常范围重叠，所以一个中间范围的键可能同时落在 8 个文件里。第 106 课把这种「查一个键要碰多个文件」叫读放大；Bloom 过滤器放在每个 SST 上，作用就是把「范围命中但键不存在」的文件尽早判负。

教学实验用 8 个 L0 SST、每个 500 个键，键交错分布，因此 8 个文件的范围几乎覆盖同一段 `article:NNNN`。查 8,000 个位于公共范围内的不存在键：

| 配置 | 过滤器内存 | 数据块读取 | 每查询读取 | 过滤器判负（useful） | 假阳性 |
|---|---:|---:|---:|---:|---:|
| 无过滤器 | 0 | 64000 | 8.0 | 0 | 0 |
| 10 bits/key | 5000 | 601 | 0.075125 | 63399 | 601 |
| 16 bits/key | 8000 | 38 | 0.00475 | 63962 | 38 |

无过滤器时，每个查询都碰 8 个文件：`range_checks=64000`、`range_skips=0`、`data_block_reads=64000`，查询耗时 0.065814 秒。10 bits/key 时，过滤器检查 64,000 次，其中 63,399 次正确判负，601 次假阳性；数据块读取降到 601，每查询 0.075125 次，约为无过滤器的 1/106。16 bits/key 时只剩 38 次数据块读取，每查询 0.00475 次。

注意耗时：10 bits/key 的查询耗时是 0.12756 秒，比无过滤器的 0.065814 秒还长。这不是说过滤器没有用，而是因为教学实现里每个查询要在内存中走 8 遍探针，而「数据块读取」只是计数器，不是真实磁盘 IO。这个实验省下的是 64,000 次到 601 次的数据块读取；在真实引擎里，这些读取可能命中块缓存，也可能落到磁盘，收益取决于缓存命中和设备延迟。教学墙钟时间不能直接当成生产延迟。

对已有键，过滤器同样减少「查错文件」的放大。4,000 个已有键在三档配置下都全部命中、假阴性为 0。无过滤器时，查一个已有键会先碰到其他重叠 SST，数据块读取合计 17,972 次，每查询 4.493 次；10 bits/key 降到 4,137 次，每查询 1.03425 次，其中 `bloom_tp=4000`、对其他文件的假阳性 137 次、过滤器正确判负 13,835 次；16 bits/key 降到 4,007 次，每查询 1.00175 次。已有键最终都要在包含它的那个文件里读一次数据块，所以每查询至少还有约 1 次；过滤器能做的是把「查错文件」的次数压掉。

分层过滤还有一个生命周期问题。官方文档说，过滤器嵌在每个 SST 里，各层通常同样构建；`optimize_filters_for_hits` 可以跳过最后一层。合并两个 SST 时，引擎不会对旧过滤器做 AND/OR，而是按新文件的键从头构建，以保证可预期的 bits-per-key 和误判率。打开 SST 时按文件里的原始参数加载 Bloom 或 Ribbon，两种 Policy 都能读对方；关闭 SST 后从内存拿掉。若要把过滤器放进 block cache，设 `cache_index_and_filter_blocks=true`。

`optimize_filters_for_hits` 的取舍可以用教学实验看清楚。实验有 L0=100、L1=200、L2=4,000 个键，查 8,000 个落在 L2 范围内的不存在键，以及 4,000 个只存在于 L2 的已有键。所有层都带过滤器：过滤器内存 5,375 字节，不存在键的数据块读取只有 60 次（每查询 0.0075，`bloom_useful=7940`，假阳性 60）；但 4,000 个 L2 命中仍要读 4,000 次数据块（`bloom_tp=4000`、`bloom_useful=0`）——命中时最后一层过滤器省不掉数据块。跳过最底层过滤器：内存降到 375 字节，不存在键的数据块读取回到 8,000 次，命中仍是 4,000 次数据块、假阴性 0。这个选项省的是「最大那一层」的过滤器内存，代价是每个不存在键都必须读最底层数据块；它适合命中型负载，不适合「大量查询落在最底层且多数不存在」的负载。

### 3. 过滤器内存和查询成本

Bloom 的第一个成本是内存。位数组按 `bits_per_key * n` 分配，10 bits/key、4,000 个键就是 40,000 位，正好 5,000 字节；16 bits/key 是 64,000 位，8,000 字节。过滤器通常既占内存又在 SST 里占磁盘：官方文档说降低 bits-per-key 会省过滤器空间，但过滤器「几乎都会同时占内存和磁盘」。构建过滤器要遍历文件的键并做 `k` 次哈希；查询过滤器要做 `k` 次探针。在按 bits-per-key 取最优哈希数的前提下，位数越多、探针越多，误判率越低，但每次查询的 CPU 也越高；如果超过最优 `k` 继续加探针，误判率反而会上升。RocksDB 的新 full filter 还会把探针限制在同一条 CPU cache line 内，减少 cache miss，这属于实现层面的布局优化，教学实现没有模拟。

同一份 4,000 键 SST、10 bits/key、20,000 次不存在查询，四种布局的数字如下：

| 布局 | 过滤器内存 | 过滤器加载/查询 | 数据块读取 | 索引读取 | 分区索引读取 | 实测误判率 |
|---|---:|---:|---:|---:|---:|---:|
| none | 0 | 0.0 | 20000 | 20000 | 0 | 1.0 |
| full | 5000 | 5000.0 | 161 | 161 | 0 | 0.00805 |
| partitioned（8 分区） | 5000 | 624.025 | 176 | 176 | 20000 | 0.008815426997245178 |
| block（旧格式） | 5000 | 20.0 | 273 | 20000 | 0 | 0.01365 |

`none` 有 250 个数据块，20,000 次查询读 20,000 次数据块，也读 20,000 次索引。`full` 是整个 SST 一个过滤器：过滤器 5,000 字节，负结果可以连索引都不查，所以索引读取和数据块读取都只有 161 次，正好等于假阳性次数；每查询加载 5,000 字节过滤器（教学计数）。`partitioned` 把同一份过滤器按 key 范围切成 8 个小 filter block，过滤器总量仍是 5,000 字节，但每查询只加载一个分区，平均 624.025 字节；顶层分区索引读取 20,000 次，数据块读取 176 次，实测误判率 0.008815426997245178，比分片前略高，这是分片后各分区键数不完全均匀导致的预期现象，不要改成和 full 一样。`block` 是旧格式，每个数据块一个小过滤器，共 250 个：每查询先读索引 20,000 次，再加载 20.0 字节的块过滤器，数据块读取 273 次，实测误判率 0.01365。三种有过滤器的布局对已有键都是 4,000 命中、0 假阴性。

读路径的顺序值得单独记：full 和 partitioned 是「范围检查 → 过滤器（partitioned 还要先查分区索引）→ 索引 → 数据块」；旧式 block 是「范围检查 → 索引 → 块过滤器 → 数据块」。所以「过滤器判负」并不总是免费的：full filter 判负可以连索引都不查；旧式 per-block filter 判负之前已经付了索引的代价；partitioned 判负之前付了顶层分区索引的代价。官方文档也明确说，旧格式每个约 2KB 的数据块一个小 Bloom，读时先查 SST index 得到块偏移，再加载对应过滤器，可能存在才读数据块，因此「过滤器判负」也已经付了 index 的代价。

官方文档还给了几个实现层面的关键差异。full filter 是整个 SST 一个过滤器，负结果可以连 index 都不查；探针被限制在同一条 CPU cache line（相当于对 Bloom 空间分片），键多时对误判率影响很小。`format_version=5`（RocksDB 6.6 及以后）换用新实现，源码里是 FastLocalBloom，使用 64 位哈希；旧 full filter 即使 100 bits/key 也很难低于约 0.1% 误判，新实现 16 bits/key 即可低于 0.1%，并能量到单过滤器数十亿键。`partitioned` 与 full filter 使用同一块格式，但按键范围切成多个更小的 filter block，用顶层 index 按需加载。开启方式是 `index_type=kTwoLevelIndexSearch` 且 `partition_filters=true`；不能只开 partitioned filters 而不开 partitioned index。它增加平均 CPU，但配合 `cache_index_and_filter_blocks=true` 时能显著减少单次小读取载入 block cache 的最坏流量。文档举例：256MB SST 的 index/filter 大约 0.5MB/5MB，远大于 4–32KB 的数据块。

Ribbon filter 是可替换实现，从 RocksDB 6.15.0 起可用。官方文档说它约为 Bloom 省 30% 空间，过滤器 CPU 约 3–4 倍（主要在后台构建）；`NewRibbonFilterPolicy(9.9)` 与 Bloom 同样约 1% 误判，但大约只用 7 bits/key；构建期临时内存约 231 bits/key（Bloom 约 74）。头文件还写：传入 10 时与 Bloom 同样约 0.95% 误判、约 7 bits/key。可以用 `bloom_before_level` 在上层用 Bloom、下层用 Ribbon。本课教学引擎没有实现 Ribbon，**不要把上面这组实验数字写成 Ribbon 的数字**。

前缀过滤器是另一个空间/误判的取舍。官方文档说，默认 `whole_key_filtering=true`，把完整键加入过滤器；关掉后，若设置了 `prefix_extractor`，则加入前缀哈希。前缀更少，过滤器更小，但误判更高；前缀 Bloom 还可以在 `Seek` / `SeekForPrev` 使用（创建 iterator 时 `check_filter`），整键 Bloom 只用于点查。自定义 comparator 若忽略键的某部分，必须同时提供忽略相同部分的 FilterPolicy，否则过滤器和比较器对「什么是同一个键」的理解会不一致。

最后是配置语义。官方文档的配置示例是 `table_options.filter_policy.reset(NewBloomFilterPolicy(10, false));`，约 10 bits/key，可以连续配置约 1 到 20 或更高。提高 bits-per-key 的收益是减少 Get（以及带 prefix Bloom 的 Seek）相关的 I/O、CPU 和 block cache churn；降低则省过滤器空间。`include/rocksdb/filter_policy.h` 的注释说，9.9 bits/key 是一个好选择，误判约 1%；`bits_per_key < 0.5` 视为 0（不生成过滤器），0.5 到 1.0 升为 1.0（约 62% 误判）。自 RocksDB 7.0 起，`NewBloomFilterPolicy` 的 `use_block_based_builder` 参数已被忽略，旧的 block-based 过滤器不再能从公开 API 启用。这些是官方语义，不是教学引擎的默认值。

## 常见误区

**标准 Bloom Filter 不能直接通过清位安全删除元素。** 位是被多个键共享的：清掉某个键的探针位，可能把其他键的探针也清掉，于是那些仍然存在的键被过滤器判负，产生假阴性。教学实验用 200 个键、8 bits/key、`k=6`、1,600 位。把 `article:0000:title` 的 6 个探针清掉后，另外 4 个仍在集合里的键变成了假阴性：`article:0017:title`、`article:0019:title`、`article:0031:title`、`article:0179:title`。用剩下 199 个键重建过滤器，假阴性回到 0，重建耗时 0.000349 秒，被删键 `may_contain=false`。这就是「正确维护才没有假阴性」：删除靠墓碑，过滤器在 flush/compaction 时按新文件的键重建，而不是改旧位图。

**墓碑必须进入该 SST 的过滤器。** 教学实验里，L1 有 `article:0001:title = Weekly digest`，L0 里是同一键的墓碑。过滤器包含墓碑键时，Get 读到 tombstone，旧标题被挡住，数据块读取 1 次。若构建过滤器时漏掉墓碑键，L0 过滤器会判负并跳过这支 SST，Get 落到 L1，错误读回 `Weekly digest`。压实到最底层后墓碑可以丢弃，重建的过滤器不再包含该键：本次 `may_contain_after_rebuild=false`，`records=70`，Get 为 miss，不会复活标题。

**过滤器判正不等于键存在。** 误判率 0.00805 的意思是，在 20,000 个不存在的查询里大约有 161 次过滤器说「可能在」；这 161 次都会继续读数据块，然后返回 `not_found`。如果应用把 Bloom 的「可能在」当成「一定在」，就会把不存在的标题当成命中。过滤器只负责减少不必要的读取，不负责给出最终值。

**过滤器不是越大约好，也不是所有布局都省同一种成本。** 10 到 16 bits/key 多花 3,000 字节只多省 150 次数据块读取；full filter 判负可以跳过索引，旧式 block filter 判负前已经读了索引，partitioned 判负前已经读了顶层分区索引。把「误判率更低」直接等同于「读路径更便宜」，会忽略这些固定成本。

**不要用教学数字替代 RocksDB 的实现数字。** 本课 10 bits/key 的 0.00805 是标准非 cache-local Bloom 在这 20,000 个查询上的实测值；官方文档里的 0.95% 是 cache-local 布局、`k=6`、`m/s=512` 的示例计算。两者不是同一个实现，也不是同一个测量口径。

## 设计权衡

节点的权衡是：更低误判率需要更多内存和构建成本。本课实验把这条权衡量化了：3 bits/key 用 1,500 字节把 20,000 次数据块读取压到 4,795 次；10 bits/key 用 5,000 字节压到 161 次；16 bits/key 用 8,000 字节压到 11 次。从 3 到 10，多花 3,500 字节，少读 4,634 次；从 10 到 16，多花 3,000 字节，只少读 150 次。选择 bits-per-key 时，应该先估计「不存在的查询占多少」「每次数据块读取有多贵」「过滤器内存预算有多大」，再看曲线的哪一段值得买。

官方文档的收益比例也支持这个判断：1.5 bits/key 已经有 100 bits/key 效果的 50%，4.9 bits/key 有 90%，9.9 bits/key 有 99%。如果内存紧张，`NewBloomFilterPolicy(3)` 和「不用过滤器」的对比，比 10 和「不用」的对比更能说明问题。反过来说，如果不存在查询很多、过滤器内存相对便宜，9.9 bits/key 左右是文档推荐的一个好选择；再往上就要接受边际收益递减。

布局也是权衡。full filter 的负结果最便宜，连索引都不用查，但每查询要加载整个过滤器（教学计数 5,000 字节）。partitioned filter 把总量保持在 5,000 字节，每查询只加载一个分区，平均 624.025 字节，代价是每次查询多一次顶层分区索引读取，而且分片后误判率略高（0.008815426997245178 对 0.00805）。旧式 block filter 每查询加载 20.0 字节，但先读索引 20,000 次，数据块读取 273 次，误判率 0.01365；它省的是单次加载的字节数，不是索引成本。真实引擎里还要把 block cache 命中、过滤器是否驻留内存、以及 `cache_index_and_filter_blocks` 的配置一起看。

分层上，`optimize_filters_for_hits` 是「省最大一层的内存」和「不存在键必须读最底层」之间的交换。教学实验里，所有层都建过滤器时内存 5,375 字节、不存在键读 60 次；跳过最底层后内存 375 字节、不存在键读 8,000 次。命中仍是 4,000 次数据块读取，因为最后一层过滤器本来也省不掉命中键的数据块。命中型负载可以接受这个交换；以不存在查询为主、且底层文件很大的负载，则要保留最底层过滤器。

Ribbon 是另一条曲线：官方说它约省 30% 空间，但过滤器 CPU 约 3–4 倍，构建期临时内存约 231 bits/key（Bloom 约 74）。它适合空间紧张、能接受后台构建成本、且读多写少的场景；`bloom_before_level` 还允许上层 Bloom、下层 Ribbon，把两种过滤器的成本分开。本课没有实现 Ribbon，这条权衡只按官方文档理解，不要用教学实验的 10/16 bits/key 数字去推算 Ribbon。

## 动手实践：对不同 bits-per-key 测量不存在键读取的 IO 和误判率

配套代码在 `/workspace/atlas/lessons/practice108`，用 Python 3.13.5 标准库写成，不链接 RocksDB，也不走网络。运行 `./test.sh` 会执行全部实验并写下 `results.json`。本次已跑通，输出 `ALL_CHECKS_PASSED`。下面的数字都来自这份结果，不要改写。

### 0. 文件结构

- `engine.py`：教学 `BloomFilter`、`SSTFile`、`BloomLSM`。标准位数组 Bloom、full / partitioned / block 三种过滤器布局、固定 16 条一块的教学 SST、范围检查和过滤器检查的读路径、墓碑与压实重建。
- `app.py`：七组实验。bits-per-key 曲线、正确性、危险删除、分层读放大、过滤器布局、`optimize_filters_for_hits`、墓碑与过滤器。
- `test_bloom.py` / `test.sh`：验收并写出 `results.json`。

### 1. 准备数据与目标操作

教学键是 `article:0000:title` 到 `article:3999:title`，值在五个标题之间轮换。每个 SST 块放 16 条记录，所以 4,000 个键有 250 个块。20,000 个不存在键形如 `article:NNNN:title~c`，落在 `(smallest, largest)` 之内，避免被范围检查直接跳过。目标是：对不同 bits-per-key 测量不存在键的 IO 和误判率；验证假阳性不影响最终正确性、已有键无错误拒绝；观察 8 个重叠 L0 文件上的读放大；比较 full / partitioned / block 布局；观察 `optimize_filters_for_hits` 的内存与读放大交换；最后用清位删除和墓碑实验说明「正确维护」的边界。

### 2. 要比较的几条路径

1. 0、3、5、10、16 bits/key 的过滤器，对 20,000 个不存在键和 4,000 个已有键的读取与误判。
2. 10 bits/key 下，161 次假阳性是否都跟着数据块读取，Get 是否仍返回 not_found。
3. 清掉一个键的探针位，是否会把其他已有键变成假阴性；用剩余键重建是否恢复。
4. 8 个交错 L0 SST、8,000 个不存在键：无过滤器、10 bits/key、16 bits/key 的数据块读取与耗时。
5. 同一 SST 的 none / full / partitioned / block 四种布局：数据块读取、索引读取、分区索引读取、过滤器加载字节、误判率。
6. L0=100、L1=200、L2=4,000 时，所有层建过滤器与跳过最底层过滤器的内存、缺失读取、命中读取。
7. 墓碑是否进入过滤器：包含墓碑、漏掉墓碑、压实后重建三种结果。

### 3. 运行并解读（真实输出）

```text
ALL_CHECKS_PASSED
SUMMARY bpk3_fpr=0.239750 bpk5_fpr=0.093050 bpk10_fpr=0.008050 bpk16_fpr=0.000550 bpk10_fp=161 bpk10_io=161 bpk10_mem=5000 no_io=20000 fn=0 unsafe_fn=4 rebuild_fn=0 layer_no_io=64000 layer_b10_io=601 layer_b16_io=38 full_load=5000.00 part_load=624.02 block_load=20.00 opt_miss_io=60 skip_miss_io=8000 opt_mem=5375 skip_mem=375 tomb_ok=1 tomb_bug=1
```

### 4. 逐组对照真实数字

1. bits-per-key 曲线已经在第 1 节表里。补充构建耗时：无过滤器 0.000694 秒、3 bits/key 0.005532 秒、5 bits/key 0.009597 秒、10 bits/key 0.008443 秒、16 bits/key 0.010129 秒。查询 20,000 个不存在键的耗时分别是 0.094908、0.061139、0.049402、0.040175、0.045778 秒。所有档位对 4,000 个已有键都是 `hits=4000`、`false_negatives=0`；20,000 个不存在键都是 `missing_hits=0`，不会编造出标题。
2. 10 bits/key 的 161 次假阳性，每一次都跟着一次数据块读取；`data_block_reads_on_missing=161`，`fp_then_data_read=true`。Get 仍返回 not_found，`missing_not_found=20000`，`false_positive_does_not_invent_value=true`。已有键 4,000 全部命中，`no_false_negative=true`。过滤器说「可能在」只是许可去读 SST，不是最终答案。
3. 200 个键、8 bits/key、`k=6`、1,600 位。清掉 `article:0000:title` 的 6 个探针后，`unsafe_false_negatives=4`，例子就是 `article:0017:title`、`article:0019:title`、`article:0031:title`、`article:0179:title`。用剩下 199 个键重建：`rebuild_false_negatives=0`，`rebuild_s=0.000349`，`deleted_key_may_contain_after_rebuild=false`，`remaining_all_present=true`。
4. 8 个 L0 SST、每个 500 个键、8,000 个不存在键。无过滤器数据块读取 64,000、每查询 8.0、耗时 0.065814 秒；10 bits/key 数据块读取 601、每查询 0.075125、耗时 0.12756 秒；16 bits/key 数据块读取 38、每查询 0.00475、耗时 0.159133 秒。已有 4,000 键在三档都全部命中、假阴性 0；无过滤器查已有键读 17,972 次数据块（每查询 4.493），10 bits/key 降到 4,137 次（每查询 1.03425，`bloom_tp=4000`、对其他文件的假阳性 137、useful 13,835），16 bits/key 降到 4,007 次（每查询 1.00175）。
5. 同一份 4,000 键 SST、10 bits/key、20,000 次不存在查询。none：250 个数据块，读取 20,000，索引读取 20,000。full：过滤器 5,000 字节，误判 0.00805，数据块 161，索引 161，每查询加载过滤器 5,000 字节。partitioned（8 个分区）：过滤器合计仍 5,000 字节，每查询只加载一个分区，平均 624.025 字节，顶层分区索引读取 20,000，数据块 176，实测误判率 0.008815426997245178。block（旧格式，每数据块一个小过滤器，共 250 个）：每查询先读索引 20,000 次再加载 20.0 字节的块过滤器，数据块 273，误判 0.01365。三种有过滤器的布局对已有键都是 4,000 命中、0 假阴性。
6. L0=100、L1=200、L2=4,000，查 8,000 个落在 L2 范围内的不存在键，以及 4,000 个只存在于 L2 的已有键。所有层都带过滤器：过滤器内存 5,375 字节，不存在键数据块读取 60（每查询 0.0075，useful 7,940，假阳性 60），L2 命中仍要读 4,000 次数据块（`bloom_tp=4000`、`useful=0`）。跳过最底层过滤器：内存降到 375 字节，不存在键数据块读取回到 8,000，命中仍是 4,000 次数据块、假阴性 0。这个选项省的是「最大那一层」的过滤器内存，代价是不存在键必须读最底层数据块；命中型负载才划算。
7. 墓碑实验：L1 有 `article:0001:title = Weekly digest`，L0 有同一键的墓碑。过滤器包含墓碑键时，Get 读到 tombstone，旧标题被挡住，`data_block_reads=1`；漏掉墓碑键时，L0 过滤器判负并跳过这支 SST，Get 落到 L1，错误读回 `Weekly digest`。压实到最底层后墓碑可丢弃，重建的过滤器不再包含该键：`may_contain_after_rebuild=false`、`records=70`，Get 为 `miss_data`，不会复活标题。

### 5. 对照验收标准

```text
# 验收 1：能解释误判不影响最终正确性
# 10 bits/key 下 161 次假阳性，每次都跟着数据块读取；
# Get 仍返回 not_found，missing_hits=0；
# 4,000 个已有键全部命中，false_negatives=0。
# 过滤器说「可能在」只是许可去读，不是最终答案。

# 验收 2：对已有键无错误拒绝
# 0、3、5、10、16 bits/key 各档 existing_hits=4000、false_negatives=0；
# unsafe_delete 实验说明清位会制造假阴性，重建后 rebuild_false_negatives=0。
```

### 6. 自己动手的扩展练习

- 把不存在查询改成「一半落在 `(smallest, largest)` 之外」，观察范围检查先跳过时，过滤器检查次数和误判率的分母怎么变。范围外的查询不应该算进过滤器误判率。
- 把 partitioned 的分区数从 8 改成 2 和 16，比较每查询加载字节、顶层分区索引读取和实测误判率；注意分区后误判率略高于 full 是预期现象。
- 把 `optimize_filters_for_hits` 打开，同时把查询改成以 L2 命中为主，观察「省内存」与「缺失查询必须读最底层」的差别。不要只看命中数，要看数据块读取。
- 给 `BloomFilter` 加一个计数器，记录 `may_contain` 的探针次数，验证 10 bits/key 的 7 个探针、16 bits/key 的 11 个探针是否与 `k = round(bits_per_key * ln(2))` 一致。

### 7. 和生产引擎的对应关系（不要记错边界）

- 教学 `BloomFilter` 是标准（非 cache-local）位数组，用 blake2b 两个 64 位哈希做 double hashing；RocksDB 的新 full filter 是 FastLocalBloom（`format_version=5`，64 位哈希，探针限制在同一条 CPU cache line）。教学公式 `k = round(bits_per_key * ln(2))` 对应旧式 `ChooseNumProbes` 的 `bits_per_key * 0.69` 思路，不是 FastLocalBloom 的全部实现。
- 教学 SST 是内存中按 key 排好序的记录列表，按固定条数切块；RocksDB 是 block-based 约 4 KiB 压缩块。教学 `data_block_read` 和 `filter_bytes_loaded` 都是计数器，不是真实磁盘 syscall，也不是真实 block cache 流量。
- 教学 `filter_mode` 的 full / partitioned / block 对应官方 full filter、partitioned filter 和旧式 per-block filter 的读路径思想；教学没有实现 `format_version`、真实 filter block 编码、`cache_index_and_filter_blocks`、prefix extractor 和 Ribbon。
- 官方文档里的 `NewBloomFilterPolicy(10, false)`、9.9 bits/key、`bits_per_key < 0.5`、RocksDB 7.0 起忽略 `use_block_based_builder`、Ribbon 的 6.15.0 / 30% / 3–4 倍 CPU / 231 与 74 bits/key，都是核对过的文档语义；本课实验用的是教学引擎，不能把这些官方数字改写成实验结果。

## 源码与文档阅读

按节点的 `sourceStudy`：阅读 RocksDB Bloom Filter 文档与 `filter_policy` 实现，关联过滤器布局和不存在键读取路径。2026-11-12 实际打开页面：facebook 入口均为 301，跳转到 `meta/rocksdb` 后返回 200；`raw.githubusercontent.com/facebook/rocksdb/main` 返回 200。下面保留节点和文档里给出的 facebook URL。

- [RocksDB Bloom Filter](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter)（节点给出的入口）：Bloom 的基本语义、配置示例、bits-per-key 的非线性收益、full filter 与旧式 per-block filter 的差别、prefix vs whole key、点查误判率的统计口径。用它核对「过滤器判负一定不在、判正可能在」以及 `NewBloomFilterPolicy(10, false)` 的语义。
- [Partitioned Index Filters](https://github.com/facebook/rocksdb/wiki/Partitioned-Index-Filters)：partitioned filter 与 full filter 的同一块格式、按键范围切分、顶层 index 按需加载、必须同时开启 `kTwoLevelIndexSearch` 和 `partition_filters`、增加平均 CPU 但减少最坏读流量。对照本课 partitioned 的 624.025 字节/查询、20,000 次分区索引读取和 176 次数据块读取。
- [Prefix Seek](https://github.com/facebook/rocksdb/wiki/Prefix-Seek)：prefix extractor、`whole_key_filtering`、prefix Bloom 在 `Seek` / `SeekForPrev` 与 `check_filter` 中的使用边界。整键 Bloom 只用于点查，不要把它说成迭代器前缀过滤。
- [RocksDB BlockBasedTable Format](https://github.com/facebook/rocksdb/wiki/Rocksdb-BlockBasedTable-Format)：filter block 在 SST 中的位置、旧式 per-block 布局与 full filter 的差别。对照本课 block 布局「先索引、再块过滤器、再数据块」的顺序。
- [Memory usage in RocksDB](https://github.com/facebook/rocksdb/wiki/Memory-usage-in-RocksDB)：过滤器内存、block cache、索引和 `cache_index_and_filter_blocks` 的关系。用它解释过滤器如何与 block cache、memtable 一起计入内存预算，以及 `cache_index_and_filter_blocks` 会把过滤器从进程堆改放到块缓存里。

源码阅读按「先看 Policy 接口，再看构建与查询」两条线走。不要编造行号或未核对的函数名。

- `include/rocksdb/filter_policy.h`：`NewBloomFilterPolicy`、`NewRibbonFilterPolicy`、`FilterPolicy::GetBuilderWithContext` / `GetFilterBitsReader`、`FilterBuildingContext`（含 `level_at_creation`、`is_bottommost`）。这里能看到 Bloom / Ribbon 的选择，以及「构建时知道层级」这件事如何支撑 `optimize_filters_for_hits`。
- `util/bloom_impl.h`：`BloomMath::StandardFpRate`（标准公式 `(1-e^(-k/(m/n)))^k`）、`CacheLocalFpRate`；`LegacyNoLocalityBloomImpl::ChooseNumProbes` 使用 `bits_per_key * 0.69`（约 `ln(2)`）；`FastLocalBloomImpl::ChooseNumProbes` / `AddHash` / `HashMayMatch`。对照本课 `k = round(bits_per_key * ln(2))`，注意教学实现不等于 FastLocalBloom。
- `table/block_based/filter_policy.cc`：`FastLocalBloomBitsBuilder` / `FastLocalBloomBitsReader`，构建与查询走 `AddHash` / `HashMayMatch`。看新 full filter 如何把探针限制在同一条 cache line 内。
- `table/block_based/full_filter_block.h`：`FullFilterBlockReader::KeyMayMatch` / `PrefixMayMatch`。对照本课 full 布局「判负可以连 index 都不查」。
- `table/block_based/partitioned_filter_block.h`：`PartitionedFilterBlockBuilder` / `PartitionedFilterBlockReader`，以及 `GetFilterPartitionHandle` / `GetFilterPartitionBlock`。对照本课 partitioned 的顶层分区索引和按需加载。
- `table/block_based/block_based_table_reader.cc`：`PrefetchIndexAndFilterBlocks`、`CreateFilterBlockReader`（在 `kFullFilter` 与 `kPartitionedFilter` 之间选择）、`FullFilterKeyMayMatch`、`MultiGetFilter` 调用 `FullFilterKeysMayMatch`。这些函数把「过滤器布局」接到「不存在键读取路径」上。

本课没有打开 `db_impl` 的 Get 实现文件，因此**不声称核对过 `DBImpl::Get` 的完整调用链**。阅读时记下三件事：你打开页面的日期和最终 URL；`Bloom-Filter` / `Filter-Policy` 这两个 wiki 路径当日不存在，不要引用；文档里的 0.95% 是 cache-local 示例，不是本课标准 Bloom 的实测值。

## 自测题

**1.** 过滤器返回「可能在」之后，为什么最终仍可能不存在？教学里 10 bits/key 的 161 次假阳性，怎样证明没有影响正确性？

**答案：** Bloom 只回答「集合中有没有可能包含这个键」，不存储值，也不保证判正一定存在。过滤器判正只是允许读路径继续查索引和数据块；真正的结果仍要由数据块和版本判断给出。教学实验里 161 次假阳性每一次都跟着一次数据块读取（`data_block_reads_on_missing=161`、`fp_then_data_read=true`），但 Get 全部返回 `not_found`，`missing_hits=0`，20,000 个不存在的键没有编造出标题。反过来，4,000 个已有键全部命中、`false_negatives=0`，说明过滤器没有错误拒绝已有键。误判影响的是读放大，不是最终正确性。

**2.** 标准 Bloom Filter 为什么不能通过清位删除元素？正确删除路径是什么？墓碑为什么必须进入该 SST 的过滤器？

**答案：** 位数组的每一位被多个键共享。清掉被删键的探针位，可能同时清掉其他键的探针，使那些仍然存在的键被判负，产生假阴性。教学实验用 200 个键、8 bits/key、`k=6`、1,600 位，清掉 `article:0000:title` 的 6 个探针后，`article:0017:title`、`article:0019:title`、`article:0031:title`、`article:0179:title` 变成假阴性；用剩下 199 个键重建后 `rebuild_false_negatives=0`，耗时 0.000349 秒，被删键 `may_contain=false`。正确做法是用墓碑表达删除，并在 flush/compaction 时按新文件的键重建过滤器。墓碑必须进过滤器，否则 L0 过滤器会判负并跳过这支 SST，Get 落到 L1 读回旧标题 `Weekly digest`；包含墓碑时 Get 读到 tombstone，旧值被挡住。压实到最底层后墓碑可丢弃，重建的过滤器不再包含该键，Get 为 miss，不会复活标题。

**3.** 为什么 10 到 16 bits/key 的收益递减？full、partitioned、block 三种布局对不存在键的代价有什么不同？

**答案：** 10 bits/key 用 5,000 字节把 20,000 次不存在查询的数据块读取压到 161 次；16 bits/key 用 8,000 字节压到 11 次。多花 3,000 字节只多省 150 次读取，因为绝对读取次数已经很小，理论误判率从 0.008193722065862417 降到 0.0004587107308146284 并不能带来同比例的绝对收益。布局上，full 是整个 SST 一个过滤器，负结果可以连索引都不查：5,000 字节过滤器、161 次数据块读取、161 次索引读取，误判 0.00805。partitioned 把过滤器切成 8 个分区，总量仍是 5,000 字节，每查询只加载一个分区，平均 624.025 字节，但要读 20,000 次顶层分区索引，数据块读取 176 次，误判 0.008815426997245178；分片后略高于 full 是预期现象。block 是旧格式，每查询先读索引 20,000 次，再加载 20.0 字节的块过滤器，数据块读取 273 次，误判 0.01365。选择布局时要看「省下的是数据块读取还是过滤器加载字节」，以及索引/分区索引是否已经成为固定成本。

## 推荐资源

- [Bloom Filter与读放大：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/RocksDB-Bloom-Filter)（github.com，英文文档，深入，免费）。重点阅读位数、哈希数与误判率、full filter 与旧式 per-block filter、partitioned filter、prefix vs whole key，以及点查误判率的统计口径，用于核对本节点实验的语义和边界。2026-11-12 该入口 301 到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/RocksDB-Bloom-Filter) 后返回 200；同一次核对建议打开 Partitioned-Index-Filters、Prefix-Seek、Rocksdb-BlockBasedTable-Format 和 Memory-usage-in-RocksDB。注意 `wiki/Bloom-Filter` 与 `wiki/Filter-Policy` 当日 301 后落到 wiki 首页，页面不存在，不要引用。

## 本课小结

- Bloom Filter 用位数组判断「一定不在」或「可能在」：任一探针为 0 就判负，全为 1 才判正。允许假阳性，正确维护时不应有假阴性。教学引擎是 Python 3 标准库的 `BloomFilter` / `SSTFile` / `BloomLSM`，不是 RocksDB；教学 SST 按固定条数切块，教学 Bloom 是标准非 cache-local 位数组，没有 FastLocalBloom、Ribbon、真实 block cache、WAL、MANIFEST、列族、快照、prefix extractor 或压缩。
- 位数、哈希数与误判率：4,000 键、20,000 个不存在查询下，无过滤器读 20,000 次；3 bits/key 读 4,795 次、过滤器 1,500 字节；5 bits/key 读 1,861 次、2,500 字节；10 bits/key 读 161 次、5,000 字节；16 bits/key 读 11 次、8,000 字节。10 到 16 多花 3,000 字节只多省 150 次读取，收益递减。所有档位对已有键都是 4,000 命中、0 假阴性；161 次假阳性都跟着数据块读取，但 Get 仍返回 not_found。
- 分层过滤：8 个交错 L0 SST 上，8,000 个不存在键无过滤器读 64,000 次（每查询 8.0），10 bits/key 降到 601 次（每查询 0.075125），16 bits/key 降到 38 次（0.00475）。已有键的读放大也从无过滤器的 17,972 次（每查询 4.493）降到 10 bits/key 的 4,137 次（1.03425）和 16 bits/key 的 4,007 次。`optimize_filters_for_hits` 把过滤器内存从 5,375 字节降到 375 字节，但不存在键读取从 60 次回到 8,000 次；命中仍要读 4,000 次数据块。
- 过滤器内存和查询成本：同一 SST 上，full 布局负结果连索引都不查（161 次数据块、161 次索引、每查询加载 5,000 字节）；partitioned 每查询平均加载 624.025 字节、分区索引 20,000 次、数据块 176 次、误判 0.008815426997245178；block 旧格式每查询先读索引 20,000 次、加载 20.0 字节块过滤器、数据块 273 次、误判 0.01365。官方文档还说明了 FastLocalBloom、partitioned filter 的开启条件、Ribbon 的 30% 空间/3–4 倍 CPU 取舍，以及 prefix vs whole key 的边界。
- 标准 Bloom 不能清位删除：清掉一个键的探针会让其他键变成假阴性；正确路径是墓碑 + 按新文件重建过滤器。墓碑必须进入该 SST 的过滤器，否则旧值会从下一层复活；压实到最底层后丢弃墓碑、重建过滤器，Get 才会稳定为 miss。
- 学完后可以用三句话自检：过滤器判正为什么不等于键存在，161 次假阳性如何仍返回 not_found；为什么 10 到 16 bits/key 的边际收益很低，full / partitioned / block 各自把成本花在哪里；清位删除为什么会制造假阴性，墓碑和重建过滤器如何保证「正确维护时没有假阴性」。三句都能答上来，并且能指出 `practice108/results.json` 里对应的字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验表，再回到官方文档里关于「任一 loc 为 0 则一定不在」「收益非线性」「full filter 判负可以跳过 index」「partitioned filter 必须同时开 partitioned index」以及「标准 Bloom 不能安全清位删除」的那几句，不要只把「加个 Bloom 就能减少读放大」这句话背下来。

写作笔记时顺便记下三件事：你核对文档的日期和实际打开的页面（包括 `Bloom-Filter` / `Filter-Policy` 当日不存在这件事），这次实验里每个误判率的分母是什么，以及墓碑在删除路径上究竟由谁负责。这三件事能把本课的过滤器边界带进后面的存储引擎课，而不是只留下一个「误判率越低越好」的印象。
