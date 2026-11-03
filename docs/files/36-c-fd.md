# 第 36/208 课：文件描述符与打开文件描述

> 节点：`c-fd` ｜ 领域：C ｜ 阶段：1 ｜ 难度：基础 ｜ 子类：文件与内存
>
> 一句话：文件描述符是进程中的整数引用，多个 fd 可能指向同一个打开文件描述并共享偏移及部分标志。

---

## 1. 学习目标

学完这一课，你应该能做到：

- 能解释 **fd 表（file descriptor table）** 与 **打开文件描述（open file description）** 的区别与对应关系。
- 能解释 `dup`、`fork` 之后为什么偏移会「串味」（共享偏移），以及 `O_APPEND`、`O_CLOEXEC`、`FD_CLOEXEC` 各自管的是什么。
- 能独立完成实验：用 `dup` 与 `fork` 演示共享偏移，并验证 `FD_CLOEXEC` 避免子进程泄漏。
- 满足两条验收标准：
  1. 能解释 **fd 复用导致的误关闭**（close 后整数很快可能分配给新连接）。
  2. 能说明 **所有权包装（RAII）不重复 close**。

### 为什么后端要懂

你是要入职腾讯微信公众号后台开发的后端工程师。后台服务的日常大概长这样：

- 每个微信用户请求进来，网关/接入层用 `accept` 拿到一个连接 fd；长连接维持、心跳、断线重连都要围绕 fd 生命周期管理。
- 处理消息时要读配置、读模板、写日志、访问缓存/数据库，背后都是 socket fd 或普通文件 fd。
- 服务经常要 `fork` + `exec` 拉起子进程（比如异步任务、回调消费者、外部脚本），如果 fd 没打好 `CLOEXEC`，子进程会把父进程的连接、监听端口「顺走」，造成泄漏甚至安全问题。
- 高并发下 fd 数量是硬资源（`ulimit -n`）。泄漏 = 迟早 `EMFILE: Too many open files`，然后整个进程的 `accept` 全部失败。
- 排障时你要能看懂 `lsof`、`/proc/PID/fd`、`strace` 的输出，快速定位「这个 fd 到底指向哪个 socket、偏移在哪、谁还引用着它」。

这些都不是玄学，全部落在「fd 表 + 打开文件描述」这一层。理解这一层，你才能解释 `dup`、`fork`、连接关闭的工程边界。

---

## 2. 从直觉到原理

### 2.1 第一层直觉：fd 只是一个整数

当你写下：

```c
int fd = open("config.json", O_RDONLY);
```

内核返回的 `3`、`4`、`5`…… 并不是「文件」，也不是「文件内容」，它只是**当前进程 fd 表里的一个下标**。

- `0` = stdin，`1` = stdout，`2` = stderr（约定俗成，进程启动时就被占用）。
- 之后 `open`/`socket`/`accept`/`pipe` 返回的，通常是当前最小的空闲下标。
- fd 是**进程私有**的：同样的整数 `3`，在进程 A 和进程 B 里完全可能指向两个毫不相干的东西。

这解释了为什么 fd 能「复用」：一旦某个下标被 `close` 释放回空闲集合，下一次分配很可能立刻把它拿去给一条新连接。

### 2.2 第二层：fd 表 → 打开文件描述

内核里的结构大致是三层的（简化版）：

```
进程 A                          系统级
┌──────────────┐
│ fd 0  ───────┼──► 打开文件描述 X ──► inode / socket 对象
│ fd 1  ───────┼──► 打开文件描述 Y
│ fd 3  ───────┼──► 打开文件描述 X   ← 和 fd 0 指向同一个！
└──────────────┘
```

- **fd 表**：每个进程一张，表项是「下标 → 指向某个打开文件描述的指针」，外加每个 fd 自己的 **fd 标志**（目前主要是 `FD_CLOEXEC`）。
- **打开文件描述（open file description, OFD）**：由 `open`/`socket`/`accept`/`pipe` 创建，是**系统级**的对象，可能被多个进程的多个 fd 共同引用。它保存：
  - **文件偏移（offset）**
  - **打开标志**（`O_RDONLY`/`O_WRONLY`/`O_RDWR`、`O_APPEND`、`O_NONBLOCK` 等）
  - 引用计数、文件状态等

