# 第 33/208 课：进程、fork与exec

> 领域 C（C 语言）· 阶段 1 · 难度：基础 · 子类：进程、线程与调度
> 前置：a-os（操作系统）、b-syntax（C 语法）

**一句话**：进程是资源与隔离单位，fork 创建新的执行上下文，exec 替换进程映像。

本课以 Linux/POSIX 的用户态语义为主。`fork`、`execve`、`waitpid` 的接口来自 POSIX，但写时复制、`clone` 实现、`/proc` 字段等细节属于 Linux 实现；换到其他 Unix 或不同 libc 时，应以对应文档为准。

## 一、学习目标与为什么后端要懂

学完本课你应该能：

1. 解释 **PID 与父子关系**：`fork` 的三种返回值、父进程与子进程各自的 PID/PPID、孤儿进程和僵尸进程的区别。
2. 能独立完成：**实现小型命令启动器，收集退出码并回收所有子进程。**

验收标准：

- 能解释 **exec 成功后为何不返回**；
- **不会留下僵尸进程**。

为什么后台要懂：节点给的理由是「理解服务启动、子进程管理和僵尸进程」。后台服务不只是在循环里处理请求，它还经常要拉起辅助进程：启动脚本、执行运维命令、调用外部工具、把重任务放到独立 worker 中。只要有 `fork`，就有父子关系、退出状态和回收责任。

在公众号后台这类长驻服务里，可以类比这些场景：异步任务执行器调用外部处理程序；定时任务做日志切割或数据导出；某个高风险操作需要放进独立进程，避免拖垮主服务。典型事故也很具体：

- 父进程 fork 了子进程，却没有 `wait`，子进程退出后变成僵尸，长期积累最终耗尽进程表，`fork` 返回 `EAGAIN`；
- 子进程 `exec` 失败后没有退出，继续执行父进程后续代码，出现两个“服务实例”；
- 父进程把监听 socket 继承给了外部命令，外部命令意外持有端口或文件描述符；
- 多线程服务里直接照搬单线程的 `fork + printf` 模板，子进程在 `malloc` 或日志锁上死锁。

所以本课不是只记 API，而是要把 **创建、替换、等待、回收** 这条链路走通。

## 二、从直觉到原理

### 2.1 要点一：PID 与父子关系

**直觉**：进程像操作系统分配的一间独立工作间。它有门牌号（PID）、自己的内存、文件描述符、当前目录、环境变量和权限身份。`fork` 相当于从当前工作间复制出一个新的执行上下文，父子各自有门牌，父进程会拿到子进程的门牌号。

**原理**：

- 进程是**资源分配与隔离**的单位。每个进程有独立的虚拟地址空间、文件描述符表、信号处置、工作目录、`umask`、凭证等。
- `PID` 是进程标识；`PPID` 是父进程标识。`getpid()` 返回自己的 PID，`getppid()` 返回父进程 PID。
- `fork()` 在父进程中返回**子进程的 PID（大于 0）**；在子进程中返回 **0**；失败返回 **-1**，并设置 `errno`，此时没有创建出子进程。
- 父子进程从 `fork` 之后的下一条语句继续执行，谁先运行由调度器决定，**不能假设子进程一定先跑或父进程一定先跑**。
- 如果父进程先退出，子进程会成为孤儿进程，通常被重新托付给最近的 subreaper 或 PID 1。它的 PPID 会变化，后续由新父进程负责回收。容器里如果 PID 1 不回收子进程，僵尸可能一直留着。
- PID 会被复用。不要把 PID 当作永久唯一身份存进数据库；需要长期标识时，应加上启动时间或自己的业务 ID。

先看一段短的 `fork` 示例。为了让 `ps`/`pstree` 有机会观察到子进程，子进程会 `sleep(2)`：

```c
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }

    if (pid == 0) {
        printf("child : pid=%ld ppid=%ld\n",
               (long)getpid(), (long)getppid());
        fflush(stdout);
        sleep(2);
        _exit(0);
    }

    printf("parent: pid=%ld child=%ld\n",
           (long)getpid(), (long)pid);
    fflush(stdout);

    int status;
    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        return 1;
    }
    if (WIFEXITED(status))
        printf("child exit=%d\n", WEXITSTATUS(status));
    return 0;
}
```

编译并运行：

```bash
gcc -std=c11 -Wall -Wextra -Wpedantic fork_demo.c -o fork_demo
./fork_demo
```

