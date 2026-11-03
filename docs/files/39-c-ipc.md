# 第 39/208 课：管道、共享内存与IPC

> 节点：`c-ipc`｜领域：C｜阶段：2｜难度：基础｜子类：通信与运行时  
> 摘要：IPC 为隔离进程提供数据或事件通道，不同机制在复制、同步和故障隔离上各有成本。  
> 为什么要学：实现进程间任务分发及控制通信。  
> 前置知识：`c-fd`（文件描述符）、`c-vm`（虚拟内存）

---

## 1. 学习目标与为什么后端要懂

学完本课，你应该能够：

1. 解释 `pipe` 和 Unix 域 Socket 的工作方式、适用场景与语义差异。
2. 独立完成：用 Unix 域 Socket 做父子进程任务通信，并对比共享内存在大块数据传输上的成本。
3. 说清楚“字节流”和“消息边界”的区别，能自己设计长度前缀等分帧协议。
4. 正确处理对端退出：`read` 返回 `0` 表示 EOF，写端可能收到 `EPIPE`，默认还会触发 `SIGPIPE`。
5. 使用 `shm_open` + `mmap(MAP_SHARED)` 建立共享内存，并为读写设计明确的同步方案。

### 为什么后端工程师要懂 IPC

进程之间默认是隔离的：各自有独立的虚拟地址空间和文件描述符表。后端系统却经常需要“隔离，但协作”：

- **master/worker 任务分发**：master 进程接收请求，把任务分给多个 worker；worker 通过 Unix 域 Socket 回传结果或心跳。
- **本机 agent 与业务进程通信**：监控 agent、日志采集器、配置下发进程需要和业务进程交换控制消息。
- **大块数据低复制传递**：图片、日志批、序列化后的消息体，如果每次都经过内核复制，成本会很明显，共享内存可以作为优化手段。
- **故障隔离**：把不稳定逻辑放进子进程，通过 IPC 控制它、观察它退出，而不是让整个服务一起崩溃。

关键点是：**IPC 不只是“怎么把字节送过去”，还包括边界、同步、生命周期和故障处理**。这些才是线上问题的高发区。

---

## 2. 从直觉到原理：pipe 和 Unix 域 Socket

### 2.1 直觉：进程之间没有共享的“内存变量”

两个进程即使执行同一份程序，`int x` 也各自独立。想让 A 进程的数据被 B 进程看到，必须经过某种“通道”：

- 数据通道：pipe、Unix 域 Socket、共享内存、消息队列等。
- 事件通道：信号、eventfd、pipe 的可读事件等。

其中 pipe 和 Unix 域 Socket 都由内核维护缓冲区，数据从用户态复制到内核，再从内核复制到对端用户态。共享内存则把同一批物理页映射进多个进程的地址空间，省掉了这次复制，但同步责任转移给了使用者。

### 2.2 pipe：单向字节流

`pipe()` 创建一对文件描述符：

```c
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

int main(void) {
    int fd[2];
    if (pipe(fd) == -1) {
        perror("pipe");
        return 1;
    }

    pid_t pid = fork();
    if (pid == 0) {
        /* 子进程只写：关闭读端，避免持有无用 fd */
        close(fd[0]);
        const char *a = "hello";
        const char *b = "world";
        write(fd[1], a, strlen(a));
        write(fd[1], b, strlen(b));
        close(fd[1]);
        _exit(0);
    }

    /* 父进程只读：关闭写端，否则永远等不到 EOF */
    close(fd[1]);

    char buf[64];
    ssize_t n;
    while ((n = read(fd[0], buf, sizeof(buf))) > 0) {
        printf("read %zd bytes: %.*s\n", n, (int)n, buf);
    }
    if (n == -1) {
        perror("read");
    }
    close(fd[0]);
    waitpid(pid, NULL, 0);
    return 0;
}
```

这段代码很可能一次读到 `helloworld`，也可能分两次读到。**pipe 是字节流，没有消息边界**：两次 `write` 不保证对应两次 `read`。内核只保证字节顺序，不保证“一个 write 等于一个 read”。

几个必须知道的细节：

- `pipe(fd)` 中 `fd[0]` 是读端，`fd[1]` 是写端。
- 必须关闭不用的端。父进程不关闭写端时，子进程退出后父进程的 `read` 仍可能阻塞，因为写端还没全部关闭。
- 写端全部关闭后，读端 `read` 返回 `0`，这是 EOF，不是错误。
- 读端全部关闭后，继续写会失败并返回 `-1`，`errno == EPIPE`，同时默认产生 `SIGPIPE` 信号。若不处理，进程会被终止。
- 空 pipe 上读会阻塞；pipe 缓冲区满时写会阻塞（默认阻塞 fd 的情况下）。
- `PIPE_BUF` 以内的单次 `write` 在 Linux 上具有原子性；超过后多个写者可能交错。不要把“原子写”误当成“消息边界”。

观察 pipe 的常用命令：

```bash
# 查看进程持有的 fd，pipe 通常显示为 pipe:[inode]
ls -l /proc/<pid>/fd

# 查看进程打开的 fd 和类型
lsof -p <pid>

# 跟踪 pipe/read/write 等系统调用
strace -f -e trace=pipe,pipe2,read,write,close ./a.out

# 查看 Unix 域 Socket（包括部分本机连接）
ss -xp
```

