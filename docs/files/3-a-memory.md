# 第 3/208 课：内存层次与局部性

> 节点：`a-memory`（Memory Hierarchy）  
> 领域：A / 硬件与操作系统  
> 阶段：1  
> 前置节点：`a-machine`  
> 难度：基础  
> 关键词：内存层次、时间局部性、空间局部性、缓存行、组相联、工作集、访问步长

## 1. 学习目标与为什么后端要懂

### 1.1 这个节点在说什么

节点的核心结论是：

> 寄存器、CPU 缓存、DRAM 和持久存储的延迟与容量差异很大，局部性决定访问效率。

节点给出的学习理由是：

> 理解同样复杂度的算法为何表现不同。

这句话对后端工程师尤其重要。两个算法都写成 `O(n)`，一个顺序扫数组，一个沿链表随机跳；复杂度分析看起来一样，真实耗时可能差几倍到几十倍。差别不一定来自“算法错了”，而可能来自**数据放在哪里、按什么顺序访问、每次搬多少字节**。

### 1.2 学完本课后你应该能够

1. 解释**时间局部性**与**空间局部性**，并能举出后端代码中的例子。
2. 解释**缓存行**与**组相联**的基本原理，知道为什么“访问一个字节”可能实际搬来一整行。
3. 解释**工作集、缓存容量、访问步长**之间的关系，能说明工作集超过缓存后为什么会变慢。
4. 独立完成实践：比较连续数组、链表、不同步长遍历的每元素耗时。
5. 避免把 **page cache（页缓存）** 当成 **CPU cache（CPU 缓存）**。

### 1.3 为什么后端要懂内存层次

以典型的微信公众号后台请求为例：

```text
接入 -> 鉴权 -> 限流 -> 消息解析 -> 业务路由 -> 存储/缓存 -> 组装响应
```

这条链路里到处是内存访问：

- **鉴权和限流**：用户状态、配额、计数器是热点数据，可能被反复读取和更新。
- **消息解析**：JSON/Protobuf 字段通常在一个连续缓冲区里，顺序解析比到处跳指针更友好。
- **业务路由**：哈希表、树、链表节点可能形成指针追逐，每次解引用都可能是一次缓存未命中。
- **批量任务**：数组、切片、批处理缓冲区如果布局紧凑，CPU 能预取后续数据；大量小对象则会增加随机访问。
- **并发写入**：两个线程更新同一缓存行里的不同计数器，可能产生伪共享，让性能远低于预期。

理解内存层次，能让你在以下问题上有更可靠的判断：

- 为什么同样复杂度的代码，换个数据结构就慢了？
- 为什么“缓存命中率很高”仍然可能被带宽卡住？
- 为什么序列化缓冲区、对象字段顺序、哈希桶布局会影响 p99？
- 为什么 `free -h` 里的 `buff/cache` 很大，不能直接说明 CPU 缓存很大？

本课不要求你背某款 CPU 的精确参数。硬件参数会变，**用命令看当前机器、用实验验证假设**才是稳定能力。

---

## 2. 从直觉到原理

### 2.1 先建立直觉：书桌、书架、仓库

把 CPU 取数据想成找工具：

- **寄存器**：手里正在用的工具，数量最少，拿起来最快。
- **L1/L2 缓存**：桌面和抽屉，放最近常用的东西。
- **L3 缓存**：房间里的书架，容量更大，但走过去要时间。
- **DRAM**：楼下仓库，容量很大，取一次明显更慢。
- **SSD/HDD**：更远的仓库，容量最大，但延迟高几个数量级。

越靠近 CPU，容量越小、速度越快、成本越高。常见数量级如下，**具体数值必须用当前机器实测**：

| 层次 | 典型容量 | 典型访问延迟 | 谁管理 |
|---|---:|---:|---|
| 寄存器 | 几十到几百个，每个 4/8 字节 | 小于 1 ns | 编译器/CPU |
| L1 缓存 | 每核几十 KiB | 约 1 ns | CPU 硬件 |
| L2 缓存 | 每核几百 KiB 到几 MiB | 几 ns 到十几 ns | CPU 硬件 |
| L3 缓存 | 几 MiB 到数百 MiB | 十几 ns 到几十 ns | CPU 硬件 |
| DRAM | 几 GiB 到 TiB | 几十 ns 到一百多 ns | 操作系统/硬件 |
| SSD | 几百 GiB 到 TiB | 几十 µs 到几百 µs | 操作系统/设备 |
| HDD | 几百 GiB 到 TiB | 几 ms 到十几 ms | 操作系统/设备 |

这张表最重要的不是记住数字，而是记住**数量级差距**。如果 CPU 访问一次 DRAM 要几十到上百纳秒，而一条简单指令不到一纳秒，那么“多一次内存未命中”就可能相当于浪费很多条指令的时间。

在 Linux 上先看自己的机器：

