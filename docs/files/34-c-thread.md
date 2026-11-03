# 第 34/208 课：Linux线程与线程资源

> 领域：C · 阶段 2 · 子类：进程、线程与调度 · 难度：基础
> 前置：c-process（进程、fork与exec）、b-lifetime（对象生命周期）
> 一句话：同一进程的线程共享地址空间和多种资源，但分别拥有执行栈与调度状态。线程之间必须为共享可变状态建立同步，而进程级资源也需协调释放。

## 一、学习目标与为什么要懂

学完这一课，你应该能：

1. 解释线程栈和 TLS（Thread-Local Storage，线程局部存储）是什么、放在哪、为什么"每线程一份"。
2. 正确使用 `pthread_create`、`pthread_join`、`pthread_detach`，知道各自谁负责回收。
3. 说清线程之间共享什么、不共享什么。
4. 独立完成节点实践：**创建受控数量线程，测量虚拟内存、RSS 和上下文切换**。

验收标准（来自节点）：

- 能说明**线程栈预留不等于全部驻留**；
- **退出时所有线程可回收**。

为什么后端要懂：节点给的理由是"**估算高并发服务的线程开销与共享边界**"。公众号后台这类服务通常是一个进程里跑着网络线程、工作线程池、日志线程等几十到上百个线程。你在监控里看到"虚拟内存 800 MB"会不会慌？线程池从 64 扩到 512 会多吃多少内存、多多少切换？一个 detach 出去的异步日志线程会不会读到已经销毁的请求对象？这些都靠本课的知识来判断。

## 二、从直觉到原理

### 2.1 直觉：线程 = 共享一套房子的多个住户

上一课讲过，进程是资源与隔离单位：一个进程有自己的地址空间、文件描述符表、信号处理方式等。线程则是**进程内的执行流**：多个线程住在同一套"房子"（地址空间）里，共用客厅和厨房（堆、全局变量、fd），但每人有自己的床（栈）和自己的小抽屉（TLS、寄存器、调度状态）。

在 Linux 上，线程由 `clone()` 创建，内核里每个线程都是一个可调度实体，有自己的 TID；同一进程的线程共用一个 PID（即线程组 ID）。用命令看一下：

```bash
# 看某进程的所有线程：LWP 列就是线程 ID
ps -L -o pid,lwp,nlwp,stat,comm -p <pid>
# 交互式按线程看 CPU
top -H -p <pid>
# 每个线程在 /proc 下有自己的目录
ls /proc/<pid>/task/
grep -E 'Threads|VmSize|VmRSS|ctxt' /proc/<pid>/status
```

### 2.2 要点一：线程栈和 TLS

**线程栈**：每个线程需要自己的栈来存放局部变量和函数调用帧。主线程的栈由内核在 exec 时建立；其他线程的栈由 glibc 的 `pthread_create` 用 `mmap` 预留出来，默认大小通常取自 `ulimit -s`（本机是 8192 KB，即 8 MB），栈尾还有一个保护页（guard page）用来捕获栈溢出。

关键事实：**`mmap` 预留的是虚拟地址范围，不是物理内存**。只有当线程真正写到某一页时，内核才在缺页异常里分配物理页。所以：

- `VmSize`（虚拟内存大小）≈ 线程数 × 栈大小，会显得很大；
- `VmRSS`（驻留物理内存）只包含真正被碰过的页，通常小得多。

本课实践里实测（100 个线程、每个栈 8 MB）：

| 场景 | VmSize | VmRSS |
|---|---|---|
| 创建线程前 | 2552 kB | 1576 kB |
| 100 线程、栈只用很少 | 822152 kB | 约 2.5 MB |
| 100 线程、每线程碰 1 MB 栈 | 822152 kB | 约 104 MB |
| 100 线程、栈设为 256 KB | 28552 kB | 约 2.5 MB |

结论很直接：VmSize 涨了约 800 MB，而 RSS 只多了不到 1 MB；只有真正用栈时，RSS 才按"用了多少"增长。

调整栈大小用线程属性：

```c
pthread_attr_t attr;
pthread_attr_init(&attr);
pthread_attr_setstacksize(&attr, 256 * 1024);   // 256 KB
pthread_create(&tid, &attr, worker, arg);
pthread_attr_destroy(&attr);
```

