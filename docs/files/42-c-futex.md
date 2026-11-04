# 第 42/208 课：futex与用户态同步慢路径

> 领域：C ｜ 阶段：3 ｜ 难度：进阶 ｜ 子类：进程、线程与调度
> 前置：`c-thread`（线程）、`c-syscall`（系统调用）

**一句话**：futex 让锁在**无竞争时完全在用户态**靠原子操作完成，只有**发生竞争时**才进内核，由内核按“用户内存地址”把线程挂起和唤醒。

---

## 1. 学习目标 & 为什么后端要懂

学完本课你应该能：

- 解释**用户态快路径**：为什么 `pthread_mutex_lock` 大多数时候根本不进内核；
- 独立完成：**观察互斥锁在低竞争和高竞争时的 futex 调用，并画出等待时序**；
- 说明“**先检查再睡眠**”会丢唤醒，以及 futex 的“比较并等待”怎样堵住这个窗口；
- 明白 **futex 只是积木，不是一把完整的锁**。

**为什么后端要懂**：节点给出的核心问题是——*互斥锁为何既需要原子操作又需要系统调用？*

- 只用原子操作（纯自旋）：抢不到锁就空转烧 CPU，临界区一长就浪费整核。
- 每次都走系统调用：哪怕没人竞争也要陷入内核，开销大。
- futex = 两者结合：**无竞争走原子操作（快），有竞争才系统调用睡眠（省 CPU）**。

后台场景：公众号后台某服务用一把全局锁保护连接池或计数器。QPS 低时一切正常；大促流量上来后 p99 抖动，`strace -c` 一看 `futex` 调用量和耗时冲到第一。这就是锁从“快路径”掉进了“慢路径”。看懂 futex，你才能判断这是锁竞争问题，而不是“系统调用太慢”。

---

## 2. 核心要点：从直觉到原理

### 2.1 用户态快路径

**直觉**：大部分时间锁没人抢。既然没人抢，何必麻烦内核？在用户态用一条原子指令（CAS）把锁变量从 0 改成 1，成功就拿到锁了。

**原理**：锁就是一个普通的 32 位整数，放在用户内存里（可以是进程私有的，也可以在共享内存里）。

- 加锁：`CAS(&lock, 0, 1)` 成功 → 拿到锁，**零系统调用**。
- 解锁：如果确定没人在等，直接写 0，**零系统调用**。
- 只有 CAS 失败（有人持锁）时才需要考虑“睡眠”，这时候才需要内核——**慢路径**。

glibc 的 `pthread_mutex_t` 底层就是这样：无竞争的 lock/unlock 用 `strace` 是看不到任何 `futex` 调用的。

### 2.2 比较并等待（FUTEX_WAIT）

**直觉**：抢不到锁，想睡觉等别人叫醒。朴素写法：

```c
while (lock != 0) {      // ① 检查：锁被占用
    sleep_until_woken(); // ② 睡眠
}
```

问题在 ① 和 ② 之间：如果持锁者恰好在这之间释放了锁并发出“唤醒”，此时你还没睡下，唤醒就**丢了**；接着你睡下去，可能再也没人来叫你。这就是**先检查再睡眠的丢唤醒风险**（lost wake-up）。

**原理**：futex 把“检查”和“睡眠”交给内核**原子地**完成：

```c
syscall(SYS_futex, uaddr, FUTEX_WAIT, val, timeout, NULL, 0);
```

语义：内核在把调用者挂到等待队列之前，检查 `*uaddr` 是否**仍等于** `val`：

- 相等 → 挂起，直到被 `FUTEX_WAKE` 唤醒（或超时、被信号打断）；
- 不相等 → 立即返回，`errno = EAGAIN`，意思是“值已经变了，你回去重新看看”。

这个检查与入队相对于同一地址上的 `FUTEX_WAKE` 是原子的，所以“我看到锁被占→我睡下”之间不会漏掉唤醒：要么值已经变了（直接返回），要么我已经在队列里（唤醒能找到我）。