关键结论：**偏移和大部分打开标志属于「打开文件描述」，不属于单个 fd。** 所以两个 fd 只要指向同一个 OFD，就会共享偏移。这就是后面 `dup`/`fork` 现象的总根源。

> 注意区分：`FD_CLOEXEC` 是存在**fd 表项**上的（每个 fd 独立）；而 `O_CLOEXEC` 是 `open` 时的一个**打开标志**，它的效果是「创建后顺手把该 fd 的 `FD_CLOEXEC` 置上」。别把两者混为一谈。

### 2.3 第三层：dup —— 复制 fd，共享同一个 OFD

```c
#include <unistd.h>
#include <fcntl.h>
#include <stdio.h>

int main(void) {
    int fd = open("demo.txt", O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    int dupfd = dup(fd);           /* 新 fd，指向同一个打开文件描述 */
    if (dupfd < 0) { perror("dup"); return 1; }

    char a, b;
    if (read(fd, &a, 1) != 1) return 1;      /* 偏移 0 → 1 */
    if (read(dupfd, &b, 1) != 1) return 1;   /* 从 1 开始读，不是 0！ */

    printf("a=%c b=%c\n", a, b);
    close(fd);
    close(dupfd);
    return 0;
}
```

如果文件内容以 `AB` 开头，输出会是 `a=A b=B`，而不是两次读到 `A`。因为 `fd` 和 `dupfd` 共享同一个 offset。

`dup2(oldfd, newfd)` 则是指定目标下标的版本：若 `newfd` 已打开会先被关闭，然后成为 `oldfd` 的副本。它常用于把管道/文件重定向到 0/1/2。

### 2.4 第四层：fork —— 子进程继承 fd，仍然共享 OFD

`fork` 之后，子进程得到父进程 fd 表的一份**拷贝**。拷贝的是「指针」，不是「打开文件描述本身」。所以：

- 父子进程中编号相同的 fd 指向**同一个 OFD**。
- 它们**共享偏移**，也共享打开标志。
- 但 `close(fd)` 是**各关各的**：子进程 `close` 只减少引用计数，父进程的同号 fd 依然有效；反之亦然。

```c
#include <unistd.h>
#include <fcntl.h>
#include <sys/wait.h>
#include <stdio.h>

int main(void) {
    int fd = open("demo.txt", O_RDONLY);
    if (fd < 0) { perror("open"); return 1; }

    pid_t pid = fork();
    if (pid == 0) {                        /* 子进程 */
        char c;
        if (read(fd, &c, 1) == 1) printf("child read: %c\n", c);
        _exit(0);
    }

    waitpid(pid, NULL, 0);
    char c;
    if (read(fd, &c, 1) == 1) printf("parent read: %c\n", c);  /* 接着子进程的位置 */
    close(fd);
    return 0;
}
```

子进程读走第一个字节后，父进程读到的是第二个字节。这正是「共享偏移」的直观证据。

> 顺带一个易错点：`O_APPEND` 也属于打开文件描述。多个进程以 `O_APPEND` 写同一个文件时，每次 `write` 都会原子地「移到末尾再写」，不会互相覆盖半行日志。这在高并发写日志时很重要。

### 2.5 第五层：CLOEXEC 与关闭责任

`exec` 系列会替换进程映像，但**默认会保留所有没有 `FD_CLOEXEC` 的 fd**。这意味着：

- 父进程监听 80 端口的 `listen_fd`、正在处理的连接 fd，如果不设 `CLOEXEC`，`fork` + `exec` 出来的子程序（可能是 `ffmpeg`、某个脚本、某个 SDK 拉起的 helper 进程）会继承它们。
- 子程序本身并不知道这些 fd 的存在，也不会关闭它们；只要子程序活着，这些连接/端口就一直被引用。
- 父进程 `close` 掉自己的 fd 后，资源并没有真正释放（引用计数没到 0），于是出现「明明关了，端口还占着」「连接泄漏」的怪异现象。