栈设太小的风险是递归深一点或局部大数组就溢出到保护页，进程直接 SIGSEGV。

**TLS**：有些数据需要"每个线程一份"，比如 `errno`、每线程的统计计数、每线程的缓冲区。C 里用 `__thread`（GCC 扩展）或 C11 的 `_Thread_local`：

```c
static __thread int tls_counter;   // 每个线程各有一份，互不影响，不需要加锁
static int shared_counter;         // 全进程只有一份，多线程写必须加锁
```

`errno` 能在多线程下正常工作，就是因为它是线程局部的。后台常见用法：每个工作线程用 TLS 存自己的请求计数，定时汇总，避免每次请求都去抢一把全局锁。

### 2.3 要点二：创建、join 与 detach

```c
#include <pthread.h>
#include <stdio.h>
#include <string.h>

static void *worker(void *arg) {
    long id = (long)arg;
    return (void *)(id * 10);          // 返回值交给 join 的人
}

int main(void) {
    pthread_t t;
    int rc = pthread_create(&t, NULL, worker, (void *)3L);
    if (rc != 0) {                      // 注意：返回错误码，不设置 errno
        fprintf(stderr, "pthread_create: %s\n", strerror(rc));
        return 1;
    }
    void *ret;
    pthread_join(t, &ret);              // 等它结束并回收资源
    printf("ret=%ld\n", (long)ret);
    return 0;
}
// 编译：gcc -pthread demo.c -o demo
```

三个要记住的规则：

1. **pthread 函数通过返回值报错**（比如线程太多时返回 `EAGAIN`），不是返回 -1 再设 errno，所以要用 `strerror(rc)`。
2. **线程默认是 joinable 的**：它结束后资源（栈、线程描述符）要等别人 `pthread_join` 才释放，类似上一课的僵尸进程。既不 join 也不 detach，线程就会一直占着资源。
3. **`pthread_detach`** 表示"我不关心它的返回值，结束后系统自动回收"。detach 之后就不能再 join 了。

所以"退出时所有线程可回收"的意思是：每个线程要么被 join，要么被 detach，二选一，不能漏。服务做优雅退出时更推荐 join：先通知各线程停止，再逐个 join，确保它们都真的结束了，再释放共享资源。

另外注意：主线程从 `main` return 相当于调用 `exit`，会直接结束整个进程，包括还在跑的 detached 线程。

### 2.4 要点三：线程与进程共享资源

同一进程内的线程：

| 共享 | 每线程独有 |
|---|---|
| 地址空间（代码、全局变量、堆、mmap 区） | 栈 |
| 文件描述符表 | 寄存器、程序计数器 |
| 信号处理方式（handler） | 信号掩码 |
| 当前工作目录、用户 ID 等进程属性 | TLS 变量、errno |
| PID | TID、调度状态与优先级 |

由此得出两条后台开发守则：

- **共享可变状态必须同步**。两个线程同时 `shared_counter++` 会丢失更新，要加互斥锁或用原子操作。下面实践里 100 个线程各加 1000 次，加锁后正好是 100000。
- **进程级资源要协调释放**。比如一个线程 `close(fd)` 了，另一个线程还在 `read(fd)`；更糟的是 fd 号码马上被复用给新连接，旧线程就读到了别人的数据。谁拥有、谁关闭，要事先约定。

上一课的误区也和这里相关：多线程进程 fork 后，子进程里只剩调用 fork 的那一个线程，别的线程持有的锁可能永远不会被释放。

## 三、常见误区与设计权衡

### 误区：detach 不会自动解决被引用对象的寿命

这是节点点名的坑。detach 只决定"线程结束后谁回收线程本身"，**不会延长线程用到的对象的寿命**。典型错误：

```c
struct req { char user[32]; };

static void *log_later(void *arg) {
    usleep(100 * 1000);                 // 模拟异步写日志
    struct req *r = arg;
    printf("user=%s\n", r->user);       // r 指向已经失效的栈对象
    return NULL;
}

static void handle(void) {
    struct req r; strcpy(r.user, "alice");
    pthread_t t;
    pthread_create(&t, NULL, log_later, &r);
    pthread_detach(t);                  // detach 了，但 r 的寿命没变
}                                       // handle 返回，r 就失效了
```