`ls -l /proc/<pid>/fd` 的例子：

```text
lr-x------ 1 app app 64 Nov  3 10:00 0 -> /dev/null
l-wx------ 1 app app 64 Nov  3 10:00 1 -> pipe:[123456]
l-wx------ 1 app app 64 Nov  3 10:00 2 -> pipe:[123457]
```

注意：`/proc/<pid>/fd` 里的 `pipe:[...]` 展示的是 fd 指向的 pipe 对象，不能直接读出内容。

### 2.3 Unix 域 Socket：本机进程通信的主力

Unix 域 Socket（AF_UNIX）和 TCP Socket 的编程接口很像，但地址不是 IP + 端口，而是：

- **路径名**：如 `/run/myapp/agent.sock`，会在文件系统留下一个 socket 文件。
- **抽象命名空间**（Linux）：名字以 `\0` 开头，不在文件系统出现，进程退出自动释放，但可移植性较差。

创建方式：

```c
int fd = socket(AF_UNIX, SOCK_STREAM, 0);   /* 字节流、面向连接 */
int fd2 = socket(AF_UNIX, SOCK_SEQPACKET, 0); /* 保留消息边界、面向连接 */
int fd3 = socket(AF_UNIX, SOCK_DGRAM, 0);   /* 保留消息边界、无连接语义 */
```

三种类型的关键差异：

| 类型 | 是否有连接 | 是否保留消息边界 | 典型用途 |
|---|---|---|---|
| `SOCK_STREAM` | 是 | 否，纯字节流 | 通用 RPC、任务通道、需要自己分帧 |
| `SOCK_SEQPACKET` | 是 | 是，一次发送对应一次接收 | 本机有消息语义的协议 |
| `SOCK_DGRAM` | 否（可 `connect`） | 是 | 本机短消息、事件通知 |

需要特别澄清：

- `SOCK_STREAM` 与 TCP 一样，`read`/`recv` 可能只读到一部分，也可能把多次 `send` 合并。**必须自己做分帧**。
- `SOCK_SEQPACKET` 保留消息边界，且面向连接、可靠有序；适合“每条消息是一个完整任务”的本机通信。
- `SOCK_DGRAM` 保留消息边界，但它是数据报语义，不提供字节流那种“连接内有序字节”的抽象。Linux 的 AF_UNIX 数据报通常是可靠的，但仍要处理缓冲区满、对端未就绪、消息大小上限等情况，不能把它当无限队列。
- 无论哪种，**都不要假设一次 `read` 能读到完整业务消息**，除非你用的正是保留边界的类型并且按完整报文读取。

#### socketpair：父子进程全双工通信

`socketpair()` 直接创建一对已连接的 socket，非常适合 `fork()` 后的父子通信：

```c
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>

static int send_all(int fd, const void *buf, size_t len) {
    const char *p = buf;
    while (len > 0) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

int main(void) {
    int sv[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sv) == -1) {
        perror("socketpair");
        return 1;
    }

    pid_t pid = fork();
    if (pid == 0) {
        close(sv[0]);
        char buf[256];
        for (;;) {
            ssize_t n = read(sv[1], buf, sizeof(buf));
            if (n == 0) {
                /* 父进程关闭了写端：EOF */
                break;
            }
            if (n < 0) {
                if (errno == EINTR) continue;
                perror("read");
                break;
            }
            /* 这里只是示例：真实协议应先累积到完整帧 */
            printf("child got: %.*s\n", (int)n, buf);
            if (send_all(sv[1], "ack", 3) == -1) {
                break;
            }
        }
        close(sv[1]);
        fflush(stdout); /* _exit 不刷 stdio 缓冲；输出重定向到文件/管道时不刷就会丢 */
        _exit(0);
    }

    close(sv[1]);
    if (send_all(sv[0], "task-1", 6) == -1) {
        perror("send");
    }
    close(sv[0]); /* 关闭写端，让子进程读到 EOF */
    waitpid(pid, NULL, 0);
    return 0;
}
```

这个例子仍然只是字节流，`read` 不保证一次拿到完整 `task-1`。实际任务协议建议加长度前缀：

```text
+--------+-------------------+
| len(4) | payload(len bytes)|
+--------+-------------------+
```

接收端循环读取 4 字节长度，再循环读满 `len` 字节。这样才能把“字节流”还原成“消息”。

### 2.4 对端退出：EOF、EPIPE 与 SIGPIPE

这是 IPC 代码最常见的线上故障之一。

#### read 返回 0 是 EOF

对于 pipe 和 `SOCK_STREAM`，当所有写端都关闭后，读端 `read` 返回 `0`。这不是错误，表示对端不会再发送数据。

注意“所有写端”：

- 父进程 fork 后，子进程继承了 pipe 的写端；如果父子都持有写端，只关闭其中一个，读端仍不会 EOF。
- 使用 `socketpair` 时，父子各自关闭不用的那一端，避免 fd 泄漏和 EOF 延迟。

#### 写已关闭的对端：EPIPE

