# 第 38/208 课：虚拟内存、缺页与mmap

> 节点：`c-vm`｜领域：C｜阶段：2｜难度：基础
>
> 一句话总结：虚拟地址通过页表映射到物理页，缺页可能分配、加载或触发访问错误；mmap 建立映射而非直接复制文件。

## 一、学习目标与为什么后端要懂

学完本节，你应当能够：

1. 解释页表与 TLB 的作用，说清一次内存访问如何从虚拟地址走到物理页。
2. 区分匿名映射与文件映射，理解 `MAP_SHARED` / `MAP_PRIVATE` 的语义差异。
3. 解释缺页的几种结果：分配零页、从 page cache 或磁盘加载、COW 复制、触发 `SIGSEGV` 或 `SIGBUS`。
4. 独立完成实验：映射一个文件并顺序/随机读取，记录 major/minor faults 与 RSS 变化。
5. 区分 VIRT 与 RSS，解释文件被截断后继续访问映射的风险。

为什么后端工程师要懂这些？因为你每天打交道的进程指标、内存占用和文件访问方式，背后都是这套机制：

- 线上 `top` 里某个服务 VIRT 几个 GB，并不代表它真的占了几个 GB 物理内存；看不懂就会误判内存泄漏。
- 用 `mmap` 加载只读词表、倒排索引、配置快照，看起来很优雅，但首次访问会触发缺页，延迟抖动、page cache 压力和错误处理都要提前设计。
- 容器里 RSS 过高会触发 OOM，而 RSS 里既可能有匿名页，也可能有共享的文件页，统计口径直接影响容量规划。
- 文件被截断、磁盘故障、网络文件系统抖动，都可能让一次普通的内存读取变成 `SIGBUS`，后端服务必须有兜底。

本节以腾讯微信公众号后台这类长驻服务为背景：只读词表/索引文件用 `mmap` 加载、进程 VIRT 很大、需要控制 RSS 和尾延迟。下面从直觉到原理逐层展开。

---

## 二、从直觉到原理

### 2.1 先建立直觉：虚拟地址不是物理地址

进程看到的每个地址都是虚拟地址。CPU 访存时，先用页表把虚拟页号（VPN）翻译成物理页号（PFN），再加上页内偏移，才真正访问物理内存。

- 虚拟地址空间可以很大，甚至大于物理内存；只有真正被访问的页才需要物理页。
- 每个进程有独立页表，所以同样的虚拟地址在不同进程里可以指向不同物理页。
- 映射关系不是“整块连续”，而是以页为单位（常见 4 KiB，也有 2 MiB/1 GiB 大页）。

这解释了一个常见现象：进程 `malloc` 了 1 GiB，VIRT 立刻涨了 1 GiB，但 RSS 可能只有几 MiB。因为地址空间只是“预留了编号”，物理页要等首次访问触发缺页才分配。

### 2.2 页表与 TLB

#### 页表

以常见的 4 KiB 页、48 位虚拟地址为例，一个虚拟地址可拆成：

```
| 页表各级索引（9+9+9+9 位） | 页内偏移（12 位） |
```

多级页表的好处是稀疏：没有用到的地址区间不需要建表。每个页表项（PTE）里除了物理页号，还有：

- present 位：该页当前是否在内存中；
- 读写/执行权限位；
- user/supervisor 位；
- accessed / dirty 位；
- 与 COW、swap、文件映射相关的标志。

一次地址翻译理论上要逐级查表，最坏多次内存访问。为了不让每次访存都变慢，CPU 引入了 TLB。

#### TLB

TLB（Translation Lookaside Buffer）是页表项的硬件缓存，缓存“虚拟页号 → 物理页号”的翻译结果。

- TLB 命中：直接得到物理地址，几乎无额外开销。
- TLB 未命中：硬件（或内核）走页表，必要时触发缺页异常。
- 进程切换、`munmap`、权限变化、页表项修改会破坏 TLB 有效性；现代 CPU 用 PCID/ASID 减少切换时的全量刷新。

