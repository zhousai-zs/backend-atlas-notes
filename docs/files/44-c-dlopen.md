# 第 44/208 课：动态加载、懒绑定与可重入性

> 课程信息
>
> | 项目 | 内容 |
> | --- | --- |
> | 主题 | 动态加载、懒绑定与可重入性（Dynamic Linking） |
> | 领域 / 阶段 | C / 阶段 4 |
> | 子类 | 通信与运行时 |
> | 难度 | 深入 |
> | 前置 | c-elf、c-thread |
> | 标签 | Dynamic Linking、C、C++ |

上一课讲了 ELF、装载与符号：链接器与动态装载器如何把共享库映射进进程。本课往前一步——**运行时主动加载共享对象**，以及随之而来的**绑定时机**与**重入边界**。

`dlopen` 看起来只是“多加载一个 `.so`”，但首次解析可能牵动初始化器、全局锁、甚至 NSS/DNS 插件链。线程安全的接口，并不自动等于信号安全或协程安全。对微信公众号后台这类高并发服务来说，这些边界常常决定“偶发卡顿”还是“难复现死锁”。

---

## 学习目标

完成本课后，你应该能够：

1. 解释 `dlopen` / `dlsym` / `dlclose` 的语义、返回值与常见错误处理。
2. 对比 `RTLD_NOW` 与 `RTLD_LAZY`：何时解析符号、对启动时延与首次调用路径有何影响。
3. 说明共享库初始化器（`.init` / `.ctors` / `.init_array`）在装载路径中的位置，以及它们与锁、重入的关系。
4. 区分**线程安全**、**可重入**、**信号安全**、**协程安全**，并说明为什么不能互相直接推广。
5. 独立完成：写一个共享库插件，对比首次调用路径并记录解析阶段；设计**启动期预热**（含错误处理，且在调度器启动前执行），而不是把某次故障断言为通用根因。

验收标准：

- 能区分线程安全与可重入。
- 预热策略包含错误处理，并在调度启动前执行。

---

## 为什么后端要懂

### 场景一：插件热加载与配置扩展

微信公众号后台常有“按业务开关加载扩展模块”的需求：新消息类型的解析器、第三方回调适配器、灰度算法包。典型写法是：

```c
void *h = dlopen("./plugins/msg_filter.so", RTLD_NOW);
if (!h) { /* 记录 dlerror，拒绝启用该插件 */ }
typedef int (*filter_fn)(const char *, size_t);
filter_fn f = (filter_fn)dlsym(h, "filter_message");
```

如果发布后插件缺符号、依赖库版本不对、初始化器里访问了尚未就绪的全局状态，故障会在 `dlopen`/`dlsym` 当场暴露，也可能拖到第一次真正调用才崩。不懂这套 API，就只能在“重启就好了”里打转。

### 场景二：首次 DNS / 名字解析触发的特殊故障

线上更隐蔽的一类问题：服务跑了很久都正常，某条路径**第一次**调用 `getaddrinfo`（或间接走到 NSS）时突然卡顿、超时，甚至与锁相关的死锁。

直觉上你会想：“DNS 慢呗”。但在 Linux/glibc 环境里，名字服务还可能通过 NSS 机制**按需加载**插件模块。具体加载哪些模块、何时解析符号，**依赖 glibc 版本与 `/etc/nsswitch.conf` 等配置**，不能把某次机器上的现象直接写成“所有后台都这样”。

本课要建立的排查习惯是：

- 怀疑“首次调用路径”是否触发了动态装载或懒绑定；
- 用可控实验对比 `LAZY` / `NOW`、记录解析阶段；
- 用**启动期预热**降低首次风险，同时**不要**把预热当成对不安全重入的万能解药。

### 场景三：协程 / 信号上下文里的“看起来线程安全”

许多 glibc 接口文档会写 thread-safe。有人据此在信号处理器或用户态协程切换点调用它们，结果偶发崩溃或死锁。原因很简单：**线程安全 ≠ 可重入 ≠ 异步信号安全 ≠ 协程安全**。动态装载路径上往往持有进程级锁、跑初始化器，这些都是重入禁区。

---

## 0. 一张图建立直觉