当读端全部关闭后，继续 `write`/`send` 会返回 `-1`，`errno = EPIPE`。同时，内核默认向写进程发送 `SIGPIPE`。`SIGPIPE` 的默认动作是终止进程。

处理方式：

```c
#include <signal.h>
#include <errno.h>
#include <sys/socket.h>

/* 方式一：进程级忽略 SIGPIPE，之后靠 EPIPE 判断 */
signal(SIGPIPE, SIG_IGN);

/* 方式二：send 时使用 MSG_NOSIGNAL（仅 socket，不适用于 pipe） */
ssize_t n = send(fd, buf, len, MSG_NOSIGNAL);
```

对 pipe 的 `write` 没有 `MSG_NOSIGNAL`，通常要全局忽略 `SIGPIPE`，或者在写入前用 `poll`/`select` 检查，但仍然存在竞态，所以最稳妥的是忽略信号并处理 `EPIPE`。

#### 关闭顺序与半关闭

`SOCK_STREAM` 支持半关闭：

```c
shutdown(fd, SHUT_WR); /* 我不再发送，但对端仍可发送 */
```

这适合“请求发送完毕，等待响应”的协议。注意 `shutdown` 与 `close` 不同：`close` 减少 fd 引用，可能有其他进程仍持有该 socket；`shutdown` 直接改变连接方向的状态。

---

## 3. 共享内存映射

### 3.1 直觉：把同一批物理页映射进多个地址空间

普通 IPC 的数据路径是：

```text
A 用户态 --copy--> 内核缓冲区 --copy--> B 用户态
```

共享内存的数据路径是：

```text
A 用户态虚拟页 ─┐
                ├─> 同一批物理页
B 用户态虚拟页 ─┘
```

省掉了内核与用户态之间的复制，但**没有自动解决同步**。两个进程同时写同一块内存，仍然是数据竞争。

### 3.2 shm_open + ftruncate + mmap(MAP_SHARED)

POSIX 共享内存的基本步骤：

1. `shm_open` 创建/打开一个共享内存对象，名字形如 `/myapp-tasks`。
2. `ftruncate` 设置对象大小。
3. `mmap` 以 `MAP_SHARED` 映射到当前进程地址空间。
4. 多个进程打开同一名字、映射同一对象，就能看到彼此的写入。
5. 用完 `munmap`，不再需要名字时 `shm_unlink`。

```c
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <pthread.h>
#include <semaphore.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#define SHM_NAME "/demo-ipc-shm"
#define SHM_SIZE (1 << 20) /* 1 MiB */

struct shared_region {
    pthread_mutex_t lock;   /* 必须初始化为进程共享 */
    sem_t ready;            /* 通知对方“有新数据” */
    size_t len;
    char data[SHM_SIZE];
};

int main(void) {
    int fd = shm_open(SHM_NAME, O_CREAT | O_RDWR, 0600);
    if (fd == -1) {
        perror("shm_open");
        return 1;
    }
    if (ftruncate(fd, sizeof(struct shared_region)) == -1) {
        perror("ftruncate");
        return 1;
    }

    struct shared_region *r = mmap(NULL, sizeof(*r),
                                   PROT_READ | PROT_WRITE,
                                   MAP_SHARED, fd, 0);
    if (r == MAP_FAILED) {
        perror("mmap");
        return 1;
    }
    close(fd); /* 映射建立后，fd 可以关闭；对象仍由映射持有引用 */

    /* 只在创建者/初始化阶段做一次；真实程序要避免多进程重复初始化 */
    pthread_mutexattr_t attr;
    pthread_mutexattr_init(&attr);
    pthread_mutexattr_setpshared(&attr, PTHREAD_PROCESS_SHARED);
    pthread_mutex_init(&r->lock, &attr);
    pthread_mutexattr_destroy(&attr);

    sem_init(&r->ready, 1 /* pshared */, 0);

    /* 写入方：加锁改数据，再发通知 */
    const char *msg = "large-payload";
    pthread_mutex_lock(&r->lock);
    r->len = strlen(msg);
    memcpy(r->data, msg, r->len);
    pthread_mutex_unlock(&r->lock);

    sem_post(&r->ready);

    /* 读取方示意：先等通知，再加锁读 */
    sem_wait(&r->ready);
    pthread_mutex_lock(&r->lock);
    printf("len=%zu data=%.*s\n", r->len, (int)r->len, r->data);
    pthread_mutex_unlock(&r->lock);

    munmap(r, sizeof(*r));
    shm_unlink(SHM_NAME);
    return 0;
}
```

上面的代码只演示 API 形态，不能直接当作多进程正确实现：真实场景中通常由创建者完成初始化，其他进程只打开并 `mmap`；还需要处理“初始化完成”的同步、进程异常退出、锁的健壮性等问题。

关键语义：

- `MAP_SHARED` 的修改对其他映射同一对象的进程可见；`MAP_PRIVATE` 是写时复制，不是共享内存。
- 共享内存对象的生命周期与文件名解绑：`shm_unlink` 只是删除名字，已有映射仍有效，直到 `munmap` 或进程退出。
- 访问超过 `ftruncate` 设置的大小可能触发 `SIGBUS`。
- 共享内存里不能放“本进程的指针”并让另一个进程直接使用；不同进程的虚拟地址可能不同。要传偏移量，不要传裸指针。
- 进程崩溃时可能持有锁不释放。需要评估 robust mutex、超时、看门狗或“锁 + 序号”等方案。