对后端的启示：

- 大量随机访问分散在大地址空间，会降低 TLB 命中率；顺序访问和局部性好得多。
- 大页（Huge Page）能显著减少 TLB 项数量和页表层级，但会带来内部碎片和分配/回收复杂度。
- `madvise(MADV_HUGEPAGE)`、透明大页（THP）等调优，本质上都在改善 TLB 与缺页行为。

### 2.3 匿名映射与文件映射

Linux 的 `mmap` 建立的是“虚拟内存区域（VMA）到后备对象”的映射。按后备对象分两类。

#### 匿名映射

没有文件作为后备，常用 `MAP_ANONYMOUS`：

```c
void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
```

特点：

- 首次读通常映射到内核的共享零页，只读；首次写触发缺页，分配真正的匿名页（按需零填充）。
- 匿名页没有文件可回写，内存紧张时只能换出到 swap（如果启用）。
- `malloc` 的大块分配、线程栈、`brk`/`mmap` 扩展的堆，很多都落在匿名映射上。

#### 文件映射

用打开的文件描述符作为后备：

```c
int fd = open("index.bin", O_RDONLY);
void *p = mmap(NULL, len, PROT_READ, MAP_PRIVATE, fd, 0);
```

按共享语义又分：

| 标志 | 写操作的结果 | 典型用途 |
| --- | --- | --- |
| `MAP_SHARED` | 修改写回文件，对其他映射者可见 | 共享内存、数据库文件、日志 |
| `MAP_PRIVATE` | 写时复制（COW），修改不落盘 | 只读加载、进程私有快照 |

文件映射的关键点是：`mmap` 本身通常只建立 VMA，不读取文件内容。真正的数据要等访问对应页时，通过缺页从 page cache 取得；page cache 没有再发起磁盘 IO。

#### 一个最小示例：映射并读取（保存为 `vmtest.c`）

```c
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>

static void show_faults(const char *tag) {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    printf("%-12s min_flt=%ld maj_flt=%ld maxrss=%ldKB\n",
           tag, ru.ru_minflt, ru.ru_majflt, ru.ru_maxrss);
}

int main(int argc, char **argv) {
    if (argc != 2) {
        fprintf(stderr, "usage: %s <file>\n", argv[0]);
        return 1;
    }

    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    struct stat st;
    if (fstat(fd, &st) < 0) { perror("fstat"); return 1; }
    size_t len = (size_t)st.st_size;

    char *p = mmap(NULL, len, PROT_READ, MAP_PRIVATE, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    show_faults("after mmap");   /* 通常还没有读文件数据 */

    volatile unsigned long sum = 0;
    for (size_t i = 0; i < len; i++)
        sum += (unsigned char)p[i];
    show_faults("sequential");

    srand(12345);
    for (int i = 0; i < 100000; i++)
        sum += (unsigned char)p[(size_t)rand() % len];
    show_faults("random");

    printf("sum=%lu\n", sum);
    munmap(p, len);
    close(fd);
    return 0;
}
```

观察重点：

- `after mmap` 时 major/minor faults 往往没有明显增长，因为映射还没触碰数据页。
- 顺序读取会触发一批 minor 或 major faults；如果文件刚被写过、还在 page cache 中，多为 minor。注意缺页次数通常**远少于页数**：Linux 文件映射有 fault-around（一次缺页顺手映射周围已在 page cache 的若干页，常见 16 页即 64 KiB），再加上预读。我在内核 6.12 上实测 16 MiB（4096 页）热缓存文件顺序读，只多了约 262 次 minor fault、0 次 major fault。
- 随机读取不一定新增 major faults，因为前面顺序读已经把页读进来了；要观察随机访问的代价，应先 `echo 3 > /proc/sys/vm/drop_caches`（需 root）或换一个未读过的文件。