用 ASan 跑（`gcc -g -fsanitize=address -pthread`，并设 `ASAN_OPTIONS=detect_stack_use_after_return=1`）会报：

```
ERROR: AddressSanitizer: stack-use-after-return ... READ of size 6 ... thread T1
    #3 ... in log_later detach_bad.c:9
```

修法是让线程**拥有**它要用的数据：在堆上分配，交给线程，由线程用完后 `free`（C++ 里传 `shared_ptr` 或按值拷贝）。这和 b-lifetime 那课讲的悬垂引用是同一个问题，只是跨线程后更难发现，因为它依赖时序，测试时经常"碰巧没事"。

### 其他常见错误

- 看到 VmSize 几百 MB 就以为内存泄漏。先看 RSS，再看线程数乘以栈大小能不能解释。
- 忘了 join/detach，线程资源一直不释放；`ps -L` 能看到线程数只增不减。
- 用 `errno` 判断 `pthread_create` 失败，应该看返回值。

### 设计权衡

节点给的权衡是：**线程数增加可隐藏阻塞，也可能引入调度和锁竞争**。

- 好处：工作线程里有阻塞调用（同步 RPC、磁盘 IO）时，线程多一些，一个线程阻塞了别的还能继续干活，吞吐看起来更高。
- 代价：线程数远超 CPU 核数时，内核要频繁切换（可以看 `voluntary_ctxt_switches` / `nonvoluntary_ctxt_switches`）；大家抢同一把锁时，加线程只会让排队更长；每个线程还带着栈和 TLS 的内存开销。

实际做法：CPU 密集型任务线程数接近核数（`nproc`）；IO 阻塞多的任务可以多开一些，但要压测找拐点，或者把阻塞调用改成异步（后面讲 epoll 和协程时会继续展开）。

## 四、动手实践：受控数量线程 + 测量 VmSize、RSS、上下文切换

目标：写一个程序 `./threads <线程数> <栈KB> <每线程触碰栈KB>`，创建指定数量线程，在所有线程都活着时读取 `/proc/self/status`，再 join 全部线程，最后打印上下文切换次数。

**步骤 1：准备测量函数。** 从 `/proc/self/status` 读 `VmSize`、`VmRSS`、`Threads` 三行。

**步骤 2：用 barrier 让所有线程"同时活着"。** 主线程和 N 个工作线程都在 `pthread_barrier_wait(&ready)` 处汇合，主线程此时测量；然后再过一个 `go` 屏障让线程退出。

**步骤 3：控制栈大小和实际使用量。** 用 `pthread_attr_setstacksize` 设栈大小；在线程里开一个局部数组，每 4096 字节写一次，强制这些页驻留。

**步骤 4：TLS 与共享计数。** 每线程 `tls_counter++` 1000 次（不加锁），`shared_counter++` 加锁；检查每个线程返回的 TLS 值都是 1000，共享值等于 N×1000。

**步骤 5：join 全部线程并统计切换。** 用 `getrusage(RUSAGE_SELF)` 读 `ru_nvcsw`（自愿切换）和 `ru_nivcsw`（非自愿切换）。

参考实现（`practice34/threads.c`）：