```bash
lscpu
getconf -a | grep -i cache
getconf LEVEL1_DCACHE_LINESIZE
getconf LEVEL1_DCACHE_SIZE
getconf LEVEL2_CACHE_SIZE
getconf LEVEL3_CACHE_SIZE
```

注意：`lscpu` 里的缓存可能是**所有实例的汇总**，例如显示 `L1d cache: 384 KiB (8 instances)`，并不等于每个核都有 384 KiB；`getconf` 通常给出单个 CPU 的缓存参数。不同发行版、不同架构暴露的字段也可能不同，看到空值并不奇怪。

### 2.2 时间局部性：刚用过的东西，马上还会用

**时间局部性**（temporal locality）是指：如果一个地址刚被访问过，那么它在不久后很可能再次被访问。

最直观的例子是循环里的累加器：

```c
uint64_t total = 0;
for (int i = 0; i < 1000000; i++) {
    total += hot_config->rate_limit;
}
```

`total` 和 `hot_config->rate_limit` 会被反复使用。编译器很可能把 `hot_config->rate_limit` 放进寄存器，之后循环里不再访问内存——这其实就是对时间局部性的极致利用。

后端场景里，时间局部性对应：

- 同一个热点用户的鉴权结果、限流配额；
- 同一个配置对象被大量请求读取；
- 同一个连接或会话的元数据在短时间内反复使用；
- 同一个哈希表桶被连续查找。

**设计启示**：如果一个数据在时间窗口内会被反复访问，就应尽量让它留在离 CPU 近的地方，或者减少重复的随机查找。应用层缓存、连接级缓存、请求级缓存都在利用时间局部性，但它们和 CPU 缓存不是同一层东西，后面会区分。

### 2.3 空间局部性：用了这个，附近的东西也会被用

**空间局部性**（spatial locality）是指：如果一个地址被访问，那么它附近的地址很可能很快也被访问。

连续数组是空间局部性的典型：

```c
uint64_t sum_array(const uint64_t *a, size_t n) {
    uint64_t sum = 0;
    for (size_t i = 0; i < n; i++) {
        sum += a[i];
    }
    return sum;
}
```

访问 `a[0]` 时，硬件不是只搬 8 字节，而是把包含 `a[0]` 的一整条缓存行搬进来。之后访问 `a[1]`、`a[2]`……很可能直接命中，不需要再去 DRAM。

反过来，二维数组的遍历顺序会明显影响空间局部性。C 的二维数组按行优先存储，下面第一段通常比第二段更友好：

```c
for (size_t i = 0; i < N; i++) {
    for (size_t j = 0; j < N; j++) {
        sum += m[i * N + j];
    }
}
```

```c
for (size_t j = 0; j < N; j++) {
    for (size_t i = 0; i < N; i++) {
        sum += m[i * N + j];
    }
}
```

第一段沿着内存连续走；第二段每次跨过一整行，访问跨度大，缓存行利用率低。矩阵很大时，差异会非常明显。

后端场景里，空间局部性对应：

- **序列化缓冲区**：Protobuf/JSON 的字段在连续 buffer 中，顺序解析比“每个字段一个小对象、再到处跳指针”更友好。
- **数组/切片**：批量用户 ID、消息 ID 放在连续内存里，扫描和批处理更快。
- **结构体布局**：经常一起访问的字段放在一起，能减少缓存行数量。
- **哈希表桶**：桶内元素紧凑，查找时一次搬入更多候选；节点分散则容易指针追逐。

### 2.4 缓存行：搬运的最小单位不是字节

CPU 缓存和内存之间通常以**缓存行**（cache line，也叫 cache block）为单位搬运。常见 x86-64 和 ARM64 机器上是 **64 字节**，但也有其他大小，必须用命令确认：

```bash
getconf LEVEL1_DCACHE_LINESIZE
```

假设缓存行是 64 字节，`uint64_t` 是 8 字节，那么一条缓存行可以放 8 个元素：

```text
地址:  0    8    16   24   32   40   48   56   64
      +----+----+----+----+----+----+----+----+
      | a0 | a1 | a2 | a3 | a4 | a5 | a6 | a7 | ...
      +----+----+----+----+----+----+----+----+
      <------------ 一条 64B 缓存行 ------------>
```

这意味着：

- 顺序访问 `a[0]` 到 `a[7]`，可能只触发一次内存搬运，后面 7 次都命中。
- 如果每次只访问一个元素，而且步长很大，那么每次都可能搬入一整行，却只用到其中 8 字节。
- 一个包含很多冷字段的大结构体，即使只读一个小字段，也可能把大量无关字节带进缓存。

一个后端常见的布局问题是“热字段和冷字段混在一起”：

```c
struct Session {
    uint64_t user_id;
    uint64_t expire_at;
    char nickname[64];
    char signature[256];
};
```