观察进程树和父子关系：

```bash
./fork_demo &
p=$!
sleep 0.5
pstree -p "$p"
ps -o pid,ppid,stat,cmd -p "$p" --ppid "$p"
wait "$p"
```

预期能看到父进程和它的子进程，子进程的 PPID 等于父进程 PID。若机器没装 `pstree`，可以用 `ps -eo pid,ppid,stat,cmd --forest` 代替。也可以直接看 `/proc`：

```bash
grep -E '^(Name|State|Pid|PPid|Threads):' /proc/$$/status
```

用 `strace` 看系统调用：

```bash
strace -f -e trace=process ./fork_demo
```

Linux 上可能看到 `clone` 而不是 `fork`，这是 glibc 用 `clone` 实现 `fork` 的结果；语义仍然是创建一个新的、拥有独立地址空间的子进程。加上 `-f` 是为了让 `strace` 跟随子进程。

### 2.2 要点二：fork 的写时复制

**直觉**：看起来 `fork` 把整个内存都复制了一遍，实际 Linux 通常先让父子共享同一批物理页，并把这些页标成只读。谁要写，谁就触发缺页，内核才把那一页复制出来。像两个人共享一本只读账本，谁要改，谁就复印自己那一页。

**原理**：

- `fork` 创建子进程时，会复制页表、虚拟内存区域描述、文件描述符表等元数据，但物理页通常不立即复制。
- 父子进程的页表项先指向同一批物理页，并设为只读。任意一方写入时触发缺页异常，内核分配新物理页、复制原内容，再让写入方指向新页。
- 因此从程序语义看，父子进程的地址空间是**逻辑独立**的：子进程改全局变量，父进程看不到；父进程改，子进程也看不到。虚拟地址甚至可能打印得一样，但背后已经是不同的物理页。
- 写时复制不是零成本。`fork` 仍要复制页表和进程结构，可能造成 TLB 失效；如果父子都大量写入，复制最终会发生。`fork` 后马上 `exec` 的典型启动场景中，旧地址空间很快被替换，写时复制通常比较划算。
- 文件描述符要单独理解：`fork` 复制的是**描述符表**，父子同号的 fd 指向同一个打开文件描述（open file description），所以文件偏移和部分状态是共享的；但在子进程 `close(fd)` 只关闭子进程自己的描述符，不会关闭父进程的。

下面的代码验证“内存独立”：

```c
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int global_value = 1;

int main(void)
{
    setvbuf(stdout, NULL, _IONBF, 0);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        return 1;
    }

    if (pid == 0) {
        global_value = 2;
        printf("child : pid=%ld global=%d &global=%p\n",
               (long)getpid(), global_value, (void *)&global_value);
        sleep(2);
        _exit(0);
    }

    int status;
    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        return 1;
    }
    printf("parent: pid=%ld global=%d &global=%p\n",
           (long)getpid(), global_value, (void *)&global_value);
    return 0;
}
```

编译运行：

```bash
gcc -std=c11 -Wall -Wextra -Wpedantic fork_cow.c -o fork_cow
./fork_cow
```

预期子进程打印 `global=2`，父进程打印 `global=1`；两者的 `&global` 往往相同，因为虚拟地址布局被继承，但对应的是不同物理页。再用 `strace` 看：有 `clone`/`fork` 和 `wait4`，没有 `execve`，说明这是纯 `fork`，没有替换映像。

```bash
strace -f -e trace=process ./fork_cow
```

工程上还要注意两点：

1. **stdio 缓冲区也会被继承**。如果 `fork` 前标准输出缓冲区里有未刷新数据，子进程会拿到一份副本，之后父子都刷新就可能重复输出。规则是：`fork` 前按需 `fflush`，子进程在 `exec` 失败或正常结束时优先用 `_exit`，避免重复刷新。
2. **fd 的 `FD_CLOEXEC` 很重要**。服务器监听 socket、日志文件、数据库连接如果不希望被外部命令继承，应设置 `FD_CLOEXEC`，或使用带 `O_CLOEXEC` 的 `open`/`pipe2`/`accept4`。否则 `exec` 后这些 fd 仍在子进程里，既可能泄漏，也可能阻止端口释放。

### 2.3 要点三：exec、wait 与退出状态

#### exec：替换映像，不是创建新进程