### 2.4 缺页、COW 与 SIGBUS

#### 缺页不是“错误”，而是一个处理流程

CPU 访问的页 PTE 不满足条件时触发缺页异常，内核根据 VMA 和 PTE 状态决定怎么处理：

1. **匿名页首次写**：分配物理页并清零。
2. **文件页首次访问**：page cache 命中则建立映射（minor fault）；未命中则发起磁盘 IO（major fault）。
3. **COW 页首次写**：复制一份物理页，修改当前进程的 PTE 指向新页，原页引用计数减一。
4. **页被换出**：从 swap 读回（major fault）。
5. **非法访问**：地址不在任何 VMA、权限不符，发送 `SIGSEGV`；文件映射超出有效范围或底层 IO 错误，可能发送 `SIGBUS`。

minor fault 不涉及磁盘 IO，major fault 需要 IO（或 swap）。这也是为什么“首次访问”通常比“热数据访问”慢得多。

#### COW：写时复制

`fork()` 之后，父子进程的页表最初指向同一批物理页，并标记为只读。任何一方写入时触发缺页，内核才复制该页：

```c
char *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
p[0] = 'A';

pid_t pid = fork();
if (pid == 0) {
    p[0] = 'B';   /* 触发 COW：给子进程复制一页 */
    _exit(0);
}
```

要点：

- COW 让 `fork` 很快，但“写”会把成本推迟；大量写会导致大量复制，RSS 也会上涨。
- `MAP_PRIVATE` 的文件映射同样是 COW：读的是 page cache，写会复制出私有匿名页，不修改文件。
- 后端服务里，`fork` 后立刻 `exec` 通常没事；`fork` 后长期在子进程里改内存，就要警惕 COW 放大。

#### SIGBUS：文件映射的“隐形炸弹”

`SIGBUS` 常见于文件映射的以下场景：

- 映射后文件被截断，访问落在“文件末尾之后、但仍在映射长度之内”的页；
- 映射的最后一页不足一页，访问超出文件实际长度的部分；
- 底层存储 IO 错误、网络文件系统断连等。

示例（保存为 `trunc.c`）：

```c
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

int main(void) {
    int fd = open("data.bin", O_RDWR | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) { perror("open"); return 1; }
    if (ftruncate(fd, 4096) < 0) { perror("ftruncate"); return 1; }

    char *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }

    p[0] = 'A';                 /* 正常 */
    if (ftruncate(fd, 0) < 0) { perror("ftruncate"); return 1; }

    printf("%c\n", p[0]);       /* 文件已截断，访问可能触发 SIGBUS */
    return 0;
}
```

为什么不是 `SIGSEGV`？因为地址本身是合法映射，问题出在“映射的后备文件不再提供这一页”，所以内核用 `SIGBUS` 表示总线/存储层面的错误。后端设计上必须注意：

- 文件映射期间，其他进程或本进程不要随意截断文件；
- 更新索引时用“写新文件 + 原子 rename”替换，而不是原地截断；
- 对可能 `SIGBUS` 的访问路径做好信号处理或改用带返回值检查的 `pread`。

### 2.5 VIRT 与 RSS：最容易混淆的两个指标

| 指标 | 含义 | 是否占物理内存 | 典型观察 |
| --- | --- | --- | --- |
| VIRT / VSZ / VmSize | 虚拟地址空间大小 | 否 | 线程栈、`malloc` 预留、映射文件都会让它变大 |
| RSS / VmRSS | 常驻物理内存（resident set size） | 是（含共享页） | 真正被访问并留在内存中的页 |
| VmData | 数据段相关虚拟内存 | 否 | 堆、匿名映射等 |
| VmSwap | 被换出的匿名页 | 否（在 swap） | 内存压力大时上涨 |

注意：