如果后台只需要扫描 `expire_at` 做过期检查，却要遍历大量 `Session`，那么 `nickname` 和 `signature` 会把缓存行占满，真正有用的字段很少。把热字段拆出来，或者采用结构体数组/数组结构体（AoS/SoA）的混合布局，通常能减少无效搬运。但这不是无条件的，后面会讲更新成本和并发冲突的权衡。

### 2.5 组相联：缓存行放在哪个“格子”里

缓存不是一整块随便放的大仓库，而是按**组（set）**和**路（way）**组织。一个 N 路组相联缓存可以简化成：

```text
一个地址拆成：| tag | set index | block offset |
```

- **block offset**：定位缓存行内的字节；
- **set index**：选择缓存中的哪一个组；
- **tag**：确认这个组里的哪一路才是目标行。

如果缓存容量是 `C`，缓存行大小是 `L`，相联度是 `W`（每组 W 路），那么：

```text
组数 S = C / (L × W)
```

例如，假设某级 L1 数据缓存是 32 KiB、缓存行 64 字节、8 路组相联：

```text
S = 32 KiB / (64 B × 8) = 64 组
```

地址的低 6 位是行内偏移（64 = 2^6），接下来 6 位是组索引（64 组 = 2^6），剩余高位是 tag。

**直接映射**可以看成 1 路组相联：每个地址只能去一个固定位置，冲突时只能互相驱逐。**全相联**则是只有一组、所有行都可以放，冲突最少，但查找成本高。组相联是折中：

- 相联度越高，越不容易发生“多个热点行映射到同一组”的冲突；
- 但查找、替换和功耗也更复杂，硬件不会无限提高相联度。

一个简化的冲突例子：假设 32 KiB、64B 行、8 路，共 64 组。如果连续访问 9 个地址，它们之间都相隔 4096 字节（4 KiB），那么它们的组索引位相同，都会落到同一组。8 路只能放 8 条，第 9 条会把其中一条挤出去；如果循环反复访问这 9 条，就可能不断互相驱逐，这叫**冲突未命中**。

```c
uint64_t sum = 0;
for (int i = 0; i < 9; i++) {
    sum += a[i * 512];
}
```

这里 `512 × 8 字节 = 4096 字节`。真实 CPU 的索引方式、物理地址转换、预取器和相联度都比这个模型复杂，所以这段代码是帮助理解“组冲突”的思维实验，不是精确的性能预测。实际机器可能是 12 路、16 路，甚至 L1 使用 VIPT 等设计，不能拿一个公式硬套所有 CPU。

### 2.6 工作集、容量和访问步长

**工作集**（working set）是指在一段时间窗口内，程序真正访问到的地址集合或字节数。注意它不是“进程虚拟地址空间有多大”，也不是“数组声明了多大”。

例如：

- 一个 1 GiB 的数组，但最近只反复访问其中 1 MiB 的热点区域：工作集可能只有 1 MiB，能放进缓存。
- 一个 64 MiB 的数组，按随机顺序访问：工作集接近 64 MiB，超过常见的 32 MiB L3，就会频繁访问 DRAM。
- 一个链表有 100 万个节点，但只遍历最近插入的 1000 个：工作集可能很小；如果遍历全部节点，工作集就是所有被触及的缓存行。

经典上可以把缓存未命中粗略分成三类：

| 类型 | 含义 | 例子 |
|---|---|---|
| 强制性未命中 | 第一次访问，缓存里本来就没有 | 冷启动扫描 |
| 容量未命中 | 工作集超过缓存容量 | 大数组随机访问 |
| 冲突未命中 | 多个热点行映射到同一组 | 4 KiB 步长的多个流 |

**访问步长**（stride）是相邻两次访问之间的地址距离。假设 `uint64_t` 为 8 字节、缓存行 64 字节：

| 步长（元素） | 相邻访问间隔 | 每条缓存行大约用到 | 每行可服务元素数 |
|---:|---:|---:|---:|
| 1 | 8 B | 64 B | 8 |
| 2 | 16 B | 32 B | 4 |
| 4 | 32 B | 16 B | 2 |
| 8 | 64 B | 8 B | 1 |
| 16 | 128 B | 8 B（且跳过部分行） | 1 |

步长越大，空间局部性通常越差：每次访问都可能落在新的缓存行上，缓存行里大部分字节没被利用。但也要注意两个细节：

1. **预取器**可能识别固定步长并提前取数据，所以“步长大”不一定线性变慢，必须实测。
2. 如果固定数组大小、只遍历整个数组，那么步长越大，访问的元素数越少，工作集也可能变小，`ns/元素` 未必单调上升。做实验时要控制变量。

衡量局部性时，常用**每元素耗时**：

```text
每元素耗时 = 总耗时 / 实际访问元素数
```

只看总时间会被“访问了多少元素”干扰。例如步长 32 的循环可能总时间更短，因为它只访问了 1/32 的元素；但按每元素计算，可能更慢。

### 2.7 把三个要点串起来

一次连续数组求和大致经历：

