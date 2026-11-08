# 第 79/208 课：竞态检测与并发故障复现

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 任务生命周期与诊断 |
| 前置 | e-race、e-lock |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | Race Detection、E、C++ |
| 子分类 | 任务生命周期与诊断 |
| 节点 ID | e-tsan |
| 英文名 | Race Detection |

> 承接说明：前置节点 e-race（数据竞争与线程安全契约，第 65 课）讲了什么是数据竞争、它为什么在 C++ 里是未定义行为，以及线程安全契约——谁负责保护哪块共享状态。前置节点 e-lock（互斥锁与锁粒度，第 66 课）讲了如何用互斥锁建立 happens-before，以及锁粒度怎样同时影响正确性与性能。本课往前走一步：当线上已经出现“偶发错乱、难复现”的并发故障时，如何用动态竞态检测（ThreadSanitizer，简称 TSan）把随机问题变成带两侧访问栈的可重现证据。本节点 summary 是：动态竞态检测观察实际执行的内存访问与同步关系，能发现许多数据竞争，但不能证明所有并发行为正确。

---

## 导读

先看本节点的 summary，后面所有内容都围绕它展开：

> 动态竞态检测观察实际执行的内存访问与同步关系，能发现许多数据竞争，但不能证明所有并发行为正确。

这句话分成两半，每一半都值得停一下。

前半句说的是**它怎么工作**：动态检测不是读你的代码做静态推理，而是让程序真的跑起来，在运行过程中记录每一次普通内存访问、每一次加锁解锁、每一次原子操作，再根据这些记录判断两个访问之间有没有 happens-before 关系。它看到的是“这一次执行”里真实发生过的访问，所以报告通常非常具体：哪个地址、哪个变量、哪个线程读的、哪个线程写的、各自的调用栈是什么。

后半句说的是**它的边界**：既然只看实际执行过的路径，那么没被执行到的分支、没被触发的调度、没被访问到的对象，它都无从判断。一次运行没有告警，只能说明“这次执行没有观察到数据竞争”，不能说明“这个程序并发正确”。这句话和第 65 课“数据竞争是 UB、但 UB 不保证每次崩溃”是同一枚硬币的两面：没有证据不等于没有缺陷。

本课沿着三个节点要点推进：

- **TSan 构建与开销**：它是编译期插桩加运行时库，怎么编译、报告长什么样、慢多少、内存涨多少、有哪些平台与链接限制、`TSAN_OPTIONS` 里哪些选项最常用。
- **最小复现和调度扰动**：怎样把“线上偶发”抽成一个几十行、能反复跑、能稳定打出两侧栈的程序；怎样用 `yield` / `sleep_for` 和 Adaptive Delay 增大交错窗口。
- **数据竞争与逻辑竞争区别**：TSan 的主业是前者；后者即使每次内存访问都有锁，业务仍然可能错，而 TSan 会保持沉默。

三个要点最终服务于同一个目标，也就是本节点的 why：**把随机线上问题转成可重现证据。**

---

## 学习目标

### goals

- 能解释 TSan 构建与开销。
- 能独立完成：对故意错误的缓存并发更新运行 TSan，修复后加入稳定回归用例。

### acceptance

- 报告含两侧访问栈与共享对象。
- 不把无告警当绝对正确。

### 把验收翻译成可检查产物

“能解释”和“能独立完成”都太容易被自我感觉糊弄过去。把两条 acceptance 翻译成看得见、摸得着的东西：

- **“报告含两侧访问栈与共享对象”**：你手里有一份真实的 TSan 输出，里面至少能找到四样东西：`WARNING: ThreadSanitizer: data race`；一侧访问（例如 `Write of size 8 ... by thread T3`）及其调用栈；另一侧访问（`Previous write ... by thread T1`）及其调用栈；共享对象的地址（`at 0x...` 或 `Location is ...`）。你还能指出这个地址对应源代码里的哪个字段——是 `Article::views`，还是 `unordered_map` 的某个节点。只截一张“有 WARNING”的图不算完成。
- **“不把无告警当绝对正确”**：修复之后重新运行，退出码为 0；但你在文档或代码注释里明确写下这次检测覆盖了什么路径、没覆盖什么路径，并保留至少一条 TSan 抓不到的逻辑竞争对照实验。也就是说，你既证明了“这一类数据竞争消失了”，又没有把它扩大成“整个服务并发正确”。

### 先修自检

- e-race：能说清数据竞争的判定条件——至少两个线程、访问同一内存位置、至少一个是写、且没有 happens-before；能解释为什么“在 x86 上看起来能跑”不能证明程序正确。
- e-lock：能写出 `std::lock_guard<std::mutex>` 保护临界区的基本形式；能解释同一把锁的 unlock 与下一次 lock 之间建立了 happens-before；能说出锁粒度太粗会拖慢并发、太细会漏保护。

如果这两条里有任何一条说不清，先回去补。本课的所有例子都建立在“什么算竞争”和“锁如何建立同步”这两个概念上。

---

## 为什么后端要懂

本节点 why 的原话是：**把随机线上问题转成可重现证据。** 在微信公众号后台，这句话对应几类非常具体的故障。它们的共同点是：日志里看不出必然性，监控上只是偶尔抖一下，开发机复现不了，但一旦发生就很难解释。

### 场景一：进程内文章缓存的半写状态

假设服务里有一个进程内缓存，键是 `article_id`，值是标题、正文摘要、阅读数等字段。读请求直接查这个缓存，写请求（编辑、审核、阅读数回写）更新它。如果更新路径没有正确同步，一个线程正在改某个 entry，另一个线程正好读到它，就可能看到“标题是新的、正文还是旧的”这种半写状态，甚至看到 `unordered_map` 内部结构被破坏后的异常行为。

这种问题的麻烦之处在于：它不一定是每次都错。线程交错稍微变一下，可能几百万次请求都没事；线上流量一大、线程池一调度，某一次就撞上了。事后看日志，只能看到“某次读到了不该读到的组合”，没有直接证据说明是哪两个线程在哪两行代码上冲突。

### 场景二：阅读数的“读出旧值 +1 再写回”

阅读数缓存是最经典的例子。两个工作线程同时处理同一篇文章的阅读事件，各自执行：

1. 从缓存里读出 `views` 的旧值，比如 100；
2. 加一，得到 101；
3. 写回缓存。

如果这三步没有同步，两个线程可能都读到 100，都写回 101。业务上少了两次阅读，监控上却完全正常——没有报错，没有异常，只是数字不对。更糟的是，这类丢失更新往往要等运营对账时才被发现。

TSan 对这类问题的价值很直接：它会告诉你“线程 T3 在 `cache_race.cpp:43` 写了这个地址，线程 T1 之前也在同一地址写过，两者之间没有同步”。你不需要再猜是哪个字段、哪一行。

### 场景三：配置 / access token 缓存的刷新与读取并发

公众号后台经常在进程内缓存一些全局配置或第三方平台的 access token。一个后台线程定时刷新，多个请求线程随时读取。如果刷新路径是“构造新对象 → 替换指针”，而读取路径没有原子性或锁保护，就可能读到空指针、读到已经释放的旧对象，或者读到“指针换了但对象还没构造完”的中间状态。

这类故障的典型表现是“偶发 panic / 偶发空指针 / 偶发 401”，而且往往集中在刷新时刻附近。动态检测能做的，是把“刷新线程写指针”和“请求线程读指针”这两侧栈摆在一起，让你看到它们确实没有同步关系。

### 场景四：“跑了很久才崩一次”不等于没有数据竞争

这是最需要纠正的直觉。很多人的判断是：“这个服务跑了半年才出一次问题，肯定不是数据竞争。”事实恰好相反：数据竞争是未定义行为，未定义行为不承诺“每次都错”，它只承诺“编译器可以假设它不存在”。于是同一份代码可能：

- 在开发机上从来不崩；
- 在某台机器上因为编译器优化、CPU 重排、内存布局不同而偶发崩溃；
- 换一个编译器版本之后突然变得“稳定复现”；
- 在压测时因为线程交错变多而暴露。

所以“偶发”不是“没有竞争”的证据，只是“这次交错没有触发可见后果”。动态检测的价值，正是把这种依赖运气的故障，转成报告里的两侧栈和共享对象地址——从“玄学”变成“证据”。

---

## 从直觉到原理

下面三个要点不是并列的知识点，而是一条排障流水线：先有一份带 TSan 的可执行文件（要点一），再把线上偶发缩成能稳定触发的最小程序（要点二），最后分清报告里抓到的是哪一类竞争、没抓到的那一类又该靠什么（要点三）。

### 要点一：TSan 构建与开销

#### 直觉：普通编译器看不见“有没有同步”

先问一个朴素的问题：编译器在编译 `x = x + 1` 的时候，知道这行代码会不会和另一个线程里的 `x = x + 1` 撞上吗？