- RSS 统计的是“物理页被这个进程映射且驻留”，同一个 page cache 页可能被多个进程计入各自的 RSS，因此多进程 RSS 相加会高估真实占用。需要更精确口径时看 PSS（`/proc/<pid>/smaps_rollup`）。
- VIRT 很大不等于内存泄漏。Go、JVM、glibc 多 arena、线程栈、文件映射都会让 VIRT 看起来夸张。
- `mmap` 一个 10 GiB 文件，VIRT 可能增加约 10 GiB，但只要不访问，RSS 不会同步增加。

常用命令：

```bash
# 查看进程的地址空间布局：每行是一个 VMA
cat /proc/<pid>/maps

# 只看关键内存计数
grep -E 'VmSize|VmRSS|VmData|VmSwap|VmPTE' /proc/<pid>/status

# 统计子进程的 minor/major faults 与峰值 RSS
/usr/bin/time -v ./vmtest index.bin

# 持续观察指定进程
ps -o pid,min_flt,maj_flt,rss,vsz,cmd -p <pid>

# 在程序内部读取自身 fault 计数
# getrusage(RUSAGE_SELF, &ru) -> ru_minflt / ru_majflt / ru_maxrss
```

字段解释：

- `min_flt`：minor page faults，无需磁盘 IO 的缺页次数；
- `maj_flt`：major page faults，需要 IO 的缺页次数；
- `rss`：常驻内存，单位通常是 KiB；
- `vsz`：虚拟地址空间大小，单位通常是 KiB；
- `ru_maxrss`：进程峰值 RSS，Linux 上单位是 KiB。

`/proc/<pid>/maps` 中常见的行：

```
55f0... r-xp 00000000 fd:01 123456 /usr/bin/vmtest
7f2a... r--p 00000000 fd:01 789012 /data/index.bin
7f2b... rw-p 00000000 00:00 0          [anon]
7ffd... rw-p 00000000 00:00 0          [stack]
```

权限位 `r-xp`、`rw-p`、偏移、设备号、inode 和路径，能帮你判断某段地址是代码、文件映射、匿名堆还是栈。`[heap]`、`[stack]`、`[anon]`、`[vdso]` 等标签也很常见。

---

## 三、常见误区与设计权衡

### 3.1 误区：mmap 意味着没有 IO、没有拷贝成本

这是本节最需要记住的坑。

`mmap` 建立映射，不等于把文件读进内存，更不等于“零成本”：

1. **仍然有 IO**：首次访问对应页时，page cache 未命中就会发起磁盘读，表现为 major fault。文件很大时，逐页缺页甚至可能比一次大块 `read` 更慢。
2. **仍然有拷贝/复制成本**：文件页本身可以做到用户态直接访问 page cache，减少一次 `read` 的内核到用户拷贝；但 `MAP_PRIVATE` 的写会触发 COW 复制，`MAP_SHARED` 的脏页需要回写，匿名映射首次写要清零分配。
3. **仍有缺页和页表开销**：每个新页都要建立 PTE、可能触发 TLB 未命中；大量小页会带来可观的软缺页开销。
4. **仍受 page cache 与回收影响**：内存压力下页可能被回收，再次访问又缺页；`mlock` 能锁定但会占用不可换出内存。
5. **随机小读未必划算**：`read` 有内核预读，顺序读大文件时往往更高效；`mmap` 的优势更多在于随机访问和多次复用。

所以选型时要问：访问模式是顺序还是随机？文件多大？是否需要多进程共享？能否接受首次访问的缺页抖动？错误如何恢复？

### 3.2 权衡：映射方便随机访问，但错误恢复和驻留控制更难

`mmap` 的优点：

- 随机访问自然：像访问数组一样访问文件，不需要自己维护缓冲区和偏移。
- 多进程共享 page cache，减少重复内存。
- 避免显式 `read` 的系统调用和用户缓冲区管理。

代价：

