# 第 46/208 课：namespace与隔离边界

> 所属阶段：第 3 阶段 · 隔离与资源治理  
> 前置课程：c-process、c-ipc  
> 关键词：Linux Namespaces、PID、mount、network、user namespace、unshare、setns、容器隔离

## 0. 本课导读

一个已经容器化部署的微信公众号后台服务，本地运行正常，进容器后却出现怪现象：容器里 `ps -ef` 只看到一两个进程；日志打印的 PID 是 `1`、`7`，宿主上却找不到；容器里 `ip addr` 只有 `lo`；容器内 `uname -r` 与宿主一模一样。

这些通常不是程序写错了，而是你正在观察两个不同的 **namespace 视图**。namespace 让一组进程看到独立的系统资源视图，但它不复制内核，也不自动限制 CPU 和内存。本课从直觉到原理讲清 PID、mount、network、user namespace，以及 `unshare`、`setns` 和共享内核的安全边界，最后完成一次宿主与容器的 namespace/PID 对比实践。

## 1. 学习目标

完成本课后，你应当能够：

1. 解释 PID、mount、network、user namespace 分别在隔离什么，边界在哪里。
2. 说明 `unshare` 与 `setns` 的区别：前者创建新 namespace，后者加入已有 namespace。
3. 解释容器为什么轻量：容器进程与宿主共享同一个内核。
4. 在受控开发环境中查看容器 namespace 链接，对比容器内 PID 与宿主 PID，并解释差异。
5. 区分 namespace、cgroup、capabilities、seccomp 各自负责什么，不把 namespace 当资源配额或完整安全沙箱。

对应节点的验收标准：

- 能解释容器共享宿主内核；
- 不把 namespace 当资源配额。

## 2. 为什么后端要懂

**部署背景。** 微信公众号后台通常由多个服务组成，运行在容器或 Kubernetes Pod 中。理解 namespace，才能理解为什么容器里的 `localhost` 和宿主不是同一个网络空间，为什么容器监听 `8080` 后宿主还要做端口映射，为什么容器里的 `/proc`、根文件系统和宿主不同。

**排障容易看错视图。** `ps`、`ss`、`top`、`cat /proc/...` 读到的内容都受 namespace 影响。容器内 `ps` 只看到 PID 1，不代表宿主空闲；容器内 `ps` 看到宿主进程，也不一定代表没有隔离，可能是 `/proc` 没有按新的 PID namespace 重新挂载。

**安全边界与运行时配置直接相关。** 容器共享宿主内核。namespace 提供可见性隔离，但不能阻止拥有过多 capability 的进程执行危险系统调用。因此还要关注非 root 运行、capability 裁剪、seccomp、LSM、user namespace 和最小挂载。

**几个真实场景。**

- 容器日志打印 `pid=1`，宿主上用 `docker inspect` 查到主进程宿主 PID 是 `34821`：两个数字指向同一个进程。
- 容器内服务监听 `0.0.0.0:8080`，宿主 `ss -lntp` 看不到，因为 network namespace 不同，宿主看到的是端口映射或 veth 一侧。
- 容器内改了配置文件，宿主文件没变，因为 mount namespace 让容器有独立挂载表。
- 容器内 `uname -r` 与宿主完全一致，因为容器没有自己的内核。

## 3. 直觉：namespace 是“视图”，不是“分区”

把内核想象成一栋大楼，进程是楼里的工作人员。namespace 不是给每个人复制一栋楼，而是发不同的“楼层指南”：

- PID namespace 改变“人员编号簿”：同一个进程，在你眼里是 1 号，在宿主眼里是 34821 号。
- mount namespace 改变“房间地图”：你看到的挂载点和别人不一样。
- network namespace 改变“通信设备清单”：你有自己的网卡、路由和端口空间。
- user namespace 改变“身份映射表”：你在自己的区域里可以是 root，在大楼管理处看来只是普通用户。

关键点是：**楼还是同一栋楼，内核还是同一个内核。** namespace 改变进程能观察到的资源视图和命名空间，不改变资源的物理数量，也不复制内核。

| 维度 | namespace 改变什么 | namespace 不改变什么 |
| --- | --- | --- |
| PID | 进程编号、可见进程集合、PID 1 的含义 | CPU 时间、进程总数上限 |
| mount | 挂载表、根文件系统视图 | 磁盘容量、I/O 带宽 |
| network | 网卡、路由、端口、iptables 视图 | 物理带宽、宿主总连接数 |
| user | UID/GID 映射、namespace 内 capability 范围 | 宿主内核漏洞、全部安全策略 |
| 资源 | 基本不负责 | CPU、内存、I/O、进程数配额，这些是 cgroup 的职责 |