1. 第一次访问 `a[0]`：缓存未命中，硬件从 DRAM 搬入包含 `a[0]` 的 64B 行。
2. 访问 `a[1]` 到 `a[7]`：大概率命中同一条缓存行，利用空间局部性。
3. 访问 `a[8]`：进入下一条缓存行，可能触发新的搬运；预取器可能已经提前取了一部分。
4. 如果数组远大于缓存，前面的行会被后来的行挤出，出现容量未命中。
5. 如果访问模式变成大步长或随机指针，缓存行利用率下降，预取效果变差，每元素耗时上升。

链表则多了一层问题：

```c
typedef struct Node {
    uint64_t value;
    struct Node *next;
} Node;
```

遍历 `p = p->next` 时，下一次访问的地址依赖当前节点里的指针。CPU 很难提前知道下一个节点在哪，预取器也不容易帮忙；如果节点在堆上分散，几乎每次解引用都可能碰到新的缓存行。**指针追逐 + 随机布局 + 依赖链**，是链表扫描常常慢于连续数组的根本原因。

### 2.8 用 `perf` 观察缓存未命中

先确认机器和缓存参数，再用 `perf` 看运行时事件：

```bash
lscpu
getconf -a | grep -i cache

perf stat -e cache-references,cache-misses,cycles,instructions ./locality
```

只看缓存未命中也可以：

```bash
perf stat -e cache-misses ./locality
```

不同内核、不同 CPU 支持的事件名不完全一样；`LLC-loads`、`LLC-load-misses` 等事件在部分机器上可用，在容器或云主机里也可能被限制。若出现权限错误，先检查：

```bash
cat /proc/sys/kernel/perf_event_paranoid
```

不要在没确认影响的情况下随意修改生产机的内核参数。测试环境里也要记录 CPU 型号、编译器版本和编译选项，否则不同次实验的数字没有可比性。

---

## 3. 常见误区与设计权衡

### 3.1 误区一：缓存命中率高，就没有带宽瓶颈

节点特别提醒：

> 缓存命中率高不代表没有带宽瓶颈。

“命中率”是一个比例：

```text
命中率 = 命中次数 / 总访问次数
```

它没有告诉你：

- 每次命中要搬多少字节；
- 总共有多少次访问；
- 缓存层级之间的带宽是否已经饱和；
- 未命中的那部分有多贵；
- 访问是否被依赖链卡住。

举例来说，一个服务每秒产生大量缓存访问，即使 99% 都在 L3 命中，剩下 1% 的未命中在巨大请求量下仍可能是每秒数百万次 DRAM 访问；同时，每次命中也要在 L1/L2/L3 之间传输缓存行，L3 带宽可能先成为瓶颈。另一种情况是命中率很高，但每次访问都依赖上一次结果，CPU 只能干等，表现为延迟瓶颈而不是带宽瓶颈。

所以至少要看一组指标，而不是只看一个比例：

- `cycles`、`instructions`：每周期指令数、整体执行效率；
- `cache-misses`：缓存未命中次数；
- `LLC-load-misses`（若支持）：最后一级缓存未命中；
- 实际吞吐、p99 延迟、每请求内存访问量；
- 工作集大小和访问步长。

### 3.2 误区二：把页缓存当成 CPU 缓存

这是两个不同层次的东西，必须分清：

| 对比项 | CPU 缓存 | 页缓存（page cache） |
|---|---|---|
| 位置 | CPU 内部或靠近 CPU 的 SRAM | DRAM 中的文件页 |
| 管理方式 | CPU 硬件自动管理 | 操作系统内核管理 |
| 单位 | 缓存行，常见 64B | 内存页，常见 4KiB |
| 主要目的 | 降低访问 DRAM 的延迟 | 避免每次都读磁盘 |
| 观察方式 | `lscpu`、`getconf`、`perf` | `free -h`、`/proc/meminfo` |
| 命中含义 | 数据在 CPU 缓存中 | 文件内容已在内存页中 |

几个容易混淆的现象：

- `free -h` 里的 `buff/cache` 很大，说明内核缓存了不少文件页，**不代表 CPU 缓存很大**。
- 文件已经在 page cache 中，读取时仍然要经过 CPU 缓存和内存；page cache 命中只是避免磁盘 I/O，不是“CPU 免费拿到数据”。
- `perf stat -e cache-misses` 统计的是 CPU 缓存未命中，不是 page cache 未命中。文件不在 page cache 里通常表现为磁盘读、缺页或 I/O 等待，要用 `iostat`、`vmstat`、`/proc/vmstat` 等工具看。

可以这样观察：

```bash
free -h
grep -E 'MemTotal|MemFree|Cached|Buffers' /proc/meminfo
getconf PAGE_SIZE
```

`getconf PAGE_SIZE` 常见结果是 4096，即 4 KiB。它和 64B 的缓存行不是一个概念：页是虚拟内存和文件缓存的管理单位，缓存行是 CPU 缓存搬运的单位。