**直觉**：`fork` 只是复制出另一个执行上下文；如果要在里面运行另一个程序，需要 `exec` 把当前进程的代码、数据、堆、栈换成新程序。像同一个人换了一份剧本，门牌号（PID）不变。

**原理**：

- `exec` 家族最终都落到 `execve(path, argv, envp)`。`execvp` 会按 `PATH` 搜索可执行文件，并继承当前环境；`execve` 使用显式路径和环境。
- `execve` 成功后，当前进程的旧代码段、数据段、堆、栈都被新程序替换，旧程序的调用栈和返回地址不复存在，所以**没有地方可以“返回”**。它不是“决定不返回”，而是旧程序已经不存在了。
- PID、PPID、进程组、会话、已打开的文件描述符（除非设置了 `FD_CLOEXEC`）等会保留；被捕获的信号处置通常会恢复默认，被忽略的信号处置通常保持忽略。新程序从入口点开始执行。
- `argv` 是以 `NULL` 结尾的字符串数组，约定 `argv[0]` 是程序名。`envp` 同样以 `NULL` 结尾。
- `execve` 失败才返回 **-1**，并设置 `errno`。失败后旧程序还在运行，必须处理错误并退出，不能在子进程里继续执行父进程的后续逻辑。

```c
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int main(void)
{
    char *argv[] = { "echo", "hello from exec", NULL };
    execvp(argv[0], argv);
    fprintf(stderr, "execvp failed: %s\n", strerror(errno));
    return 127;
}
```

```bash
gcc -std=c11 -Wall -Wextra -Wpedantic exec_demo.c -o exec_demo
./exec_demo
strace -f -e trace=execve ./exec_demo
```

成功时只会看到 `echo` 的输出，`fprintf` 那一行不会执行。`strace` 会显示 `execve` 调用成功；`execvp` 的 `PATH` 搜索在底层也会表现为多次 `execve` 尝试。

**回答验收问题“exec 成功后为何不返回”**：因为 `execve` 成功后，当前进程的整个用户态映像被新程序替换，旧程序的代码、栈帧和返回地址都已被丢弃。内核把控制权交给新程序入口，旧调用链没有可恢复的上下文。只有 `execve` 失败时，控制流才会以 `-1` 返回到调用点。

#### fork + exec：启动外部命令的标准骨架

后台启动外部命令，通常是“父进程 fork，子进程 exec，父进程 wait”：

```c
pid_t pid = fork();
if (pid < 0) {
    perror("fork");
} else if (pid == 0) {
    execvp(argv[0], argv);
    write(STDERR_FILENO, "exec failed\n", 12);
    _exit(127);
} else {
    int status;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
        ;
}
```

子进程里 `exec` 失败后必须 `_exit(127)`，不能 `return` 回父进程的代码。`write` 是异步信号安全的，比在子进程里调用复杂日志函数更稳妥。

#### wait 与退出状态

子进程退出后，内核并不会立刻丢掉它的所有信息，而是保留一个最小记录，里面至少有 PID 和退出状态，直到父进程读取。这个“已退出但还没被回收”的状态就是僵尸进程。

父进程用 `wait`/`waitpid` 读取并回收：

```c
int status;
pid_t pid = waitpid(child_pid, &status, 0);
if (pid < 0) {
    perror("waitpid");
} else if (WIFEXITED(status)) {
    int code = WEXITSTATUS(status);
    printf("normal exit: %d\n", code);
} else if (WIFSIGNALED(status)) {
    int sig = WTERMSIG(status);
    printf("killed by signal: %d\n", sig);
}
```

要点：

- 先判断 `WIFEXITED`，再取 `WEXITSTATUS`；先判断 `WIFSIGNALED`，再取 `WTERMSIG`。不要直接读 `status`。
- `exit(300)` 这类退出码通常只有低 8 位对父进程可见；不要用退出码传递大的整数。shell 常把“被信号杀死”显示成 `128 + 信号编号`，这是 shell 的约定。
- `wait(&status)` 等待任意一个子进程；`waitpid(pid, ...)` 等待指定子进程；`waitpid(-1, ...)` 等待任意子进程。
- 父进程只能等待自己的直接子进程。孤儿被重新托付后，要由新的父进程回收。
- `waitpid` 可能被信号中断并返回 `-1`/`EINTR`，应重试。若使用 `WNOHANG`，返回 0 表示“有子进程但都还没退出”，返回 `-1`/`ECHILD` 表示没有可等待的子进程。