常用观察命令：

```bash
# 查看 /dev/shm 中的共享内存对象（名字去掉开头的 /）
ls -l /dev/shm

# 查看大小与占用
df -h /dev/shm

# 查看谁打开了共享内存对象
lsof /dev/shm/demo-ipc-shm

# 跟踪创建、截断、映射系统调用
strace -f -e trace=openat,ftruncate,mmap,munmap,shm_open ./a.out

# 查看进程的映射区域
cat /proc/<pid>/maps
```

`/dev/shm` 通常是 tmpfs，占内存而不是磁盘。共享内存对象如果没有 `shm_unlink`，会残留到系统重启；线上要把它当作需要清理的命名资源。

### 3.3 共享内存必须自己做同步

共享内存**不自动提供互斥，也不自动提供消息边界**。它提供的只是“同一批字节”。因此至少要考虑：

1. **互斥**：多个进程同时读写同一结构体时，用进程共享的 `pthread_mutex_t`（`PTHREAD_PROCESS_SHARED`）或 `sem_t`（`pshared=1`）。
2. **通知**：一方写完，另一方怎么知道？可以用：
   - 共享内存中的 `sem_post` / `sem_wait`；
   - 通过 Unix 域 Socket 发一条“有新数据”的小消息；
   - `eventfd`、pipe 等事件通道。
3. **可见性与顺序**：POSIX 的互斥锁/信号量操作提供必要的同步语义；如果自己用原子变量做无锁队列，需要非常谨慎地处理内存序、ABA、生命周期。
4. **边界**：共享内存是裸字节区，消息长度、消息类型、是否还有下一条，都要自己在布局里定义。
5. **生命周期**：创建、初始化、打开、映射、解除映射、删除名字、异常退出清理，每一步都要有明确责任方。

“锁 + 条件/信号量”是最常见的起点；“通过 socket 发通知”则把控制面和数据面分开：大数据放共享内存，小通知走 socket，避免轮询。

---

## 4. 协议边界和同步责任

### 4.1 为什么“字节流”必须分帧

pipe 和 `SOCK_STREAM` 只保证字节顺序，不保证读写次数对应。典型错误是：

```c
write(fd, &task, sizeof(task));   /* 发送端以为这是一条消息 */
read(fd, &task, sizeof(task));    /* 接收端可能只读到一半 */
```

正确做法是定义协议：

- **固定长度消息**：每条消息大小固定，读满即可。
- **长度前缀**：先读 4 字节长度，再读 payload；注意字节序。
- **分隔符**：如换行分隔，但 payload 内不能出现分隔符，且要限制最大长度。
- **自描述格式**：如 TLV（type-length-value）。

接收循环必须处理：

- 短读：`read` 返回小于请求的字节数。
- `EINTR`：被信号中断，重试。
- EOF：`read` 返回 `0`，停止并清理。
- 部分消息：连接断开时可能留下半条消息，不能当完整任务处理。
- 非阻塞 fd 的 `EAGAIN`/`EWOULDBLOCK`：交给事件循环，下次继续。

### 4.2 各机制的边界与同步责任对比

| 机制 | 数据复制 | 消息边界 | 同步责任 | 生命周期 |
|---|---|---|---|---|
| pipe | 有 | 无 | 内核提供阻塞/唤醒，但不提供消息互斥 | fd 关闭即释放 |
| Unix `SOCK_STREAM` | 有 | 无 | 连接状态由内核维护，应用负责分帧 | fd/socket 文件 |
| Unix `SOCK_SEQPACKET` | 有 | 有 | 应用负责请求/响应状态 | fd/socket 文件 |
| Unix `SOCK_DGRAM` | 有 | 有 | 应用负责重试、缓冲与对端就绪 | fd/socket 文件 |
| 共享内存 | 无（映射后直接访问） | 无 | 应用负责互斥、通知、内存序 | 名字 + 映射引用 |

一句话总结：**复制少不代表复杂度低，复杂度只是从内核转移到了应用。**

### 4.3 后台场景怎么选

- **master/worker 任务分发**：优先 Unix 域 Socket。它天然支持连接、EOF、权限控制（socket 文件权限）、多 worker 负载均衡；任务体小或中等时，复制成本可以接受。
- **本机 agent 与业务进程控制通信**：`SOCK_SEQPACKET` 或带长度前缀的 `SOCK_STREAM` 都合适；控制消息小，边界清晰比极致性能更重要。
- **大块数据传递**：共享内存 + 小通知通道。数据面零复制，控制面用 socket 或信号量通知。需要额外设计锁、版本号、生命周期和异常恢复。
- **简单父子单向输出**：pipe 足够；需要双向时用 `socketpair`。

---

## 5. 常见误区与设计权衡

### 5.1 常见误区

1. **把共享内存当消息队列**  
   共享内存没有消息边界，也没有自动互斥。写入方和读取方必须自己约定布局、长度、同步和通知。

