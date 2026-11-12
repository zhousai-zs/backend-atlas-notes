# 第 106/208 课：LSM Tree、SST与压实

## 元信息

| 项 | 内容 |
|---|---|
| 节点 ID | `g-lsm` |
| 英文名 | LSM Trees |
| 领域 / 阶段 | G / 第 4 阶段 |
| 子类 | 存储引擎 |
| 难度 | 深入 |
| 前置节点 | `g-wal`、`a-tree` |
| 标签 | `LSM Trees`、`G`、`Systems` |
| 参考资料 | [LSM Tree、SST与压实：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Compaction)（github.com，文档；2026-11-12 该 URL 返回 301，跳转到 [meta/rocksdb wiki：Compaction](https://github.com/meta/rocksdb/wiki/Compaction) 返回 200。同批核对 Leveled-Compaction、Universal-Compaction、Write-Stalls、MemTable、A-Tutorial-of-RocksDB-SST-formats，均为 301 后 200；Flush-WAL-and-Compaction 301 后落到 wiki 首页，页面不存在，未引用） |
| 摘要 | LSM把更新先写入内存再刷成有序文件，通过压实整理多层数据，并在读写空间放大之间取舍。 |

> **边界说明**：本课中出现的「内容后台」「文章标题」「发布」「删除稿」等场景都是**教学假设**，用来让 memtable、flush、compaction 和 write stall 变得可观察，不代表腾讯/微信或任何企业的内部实现。配套代码是 Python 3 标准库写的教学 `LSMEngine`，**不是 RocksDB，不是 Redis，也不是一个对外提供网络协议的服务**。教学 SST 是**内存中带序号的有序记录集合**，不是 RocksDB 的 block-based 4 KiB 块或 plain table，也**没有** bloom filter、block cache、WAL、MANIFEST、列族、快照、subcompaction、FIFO、trivial move 或动态 level bytes。本课**不实现 WAL**，假设写入已经进入内存表；持久性边界见第 105 课。官方文档已于 **2026-11-12** 核对：节点 URL `https://github.com/facebook/rocksdb/wiki/Compaction` 返回 301，跳转到 `https://github.com/meta/rocksdb/wiki/Compaction` 并返回 200；同期打开的 Leveled-Compaction、Universal-Compaction、Write-Stalls、MemTable、A-Tutorial-of-RocksDB-SST-formats 同样 301 后 200。`https://github.com/facebook/rocksdb/wiki/Flush-WAL-and-Compaction` 301 后落到 wiki 首页，页面不存在，本课不引用。文中的默认值、日志措辞和机制只写到核对过的页面和本次教学实验能够支撑的范围；教学实验里的字节数、耗时、放大倍数都来自 `practice106/results.json`，不是生产引擎的基准数字。

## 导读

### 一个直觉：发布很快，读的时候却要翻几层

内容后台里，编辑发布一篇稿，或者把标题改一版，写入看起来只是往内存里放一条记录，很快就能返回。可数据过一段时间会变成磁盘上的有序文件；同一个键可能在不同层里留下多个版本；删掉一篇稿，也不是立刻从所有文件里消失，而是先留下一个「墓碑」标记。LSM Tree 要解决的就是这件事：把随机更新先攒在内存里，刷成按 key 排好序的文件，再用压实把这些文件一层层合并。节点把它概括为：LSM 把更新先写入内存再刷成有序文件，通过压实整理多层数据，并在读写空间放大之间取舍。

这带来两个必须同时看见的面：写路径因为顺序刷盘而适合写密集负载；读路径却要查内存表、最新的 L0 文件，再逐层往下找，后台压实跟不上时还会触发减速甚至停写。只报 QPS，往往看不出这些代价。

### 接上前置

第 105 课的 WAL 解决的是「已经确认的写入，崩溃后还能不能重放回来」。它让写入先有一份可重放的日志，但没有回答这些键按什么结构组织、历史版本放在哪里、删除怎样盖住旧值。另一门前置 `a-tree` 讨论有序结构上的查找；LSM 把这种有序性从一棵内存树扩展成一组按层组织的有序文件。把两者接起来：WAL 负责耐久性边界，LSM 负责把有序数据刷成 SST，并在后台压实；本课假设写入已经进入内存表，不再实现 WAL。

### 一次写入走进 LSM 之后

一次 `article:011:title = draft-011` 的教学写入，会走这条路径：

1. 分配一个单调递增的序号，把 `put` 记录写进活跃 memtable。这里假设它已经通过 WAL 获得持久性；本课不重复实现第 105 课的日志。
2. memtable 达到 `write_buffer_size` 后被封存为 immutable，新的写入进入新的活跃 memtable。
3. 后台把 immutable 按 key 排序，刷成一个 L0 的 SST，并记录这个文件的最小 key 和最大 key。
4. L0 文件数达到触发阈值后，压实把 L0 合并到 L1；L1 超过目标大小后，再把部分文件与 L2 中范围重叠的文件合并。
5. 读取时先查活跃 memtable，再查还没刷完的 immutable，然后按从新到旧的顺序查 L0，最后逐层向下。序号更大的版本获胜；如果最新版本是墓碑，就返回 `not_found`，而不是继续把旧值翻出来。

### 教学场景（教学假设）

下面用「文章标题的发布、修订和删除」做例子。键形如 `article:001:title`，值是 `draft-001`，修订版标题用 `Weekly digest (revised)`。这是教学假设，用来观察刷盘、压实、墓碑和写放大，不代表任何线上内容系统的真实存储设计。

### 本课三条主线

1. **memtable 和 SST**：写先进入内存表，满了变成 immutable，再刷成有序的 L0 文件；读必须先看更新的内存表，SST 让顺序扫描和范围查找成为可能。
2. **level/universal compaction**：leveled 用多层有序 run 控制空间放大，universal/tiered 用少改写下一层来控制写放大；RocksDB 的 Level 压实本身是 tiered + leveled 的组合。
3. **读写空间放大与墓碑**：压实要付出写放大，读要触碰多个文件，删除留下的墓碑只有到达该 key 的 bottom-most level 才能丢掉。

## 学习目标

- 能解释 memtable 和 SST：写为什么先进入内存表，memtable 满了之后为什么先变成 immutable 再刷盘，读为什么必须先查 memtable，以及 SST 在 RocksDB 里是什么格式、教学 SST 又简化了什么。
- 能解释 level/universal compaction：L0 为什么通常要整层合并进 L1，leveled 的 some-to-some 合并与 universal 的 tiered 合并差别在哪里，write stall 由哪些压力触发。
- 能独立完成：用可视化日志追踪 flush 和 compaction，改变写入速率观察 write stall；能报告写、读、空间放大指标，而不只报 QPS 或 p99。

## 为什么后端要懂

下面用内容后台的发布、改标题、批量导入和运营回填作为**教学假设**：它们都是写密集操作，会不断产生新版本。RocksDB 这类 LSM 存储之所以能扛住写密集负载，一个重要原因是写入先顺序进入 memtable，再顺序刷成 SST，而不是每次都在原地改一棵磁盘 B 树。节点把「为什么要懂」写成一句话：理解 RocksDB 写密集能力与后台抖动来源。后端工程师如果只知道「LSM 写快」，就很容易在发布高峰把后台压实挤到前台，看到 p99 突然变长，却不知道是 flush 还是 compaction 跟不上。

更具体地说，发布接口的 SLO 不只看平均 QPS，还要看停写和尾延迟。后台压实欠下的债，可能先表现为 L0 文件堆积、读放大上升，最后才表现为 `Stalling writes` 或 `Stopping writes`。如果监控只有 QPS，就会把「系统还在接请求」误当成「系统还健康」。本课要求报告放大指标，就是为了把这类债务提前暴露出来。

## 核心知识点

### 1. memtable 和 SST

MemTable 是刷盘前的内存结构。RocksDB 文档写明它默认是 skiplist；写入先插入 memtable，读取必须先查 memtable，因为最新的更新还在内存里。memtable 满了以后不会立刻被销毁，而是变为 immutable，等后台线程把它 flush 成 SST 之后才能释放。RocksDB 的 `write_buffer_size` 默认是 64 MB，`max_write_buffer_number` 默认是 2；前者决定单个 memtable 多大，后者约束内存中最多能同时存在多少个 memtable。flush 还可能因为总内存或 WAL 过大而提前触发，所以一个 SST 完全可能比它对应的 memtable 小，不能拿「memtable 多大，SST 就多大」去做容量估算。

SST 在 RocksDB 文档里写作 **Static Sorted Table**，文献里也常称 **Sorted String Table**。默认的 block-based table 把数据切成大约 4 KiB 的块，可以压缩，读取时经过 block cache。它的关键是「按 key 有序」：文件内部可以二分，文件之间的最小/最大 key 让上层知道该去哪个文件找。本课的教学 SST 只是内存中带序号的有序记录集合：没有块格式、没有 bloom、没有 block cache，也没有真正的磁盘 I/O；它的作用是把「有序 run」和「层级」讲清楚。

用 40 篇文章标题做一次可视化追踪。教学实验把 `write_buffer_size` 设成 200 字节（约 5 条记录刷一次），`level0_file_num_compaction_trigger=4`，并把压实带宽调得足够大，避免写入被 stall 干扰：

| 事件 | 真实日志 / 结果 |
|---|---|
| 第 1 次 flush | `t=0.05`，`file=1`，`keys=5`，`bytes=210`，范围 `article:001:title`..`article:005:title` |
| 第 4 次 flush 后 | 立刻 `COMPACT L0→L1`，`inputs=[1,2,3,4]`，`outputs=[5,6]`，`input_bytes=output_bytes=840` |
| L1 超过目标 800 字节 | 又把 `file 5`（420 字节）压到 L2，`outputs=[7]` |
| 追踪结束 | `flush=8`、`compact=3`、`hits=40`、`L0=0`、`L1=3`（共 1260 字节） |

日志里能看到 `MEMTABLE_SEAL`、`FLUSH`、`COMPACT` 和 `TREE` 四类事件：先是内存表封存，再出现 L0 文件，随后是 L0→L1 和 L1→L2 的压实，最后树形恢复成可读状态。40 次点查全部命中（`hits=40`），说明压实改写之后键值仍然正确；这次追踪的完整指标是 `write_amp=2.25`、`read_amp=2.0`、`space_amp=1.0`，用户写入 1680 字节，flush 1680 字节，压实又写了 2100 字节。这里的「文件」是教学引擎的编号，不代表真的有 4 KiB 块落盘。

读路径也能从这组数字看出来：每次 `get` 至少触碰活跃 memtable，然后按新到旧查 L0；如果 L0 没命中，再进入 L1/L2 的范围查找。教学引擎把一次 `get` 平均触碰的 memtable/SST 数记为 `read_amp`。L0 文件越多、层数越深，读放大通常越大。生产 RocksDB 还会用 bloom filter、block cache 等减少无效读取，本课没有这些结构，所以不要把教学 `read_amp` 直接当成线上读放大。

### 2. level/universal compaction

**先统一术语。** 编号更小的 level 在「上面」，L0 最新，Lmax 最老、在最底。某个 key 的 **bottom-most level**，是「包含该 key、且更低 level 不再包含它」的那一层。理解这句话，后面判断墓碑能不能删、压实有没有把旧版本清干净，都靠它。

压实算法可以按目标粗略分成两类。Classic Leveled 最小化空间放大，代价是读放大和写放大；Tiered 最小化写放大，代价是读放大和空间放大。RocksDB 实现的 Level compaction 其实是 **Tiered + Leveled**：memtable 与 L0 是 tiered 的，flush 只是向 L0 增加一条新的 sorted run，不会改写已有的 L0 文件；L0 文件数由 `level0_file_num_compaction_trigger` 约束，默认是 4。Universal 是 tiered 的另一种实现；此外还有 FIFO，本课不展开。

**Leveled。** L0 是刚从 memtable flush 出来的文件，通常互相重叠，所以达到 trigger 时通常要把 L0 文件一起合并进 L1，才能得到一条按 key 有序、互不重叠的 L1 run。L1 及其以下每一层通常是一条 sorted run，按范围切成多个 SST，查找时可以先二分定位文件。L0→L1 之后，如果 L1 超过目标大小，就挑至少一个 L1 文件，与 L2 中范围重叠的文件合并；这是 some-to-some，不是原始 LSM 论文里那种 all-to-all。目标大小通常由 `max_bytes_for_level_base` 配合 multiplier 指数增长。教学追踪里正是这个顺序：4 个 L0 文件先合并成 L1 的两个输出，L1 总量 840 字节超过教学目标 800 字节后，再把其中一个 420 字节文件压到 L2。

**Universal / tiered。** Universal 合并同一层的多个 sorted run，生成下一层的新 run，不改写下一层已有的 run；单层写放大因此可以接近 1。它的空间放大有一个启发式：除最早那个文件外都算放大；默认 `max_size_amplification_percent=200`，意思是 100 字节用户数据最多大约占 300 字节物理空间；完全压实后空间放大视为 0%。本课引擎报告的 `space_amp` 是 `stored_bytes / live_bytes`，`1.0` 表示没有额外空间，和文档里的百分比启发式不是同一种写法，读的时候不要混在一起。

**write stall：压实跟不上的反馈。** flush/compaction 跟不上时，系统会主动减速，避免空间放大和读放大继续恶化。RocksDB 文档列出三个原因：

1. 等待 flush 的 memtable 过多。达到 `max_write_buffer_number` 时停写；如果该值大于 3 且达到 `max-1`，则减速。
2. L0 文件数达到 `level0_slowdown_writes_trigger` 时减速，达到 `level0_stop_writes_trigger` 时停写，等待 L0→L1 压实。
3. 待压实字节达到 `soft_pending_compaction_bytes` 时减速，达到 `hard_pending_compaction_bytes` 时停写。

减速时写入会被降到 `delayed_write_rate`。触发条件按列族计算，但 stall 作用于整个 DB。日志大意是 `Stalling writes because we have 4 level-0 files` / `Stopping writes because we have 20 level-0 files`。`WriteOptions.no_slowdown=true` 时不阻塞，立刻返回 `Incomplete`。需要特别说明：本课核对的 Write-Stalls 页面没有给出 slowdown/stop 的官方默认数字，所以本课不编造这两个默认值；实验里的 8 和 12 只是教学阈值。

教学实验用同一套阈值（slowdown=8、stop=12、`compaction_bandwidth=800` 字节/秒、`max_write_buffer_number=3`）写 80 文章标题，只改写入间隔：

| 路径 | 写入间隔 | stall | stop | stall 秒 | stop 秒 | 写 p99 | L0 峰值 | write / read / space amp |
|---|---|---|---|---|---|---|---|---|
| 快写 | 0.001 秒（试图 1000 次/秒） | 10 | 10 | 1.05 | 6.0375 | 3.2035 秒 | 12 | 1.5625 / 5.375 / 1.0 |
| 慢写 | 0.15 秒（约 6.6667 次/秒） | 0 | 0 | 0.0 | 0.0 | 0.15 秒 | 4 | 2.75 / 2.0 / 1.0 |

快写日志里同时出现 `stopping writes because we have 3 immutable memtables (waiting for flush), max_write_buffer_number is set to 3` 和 `stopping writes because we have 12 level-0 files`；stall 从 8 个 L0 文件开始，随后出现 9、10、11 个 L0。慢写没有 stall，也没有 stop，p99 就是写入间隔 0.15 秒。快写结束时仍有 3 个不可变内存表和 1 个 L0 文件没清空，所以它的 `write_amp=1.5625` 是按已写 SST 5250 字节 / 用户 3360 字节算出的，并不是一次完成所有 flush 和 compaction 后的最终比率；不要拿它和慢写的 2.75 简单比较，说「快写更省」。慢写完成了 16 次 flush、10 次 compact，最终 L0 为 0。快写真正暴露的问题是：后台带宽跟不上时，前台会付出 6.0375 秒停写和 3.2035 秒 p99 的代价。

### 3. 读写空间放大与墓碑

先明确本课三个指标的定义，避免把不同引擎的口径混在一起：

- `write_amp = sst_bytes_written / user_bytes`：用户每写 1 字节，最终向 SST 写了多少字节（flush 加 compaction）。
- `read_amp = files_touched / get_count`：一次点查平均触碰多少个 memtable/SST；这是教学口径，不等于 RocksDB 线上按字节或 I/O 计的读放大。
- `space_amp = stored_bytes / live_bytes`：物理存了多少，对比当前还活着的数据有多少。

用 80 个顺序新键做 insert-only 实验，settle 后再读：

| 风格 | user_bytes | flush_bytes | compact_bytes | sst_bytes_written | write_amp | read_amp | space_amp | flush / compact |
|---|---|---|---|---|---|---|---|---|
| leveled | 3360 | 3360 | 6300 | 9660 | 2.875 | 2.0 | 1.0 | 16 / 8 |
| universal | 3360 | 3360 | 3360 | 6720 | 2.0 | 2.0 | 1.0 | 16 / 1 |

顺序 flush 看起来「刚好等于用户字节」（`flush_equals_user=True`），但 leveled 还要再写 6300 字节压实，总写放大是 2.875，不是 1。这就是节点写下的误区：**顺序写入优势不代表总写放大低**。RocksDB 文档提到针对按键序插入的优化可以明显降低写放大，但本课引擎没有实现 trivial move，不能把 2.875 说成 RocksDB 的生产数字。这个实验也说明 insert-only 且没有重复版本时，空间放大可以是 1.0；空间放大不是每个 LSM 工作负载都必然大于 1。

墓碑实验把「删除」单独拿出来。标题 `article:001:title = Weekly digest (revised)` 先 flush 到 L0 的 file 1，再被强制压到 L2；此时 L2 持有旧值，读取仍是 `ok`。随后删除这个键，墓碑先在 memtable 里，读取已经是 `not_found`；墓碑刷到 L0 的 file 4，读取仍是 `not_found`。关键在接下来的两次压实：

| 阶段 | 读取 | tombstones_kept | tombstones_dropped | 旧值是否还在 |
|---|---|---|---|---|
| L0→L1，L2 仍有旧值 | `not_found` | 1 | 0 | 是（`l2_still_has_value=True`） |
| L1 与 L2 合并到该 key 的 bottom-most | `not_found` | 0 | 1 | 否，`outputs=[]`、`output_bytes=0` |

第一次 L0→L1 不能丢墓碑，因为 L2 还持有这个 key 的旧值；如果此时把墓碑删掉，旧标题会重新「复活」。第二次把 L1 与重叠的 L2 合并到该 key 的 bottom-most 时，输出层更低的位置已经不再包含这个 key，墓碑才能被丢掉；这次 `versions_dropped=1`，输出为空，树里的墓碑计数清零。结论就是验收标准里的那句话：**只有输出层对该 key 已经是 bottom-most（更低层不再有该 key）才能丢掉墓碑**。本课教学引擎没有快照；生产引擎如果有更早的快照还在读，墓碑可能需要保留更久，不能把这条简化结论直接当成所有场景的规则。

## 常见误区

**把顺序写入当成低总写放大。** 节点专门提醒：顺序写入优势不代表总写放大低。实验里 leveled 的 flush 字节正好等于用户字节 3360，但压实又写了 6300 字节，总写放大是 2.875。只看 flush 那一段，会误以为顺序插入几乎没有放大；把 flush 和 compaction 加起来，才能看到真实成本。

**以为删除会立刻释放空间。** 删除先产生墓碑，墓碑在到达该 key 的 bottom-most level 之前必须保留。实验中 L0→L1 时 `tombstones_kept=1`、`tombstones_dropped=0`，因为 L2 还有旧值；直到 L1 与 L2 合并到最底层，才出现 `tombstones_dropped=1`、`outputs=[]`。如果提前丢墓碑，旧值会复活。

**只看 QPS 或 p99 判断压实策略。** 后面的积极/惰性压实实验里，两边写 p99 都是 0.125 秒，但积极一侧 `read_amp=2.0`、`L0=0`，惰性一侧 `read_amp=5.9286`、`L0=5`，压实字节分别是 5460 和 3150。前台数字一样，不代表后台债务一样；放大指标和残留 L0 才能把债暴露出来。

**把快写的较低写放大当成优势。** 快写实验结束时还有 3 个不可变内存表和 1 个 L0 文件没清空，`write_amp=1.5625` 只是已完成 SST 的比率；慢写完成了全部 flush 和压实，`write_amp=2.75`。两者完成度不同，不能直接比较。

**把教学数字当成 RocksDB 生产数字。** 本课引擎没有 block cache、bloom、trivial move、subcompaction 等结构，2.875、1.5625 这类数字只描述这次教学实验。RocksDB 在按键序插入等场景有额外优化，真实生产数字要用生产配置和真实负载测。

## 设计权衡

节点给出的权衡是：更积极压实改善读取，却消耗 IO 并影响前台尾延迟。机制很直接：L0 文件越早合并，读时要检查的文件越少，读放大下降；但压实本身要读写 SST，占用磁盘带宽和 CPU，和前台写入争资源。用 70 个顺序键、写入间隔 0.02 秒、压实带宽 2000 字节/秒，对比 `level0_file_num_compaction_trigger=2` 与 `8`：

| 策略 | trigger | L0 残留 | read_amp | compact_bytes | write_amp | compact 次数 | 写 p99 | stall / 停写 |
|---|---|---|---|---|---|---|---|---|
| 积极 | 2 | 0 | 2.0 | 5460 | 2.8571 | 8 | 0.125 秒 | 9 次 / 0 秒 |
| 惰性 | 8 | 5 | 5.9286 | 3150 | 2.0714 | 4 | 0.125 秒 | 9 次 / 0 秒 |

积极一侧把 L0 清空，读放大从 5.9286 降到 2.0，代价是多写了 2310 字节压实（5460 对 3150）并多做 4 次压实。惰性一侧少写压实，但留下 5 个 L0 文件，读放大升到 5.9286，债没有消失，只是留到后面。需要如实写出这次实验的观察：两边写 p99 都是 0.125 秒，stall 都是 9 次、累计 0.945 秒，停写都是 0；**这次实验并没有观察到积极一侧 p99 更高**。如果只看前台 p99 或 QPS，会以为两者一样；放大指标和残留 L0 才把差异暴露出来。不要把「更积极压实一定让 p99 变差」硬写成这次实验的结论，也不要把「p99 一样」误写成「两种策略等价」。

leveled 与 universal 也是同一类取舍。Leveled 用多层有序 run 压低空间放大，但每往下一层都可能重写数据，写放大更高；Universal 尽量不改写下一层，写放大低，但读放大和空间放大的风险更高。本次 insert-only 实验里 universal 的 `write_amp=2.0`、leveled 的 `write_amp=2.875`，读放大和空间放大都是 2.0 和 1.0。这不是因为两边都被压成了同一条 run：universal 最终确实是 1 条 run（3360 字节），leveled 则是 L1 的 420 字节加上 L2 的 2940 字节。空间放大等于 1.0，是因为这次只有新键、没有重叠版本；文档把 tiered 的代价写在读放大和空间放大上，要换更新密集或尚未充分压实的负载才能看见，不能据此说 universal 在所有场景都全面更好。

write stall 本身也是权衡：它是保护机制，用减速或停写换取时间，让 flush/compaction 把 L0 和待压实字节降下来，避免读放大和空间放大继续恶化；代价是前台请求被阻塞，尾延迟变长。快写实验的 6.0375 秒停写就是这笔代价。发布路径若不能接受这种停顿，就要么降低写入突发、要么提高压实带宽、要么在容量规划上预留更多后台余量，而不是把 stall 日志当成无关警告。

## 动手实践：用可视化日志追踪 flush 和 compaction，改变写入速率观察 write stall

实践目录是 `practice106/`，只依赖 Python 3 标准库。核心类型是 `LSMEngine`（memtable、immutable、L0/L1/L2 层、leveled 与简化 universal 两种风格、可视化日志、write stall 模拟、墓碑合并）。它不是 RocksDB，也不是网络服务。

### 0. 文件结构

```
practice106/
├── engine.py       # LSMEngine、MemTable、SST、Options、VisualLog
├── app.py          # 全部对照实验
├── test_lsm.py     # 验收断言，并把结果写入 results.json
├── test.sh         # python3 test_lsm.py
└── results.json    # 本次 ALL_CHECKS_PASSED 的完整输出
```

在该目录执行：

```bash
bash test.sh
```

成功时第一行是 `ALL_CHECKS_PASSED`，第二行是本次摘要。下面的数字都来自这次运行生成的 `results.json`（Python 3.13.5，文档核对日期 2026-11-12）。换一台机器或再跑一次，耗时和 p99 可能有波动，语义断言不应翻盘。

### 1. 准备数据与目标操作

实验统一使用 `article:001:title` 这类教学键。第一组写 40 篇标题，观察 memtable 封存、flush、L0→L1、L1→L2；第二组用 80 次写入和两种速率观察 write stall；第三组对 `article:001:title` 先写 `Weekly digest (revised)`，再删除，观察墓碑何时能丢；第四组用 80 个顺序新键比较 leveled 与 universal 的放大；第五组用 70 个顺序键比较积极与惰性压实。目标操作就是节点要求的那件事：用可视化日志追踪 flush 和 compaction，改变写入速率观察 write stall。

### 2. 要比较的几条路径

1. 40 篇标题：`write_buffer_size=200`、`level0_file_num_compaction_trigger=4`、带宽充足，观察第一次 flush 和第一次 L0→L1，以及 L1 超目标后的 L1→L2。
2. 80 次写入：快写间隔 0.001 秒与慢写间隔 0.15 秒，使用同一套 slowdown=8、stop=12、`compaction_bandwidth=800` 字节/秒，比较 stall、stop、停写时长、p99 和 L0 峰值。
3. 墓碑：先把旧值压到 L2，再删除；分别观察 memtable、L0、L0→L1、L1+L2 合并到最底层时墓碑的 kept/dropped。
4. 放大：80 个顺序新键，leveled 与 universal 都 settle 后再读，比较 `flush_bytes`、`compact_bytes`、`sst_bytes_written` 和三个放大指标。
5. 权衡：70 个顺序键，`level0_file_num_compaction_trigger=2` 对 8，比较 read_amp、L0 残留、compact_bytes、write_amp、p99 和 stall。

### 3. 运行并解读（真实输出）

本次摘要如下（完整字段见 `results.json`）：

```
ALL_CHECKS_PASSED
SUMMARY flush=8 compact=3 hits=40 l0_after=0 l1_after=3 fast_stall=10 fast_stop=10 fast_stop_s=6.0375 fast_p99=3.2035 fast_l0_peak=12 slow_stall=0 slow_stop=0 slow_stop_s=0.0 slow_p99=0.15 tomb_mid_kept=1 tomb_mid_dropped=0 l2_still_has_value=True tomb_bot_kept=0 tomb_bot_dropped=1 tomb_left=0 lv_wamp=2.875 lv_ramp=2.0 lv_samp=1.0 un_wamp=2.0 un_ramp=2.0 un_samp=1.0 flush_eq_user=True lv_flush=3360 lv_compact=6300 agg_ramp=2.0 lazy_ramp=5.9286 agg_l0=0 lazy_l0=5 agg_p99=0.125 lazy_p99=0.125 agg_compact_bytes=5460 lazy_compact_bytes=3150
```

读这行摘要可以按三条主线拆开。memtable/SST：40 篇标题产生 8 次 flush、3 次 compact，结束时 L0 为 0、L1 为 3，40 次点查全部命中。压实：第一次 L0→L1 是 `inputs=[1,2,3,4] outputs=[5,6]`、840 到 840 字节，随后 L1 超过 800 字节目标，把 file 5 的 420 字节压到 L2。stall：快写 10 次 stall、10 次 stop、停写 6.0375 秒、p99 3.2035 秒、L0 峰值 12；慢写全部为 0，p99 就是 0.15 秒的写入间隔。墓碑：中途 `kept=1 dropped=0` 且 L2 仍有旧值，到最底层 `kept=0 dropped=1`，树里墓碑清零。放大：leveled 是 2.875 / 2.0 / 1.0，universal 是 2.0 / 2.0 / 1.0；积极压实 read_amp 2.0、L0 0，惰性压实 read_amp 5.9286、L0 5，但两者 p99 都是 0.125 秒。

### 4. 逐组对照真实数字

**可视化追踪。** 第 1 次 flush 在 `t=0.05`，file=1，keys=5，bytes=210，范围 `article:001:title` 到 `article:005:title`。第 4 次 flush 后立刻 L0→L1，输入 1–4，输出 5–6，输入输出都是 840 字节；随后 L1 超过目标 800，把 file 5 的 420 字节压到 L2。结束时 flush=8、compact=3、hits=40、L0=0、L1=3（1260 字节）。日志里的 `MEMTABLE_SEAL`、`FLUSH`、`COMPACT`、`TREE` 可以把「内存表封存 → 刷 L0 → 合并到 L1 → 推一个文件到 L2」完整串起来。

**write stall。** 快写间隔 0.001 秒，试图 1000 次/秒，结果 stall 10 次、stop 10 次、停写 6.0375 秒、写 p99 3.2035 秒、L0 峰值 12；日志原因既有 3 个 immutable memtables 等待 flush，也有 12 个 level-0 files 触发停写，stall 从 8 个 L0 文件开始。慢写间隔 0.15 秒，约 6.6667 次/秒，stall 0 次、stop 0 次、停写 0 秒、p99 0.15 秒、L0 峰值 4。两边都报告放大：快写 1.5625 / 5.375 / 1.0，慢写 2.75 / 2.0 / 1.0；快写结束时还有 3 个不可变内存表未刷，写放大按已写 SST 计，不要直接和慢写比谁更省。

**墓碑。** 旧值 `Weekly digest (revised)` 先到 L2；删除后 memtable、L0 都是 `not_found`；L0→L1 时 `tombstones_kept=1`、`dropped=0`，L2 仍有旧值；L1 与 L2 合并到该 key 的 bottom-most 时 `kept=0`、`dropped=1`，`outputs=[]`、`output_bytes=0`、`versions_dropped=1`，树里墓碑清零。只有更低层不再包含这个 key，墓碑才能删。

**放大。** 80 个顺序新键，leveled：user_bytes=flush_bytes=3360（`flush_equals_user=True`），compact_bytes=6300，SST 共写 9660，write_amp=2.875，read_amp=2.0，space_amp=1.0，flush 16 次，compact 8 次。universal：compact_bytes=3360，SST 共写 6720，write_amp=2.0，read_amp=2.0，space_amp=1.0，compact 1 次。顺序 flush 等于用户字节，但 leveled 还要再写 6300 字节压实，总写放大是 2.875 不是 1。

**积极对惰性。** 70 个顺序键，trigger=2 对 8。积极：L0=0，read_amp=2.0，compact_bytes=5460，write_amp=2.8571，compact 8 次。惰性：L0=5，read_amp=5.9286，compact_bytes=3150，write_amp=2.0714，compact 4 次。两边 write_p99 都是 0.125 秒，stall 都是 9 次、累计 0.945 秒，停写都是 0。只看 p99/QPS 会以为两者一样；放大指标和残留 L0 才把惰性一侧的债显示出来。这次实验没有观察到积极一侧 p99 更高，不要把机制上的权衡写成这次数据没有出现的结论。

### 5. 对照验收标准

```
# 验收 1：能解释墓碑何时可删除
# L0->L1 时 L2 仍有旧值：tombstones_kept=1, tombstones_dropped=0；
# L1 与 L2 合并到该 key 的 bottom-most：tombstones_kept=0, tombstones_dropped=1,
# outputs=[], output_bytes=0, versions_dropped=1，树里墓碑清零。
# 所以只有更低层不再包含该 key 时才能丢墓碑；本课没有快照。

# 验收 2：报告放大指标而不只 QPS
# leveled: write_amp=2.875, read_amp=2.0, space_amp=1.0；
# universal: write_amp=2.0, read_amp=2.0, space_amp=1.0。
# 快写: 1.5625 / 5.375 / 1.0；慢写: 2.75 / 2.0 / 1.0。
# 积极: read_amp=2.0, L0=0, compact_bytes=5460, write_amp=2.8571；
# 惰性: read_amp=5.9286, L0=5, compact_bytes=3150, write_amp=2.0714；
# 两者 p99 都是 0.125 秒，不能只拿 p99 判断策略优劣。
```

### 6. 自己动手的扩展练习

把快写实验的 `compaction_bandwidth` 从 800 字节/秒逐步调高，观察 stall 次数、停写秒数和 L0 峰值怎样变化；再把慢写的间隔从 0.15 秒逐步缩短，找到「开始出现 stall」的临界速率。记录每次的 write/read/space amp，而不只记录 QPS。然后给教学引擎加一个简单的「读快照」概念，思考：如果还有一个旧快照要读 `article:001:title`，L1 与 L2 合并时还能不能直接丢掉墓碑？这个扩展不需要实现完整快照，只要能说清楚「为什么墓碑可能要多留一段时间」即可。

### 7. 和生产引擎的对应关系（不要记错边界）

- RocksDB 的 memtable 默认是 skiplist，`write_buffer_size` 默认 64 MB，`max_write_buffer_number` 默认 2；L0 文件数由 `level0_file_num_compaction_trigger` 约束，默认 4。本课实验为了可观察，把 `write_buffer_size` 设成 200 字节，约 5 条刷一次，这不是生产默认值。
- RocksDB 默认 SST 是 block-based table（约 4 KiB 块，可压缩，经 block cache 读取）；本课教学 SST 是内存中的有序记录集合，没有块、bloom、block cache、MANIFEST 或真正的文件 I/O。
- RocksDB 的 Level compaction 是 tiered + leveled：memtable 与 L0 是 tiered，L0→L1 通常带上全部 L0；更低层是 leveled，使用 some-to-some 的文件选择。Universal 是 tiered，另有 FIFO。本课只实现简化版 leveled 和简化版 universal。
- write stall 的三个方向是：等待 flush 的 memtable 过多、L0 文件数触发 slowdown/stop、待压实字节触发 soft/hard 限制。官方页面没有给出 slowdown/stop 的默认数字，本课的 8/12 是教学阈值，不要写成官方默认。
- 本课没有 WAL、快照、subcompaction、trivial move、动态 level bytes、FIFO、列族和 compaction filter；这些边界不能靠教学引擎的行为去推断。

## 源码与文档阅读

按节点的 `sourceStudy`：从 RocksDB 文档到 DBImpl 的 write、flush 和 compaction 路径，标注后台任务与前台写入的交点。2026-11-12 实际打开的页面如下。

- [Compaction](https://github.com/facebook/rocksdb/wiki/Compaction)（节点给出的入口；当日 301 到 `meta/rocksdb` 后 200）：压实算法的分类、leveled 与 universal 的目标，以及写放大、读放大、空间放大的基本取舍。用它核对本课的术语和「L0 是 tiered、L1 以下是 leveled」这条边界。
- [Leveled-Compaction](https://github.com/facebook/rocksdb/wiki/Leveled-Compaction)：L0→L1、L1 超过目标后与 L2 的 some-to-some 合并，目标大小随层数增长。对照本课 `inputs=[1,2,3,4] outputs=[5,6]` 和随后 `file 5 → L2` 的追踪。
- [Universal-Compaction](https://github.com/facebook/rocksdb/wiki/Universal-Compaction)：tiered 合并、单层写放大接近 1、空间放大的百分比启发式，以及 `max_size_amplification_percent=200` 的含义。对照本课 universal 的 `compact_bytes=3360`、`write_amp=2.0`。
- [Write-Stalls](https://github.com/facebook/rocksdb/wiki/Write-Stalls)：三个 stall/stop 原因、`delayed_write_rate`、按列族触发但作用于整个 DB、`no_slowdown` 返回 `Incomplete`。注意该页面在本课核对时没有给出 slowdown/stop 的默认数字。
- [MemTable](https://github.com/facebook/rocksdb/wiki/MemTable)：memtable 默认 skiplist，写先进入 memtable，读先查 memtable，满了变 immutable，flush 成 SST 后才能销毁；`write_buffer_size` 默认 64 MB，`max_write_buffer_number` 默认 2。
- [A-Tutorial-of-RocksDB-SST-formats](https://github.com/facebook/rocksdb/wiki/A-Tutorial-of-RocksDB-SST-formats)：SST 的 block-based 格式、块大小和读取路径。用它来对照「教学 SST 只是内存中的有序记录集合」，不要把两者混为一谈。
- `Flush-WAL-and-Compaction` 在 2026-11-12 从 facebook 入口 301 后落到 wiki 首页，页面不存在，本课没有引用它，也不拿它作为任何默认值或行为的依据。

以下源码链接在 2026-11-12 从 facebook 入口打开时同样返回 301，跳转到 `meta/rocksdb` 后返回 200；这里保留入口给出的 facebook URL。源码阅读按前台写入与后台任务两条线走：

- [db/db_impl/db_impl_write.cc](https://github.com/facebook/rocksdb/blob/main/db/db_impl/db_impl_write.cc)：`DBImpl::Write` / `WriteImpl` 在 `PreprocessWrite` 里检查压力，碰到 stall 时走 `DelayWrite`，这是前台写入与后台压实的交点；`WriteOptions.no_slowdown` 为真时返回 `Incomplete`。
- [db/db_impl/db_impl_compaction_flush.cc](https://github.com/facebook/rocksdb/blob/main/db/db_impl/db_impl_compaction_flush.cc)：`FlushMemTable` / `FlushMemTableToOutputFile`、`WaitUntilFlushWouldNotStallWrites`、`Level0StopWriteTrigger`、`CompactRange`。这里能看到 flush 与 L0 stop 条件怎样互相牵制。
- [db/flush_job.cc](https://github.com/facebook/rocksdb/blob/main/db/flush_job.cc)：`FlushJob::Run` 和 `WriteLevel0Table`，对应本课从 immutable memtable 生成 L0 SST 的那一步。
- [db/compaction/compaction_job.cc](https://github.com/facebook/rocksdb/blob/main/db/compaction/compaction_job.cc)：`CompactionJob::Run`、`ProcessKeyValue`、`RecordDroppedKeys`。阅读时重点找「旧版本和墓碑在什么条件下被丢掉」，再回到本课 `tombstones_kept` / `tombstones_dropped` 的两次对照。

阅读时记下三件事：你打开页面的日期和最终 URL；RocksDB 的 L0 是 tiered、L1 以下是 leveled，不要把整棵树都说成同一种算法；墓碑的删除条件是否和「bottom-most」一致。不要把本课教学引擎的函数名、阈值或数字当成 RocksDB 的实现细节。

## 自测题

**1.** memtable 满了之后为什么不能立刻销毁？读为什么必须先查 memtable？RocksDB 的 `write_buffer_size` 和 `max_write_buffer_number` 默认是多少？为什么 SST 可能比对应的 memtable 小？

**答案：** memtable 满了会先变成 immutable，后台线程把它 flush 成 SST 之后才能释放；如果立刻销毁，内存里还没落成 SST 的更新就丢了。读必须先查 memtable，因为最新版本还在内存里；跳过它就会读到旧 SST 的值。RocksDB 的 `write_buffer_size` 默认 64 MB，`max_write_buffer_number` 默认 2。flush 可能因为总内存或 WAL 过大提前触发，所以 SST 完全可能比 memtable 小。教学实验把 `write_buffer_size` 设成 200 字节，约 5 条刷一次，这不是生产默认值。

**2.** 墓碑在 L0→L1 时 `tombstones_kept=1`、`tombstones_dropped=0`，到 L1 与 L2 合并时 `tombstones_kept=0`、`tombstones_dropped=1`。为什么第一次不能丢？「bottom-most」在这里指什么？如果还有旧快照要读这个 key，结论会怎样变化？

**答案：** 第一次 L0→L1 时，L2 还持有 `article:001:title` 的旧值，墓碑必须留下盖住它；如果丢掉，旧标题会重新出现。第二次 L1 与 L2 合并到该 key 的 bottom-most，更低层已经不再包含这个 key，墓碑才可以删，于是 `outputs=[]`、`output_bytes=0`、`versions_dropped=1`，树里墓碑清零。bottom-most 指「包含该 key、且更低 level 不再包含它」的那一层。本课引擎没有快照；生产引擎如果还有旧快照要读，墓碑可能需要保留更久，不能直接套用这条简化结论。

**3.** 快写 1000 次/秒为什么会出现 stall/stop，而慢写约 6.6667 次/秒没有？如果只看 p99 或 QPS，会漏掉什么？请用本课的放大数字说明。

**答案：** 快写的写入速率超过了教学压实带宽 800 字节/秒，immutable memtable 堆积到 3 个就停写，L0 文件达到 8 个开始 stall、达到 12 个停写；结果是 stall 10 次、stop 10 次、停写 6.0375 秒、p99 3.2035 秒、L0 峰值 12。慢写让后台跟得上，stall 和 stop 都是 0，p99 就是 0.15 秒的写入间隔，L0 峰值 4。只看 p99/QPS 会漏掉放大和残留债务：快写 1.5625 / 5.375 / 1.0，慢写 2.75 / 2.0 / 1.0（快写还有 3 个 immutable 未刷，写放大不是完成态）；积极压实与惰性压实的 p99 都是 0.125 秒，但 read_amp 是 2.0 对 5.9286、L0 是 0 对 5、compact_bytes 是 5460 对 3150。只看前台数字会以为两种策略一样，放大指标和残留 L0 才能说明谁把债留到了后面。

## 推荐资源

- [LSM Tree、SST与压实：参考文档 / 原始资料](https://github.com/facebook/rocksdb/wiki/Compaction)（github.com，英文文档，深入，免费）。重点阅读 memtable 和 SST 与 level/universal compaction，用于核对本节点实验的语义和边界。2026-11-12 该入口 301 到 [meta/rocksdb 的同一页](https://github.com/meta/rocksdb/wiki/Compaction) 后返回 200；同一次核对建议打开 Leveled-Compaction、Universal-Compaction、Write-Stalls、MemTable 和 A-Tutorial-of-RocksDB-SST-formats。

## 本课小结

- MemTable 是刷盘前的内存结构，写入先进入 memtable，满了变成 immutable，后台 flush 成 L0 的 SST 后才能释放。RocksDB 默认 `write_buffer_size=64MB`、`max_write_buffer_number=2`，flush 可能提前触发，所以 SST 不一定和 memtable 一样大。教学 SST 只是内存中的有序记录集合，不是 block-based 4 KiB 块，也没有 bloom、block cache、WAL、MANIFEST、列族、快照、subcompaction、FIFO、trivial move 或动态 level bytes。
- 40 篇标题的追踪里，第 1 次 flush 在 t=0.05、file=1、5 条 210 字节；第 4 次 flush 后 L0→L1 是 `inputs=[1,2,3,4] outputs=[5,6]`、840 到 840 字节；L1 超过 800 后把 file 5 的 420 字节压到 L2。结束时 flush=8、compact=3、hits=40、L0=0、L1=3。
- RocksDB 的 Level compaction 是 tiered + leveled：L0 是 tiered，L1 以下是 leveled；Universal 是 tiered，另有 FIFO。write stall 有三个方向：immutable memtable 过多、L0 文件数触发 slowdown/stop、待压实字节触发 soft/hard 限制；减速降到 `delayed_write_rate`，`no_slowdown` 返回 `Incomplete`。本课核对的页面没有给出 slowdown/stop 的官方默认数字，实验里的 8/12 只是教学阈值。
- 快写 1000 次/秒导致 stall 10 次、stop 10 次、停写 6.0375 秒、p99 3.2035 秒、L0 峰值 12；慢写约 6.6667 次/秒没有 stall/stop，p99 是 0.15 秒，L0 峰值 4。两边都要报告放大：快写 1.5625 / 5.375 / 1.0，慢写 2.75 / 2.0 / 1.0。
- 墓碑只有到达该 key 的 bottom-most level 才能丢：L0→L1 时 L2 还有旧值，`kept=1 dropped=0`；L1 与 L2 合并到最底层才 `kept=0 dropped=1`，输出为空，树里墓碑清零。本课没有快照。
- 顺序 flush 不等于低总写放大：leveled 的 flush 正好 3360 字节，但 compact 又写 6300 字节，总写放大 2.875；universal 是 2.0。RocksDB 有按键序插入的优化，但本课没有 trivial move，2.875 不是生产数字。积极压实把 read_amp 从 5.9286 降到 2.0、L0 从 5 清到 0，代价是多写压实；这次实验两边 p99 都是 0.125 秒，不能只凭 p99 判断策略优劣。

学完后可以用三句话自检：memtable 满了为什么先变 immutable 再刷 SST，读为什么必须先查它；L0→L1 为什么通常要带上全部 L0，墓碑在什么条件下才能删；快写和慢写的 stall/stop 数字差在哪里，为什么报告放大指标比只报 QPS 更能说明后台债务。三句都能答上来，并且能指出 `practice106/results.json` 里对应的那组字段，这一课就算过关。

如果有一句答不上来，先回到对应的实验表，再回到官方文档里关于 memtable 默认 skiplist、L0 是 tiered、Universal 是 tiered、write stall 三个原因以及墓碑 bottom-most 的那几句，不要只把「LSM 写快」这句话背下来。

写作笔记时顺便记下三件事：你核对文档的日期和实际打开的页面，这次实验的写放大是否把 flush 和 compaction 都算进去了，以及删除的墓碑是在哪一层被丢掉的。这三件事能把本课的边界说明带进后面的存储引擎课，而不是只留下一句「LSM 适合写多读少」。