答案通常是否定的。编译器看到的是一个翻译单元里的语句，它知道这行读写了哪个地址，也知道附近有没有 `pthread_mutex_lock` 调用，但它不会在运行期跟踪“此刻另一个线程在做什么”，更不会替所有可能的线程交错建模。它甚至可能因为“这里没有同步，所以另一个线程不会同时访问”的假设而做优化——这正是数据竞争是 UB 的来源。

TSan 换了一个思路：既然编译器在编译期看不见，那就让程序在运行期自己记账。

- **编译期插桩**：编译器在每一次普通内存读写、每一次函数调用边界、每一次加锁 / 解锁 / 原子操作前后，插入对 TSan 运行时库的调用。
- **运行期记录**：运行时库为每个内存位置维护“最近谁访问过它”的元数据（shadow memory），为每个线程维护逻辑时钟（vector clock）。
- **用 happens-before 判断**：当线程 B 访问某个地址时，TSan 检查该地址上一次访问的线程 A 与 B 之间是否存在 happens-before 边。如果没有，且至少有一次是写，就报告数据竞争。

用一句话概括：**TSan 不是“看代码猜竞争”，而是“记录实际发生的访问，再用 happens-before 规则判定冲突”。**

这也解释了它为什么能给出那么具体的报告：它手上有真实的地址、真实的线程 ID、真实的调用栈。

#### 构建：编译和链接都要加 `-fsanitize=thread`

TSan 不是那种只在链接阶段塞一个库的工具。它需要在编译阶段改写每一条内存访问，因此**编译和链接都必须带 `-fsanitize=thread`**。只加在链接命令上，你会得到“运行时库在、但代码没插桩”的半成品，检测能力会大打折扣甚至直接链接失败。

一条完整的命令长这样：

```bash
clang++ -fsanitize=thread -g -O1 -pthread -o tiny_race tiny_race.cpp
```

三个附加选项各有理由，且都来自官方文档的推荐：

- `-g`：生成调试信息。没有它，报告里的栈帧可能只有函数名和地址，看不到文件名与行号，你很难把报告对回源码。
- `-O1` 或更高：TSan 在未经优化的代码上开销更大，官方建议开启优化以获得合理性能。注意不要为了“看得更清楚”而长期用 `-O0` 跑 TSan 回归，慢到跑不动反而会让人放弃。
- `-pthread`：使用 `std::thread` 时需要线程库；在 Linux 上这是标准做法。

`g++` 同样支持这个 sanitizer 标志：

```bash
g++ -fsanitize=thread -g -O1 -pthread -o tiny_race tiny_race.cpp
```

如果项目用 CMake，关键是保证**所有参与最终可执行文件的编译单元**都带上这个标志，而不是只给某个 target 的链接选项加。一个常见的错误是：主 target 加了，某个静态库没加，结果库里的访问完全没有被插桩。

#### 一个最小例子：两个线程写同一个 `int`

先看一个和官方文档 tiny_race 同构的例子。它只有十几行，故意不做任何同步：

```cpp
// tiny_race.cpp
#include <thread>

int g_counter = 0;

void worker() {
    for (int i = 0; i < 100000; ++i) {
        ++g_counter;   // 普通读 + 普通写，没有任何同步
    }
}

int main() {
    std::thread t1(worker);
    std::thread t2(worker);
    t1.join();
    t2.join();
    return 0;
}
```

编译并运行：

```bash
clang++ -fsanitize=thread -g -O1 -pthread -o tiny_race tiny_race.cpp
./tiny_race
```

你会看到类似下面的输出（地址、线程号、行号因环境而异，这里只保留结构）：

```text
==================
WARNING: ThreadSanitizer: data race (pid=41217)
  Write of size 4 at 0x7b0400000b40 by thread T2:
    #0 worker() tiny_race.cpp:7 (tiny_race+0x...)
    #1 void std::__invoke_impl<void, void (*)()>(...)
    #2 std::thread::_M_start_thread(...)
  Previous write of size 4 at 0x7b0400000b40 by thread T1:
    #0 worker() tiny_race.cpp:7 (tiny_race+0x...)
    #1 void std::__invoke_impl<void, void (*)()>(...)
    #2 std::thread::_M_start_thread(...)
  Location is global 'g_counter' of size 4 at 0x7b0400000b40 (tiny_race+0x...)
  Thread T2 (tid=..., finished) created by main thread at:
    #0 pthread_create ...
    #1 std::thread::_M_start_thread ...
    #2 main tiny_race.cpp:14 (tiny_race+0x...)
  Thread T1 (tid=..., finished) created by main thread at:
    #0 pthread_create ...
    #1 std::thread::_M_start_thread ...
    #2 main tiny_race.cpp:13 (tiny_race+0x...)
SUMMARY: ThreadSanitizer: data race tiny_race.cpp:7 in worker()
==================
ThreadSanitizer: reported 1 warnings
```

#### 报告解剖：四样东西缺一不可

把这份输出拆开看，正好对应 acceptance 的第一条。

1. **告警类型**：`WARNING: ThreadSanitizer: data race`。先确认它是数据竞争，不是别的 sanitizer 报告，也不是程序自身的断言失败。
2. **一侧访问**：`Write of size 4 ... by thread T2`，下面跟着 T2 的调用栈。`size 4` 说明写的是 4 字节，常见于 `int`；栈顶指向 `tiny_race.cpp:7`，也就是 `++g_counter` 那一行。
3. **另一侧访问**：`Previous write ... by thread T1`，同样带栈。注意这里写的是 “Previous”，表示 TSan 记录到的上一次访问。有时候另一侧会是 `Read of size 4`，取决于实际交错。
4. **共享对象**：`Location is global 'g_counter' of size 4 at 0x...`。如果是堆上的对象，通常会变成 `Location is heap block of size N at 0x... allocated by ...`，并附带分配栈。这个地址就是你在验收里要能指认的“共享对象”。

此外还有**线程创建栈**：`Thread T2 ... created by main thread at: ... main tiny_race.cpp:14`。它能帮你回答“这个线程是谁起的、起它的时候在做什么”。在真实服务里，这一栏常常直接指向线程池的创建点。

读报告的顺序建议是：先看 SUMMARY 定位代码行，再回到上面看两侧访问的栈，最后看 Location 与创建栈确认共享对象和线程归属。不要只读第一行就下结论。

#### 一个容易忽略的细节：报告是“某一对访问”

TSan 报告的是它观察到的**一对冲突访问**，而不是“这个程序里所有竞争点的全集”。同一个共享对象可能有多个读写点、多个线程参与，TSan 往往在第一次冲突处就停下来（尤其开了 `halt_on_error=1`），修完之后再跑，可能又冒出另一对。这是正常的：动态检测是一个“发现—修复—再发现”的迭代过程，不是一次扫描出全部问题。

#### 开销：官方给出的数字

动态插桩不是免费的。官方文档给出的典型量级是：

- **速度**：典型减速约 **5x–15x**。
- **内存**：典型内存开销约 **5x–10x**。
- **默认配置**：内存开销大约是 **5x**，再加**每个线程约 1MB**。
- 文档还提到 **3x**（分析更粗）和 **9x**（分析更准）的档位，用于在“跑得动”和“看得细”之间取舍。

这些数字有两个实际含义。

第一，**TSan 构建不适合当日常开发构建**。一个平时几百毫秒的单元测试，插桩后可能变成数秒；一个原本内存预算就紧张的服务，插桩后可能直接超出预算。数字不必精确，按官方给的 5x–10x 量级估算即可。所以它通常作为独立的 CI 任务或专项压测构建存在，而不是替换掉普通构建。

第二，**内存限制的表现会变**。TSan 会映射大量虚拟地址空间来存放 shadow memory（文档明确说这些地址不一定真正占用物理内存）。因此像 `ulimit -v` 这样的虚拟内存限制，可能在 TSan 下表现得和平时完全不同：普通构建轻松通过，TSan 构建却分配失败。遇到“本地能跑、容器里起不来”的情况，先检查是不是虚拟内存 / 地址空间限制，而不是先怀疑代码。

#### 平台与链接限制

按官方文档，使用前要核对这几条：

- **平台**：支持 Linux x86_64、aarch64 等 64 位平台；32 位平台没有计划。
- **静态链接 libc / libstdc++ 不支持**。如果可执行文件把 C 运行时或 C++ 标准库静态链进来，TSan 无法正常工作。
- **非 PIE 可执行文件不支持**。因此该标志会让 Clang 在编译时相当于加上 `-fPIE`、链接时相当于加上 `-pie`。如果你的构建系统里显式写了 `-no-pie`，需要把它去掉或单独为 TSan 目标覆盖。
- **一般要求所有代码都用 `-fsanitize=thread` 编译**。未插桩的预编译动态库是灰色地带：库内部的访问不会被记录，可能导致漏报；库与插桩代码交界处的行为也可能导致误报。能自己编译的依赖，尽量一起插桩。

还有一个安全边界必须说清楚：**TSan 运行时库不是为生产可执行文件设计的**。官方把它定位为缺陷检测工具。不要把 TSan 构建发到线上，也不要指望它在生产环境里“顺便帮你监控竞争”——它的开销、内存映射方式和运行时假设都不适合。