```c
// 用法: ./threads <线程数> <每线程栈KB> <touchKB>
#define _GNU_SOURCE
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/resource.h>

static __thread int tls_counter;          // 每线程一份
static int shared_counter;                // 全进程一份
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_barrier_t ready, go;
static size_t touch_kb;

static void print_status(const char *tag) {
    FILE *f = fopen("/proc/self/status", "r"); char line[256];
    printf("[%s]", tag);
    while (fgets(line, sizeof line, f))
        if (!strncmp(line,"VmSize",6)||!strncmp(line,"VmRSS",5)||!strncmp(line,"Threads",7)) {
            line[strcspn(line,"\n")]=0; printf(" %s", line); }
    fclose(f); printf("\n");
}

static void *worker(void *arg) {
    (void)arg;
    volatile char buf[touch_kb ? touch_kb * 1024 : 1];
    for (size_t i = 0; i < sizeof buf; i += 4096) buf[i] = 1;   // 真正触碰栈页
    for (int i = 0; i < 1000; i++) {
        tls_counter++;
        pthread_mutex_lock(&mu); shared_counter++; pthread_mutex_unlock(&mu);
        if (i % 100 == 0) sched_yield();
    }
    pthread_barrier_wait(&ready);   // 等主线程测量
    pthread_barrier_wait(&go);
    return (void *)(long)tls_counter;
}

int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 100;
    size_t stack_kb = argc > 2 ? atol(argv[2]) : 8192;
    touch_kb = argc > 3 ? atol(argv[3]) : 0;
    pthread_t *t = calloc(n, sizeof *t);
    pthread_attr_t attr; pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, stack_kb * 1024);
    pthread_barrier_init(&ready, NULL, n + 1); pthread_barrier_init(&go, NULL, n + 1);
    print_status("before");
    for (long i = 0; i < n; i++) {
        int rc = pthread_create(&t[i], &attr, worker, (void *)i);
        if (rc) { fprintf(stderr, "pthread_create #%ld: %s\n", i, strerror(rc)); return 1; }
    }
    pthread_barrier_wait(&ready);
    print_status("running");
    pthread_barrier_wait(&go);
    long ok = 0;
    for (int i = 0; i < n; i++) { void *r; pthread_join(t[i], &r); if ((long)r == 1000) ok++; }
    struct rusage ru; getrusage(RUSAGE_SELF, &ru);
    print_status("joined");
    printf("tls_ok=%ld/%d shared=%d expected=%d vol_cs=%ld invol_cs=%ld\n",
           ok, n, shared_counter, n * 1000, ru.ru_nvcsw, ru.ru_nivcsw);
    pthread_attr_destroy(&attr); free(t); return 0;
}
```

**步骤 6：跑三组对比。**

```bash
gcc -O2 -pthread threads.c -o threads
./threads 100 8192 0      # 8MB 栈，几乎不用
./threads 100 8192 1024   # 8MB 栈，每线程用 1MB
./threads 100 256 0       # 256KB 栈
```

本机实测输出（8 核，切换次数每次运行会有波动）：

```
[before] VmSize: 2552 kB VmRSS: 1576 kB Threads: 1
[running] VmSize: 822152 kB VmRSS: 2472 kB Threads: 101
[joined] VmSize: 35336 kB VmRSS: 1832 kB Threads: 1
tls_ok=100/100 shared=100000 expected=100000 vol_cs=607 invol_cs=923

[running] VmSize: 822152 kB VmRSS: 104872 kB Threads: 101     # 每线程碰 1MB
[running] VmSize: 28552 kB VmRSS: 2496 kB Threads: 101        # 256KB 栈
```

怎么解读：

- **栈预留不等于驻留**：第一组 VmSize 约 800 MB（100 × 8 MB 加保护页），RSS 只有约 2.5 MB；第二组 VmSize 不变，RSS 涨到约 104 MB，正好约等于 100 × 1 MB。这条对应第一个验收点。
- **退出时所有线程可回收**：join 之后 `Threads: 1`，只剩主线程，对应第二个验收点。join 后 VmSize 还剩约 35 MB 而不是回到 2.5 MB，是 glibc 为线程分配的 malloc arena 等没有还给系统，属于正常现象。
- **上下文切换**：`sched_yield` 和抢锁都会引起切换。可以把线程数改成 8、100、1000 对比 `vol_cs`/`invol_cs` 的变化，体会"线程多了调度开销也多"。

**步骤 7：观察运行中的进程（可选）。** 在 worker 里加一个 `sleep(30)`，另开终端：

```bash
ps -L -o pid,lwp,stat,comm -p $(pgrep threads) | head
grep -E 'Threads|VmSize|VmRSS|ctxt' /proc/$(pgrep threads)/status
pmap -x $(pgrep threads) | sort -k2 -n | tail -5   # 能看到大量 8MB 级别的 mapping
```

**步骤 8：复现 detach 误区。** 用第三节的 `detach_bad.c` 配 ASan 看到 `stack-use-after-return`，再改成堆分配、由线程 free 的版本，确认不再报错。

一键自测脚本见附录 `practice34/test.sh`，本机运行结果为 `ALL_CHECKS_PASSED`。

## 五、自测题

**1. 某服务 `top` 里 VIRT 显示 2 GB，RES 只有 150 MB，进程有 200 个线程。同事说"内存泄漏了"，你怎么判断？**

