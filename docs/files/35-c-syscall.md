# 第 35/208 课：系统调用、errno与部分成功

> 领域：C · 阶段 2 · 子类：进程、线程与调度 · 难度：基础
> 前置：c-process（进程）、b-pointer（指针）

## 一、学习目标与为什么后端要懂

**节点摘要**：系统调用是用户态访问内核服务的接口，返回值和 errno 共同表达结果，部分成功通常需要应用继续处理。

**为什么要懂**：正确处理 EINTR、短读写和错误边界。

学完本课你应该能：

1. 解释**返回值与 errno 的有效期**：什么时候该看 errno，什么时候看了也没意义。
2. 独立完成：**实现 `read_full` / `write_full` 辅助函数，并用管道触发短读写**。

验收标准：

- 能说明**成功后 errno 不代表错误**；
- 能区分 `read`/`write` 返回 **0、正数和 -1** 的语义。

后台场景：公众号后台的服务进程每秒在 socket、管道、日志文件上做大量 `read`/`write`。一次 `write(fd, buf, 64KB)` 返回 16KB 却被当成“写完了”，下游就会收到被截断的包；把 `EINTR` 当成致命错误，进程收到一个监控信号就会断连；把 `EAGAIN` 当成“再试一次”放进死循环，CPU 会被打满。这些都是线上真实会出现的问题形态。

---

## 二、从直觉到原理

### 2.1 直觉：系统调用就是“请内核帮忙”

用户态程序不能直接操作磁盘、网卡、进程表，要通过系统调用（`read`、`write`、`open`、`fork`……）进入内核。你平时调用的 `read()` 其实是 glibc 里的一个**封装函数**：它把参数放进寄存器、执行 `syscall` 指令陷入内核，内核返回后再把结果翻译成 C 语言约定。

```bash
man 2 syscalls      # 列出 Linux 全部系统调用（本课推荐资源）
man 2 read          # 第 2 节是系统调用，第 3 节是库函数（如 man 3 fread）
```

可以用 `strace` 看到程序实际发出的系统调用（box 上若没装：`apt install strace`）：

```bash
strace -e trace=openat,read,write cat /etc/hostname
```

### 2.2 要点一：返回值与 errno 有效期

**约定**（绝大多数系统调用封装都遵守）：

- 失败时返回 **-1**（或 `NULL`、`MAP_FAILED` 等特定值），并把错误码写入线程局部变量 `errno`；
- 成功时返回非负值，**不保证会清零 errno**，errno 里可能还是之前某次失败留下的旧值。

所以正确姿势是：**先看返回值，只有返回值表示失败时才读 errno**。

```c
errno = 0;
open("/nonexistent/x", O_RDONLY);         // 失败，errno = ENOENT
int fd = open("/dev/null", O_RDONLY);     // 成功
printf("fd=%d errno=%s\n", fd, strerror(errno));
// 输出 fd=3 errno=No such file or directory —— errno 是旧值，不代表这次出错
```

另外两条关于“有效期”的实践：

- 失败后要**立刻保存** errno（`int saved = errno;`），因为接下来调用 `printf`、`close`、`malloc` 等都可能改写它；
- errno 是**每线程一份**（TLS，上一课讲过），多线程互不覆盖，但同一线程内后续调用会覆盖。

**read/write 返回值的三种语义**：

| 返回值 | read 含义 | write 含义 |
|---|---|---|
| 正数 n | 读到 n 字节，**可能少于请求**（短读） | 写入 n 字节，**可能少于请求**（短写） |
| 0 | **EOF**：对端关闭/文件读完，不是错误 | 请求写 0 字节时返回 0（一般不会遇到） |
| -1 | 失败，看 errno（EINTR、EAGAIN、EBADF…） | 同左（EPIPE、EAGAIN、ENOSPC…） |

“部分成功”指的就是正数但小于请求：内核做了一部分，剩下的需要应用**继续处理**。管道、socket、终端上很常见；普通文件被信号打断或磁盘写满时也可能发生。

### 2.3 要点二：EINTR 和重试条件

进程阻塞在慢速系统调用（读管道、读 socket、`wait` 等）时如果收到信号且执行了信号处理函数，调用可能被打断：

- 还**没传输任何数据** → 返回 -1，`errno == EINTR`；
- **已经传输了一部分** → 返回已传输字节数（短读/短写）。

如果用 `sigaction` 安装处理函数时带了 `SA_RESTART`，很多调用会被内核自动重启；但不是所有调用都会（具体见 `man 7 signal` 的 “Interruption of system calls” 一节），所以健壮的代码仍然要处理 EINTR。