2. **以为一次 `write` 对应一次 `read`**  
   pipe 和 `SOCK_STREAM` 都是字节流。必须分帧，并循环读写。

3. **忘记关闭不用的 fd**  
   父子进程都持有 pipe 写端时，读端等不到 EOF；socket 不关闭不用的端也会导致连接状态混乱。

4. **忽略 `SIGPIPE`**  
   对端关闭后写数据，默认可能直接杀死进程。应忽略 `SIGPIPE` 并处理 `EPIPE`，或使用 `MSG_NOSIGNAL`。

5. **把 `read` 返回 `0` 当成错误**  
   `0` 是 EOF。真正的错误是 `-1`，此时看 `errno`。

6. **在共享内存里传指针**  
   不同进程的虚拟地址不同，裸指针没有意义。应传偏移量或固定布局。

7. **共享内存不清理**  
   `/dev/shm` 里的对象会残留。需要明确 `shm_unlink` 的时机和权限。

8. **以为 `PIPE_BUF` 原子写等于消息边界**  
   它只保证小写入不被其他写者穿插，不保证读端一次读到一条完整消息。

9. **进程崩溃后锁永远不释放**  
   共享内存中的普通互斥锁可能被死进程持有。需要 robust mutex、超时或外部看门狗。

### 5.2 设计权衡

| 方案 | 优点 | 代价 |
|---|---|---|
| pipe | 简单、内核缓冲、天然 EOF | 单向、字节流、无消息边界 |
| Unix 域 Socket | 双向、连接管理、权限控制、支持多种语义 | 有复制开销、需要协议设计 |
| 共享内存 | 低复制、大块数据吞吐高 | 同步、生命周期、故障恢复复杂 |

低复制方案把复杂度转移到同步与生命周期：你需要自己保证“谁先写、谁后读、写到哪、什么时候可以覆盖、进程死了怎么办”。如果没有明确的性能瓶颈，**优先选择更简单、边界更清晰的 Socket**；只有在复制成本确实成为瓶颈时，再引入共享内存。

---

## 6. 动手实践：Unix 域 Socket 任务通信 vs 共享内存大块数据

目标：用 Unix 域 Socket 做父子进程任务通信，并用共享内存传大块数据做对比。以下步骤可直接执行，不必先追求完整框架，重点是语义正确。

### 步骤 1：建立父子进程任务通道

1. 创建 `socketpair(AF_UNIX, SOCK_STREAM, 0, sv)`。
2. `fork()`。
3. 父进程关闭 `sv[1]`，子进程关闭 `sv[0]`。
4. 定义任务协议：4 字节网络序长度 + payload。父进程发送任务，子进程读取并回 `ack`。
5. 接收端实现 `read_full(fd, buf, len)`，循环处理短读和 `EINTR`。

验收点：发送两条任务，接收端能正确切分；即使一次 `read` 只拿到部分数据，也能恢复出完整消息。

### 步骤 2：处理对端退出与 EOF

1. 子进程循环 `read`，当 `read` 返回 `0` 时打印 `peer closed (EOF)` 并退出。
2. 父进程发送完任务后 `close(sv[0])` 或 `shutdown(sv[0], SHUT_WR)`。
3. 再做一个反向实验：父进程提前关闭读端，子进程继续 `send`，观察 `EPIPE`。
4. 在程序开始处 `signal(SIGPIPE, SIG_IGN)`，确认进程不再被信号杀死，而是拿到 `EPIPE`。
5. 用 `strace -f -e trace=read,write,close,sendto,recvfrom ./demo` 观察系统调用。

验收点：

- 对端正常退出时，`read` 返回 `0`，程序不把它当错误。
- 写已关闭连接时，得到 `EPIPE`，程序能清理并退出。
- 没有因为 `SIGPIPE` 默认动作而崩溃。

### 步骤 3：共享内存传递大块数据

1. 父进程 `shm_open("/demo-ipc-shm", O_CREAT | O_RDWR, 0600)`，`ftruncate` 到足够大（例如 8 MiB）。
2. `mmap(..., MAP_SHARED, ...)`，把共享区布局定义为：

   ```c
   struct shared_region {
       pthread_mutex_t lock;
       sem_t ready;
       uint64_t seq;
       size_t len;
       char data[];
   };
   ```

3. 初始化进程共享互斥锁：

   ```c
   pthread_mutexattr_t attr;
   pthread_mutexattr_init(&attr);
   pthread_mutexattr_setpshared(&attr, PTHREAD_PROCESS_SHARED);
   pthread_mutex_init(&r->lock, &attr);
   pthread_mutexattr_destroy(&attr);
   sem_init(&r->ready, 1, 0);
   ```

4. 父进程加锁，写 `len` 和 `data`，解锁，`sem_post(&r->ready)`。
5. 子进程 `sem_wait(&r->ready)`，加锁读取，解锁。
6. 用 `seq` 或版本号检测覆盖/重复消费；明确“读完后能否被下一轮覆盖”的协议。
7. 结束后 `munmap`，父进程 `shm_unlink`。用 `ls -l /dev/shm` 和 `lsof /dev/shm/demo-ipc-shm` 检查残留。

验收点：