回收所有子进程的常见循环：

```c
for (;;) {
    int status;
    pid_t pid = waitpid(-1, &status, 0);

    if (pid > 0) {
        if (WIFEXITED(status)) {
            printf("exit %d\n", WEXITSTATUS(status));
        } else if (WIFSIGNALED(status)) {
            printf("signal %d\n", WTERMSIG(status));
        }
        continue;
    }
    if (pid < 0 && errno == EINTR)
        continue;
    if (pid < 0 && errno == ECHILD)
        break;
    perror("waitpid");
    break;
}
```

长驻服务不要在主循环里无脑阻塞 `wait`。更常见的做法是用 `SIGCHLD`、self-pipe、`signalfd` 或事件循环唤醒，再用 `waitpid(-1, ..., WNOHANG)` 循环回收，直到返回 0 或 `ECHILD`。信号处理函数里只做尽量简单的事，不要在里面对日志框架、锁或堆做复杂操作。

#### 僵尸进程的观察

```bash
ps -o pid,ppid,stat,cmd -p <child_pid>
ps -eo pid,ppid,stat,cmd | awk '$3 ~ /^Z/'
```

僵尸的 `STAT` 通常是 `Z` 或 `Z+`，命令名后面带 `<defunct>`。它已经死了，`kill -9` 不会让它消失；只能由父进程 `wait` 回收，或让父进程退出、由新的父进程（通常是 PID 1 或 subreaper）回收。容器里要特别确认 PID 1 是否会回收孤儿。

## 三、常见误区与设计权衡

### 3.1 常见误区：多线程进程 fork 后，子进程可安全调用的函数受限

节点原文就是：**多线程进程 fork 后子进程可安全调用的函数受限。**

原因不是“子进程没有内存”，而是 `fork` 只复制**调用 fork 的那个线程**。其他线程在子进程里不存在，但它们持有的互斥锁、`malloc` 内部锁、stdio 锁、日志框架锁不会自动释放。子进程如果调用 `printf`、`malloc`、`pthread_mutex_lock`、`syslog` 等，可能永远等在一个“已经被消失的线程持有”的锁上。

因此，在多线程进程里 `fork` 之后、`exec` 之前，子进程只能安全调用**异步信号安全（async-signal-safe）**的函数，例如 `write`、`_exit`、`execve`、`close` 等。`printf`、`malloc`、`strerror`、`execvp` 等不在保证范围内。失败的子进程应该用 `write` 写固定消息，然后 `_exit(127)`，不要在子进程里走复杂日志和错误格式化。

工程上的选择：

- 多线程服务优先使用 `posix_spawn`/`posix_spawnp` 这类专门的 spawn 封装，或在父进程里把可执行文件路径解析好，子进程只做 `execve`；
- 在创建线程之前完成需要 `fork` 的初始化；
- 如果子进程不 `exec` 而要继续运行，必须只调用异步信号安全函数并自己重建必要状态，这通常很难，不是推荐做法；
- 本课实践程序是单线程的，所以可以使用 `execvp` 简化 `PATH` 搜索；把这段代码直接搬进多线程服务是不严谨的。

### 3.2 其他常见误区

- **“exec 会创建新进程”**：不会。它替换当前进程映像，PID 不变；创建新进程的是 `fork`。
- **“fork 后父子变量共享”**：普通内存不共享，写时复制保证逻辑独立；共享的是打开文件描述等内核对象。
- **“写时复制等于零成本”**：页表复制、缺页和后续复制都有成本，只是避免了立即复制所有物理页。
- **“子进程退出就自动消失”**：父进程不 `wait` 就会留下僵尸；父进程先退出则变成孤儿，交给新父进程回收。
- **“僵尸可以 kill 掉”**：僵尸已经退出，`kill` 无效；必须回收。
- **“PID 是永久唯一 ID”**：PID 会复用，不能作为长期身份标识。
- **“退出码可以是任意 int”**：父进程通常只能看到低 8 位，传递大整数会截断。

### 3.3 设计权衡：子进程隔离清晰，但启动和通信有成本

节点原文是：**子进程隔离清晰，但启动和通信有成本。** 这句话在后台设计里非常实用。