看到“容器隔离”时，要在心里补一句：**隔离的是视图，不是全部资源和全部权限。**

## 4. 核心模型：task_struct、nsproxy 与 inode

Linux 内核用 `task_struct` 表示进程或线程，每个任务通过 `nsproxy` 关联一组 namespace：PID、mount、network、user、UTS、IPC、cgroup、time 等。一个进程在任一时刻，对每一类 namespace 都有一个“当前所属”的实例；多个进程可以共享同一个实例。

最直接的观察方式是：

```bash
ls -l /proc/self/ns
```

输出类似：

```text
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 cgroup -> 'cgroup:[4026531835]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 ipc -> 'ipc:[4026531839]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 mnt -> 'mnt:[4026531841]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 net -> 'net:[4026531840]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 pid -> 'pid:[4026531836]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 pid_for_children -> 'pid:[4026531836]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 user -> 'user:[4026531837]'
lrwxrwxrwx 1 dev dev 0 Nov  4 10:00 uts -> 'uts:[4026531838]'
```

方括号中的数字是 namespace 的 inode 号。它有两个用途：

1. **判断两个进程是否在同一 namespace**：读取同一个链接目标，inode 相同，就是同一个实例。
2. **作为 `setns` 的入口**：打开 `/proc/<pid>/ns/<type>`，把文件描述符传给 `setns`。

```bash
readlink /proc/self/ns/pid
readlink /proc/1/ns/pid
readlink /proc/$$/ns/mnt
```

注意 `pid` 和 `pid_for_children` 的区别：前者是进程当前所在的 PID namespace，后者是它将来 fork 出的子进程将进入的 PID namespace。`unshare(CLONE_NEWPID)` 之后，调用者的 `pid` 链接不变，但 `pid_for_children` 会指向新 namespace。

`lsns` 可以从全局视角列出 namespace：

```bash
lsns
lsns -p <pid>
lsns -t pid
lsns -t net
```

`NPROCS` 表示共享该 namespace 的进程数，可用来判断某个 namespace 是只有容器里的少数进程在用，还是系统上大量进程共享的初始 namespace。

## 5. PID namespace：独立的进程编号体系

### 5.1 基本语义

PID namespace 隔离进程 ID 的编号空间和“哪些进程可见”。

- 每个 PID namespace 中，第一个被创建的进程获得 PID 1。
- 该 namespace 内的进程只能看到同一 namespace 或其后代 namespace 中的进程。
- 祖先 namespace 可以看到后代 namespace 中的进程，但看到的是祖先 namespace 中的 PID。
- 一个进程只属于一个 PID namespace，但它的子进程可以进入新的、嵌套的 PID namespace。

因此同一个进程可能同时拥有多个 PID：容器内是小数字，宿主上是另一个数字，二者都指向同一个 `task_struct`。

### 5.2 PID 1 的特殊性

容器里的 PID 1 不是普通进程：

- 它是该 PID namespace 中的孤儿进程收养者，失去父进程的进程会被 reparent 到它。
- 对于没有安装处理函数的信号，PID 1 往往不会像普通进程那样被默认动作终止。这解释了为什么容器主进程是 shell 或某些不转发信号的程序时，`docker stop` 可能等很久，最后只能 `SIGKILL`。
- 从祖先 namespace 发送的 `SIGKILL` 或 `SIGSTOP` 仍然可以作用于它。
- PID 1 退出后，内核会向该 namespace 内其他进程发送 `SIGKILL`，整个 namespace 终止。

### 5.3 用 `unshare` 观察 PID namespace

```bash
sudo unshare --pid --fork --mount-proc bash
```

进入后执行：

```bash
echo $$
ps -ef
ls -l /proc/self/ns/pid
```

当前 shell 的 PID 很可能变成 1 或很小的数字，`ps` 也只显示这个 namespace 里的进程。三个参数要分清：

- `--pid`：创建新的 PID namespace。
- `--fork`：`unshare` 自己进入新 namespace 不会改变已有 PID，必须 fork 子进程，子进程才成为新 namespace 的 PID 1。
- `--mount-proc`：在新的 mount namespace 中重新挂载 `/proc`。否则 `ps` 读取旧 `/proc`，可能显示宿主进程和宿主 PID。

如果只执行 `sudo unshare --pid bash`，shell 的 PID 通常不变。这不是命令失效，而是只有之后 fork 出的子进程才进入新 PID namespace。