- 共享内存读写有明确的同步：同一时刻只有一个写者修改数据；读取方在收到通知后才读。
- 数据长度、序号、覆盖规则在协议中有定义，不依赖“碰巧没冲突”。
- 进程异常退出时，有清理或恢复策略（至少能检测到残留并处理）。

### 步骤 4：对比两种方式

1. 分别用 Socket 和共享内存传输 1 KiB、1 MiB、64 MiB 数据。
2. 记录耗时、CPU 占用、系统调用次数（可用 `strace -c`）。
3. 观察小数据量下 Socket 是否足够；大数据量下共享内存的优势是否明显。
4. 记录额外复杂度：分帧、锁、通知、生命周期、异常恢复。

验收点：能用自己的话解释“为什么共享内存快”和“为什么共享内存更难”，而不是只记住结论。

### 步骤 5：整理成可复用的检查清单

- [ ] 发送端和接收端是否都处理了短读/短写？
- [ ] 协议是否有明确的消息边界（长度前缀或保留边界的 socket 类型）？
- [ ] 是否关闭了所有不用的 fd？
- [ ] 是否处理了 EOF（`read == 0`）？
- [ ] 是否处理了 `EPIPE`，并避免 `SIGPIPE` 杀死进程？
- [ ] 共享内存是否有进程共享的锁或信号量？
- [ ] 共享内存是否有通知机制，避免忙等？
- [ ] 是否定义了生命周期和清理责任？
- [ ] 是否避免了在共享内存中传递裸指针？

---

## 7. 自测题

### 题 1：pipe 的两次 `write`，对端会对应两次 `read` 吗？

**答案：不会保证。** pipe 是字节流，没有消息边界。两次 `write` 的数据可能被一次 `read` 合并，也可能被拆成多次。需要消息语义时，必须自己分帧（如长度前缀），或改用保留边界的 `SOCK_SEQPACKET`/`SOCK_DGRAM`。

### 题 2：对端关闭后，`read` 和 `write` 分别会怎样？

**答案：** 当所有写端关闭后，读端 `read` 返回 `0`，表示 EOF；这不是错误。当读端全部关闭后，继续 `write` 会返回 `-1`，`errno = EPIPE`，同时默认触发 `SIGPIPE`。应忽略 `SIGPIPE` 并处理 `EPIPE`，socket 上也可使用 `MSG_NOSIGNAL`。

### 题 3：共享内存已经能直接读写，为什么还需要同步？

**答案：** 共享内存只提供同一批物理页的映射，不提供互斥、消息边界或通知。多个进程同时读写会产生数据竞争，读取方也无法知道数据何时写完。因此需要进程共享的互斥锁/信号量、明确的布局和长度，以及通知机制（如 `sem_post` 或 Unix 域 Socket 消息）。同时还要考虑进程崩溃后的锁释放和共享内存对象的生命周期。

---

## 8. 推荐资源

- **管道、共享内存与 IPC：Linux man-pages**  
  https://man7.org/linux/man-pages/man7/unix.7.html  
  来源：man7.org｜语言：英文｜类型：文档｜难度：进阶｜免费  
  重点阅读 `pipe` 和 Unix 域 Socket 与共享内存映射相关内容，用于核对本节点实验的语义和边界。建议同时查阅同站的 `pipe(7)`、`socketpair(2)`、`shm_open(3)`、`mmap(2)`、`sem_init(3)`、`pthread_mutexattr_setpshared(3)` 等手册页，沿定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

---

## 9. 本课小结

- pipe 和 `SOCK_STREAM` 是字节流，必须自己分帧；`SOCK_SEQPACKET`/`SOCK_DGRAM` 保留边界，但仍有各自的使用约束。
- `read` 返回 `0` 是 EOF；写已关闭的对端会得到 `EPIPE`，默认可能收到 `SIGPIPE`。
- `socketpair` 适合父子进程双向通信，务必关闭不用的端。
- `shm_open` + `mmap(MAP_SHARED)` 提供低复制共享内存，但互斥、通知、边界、生命周期全部由应用负责。
- 共享内存不自动提供互斥或消息边界；低复制方案把复杂度转移到同步与生命周期。设计时先问：复制成本真的是瓶颈吗？

---

## 附：本课实践参考实现（已在 box 上编译运行验证）

一个程序覆盖实践和两条验收标准：pipe 无边界、SEQPACKET 有边界、socketpair + 长度前缀做 master/worker 任务分发、worker 中途崩溃时父进程读到 EOF 而不卡死、再写得到 EPIPE（已忽略 SIGPIPE）、共享内存用进程共享 mutex + 条件变量同步传 256MB、以及无锁计数反例。

实测输出（box，gcc 14）：

```text
pipe_3writes_1read bytes=6
seqpacket reads=2,4
tasks_normal ok=100 sum=338350 eof=0 epipe=0 child_exit=0
tasks_crash ok=10 sum=385 eof=1 epipe=1 child_exit=9
shm_sum=4194304 expect=4194304
sock_sum=4194304
bulk_256MB shm=0.481s sock=0.448s
race_nolock counter=1115246 expect=2000000
race_locked counter=2000000 expect=2000000
ALL_CHECKS_PASSED
```

两点要看懂：