| 维度 | 子进程 | 线程/进程内调用 |
|---|---|---|
| 地址空间 | 独立，默认不共享内存 | 共享地址空间 |
| 故障隔离 | 子进程崩溃通常不拖垮父进程 | 一个线程崩溃可能带走整个进程 |
| 权限与资源 | 可单独降权、限制资源、`chroot` | 共享进程凭证和资源限制 |
| 启动成本 | 页表复制、`exec` 装载、动态链接，通常更重 | 创建线程或直接调用通常更轻 |
| 通信成本 | 管道、socket、共享内存，需要协议和序列化 | 直接函数调用或共享内存，成本低 |
| 生命周期 | 需要处理 fork/exec/wait/信号/僵尸 | 需要处理锁、竞态、线程安全 |
| 适用场景 | 外部命令、不可信插件、重任务、需要强隔离 | 高频、短小、可信的内部逻辑 |

所以不要因为“进程隔离好”就在每个请求里 `fork + exec`。高频短任务更适合常驻 worker、进程池或线程池；确实需要外部程序时，可以用进程池保持常驻，通过管道/socket 通信，而不是每次重新装载。反过来，如果任务可能崩溃、需要不同权限、或要调用不受控的第三方程序，进程隔离带来的清晰边界往往值得这些成本。

公众号后台的异步任务、媒体处理、外部工具调用等场景，可以按这个维度判断：**隔离收益是否大于启动和 IPC 成本**。如果答案是否定的，就别把进程当成免费的函数调用。

## 四、动手实践：实现小型命令启动器

### 4.1 目标与验收

实现一个 `launcher`：

```text
./launcher cmd [args...] [-- cmd [args...]]
```

- 每个被 `--` 分隔的片段启动一个子进程；
- 子进程用 `fork + execvp` 执行命令；
- 父进程收集并打印每个子进程的退出码或终止信号；
- 父进程回收**所有**已启动的子进程；
- 全部子进程正常退出且退出码为 0 时，launcher 返回 0；否则返回 1；用法错误返回 2；
- 运行结束后不能留下僵尸进程。

验收时你要能回答：**为什么 exec 成功后不返回？** 并能用 `ps`/`strace` 证明：**不会留下僵尸进程。**

### 4.2 第 1 步：准备目录

```bash
mkdir -p ~/c-lab/33-process
cd ~/c-lab/33-process
```

### 4.3 第 2 步：编写 `launcher.c`

```c
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc < 2) {
        fprintf(stderr, "usage: %s cmd [args...] [-- cmd [args...]]\n", argv[0]);
        return 2;
    }

    char ***starts = calloc((size_t)argc, sizeof(*starts));
    if (starts == NULL) {
        perror("calloc");
        return 1;
    }

    size_t ncmds = 0;
    int i = 1;
    while (i < argc) {
        if (strcmp(argv[i], "--") == 0) {
            i++;
            continue;
        }

        starts[ncmds++] = &argv[i];
        while (i < argc && strcmp(argv[i], "--") != 0)
            i++;
        if (i < argc) {
            argv[i] = NULL;
            i++;
        }
    }

    if (ncmds == 0) {
        fprintf(stderr, "usage: %s cmd [args...] [-- cmd [args...]]\n", argv[0]);
        free(starts);
        return 2;
    }

    size_t started = 0;
    int ok = 1;

    for (size_t k = 0; k < ncmds; k++) {
        pid_t pid = fork();
        if (pid < 0) {
            perror("fork");
            ok = 0;
            break;
        }
        if (pid == 0) {
            execvp(starts[k][0], starts[k]);
            static const char msg[] = "launcher: exec failed\n";
            ssize_t n = write(STDERR_FILENO, msg, sizeof(msg) - 1);
            (void)n;
            _exit(127);
        }
        started++;
    }

    size_t remaining = started;
    while (remaining > 0) {
        int status;
        pid_t pid = waitpid(-1, &status, 0);

        if (pid < 0) {
            if (errno == EINTR)
                continue;
            perror("waitpid");
            ok = 0;
            break;
        }

        remaining--;

        if (WIFEXITED(status)) {
            int code = WEXITSTATUS(status);
            printf("pid %ld: exit %d\n", (long)pid, code);
            if (code != 0)
                ok = 0;
        } else if (WIFSIGNALED(status)) {
            int sig = WTERMSIG(status);
            printf("pid %ld: signal %d\n", (long)pid, sig);
            ok = 0;
        } else {
            printf("pid %ld: other status\n", (long)pid);
            ok = 0;
        }
    }

    free(starts);
    return ok ? 0 : 1;
}
```