### 5.4 对比容器内 PID 与宿主 PID

假设容器主进程宿主 PID 是 `34821`：

```bash
readlink /proc/34821/ns/pid
docker exec -it <container> readlink /proc/1/ns/pid
```

若两个 inode 相同，说明它们指向同一个 PID namespace 实例。容器内路径是 `/proc/1/...`，宿主路径是 `/proc/34821/...`，但链接目标可以相同。

再执行：

```bash
ps -o pid,ppid,user,comm -p 34821
docker exec -it <container> ps -ef
```

宿主的 PID 是 `34821`，容器内是 `1`，但进程是同一个。`docker inspect -f '{{.State.Pid}}' <container>` 取得的是宿主 PID，`docker top <container>` 显示的通常也是宿主视角 PID。

### 5.5 用 `clone` 创建 PID namespace

```c
#define _GNU_SOURCE
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>
#define STACK_SIZE (1024 * 1024)

static int child_main(void *arg)
{
    (void)arg;
    printf("child: getpid()=%d, getppid()=%d\n", getpid(), getppid());
    return 0;
}

int main(void)
{
    char *stack = malloc(STACK_SIZE);
    if (!stack) { perror("malloc"); return 1; }

    pid_t pid = clone(child_main, stack + STACK_SIZE,
                      CLONE_NEWPID | SIGCHLD, NULL);
    if (pid == -1) { perror("clone"); free(stack); return 1; }

    if (waitpid(pid, NULL, 0) == -1) { perror("waitpid"); free(stack); return 1; }

    free(stack);
    return 0;
}
```

子进程打印的 `getpid()` 是 `1`，`getppid()` 是 `0`，因为父进程在新 PID namespace 之外，对它不可见。宿主上通过 `ps` 看到的仍是子进程的宿主 PID，而不是 1。

### 5.6 用 `unshare` 系统调用创建 PID namespace

`unshare(2)` 对 PID namespace 的关键语义是：**影响之后创建的子进程，而不是调用者自己。**

```c
#define _GNU_SOURCE
#include <sched.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
    if (unshare(CLONE_NEWPID) == -1) { perror("unshare"); return 1; }

    pid_t pid = fork();
    if (pid == -1) { perror("fork"); return 1; }

    if (pid == 0) {
        printf("child PID in new namespace: %d\n", getpid());
        execlp("sh", "sh", "-c",
               "ls -l /proc/self/ns/pid; echo '---'; ps -ef", NULL);
        perror("execlp");
        _exit(127);
    }

    if (waitpid(pid, NULL, 0) == -1) { perror("waitpid"); return 1; }
    return 0;
}
```

`unshare(CLONE_NEWPID)` 之后调用 `fork()`，子进程才进入新 PID namespace，并成为其中的 PID 1；父进程仍留在原 PID namespace。

## 6. mount namespace：独立的挂载表

mount namespace 隔离进程看到的挂载点集合，也就是文件系统树。每个 mount namespace 有自己的挂载表，一个 namespace 中的 `mount`、`umount`、`pivot_root` 操作，在另一个 namespace 中不一定可见。

容器运行时会利用它：

- 为容器准备独立根文件系统；
- 挂载 `/proc`、`/sys`、`/dev`；
- 把镜像目录绑定挂载到指定位置；
- 通过 `pivot_root` 切换根目录，让容器看不到宿主完整文件系统。

但这不意味着容器有独立磁盘。容器根文件系统仍是宿主文件系统上的目录、镜像层或联合文件系统视图。

观察命令：

```bash
cat /proc/self/mountinfo
findmnt
sudo unshare --mount bash
mkdir -p /tmp/ns-mount-demo
mount -t tmpfs tmpfs /tmp/ns-mount-demo
findmnt /tmp/ns-mount-demo
```

在另一个宿主 shell 中执行 `findmnt /tmp/ns-mount-demo`。若看不到该挂载，说明 mount namespace 是隔离的，并且传播类型为 private。

**挂载传播容易被忽略。** 常见类型有 `shared`、`slave`、`private`、`unbindable`。很多系统默认根挂载是 `shared`，创建新 mount namespace 后，挂载事件可能仍传播回宿主。做隔离实验时常见做法是：

```bash
sudo unshare --mount --propagation private bash
```

或在新 namespace 内执行 `mount --make-rprivate /`。这不是 namespace“漏了”，而是传播规则在起作用。