### 3.3 其他常见误区

**误区：数组一定比链表快。**  
对于“从头到尾顺序扫描”，连续数组通常更有优势，因为空间局部性和预取器都站在它这边。但如果是频繁在已知位置插入/删除，链表可能避免大范围搬移；如果数组访问是随机的、链表节点恰好很集中，结论也可能变化。**布局和访问模式要一起看。**

**误区：缓存越大越好。**  
缓存变大可能降低容量未命中，但也会带来更高的访问延迟、更复杂的相联/替换逻辑和更高的成本。真实系统是层次化折中，不是单一容量数字比赛。

**误区：对齐和填充总是有益。**  
把热点计数器按缓存行对齐，能避免伪共享；但填充会增加内存占用，减少有效缓存容量，也可能让更新路径更复杂。是否值得，要用并发量和性能数据判断。

### 3.4 设计权衡：紧凑布局不是免费午餐

节点给出的权衡是：

> 紧凑布局通常利于缓存，但可与更新成本和并发写冲突。

这句话可以展开成几组常见取舍：

1. **AoS 与 SoA**  
   把一条记录的字段放在一起（AoS），读取整条记录时局部性好；但如果只扫描其中一个热字段，冷字段会浪费缓存行。拆成多个数组（SoA），扫描单个字段更紧凑，但更新一条完整记录可能要访问多个数组，代码和内存分配也更复杂。

2. **紧凑与伪共享**  
   两个线程分别更新同一缓存行里的两个计数器，逻辑上互不相关，硬件却要来回同步这条缓存行，这就是伪共享。把计数器分开到不同缓存行可以缓解，但会增加内存占用。紧凑布局节省容量，却可能制造写冲突。

3. **不可变快照与更新成本**  
   读多写少的场景可以用不可变快照/写时复制，让读路径看到紧凑、稳定的数据；但每次更新要复制，分配和 GC 压力会上升。读路径的缓存友好，可能是用写路径的成本换来的。

4. **连续缓冲区与扩容**  
   序列化时把字段连续写入一个 buffer，解析和扫描都更友好；但 buffer 扩容可能触发整块复制。预留容量能减少复制，却会浪费内存。大量小对象则灵活，但容易形成指针追逐和分配器压力。

5. **批处理与延迟**  
   把随机小访问合并成连续批量访问，通常能提高缓存和带宽利用率；但批处理会增加排队延迟。后台系统要在吞吐和 p99 之间做选择。

一个实用的原则是：**先测量，再改布局。** 不要因为“理论上缓存友好”就把结构改得难以维护；也不要因为“复杂度一样”就忽略数据布局。

---

## 4. 动手实践：比较数组、链表与步长

### 4.1 实践目标

完成一个可运行的 C 程序，记录：

- 连续数组顺序遍历的每元素耗时；
- 链表遍历的每元素耗时；
- 不同步长遍历数组的每元素耗时；
- 工作集/访问跨度变化时，每元素耗时如何变化。

节点给出的实践要求是：

> 比较连续数组、链表与不同步长遍历，记录每元素耗时。

### 4.2 第一步：查看当前机器

```bash
lscpu
getconf -a | grep -i cache
getconf LEVEL1_DCACHE_LINESIZE
getconf LEVEL1_DCACHE_SIZE
getconf LEVEL2_CACHE_SIZE
getconf LEVEL3_CACHE_SIZE
```

把结果记下来，尤其是：

- CPU 型号；
- L1/L2/L3 容量；
- 缓存行大小；
- 是否有多个 NUMA 节点。

后面的实验数字必须和这些参数一起解释。不要拿一台机器的结果去断言另一台机器。

### 4.3 第二步：保存程序

把下面的代码保存为 `locality.c`。程序默认：

- 数组/链表实验使用 8M 个元素；
- 步长实验每次访问 1M 个元素，最大步长 32；
- 每个测试跑 3 次，取中位数；
- 用 `volatile` 全局变量接收结果，防止编译器把整个循环优化掉。