设计说明：

- 这里直接修改 `argv`，把每个 `--` 改成 `NULL`，自然得到每段命令的 `NULL` 结尾参数数组，不需要为每个参数再分配内存。
- 子进程 `execvp` 失败后只调用 `write` 和 `_exit(127)`。本程序是单线程的，使用 `execvp` 可以接受；多线程服务应改成 `posix_spawn`，或预先解析路径后在子进程调用 `execve`。
- 父进程用 `waitpid(-1, ...)` 回收任意先退出的子进程，而不是按启动顺序阻塞等待某一个。这样先退出的子进程能及时被回收，不会因为父进程还在等别人而短暂变成僵尸。
- 如果 `fork` 中途失败，`started` 记录的是已经成功启动的数量，后面的循环仍会回收这些子进程。

### 4.4 第 3 步：编译

```bash
gcc -std=c11 -Wall -Wextra -Wpedantic -O2 launcher.c -o launcher
```

### 4.5 第 4 步：运行多个命令并收集退出码

```bash
./launcher /bin/echo hello -- /bin/sh -c 'exit 7' -- /bin/false
printf 'launcher exit=%d\n' "$?"
```

预期会看到：

- `hello`；
- 三个子进程的结果，分别类似 `exit 0`、`exit 7`、`exit 1`；
- launcher 最终返回 1。

具体打印顺序不固定，因为三个子进程的退出顺序由调度决定。第 2 个子进程的退出码 7 是 `exec` 后的 `/bin/sh` 自己 `exit 7` 产生的；父进程通过 `WEXITSTATUS` 取到它。

### 4.6 第 5 步：验证 exec 失败与信号终止

`execvp` 找不到命令时，子进程会走失败路径：

```bash
./launcher /definitely/not/here
printf 'launcher exit=%d\n' "$?"
```

预期看到 `launcher: exec failed`，以及该子进程 `exit 127`；launcher 自身返回 1。注意区分：**127 是子进程的退出码，launcher 只是汇总为失败状态 1。**

验证被信号杀死：

```bash
./launcher /bin/sh -c 'kill -TERM $$'
printf 'launcher exit=%d\n' "$?"
```

预期看到 `signal 15`，launcher 返回 1。这里父进程通过 `WIFSIGNALED` 和 `WTERMSIG` 识别信号，而不是把 `status` 当普通整数读。

### 4.7 第 6 步：验证没有僵尸

启动一个 2 秒的子进程，同时让另一个命令立即结束：

```bash
./launcher /bin/sh -c 'sleep 2' -- /bin/true &
p=$!
sleep 0.5
ps -o pid,ppid,stat,cmd --ppid "$p"
pstree -p "$p"
wait "$p"
printf 'launcher exit=%d\n' "$?"
```

在 `sleep 0.5` 时，`/bin/true` 应该已经被父进程回收；仍在运行的是 `sleep` 对应的子进程。`ps` 输出中不应出现 `Z` 或 `<defunct>`。如果看到僵尸，通常说明父进程只 `wait` 了部分 PID，或者忽略了某个子进程。

再用 `strace` 看完整的生命周期：

```bash
strace -f -e trace=process ./launcher /bin/sh -c 'exit 7' -- /bin/false
```

预期能看到子进程的 `clone`（或 `fork`）、`execve`、`exit_group`，以及父进程的 `wait4`。每个启动的子进程最终都有对应的回收动作，这就是“不会留下僵尸”的直接证据。

### 4.8 自查清单

- [ ] `execvp` 成功后，子进程运行的是新程序，原来的 `launcher` 代码不会继续执行；
- [ ] `execvp` 失败时，子进程用 `_exit(127)` 退出，而不是 `return` 回父进程逻辑；
- [ ] 每个成功 `fork` 的子进程都进入回收循环，没有遗漏；
- [ ] 用 `ps` 观察不到本程序留下的 `Z`/`<defunct>`；
- [ ] 能解释 `waitpid(-1, ...)` 为什么能在子进程先退出时及时回收；
- [ ] 能说明多线程服务为什么不能直接照搬这里的 `execvp` 子进程代码。

## 五、自测题

### 题 1

`fork()` 在父进程和子进程中的返回值分别是什么？返回 `-1` 意味着什么？子进程退出后、父进程尚未 `wait` 时处于什么状态？如果父进程先退出，子进程的 PPID 会怎样？