**哪些该重试、哪些不该**：

| 情况 | 处理 |
|---|---|
| `EINTR` | 被信号打断、什么都没做 → 可以原样重试 |
| 短读/短写（正数但不够） | 从已完成位置**继续**，不是“重做整次” |
| `EAGAIN`/`EWOULDBLOCK`（非阻塞 fd） | 现在没数据/缓冲区满 → **不要立刻循环重试**，应交给 epoll 等待可读/可写 |
| `EBADF`、`EFAULT`、`EINVAL`、`ENOSPC`、`EPIPE` 等 | 重试没用，返回给调用者 |

### 2.4 要点三：库函数封装与内核接口

- **内核接口**：原始系统调用返回值在寄存器里，出错时是 `-错误码`（例如 -4 表示 EINTR）。
- **glibc 封装**：把负数翻译成“返回 -1 + 设置 errno”，这就是你在 C 里看到的约定。
- **更高层库**（stdio 的 `fread`/`fwrite`）：自己带缓冲和循环，`fread` 返回的是“元素个数”，要用 `feof`/`ferror` 区分结束和出错；
- **不按 errno 约定的接口**：例如 pthread 系列函数**直接返回错误码**，不设置 errno（`int rc = pthread_create(...); if (rc) strerror(rc)`）。

所以每用一个接口都要看 man page 的 RETURN VALUE 和 ERRORS 两节，不要想当然。自己写封装时，也应该**保留原始错误与上下文**（见设计权衡）。

---

## 三、常见误区与设计权衡

**常见误区（节点原文）**：盲目重试所有失败可能重复操作或形成忙循环。

具体表现：

1. `while (write(...) < 0);` —— 遇到 EBADF/EPIPE 会永远循环；遇到非阻塞 fd 的 EAGAIN 会空转打满 CPU。
2. 短写后把**整块 buffer 重发**，导致对端收到重复数据。正确做法是 `buf + done, n - done`。
3. 对非幂等操作（比如已经部分写出的请求、已经发出去的 RPC）在外层“失败就重来”，可能产生重复的业务副作用。
4. 成功后看 errno 判断“有没有出错”。
5. 把 `read` 返回 0 当成错误或“再读一次”，结果对端关闭后陷入死循环。

**设计权衡（节点原文）**：封装应保留原始错误与上下文，避免隐藏语义。

- `read_full` 帮你吞掉 EINTR 和短读很方便，但应该把“读到 EOF 时只读了多少”和“真正的错误 errno”原样交还调用者，而不是统一返回 `false`；
- 记录日志时带上 fd/对端地址/已完成字节数，例如 `write to 10.0.0.3:8080 failed after 16384/65536 bytes: EPIPE`，排障时一眼看出是部分成功；
- 非阻塞场景里，`write_full` 这种“一直写到完”的封装不合适，它会和事件循环的语义冲突，应该改成“写多少算多少，剩余挂到发送缓冲里等 EPOLLOUT”。

---

## 四、动手实践：read_full / write_full + 管道触发短读写

节点实践：实现 read_full/write_full 辅助函数并用管道触发短读写。

### 步骤 1：写辅助函数

```c
/* 返回已读字节数（< n 表示提前 EOF）；出错返回 -1，errno 保留原值 */
ssize_t read_full(int fd, void *buf, size_t n) {
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, (char *)buf + got, n - got);
        if (r > 0) { got += (size_t)r; continue; }   // 部分成功：继续
        if (r == 0) break;                           // EOF：不是错误
        if (errno == EINTR) continue;                // 被信号打断：重试
        return -1;                                   // 其他错误：交给调用者
    }
    return (ssize_t)got;
}

ssize_t write_full(int fd, const void *buf, size_t n) {
    size_t put = 0;
    while (put < n) {
        ssize_t w = write(fd, (const char *)buf + put, n - put);
        if (w >= 0) { put += (size_t)w; continue; }
        if (errno == EINTR) continue;
        return -1;                                   // EPIPE/EAGAIN 等不盲目重试
    }
    return (ssize_t)put;
}
```

### 步骤 2：触发短读

子进程分 4 次、每次间隔 50ms 往管道写 1000 字节；父进程一次 `read(fd, buf, 4000)`。管道里当时只有 1000 字节，`read` 就返回 1000，不会等凑满 4000。再用 `read_full` 读剩下的 3000，它会循环多次直到读满；子进程退出后再 `read` 返回 0（EOF）。

### 步骤 3：触发短写