<details><summary>答案</summary>
先算线程栈预留：200 × 8 MB（默认栈）≈ 1.6 GB，这部分只是 mmap 预留的虚拟地址，没被写过的页不占物理内存，所以 VIRT 大是正常的。判断泄漏要看 RSS 是否随时间持续上涨，而不是看 VIRT。若想降低 VIRT，可以用 pthread_attr_setstacksize 减小栈，但要确保不会栈溢出。
</details>

**2. 线程既没有 join 也没有 detach，结束后会怎样？join 和 detach 怎么选？**

<details><summary>答案</summary>
它的栈和线程描述符不会释放，一直占着资源，类似僵尸进程。需要拿返回值、或者需要确认它确实结束了（比如优雅退出时要先等所有工作线程停下再释放共享资源）时用 join；真正"发出去就不管"且不依赖外部对象寿命的线程才 detach。两者只能选一个，detach 后不能再 join。
</details>

**3. 下面代码有什么问题？怎么修？**

```c
void on_request(void) {
    char user[32] = "alice";
    pthread_t t;
    pthread_create(&t, NULL, async_log, user);
    pthread_detach(t);
}
```

<details><summary>答案</summary>
user 是 on_request 的栈变量，函数返回后就失效了，而 detach 出去的 async_log 可能稍后才读它，读到的是已经失效的内存（ASan 会报 stack-use-after-return）。detach 不延长对象寿命。修法：在堆上分配一份拷贝传给线程，由线程用完后 free（C++ 里按值捕获或用 shared_ptr）；或者改为 join，保证线程在 user 失效前结束。
</details>

## 六、推荐资源

- [Linux线程与线程资源：Linux man-pages（pthreads(7)）](https://man7.org/linux/man-pages/man7/pthreads.7.html)（man7.org，英文文档，进阶，免费）：重点阅读线程栈和 TLS 与创建、join 与 detach，用于核对本节点实验的语义和边界。

延伸阅读建议：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件（比如 `pthread_create` 返回 `EAGAIN` 的条件、默认栈大小从哪来）。

## 附录：practice34/test.sh

```bash
#!/bin/bash
# 第34课实践自测：线程栈预留 vs 驻留、TLS、join 回收、detach 寿命
# 同目录需有 threads.c、detach_bad.c、detach_ok.c（见正文）
# 第34课实践自测：线程栈预留 vs 驻留、TLS、join 回收、detach 寿命
set -u; cd "$(dirname "$0")"; fail=0
gcc -O2 -pthread threads.c -o threads || exit 1
gcc -g -O0 -fsanitize=address -pthread detach_bad.c -o detach_bad || exit 1
gcc -g -O0 -fsanitize=address -pthread detach_ok.c -o detach_ok || exit 1
kb(){ echo "$1" | grep "\[$2\]" | sed -E "s/.*$3:[[:space:]]*([0-9]+) kB.*/\1/"; }
a=$(./threads 100 8192 0); echo "$a"
b=$(./threads 100 8192 1024); echo "$b"
vs=$(kb "$a" running VmSize); rss=$(kb "$a" running VmRSS); rss2=$(kb "$b" running VmRSS)
[ "$vs" -gt 800000 ] && [ "$rss" -lt 20000 ] && echo "OK 栈预留(VmSize ${vs}kB) 远大于驻留(RSS ${rss}kB)" || { echo FAIL1; fail=1; }
[ "$rss2" -gt 100000 ] && echo "OK 每线程触碰1MB后 RSS=${rss2}kB" || { echo FAIL2; fail=1; }
echo "$a" | grep -q "tls_ok=100/100 shared=100000 expected=100000" && echo "OK TLS 各自计数、共享计数加锁正确" || { echo FAIL3; fail=1; }
echo "$a" | grep "\[joined\]" | grep -q "Threads:	1" && echo "OK join 后只剩主线程" || { echo FAIL4; fail=1; }
ASAN_OPTIONS=detect_stack_use_after_return=1 ./detach_bad 2>&1 | grep -q stack-use-after-return && echo "OK detach 反例被 ASan 检出 stack-use-after-return" || { echo FAIL5; fail=1; }
ASAN_OPTIONS=detect_stack_use_after_return=1 ./detach_ok | grep -q user=alice && echo "OK 堆对象交给线程拥有后正常" || { echo FAIL6; fail=1; }
[ $fail = 0 ] && echo ALL_CHECKS_PASSED
```