**`/proc` 要与 PID namespace 配合。** `/proc` 在挂载时与某个 PID namespace 关联。只创建新 PID namespace 却继续用旧 `/proc`，`ps` 读到的仍是旧 namespace 的信息。因此需要新的 mount namespace，并在其中重新挂载 `/proc`。这就是 `unshare --pid --fork --mount-proc` 中 `--mount-proc` 的意义。

## 7. network namespace：独立的网络栈视图

network namespace 隔离网络资源视图，包括网络接口、IPv4/IPv6 地址、路由表、ARP 表、端口空间、iptables/nftables 规则以及部分 `/proc/net` 信息。每个 network namespace 默认只有一张 `lo` 回环接口，而且通常处于 down 状态。

```bash
sudo unshare --net bash
ip link
ip addr
ip link set lo up
ping -c 1 127.0.0.1
```

新 namespace 里最初只看到：

```text
1: lo: <LOOPBACK> mtu 65536 qdisc noop state DOWN mode DEFAULT group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
```

这解释了为什么手工创建的 network namespace 或精简容器里，`127.0.0.1` 可能不通：不是服务写错，而是 `lo` 没有启用。

`ip netns` 可以按名字管理 network namespace：

```bash
sudo ip netns add demo
sudo ip netns list
sudo ip netns exec demo ip link
sudo ip netns exec demo ip link set lo up
sudo ip netns exec demo ip addr
```

连接两个 namespace 通常用 veth pair：

```bash
sudo ip link add veth-host type veth peer name veth-demo
sudo ip link set veth-demo netns demo
sudo ip addr add 10.200.0.1/24 dev veth-host
sudo ip link set veth-host up
sudo ip netns exec demo ip addr add 10.200.0.2/24 dev veth-demo
sudo ip netns exec demo ip link set veth-demo up
sudo ip netns exec demo ip link set lo up
ping -c 1 10.200.0.2
```

容器网络不是魔法，通常由 veth pair、bridge、路由和 NAT 组合而成：network namespace 提供隔离端点，veth 提供连接通道，宿主转发规则提供出口。

`nsenter` 可以进入目标进程的 network namespace：

```bash
sudo nsenter -t 34821 -n ip addr
sudo nsenter -t 34821 -n ss -lntp
sudo nsenter -t 34821 -n ip route
sudo nsenter -t 34821 -n -p -m bash
```

分别表示 network、PID、mount namespace。实际使用时注意：进入 PID namespace 通常需要 fork 子进程才能获得新的 PID 视图，而进入 network namespace 调用者会立即进入。

**端口空间独立。** 容器里监听 `8080` 指容器 network namespace 中的 `8080`；宿主上的 `8080` 是另一个 network namespace 中的端口，除非运行时做映射或代理。排查“端口不通”时依次确认：

1. 服务是否真的在容器内监听；
2. 监听地址是 `127.0.0.1` 还是 `0.0.0.0`；
3. 宿主是否有端口映射或负载均衡；
4. 防火墙、iptables、安全组是否放行；
5. 是否进错了 network namespace。

**网络隔离不等于带宽隔离。** network namespace 不负责限制带宽、每秒连接数，也不保证固定带宽。带宽和连接数限制通常由 cgroup、tc、eBPF 或运行时配置完成。

## 8. user namespace：UID/GID 映射与能力范围

user namespace 隔离用户和组 ID 的映射关系，以及 capability 的作用范围。在新的 user namespace 中，进程可以拥有“看起来是 root”的 UID 0，但这个 UID 0 在宿主上可能映射到普通用户，比如 UID 1000。

```bash
cat /proc/self/uid_map
cat /proc/self/gid_map
cat /proc/self/setgroups
```

映射示例：

```text
         0       1000          1
```

表示 namespace 内的 UID 0 映射到宿主 UID 1000，长度为 1。范围映射可能写成：

```text
         0       100000      65536
```

表示 namespace 内 UID 0 到 65535 映射到宿主 100000 到 165535。

创建 user namespace：

```bash
unshare --user --map-root-user bash
id
cat /proc/self/uid_map
```

你可能在这个 namespace 中变成 root，但宿主上的真实 UID 没有改变。user namespace 还允许非特权用户创建其他类型的 namespace：先创建 user namespace，再在其中创建 PID、mount、network namespace。这是 rootless 容器的基础之一。

Linux 把传统 root 权限拆成 capability，例如 `CAP_SYS_ADMIN`、`CAP_NET_ADMIN`、`CAP_NET_BIND_SERVICE`、`CAP_SYS_CHROOT`、`CAP_KILL`、`CAP_SYS_PTRACE`。user namespace 会影响 capability 的作用范围：进程可以在自己的 user namespace 内拥有完整 capability 集合，但这些能力不会自动作用于祖先 user namespace 或其他 namespace 中的资源。