用 `pipe2(q, O_NONBLOCK)` 建非阻塞管道，一次写 1MB：管道容量默认 64KB，`write` 返回 65536（部分成功）；再写就返回 -1、`errno=EAGAIN`。这正是“不能盲目循环重试”的场景。

### 步骤 4：触发 EINTR

用 `sigaction` 安装 SIGALRM 处理函数且**不带 SA_RESTART**，`ualarm(200000,0)` 后阻塞 `read` 一个 1 秒后才有数据的管道：原始 `read` 返回 -1、`EINTR`；换成 `read_full` 后，它吞掉 EINTR 继续等，最终读到 `hello`。

### 步骤 5：验证成功后 errno 是旧值

先让 `open` 失败，再成功 `open("/dev/null")`，打印 errno 仍是 ENOENT。

### 步骤 6：编译运行

```bash
cd /workspace/atlas/practice35
gcc -O1 -g -Wall -fsanitize=address,undefined rw.c -o rw && ./rw
```

我在 box 上实际运行的输出：

```
single_read=1000 (asked 4000)
read_full_rest=3000 calls=3
after_eof_read=0
nonblock_short_write=65536 of 1048576
full_pipe_write=-1 errno=Resource temporarily unavailable
raw_read_interrupted=-1 errno=Interrupted system call
read_full_after_eintr=5 eintr=1 data=hello
success_fd=3 stale_errno=No such file or directory
```

`./test.sh` 会编译并逐项断言，最后打印 `ALL_CHECKS_PASSED`。完整代码见文末附录。

### 自查（对应验收标准）

- [ ] 能解释最后一行：fd=3 成功，errno 却是 ENOENT，所以成功后 errno 不代表错误。
- [ ] 能指出输出里 1000（正数、短读）、0（EOF）、-1（EAGAIN/EINTR）各自的含义和处理方式。

---

## 五、自测题

**题 1**：下面代码有什么问题？

```c
int fd = open(path, O_RDONLY);
if (errno != 0) { perror("open"); return -1; }
```

<details><summary>答案</summary>
判断顺序错了。应当先看返回值 `fd == -1`，失败时才读 errno。open 成功时 errno 可能保留之前的旧值，这段代码会把成功误判成失败；反过来，如果之前有人把 errno 清零，也不能靠它判断。正确写法：`if (fd == -1) { perror("open"); return -1; }`。
</details>

**题 2**：从 TCP socket 读一个 4 字节长度头，`read(fd, &len, 4)` 返回 2，应该怎么处理？返回 0 呢？

<details><summary>答案</summary>
返回 2 是部分成功（短读），要从 `(char*)&len + 2` 继续读剩下的 2 字节，可以直接用 read_full。返回 0 表示对端关闭（EOF），不是错误也不该重试：如果一个字节都还没读到，就是连接正常关闭；如果已经读了一部分，就是协议被截断，应该关闭连接并记录日志。
</details>

**题 3**：某服务把 socket 设成非阻塞后，发送函数写成 `while (write_full(fd, buf, n) < 0) {}`，上线后 CPU 100%。为什么？怎么改？

<details><summary>答案</summary>
非阻塞 fd 的发送缓冲区满时，write 返回 -1、errno=EAGAIN。外层循环立刻重试，形成忙循环；如果错误是 EPIPE/ECONNRESET 这类永久错误，循环永远不会结束。应该区分错误码：EINTR 原地重试；EAGAIN 把未发完的部分留在应用层发送缓冲里，注册 EPOLLOUT 等可写时再发；其他错误保存 errno、带上下文记录日志后关闭连接。封装还应返回已写字节数，避免重复发送已经发出的部分。
</details>

---

## 六、推荐资源