- **无锁计数只有约 111 万（每次运行都不同）**，加锁后严格 200 万，这就是「共享内存不自动提供互斥」。
- **这次 256MB 共享内存并没有比 socket 快**：共享内存是新建的映射，首次写入要逐页缺页分配（上一课讲的 minor fault），这个成本吃掉了省下的拷贝。共享内存的优势要在「区域长期复用、反复传大块数据」时才显现。所以动手对比时要分清首次和稳态，别只看一次的结论。

`ipc.c`：

```c
/* 第39课实践：Unix 域 socket 父子任务通信 + 共享内存大块数据对比 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <signal.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/mman.h>
#include <sys/wait.h>

static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
/* 读满/写满：处理短读短写与 EINTR；返回 0 表示对端关闭(EOF) */
static ssize_t read_full(int fd,void*b,size_t n){size_t g=0;while(g<n){ssize_t r=read(fd,(char*)b+g,n-g);
 if(r<0){if(errno==EINTR)continue;return -1;} if(r==0)return g; g+=r;} return g;}
static int write_full(int fd,const void*b,size_t n){size_t g=0;while(g<n){ssize_t r=write(fd,(const char*)b+g,n-g);
 if(r<0){if(errno==EINTR)continue;return -1;} g+=r;} return 0;}
/* 长度前缀帧：4 字节长度 + 负载，在字节流上自己定义消息边界 */
static int send_msg(int fd,const void*p,uint32_t n){if(write_full(fd,&n,4))return -1;return write_full(fd,p,n);}
static int recv_msg(int fd,void*p,uint32_t cap,uint32_t*n){ssize_t r=read_full(fd,n,4); if(r==0)return 0; if(r!=4)return -1;
 if(*n>cap)return -1; return read_full(fd,p,*n)==(ssize_t)*n?1:-1;}

/* 1. pipe 是字节流：三次 write 可能被一次 read 读走 */
static void demo_pipe(void){int p[2];pipe(p);write(p[1],"aa",2);write(p[1],"bb",2);write(p[1],"cc",2);
 char b[64];ssize_t r=read(p[0],b,sizeof b);printf("pipe_3writes_1read bytes=%zd\n",r);close(p[0]);close(p[1]);}
/* 2. SOCK_SEQPACKET 保留消息边界 */
static void demo_seqpacket(void){int s[2];socketpair(AF_UNIX,SOCK_SEQPACKET,0,s);write(s[0],"aa",2);write(s[0],"bbbb",4);
 char b[64];ssize_t r1=read(s[1],b,64),r2=read(s[1],b,64);printf("seqpacket reads=%zd,%zd\n",r1,r2);close(s[0]);close(s[1]);}

/* 3. master/worker：任务分发 + 对端退出 EOF/EPIPE */
static void worker(int fd,int crash_after){char buf[256];uint32_t n;int done=0;
 for(;;){int r=recv_msg(fd,buf,sizeof buf-1,&n); if(r<=0)break; buf[n]=0;
  if(crash_after&&done==crash_after)_exit(9); /* 模拟 worker 崩溃 */
  long x=atol(buf); char out[64]; int k=snprintf(out,sizeof out,"%ld",x*x); send_msg(fd,out,k); done++;}
 _exit(0);}
static void demo_tasks(int crash_after,const char*tag){int s[2];socketpair(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0,s);
 pid_t pid=fork(); if(pid==0){close(s[0]);worker(s[1],crash_after);} close(s[1]);
 int ok=0,eof=0,epipe=0; long sum=0;
 for(int i=1;i<=100;i++){char t[32];int k=snprintf(t,sizeof t,"%d",i);
  if(send_msg(s[0],t,k)){if(errno==EPIPE)epipe=1;break;}
  char r[64];uint32_t n;int rr=recv_msg(s[0],r,63,&n); if(rr==0){eof=1;break;} if(rr<0)break; r[n]=0; sum+=atol(r); ok++;}
 /* 对端已死时再写一次：忽略 SIGPIPE 后得到 EPIPE 而不是进程被杀 */
 if(eof){usleep(10000); if(send_msg(s[0],"1",1)&&errno==EPIPE)epipe=1;}
 close(s[0]); int st; waitpid(pid,&st,0);
 printf("%s ok=%d sum=%ld eof=%d epipe=%d child_exit=%d\n",tag,ok,sum,eof,epipe,WIFEXITED(st)?WEXITSTATUS(st):-1);}

/* 4. 共享内存：进程共享互斥锁+条件变量做同步 */
struct shm{pthread_mutex_t mu;pthread_cond_t cv;int ready;size_t len;uint64_t sum;char data[];};
static void demo_shm(size_t sz,double*t_shm,double*t_sock){
 struct shm*m=mmap(NULL,sizeof *m+sz,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
 pthread_mutexattr_t ma;pthread_mutexattr_init(&ma);pthread_mutexattr_setpshared(&ma,PTHREAD_PROCESS_SHARED);pthread_mutex_init(&m->mu,&ma);
 pthread_condattr_t ca;pthread_condattr_init(&ca);pthread_condattr_setpshared(&ca,PTHREAD_PROCESS_SHARED);pthread_cond_init(&m->cv,&ca);
 m->ready=0;
 pid_t pid=fork();
 if(pid==0){pthread_mutex_lock(&m->mu);while(m->ready!=1)pthread_cond_wait(&m->cv,&m->mu);
  uint64_t s=0;for(size_t i=0;i<m->len;i+=64)s+=(unsigned char)m->data[i]; m->sum=s;m->ready=2;
  pthread_cond_signal(&m->cv);pthread_mutex_unlock(&m->mu);_exit(0);}
 double t0=now(); memset(m->data,1,sz);
 pthread_mutex_lock(&m->mu);m->len=sz;m->ready=1;pthread_cond_signal(&m->cv);
 while(m->ready!=2)pthread_cond_wait(&m->cv,&m->mu); pthread_mutex_unlock(&m->mu);
 *t_shm=now()-t0; waitpid(pid,NULL,0);
 printf("shm_sum=%llu expect=%zu\n",(unsigned long long)m->sum,sz/64);
 /* 同样数据走 socket：多两次拷贝(用户->内核->用户) */
 int s[2];socketpair(AF_UNIX,SOCK_STREAM,0,s); pid=fork();
 if(pid==0){close(s[0]);char*b=malloc(1<<16);size_t got=0;uint64_t sm=0;ssize_t r;
  while((r=read(s[1],b,1<<16))>0){for(ssize_t i=0;i<r;i++)if(((got+i)&63)==0)sm+=(unsigned char)b[i];got+=r;}
  write_full(s[1],&sm,8);_exit(0);}
 close(s[1]); t0=now(); char*src=malloc(sz);memset(src,1,sz); write_full(s[0],src,sz); shutdown(s[0],SHUT_WR);
 uint64_t sm=0; read_full(s[0],&sm,8); *t_sock=now()-t0; waitpid(pid,NULL,0); close(s[0]); free(src);
 printf("sock_sum=%llu\n",(unsigned long long)sm); munmap(m,sizeof *m+sz);}

/* 5. 反例：共享内存无同步，两个进程各加 100 万次 */
static void demo_race(int locked){struct{pthread_mutex_t mu;volatile long c;}*m=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
 pthread_mutexattr_t ma;pthread_mutexattr_init(&ma);pthread_mutexattr_setpshared(&ma,PTHREAD_PROCESS_SHARED);pthread_mutex_init(&m->mu,&ma);m->c=0;
 pid_t p[2];for(int k=0;k<2;k++)if((p[k]=fork())==0){for(int i=0;i<1000000;i++){if(locked)pthread_mutex_lock(&m->mu);m->c++;if(locked)pthread_mutex_unlock(&m->mu);}_exit(0);}
 waitpid(p[0],NULL,0);waitpid(p[1],NULL,0); printf("%s counter=%ld expect=2000000\n",locked?"race_locked":"race_nolock",m->c); munmap(m,4096);}

int main(void){signal(SIGPIPE,SIG_IGN); setvbuf(stdout,NULL,_IONBF,0);
 demo_pipe(); demo_seqpacket(); demo_tasks(0,"tasks_normal"); demo_tasks(10,"tasks_crash");
 double a,b; demo_shm(256UL<<20,&a,&b); printf("bulk_256MB shm=%.3fs sock=%.3fs\n",a,b);
 demo_race(0); demo_race(1); return 0;}
```