所以“容器内是 root”不必然意味着“能在宿主上为所欲为”，但也不代表绝对安全。内核漏洞、错误挂载、过度 capability、宿主目录误挂载，都可能让隔离失效。

user namespace 主要解决 UID/GID 映射、非特权用户创建 namespace、capability 作用范围；它不自动解决容器能执行哪些系统调用、能用多少 CPU 和内存、能否访问宿主设备、镜像是否利用内核漏洞等问题。它只是安全拼图中的一块。

## 9. unshare 与 setns：创建还是加入

### 9.1 `unshare`：创建新 namespace

`unshare(2)` 让调用者脱离某些当前共享资源，并在指定类型上创建新 namespace。常见标志有 `CLONE_NEWPID`、`CLONE_NEWNS`、`CLONE_NEWNET`、`CLONE_NEWUSER`、`CLONE_NEWUTS`、`CLONE_NEWIPC`、`CLONE_NEWCGROUP`。对应命令行工具是 `unshare(1)`：

```bash
unshare --pid --fork --mount-proc bash
unshare --net bash
unshare --mount --propagation private bash
unshare --user --map-root-user bash
```

不同 namespace 对调用者影响不同：

- `CLONE_NEWPID`：调用者本身不改变 PID，之后 fork 的子进程进入新 PID namespace。
- `CLONE_NEWNET`：调用者立即进入新 network namespace。
- `CLONE_NEWNS`：调用者立即进入新 mount namespace。
- `CLONE_NEWUSER`：调用者立即进入新 user namespace，但 UID 映射需要额外配置。

### 9.2 `setns`：加入已有 namespace

`setns(2)` 让调用者加入一个已经存在的 namespace，通常接收指向 `/proc/<pid>/ns/<type>` 的文件描述符。

```c
#define _GNU_SOURCE
#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s /proc/<pid>/ns/<type>\n", argv[0]);
        return 1;
    }

    int fd = open(argv[1], O_RDONLY);
    if (fd == -1) { perror("open"); return 1; }

    if (setns(fd, 0) == -1) { perror("setns"); close(fd); return 1; }
    close(fd);

    /* PID namespace 必须 fork 后子进程才真正进入 */
    pid_t pid = fork();
    if (pid == -1) { perror("fork"); return 1; }

    if (pid == 0) {
        execlp("sh", "sh", NULL);
        perror("execlp");
        _exit(127);
    }

    if (waitpid(pid, NULL, 0) == -1) { perror("waitpid"); return 1; }
    return 0;
}
```

注意：

- 打开 namespace 链接和 `setns` 通常需要相应权限，常见要求是目标 user namespace 中的 `CAP_SYS_ADMIN`。
- 对 PID namespace，`setns` 不改变调用者自己的 PID，而是让之后 fork 的子进程进入目标 PID namespace。
- 不能通过 `setns` 加入祖先 PID namespace。
- 加入 mount namespace 还涉及 `CAP_SYS_CHROOT` 等额外约束。

`nsenter(1)` 本质上是对 `setns` 的封装，额外处理打开文件、fork、执行命令等细节。排障时优先用 `nsenter` 更省事；理解 `setns` 能帮助你明白它到底做了什么。

### 9.3 对比

| 维度 | `unshare` | `setns` |
| --- | --- | --- |
| 目的 | 创建新 namespace | 加入已有 namespace |
| 入口 | 系统调用标志 | `/proc/<pid>/ns/<type>` 文件描述符 |
| PID namespace | 影响之后 fork 的子进程 | 影响之后 fork 的子进程 |
| network/mount | 调用者立即进入新 namespace | 调用者立即进入目标 namespace |
| 常见工具 | `unshare(1)` | `nsenter(1)` |
| 典型场景 | 启动容器、创建隔离环境 | 进入容器排障、检查某进程视图 |

## 10. capability 与宿主内核共享

### 10.1 容器没有自己的内核

这是本课最重要的结论：

> 容器共享宿主内核。namespace 没有复制内核，也没有启动新内核。

验证方式：

```bash
uname -r
cat /proc/version
```

在宿主和容器中执行，输出通常完全一致。还可以观察：容器内不能加载只属于“容器内核”的模块；宿主内核升级后所有容器使用的内核随之变化；宿主内核漏洞影响所有容器；容器内的系统调用最终由宿主内核处理。

这与虚拟机有本质区别。虚拟机有独立内核，容器没有。容器轻量也源于此：启动容器不需要启动内核，只需创建 namespace、配置 cgroup、准备根文件系统、启动进程。