**答案**：

父进程中 `fork` 返回子进程 PID（大于 0）；子进程中返回 0；返回 -1 表示创建失败，`errno` 给出原因，此时没有新子进程。子进程已退出但父进程尚未回收时是僵尸进程，`ps` 中通常显示 `Z`/`<defunct>`。父进程先退出时，子进程成为孤儿，通常被重新托付给最近的 subreaper 或 PID 1，PPID 随之改变，由新父进程负责回收；容器中的 PID 1 必须承担回收责任。

### 题 2

`fork` 后父进程和子进程修改同一个全局变量，为什么互不影响？但父进程打开的同一个文件描述符，文件偏移为什么又会互相影响？

**答案**：

普通内存使用写时复制：父子逻辑上拥有独立地址空间，物理页先只读共享，任一方写入时内核才复制该页，所以全局变量各自独立。文件描述符表虽然被复制，但同号 fd 指向同一个打开文件描述（open file description），文件偏移和部分状态保存在这个共享对象里，因此一方 `read`/`write` 推进偏移，另一方会看到；在子进程 `close(fd)` 只关闭子进程自己的描述符，不会关闭父进程的。

### 题 3

为什么 `execve` 成功后不会返回？如果失败，子进程应如何处理？多线程进程 `fork` 后，为什么子进程不能随便调用 `printf`、`malloc` 或 `execvp`？

**答案**：

`execve` 成功后，当前进程的代码、数据、堆、栈被新程序替换，旧调用栈和返回地址已经不存在，所以没有旧上下文可以返回；PID 等进程属性保留，但运行的是新程序。失败时 `execve` 返回 -1 并设置 `errno`，子进程应在失败路径用异步信号安全的 `write` 写固定错误信息，然后 `_exit(127)`，不能继续执行父进程后续代码。多线程进程 `fork` 后只有调用线程被复制到子进程，其他线程消失但它们持有的 `malloc`/stdio/日志锁可能仍然锁着，因此子进程在 `exec` 前只能调用异步信号安全函数；`printf`、`malloc`、`execvp` 等不在保证范围内，可能死锁。多线程服务应优先使用 `posix_spawn` 一类封装，或在父进程解析好路径后让子进程只做 `execve`。

## 六、推荐资源

- [进程、fork与exec：Linux man-pages](https://man7.org/linux/man-pages/man2/fork.2.html) — man7.org，英文，文档，基础，免费。重点阅读 PID 与父子关系、`fork` 的写时复制，用于核对本节点实验的语义和边界。

阅读时按本节点的 sourceStudy 要求做核对：

1. 沿着资源中的定义、示例和错误处理路径，对照本文的 `fork` 返回值、COW 语义和 `wait` 回收逻辑；
2. 在本地补充 `man 2 execve`、`man 2 wait`、`man 7 signal-safety`，核对 `exec` 成功/失败路径和多线程 `fork` 的安全函数限制；
3. 记录实验环境的适用版本与边界条件，例如 `uname -r` 的内核版本、`ldd --version` 的 libc 版本、是否在容器中运行，以及 PID 1 是否会回收孤儿进程。写时复制、`clone` 实现和 `/proc` 字段是 Linux 细节，不要直接套到所有 Unix。

## 附：已验证的参考练习（practice33）

除了上面的 `launcher`，这里还有一套我在 Linux（gcc）上跑通的小测试，位置在 `/workspace/atlas/practice33/`，运行 `./test.sh` 会依次验证：

1. `cow.c`：fork 后子进程把 `counter` 改成 101，父进程仍是 100，两边打印的 `&counter` 地址相同（写时复制：虚拟地址相同，物理页各自独立）。
2. `launcher.c`（另一种写法：每个参数是一条用空格分词的命令）：同时启动 `true`、`ls /no/such/path`、`no_such_cmd_xyz`、`sleep 0.2`、被 `kill -9` 的 shell，结果 `started=5 reaped=5 failed=3`，退出码分别是 0、2、127（exec 失败）、0，以及 `signal=9`。
3. 启动器运行期间用 `ps --ppid` 能看到子进程，跑完后检查该启动器名下的僵尸数为 0。
4. 反例 `zombie.c`：父进程 fork 后不 wait 就睡 2 秒，`ps` 能看到子进程状态是 `Z`、命令名为 `[zombie] <defunct>`。

最后输出 `ALL_CHECKS_PASSED`。