- **错误恢复难**：普通 `read` 返回 `-1` 和 `errno`，调用方可以重试；`mmap` 访问出错可能直接是 `SIGBUS`/`SIGSEGV`，需要信号处理或额外校验，恢复路径复杂。
- **驻留控制难**：你很难精确控制哪些页常驻；`madvise(MADV_SEQUENTIAL/WILLNEED/DONTNEED)`、`mlock` 只是建议或限制，最终受内核回收策略影响。
- **生命周期耦合**：文件被截断、替换、卸载，映射可能失效；必须约定好更新协议。
- **可观测性更绕**：RSS 里混入共享文件页，排障时要结合 `smaps`、`maps` 判断。

一个实用的后台经验：只读、大、随机访问、更新不频繁的索引/词表适合 `mmap`；需要精细错误处理、流式顺序读、或文件会被频繁改写的场景，优先考虑 `read`/`pread` 加显式缓冲。

---

## 四、动手实践：映射文件并顺序/随机读取

目标：亲手观察 `mmap` 后 VIRT、RSS、minor/major faults 的变化，并复现文件截断后的风险。

### 步骤 1：准备一个足够大的测试文件

```bash
mkdir -p /tmp/vm-lab && cd /tmp/vm-lab

# 生成 256 MiB 文件；用 urandom 避免全是零页导致观察偏差
dd if=/dev/urandom of=index.bin bs=1M count=256 status=progress
ls -lh index.bin
```

说明：如果文件全是零，某些路径可能走共享零页，缺页表现不典型。用随机数据更接近真实索引文件。

### 步骤 2：编写并编译实验程序

把 2.3 节的最小示例保存到 `/tmp/vm-lab/vmtest.c`，然后：

```bash
cc -O2 -Wall -Wextra -o vmtest vmtest.c
```

### 步骤 3：冷缓存下观察顺序读取

```bash
sync
echo 3 | sudo tee /proc/sys/vm/drop_caches   # 需要 root；生产环境不要随便执行

/usr/bin/time -v ./vmtest index.bin
```

记录：

- `Minor (reclaiming a frame) page faults`
- `Major (requiring I/O) page faults`
- `Maximum resident set size`
- 程序自身打印的 `after mmap` / `sequential` / `random` 三组 `getrusage` 数据

预期：`after mmap` 时 RSS 和 faults 增长不明显；顺序读取后 RSS 明显上升，冷缓存下 major faults 增多；再次运行时由于 page cache 已热，major faults 明显减少。

### 步骤 4：热缓存下重复

```bash
/usr/bin/time -v ./vmtest index.bin
```

对比两次结果：同样的代码，major faults 可能从很多降到接近 0，说明“缺页成本”高度依赖 page cache 状态。

### 步骤 5：运行中观察 `/proc`

另开一个终端，让程序处理更大文件或加 `sleep`，然后：

```bash
pid=$(pgrep -n vmtest)
cat /proc/$pid/maps | grep -E 'index.bin|anon|stack'
grep -E 'VmSize|VmRSS|VmData|VmSwap' /proc/$pid/status
ps -o pid,min_flt,maj_flt,rss,vsz,cmd -p $pid
```

重点确认：

- `index.bin` 出现在 `maps` 中，权限通常是 `r--p` 或 `r--s`；
- `VmSize` 明显大于 `VmRSS`；
- `VmRSS` 随访问页数增加而增加，但不是一次性等于文件大小。

### 步骤 6：复现文件截断后的 SIGBUS

把 2.4 节的截断示例保存为 `trunc.c`，编译运行：

```bash
cc -O0 -g -Wall -o trunc trunc.c
./trunc; echo "exit=$?"
```

预期：进程可能因 `SIGBUS` 终止（shell 显示 `Bus error`，退出码通常为 135）。用 `dmesg` 或 `strace` 可进一步确认信号来源。

进一步实验：先映射并读一遍，再在另一个进程里 `truncate -s 0 data.bin`，然后继续访问，观察行为。理解“映射长度没变，但后备文件内容没了”这一风险。

### 步骤 7：记录实验结论