### 10.2 capability、seccomp、cgroup 的分工

传统 Unix 中 root 拥有几乎全部权限，Linux capability 把 root 权限拆成细粒度能力。容器运行时通常会裁剪 capability，只保留业务真正需要的能力，例如绑定低端口时添加 `CAP_NET_BIND_SERVICE`，避免使用 `--privileged`。

```bash
grep Cap /proc/self/status
capsh --print
```

seccomp 允许运行时限制容器可以执行哪些系统调用。namespace 改变“能看到什么资源”，seccomp 改变“能调用哪些内核接口”，二者不能互相替代。

cgroup 负责资源计量和限制，例如 CPU 使用时间、内存上限、块设备 I/O、进程数量上限。namespace 不负责这些：一个容器可以有独立 PID namespace，仍然可以吃满宿主 CPU；也可以有独立 network namespace，仍然可以占满带宽。

| 机制 | 主要职责 | 典型问题 |
| --- | --- | --- |
| namespace | 隔离资源视图和命名空间 | 容器内看到的 PID、网卡、挂载点与宿主不同 |
| cgroup | 限制和计量 CPU、内存、I/O、进程数 | 容器能吃多少资源 |
| capabilities | 拆分 root 权限 | 容器内 root 能做什么管理操作 |
| seccomp | 过滤系统调用 | 容器能调用哪些内核接口 |
| LSM | 强制访问控制 | 容器能访问哪些文件、网络、对象 |

## 11. 常见误区

**误区一：namespace 会自动限制 CPU 和内存。** 错误。namespace 主要提供视图隔离，CPU 配额、内存上限、I/O 限速、进程数上限由 cgroup 负责。容器即使有独立 PID、network、mount namespace，也可能打满宿主 CPU 或触发宿主 OOM。

**误区二：namespace 是完整安全沙箱。** 错误。namespace 是重要基础，但容器与宿主共享内核。内核漏洞、过多 capability、错误挂载、未启用 seccomp、以 root 运行且未启用 user namespace、运行时配置错误，都可能削弱隔离。生产环境通常需要组合使用非 root、capability 裁剪、seccomp、LSM、只读文件系统、最小挂载和网络策略。

**误区三：容器里看到的 PID 就是宿主 PID。** 错误。容器内通常是容器 PID namespace 中的编号，宿主上是另一个编号。找容器主进程宿主 PID 用：

```bash
docker inspect -f '{{.State.Pid}}' <container>
docker top <container>
```

`docker top` 显示的通常是宿主视角 PID。

**误区四：容器里 `ps` 看到宿主进程，说明没有隔离。** 不一定。更常见的原因是 `/proc` 没有针对新 PID namespace 重新挂载。检查：

```bash
ls -l /proc/self/ns/pid
readlink /proc/self/ns/pid
cat /proc/self/mountinfo | grep ' /proc '
```

**误区五：`unshare --pid` 之后当前 shell 的 PID 立刻变成 1。** 错误。需要 `--fork`，子进程才进入新 namespace 并成为 PID 1。

**误区六：`setns` 进入 PID namespace 后当前进程立刻获得新 PID。** 错误。它主要影响之后 fork 的子进程。

**误区七：有 network namespace 就有独立带宽。** 错误。带宽限制需要 tc、cgroup、eBPF 或运行时配置。

**误区八：容器里的 root 就是宿主 root。** 不一定。启用 user namespace 映射后，容器 UID 0 可能映射到宿主普通 UID；即使没启用，容器 root 通常也被 capability 裁剪和 seccomp 限制。

## 12. 设计权衡

**容器为什么轻量。** 共享宿主内核，不需要为每个容器启动完整内核；启动快、资源开销小、镜像可分层复用、密度高，适合微服务和弹性伸缩。namespace 让多个容器在同一内核上拥有相对独立的视图，cgroup 让它们共享资源但受到限制。

**轻量的代价。** 共享内核意味着内核漏洞影响面更大，隔离强度通常弱于虚拟机，运行时配置对安全影响极大，容器逃逸后果可能严重。安全要求很高的场景可能选择 gVisor、Kata Containers 或其他更强隔离方案，或把敏感服务放在独立节点、独立集群。

**隔离强度与运维复杂度的权衡。**

- namespace + cgroup：轻量、简单，但安全边界依赖运行时配置。
- 加 user namespace：降低容器内 root 风险，但增加 UID 映射、卷权限、镜像兼容性的复杂度。
- 加 seccomp 和 LSM：提高安全性，但可能导致某些应用行为异常，需要调试策略。
- 使用 gVisor、Kata、VM：安全性更高，但启动更慢、资源开销更大、运维更复杂。