```text
编译期链接（上一课）          运行时动态加载（本课）
─────────────────            ─────────────────
DT_NEEDED → 启动时装载         dlopen → 任意时刻装载
PLT/GOT 懒绑定（可配置）        RTLD_LAZY / RTLD_NOW
符号解析 + 重定位              dlsym 按名取地址
.init_array 等初始化器         装载成功路径上同样会跑

首次调用某外部符号
        │
        ├─ 已绑定 → 直接跳转
        └─ 未绑定 → 解析 / 可能装载依赖 / 可能跑初始化器
                     │
                     └─ 若此时持锁、在信号里、或协程半切换
                        → 卡顿、死锁、未定义行为风险上升
```

记住三句话：

1. **`dlopen` 决定“库何时进进程”**；
2. **`RTLD_*` 与懒绑定决定“符号何时解析”**；
3. **初始化器与装载器内部锁决定“哪些上下文不能碰这条路径”**。

---

## 1. dlopen / dlsym / dlclose

### 1.1 头文件与链接

```c
#define _GNU_SOURCE
#include <dlfcn.h>

/* 链接时需要 -ldl（部分工具链已把功能并入 libc，但仍建议显式 -ldl） */
```

### 1.2 dlopen：装载共享对象

```c
void *dlopen(const char *filename, int flags);
```

- `filename` 为 `NULL`：返回**主程序**的句柄（可用来查主程序导出符号，行为与标志组合有关，需查 man 页）。
- 路径可以是绝对路径，也可以是相对路径；相对路径的搜索规则与普通动态依赖不完全相同，生产环境优先用明确路径或已配置好的搜索路径。
- 成功返回非空句柄；失败返回 `NULL`，用 `dlerror()` 取可读错误信息。

**调用约定（极易踩坑）：**

```c
dlerror();                       /* 先清空旧错误 */
void *h = dlopen(path, flags);
const char *err = dlerror();
if (h == NULL) {
    /* err 可能为具体原因；务必打日志后返回错误，不要 assert 了事 */
}
```

`dlerror` 返回的是指向内部缓冲区的指针，下一次 `dl*` 调用可能覆盖它；需要持久化时先 `strdup`。

### 1.3 常用 flags

| 标志 | 含义（教学级摘要） |
| --- | --- |
| `RTLD_LAZY` | 尽量延迟解析函数符号，直到第一次被引用 |
| `RTLD_NOW` | 在 `dlopen` 返回前尽量完成可重定位符号的解析 |
| `RTLD_GLOBAL` | 后续装载的对象也能解析到本对象的符号 |
| `RTLD_LOCAL` | 符号不进入全局解析范围（常见默认倾向，以平台为准） |
| `RTLD_NODELETE` | `dlclose` 时不真正卸载（便于避免卸载后仍持有函数指针） |
| `RTLD_NOLOAD` | 若尚未装载则失败；常用于查询是否已在映射中 |

`LAZY` 与 `NOW` 是本课重点；`GLOBAL`/`LOCAL` 在插件系统里决定“插件 A 的符号会不会被插件 B 看见”，设计 ABI 时要显式选择。

### 1.4 dlsym：按名取符号

```c
void *dlsym(void *handle, const char *symbol);
```

```c
typedef int (*plugin_init_fn)(void);
plugin_init_fn init = (plugin_init_fn)dlsym(h, "plugin_init");
if (!init) {
    fprintf(stderr, "dlsym: %s\n", dlerror());
}
```

注意：

- C++ 符号默认 name mangling，插件接口通常要用 `extern "C"` 导出稳定名字。
- `dlsym` 成功时也可能返回“看起来像空指针”的地址（极少见但合法）。严格区分失败时，应先 `dlerror()` 清空，再 `dlsym`，再查 `dlerror()` 是否非空（man 页有标准写法）。

GNU 扩展还有 `dlvsym`（带版本）、`dlsym(RTLD_DEFAULT, ...)` / `RTLD_NEXT`（符号解析作用域），后台排查“到底绑到了哪个实现”时很有用。

### 1.5 dlclose：引用计数式关闭

```c
int dlclose(void *handle);  /* 成功返回 0 */
```

动态装载器对同一对象维护引用计数；`dlclose` 减引用，到零才可能卸载并跑析构/`.fini_array`。

实战建议：

- 若业务长期持有从该库取出的函数指针，**不要轻易 `dlclose`**，或使用 `RTLD_NODELETE`。
- 插件热更新若要卸载旧库，必须保证没有任何线程还在执行旧代码，也没有任何全局回调仍指向旧地址——这比 `dlopen` 本身难一个数量级。

### 1.6 最小可运行示例