#### 运行选项：`TSAN_OPTIONS`

TSan 的运行时行为通过环境变量 `TSAN_OPTIONS` 控制，多个选项用空格分隔。常用的有这几个：

| 选项 | 作用 | 什么时候用 |
| --- | --- | --- |
| `halt_on_error=1` | 发现第一个错误就退出 | CI 回归、希望快速失败时 |
| `history_size=0..7` | 控制每个线程保留的访问历史量 | 看到 `failed to restore the stack` 时调大 |
| `print_full_thread_history=1` | 打印完整的线程历史 | 需要看清线程从创建到出事的路径时 |
| `exitcode=N` | 自定义退出码 | 脚本需要区分不同失败类型时 |
| `enable_adaptive_delay=1` | 在同步点注入延迟以探索更多交错 | 竞争难以触发时 |

一个典型的组合是：

```bash
TSAN_OPTIONS="history_size=7 halt_on_error=1" ./cache_race
```

要注意一个与直觉不同的默认值：**TSan 的默认退出码是 66**，和其他 sanitizer 不一样。很多 CI 脚本习惯判断“非零即失败”，这没问题；但如果你要写“期望某个退出码”的回归用例，就必须知道 66 这个值，而不是想当然地写 1。后面实践节的脚本会用到它。

#### 两个“关插桩”的开关，以及它们的边界

有时某些代码会被 TSan 报出难以处理的告警，或者某些底层代码确实无法插桩。TSan 提供了两个开关：

- 编译期特性检测：`__has_feature(thread_sanitizer)`，用于在源码里判断当前是否在 TSan 下构建。
- 函数属性：`__attribute__((no_sanitize("thread")))`，用于关闭某个函数的普通 load / store 插桩。

使用方式大致如下：

```cpp
#if defined(__has_feature)
#  if __has_feature(thread_sanitizer)
#    define TSAN_ENABLED 1
#  endif
#endif

__attribute__((no_sanitize("thread")))
void hot_path_with_known_synchronization() {
    // 这里可能关掉一部分普通访问插桩
}
```

但必须强调：**`no_sanitize("thread")` 不是“这段代码没问题”的证明。** 官方文档也说明，TSan 仍会对被标注的函数做一部分插桩，目的是避免误报、保留有意义的调用栈。它不是一个“关掉就清净”的开关，更不是把告警藏起来的手段。用它之前，先问自己：我能不能用 ignorelist 精确地排除第三方代码，而不是把整段业务逻辑静音？

#### pitfalls：不同 Sanitizer 不能任意组合

这是本节点明确点名、也最容易在工程里踩到的坑：**不同 Sanitizer 不能任意组合，必须按工具限制独立构建。**

- AddressSanitizer（`-fsanitize=address`）与 ThreadSanitizer（`-fsanitize=thread`）**不能打进同一个二进制**。
- MemorySanitizer（`-fsanitize=memory`）同样不能和 TSan 组合。
- 因此绝对不要写 `-fsanitize=address,thread` 这种“我全都要”的选项列表。它不是“检测得更全面”，而是直接构建失败，或者得到行为不可预期的产物。

正确的工程做法是把检测目标拆开：

```bash
# 越界 / UAF / double free：用 ASan 构建
clang++ -fsanitize=address -g -O1 -pthread -o cache_asan cache.cpp

# 数据竞争：用 TSan 构建
clang++ -fsanitize=thread -g -O1 -pthread -o cache_tsan cache.cpp
```

两套产物、两套 CI 任务、两套运行脚本。它们的退出码、运行时选项、内存预算都不同，不要试图复用同一个 job。CI 里至少应该有一个名字里带 `tsan` 的独立构建目标，这样“这次改动有没有引入新的数据竞争”才有地方回答。

---

### 要点二：最小复现和调度扰动

#### 线上偶发 ≠ 无法取证

动态检测有一个前提：**被检测的代码必须真的执行到**。如果竞争发生在一条很少走到的分支上，或者只在特定线程交错下才产生可见后果，那么直接在完整服务里跑 TSan，很可能等半天也等不到报告，最后得出一个错误结论：“跑了一小时没报，应该没问题。”

最小复现要解决的就是这个问题。它把“线上偶发”拆成两层：

- **内存层面的冲突**：哪两个线程、访问哪个对象、各自从哪条路径来。这一层只要路径被执行到，TSan 就能记录并判定。
- **业务层面的可见后果**：丢失更新、半写状态、崩溃。这一层依赖交错，可能十次才出现一次。

我们要做的是先把第一层稳定地打出来，再通过扰动让第二层也更容易出现。前者给你证据，后者帮你确认“这个竞争确实会造成业务错误”，而不是一个理论上存在、实际无害的告警。

#### 最小复现的四步

1. **定位可疑共享对象**。从故障现象倒推：是文章缓存里的某个 entry？是配置指针？是阅读数？在完整服务里，这个对象可能被很多层包装，先把它找出来。
2. **抽出“并发更新”这一条路径**。去掉网络、数据库、鉴权、业务分支，只保留“两个线程对同一对象做读改写”这件事。不要怕代码变得“不像线上”——复现的目标是暴露冲突，不是复刻架构。
3. **用两个或几个线程对同一 key 做更新，循环足够多次**。循环的作用不是“制造概率”，而是保证两个线程都真正进入冲突区，并让 TSan 有机会记录到冲突的那一对访问。
4. **在 TSan 构建下运行，直到报告出现**。如果一直不出现，先检查：两个线程是否都执行了？是否有一方因为提前返回而根本没碰到共享对象？共享对象是不是被复制到了各自的栈上，根本没有共享？

一个反例值得警惕：有人把“最小复现”写成了一个仍然需要启动整个服务、连接数据库、发 HTTP 请求的程序。那不叫最小复现，那叫“把线上环境搬到本地”。真正的最小复现应该能在几秒内编译、几秒内跑完、输出稳定。

#### 调度扰动之一：在代码里插入 `yield` 或短暂 `sleep_for`

数据竞争依赖线程交错。即使代码里有 bug，某一次运行也可能“碰巧没撞上”——尤其是读和写之间只隔几条指令时，CPU 和调度器往往一口气跑完。一个可移植、好理解的教学手段，是在“读”和“写”之间主动让出 CPU：

```cpp
// yield_race.cpp
#include <chrono>
#include <thread>

long total = 0;

void bump() {
    for (int i = 0; i < 1000; ++i) {
        long old = total;              // 读
        std::this_thread::yield();     // 主动让出，把读和写之间的窗口拉大
        total = old + 1;               // 写回基于旧值的计算结果
    }
}

int main() {
    std::thread a(bump);
    std::thread b(bump);
    a.join();
    b.join();
    return 0;
}
```

编译运行：

```bash
clang++ -fsanitize=thread -g -O1 -pthread -o yield_race yield_race.cpp
TSAN_OPTIONS="halt_on_error=1 history_size=7" ./yield_race
```

`yield()` 不保证一定切换到另一个线程，但它给了调度器一个机会；在某些实现上，`sleep_for(std::chrono::microseconds(1))` 的扰动更强，代价是整体变慢。两者都只是**教学与复现手段**，不是修复手段——加上 `yield` 之后程序依然是错的，只是错误更容易被看见。

这里要避免一个误读：TSan 报告的是“两个访问之间没有 happens-before”，并不要求它们在纳秒级同时发生。但**如果某次执行里其中一个线程根本没走到那行代码**，TSan 自然也无从记录。扰动的作用，是让两个线程都更稳定地进入冲突区，并让业务层的丢失更新更容易显现。它提高的是“观察到问题”的概率，不是“问题存在”的概率。

#### 调度扰动之二：Adaptive Delay

官方文档还提供了运行期的扰动手段：Adaptive Delay。它会在同步点注入延迟（自旋、`yield` 或短暂 sleep），以探索更多线程交错：

```bash
TSAN_OPTIONS="enable_adaptive_delay=1" ./myapp
```

文档还提到 `adaptive_delay_aggressiveness` 这个选项，建议的取值有 10、50、200，数值越大扰动越强。可以这样组合：

```bash
TSAN_OPTIONS="enable_adaptive_delay=1 adaptive_delay_aggressiveness=50" ./cache_race
```

它适合那种“代码路径明确、但交错窗口太窄”的场景。注意不要编造文档里没有的选项；上面这些是文档给出的，其他名字不要凭感觉写。

#### `history_size` 与 “failed to restore the stack”

最小复现的目标不只是“看到 `data race` 这几个字”，而是**两侧栈都完整**。如果报告里出现：

```text
failed to restore the stack
```

说明 TSan 保留的历史不够，无法还原某一侧的调用栈。这时应该调大 `history_size`（范围 0–7）：

```bash
TSAN_OPTIONS="history_size=7 halt_on_error=1" ./cache_race
```