没有“默认正确”的答案，只有与业务风险、性能要求和运维能力匹配的选择。

**可观测性与排障的权衡。** namespace 让容器拥有独立视图，也让排障变复杂：容器内 PID 与宿主 PID 不一致，网络和挂载视图也不一致。好的实践是：日志中同时记录容器内 PID 和可关联的请求 ID；排障时先确认自己在哪个 namespace；使用 `docker inspect`、`nsenter`、`lsns` 建立宿主与容器的对应关系；不要把容器内 PID 直接当作宿主 PID 去 `kill`。

## 13. 动手实践：查看容器 namespace 链接，对比宿主和容器 PID

本实践必须在**受控开发环境**中进行，例如自己的开发机、测试集群或临时虚拟机。不要在生产节点上随意创建 namespace、修改挂载或执行 `nsenter`。准备一台 Linux 开发机、可用的 Docker 或 Podman、可执行 `docker`/`lsns`/`readlink` 的权限；部分 `nsenter` 命令可能需要 `sudo`。示例容器使用 Alpine，命令中的容器名、PID 和路径请按实际环境替换。

### 13.1 步骤一：观察宿主当前进程的 namespace 链接

```bash
ls -l /proc/self/ns
readlink /proc/self/ns/pid
readlink /proc/self/ns/mnt
readlink /proc/self/ns/net
readlink /proc/self/ns/user
lsns
```

记录每个链接的 inode 号，尤其是 `pid`、`mnt`、`net`、`user`。验收点：能说出 `/proc/self/ns` 中每个链接代表哪类 namespace，以及方括号中的数字是什么。

### 13.2 步骤二：启动测试容器

```bash
docker run -d --name ns-demo alpine sleep 3600
docker ps --filter name=ns-demo
```

### 13.3 步骤三：获取容器主进程的宿主 PID

```bash
docker inspect -f '{{.State.Pid}}' ns-demo
```

记下输出，例如 `34821`。这是**宿主 PID namespace 中的 PID**，不是容器内看到的 PID。也可以执行 `docker top ns-demo`，观察 PID 列。验收点：能解释 `docker inspect` 得到的 PID 属于哪个 PID namespace 的编号体系。

### 13.4 步骤四：对比容器内 PID 与宿主 PID

```bash
docker exec -it ns-demo ps -ef
```

这里的 `1` 是容器 PID namespace 中的 PID。回到宿主：

```bash
ps -o pid,ppid,user,comm -p 34821
readlink /proc/34821/ns/pid
docker exec ns-demo readlink /proc/1/ns/pid
```

宿主上能看到 `sleep 3600`，但 PID 是 `34821`。若两条 `readlink` 输出的 inode 相同，说明正在从两个视角观察同一个 PID namespace。验收点：能用一句话解释“为什么容器内是 PID 1，宿主上是 PID 34821”。

### 13.5 步骤五：查看容器进程的 namespace 链接

```bash
ls -l /proc/34821/ns
lsns -p 34821
docker exec ns-demo ls -l /proc/self/ns
```

与步骤一记录的宿主 namespace inode 对比。通常可以看到：容器的 `pid`、`mnt`、`net` namespace 与宿主不同；取决于运行时配置，`user` namespace 可能相同也可能不同；`uts`、`ipc` 等通常也不同。验收点：能指出哪些 namespace 不同，并解释这些差异分别影响什么可见性。

### 13.6 步骤六：进入容器的 network namespace 观察

```bash
docker exec ns-demo ip addr
docker exec ns-demo ip route
sudo nsenter -t 34821 -n ip addr
sudo nsenter -t 34821 -n ip route
sudo nsenter -t 34821 -n ss -lntp
ip addr
ip route
ss -lntp
```

对比容器内、容器 network namespace、宿主三者的网络视图。容器内的 `eth0`、路由和端口空间与宿主不同，容器内的 `127.0.0.1` 也只属于容器的 network namespace。验收点：能解释为什么容器内监听 `127.0.0.1:8080` 时，宿主不能直接通过宿主 `127.0.0.1:8080` 访问。

### 13.7 步骤七：对比内核版本

```bash
uname -r
uname -a
docker exec ns-demo uname -r
docker exec ns-demo uname -a
```

两者通常完全一致，或至少内核版本一致。这直接说明容器共享宿主内核，没有独立内核。验收点：能根据 `uname -r` 的输出解释“容器不是虚拟机”。