建议按下面模板记录，形成自己的排障笔记：

```
文件大小：
是否冷缓存：
after mmap:   min_flt=      maj_flt=      rss=
sequential:   min_flt=      maj_flt=      rss=
random:       min_flt=      maj_flt=      rss=
VmSize:       VmRSS:
截断实验现象：
结论：
```

### 附：一键验证脚本（我在 box 上实测通过）

如果没有 root、做不了 `drop_caches`，可以用 `fsync + posix_fadvise(POSIX_FADV_DONTNEED)` 把刚写的文件尽量踢出 page cache，再映射读取。课程附带的 `practice38/vm.c` + `test.sh` 做了这件事：写 64 MiB 文件 → 踢缓存 → mmap → 顺序读前一半 → `MADV_RANDOM` 后随机读后一半 2000 页 → 再顺序读一遍 → `ftruncate` 到 1 页后访问末尾 → 最后再测匿名映射。在 Linux 6.12 上的实测输出：

```
after_mmap VSZ+65536KB RSS+216KB          # VIRT 涨满 64MB，RSS 几乎不动
seq_half minflt=512 majflt=1 RSS+32988KB pages=8192   # 8192 页只缺页 512 次：fault-around + 预读
rand_2000 minflt=2 majflt=1764 RSS+40172KB            # 关掉预读后随机读，几乎每页一次 major fault
seq_again minflt=0 majflt=0                           # 已驻留且已建立 PTE，不再缺页
truncate_access SIGBUS                                # 截断后访问 EOF 之后的映射页
first_page_ok=x                                       # 仍在文件范围内的第一页照常可读
anon_256MB_untouched RSS+0KB                          # 匿名映射不碰就不占物理内存
anon_touch_32MB RSS+32768KB                           # 碰多少页就涨多少
```

`test.sh` 自动检查 6 项（VSZ 与 RSS 分离、顺序读缺页数远少于页数、随机读出现大量 major fault、重复访问不缺页、截断后 SIGBUS、匿名映射按需占用），全部通过时打印 `ALL_CHECKS_PASSED`。具体数字会随内核版本、文件系统和内存压力变化，看趋势即可。

运行：`cd practice38 && ./test.sh`

### 验收标准

- 能用自己的话区分 VIRT 与 RSS，并说明为什么 VIRT 很大不一定代表物理内存占用高。
- 能解释文件截断后继续访问映射的风险：地址仍合法，但后备页失效，可能触发 `SIGBUS`；更新文件应采用“写新文件 + 原子 rename”。
- 能根据 `/proc/<pid>/maps`、`/proc/<pid>/status`、`ps`、`/usr/bin/time -v` 和 `getrusage` 的数据，判断一次访问是 minor fault 还是 major fault。

---

## 五、自测题（附答案）

### 题 1：进程 VIRT 显示 8 GiB，RSS 只有 300 MiB，是否说明存在内存泄漏？

**答案**：不能。VIRT 是虚拟地址空间大小，包含尚未访问的匿名映射、文件映射、线程栈、分配器预留等；RSS 才是当前驻留的物理页。8 GiB VIRT 可能只是映射了一个大索引文件或预留了地址空间。判断泄漏应看 RSS/PSS 随时间的变化趋势、`/proc/<pid>/smaps` 的具体区间，以及业务负载是否同步变化。

### 题 2：`mmap` 一个文件后立刻 `munmap`，为什么 major faults 可能几乎为 0？

**答案**：`mmap` 通常只建立 VMA，不读取文件数据；没有访问就没有缺页。即使访问了，如果文件内容已在 page cache 中，缺页是 minor fault，不需要磁盘 IO。只有 page cache 未命中、需要从磁盘或远端存储加载时，才会产生 major fault。因此“映射了”不等于“读过了”，更不等于“发生了 IO”。

### 题 3：映射文件后，另一个进程把文件截断，继续读取映射为什么会触发 `SIGBUS` 而不是普通的读错误？