```c
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stddef.h>
#include <time.h>

typedef struct Node {
    uint64_t value;
    struct Node *next;
} Node;

static volatile uint64_t sink;

static double now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec * 1e9 + (double)ts.tv_nsec;
}

static double median3(double a, double b, double c) {
    if (a > b) { double t = a; a = b; b = t; }
    if (b > c) { double t = b; b = c; c = t; }
    if (a > b) { double t = a; a = b; b = t; }
    return b;
}

static double bench_array(const uint64_t *a, size_t n, size_t stride,
                          size_t *visited_out) {
    uint64_t sum = 0;
    size_t visited = 0;
    double t0 = now_ns();
    for (size_t i = 0; i < n; i += stride) {
        sum += a[i];
        visited++;
    }
    double t1 = now_ns();
    sink = sum;
    *visited_out = visited;
    return t1 - t0;
}

static double bench_array_fixed(const uint64_t *a, size_t visits, size_t stride) {
    uint64_t sum = 0;
    double t0 = now_ns();
    for (size_t i = 0; i < visits; i++) {
        sum += a[i * stride];
    }
    double t1 = now_ns();
    sink = sum;
    return t1 - t0;
}

static double bench_list(Node *head, size_t *visited_out) {
    uint64_t sum = 0;
    size_t visited = 0;
    double t0 = now_ns();
    for (Node *p = head; p != NULL; p = p->next) {
        sum += p->value;
        visited++;
    }
    double t1 = now_ns();
    sink = sum;
    *visited_out = visited;
    return t1 - t0;
}

static Node *build_shuffled_list(size_t n) {
    if (n == 0) return NULL;
    Node **slots = malloc(n * sizeof(*slots));
    if (!slots) return NULL;

    for (size_t i = 0; i < n; i++) {
        Node *node = malloc(sizeof(*node));
        if (!node) {
            for (size_t j = 0; j < i; j++) free(slots[j]);
            free(slots);
            return NULL;
        }
        node->value = i;
        node->next = NULL;
        slots[i] = node;
    }

    uint64_t state = 0x9e3779b97f4a7c15ULL;
    for (size_t i = n; i > 1; i--) {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        size_t j = (size_t)(state % i);
        Node *tmp = slots[i - 1];
        slots[i - 1] = slots[j];
        slots[j] = tmp;
    }

    for (size_t i = 0; i + 1 < n; i++) slots[i]->next = slots[i + 1];
    slots[n - 1]->next = NULL;
    Node *head = slots[0];
    free(slots);
    return head;
}

static void free_list(Node *head) {
    while (head != NULL) {
        Node *next = head->next;
        free(head);
        head = next;
    }
}

static void report(const char *name, size_t visited, double span_mib,
                   double ns, size_t elements) {
    double per = elements ? ns / (double)elements : 0.0;
    printf("%-24s %10zu %10.1f %12.3f %12.3f\n",
           name, visited, span_mib, ns / 1e6, per);
}

int main(int argc, char **argv) {
    size_t n = 8u * 1024u * 1024u;
    size_t visits = 1u * 1024u * 1024u;
    const size_t max_stride = 32;

    if (argc > 1) {
        char *end = NULL;
        unsigned long long v = strtoull(argv[1], &end, 10);
        if (end == argv[1] || *end != '\0' || v == 0) {
            fprintf(stderr, "用法: %s [元素数] [步长实验访问次数]\n", argv[0]);
            return 1;
        }
        n = (size_t)v;
        if ((unsigned long long)n != v) {
            fprintf(stderr, "元素数超出 size_t 范围\n");
            return 1;
        }
    }
    if (argc > 2) {
        char *end = NULL;
        unsigned long long v = strtoull(argv[2], &end, 10);
        if (end == argv[2] || *end != '\0' || v == 0) {
            fprintf(stderr, "用法: %s [元素数] [步长实验访问次数]\n", argv[0]);
            return 1;
        }
        visits = (size_t)v;
        if ((unsigned long long)visits != v) {
            fprintf(stderr, "访问次数超出 size_t 范围\n");
            return 1;
        }
    }
    if (n > SIZE_MAX / sizeof(uint64_t) ||
        visits > SIZE_MAX / max_stride ||
        visits * max_stride > SIZE_MAX / sizeof(uint64_t)) {
        fprintf(stderr, "规模过大，乘法会溢出\n");
        return 1;
    }

    uint64_t *a = malloc(n * sizeof(*a));
    if (!a) {
        perror("malloc array");
        return 1;
    }
    for (size_t i = 0; i < n; i++) a[i] = i;

    Node *head = build_shuffled_list(n);
    if (!head) {
        perror("build list");
        free(a);
        return 1;
    }

    printf("数组/链表: n=%zu, 数组约 %.1f MiB, 单节点 %zu B\n",
           n, (double)(n * sizeof(*a)) / (1024.0 * 1024.0), sizeof(Node));
    printf("%-24s %10s %10s %12s %12s\n",
           "workload", "visited", "span_MiB", "total_ms", "ns/elem");

    double t[3];
    size_t visited = 0;
    for (int r = 0; r < 3; r++) {
        t[r] = bench_array(a, n, 1, &visited);
    }
    report("array stride=1", visited,
           (double)(n * sizeof(*a)) / (1024.0 * 1024.0),
           median3(t[0], t[1], t[2]), visited);

    for (int r = 0; r < 3; r++) {
        t[r] = bench_list(head, &visited);
    }
    report("linked list shuffled", visited,
           (double)(n * sizeof(Node)) / (1024.0 * 1024.0),
           median3(t[0], t[1], t[2]), visited);

    free_list(head);
    free(a);

    size_t sn = visits * max_stride;
    uint64_t *s = malloc(sn * sizeof(*s));
    if (!s) {
        perror("malloc stride array");
        return 1;
    }
    for (size_t i = 0; i < sn; i++) s[i] = i;

    printf("\n步长实验: 每个步长访问 %zu 个元素, 数组约 %.1f MiB\n",
           visits, (double)(sn * sizeof(*s)) / (1024.0 * 1024.0));
    printf("%-24s %10s %10s %12s %12s\n",
           "workload", "visited", "span_MiB", "total_ms", "ns/elem");

    size_t strides[] = {1, 2, 4, 8, 16, 32};
    for (size_t k = 0; k < sizeof(strides) / sizeof(strides[0]); k++) {
        size_t stride = strides[k];
        for (int r = 0; r < 3; r++) {
            t[r] = bench_array_fixed(s, visits, stride);
        }
        char name[64];
        snprintf(name, sizeof(name), "array stride=%zu", stride);
        report(name, visits,
               (double)(visits * stride * sizeof(*s)) / (1024.0 * 1024.0),
               median3(t[0], t[1], t[2]), visits);
    }

    free(s);
    return 0;
}
```