内核用**用户内存地址**作为等待队列的键（私有 futex 用“地址空间+虚拟地址”，共享的用底层页面），内核里并不存在一个“锁对象”——锁的状态始终在用户内存里。

### 2.3 唤醒、竞态与虚假唤醒（FUTEX_WAKE）

```c
syscall(SYS_futex, uaddr, FUTEX_WAKE, n, NULL, NULL, 0); // 最多唤醒 n 个等待者
```

需要牢记三点：

1. **唤醒 ≠ 拿到锁**：被唤醒只是“你可以再试一次了”。醒来的线程要重新 CAS，可能被刚到的新线程抢先（插队）。
2. **唤醒 ≠ 立即运行**：被唤醒的线程只是变成可运行，什么时候上 CPU 由调度器决定（回顾第 41 课）。
3. **虚假唤醒**：`FUTEX_WAIT` 可能因为信号（`EINTR`）、超时、或者别的原因返回，而条件并未满足。所以**等待永远要写在循环里，醒来重新检查条件**。

### 2.4 一个简化的三状态锁（教学示例）

参考 Ulrich Drepper 论文《Futexes Are Tricky》中的思路。状态：`0` = 未锁，`1` = 已锁无等待者，`2` = 已锁且可能有等待者。

```c
// futex_lock.c —— 教学用简化锁，不是生产级实现
#define _GNU_SOURCE
#include <linux/futex.h>
#include <stdatomic.h>
#include <sys/syscall.h>
#include <unistd.h>

static long futex(atomic_int *uaddr, int op, int val) {
    return syscall(SYS_futex, uaddr, op, val, NULL, NULL, 0);
}

void lock(atomic_int *m) {
    int c = 0;
    // 快路径：0 -> 1，成功就返回，不进内核
    if (atomic_compare_exchange_strong(m, &c, 1))
        return;
    // 慢路径：标记为 2（有等待者），然后睡
    if (c != 2)
        c = atomic_exchange(m, 2);
    while (c != 0) {
        // 只有当 *m 仍为 2 时才睡；否则 EAGAIN 立即返回再试
        futex(m, FUTEX_WAIT_PRIVATE, 2);
        c = atomic_exchange(m, 2);   // 醒来后重新抢（可能被插队）
    }
}

void unlock(atomic_int *m) {
    // 原值为 1：没人等，直接返回（快路径，无系统调用）
    if (atomic_fetch_sub(m, 1) != 1) {
        // 原值为 2：可能有人在等
        atomic_store(m, 0);
        futex(m, FUTEX_WAKE_PRIVATE, 1);
    }
}
```

要点：

- `lock` 的快路径只是一次 CAS；`unlock` 在状态为 1 时也不进内核。
- 醒来后用 `exchange(m, 2)` 而不是 `1`：因为不知道是否还有其他等待者，保守地标 2，代价是偶尔多一次无用的 `FUTEX_WAKE`。
- `FUTEX_PRIVATE_FLAG`（`*_PRIVATE`）告诉内核这个 futex 只在本进程内使用，内核可以省去跨进程查找共享页的开销。glibc 对进程内 mutex 默认使用私有 futex。

**为什么说它不是完整的锁？** 它没有：错误检查与递归锁、持有者记录、健壮锁（持有者崩溃后的恢复，`robust futex`）、优先级继承（`FUTEX_LOCK_PI`）、超时、公平性、自适应自旋……这些都是 glibc `pthread_mutex` 在 futex 之上额外做的。**futex 只提供“按地址睡/醒”的原语**，正确的锁协议得由用户态代码设计，而且很容易写错——论文标题就叫“Futexes Are Tricky”。

---

## 3. Linux 观测命令