```c
/* host.c */
#define _GNU_SOURCE
#include <stdio.h>
#include <dlfcn.h>

int main(void) {
    dlerror();
    void *h = dlopen("./libplugin.so", RTLD_NOW);
    if (!h) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 1;
    }
    int (*greet)(const char *) = dlsym(h, "greet");
    char *err = dlerror();
    if (err != NULL) {
        fprintf(stderr, "dlsym failed: %s\n", err);
        dlclose(h);
        return 1;
    }
    printf("greet -> %d\n", greet("wechat-mp"));
    dlclose(h);
    return 0;
}
```

```c
/* plugin.c */
#include <stdio.h>
int greet(const char *name) {
    printf("hello, %s\n", name);
    return 0;
}
```

```bash
gcc -fPIC -shared -o libplugin.so plugin.c
gcc -o host host.c -ldl
./host
```

---

## 2. RTLD_NOW 与 RTLD_LAZY：绑定策略

### 2.1 懒绑定在做什么

回顾上一课的 PLT/GOT：对外部函数的调用，可以在**第一次执行**时才向动态链接器请求解析，把真实地址写回 GOT，之后直接跳转。`RTLD_LAZY` 与进程默认的懒绑定策略一脉相承：把成本摊到首次调用。

`RTLD_NOW` 则要求：在 `dlopen` 成功返回前，把需要的重定位尽量做完。缺符号时，失败发生在装载阶段，而不是某个随机请求的热点路径上。

### 2.2 对比表

| 维度 | `RTLD_LAZY` | `RTLD_NOW` |
| --- | --- | --- |
| `dlopen` 耗时 | 通常更短 | 通常更长 |
| 缺符号暴露时机 | 可能迟到首次调用 | 尽量在 `dlopen` 时失败 |
| 首次业务调用 | 可能触发解析、装载依赖 | 解析成本已前置 |
| 适合 | 可选插件、快速启动试验 | 生产关键路径、希望 fail-fast |

可以用环境变量观察绑定过程（输出很吵，只在实验机用）：

```bash
LD_DEBUG=bindings,files ./host
# 或强制立即绑定整个进程：
LD_BIND_NOW=1 ./host
```

`LD_BIND_NOW=1` 影响的是进程级绑定策略，和某个 `dlopen(..., RTLD_NOW)` 相关但不完全等同；排查时要分清“进程默认”与“这次 dlopen 的 flags”。

### 2.3 和“首次 DNS”类故障的关系

当业务第一次走到名字解析时，可能叠加多层成本：

1. 函数尚未绑定 → 进入解析路径；
2. 解析或实现内部再触发其它共享对象装载；
3. 新对象的初始化器运行；
4. 初始化器或解析路径上获取进程级锁。

是否出现、以何种形式出现，**强烈依赖 glibc 版本与本机 NSS/解析配置**。正确态度是：

- 把“首次路径动态装载”当作**可疑因子**去验证；
- 用日志、`LD_DEBUG`、`/proc/<pid>/maps` 前后快照记录证据；
- **禁止**把某次故障写成“所有 getaddrinfo 都会 dlopen xxx”的万能结论。

---

## 3. 初始化器、锁与重入边界

### 3.1 初始化器何时跑

共享对象被装载并重定位后，动态链接器会执行其初始化逻辑，常见来源包括：

- `.init` / `.fini`（历史机制）
- `.init_array` / `.fini_array`（现代主力）
- C++ 全局对象构造函数（经 `.init_array` 等安排）
- 标记为 constructor 的函数：`__attribute__((constructor))`

卸载时则对应 destructor / `.fini_array`。

实验观察：

```c
/* plugin.c */
#include <stdio.h>
__attribute__((constructor))
static void on_load(void) {
    fprintf(stderr, "plugin constructor running\n");
}
__attribute__((destructor))
static void on_unload(void) {
    fprintf(stderr, "plugin destructor running\n");
}
int greet(const char *name) { return 0; }
```

```bash
gcc -fPIC -shared -o libplugin.so plugin.c
# dlopen 成功路径上应看到 constructor 输出
```

也可用：

```bash
readelf -d libplugin.so | grep -E 'INIT|FINI'
readelf -S libplugin.so | grep -E 'init|fini|ctor'
```

### 3.2 锁在哪里

动态装载器在管理链路列表、符号解析、初始化顺序时，需要**进程内同步**。具体实现随 glibc 版本演进，你不必背函数名，但要建立模型：

> 某线程正在 `dlopen` / 解析符号 / 跑初始化器时，其它线程若再进入装载或某些依赖装载的路径，可能阻塞在同一把（或同一组）锁上。