### 4.4 第三步：编译并运行

```bash
gcc -O2 -Wall -Wextra -o locality locality.c
./locality
```

如果内存较小，先用小一点的规模验证程序：

```bash
./locality 2000000 1000000
```

程序的两个参数分别是：

1. 数组/链表实验的元素数，默认 `8388608`（8M）；
2. 步长实验每次访问的元素数，默认 `1048576`（1M）。

步长实验会分配 `访问次数 × 32` 个 `uint64_t`。默认约 256 MiB。如果机器内存紧张，把第二个参数调小，例如：

```bash
./locality 2000000 250000
```

### 4.5 第四步：理解输出

在某台机器上运行 `./locality 2000000 1000000`，可能看到类似输出（数字仅用于说明格式，不是标准答案）：

```text
数组/链表: n=2000000, 数组约 15.3 MiB, 单节点 16 B
workload                    visited   span_MiB     total_ms      ns/elem
array stride=1              2000000       15.3        2.136        1.068
linked list shuffled        2000000       30.5      245.817      122.908

步长实验: 每个步长访问 1000000 个元素, 数组约 244.1 MiB
workload                    visited   span_MiB     total_ms      ns/elem
array stride=1              1000000        7.6        0.964        0.964
array stride=2              1000000       15.3        1.294        1.294
array stride=4              1000000       30.5        2.098        2.098
array stride=8              1000000       61.0        3.189        3.189
array stride=16             1000000      122.1        6.227        6.227
array stride=32             1000000      244.1        7.751        7.751
```

读表时注意：

- `visited` 是实际访问的元素数；
- `span_MiB` 是访问跨度或数据大小的近似值，不等于真实占用（链表还有分配器开销）；
- `total_ms` 是 3 次运行的中位数；
- `ns/elem` 才是横向比较时最常用的指标。

链表通常明显慢于连续数组，原因不只是“链表多了一次指针解引用”，还包括：

- 节点来自多次 `malloc`，地址分散；
- 每个节点只有 16 字节，缓存行利用率低；
- 下一次访问依赖当前节点的 `next`，形成依赖链；
- 预取器难以预测随机地址。

步长实验中，每个步长访问的元素数相同，但地址跨度随步长增大。`span_MiB` 变大时，缓存行利用率和预取效率通常会下降，`ns/elem` 往往上升。要注意，`span_MiB` 是地址跨度，不等于实际触及的字节数：步长很大时会跳过一些缓存行，真正搬入的字节可能少于跨度。判断是否超过缓存容量时，应结合 `perf` 的未命中数据，而不是只看跨度。若你的机器 L3 很大，曲线可能比较平；这时把第二个参数调大，或者把 `max_stride` 调大，让实际工作集超过最后一级缓存。

### 4.6 第五步：用 `perf` 交叉验证

```bash
perf stat -e cache-references,cache-misses,cycles,instructions ./locality 2000000 1000000
```

也可以只看缓存未命中：

```bash
perf stat -e cache-misses ./locality 2000000 1000000
```

建议把下面这些信息一起记录到实验笔记里：

```text
CPU 型号：
L1d / L2 / L3 容量：
缓存行大小：
编译器版本：
编译选项：
元素数 / 访问次数：
三次运行的 ns/elem：
perf 的 cache-misses、cycles、instructions：
```

不要只记录一个“快了/慢了”的结论。节点提醒过，资源里的历史硬件参数要和当前机器实测区分；同样，别人的实验数字也不能直接套到你的机器上。

### 4.7 第六步：解释工作集超过缓存后的变化

用实验结果回答这三个问题：

1. **连续数组为什么通常快？**  
   因为顺序访问利用了空间局部性，一条 64B 行可服务多个 8B 元素，预取器也容易工作。

2. **链表为什么可能慢很多？**  
   因为指针追逐让下一次地址不可预测，节点分散导致每次访问新缓存行的概率高，且存在依赖链。