为什么要强调完整栈？因为验收要求“报告含两侧访问栈与共享对象”。只有一侧栈、或者栈被截断，你就无法回答“另一侧是谁、从哪条业务路径来的”，也就无法判断修复应该加在哪一层。看到 `failed to restore the stack` 却不调 `history_size`，等于把一份本可以定位问题的报告降级成了“知道有竞争，但不知道在哪”。

#### 这次没报警，不等于没有竞争

这是本课最需要反复强调的边界，也是 acceptance 第二条：

- TSan 只观察**实际执行过**的访问。没走到的分支、没创建的线程、没触发的定时刷新，它都看不到。
- 一次运行没有告警，可能是调度恰好没有形成冲突访问，也可能是那条路径根本没执行。
- 即使 TSan 报了警，它报告的也只是“某一对”冲突访问，不代表已经穷尽了所有竞争点。

所以正确的表述是：“在本次构建、本次输入、本次调度下，TSan 没有观察到数据竞争。”而不是：“这个程序没有数据竞争。”前者是证据，后者是过度承诺。

---

### 要点三：数据竞争与逻辑竞争区别

#### 数据竞争：内存层面的规则

先把定义摆出来。**数据竞争（data race）** 指同时满足以下条件的两个访问：

1. 它们来自不同线程；
2. 它们访问同一个内存位置；
3. 至少有一个是写；
4. 两者之间没有 happens-before 关系（没有同一把锁、没有原子操作、没有线程创建 / join 等同步边）。

在 C++ 里，数据竞争是**未定义行为**。这是第 65 课的核心结论，本课直接继承。TSan 的主业就是发现这类问题：它记录每次访问，推导 happens-before，然后指出哪一对访问违反了规则。

注意定义里的“同一内存位置”是字节级的。两个线程分别改一个结构体的不同字段，通常不构成数据竞争（它们不是同一内存位置）；但如果一个线程改字段、另一个线程整体拷贝这个结构体，就可能因为重叠字节而构成竞争。`unordered_map` 更麻烦：一个线程插入导致 rehash，另一个线程同时在查找，即使操作的是不同 key，也可能竞争到桶数组这块共享内存。

#### 逻辑竞争：业务层面的规则

**逻辑竞争（logic race / race condition 的广义用法）** 是另一回事。它说的是：即使每一次内存访问都被锁或原子操作正确保护，业务结果仍可能因为交错而错。

最典型的模式是 check-then-act：

1. 线程 A 检查某个条件（缓存未命中）；
2. 线程 A 根据检查结果做决定（去数据库加载）；
3. 线程 B 在 A 完成之前也做了同样的检查和决定；
4. 两者都执行了本应只执行一次的动作。

从内存模型的角度看，这可能完全合法：每次 `map.find` 都在锁里，每次写入也都在锁里，TSan 找不到任何一对没有 happens-before 的冲突访问。但从业务角度看，“只查一次库”“只发一次消息”“后写不能覆盖先写”这些不变量已经被破坏了。TSan 对此保持沉默，因为它本来就不负责这件事。

#### 用文章缓存把两者对照

先看数据竞争版本。所有线程直接操作同一个 `unordered_map`，没有任何锁：

```cpp
// 数据竞争：TSan 会报
class ArticleCache {
public:
    void increment_views(int id) {
        auto it = table_.find(id);
        if (it == table_.end()) return;
        it->second.views = it->second.views + 1;   // 读 + 写，无同步
    }

    void upsert(int id, const std::string& title) {
        table_[id] = Article{id, title, 0};        // 并发插入还可能触发 rehash
    }

private:
    std::unordered_map<int, Article> table_;
};
```

两个线程同时 `increment_views(42)` 时，TSan 会报告对 `Article::views` 的冲突写；如果一个线程 `upsert` 新 key 触发 rehash、另一个线程 `find`，还可能报告到桶数组或节点指针上。报告里能看到两侧栈和共享对象地址——这正好是我们要的证据。

再看逻辑竞争版本。每次 map 访问都加了同一把锁：

```cpp
// 逻辑竞争：每次访问都有锁，TSan 不会报
std::optional<Article> get(int id) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = table_.find(id);
    if (it == table_.end()) return std::nullopt;
    return it->second;
}

void refresh_from_db(int id) {
    if (!get(id)) {                          // 第一次加锁：查缓存，未命中
        Article fresh = load_from_db(id);    // 锁外：查库，可能很慢
        std::lock_guard<std::mutex> lk(mu_);
        table_[id] = fresh;                  // 第二次加锁：写入
    }
}
```

如果两个线程同时刷新同一个 `id`，它们都可能看到未命中，都去查库，然后先后写入。每次内存访问都有锁，没有数据竞争；但“未命中只查一次库”这个业务不变量被破坏了，还可能发生后写覆盖先写。TSan 不会为这段代码报警。你不能因此说它“并发正确”。

把两者放在一张表里：

| 维度 | 数据竞争 | 逻辑竞争 |
| --- | --- | --- |
| 关注点 | 内存访问是否有 happens-before | 业务不变量是否被交错破坏 |
| 典型例子 | 无锁 `map` 并发读写、`count = count + 1` | check-then-act、双重检查后重复加载 |
| C++ 语义 | 未定义行为 | 通常是合法执行，只是结果不符合预期 |
| TSan | 主业，会报告 | 通常不报告 |
| 修复手段 | 加锁、原子操作、缩小共享 | 扩大临界区、用“加载一次”的协议、去重 / 版本号 |
| 验收方式 | 报告 + 回归脚本 | 业务断言、并发测试、代码审查 |

#### 阅读数的第二次对照

阅读数这个例子可以把两者的区别压到最小。

**数据竞争版本**：

```cpp
// 没有同步
article.views = article.views + 1;
```

两个线程同时执行，TSan 报告对 `views` 的冲突写。这是内存层面的错误。

**加了保护、但仍有逻辑竞争的版本**：

```cpp
// 单次加减有保护
void add_view(Article& article) {
    std::lock_guard<std::mutex> lk(article.mu);
    article.views += 1;
}
```

如果业务只是“对同一个 `Article` 对象做原子式加一”，这样写是对的。但如果业务是“先读出整个文章对象、在对象副本上改一个字段、再把整个对象写回缓存”，而“读出来”和“写回去”之间没有更大的临界区，那么另一个线程的更新可能在中间被覆盖：

```cpp
Article snapshot = cache.get(id);     // 加锁读出一份副本
snapshot.views += 1;                  // 在副本上改
cache.put(id, snapshot);              // 加锁写回整个对象
```

每次 `get` / `put` 都有锁，TSan 沉默；但两个线程可能都基于同一个旧快照，最后后写覆盖先写，丢失一次阅读。这就是逻辑竞争。

结论必须回到 summary：**TSan 能发现许多数据竞争，但不能证明所有并发行为正确。** 不把无告警当绝对正确，不是一句口号，而是要求在报告之外，继续用业务断言、并发测试和代码审查去覆盖“内存合法但业务错误”的那一类问题。

---

## 常见误区与设计权衡

### pitfalls：Sanitizer 不能“叠 buff”

本节点点名的坑只有一句：**不同 Sanitizer 不能任意组合，须按工具限制独立构建。** 这句话值得展开成三条工程纪律。

第一，**不要写合成标志**。`-fsanitize=address,thread` 不是“同时查内存越界和数据竞争”，而是把两个互不兼容的运行时塞进同一个进程。正确做法是分别构建：

```bash
clang++ -fsanitize=address -g -O1 -pthread -o cache_asan cache.cpp
clang++ -fsanitize=thread  -g -O1 -pthread -o cache_tsan cache.cpp
```

第二，**CI 里要有独立的 tsan 目标**。常见反模式是：项目只有一个 `test` job，里面用 ASan 跑单测，然后宣称“并发也测了”。ASan 不会告诉你数据竞争。你应该有一个单独的 job，名字明确写 `tsan`，使用 TSan 构建、`TSAN_OPTIONS="halt_on_error=1 history_size=7"` 运行、并把退出码当成失败信号。这个 job 可以只跑并发相关的测试集，控制时间成本。

第三，**不要为了“跑得快”把不同工具的产物混用**。比如用 ASan 构建的库去链接 TSan 构建的主程序，或者反过来。运行时库之间会冲突，行为不可预期。每个检测目标都应该是一套完整、自洽的构建。

### tradeoffs：动态检测的覆盖边界

本节点的 tradeoffs 是：**动态检测覆盖实际路径，模型检查和代码审查补足未执行路径。**

这句话不是让你“再学一个模型检查工具”，而是让你在工程上诚实：

- TSan 能覆盖的，是这次运行真正执行到的内存访问与同步操作。它对这些路径的判定很精确，报告也很具体。
- TSan 覆盖不了的，是没有执行到的分支、没有创建的线程、没有触发的定时任务，以及“内存访问都合法但业务不变量被破坏”的逻辑竞争。
- 补足手段有三类：**模型检查 / 静态分析**用来枚举或推理更多路径；**代码审查**用人类对业务不变量的理解去发现 check-then-act；**并发测试与业务断言**用可观察的结果去兜底。
- 它们的成本不同：动态检测要跑起来、要接受 5x–15x 减速；静态分析可能误报；代码审查依赖 reviewer 的经验。没有一种手段能单独给出“并发正确”的证明。