**答案**：映射的虚拟地址范围仍然存在，PTE 也可能仍指向原来的 page cache 页；但文件被截断后，超出新文件长度的部分不再有有效后备数据。内核在访问该页时发现后备对象无法提供这一页（或底层 IO 出错），于是发送 `SIGBUS`。它不是地址非法（那通常是 `SIGSEGV`），而是“映射的后备存储不可用”。后台系统应避免原地截断正在被映射的文件，改用写新文件再原子 `rename` 的发布方式。

---

## 六、推荐资源

- [虚拟内存、缺页与mmap：Linux man-pages](https://man7.org/linux/man-pages/man2/mmap.2.html)（man7.org，英文，文档，进阶，免费）

  重点阅读页表与 TLB、匿名和文件映射相关章节，并核对 `mmap` 的 flags、返回值、`MAP_SHARED`/`MAP_PRIVATE` 语义、`SIGBUS` 条件与错误处理路径。建议结合本节点实验，记录你使用的内核版本、文件系统类型和边界条件，因为不同版本、不同文件系统（如本地 ext4、网络文件系统）的行为可能不同。

---

## 七、小结

- 虚拟内存把“地址”与“物理页”解耦；页表负责翻译，TLB 负责加速。
- `mmap` 建立 VMA 与后备对象的映射，不直接复制文件；匿名映射与文件映射、共享与私有决定缺页和写回行为。
- 缺页可能分配、加载、COW 或报错；minor fault 不涉及 IO，major fault 需要 IO。
- VIRT 是地址空间，RSS 是驻留物理页；排障要结合 `maps`、`status`、`ps`、`time -v` 和 `getrusage`。
- `mmap` 不是免费的：仍有 IO、缺页、页表、COW 和回收成本；它擅长随机访问和共享，但错误恢复与驻留控制更难。

---

## 附录：practice38/vm.c 与 test.sh

`vm.c`：

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>

static long rss_kb(void){FILE*f=fopen("/proc/self/status","r");char l[256];long v=-1;
 while(fgets(l,sizeof l,f)) if(!strncmp(l,"VmRSS:",6)) sscanf(l+6,"%ld",&v); fclose(f);return v;}
static long vsz_kb(void){FILE*f=fopen("/proc/self/status","r");char l[256];long v=-1;
 while(fgets(l,sizeof l,f)) if(!strncmp(l,"VmSize:",7)) sscanf(l+7,"%ld",&v); fclose(f);return v;}
static void flt(long*mn,long*mj){struct rusage r;getrusage(RUSAGE_SELF,&r);*mn=r.ru_minflt;*mj=r.ru_majflt;}
static sigjmp_buf jb; static void onbus(int s){(void)s;siglongjmp(jb,1);}

int main(int argc,char**argv){
 const char*path=argc>1?argv[1]:"data.bin"; size_t sz=64UL<<20; long pg=sysconf(_SC_PAGESIZE);
 int fd=open(path,O_RDWR|O_CREAT|O_TRUNC,0644); char*buf=malloc(1<<20); memset(buf,'x',1<<20);
 for(int i=0;i<64;i++) if(write(fd,buf,1<<20)!=1<<20){perror("write");return 1;}
 fsync(fd); posix_fadvise(fd,0,0,POSIX_FADV_DONTNEED);  /* 尽量把页缓存踢掉 */
 long mn0,mj0,mn1,mj1; long r0=rss_kb(),v0=vsz_kb();
 char*p=mmap(NULL,sz,PROT_READ,MAP_SHARED,fd,0); if(p==MAP_FAILED){perror("mmap");return 1;}
 printf("after_mmap VSZ+%ldKB RSS+%ldKB\n",vsz_kb()-v0,rss_kb()-r0);
 flt(&mn0,&mj0); volatile long sum=0;
 for(size_t off=0;off<sz/2;off+=pg) sum+=p[off];          /* 顺序读前一半 */
 flt(&mn1,&mj1);
 printf("seq_half minflt=%ld majflt=%ld RSS+%ldKB pages=%zu\n",mn1-mn0,mj1-mj0,rss_kb()-r0,sz/2/pg);
 madvise(p,sz,MADV_RANDOM); srand(1); flt(&mn0,&mj0);
 for(int i=0;i<2000;i++){size_t off=(size_t)(sz/2+ (rand()%(sz/2/pg))*pg); sum+=p[off];} /* 随机读后一半 */
 flt(&mn1,&mj1);
 printf("rand_2000 minflt=%ld majflt=%ld RSS+%ldKB\n",mn1-mn0,mj1-mj0,rss_kb()-r0);
 flt(&mn0,&mj0); for(size_t off=0;off<sz/2;off+=pg) sum+=p[off]; flt(&mn1,&mj1);
 printf("seq_again minflt=%ld majflt=%ld\n",mn1-mn0,mj1-mj0);
 /* 截断后访问映射 -> SIGBUS */
 ftruncate(fd,pg); signal(SIGBUS,onbus);
 if(sigsetjmp(jb,1)==0){ sum+=p[sz-1]; printf("truncate_access no_signal\n"); }
 else printf("truncate_access SIGBUS\n");
 printf("first_page_ok=%c\n",p[0]);
 /* 匿名映射：只有触碰才占 RSS */
 long ra=rss_kb(); char*a=mmap(NULL,256UL<<20,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
 printf("anon_256MB_untouched RSS+%ldKB\n",rss_kb()-ra);
 for(size_t o=0;o<(32UL<<20);o+=pg) a[o]=1;
 printf("anon_touch_32MB RSS+%ldKB\n",rss_kb()-ra);
 munmap(a,256UL<<20); munmap(p,sz); close(fd); unlink(path); (void)sum; return 0;}
```

`test.sh`：

```bash
#!/usr/bin/env bash
# 第38课实践：mmap 文件顺序/随机读，记录 minor/major faults、VSZ/RSS，截断后 SIGBUS
set -u; cd "$(dirname "$0")"
gcc -O1 -w -o vm vm.c || exit 1
out=$(./vm ./data.bin); echo "$out"; fail=0
chk(){ if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
g(){ echo "$out" | grep "^$1" | grep -o "$2=[0-9]*" | cut -d= -f2; }
vsz=$(echo "$out"|grep after_mmap|sed 's/.*VSZ+\([0-9]*\)KB RSS+\([0-9]*\)KB/\1/')
rss=$(echo "$out"|grep after_mmap|sed 's/.*RSS+\([0-9]*\)KB/\1/')
chk "mmap 后 VSZ 涨 64MB 而 RSS 几乎不涨" '[ "$vsz" -ge 65536 ] && [ "$rss" -lt 4096 ]'
chk "顺序读缺页数远少于页数(fault-around/预读)" '[ "$(g seq_half minflt)" -lt 8192 ]'
chk "MADV_RANDOM 随机读出现大量 major fault" '[ "$(g rand_2000 majflt)" -gt 100 ]'
chk "再次顺序读已驻留页不再缺页" '[ "$(g seq_again minflt)" -eq 0 ] && [ "$(g seq_again majflt)" -eq 0 ]'
chk "截断后访问越界映射收到 SIGBUS" 'echo "$out" | grep -q "truncate_access SIGBUS"'
a0=$(echo "$out"|grep anon_256MB|grep -o '+[0-9]*'|tr -d +); a1=$(echo "$out"|grep anon_touch|grep -o '+[0-9]*'|tr -d +)
chk "匿名 256MB 未触碰不占 RSS，触碰 32MB 才涨约 32MB" '[ "$a0" -lt 1024 ] && [ "$a1" -ge 32000 ]'
[ $fail -eq 0 ] && echo ALL_CHECKS_PASSED
```