3. **步长变大后为什么每元素耗时上升？**  
   因为每次访问利用到的缓存行字节变少，地址跨度变大，工作集更容易超过缓存；但要注意预取器和实际缓存大小会让曲线不完全线性。

如果工作集超过 L3 后，每元素耗时明显上升，通常说明容量未命中开始占主导，但也可能混有 TLB 未命中、冲突未命中或预取失效等因素。若曲线没有变化，可能是：

- 工作集仍然小于 L3；
- 预取器很好地处理了固定步长；
- 编译器做了优化；
- 测量被循环开销、频率变化或容器噪声干扰。

### 4.8 实践验收

完成后，你应该能用自己的话解释：

- [ ] 为什么连续数组的每元素耗时通常低于随机链表；
- [ ] 为什么步长增大可能降低缓存行利用率；
- [ ] 为什么工作集超过缓存后，命中率和每元素耗时会变化；
- [ ] 为什么 `free -h` 里的 `buff/cache` 不能当作 CPU 缓存大小；
- [ ] 为什么“缓存命中率高”仍然可能是带宽或延迟问题。

---

## 5. 自测题与答案

### 题 1：时间局部性和空间局部性有什么区别？

请分别解释，并各举一个微信公众号后台的场景。

**答案：**

- **时间局部性**：同一个地址在短时间内被反复访问。例如同一个热点用户的鉴权结果、限流配额、配置对象被大量请求重复读取。优化方向是让热点数据留在近处、减少重复随机查找，或使用合适的应用层缓存。
- **空间局部性**：一个地址被访问后，附近地址很快也被访问。例如顺序解析 Protobuf/JSON 缓冲区、顺序扫描用户 ID 数组。优化方向是使用连续布局、批量处理、减少指针追逐。

两者经常同时出现：顺序扫描数组既利用了空间局部性，累加器又利用了时间局部性。

### 题 2：一个 32 KiB、64B 缓存行、8 路组相联的 L1 缓存有多少组？为什么 9 个相隔 4 KiB 的热点地址可能发生冲突？

**答案：**

组数：

```text
S = 32 KiB / (64 B × 8) = 64 组
```

64 组意味着组索引使用 6 位，64B 行内偏移使用 6 位。相隔 4096 字节（4 KiB）的地址，其低位组索引不变，因此会映射到同一组。8 路只能同时容纳 8 条这样的缓存行；如果循环反复访问 9 条，就会互相驱逐，形成冲突未命中。

这是简化模型。真实 CPU 的相联度、地址转换、预取器和替换策略更复杂，实际结果要以机器参数和 `perf` 实测为准。

### 题 3：服务缓存命中率 99%，但 p99 延迟上升。可能是什么原因？应该看哪些指标？

**答案：**

可能原因包括：

- 命中率是比例，不反映总访问量和每次搬运的字节数；高并发下，即使 1% 未命中也可能产生大量 DRAM 访问。
- 命中发生在较远的缓存层（例如 L3），延迟仍然不低。
- L3/DRAM 带宽可能已经饱和，缓存命中率高不代表没有带宽瓶颈。
- 访问依赖链长，CPU 在等下一次加载，表现为延迟问题。
- 工作集、步长或 TLB 行为变化，导致未命中的代价变高。
- 把 page cache 命中和 CPU 缓存命中混为一谈，误判了瓶颈位置。

建议至少看：

- `perf stat -e cache-references,cache-misses,cycles,instructions`；
- 若机器支持，看 `LLC-loads`、`LLC-load-misses`；
- 每请求的内存访问量、实际吞吐和 p99；
- 工作集大小、访问步长、NUMA 和 CPU 迁移情况；
- 磁盘 I/O 和 page cache 指标（`free -h`、`/proc/meminfo`、`iostat`、`vmstat`），用于排除“页缓存/磁盘”问题。

关键不是只追求更高的命中率，而是找到**延迟、带宽、容量、访问模式**中真正的限制因素。

---

## 6. 推荐资源

节点给出的资源是：

- **What Every Programmer Should Know About Memory**
  - 作者：Ulrich Drepper
  - 类型：技术论文
  - 语言：英文
  - 难度：进阶
  - 是否免费：是
  - 链接：https://www.akkadia.org/drepper/cpumemory.pdf

阅读建议：

1. 先读缓存、局部性和数据布局相关章节，把本课的缓存行、组相联、工作集概念串起来。
2. 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件，不要只记结论。
3. 论文里的硬件参数有历史背景，**必须和当前机器的 `lscpu`、`getconf`、`perf` 实测区分**。
4. 读完一章后，回到本课的 `locality.c`，改一改数组大小、步长、链表布局，观察结果是否支持论文中的解释。

本课的最终目标不是记住“缓存有几级”，而是形成一种稳定的分析方法：

```text
先看数据布局和访问顺序
-> 再看工作集是否超过缓存
-> 用 perf 和实验验证
-> 最后才决定是否调整结构
```