若初始化器自己又回调业务代码，业务代码再触发装载或同锁保护的操作，就形成**重入**或**锁顺序**问题。

### 3.3 重入边界：哪些上下文危险

危险程度从高到低（教学直觉，非形式化证明）：

1. **异步信号处理器**：几乎不能做带锁、带分配、带动态装载的事。
2. **持有业务锁时调用可能装载的路径**：易自死锁或锁顺序死锁。
3. **用户态协程在持有运行时锁时切出**：另一协程在同线程再进入装载路径 → 线程安全文档帮不上忙。
4. **普通多线程首次调用**：通常“合法但可能卡顿”；要用预热与 `RTLD_NOW` 管理风险，而不是假装没有首次成本。

---

## 4. 线程安全 ≠ 可重入 ≠ 信号 / 协程安全

### 4.1 四个概念

| 概念 | 直观含义 | 不保证什么 |
| --- | --- | --- |
| 线程安全 | 多线程并发调用该接口，不破坏其自身数据约束 | 不保证可在持锁/信号/半切换时调用 |
| 可重入 | 同一执行上下文未完成时再次进入仍正确（定义有多种口径） | 不等于“任意异步点可重入” |
| 异步信号安全 | async-signal-safe：信号处理器里可调用的集合很小 | 绝大多数库函数都不在此列 |
| 协程安全 | 在可能挂起的调度点之间，不依赖“线程独占”的隐式不变量 | 线程安全 API 常常**不是**协程安全 |

### 4.2 为什么不能直接推广

“函数 A 是 thread-safe”只说明：实现用锁或无锁算法保护了**它自己的共享状态**，使多个 OS 线程并发调用时行为符合文档。

它**没有**说明：

- 信号打断线程执行到一半时，再调 A 是否安全；
- 协程在 A 内部（或 A 调用的装载路径内部）挂起后，同线程另一协程再调 A 是否安全；
- A 的实现是否在内部调用了 `dlopen`、解析器、带全局锁的初始化逻辑。

动态装载相关路径尤其如此：锁的粒度往往是进程级的，持锁时间可能覆盖初始化器中的用户代码。

### 4.3 后台设计含义

对公众号消息处理线程池 / 协程池：

- **不要**在请求处理热路径上首次 `dlopen` 关键插件；启动阶段完成装载与解析。
- **不要**在信号处理器里做名字解析、日志落盘到复杂库、或任何可能分配/加锁的逻辑。
- **不要**假设“库声明 thread-safe，所以我在协程里随便挂起”——若必须在协程环境用可能阻塞装载的接口，应在文档与评审里显式讨论调度点。

---

## 5. 常见误区与设计权衡

### 5.1 误区

1. **把某次 DNS/NSS 故障泛化**  
   具体加载行为依赖 glibc 版本和配置。正确做法是对照目标环境做实验与源码阅读（如关注发行版对应的装载实现），而不是写进“通用根因”工单模板。

2. **`RTLD_LAZY` + 生产热路径首次调用**  
   缺符号、慢解析、初始化器副作用会打在峰值流量上。关键插件优先 `RTLD_NOW`，并在启动期调用一次空操作/探针函数完成绑定。

3. **`dlclose` 后仍调用旧函数指针**  
   表现为偶发 SIGSEGV，难以复现。热更新需要世代号、屏障、拒绝卸载或 `RTLD_NODELETE`。

4. **用线程安全证明信号/协程安全**  
   概念偷换，是死锁与内存破坏的高频来源。

5. **预热成功就断言“根因已消除”**  
   预热只是降低首次调用风险；若调度模型仍允许在持锁时进入装载路径，根因仍在。

### 5.2 权衡（对应节点 tradeoffs）

| 策略 | 收益 | 代价 / 残留风险 |
| --- | --- | --- |
| 启动期预热 | 把装载/解析/初始化前移，失败可阻断启动 | 启动变慢；不能修复不安全重入 |
| `RTLD_NOW` | fail-fast，热路径更稳 | `dlopen` 更慢；可选插件可能被过度绑定 |
| `RTLD_LAZY` | 启动快、按需付费 | 首次调用抖动；错误推迟 |
| 禁止热路径 `dlopen` | 行为可预测 | 降低“运行中任意加载”的灵活性 |
| 消除不安全调度重入 | 真正处理死锁类根因 | 需要改架构，成本高于加几行预热 |

**结论：** 预热降低首次调用风险，**仍需**消除不安全的调度重入。

---

## 6. 动手实践

工作目录建议：`/tmp/atlas-dlopen-lab`（或你的沙箱目录）。