一个实用的组合是：TSan 负责“内存层面的硬错误”，并发单测负责“业务不变量”，review checklist 负责“锁的边界和 check-then-act”。三者都过，才敢说“这一类问题风险可控”。

### 其他常见误区

除了节点明确点名的 pitfalls 和 tradeoffs，下面这些也经常出现：

1. **把一次 TSan 无报警当成“并发正确”的证明。** 这是 acceptance 第二条的反面。没有观察到，不等于不存在。
2. **只在开发机偶尔手跑，不放进回归。** 今天修好了，明天有人改回去，没人知道。检测必须变成可重复的 CI 步骤。
3. **最小复现做得不够小，在完整服务里等偶发。** 启动慢、依赖多、跑十次也不一定触发。先把冲突路径抽出来。
4. **看到 `failed to restore the stack` 却不调 `history_size`。** 报告缺栈，定位价值大打折扣。
5. **把 TSan 构建发到生产。** 运行时库不是为生产设计的，开销和内存映射都不合适。
6. **只测“单线程路径”却声称测过并发。** 单线程跑一万次也碰不到线程交错。
7. **用 ignorelist / `no_sanitize` 大面积关掉插桩，然后宣布干净。** 你只是把告警藏起来了，没有修复竞争。
8. **把逻辑竞争误认为 TSan 能抓。** 每次访问都有锁的 check-then-act，TSan 不会报告，需要业务层测试。
9. **只修报告里的那一对访问，不检查同类模式。** 同一个共享对象可能在别处还有未加锁的路径；修完要再跑，并做一次模式排查。
10. **忽略未插桩的依赖库。** 预编译动态库里的访问不会被记录，可能出现漏报或误报；能重编的依赖尽量一起插桩。

### 一张权衡表

| 手段 | 能发现 | 发现不了 | 成本 |
| --- | --- | --- | --- |
| TSan 动态检测 | 实际执行路径上的数据竞争，带两侧栈 | 未执行路径、逻辑竞争 | 5x–15x 减速，5x–10x 内存 |
| ASan | 越界、UAF、double free | 数据竞争 | 需独立构建，不能与 TSan 合并 |
| 静态分析 / 模型检查 | 部分未执行路径上的模式 | 依赖运行期输入的复杂交错 | 误报、配置成本 |
| 代码审查 | 业务不变量、锁边界、check-then-act | 难以人眼覆盖的所有交错 | 人力、依赖经验 |
| 并发单测 + 业务断言 | 业务结果错误 | 未覆盖的调度与输入 | 需要设计可观测的不变量 |

这张表想说明的不是“TSan 不够好”，而是“它在一个更大的工具箱里”。本课把 TSan 用到位，同时清楚它不负责什么。

---

## 动手实践：给文章缓存做一次 TSan 体检

### 实践目标与产物

本节的 practice 原话是：**对故意错误的缓存并发更新运行 TSan，修复后加入稳定回归用例。**

做完之后，你应该手上有四样产物：

1. 一个故意有 bug 的 `cache_race.cpp`，用宏 `BUGGY_CACHE` 切换“有 bug / 已修复”两种实现。
2. 一份真实的 TSan 报告，能圈出两侧访问栈和共享对象地址。
3. 一条修复后的 TSan 运行记录，退出码为 0。
4. 一个 shell 回归脚本：buggy 版本期望 TSan 报告（退出码 66），fixed 版本期望退出码 0。

注意第 4 条的措辞：回归测的是“这类缓存并发更新不再出现数据竞争”，不是“整个服务并发正确”。这正好对应 acceptance 第二条。

### 步骤 0：环境检查

先确认编译器支持 TSan，并且不是 32 位环境：

```bash
clang++ --version
uname -m          # 期望 x86_64 或 aarch64
```

如果用的是容器，确认没有设置过小的虚拟内存限制。TSan 会映射大量虚拟地址空间，`ulimit -v` 太小会导致启动失败：

```bash
ulimit -v
```

如果输出是一个较小的数字（比如几十万 KB），先在同一条 shell 里放宽，或换一个不受限的环境再做实验。这个现象本身也值得记住：**TSan 下的内存限制表现和普通构建不同。**

### 步骤 1：写一个有 bug 的文章缓存

把下面的代码保存为 `cache_race.cpp`。场景是公众号后台把阅读数回写到进程内缓存：`ArticleCache` 内部是 `unordered_map<int, Article>`，`Article` 有 `id`、`title`、`views` 三个字段。四个工作线程并发对同一批 `article_id` 做 `increment_views`。

```cpp
// cache_race.cpp
//
// 编译：
//   buggy: clang++ -fsanitize=thread -g -O1 -pthread -DBUGGY_CACHE \
//                  -o cache_race_buggy cache_race.cpp
//   fixed: clang++ -fsanitize=thread -g -O1 -pthread \
//                  -o cache_race_fixed cache_race.cpp
//
// 运行：
//   TSAN_OPTIONS="halt_on_error=1 history_size=7" ./cache_race_buggy
//   TSAN_OPTIONS="halt_on_error=1 history_size=7" ./cache_race_fixed

#include <cstdio>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

struct Article {
    int id = 0;
    std::string title;
    long views = 0;
};

class ArticleCache {
public:
    void upsert(int id, const std::string& title) {
#ifdef BUGGY_CACHE
        // 故意错误：没有锁，直接改 map
        table_[id] = Article{id, title, 0};
#else
        std::lock_guard<std::mutex> lk(mu_);
        table_[id] = Article{id, title, 0};
#endif
    }

    void increment_views(int id) {
#ifdef BUGGY_CACHE
        // 故意错误：find 之后直接读改写 views，没有任何同步
        auto it = table_.find(id);
        if (it == table_.end()) return;
        it->second.views = it->second.views + 1;
#else
        std::lock_guard<std::mutex> lk(mu_);
        auto it = table_.find(id);
        if (it == table_.end()) return;
        it->second.views = it->second.views + 1;
#endif
    }

    long views(int id) {
#ifdef BUGGY_CACHE
        auto it = table_.find(id);
        if (it == table_.end()) return -1;
        return it->second.views;
#else
        std::lock_guard<std::mutex> lk(mu_);
        auto it = table_.find(id);
        if (it == table_.end()) return -1;
        return it->second.views;
#endif
    }

private:
    std::unordered_map<int, Article> table_;
#ifndef BUGGY_CACHE
    std::mutex mu_;
#endif
};

int main() {
    ArticleCache cache;
    for (int id = 1; id <= 8; ++id) {
        cache.upsert(id, "article-" + std::to_string(id));
    }

    constexpr int kThreads = 4;
    constexpr int kRounds = 10000;

    std::vector<std::thread> workers;
    workers.reserve(kThreads);
    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&cache] {
            for (int round = 0; round < kRounds; ++round) {
                for (int id = 1; id <= 8; ++id) {
                    cache.increment_views(id);
                }
            }
        });
    }
    for (auto& w : workers) w.join();

    const long expected = static_cast<long>(kThreads) * kRounds;
    const long actual = cache.views(1);
    std::printf("views(1) = %ld, expected = %ld\n", actual, expected);

    // fixed 版本期望计数完整；buggy 版本通常会少，但因为 TSan 先退出，
    // 这里主要作为“修复后业务结果也正确”的二次确认。
    return actual == expected ? 0 : 2;
}
```

几个设计点说明：

- **只让 `increment_views` 并发**。`upsert` 在主线程里预置数据，避免一开始就 rehash，把竞争集中到 `views` 这个字段上，报告更干净。
- **同一批 key**。四个线程都对 `id = 1..8` 做更新，保证它们真的访问同一批内存位置，而不是各改各的、碰巧没有交集。
- **循环足够多次**。`4 × 10000 × 8` 次更新，既能让 TSan 稳定记录到冲突访问，也能让修复后的计数校验有意义。
- **`BUGGY_CACHE` 宏切换两种实现**。这样 buggy 和 fixed 是同一份源码的两个构建，回归脚本不会因为两份代码逐渐分叉而失效。

如果想观察更严重的 map 结构破坏，可以另加一个实验：让若干线程对**新 key** 调用 `upsert`，同时其他线程 `increment_views`。并发插入可能触发 rehash，TSan 会报告到桶数组或节点上，而不只是 `views` 字段。但作为第一个实验，建议先用上面的版本，因为它的报告最容易读。

### 步骤 2：用 TSan 构建并运行

先构建 buggy 版本：

```bash
clang++ -fsanitize=thread -g -O1 -pthread -DBUGGY_CACHE \
        -o cache_race_buggy cache_race.cpp
```

再运行：

```bash
TSAN_OPTIONS="halt_on_error=1 history_size=7" ./cache_race_buggy
```