```bash
# 只跟踪 futex 系统调用（-f 跟踪所有线程，-T 显示每次耗时，-tt 时间戳）
strace -f -tt -T -e trace=futex ./bench 4

# 统计各系统调用次数与耗时，看 futex 是否占大头
strace -f -c ./bench 16

# 对运行中的服务挂上去看 10 秒（注意 strace 本身开销很大，线上慎用）
timeout 10 strace -f -c -p <pid>

# 开销更低的替代：perf trace
perf trace -e futex -p <pid> -- sleep 5
perf trace -s ./bench 16        # 按线程汇总系统调用

# 上下文切换：高竞争时自愿切换（睡在 futex 上）显著增多
perf stat -e context-switches,cpu-migrations ./bench 16
grep ctxt_switches /proc/<pid>/status
cat /proc/<pid>/task/*/status | grep voluntary
```

`strace` 输出里常见形态（示意）：

```
futex(0x5581..., FUTEX_WAIT_PRIVATE, 2, NULL) = 0          <- 睡下后被唤醒
futex(0x5581..., FUTEX_WAIT_PRIVATE, 2, NULL) = -1 EAGAIN  <- 值已变，没睡就返回
futex(0x5581..., FUTEX_WAKE_PRIVATE, 1) = 1                <- 唤醒了 1 个等待者
futex(0x5581..., FUTEX_WAKE_PRIVATE, 1) = 0                <- 没人在等（多余的唤醒）
```

`EAGAIN` 正是“比较并等待”在起作用：检查发现值已变，拒绝睡眠，避免了丢唤醒。

---

## 4. 常见误区

| 误区 | 正确理解 |
|---|---|
| 看到 `futex` 调用就说是“系统调用慢” | futex 出现意味着**发生了竞争**；根因通常是锁粒度太粗或临界区太长 |
| `FUTEX_WAKE` 返回 1 就说明那个线程拿到锁了 | **唤醒不代表调用者立即获得锁或立即运行**，它还要被调度、再去抢 |
| `FUTEX_WAIT` 返回 0 就说明条件满足 | 可能是虚假唤醒；必须在循环里重新检查 |
| futex 就是一把锁 | futex 只是“按地址睡/醒”的原语，**不是完整锁实现** |
| 先读一下变量，看到被占再调用 sleep 就行 | 先检查再睡眠存在丢唤醒窗口，要靠 FUTEX_WAIT 的原子比较 |

---

## 5. 设计权衡：自旋还是休眠？

节点给出的权衡：**自旋与休眠的切换点依赖临界区长度和负载**。

- **临界区很短**（几十纳秒，比如加个计数）：持锁者马上就会释放，自旋几圈往往比“睡下 + 被唤醒 + 重新调度”（涉及两次系统调用和上下文切换）更划算。
- **临界区长或持锁者可能被换出**：自旋就是白烧 CPU，应该尽快睡。
- **负载高、CPU 已满**：自旋会抢走持锁者需要的 CPU，反而更慢。

所以常见做法是“**先自旋一小会儿，再休眠**”，例如 glibc 的 `PTHREAD_MUTEX_ADAPTIVE_NP` 自适应互斥锁。但最根本的手段往往不是调锁，而是**减少竞争**：分片锁、每线程本地计数后再汇总、无锁队列、把慢操作（IO、日志）移出临界区。

---

## 6. 动手实践：观察低竞争与高竞争的 futex 调用

**目标**：观察互斥锁低竞争和高竞争时的 futex 调用，绘制等待时序。

**步骤 1：写压测程序** `bench.c`：N 个线程各自对一个全局计数器加锁自增 `ITER` 次，临界区可选加一段空循环来模拟“长临界区”。

```c
// gcc -O2 -pthread bench.c -o bench
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static long counter;
static int ITER = 200000, WORK = 0;

static void *worker(void *arg) {
    for (int i = 0; i < ITER; i++) {
        pthread_mutex_lock(&mu);
        counter++;
        for (volatile int k = 0; k < WORK; k++) ;   // 模拟临界区长度
        pthread_mutex_unlock(&mu);
    }
    return NULL;
}

int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 1;
    if (argc > 2) WORK = atoi(argv[2]);
    pthread_t t[n];
    for (int i = 0; i < n; i++) pthread_create(&t[i], NULL, worker, NULL);
    for (int i = 0; i < n; i++) pthread_join(t[i], NULL);
    printf("threads=%d counter=%ld\n", n, counter);
}
```