正确做法：

```c
/* 方式一：open 时直接带 O_CLOEXEC（推荐，原子，无竞态） */
int fd = open("data.bin", O_RDONLY | O_CLOEXEC);

/* 方式二：先创建再设置 fd 标志（多线程下存在竞态窗口） */
int flags = fcntl(fd, F_GETFD);
fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
```

`accept4`、`socket`（带 `SOCK_CLOEXEC`）、`pipe2`、`dup3` 等都提供了原子版本，多线程服务里应优先使用。

**关闭责任的划分**：

- 谁创建/拥有 fd，谁负责 `close`；`dup` 出来的每个 fd 都是独立的「关闭义务」，都要各关一次。
- `fork` 后的子进程若 `exec`，靠 `FD_CLOEXEC` 自动清理；若不 `exec`，则子进程要自己 `close` 不需要的 fd（或干脆在 `fork` 前就规划好）。
- 引用计数归零，OFD 才释放，底层 inode/socket 才真正关闭。

### 2.6 用 Linux 工具把这一切「看见」

**看 fd 表：**

```bash
ls -l /proc/PID/fd
# lrwx------ 1 user user 64 ... 0 -> /dev/pts/0
# lrwx------ 1 user user 64 ... 1 -> /dev/pts/0
# lrwx------ 1 user user 64 ... 3 -> socket:[123456]
# lrwx------ 1 user user 64 ... 4 -> /var/log/app.log
```

`/proc/PID/fd` 里的每个符号链接就是 fd 表项，箭头指向它引用的对象。两个不同编号指向同一个 `socket:[inode]`，说明它们共享同一个 socket 的打开文件描述。

**看 fd 标志与偏移：**

```bash
cat /proc/PID/fdinfo/3
# pos:    1024                ← 共享的偏移
# flags:  02100000            ← 八进制：O_RDONLY + O_LARGEFILE(0100000) + O_CLOEXEC(02000000)
# mnt_id: 25
# ino:    123456
```

`pos` 是偏移，两个 fd 若指向同一 OFD，`pos` 会一致变化；`flags` 是八进制的打开标志，内核会在这里把该 fd 的 `FD_CLOEXEC` 显示为 `O_CLOEXEC` 位（`02000000`）。所以同一个 OFD 的两个 fd，`pos` 一样，`02000000` 这一位却可能不同（例如 `dup` 出来的新 fd 默认不带 CLOEXEC）。

**看谁打开了什么：**

```bash
lsof -p PID
lsof -i :8080
lsof /var/log/app.log
```

`lsof` 会列出进程的 fd、类型、设备、大小/偏移、名称，适合快速排查「谁占着端口/文件」。

**看系统调用轨迹：**

```bash
strace -f -e trace=openat,close,dup,dup2,dup3,fcntl,accept4 -p PID
# 或从启动开始：
strace -f -e trace=file,desc ./server
```

`strace -f` 会跟踪子进程，能直接看到 `fork`/`exec` 前后哪些 fd 被打开、被设置 `FD_CLOEXEC`、有没有被泄漏。

**一个具体的对照实验（不执行也可看懂）：**

```bash
# 终端 1：启动一个会长期运行并打开文件的进程
./server &
PID=$!

# 终端 2：
ls -l /proc/$PID/fd
cat /proc/$PID/fdinfo/3
lsof -p $PID | head
strace -f -e trace=desc -p $PID
```

---

## 3. 常见误区

### 误区 1：close 之后那个整数就「安全了」

**不是。** `close(fd)` 只是把下标还回空闲池。紧接着 `accept`/`open`/`socket` **很可能拿到同一个整数**。如果代码里还留着一份旧的 `fd` 副本（比如某个回调、定时器、事件循环里的缓存），之后再对这个整数 `close` 一次，就会把**新连接**误关掉。

这就是验收标准里说的「**fd 复用导致的误关闭**」，典型场景：

```c
/* 反面教材 */
void on_timeout(int fd) {
    close(fd);            /* 第一处关闭 */
}

void on_error(int fd) {
    close(fd);            /* 若此时 fd 已被复用为新连接，误杀！ */
}
```