### 6.1 步骤 A：写共享库插件与宿主

```bash
mkdir -p /tmp/atlas-dlopen-lab && cd /tmp/atlas-dlopen-lab
```

`plugin.c`：

```c
#include <stdio.h>
#include <unistd.h>

__attribute__((constructor))
static void on_load(void) {
    fprintf(stderr, "[plugin] constructor pid=%d\n", getpid());
}

int plugin_work(int x) {
    fprintf(stderr, "[plugin] work x=%d\n", x);
    return x + 1;
}

int plugin_need_sym(void); /* 故意在 LAZY 场景用到的外部依赖可另做实验 */

int plugin_entry(int x) {
    return plugin_work(x);
}
```

`host.c`：

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>
#include <time.h>

static long long nsec_now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "now";
    int flags = (strcmp(mode, "lazy") == 0) ? RTLD_LAZY : RTLD_NOW;

    dlerror();
    long long t0 = nsec_now();
    void *h = dlopen("./libplugin.so", flags);
    long long t1 = nsec_now();
    if (!h) {
        fprintf(stderr, "dlopen: %s\n", dlerror());
        return 1;
    }
    fprintf(stderr, "dlopen(%s) took %lld ns\n",
            (flags & RTLD_NOW) ? "NOW" : "LAZY", t1 - t0);

    dlerror();
    int (*entry)(int) = dlsym(h, "plugin_entry");
    if (dlerror() != NULL) {
        fprintf(stderr, "dlsym failed\n");
        return 1;
    }

    long long c0 = nsec_now();
    int r = entry(41);
    long long c1 = nsec_now();
    fprintf(stderr, "first call -> %d, took %lld ns\n", r, c1 - c0);

    long long c2 = nsec_now();
    r = entry(1);
    long long c3 = nsec_now();
    fprintf(stderr, "second call -> %d, took %lld ns\n", r, c3 - c2);

    dlclose(h);
    return 0;
}
```

编译运行：

```bash
gcc -fPIC -shared -o libplugin.so plugin.c
gcc -o host host.c -ldl
./host now
./host lazy
```

记录：

- `dlopen` 耗时（NOW vs LAZY）
- 第一次 / 第二次 `plugin_entry` 耗时
- constructor 打印出现的时机（相对 `dlopen` 返回）

可选加强：

```bash
LD_DEBUG=bindings,files ./host lazy 2> lazy.dbg
LD_DEBUG=bindings,files ./host now  2> now.dbg
# 对比两份调试输出中符号解析发生的阶段
```

在 `dlopen` 前后抓 maps：

```bash
# 在 host 里临时 raise(SIGSTOP) 或 getchar()，另开终端：
# cat /proc/$(pidof host)/maps | grep plugin
```

### 6.2 步骤 B：对比“缺符号”暴露时机（可选）

让插件引用一个故意不存在的符号，分别用 `LAZY`/`NOW` 观察是 `dlopen` 失败还是首次调用失败。注意：能否复现取决于链接方式与重定位类型；若本地链接触发了未定义引用错误，可改为在第二个 `.so` 里提供/删除符号，用 `DT_NEEDED` 组合实验。重点是**记录解析阶段**，而不是追求单一固定现象。

### 6.3 步骤 C：设计启动期预热（含错误处理，调度启动前）

下面是一个“公众号后台风格”的骨架：先预热，再启动线程池/协程调度。

```c
/* warmup.h */
#pragma once
int warmup_plugins(void); /* 0 成功，非 0 失败（已打日志） */

/* warmup.c 概念代码 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>

static void *g_filter;

int warmup_plugins(void) {
    dlerror();
    g_filter = dlopen("/opt/wechat/plugins/msg_filter.so", RTLD_NOW | RTLD_LOCAL);
    if (!g_filter) {
        fprintf(stderr, "FATAL warmup dlopen: %s\n", dlerror());
        return -1;
    }
    dlerror();
    int (*probe)(void) = dlsym(g_filter, "plugin_probe");
    if (dlerror() != NULL || probe == NULL) {
        fprintf(stderr, "FATAL warmup dlsym plugin_probe\n");
        return -1;
    }
    if (probe() != 0) {
        fprintf(stderr, "FATAL warmup probe failed\n");
        return -1;
    }
    /* 若还需触发名字解析等路径，在这里做一次受控调用，
       并明确：这只是预热，不能证明目标环境 NSS 行为的普遍结论 */
    return 0;
}