**步骤 2：低竞争基线**（单线程）

```bash
strace -f -c -e trace=futex ./bench 1
```

预期：几乎没有 `futex` 调用——这就是用户态快路径。

**步骤 3：逐步加大竞争**

```bash
for n in 2 4 8 16; do
  echo "== $n threads"; strace -f -c -e trace=futex ./bench $n 2>&1 | tail -4
done
# 再加长临界区对比
strace -f -c -e trace=futex ./bench 8 200
```

记录每组的 futex 调用次数、错误数（EAGAIN）、总耗时。

**步骤 4：记录时序**

```bash
strace -f -tt -T -e trace=futex -o trace.txt ./bench 4 200
grep -E 'WAIT|WAKE' trace.txt | head -50
```

**步骤 5：绘制等待时序**。从 `trace.txt` 里挑 2 个线程（按 tid），在时间轴上标出：`FUTEX_WAIT` 开始 → 返回（睡了多久，看 `<耗时>`）、另一个线程何时发 `FUTEX_WAKE`、被唤醒线程返回后又过多久再次 WAIT。手画或用 Python/matplotlib 画甘特图都行。重点观察：WAKE 之后，被唤醒的线程**不一定马上**拿到锁，可能紧接着又 WAIT（被插队了）。

**步骤 6：交叉验证**

```bash
perf stat -e context-switches ./bench 1
perf stat -e context-switches ./bench 16
```

上下文切换次数应该随竞争明显上升。

**验收标准**：

- [ ] 能说明先检查再睡眠的丢唤醒风险（并能在 trace 中指出 EAGAIN 的含义）；
- [ ] 不把 futex 当完整锁实现（能列出 pthread_mutex 在 futex 之上多做了哪些事）。

> 注意：strace 会大幅拖慢程序、改变时序，适合定性观察；要测真实性能用 `perf stat` / `perf trace`。

---

## 7. 自测题

**Q1**：单线程程序反复 `pthread_mutex_lock/unlock` 一百万次，`strace -c` 里能看到多少次 `futex`？为什么？

<details><summary>答案</summary>
基本为 0。没有竞争时加锁/解锁只是用户态的原子操作（快路径），无需进入内核；只有加锁失败需要睡眠，或解锁时发现有等待者需要唤醒，才会调用 futex。
</details>

**Q2**：`FUTEX_WAIT` 为什么要传一个期望值 `val`？如果去掉它会发生什么？

<details><summary>答案</summary>
内核在挂起线程前原子地检查 `*uaddr == val`，不等就返回 EAGAIN。这样“检查锁被占”与“睡下”之间不会漏掉别人的释放和唤醒。如果没有这个比较，线程在用户态看到锁被占后、真正睡下前，持锁者可能已经释放并发出唤醒，唤醒就丢了，等待者可能永久睡眠（丢唤醒）。
</details>

**Q3**：线上 strace 看到 `futex(..., FUTEX_WAKE_PRIVATE, 1) = 1`，同事说“说明等待的请求线程已经拿到锁开始处理了”。对吗？

<details><summary>答案</summary>
不对。返回 1 只说明唤醒了 1 个等待者，让它变成可运行。它还要等调度器给它 CPU，醒来后还要重新抢锁，可能被新来的线程插队后再次睡眠。唤醒不代表立即获得锁，也不代表立即运行。
</details>

---

## 8. 推荐资源

- [futex(2) — Linux man-pages](https://man7.org/linux/man-pages/man2/futex.2.html)（man7.org，英文文档，进阶，免费）：重点阅读用户态快路径与比较并等待，用于核对本节点实验的语义和边界。

**源码研读建议**：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件（例如 `EAGAIN`、`EINTR`、`ETIMEDOUT` 各在什么情况下返回）。