或者更隐蔽的：

```c
int fd = accept(...);
register_callback(fd);    /* 回调里还会用它 */
close(fd);                /* 提前关了 */
/* ... 新连接 accept 拿到同一个整数 ... */
/* 回调触发，读写/关闭的是别人的连接 */
```

**防御手段：**

- 用「连接对象」而不是裸整数作为身份，对象里带 `fd` 和 `closed` 状态；关闭后立即把状态置为 closed，所有路径先检查状态。
- 关闭后把变量置为 `-1`（`fd = -1`），让二次 `close(-1)` 变成一个无害的 `EBADF`，而不是误关别人。
- 事件循环里用 `epoll` 的 `data.ptr` 指向对象而非 fd，避免 fd 复用后事件投递到错误对象。
- 明确「唯一所有者」，其余位置只持有弱引用/句柄，不各自 `close`。

### 误区 2：fork 之后父子共用 fd，所以关一次就行

**错。** `fork` 后父子各有一份 fd 表，`close` 是各关各的。要释放底层 OFD，必须让**所有**引用都关掉（引用计数归零）。父进程关了、子进程没关，资源仍然泄漏。

### 误区 3：dup 出来的 fd 是「别名」，关一个就都关了

**错。** `dup` 返回的是**新的 fd 表项**，有独立的关闭义务。关掉 `dupfd` 不影响 `fd` 的可用性（只要还有引用，OFD 就活着）。只是它们共享偏移。

### 误区 4：`O_CLOEXEC` 和 `FD_CLOEXEC` 是同一个东西

`O_CLOEXEC` 是 `open` 的入参标志；`FD_CLOEXEC` 是 fd 表项上的标志。前者是「创建时顺手设置后者」的便捷方式。用 `fcntl(F_SETFD)` 改的是后者。多线程环境下「先 open 再 fcntl」中间有竞态窗口，可能被别的线程 `fork`+`exec` 时泄漏，所以优先 `O_CLOEXEC`/`SOCK_CLOEXEC`/`accept4`。

### 误区 5：`close` 返回值不重要

在 Linux 上，`close` 可能因为延迟写入错误（如 NFS）返回 `-1`，但**fd 已经被释放**。所以「close 失败就重试」是危险的——重试可能关掉被复用的新 fd。正确处理是记录错误，但不要盲目重试同一个整数。

---

## 4. 设计权衡

### 4.1 集中管理 fd 减少泄漏

**收益**：把所有 fd 的创建/关闭收敛到一个「连接池 / fd 管理器 / RAII 包装」里，能显著降低泄漏与双重关闭的概率。常见做法：

```cpp
class FileDesc {
public:
    explicit FileDesc(int fd = -1) : fd_(fd) {}
    ~FileDesc() { reset(); }

    FileDesc(const FileDesc&) = delete;
    FileDesc& operator=(const FileDesc&) = delete;

    FileDesc(FileDesc&& o) noexcept : fd_(o.release()) {}
    FileDesc& operator=(FileDesc&& o) noexcept {
        if (this != &o) { reset(); fd_ = o.release(); }
        return *this;
    }

    int get() const { return fd_; }
    int release() { int t = fd_; fd_ = -1; return t; }   /* 放弃所有权，不 close */
    void reset(int fd = -1) {
        if (fd_ != -1) { ::close(fd_); }                 /* 只 close 一次 */
        fd_ = fd;
    }
    explicit operator bool() const { return fd_ != -1; }

private:
    int fd_;
};
```

要点：

- **所有权唯一**：同一时刻只有一个包装对象拥有该 fd，析构时 `close` 一次。
- **移动而非拷贝**：转移所有权用 `release()` 把源置 `-1`，避免两份都去 `close`。
- **`close` 后置 `-1`**：让重复析构/重复关闭变成无害操作。
- **对外只暴露 `get()`**：其他模块要用 fd 就读，不拥有、不关闭。

这就对应验收标准「**所有权包装不重复 close**」。

### 4.2 但异步关闭需要协调事件回调