- [系统调用、errno与部分成功：Linux man-pages](https://man7.org/linux/man-pages/man2/syscalls.2.html)（man7.org · 英文 · 文档 · 进阶 · 免费）：重点阅读返回值与 errno 有效期、EINTR 和重试条件，用来核对本节点实验的语义和边界。

学习方法（节点 sourceStudy）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

---

## 附：已验证的参考练习（practice35）

### rw.c
```c
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

/* 返回已读字节数（可能 < n 表示 EOF）；出错返回 -1，errno 保留原值 */
ssize_t read_full(int fd, void *buf, size_t n, int *eintr_cnt, int *calls) {
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, (char *)buf + got, n - got);
        if (calls) (*calls)++;
        if (r > 0) { got += (size_t)r; continue; }
        if (r == 0) break;                       /* EOF：不是错误 */
        if (errno == EINTR) { if (eintr_cnt) (*eintr_cnt)++; continue; }
        return -1;                               /* 其他错误：不重试，交给调用者 */
    }
    return (ssize_t)got;
}
ssize_t write_full(int fd, const void *buf, size_t n, int *calls) {
    size_t put = 0;
    while (put < n) {
        ssize_t w = write(fd, (const char *)buf + put, n - put);
        if (calls) (*calls)++;
        if (w >= 0) { put += (size_t)w; continue; }
        if (errno == EINTR) continue;
        return -1;
    }
    return (ssize_t)put;
}
static void on_alrm(int s) { (void)s; }

int main(void) {
    /* 1. 短读：子进程分 4 次每次写 1000 字节，父进程一次 read(4000) 只拿到一部分 */
    int p[2]; if (pipe(p)) return 1;
    pid_t c = fork();
    if (c == 0) { close(p[0]); char b[1000]; memset(b, 'x', sizeof b);
        for (int i = 0; i < 4; i++) { write(p[1], b, sizeof b); usleep(50000); } _exit(0); }
    close(p[1]);
    char buf[8192];
    usleep(10000);
    ssize_t once = read(p[0], buf, 4000);
    printf("single_read=%zd (asked 4000)\n", once);
    int calls = 0;
    ssize_t rest = read_full(p[0], buf, 3000, NULL, &calls);
    printf("read_full_rest=%zd calls=%d\n", rest, calls);
    ssize_t eof = read(p[0], buf, 10);
    printf("after_eof_read=%zd\n", eof);
    close(p[0]); waitpid(c, NULL, 0);

    /* 2. 非阻塞管道写满 -> 短写/EAGAIN */
    int q[2]; if (pipe2(q, O_NONBLOCK)) return 1;
    static char big[1 << 20]; memset(big, 'y', sizeof big);
    ssize_t w = write(q[1], big, sizeof big);
    printf("nonblock_short_write=%zd of %zu\n", w, sizeof big);
    errno = 0; ssize_t w2 = write(q[1], big, 10);
    printf("full_pipe_write=%zd errno=%s\n", w2, w2 < 0 ? strerror(errno) : "none");
    close(q[0]); close(q[1]);

    /* 3. EINTR：无 SA_RESTART 的 SIGALRM 打断阻塞 read */
    struct sigaction sa = {0}; sa.sa_handler = on_alrm; sa.sa_flags = 0;
    sigaction(SIGALRM, &sa, NULL);
    int r2[2]; pipe(r2);
    pid_t c2 = fork();
    if (c2 == 0) { close(r2[0]); sleep(1); write(r2[1], "hello", 5); _exit(0); }
    close(r2[1]);
    alarm(0); ualarm(200000, 0);
    errno = 0; ssize_t raw = read(r2[0], buf, 5);
    int saved = errno;
    printf("raw_read_interrupted=%zd errno=%s\n", raw, raw < 0 ? strerror(saved) : "none");
    ualarm(200000, 0);
    int eintr = 0; calls = 0;
    ssize_t ok = read_full(r2[0], buf, 5, &eintr, &calls);
    printf("read_full_after_eintr=%zd eintr=%d data=%.5s\n", ok, eintr, buf);
    close(r2[0]); waitpid(c2, NULL, 0);

    /* 4. 成功调用后 errno 可能是旧值 */
    errno = 0; open("/nonexistent/x", O_RDONLY);
    int fd = open("/dev/null", O_RDONLY);
    printf("success_fd=%d stale_errno=%s\n", fd, strerror(errno));
    close(fd);
    return 0;
}
```

### test.sh
```bash
#!/bin/bash
set -e
cd "$(dirname "$0")"
gcc -O1 -g -Wall -fsanitize=address,undefined rw.c -o rw
./rw | tee out.txt
grep -Eq '^single_read=(1000|2000|3000) ' out.txt
grep -Eq '^read_full_rest=3000 calls=[2-9]' out.txt
grep -q '^after_eof_read=0$' out.txt
grep -Eq '^nonblock_short_write=[0-9]+ of 1048576' out.txt
! grep -q 'nonblock_short_write=1048576' out.txt
grep -q 'full_pipe_write=-1 errno=Resource temporarily unavailable' out.txt
grep -q 'raw_read_interrupted=-1 errno=Interrupted system call' out.txt
grep -Eq 'read_full_after_eintr=5 eintr=[1-9] data=hello' out.txt
grep -q 'stale_errno=No such file or directory' out.txt
echo ALL_CHECKS_PASSED
```