如果使用 `g++`，把命令开头的 `clang++` 换成 `g++` 即可，sanitizer 标志相同：

```bash
g++ -fsanitize=thread -g -O1 -pthread -DBUGGY_CACHE \
    -o cache_race_buggy cache_race.cpp
```

`halt_on_error=1` 让 TSan 在第一个错误后退出，`history_size=7` 尽量保留完整栈。运行结束后立刻查看退出码：

```bash
echo $?
```

在报告了数据竞争的情况下，默认退出码是 **66**，不是 1。这一点在后面的回归脚本里会用到。

### 步骤 3：读报告，圈出四个要素

你会看到类似下面的输出。行号、线程编号、地址因环境和编译选项而异，但结构是稳定的：

```text
==================
WARNING: ThreadSanitizer: data race (pid=52163)
  Write of size 8 at 0x7b0400000b78 by thread T3:
    #0 ArticleCache::increment_views(int) cache_race.cpp:43 (cache_race_buggy+0x...)
    #1 operator() cache_race.cpp:87 (cache_race_buggy+0x...)
    #2 void std::__invoke_impl<...>(...)
    #3 std::thread::_M_start_thread(...)

  Previous write of size 8 at 0x7b0400000b78 by thread T1:
    #0 ArticleCache::increment_views(int) cache_race.cpp:43 (cache_race_buggy+0x...)
    #1 operator() cache_race.cpp:87 (cache_race_buggy+0x...)
    #2 void std::__invoke_impl<...>(...)
    #3 std::thread::_M_start_thread(...)

  Location is heap block of size 64 at 0x7b0400000b40 allocated by main thread:
    #0 operator new(unsigned long) ...
    #1 std::__detail::_Hash_node<...>::_M_allocate_node() ...
    #2 main cache_race.cpp:73 (cache_race_buggy+0x...)

  Thread T3 (tid=..., running) created by main thread at:
    #0 pthread_create ...
    #1 std::thread::_M_start_thread ...
    #2 main cache_race.cpp:84 (cache_race_buggy+0x...)

SUMMARY: ThreadSanitizer: data race cache_race.cpp:43 in ArticleCache::increment_views(int)
==================
ThreadSanitizer: reported 1 warnings
```

按 acceptance 第一条，逐项圈出来：

1. **告警类型**：`WARNING: ThreadSanitizer: data race`。确认是数据竞争。
2. **一侧访问栈**：`Write of size 8 ... by thread T3`，栈顶 `cache_race.cpp:43`，也就是 `it->second.views = it->second.views + 1;` 这一行。`size 8` 对应 64 位 Linux 上的 `long`。
3. **另一侧访问栈**：`Previous write ... by thread T1`，同样指向第 43 行。说明两个线程都在无同步地写同一个 `views`。
4. **共享对象地址**：`Location is heap block of size 64 at 0x...`。`Write` 那一行给出的地址就是 `views` 字段所在位置；`Location` 给出的是包含它的堆块起始地址与大小。块的分配栈指向 `unordered_map` 的节点分配，也就是某个 `Article` 所在的哈希节点。两处地址都保留在报告里，方便你对照，不要求手算偏移。

如果你看到的是 `Location is global`，说明共享对象是全局变量；如果是 `heap block`，就是堆对象。两者都能满足“报告含共享对象”的要求。关键是你要能说出：**这个地址对应哪个字段或哪个 map 节点**。在本例里，它对应 `Article::views`，而不是 `title` 或 `id`——因为报告里的 `size 8` 和源码行都指向 `views` 的自增。

这里也顺便回答一个常见疑问：为什么另一侧是 `Previous write` 而不是 `Read`？因为 `views = views + 1` 既读又写，TSan 记录到的冲突访问可能呈现为两次写，也可能呈现为一读一写，取决于它先记录到哪一次。只要两侧栈都在、地址一致，就足以定位。

---

### 步骤 4：修复：用一把互斥锁保护整段临界区

修复版本已经在代码里：不定义 `BUGGY_CACHE` 时，`ArticleCache` 持有一把 `std::mutex mu_`，`upsert`、`increment_views`、`views` 三个入口都先 `std::lock_guard<std::mutex> lk(mu_);`，再访问 `table_`。

这里的关键不是“加了锁”三个字，而是**锁覆盖了整段读改写**：

```cpp
void increment_views(int id) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = table_.find(id);          // 查找在锁内
    if (it == table_.end()) return;
    it->second.views = it->second.views + 1;   // 读 + 写都在锁内
}
```

如果只在写回时加锁、查找在锁外，`it` 可能在查找之后失效（另一个线程插入触发 rehash），仍然是错的。锁的边界要和“这个操作依赖哪些共享状态”一致，这正是第 66 课讲的锁粒度问题。

重新构建并运行 fixed 版本：

```bash
clang++ -fsanitize=thread -g -O1 -pthread \
        -o cache_race_fixed cache_race.cpp

TSAN_OPTIONS="halt_on_error=1 history_size=7" ./cache_race_fixed
echo $?
```

期望输出类似：

```text
views(1) = 40000, expected = 40000
```

退出码为 `0`。注意，这里同时验证了两件事：TSan 没有再报告这条数据竞争；修复后的业务计数也回到了期望值。前者是内存层面的证据，后者是业务层面的二次确认，二者不能互相替代。

### 步骤 5：把检测变成稳定回归用例

不要靠“我眼睛看过一次报告”。把 buggy 和 fixed 两次运行写进脚本，让退出码说话。保存为 `run_tsan_regression.sh`：

```bash
#!/usr/bin/env bash
set -u

CXX="${CXX:-clang++}"
SRC="cache_race.cpp"
FLAGS="-fsanitize=thread -g -O1 -pthread"
OPTS="halt_on_error=1 history_size=7"

echo "== 1. buggy 版本：期望 TSan 报告数据竞争，退出码 66 =="
if ! "$CXX" $FLAGS -DBUGGY_CACHE -o cache_race_buggy "$SRC"; then
    echo "FAIL: buggy 构建失败"
    exit 1
fi
TSAN_OPTIONS="$OPTS" ./cache_race_buggy > buggy.log 2>&1
buggy_rc=$?
echo "buggy exit code = $buggy_rc"

echo "== 2. fixed 版本：期望退出码 0 =="
if ! "$CXX" $FLAGS -o cache_race_fixed "$SRC"; then
    echo "FAIL: fixed 构建失败"
    exit 1
fi
TSAN_OPTIONS="$OPTS" ./cache_race_fixed > fixed.log 2>&1
fixed_rc=$?
echo "fixed exit code = $fixed_rc"

if [ "$buggy_rc" -ne 66 ]; then
    echo "FAIL: buggy 版本没有以 TSan 默认退出码 66 结束"
    echo "      检查复现是否够小、线程是否真的进入冲突区"
    exit 1
fi

if ! grep -q "WARNING: ThreadSanitizer: data race" buggy.log; then
    echo "FAIL: buggy 输出里没有 data race 告警"
    exit 1
fi

if [ "$fixed_rc" -ne 0 ]; then
    echo "FAIL: 修复版本仍然非零退出"
    exit 1
fi

echo "PASS: buggy 报数据竞争，fixed 干净"
```

赋予执行权限并运行：

```bash
chmod +x run_tsan_regression.sh
./run_tsan_regression.sh
```

几点说明：

- 脚本要求 buggy 的退出码**恰好是 66**，而不是“非零即可”。这样能区分“TSan 真的报了竞争”和“程序因为计数不对返回 2”这两种情况。TSan 的默认退出码就是 66，这一点和 ASan 等工具不同，必须显式记住。
- `halt_on_error=1` 保证遇到第一个竞争就退出，不会在 buggy 版本里继续跑完整个循环。这也让 buggy 的退出码稳定为 66。
- fixed 版本要求退出码 0，同时它的 `main` 会检查 `views(1)` 是否等于期望值。如果锁没加对，计数校验也会失败。
- 这个脚本适合放进 CI 的 `tsan` job。它测的是“这类缓存并发更新不再出现数据竞争”，不是“整个服务并发正确”。不要把它扩大解释。

如果你的 CI 环境对 TSan 的内存开销敏感，可以把 `kRounds` 调小，或者只跑一个专门的并发测试子集。但不要因为“跑得慢”就把这个 job 删掉——那等于把本课最有价值的产物扔了。

### 步骤 6：别把逻辑竞争混进来

修复数据竞争之后，缓存可能仍然有逻辑竞争。看下面这段代码：每次 `get` 和 `put` 都有锁，TSan 不会报警，但两个线程可能同时未命中、同时查库、先后写回。

```cpp
// 这段代码没有数据竞争，但有逻辑竞争：TSan 不会报
std::optional<Article> get(int id) {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = table_.find(id);
    if (it == table_.end()) return std::nullopt;
    return it->second;
}

void refresh_from_db(int id) {
    auto cached = get(id);                 // 第一次加锁：查缓存
    if (cached) return;

    Article fresh = load_from_db(id);      // 锁外：查库，可能很慢

    std::lock_guard<std::mutex> lk(mu_);
    table_[id] = fresh;                    // 第二次加锁：写入
}
```