RAII 把「何时 close」绑定到了对象生命周期，这在同步代码里很香，但在**异步事件驱动**的服务里会打架：

- 事件循环（`epoll`/libevent/libuv）里可能还挂着这个 fd 的读/写事件回调。
- 如果对象析构（`close` 掉 fd）时没有先从事件循环注销，可能出现：回调仍被触发 → 访问已释放对象（use-after-free）；或者 fd 被复用后，旧事件投递到新连接（误关/误读写）。
- 所以「关闭」通常要拆成两步：先 `epoll_ctl(DEL)` / 注销回调，等确认没有在途回调，再真正 `close`。有时还要用「延迟关闭」或「引用计数对象 + 最后持有者关闭」来保证安全。

**权衡结论**：

- 同步、短生命周期：优先 RAII/所有权包装，简单可靠。
- 异步、回调密集：需要「所有权包装 + 生命周期引用计数 + 事件注销协议」三者配合；只在所有回调都不再引用时才真正 `close`。
- 无论哪种，都要遵守「唯一关闭者」和「close 后置 `-1`」这两条底线。

在公众号后台这类高并发长连接场景，连接对象通常被事件循环、业务回调、超时定时器多方引用，因此更常见的是「引用计数连接对象 + 最后释放时注销并 close」的模型，而不是裸 fd 直接 RAII。

---

## 5. 动手实践（可执行步骤）

> 目标：用 `dup` 与 `fork` 演示共享偏移，并验证 `FD_CLOEXEC` 避免子进程泄漏。
> 以下步骤在 Linux 上执行即可。实验 C 之后附了一份我实际跑通的一键验证脚本。

### 实验 A：dup 演示共享偏移

**步骤 1：准备文件**

```bash
printf 'ABCDEFGHIJ' > /tmp/ofd_demo.txt
od -c /tmp/ofd_demo.txt
```

**步骤 2：写程序 `dup_demo.c`**

```c
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

int main(void) {
    int fd = open("/tmp/ofd_demo.txt", O_RDONLY | O_CLOEXEC);
    if (fd < 0) { perror("open"); return 1; }

    int fd2 = dup(fd);
    if (fd2 < 0) { perror("dup"); return 1; }

    char a, b, c;
    if (read(fd,  &a, 1) != 1) return 1;   /* 期望 A */
    if (read(fd2, &b, 1) != 1) return 1;   /* 期望 B（共享偏移） */
    if (read(fd,  &c, 1) != 1) return 1;   /* 期望 C */

    printf("fd=%d fd2=%d reads: %c %c %c\n", fd, fd2, a, b, c);

    close(fd);
    close(fd2);
    return 0;
}
```

**步骤 3：编译运行**

```bash
cc -Wall -Wextra -O0 -g -o /tmp/dup_demo /tmp/dup_demo.c
/tmp/dup_demo
# 期望输出：fd=3 fd2=4 reads: A B C
```

**验收点**：两次读没有都拿到 `A`，而是连续推进，证明共享偏移。

**步骤 4：观察 fdinfo（可选）**

把程序改成读一个字节后 `sleep(30)`，运行后另开终端：

```bash
PID=$(pgrep -f dup_demo | head -1)
ls -l /proc/$PID/fd
cat /proc/$PID/fdinfo/3
cat /proc/$PID/fdinfo/4
```

对比两个 `pos:`，读数后它们应保持一致；`flags:` 中可见 `O_CLOEXEC` 位。

### 实验 B：fork 演示共享偏移

**步骤 1：写 `fork_demo.c`**

```c
#include <fcntl.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    int fd = open("/tmp/ofd_demo.txt", O_RDONLY | O_CLOEXEC);
    if (fd < 0) { perror("open"); return 1; }

    pid_t pid = fork();
    if (pid < 0) { perror("fork"); return 1; }

    if (pid == 0) {
        char c;
        if (read(fd, &c, 1) == 1) printf("child  read: %c\n", c);
        _exit(0);
    }

    waitpid(pid, NULL, 0);
    char c;
    if (read(fd, &c, 1) == 1) printf("parent read: %c\n", c);
    close(fd);
    return 0;
}
```