### 13.8 步骤八：清理

```bash
docker rm -f ns-demo
# 如果做过第 7 节的 ip netns 实验，再清理：
sudo ip netns del demo
sudo ip link del veth-host 2>/dev/null
```

### 13.9 实践验收清单

- [ ] 我能找到 `/proc/<pid>/ns/` 下各类 namespace 的链接。
- [ ] 我能用 `readlink` 读出 namespace inode。
- [ ] 我能用 `lsns` 查看进程所属 namespace。
- [ ] 我能用 `docker inspect` 获取容器主进程的宿主 PID。
- [ ] 我能对比容器内 PID 与宿主 PID，并解释差异。
- [ ] 我能用 `nsenter` 进入容器的 network namespace 观察网络。
- [ ] 我能用 `uname -r` 说明容器与宿主共享内核。
- [ ] 我能明确说出 namespace 不负责 CPU、内存和系统调用权限限制。
- [ ] 我能说出 cgroup、capabilities、seccomp 各自负责什么。

## 14. 自测题

### 题一：容器内 `ps -ef` 只看到 PID 1 和当前命令，宿主上却有很多进程。为什么？

**参考答案：** 容器运行在独立的 PID namespace 中。PID namespace 隔离进程编号和可见进程集合，容器内进程只能看到同一 PID namespace 及其后代 namespace 中的进程，所以通常只能看到自己的主进程和当前命令。宿主处于祖先 PID namespace，可以看到包括容器进程在内的更多进程。两个视角看到的 PID 编号也可能不同：容器内是 1，宿主上是另一个数字，但指向同一个进程。如果容器内反而看到宿主全部进程，要检查 `/proc` 是否针对新 PID namespace 正确挂载，而不是直接断定“没有 PID namespace”。

### 题二：给容器配置了 PID、mount、network namespace，是否意味着它最多只能用 2 核 CPU 和 4 GB 内存？

**参考答案：** 不是。namespace 负责资源视图和命名空间隔离，不负责资源配额。CPU 核数、CPU 时间、内存上限、I/O 带宽、进程数上限由 cgroup 或运行时资源限制负责。一个拥有独立 namespace 的容器仍然可能吃满宿主 CPU 或耗尽内存。要限制资源，需要设置 cgroup 限制，例如 CPU quota、memory limit、pids limit。

### 题三：容器内执行 `uname -r` 与宿主完全一致，说明了什么？这对安全有什么含义？

**参考答案：** 说明容器与宿主共享同一个内核。容器没有自己的内核，namespace 只是让容器进程看到独立的资源视图；`uname` 读取的是宿主内核信息，所以结果一致。安全含义是：宿主内核漏洞会影响所有容器；容器内执行系统调用最终由宿主内核处理；容器安全边界不仅依赖 namespace，还依赖 capability 裁剪、seccomp、LSM、user namespace、只读文件系统、最小挂载和运行时配置。容器轻量，但不能因此认为它等价于完整虚拟机。

## 15. 推荐资源

- **namespace与隔离边界：Linux man-pages**  
  https://man7.org/linux/man-pages/man7/namespaces.7.html  
  来源：man7.org；语言：英文；类型：文档；难度：进阶。  
  说明：重点阅读 PID、mount、network 与 user namespace，以及 `unshare` 和 `setns` 相关章节，用于核对本节点实验的语义和边界。建议结合本课实践步骤，边看边在受控环境中验证。

本地可继续查阅：

```bash
man 7 namespaces
man 2 unshare
man 2 setns
man 2 clone
man 1 unshare
man 1 nsenter
man 1 lsns
man 5 proc
```

## 16. 本课小结

- namespace 是 Linux 内核提供的资源视图隔离机制，是容器的基础之一，但不是完整虚拟机。
- PID namespace 隔离进程编号和可见进程集合；mount namespace 隔离挂载表；network namespace 隔离网络设备和端口空间；user namespace 隔离 UID/GID 映射和 capability 作用范围。
- `unshare` 创建新 namespace，`setns` 加入已有 namespace；PID namespace 对两者的影响都主要体现在之后 fork 的子进程。
- 容器与宿主共享内核，`uname -r` 一致是直接证据。
- namespace 不限制 CPU、内存和系统调用权限；资源限制靠 cgroup，权限控制靠 capabilities，系统调用过滤靠 seccomp，强制访问控制靠 LSM。
- 容器轻量、密度高，但安全边界需要运行时配置共同保证。排障时先确认自己在看哪个 namespace 的视图，再解读 PID、端口和挂载路径。