/* main 启动顺序 */
int main(void) {
    if (warmup_plugins() != 0)
        return 1;          /* 错误处理：失败则拒绝进入服务态 */
    /* start_thread_pool(); 或 start_coroutine_scheduler();
       必须在预热成功之后 */
    /* serve(); */
    return 0;
}
```

验收清单：

- [ ] 预热失败有明确错误处理（日志 + 非零退出 / 拒绝接流量）。
- [ ] 预热在调度器/线程池启动**之前**执行。
- [ ] 文档写明：预热不替代“消除不安全重入”。
- [ ] 对 DNS/NSS 相关预热，注明依赖发行版与配置，不写死为通用根因。

### 6.4 实践验收

- [ ] 能解释 `dlopen`/`dlsym`/`dlclose` 与 `dlerror` 用法。
- [ ] 能对比 `RTLD_NOW` 与 `RTLD_LAZY` 的首次调用路径差异并留下记录。
- [ ] 能说明 constructor 与装载的时序关系。
- [ ] 能区分线程安全与可重入（以及信号/协程维度）。
- [ ] 预热策略含错误处理且在调度启动前执行。

---

## 7. 自测题

### 题 1：API 语义

某插件系统使用 `dlopen(path, RTLD_LAZY | RTLD_LOCAL)` 成功，随后 `dlsym` 取得 `handle_event` 并缓存函数指针。运行一段时间后调用 `dlclose`，仍有工作线程可能执行 `handle_event`。可能出现什么问题？应如何改设计？

**答案：** `dlclose` 在引用计数归零后可能卸载映射，后续再跳进已卸载代码段会导致崩溃或未定义行为。改法包括：进程生命周期内不 `dlclose`；使用 `RTLD_NODELETE`；或做严格的停流量、等待 in-flight 请求结束、世代切换后再卸载。缓存函数指针不等于延长了库的生命周期，除非引用计数仍被其它 `dlopen` 持有。

### 题 2：NOW vs LAZY

为什么生产环境对“消息过滤关键插件”更倾向 `RTLD_NOW`，并对 `plugin_probe` 做启动期调用？这能否证明“线上所有首次 DNS 卡顿都是懒绑定造成的”？

**答案：** `RTLD_NOW` 把符号解析前置到装载期，缺符号时 fail-fast；启动期调用探针可进一步完成懒路径上残留绑定并执行初始化副作用，避免打在请求热路径。但这**不能**证明所有 DNS 卡顿都来自懒绑定：NSS/解析行为依赖 glibc 版本与配置，必须在目标环境取证，不能把单次故障泛化为通用根因。

### 题 3：线程安全与可重入

文档写明某函数 thread-safe。是否可以在该函数执行期间做用户态协程切换，并在同线程另一协程再次调用它？是否可以在信号处理函数里调用它？理由？

**答案：** 一般**不可以**据此推断安全。线程安全通常假设“以 OS 线程为并发单位”的同步；协程在持有内部锁或非原子不变量时切出，同线程重入可能破坏这些不变量，甚至与动态装载器锁交互导致死锁。异步信号上下文更严格，只有 async-signal-safe 函数可调用；thread-safe 不蕴含 async-signal-safe。动态装载/初始化路径尤其应避开信号与不安全的调度重入。

---

## 8. 推荐资源

- [动态加载、懒绑定与可重入性：Linux man-pages](https://man7.org/linux/man-pages/man3/dlopen.3.html)（man7.org，英文，文档，深入，免费）

  重点阅读 `dlopen` / `dlsym` / `dlclose` 与 `RTLD_NOW` / `RTLD_LAZY` 的语义、错误处理与标志组合，用于核对本课实验。学习时对照目标系统的 glibc/man 页版本；涉及 DNS/NSS 时单独追踪配置与实现，勿推定。

---

## 9. 本课小结

- `dlopen`/`dlsym`/`dlclose` 提供运行时装载与符号查找；务必正确使用 `dlerror`，并谨慎对待 `dlclose` 与函数指针生命周期。
- `RTLD_NOW` 倾向 fail-fast 与前置成本；`RTLD_LAZY` 把解析推到首次引用，可能造成热路径抖动。
- 初始化器在装载成功路径上执行，可能与装载器锁、业务回调纠缠，构成重入边界。
- 线程安全不能直接推广为可重入、信号安全或协程安全。
- 启动期预热（含错误处理、在调度启动前执行）能降低首次调用风险，但**不能**替代消除不安全的调度重入；DNS/NSS 相关现象不可随意泛化。