**步骤 2：编译运行**

```bash
cc -Wall -Wextra -O0 -g -o /tmp/fork_demo /tmp/fork_demo.c
/tmp/fork_demo
# 期望：child read: A
#       parent read: B   ← 父进程没有从 A 重新开始
```

**验收点**：父子共享同一 OFD 的偏移；若父进程「重新读到 A」，则说明你其实做了 `lseek(fd, 0, SEEK_SET)` 或另开了文件。

### 实验 C：验证 FD_CLOEXEC 避免子进程泄漏

**步骤 1：写 `cloexec_demo.c`**

```c
#include <fcntl.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
    int fd_cloexec = open("/tmp/ofd_demo.txt", O_RDONLY | O_CLOEXEC);
    int fd_plain   = open("/tmp/ofd_demo.txt", O_RDONLY);   /* 故意不设 */
    if (fd_cloexec < 0 || fd_plain < 0) { perror("open"); return 1; }

    printf("parent fds: cloexec=%d plain=%d\n", fd_cloexec, fd_plain);
    fflush(stdout);

    pid_t pid = fork();
    if (pid < 0) { perror("fork"); return 1; }

    if (pid == 0) {
        char buf[64];
        snprintf(buf, sizeof buf, "ls -l /proc/%d/fd", getpid());
        execl("/bin/sh", "sh", "-c", buf, (char *)NULL);  /* exec 后看子进程 fd */
        _exit(127);
    }

    waitpid(pid, NULL, 0);
    close(fd_cloexec);
    close(fd_plain);
    return 0;
}
```

**步骤 2：编译运行**

```bash
cc -Wall -Wextra -O0 -g -o /tmp/cloexec_demo /tmp/cloexec_demo.c
/tmp/cloexec_demo
```

**期望结果**：子进程（`sh`）列出的 fd 里能看到 `plain` 那个，看不到 `cloexec` 那个。

**步骤 3：加验证（把「看到」变成「断言」）**

注意：判断必须发生在 **exec 之后**。如果在 `fork` 后、`exec` 前用 `fcntl` 检查，两个 fd 都还开着，因为 `FD_CLOEXEC` 只在 `execve` 那一刻生效。所以让 exec 出来的 `sh` 自己检查 `/proc/self/fd`：

```c
if (pid == 0) {
    char cmd[160];
    snprintf(cmd, sizeof cmd,
        "for f in %d %d; do [ -e /proc/self/fd/$f ] && echo child_has_fd=$f || echo child_no_fd=$f; done",
        fd_plain, fd_cloexec);
    execl("/bin/sh", "sh", "-c", cmd, (char *)NULL);
    _exit(127);
}
```

**验收点**：`fd_plain` 输出 `child_has_fd`（泄漏到了 exec 后的新程序），`fd_cloexec` 输出 `child_no_fd`。这就验证了 `FD_CLOEXEC` 确实避免子进程泄漏。

**步骤 4：用 strace 复核**

```bash
strace -f -e trace=openat,close,fcntl,execve /tmp/cloexec_demo 2>&1 | grep -E 'openat|execve|fcntl'
```

你能看到 `openat(..., O_CLOEXEC)`，以及 `execve` 后子进程没有再引用 `fd_cloexec` 的迹象。

### 一键验证脚本（已在 gcc/g++ + ASan/UBSan 下实测通过）

`practice36/` 里有三个文件：`fdlab.c`（dup 共享偏移、两次 open 不共享、fork 共享、CLOEXEC 对比、fd 复用误关闭）、`owned_fd.cpp`（只能移动的 RAII 包装，统计 close 次数）、`test.sh`（编译、运行并逐条断言）。实测输出：

```
dup: a=3 b=4 read via a -> pos(a)=3 pos(b)=3
open twice: pos(c)=0 (独立)
fork: child read 3 -> parent pos(a)=6
leak=6 safe=7
child_has_fd=6
child_no_fd=7
reuse: old=8 new=8 same_number=yes
reuse: newconn closed by mistake (EBADF)
a=-1 b=-1 c>=0:1
closes=2 (期望 2，每个 fd 恰好一次)
ALL_CHECKS_PASSED
```