两个线程同时 `refresh_from_db(42)`：

1. 都调用 `get(42)`，都得到未命中；
2. 都调用 `load_from_db(42)`，数据库被查了两次；
3. 都写回，后写的覆盖先写的。

每一次内存访问都在锁里，没有任何一对访问缺少 happens-before，所以 TSan 沉默。但“未命中只加载一次”这个业务不变量已经被破坏。修复方向不是“再加一把锁”，而是改变协议：比如用“加载中”占位、用 `std::once_flag` / 版本号、或者把“查缓存 + 加载 + 写回”设计成可去重的流程。具体选哪种，取决于业务对一致性、延迟和实现复杂度的要求。

把这段对照实验保留在仓库里，并在注释里写清楚“TSan 不报是预期行为”。它是 acceptance 第二条最好的教材：**无告警不等于正确。**

### 步骤 7：验收清单

做完之后，对照下面这张表逐项打勾：

| 验收项 | 可检查事实 | 对应 acceptance |
| --- | --- | --- |
| 有 TSan 构建 | 命令里同时有 `-fsanitize=thread -g -O1 -pthread` | goals |
| 有两侧栈 | 报告里能找到 `Write ... by thread T3` 和 `Previous write ... by thread T1`，各自带源码行 | 报告含两侧访问栈 |
| 有共享对象 | 报告里有 `at 0x...` 或 `Location is ...`，并能指出对应 `Article::views` / map 节点 | 报告含共享对象 |
| 修复有效 | fixed 版本 TSan 运行退出码 0，计数等于期望值 | practice |
| 回归稳定 | 脚本对 buggy 期望 66、对 fixed 期望 0 | practice |
| 边界清楚 | 文档或注释里写明“只覆盖本次执行路径”“逻辑竞争不在 TSan 职责内” | 不把无告警当绝对正确 |

如果表格里有任何一项只能靠“我记得跑过”来回答，就还没有达到验收标准。

---

## 延伸阅读：sourceStudy

本节点只给了一条资源：

- **竞态检测与并发故障复现：参考文档 / 原始资料**
- 链接：https://clang.llvm.org/docs/ThreadSanitizer.html

按节点建议，沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。下面分三遍读。

### 第一遍：Usage —— 构建方式与 tiny_race 报告

先看文档的 Usage 部分，确认三件事：

1. **编译方式**：使用 `-fsanitize=thread`，并配合 `-g` 与 `-O1`。`-g` 是为了报告里的文件名和行号，`-O1` 是为了性能。核对一下文档给出的命令形式，和你实际用的构建命令是否一致；尤其注意编译与链接都要带标志。
2. **tiny_race 示例**：文档给了一个最小的数据竞争程序，并展示了典型报告。对照本课的 `tiny_race.cpp`，确认报告里这几栏的含义：`Write of size ...`、`Previous write ...`、两侧的调用栈，以及线程创建栈。
3. **报告结构**：文档的示例报告通常会包含 `SUMMARY` 行，把竞争定位到函数和源码行。先学会从 `SUMMARY` 回到两侧栈，再回到共享对象，这是读任何 TSan 报告的基本路径。

这一遍的目标不是背命令，而是建立“命令 → 报告 → 源码行”的映射。读完之后，你应该能拿任意一份 TSan 报告，在五分钟内说出“哪两个线程、哪两行、哪个对象”。

### 第二遍：Limitations、Security、Run-time Flags

第二遍读边界和运行时配置，这是最容易“知道有这回事、但真遇到时想不起来”的部分。

**Limitations** 里重点核对：

- **内存与虚拟地址**：TSan 会映射大量虚拟地址空间，但文档也说明这些地址不一定真正占用物理内存。结合本课的实验，理解为什么 `ulimit` 一类限制在 TSan 下表现不同。
- **静态链接**：不支持静态链接 libc / libstdc++。
- **PIE**：不支持非 PIE 可执行文件，因此该标志会让 Clang 在编译时相当于加 `-fPIE`、链接时相当于加 `-pie`。
- **未插桩库**：一般要求所有代码都用 `-fsanitize=thread` 编译；未插桩的预编译动态库可能导致漏报或误报。
- **GDB 与 ASLR**：文档提到，GDB 默认关闭 ASLR 可能导致 shadow memory 分配失败。调试时需要用类似下面的命令：
  ```bash
  gdb -ex 'set disable-randomization off' --args ./a.out
  ```
  这一条经常在“程序单独跑没问题、一进 GDB 就起不来”时救命。

**Security Considerations** 里重点确认：TSan 是缺陷检测工具，运行时库不是为生产可执行文件设计的。不要把 TSan 构建发到线上。这一点和本课 pitfalls 里的“独立构建”是配套的：检测构建只存在于 CI / 本地，不进入发布产物。

**Run-time Flags** 里核对本课用到的选项：

- `TSAN_OPTIONS`：通过环境变量传，多个选项用空格分隔。
- `halt_on_error=1`：发现第一个错误就退出。
- `history_size`：范围 0–7，看到 `failed to restore the stack` 时调大。
- `print_full_thread_history=1`：打印完整线程历史。
- `exitcode`：TSan 默认退出码是 **66**，和其他 sanitizer 不同。
- `enable_adaptive_delay=1`：在同步点注入延迟，探索更多交错；文档还提到 `adaptive_delay_aggressiveness`，建议值有 10 / 50 / 200。

读这一遍时，建议把本课实践脚本里的选项和文档逐条对上，确认没有拼写错误、没有把别的工具的选项混进来。

### 第三遍：划边界

第三遍只做一件事：把“这个工具能做什么、不能做什么”写成自己的清单。对照文档确认：

- **支持的平台**：Linux x86_64、aarch64 等 64 位平台；32 位平台没有计划。
- **典型开销**：速度约 5x–15x，内存约 5x–10x；默认内存约 5x 再加每个线程约 1MB。文档还提到 3x / 9x 的分析档位。
- **组合限制**：不同 sanitizer 不能随意组合。ASan、MSan 与 TSan 不能打进同一个二进制；要查越界用 ASan 构建，要查数据竞争用 TSan 构建。
- **不适用场景**：生产可执行文件、静态链接 libc/libstdc++、非 PIE、大量未插桩依赖。

同时记录**适用版本**：以你实际使用的 Clang / GCC 版本为准，核对文档对应的版本说明。不同版本对平台、选项、报告格式的支持可能有差异；把版本写进实验记录，避免“文档说有、我的编译器没有”的困惑。

最后提醒一句：不要编造该页面没有的内容。文档没写的选项、没承诺的支持范围、没给出的性能数字，都不要凭印象补充。工程判断可以写，但要和“文档事实”分开标注。

---

## 自测题

先别看答案。能对着三个要点讲清楚，再对照答案检查。

### 题 1：构建、开销与退出码

**问题**：你要为一个 C++17 的并发缓存程序开启 TSan。请回答：

1. 编译和链接命令应该怎么写？为什么 `-g` 和 `-O1` 都建议加？
2. 官方给出的典型速度和内存开销大概是多少？默认内存开销的构成是什么？
3. 如果 TSan 报告了数据竞争，程序默认退出码是多少？为什么不能想当然地按 1 来处理？
4. 能不能写成 `-fsanitize=address,thread`？为什么？

**答案**：

1. 编译和链接都要带 `-fsanitize=thread`，例如：
   ```bash
   clang++ -fsanitize=thread -g -O1 -pthread -o cache_race cache_race.cpp
   ```
   `g++` 同理。`-g` 让报告里能显示文件名和行号；`-O1` 或更高用于获得合理性能，避免未优化代码让 TSan 慢到不可用。`-pthread` 是使用 `std::thread` 的常规要求。
2. 典型减速约 **5x–15x**，典型内存开销约 **5x–10x**。默认配置下内存开销大约是 **5x**，再加**每个线程约 1MB**。文档还提到 3x（分析更粗）和 9x（分析更准）的档位。
3. 默认退出码是 **66**。和其他 sanitizer 不同，所以回归脚本如果要判断“TSan 是否真的报告了竞争”，应该显式检查 66，而不是只判断“非零”。如果只判断非零，程序自身的业务校验失败（比如返回 2）也会被误认为“TSan 抓到了”。
4. 不能。AddressSanitizer 和 ThreadSanitizer 不能打进同一个二进制，MemorySanitizer 也不能和 TSan 组合。`-fsanitize=address,thread` 不是“检测更全面”，而是构建失败或行为不可预期。正确做法是两套独立构建：ASan 查越界 / UAF，TSan 查数据竞争；CI 里也应该是两个独立任务。

### 题 2：最小复现、调度扰动与报告要素

**问题**：线上偶发“文章缓存读到半写状态”，你在完整服务里跑 TSan 半小时没有报告。请回答：