`test.sh`：

```bash
#!/usr/bin/env bash
# 第39课实践：Unix 域 socket 父子任务通信（含对端退出 EOF/EPIPE）+ 共享内存同步传大块数据
set -u; cd "$(dirname "$0")"
gcc -O1 -pthread -w -o ipc ipc.c || exit 1
out=$(./ipc); echo "$out"; fail=0
chk(){ if eval "$2"; then echo "PASS $1"; else echo "FAIL $1"; fail=1; fi; }
v(){ echo "$out" | grep "^$1 " | grep -o "$2=[0-9.]*" | cut -d= -f2; }
chk "pipe 是字节流：3 次 write 被 1 次 read 读成 6 字节" '[ "$(v pipe_3writes_1read bytes)" = 6 ]'
chk "SOCK_SEQPACKET 保留边界：两次 read 得 2 和 4" 'echo "$out" | grep -q "seqpacket reads=2,4"'
chk "正常任务分发 100 个，平方和 338350" '[ "$(v tasks_normal ok)" = 100 ] && [ "$(v tasks_normal sum)" = 338350 ]'
chk "worker 崩溃：父进程读到 EOF 而非卡死，再写得 EPIPE，子进程退出码 9" '[ "$(v tasks_crash ok)" = 10 ] && [ "$(v tasks_crash eof)" = 1 ] && [ "$(v tasks_crash epipe)" = 1 ] && [ "$(v tasks_crash child_exit)" = 9 ]'
chk "共享内存在锁+条件变量同步下结果正确" 'echo "$out" | grep -q "^shm_sum=4194304 " && echo "$out" | grep -q "^sock_sum=4194304$"'
chk "加锁计数严格等于 2000000" '[ "$(v race_locked counter)" = 2000000 ]'
n=$(v race_nolock counter); echo "INFO 无锁计数=$n（通常小于 2000000，说明共享内存不自带互斥）"
[ $fail -eq 0 ] && echo ALL_CHECKS_PASSED
```