`reuse` 两行就是验收标准一：旧整数 8 被 close 后，下一次 open 立刻又拿到 8，再对旧变量 close 一次，新连接就被误关了。`closes=2` 是验收标准二：两次 move 之后，两个 fd 各被 close 恰好一次。

fdlab.c 中 fd 复用部分的核心：

```c
int conn = open(path, O_RDONLY);
int stale = conn;               // 某个回调里保存的旧整数
close(conn);
int newconn = open("/dev/null", O_RDONLY);   // 拿到同一个数字
close(stale);                   // 二次 close 误关了新连接
// fcntl(newconn, F_GETFD) == -1, errno == EBADF
```

### 实验 D（选做）：观察 fd 复用导致的误关闭

思路：父进程 `accept` 一个连接拿到 `fd=5`，业务代码误 `close(5)`；随后再 `accept` 新连接，内核很可能又给 `5`；此时旧的回调再 `close(5)`，新连接被误关。可用一个最小 TCP 服务 + 两个客户端 + `strace -e trace=accept,close` 复现。观察日志里「同一个整数被 close 两次，中间夹着一次 accept」。

---

## 6. 自测题

### 题 1：两个 fd 都读同一个文件，为什么后读的没有从文件开头开始？

**参考答案**：因为 `dup`（以及 `fork` 继承）产生的新 fd 指向**同一个打开文件描述**，而文件偏移保存在打开文件描述里，是被这些 fd **共享**的。`read` 会推进共享偏移，所以第二个 fd 从第一个 fd 读到的位置继续往下读。若想各自独立，需要重新 `open`（得到新的 OFD），或显式 `lseek` 调整。

### 题 2：`close(fd)` 之后马上把 `fd` 置为 `-1` 有什么意义？这和「fd 复用导致的误关闭」有什么关系？

**参考答案**：`close` 只是释放下标，操作系统随时可能把同一个整数分配给新的 `accept`/`open`/`socket`。如果代码还保留旧整数并再次 `close`，就会误关新连接。把变量置 `-1` 后，二次 `close(-1)` 只会返回 `EBADF`，不会伤及他人；同时在对象层面记录 `closed` 状态、用唯一所有者模型、事件回调使用对象而非裸 fd，都能进一步避免误关闭。

### 题 3：父进程 `fork` 后 `exec` 一个外部程序，为什么监听端口的 fd 可能被子进程「带走」？怎么避免？

**参考答案**：`exec` 默认保留所有未设置 `FD_CLOEXEC` 的 fd；子进程继承父进程 fd 表的拷贝，`exec` 后这些 fd 仍在，且子程序并不知道、也不会关闭它们，于是监听 socket 的引用计数不归零，端口持续被占用，形成泄漏。避免方法：创建 fd 时用 `O_CLOEXEC`（或 `SOCK_CLOEXEC`、`accept4(..., SOCK_CLOEXEC)`、`pipe2(..., O_CLOEXEC)` 等原子方式），或对已存在的 fd 用 `fcntl(fd, F_SETFD, FD_CLOEXEC)`；多线程下优先使用原子创建方式以避免竞态窗口。

---

## 7. 推荐资源

- **文件描述符与打开文件描述：Linux man-pages**
  <https://man7.org/linux/man-pages/man2/open.2.html>
  - 来源：man7.org ｜ 语言：英文 ｜ 类型：文档 ｜ 难度：基础 ｜ 免费
  - 推荐理由：重点阅读 fd 表与 open file description、`dup` 与共享偏移相关说明，用于核对本节点实验的语义和边界（注意不同内核版本的 `O_CLOEXEC` / `FD_CLOEXEC` 行为细节可能略有差异，实践中以目标系统 man-pages 为准）。

> 学习建议（对应节点 `sourceStudy`）：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。比如把 `open(2)` 里关于 `O_CLOEXEC`、`O_APPEND`、文件偏移共享的描述逐条对到本课实验现象上，记录你所用内核版本的输出差异。