1. 为什么“跑了半小时没报”不能说明没有问题？
2. 最小复现应该怎么抽？至少说出四个步骤。
3. 有哪些调度扰动手段？它们的作用是什么？
4. 为什么验收要求报告里必须有两侧访问栈和共享对象？只看到 `WARNING: data race` 不够吗？
5. 报告里出现 `failed to restore the stack` 怎么办？

**答案**：

1. TSan 只观察实际执行过的内存访问与同步关系。完整服务里可能没有走到冲突路径、没有创建相关线程、或者调度恰好没有让冲突访问发生。半小时没有报告，只能说明“这半小时的执行里没有观察到”，不能证明程序没有数据竞争。
2. 四步：① 定位可疑共享对象（比如某个缓存 entry 或 map 节点）；② 抽出“并发更新”这一条路径，去掉网络、数据库、业务分支；③ 用两个或几个线程对同一 key 做更新，循环足够多次；④ 在 TSan 构建下反复运行，直到报告出现。如果还不出现，检查两个线程是否都真的进入了冲突区、共享对象是否真的共享。
3. 两种主要手段：一是在代码里显式插入 `std::this_thread::yield()` 或短暂 `sleep_for`，把“读-改-写”拆开，增大交错窗口；二是使用官方的 Adaptive Delay：`TSAN_OPTIONS="enable_adaptive_delay=1" ./myapp`，还可以配合 `adaptive_delay_aggressiveness`（文档建议值 10 / 50 / 200）。它们提高的是“观察到问题”的概率，不是修复问题。
4. 因为只有告警类型，你无法回答“谁和谁冲突、在哪个对象上、从哪条业务路径来”。两侧访问栈告诉你两个线程各自的调用路径；共享对象地址（`at 0x...` / `Location is ...`）让你把它对回具体的字段或 map 节点。没有这些，修复只能靠猜，也无法确认修的是不是报告里的那一处。这就是 acceptance 第一条“报告含两侧访问栈与共享对象”的意义。
5. 调大 `history_size`（范围 0–7），例如：
   ```bash
   TSAN_OPTIONS="history_size=7 halt_on_error=1" ./cache_race
   ```
   目标是让两侧栈都尽量完整。看到这个提示却不调，报告的可定位性会明显下降。

### 题 3：数据竞争 vs 逻辑竞争

**问题**：下面两段代码都可能让阅读数出错，但 TSan 的行为不同。请分别说明它们属于哪类问题、TSan 会不会报告，以及为什么“无告警”不能推出“并发正确”。

代码 A：

```cpp
article.views = article.views + 1;   // 多线程直接执行，无锁
```

代码 B：

```cpp
Article snapshot = cache.get(id);    // get 内部加锁
snapshot.views += 1;
cache.put(id, snapshot);             // put 内部加锁
```

**答案**：

代码 A 是**数据竞争**：多个线程访问同一个 `views`，至少一个是写，且没有 happens-before。它在 C++ 里是未定义行为。TSan 会报告，报告里能看到两侧访问栈和 `views` 的地址。修复方式是加锁、使用原子操作，或缩小共享范围。

代码 B 是**逻辑竞争**：`get` 和 `put` 各自都有锁，每一次内存访问都受保护，因此不存在数据竞争，TSan 通常不会报告。但“读出快照 → 修改 → 写回”这三步不是一个原子操作；两个线程可能基于同一份旧快照，后写覆盖先写，导致丢失更新。修复方向是扩大临界区、使用“加载一次”的协议、版本号或 CAS 之类的业务层机制，而不是简单再加一把锁。

“无告警不等于正确”的原因：TSan 的主业是发现数据竞争，它不负责检查业务不变量。代码 B 在内存模型上完全合法，但业务结果可能错误。动态检测还只覆盖实际执行过的路径，未触发的调度、未走到的分支都看不到。因此正确表述是“本次执行没有观察到数据竞争”，而不是“程序并发正确”。补足手段包括并发测试、业务断言、代码审查，以及在需要时使用模型检查 / 静态分析去覆盖未执行路径——这正是节点的 tradeoffs。

---

## 推荐资源

- **竞态检测与并发故障复现：参考文档 / 原始资料**
  - 链接：https://clang.llvm.org/docs/ThreadSanitizer.html
  - 来源：clang.llvm.org
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：按节点建议，重点阅读“TSan 构建与开销”和“最小复现与调度扰动”，用于核对本节点实验的语义和边界。阅读时抓住：Usage 里的 `-fsanitize=thread` / `-g` / `-O1` 与 tiny_race 报告结构（Write / Previous write / 线程创建栈）；Limitations 里的内存与虚拟地址、静态链接、PIE、未插桩库，以及 GDB 关闭 ASLR 导致 shadow memory 分配失败时的调试方式；Security Considerations 里“不要用于生产”的边界；Run-time Flags 里的 `TSAN_OPTIONS`、`halt_on_error`、`history_size`、默认退出码 66、`enable_adaptive_delay`。最后核对支持平台与典型开销（5x–15x 速度、5x–10x 内存），并记下不同 sanitizer 必须独立构建这一限制。页面没有展开的业务层逻辑竞争、并发回归设计，以本课讲解与实践为准。

---

## 本课小结

1. **动态检测看的是“这次执行”。** TSan 记录实际发生的内存访问与同步操作，用 happens-before 判断冲突；它能发现许多数据竞争，但不能证明所有并发行为正确。这是本节点 summary，也是全课的总纲。
2. **TSan 是编译期插桩 + 运行时库。** 编译和链接都要带 `-fsanitize=thread`；`-g` 保留文件名和行号，`-O1` 或更高保证性能，`-pthread` 支持线程库。
3. **报告要有四样东西。** 告警类型、一侧访问栈、另一侧 `Previous write/read` 栈、共享对象地址（以及线程创建栈）。能把这四样对回源码字段，才算满足 acceptance 第一条。
4. **开销要用官方数字说话。** 典型减速 5x–15x，典型内存 5x–10x；默认内存约 5x 再加每线程约 1MB；还有 3x / 9x 的分析档位。TSan 会映射大量虚拟地址空间，`ulimit` 表现可能和平时不同。
5. **平台与链接边界要提前核对。** 支持 Linux x86_64、aarch64 等 64 位平台，32 位不在计划内；不支持静态链接 libc/libstdc++；不支持非 PIE；未插桩的预编译库可能导致漏报或误报。
6. **TSan 构建不进生产。** 它是缺陷检测工具，运行时库不是为生产可执行文件设计的。它应该出现在本地实验和 CI 的独立 `tsan` 任务里。
7. **`TSAN_OPTIONS` 是运行期控制面。** 常用 `halt_on_error=1`、`history_size`（0–7）、`print_full_thread_history=1`、`exitcode`；默认退出码是 66，不是 1。看到 `failed to restore the stack` 就调大 `history_size`。
8. **不同 Sanitizer 不能任意组合。** 不要把 ASan 和 TSan 写进同一个 `-fsanitize=` 列表；MSan 也不能和 TSan 组合。要查越界用 ASan 构建，要查数据竞争用 TSan 构建，两套产物、两套 CI 任务。这就是节点 pitfalls。
9. **最小复现 + 调度扰动，把偶发变成证据。** 抽出可疑共享对象和并发更新路径，用少量线程、同一批 key、足够多轮循环去触发；用 `yield` / `sleep_for` 或 `enable_adaptive_delay` 增大交错窗口。扰动提高观察概率，不修复问题。
10. **数据竞争和逻辑竞争要分开。** 数据竞争是“同一内存位置、至少一写、没有 happens-before”，是 UB，TSan 的主业；逻辑竞争是“每次访问都有锁、但业务不变量仍被交错破坏”，TSan 通常沉默。修复前者靠锁 / 原子操作，修复后者靠协议、临界区设计和业务断言。
11. **无告警不是绝对正确。** 动态检测只覆盖实际执行路径；模型检查和代码审查补足未执行路径，并发测试和业务断言补足逻辑竞争。这是节点 tradeoffs，也是 acceptance 第二条。
12. **回归要测“这一类问题不再出现”。** 用 `BUGGY_CACHE` 切换两种实现，脚本对 buggy 期望 TSan 退出码 66、对 fixed 期望 0；同时让 fixed 的业务计数校验通过。它证明的是缓存并发更新这一类数据竞争被修复，不是整个服务并发正确。
13. **为什么后端要懂：把随机线上问题转成可重现证据。** 文章缓存半写、阅读数丢失更新、配置 / token 刷新读到空指针，这些“跑了很久才崩一次”的问题，不会因为难复现就消失。TSan 把两侧栈和共享对象摆到你面前，让排障从猜变成证据。

本课的主线可以压缩成一句话：**先用 TSan 把“这一次执行”的冲突访问记下来，再用最小复现和调度扰动让它稳定出现，最后分清数据竞争与逻辑竞争，既不放过证据，也不过度承诺。** 下一步可以继续沿着诊断能力往下走：如何为 TSan 设计低噪声的 CI 回归、如何用 ignorelist 精确排除第三方代码，以及如何把并发业务不变量写成可执行的断言。
