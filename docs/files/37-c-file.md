# 第 37/208 课：文件读写、页缓存与持久性

> 领域：C · 阶段 2 · 子类：文件与内存 · 难度：基础 · 前置：c-fd（文件描述符与打开文件描述）
>
> 节点一句话：**普通文件 IO 通常经过页缓存，write 成功表示内核接收数据，不等同于掉电后数据持久。**

（说明：本课由 opencode 生成超时，基于节点原始内容人工编写，实践脚本已在本机验证通过。）

---

## 一、学习目标 & 为什么后端要懂

**学完你应该能：**

1. 解释**页缓存（page cache）与脏页（dirty page）**：write 之后数据在哪、什么时候才真的落盘。
2. 独立实现**「写临时文件 → fsync → 原子 rename → fsync 目录」**这套配置安全更新流程。

**验收标准（节点原文）：**

- 能说明 write 与 fsync 的区别；
- 崩溃恢复测试不会读到半份配置。

**为什么后端要懂（节点：设计可靠日志与配置文件更新）：**
在微信公众号后台这类服务里，本地文件随处可见：下发到机器的路由/限流配置、本地落盘的业务日志或 binlog 式的操作记录、断点续传的 checkpoint。常见事故有两种：

- 配置推送过程中进程或机器挂了，服务重启读到**半份 JSON**，解析失败直接起不来；
- 日志"写成功"了、机器掉电后却**少了最后几秒**，对账对不上。

两者的根源都是：搞不清 write、页缓存、fsync、rename 各自保证了什么。

---

## 二、从直觉到原理

### 2.1 页缓存与脏页

**直觉：** `write()` 返回了 n，数据就"写到磁盘"了？——不是。

**原理：** 普通（非 `O_DIRECT`）文件 IO 走内核的页缓存：

```
用户 buffer --write()--> 页缓存中的页（标记为 dirty）--回写线程(稍后)--> 磁盘
                                     ^
                          read() 优先从这里读
```

- `write()` 成功 = 数据已拷贝进内核页缓存，页被标记为**脏页**。此时**别的进程 read 立刻能看到新数据**（可见性），但**掉电后不一定还在**（持久性）。
- 内核的回写（writeback）会在稍后把脏页写到设备，时机受 `vm.dirty_expire_centisecs`、`vm.dirty_writeback_centisecs`、`vm.dirty_ratio` 等参数影响。
- 进程崩溃（kill -9、段错误）**不会**丢已 write 进页缓存的数据——数据在内核里；**机器掉电/内核崩溃**才会丢未回写的脏页。这两种"崩溃"要分清。

**动手看：**

```bash
free -m                                # buff/cache 一列就是页缓存等占用
grep -E '^(Dirty|Writeback):' /proc/meminfo   # 当前脏页量、正在回写量
dd if=/dev/zero of=/tmp/big bs=1M count=500   # 写 500MB（不加 conv=fsync）
grep -E '^(Dirty|Writeback):' /proc/meminfo   # Dirty 明显上涨
sync                                   # 要求把脏数据写回
grep -E '^(Dirty|Writeback):' /proc/meminfo   # Dirty 回落
vmstat 1 5                             # bo 列：每秒写出到块设备的量
sysctl vm.dirty_expire_centisecs vm.dirty_ratio vm.dirty_background_ratio
```

你会发现 `dd` 很快就"写完"，真正的磁盘写发生在之后（或 `sync` 时）。加上 `conv=fsync` 再跑一次对比耗时，就能直观感到"持久"的代价。

### 2.2 fsync / fdatasync

要持久性，必须显式要求：

| 调用 | 保证 |
|---|---|
| `fsync(fd)` | 把该文件的**数据和元数据**（大小、mtime 等 inode 信息）刷到存储设备，返回成功后才算落盘（并请求设备刷写其易失缓存） |
| `fdatasync(fd)` | 只刷数据和**读回数据所必需的元数据**（如文件大小变化）；mtime 这类非必需元数据可以不刷，所以通常更快 |
| `sync()` / `syncfs(fd)` | 整个系统 / 整个文件系统的脏数据 |

（以上语义以节点资源 [fsync(2) man page](https://man7.org/linux/man-pages/man2/fsync.2.html) 为准。）

```c
ssize_t n = write(fd, rec, len);   // 只进页缓存
if (n != (ssize_t)len) { /* 短写/错误要处理，见第35课 */ }
if (fdatasync(fd) < 0) {           // 这一步返回成功，才算"写进盘"
    perror("fdatasync");           // 注意：fsync 失败要当成数据可能丢失来处理
}
```

要点：

- **fsync 的返回值必须检查。** 回写出错（如 EIO）会通过 fsync 报告；报错后不要假设"再 fsync 一次就好了"——出错的脏页可能已被丢弃，稳妥做法是认为这段数据没写成功，从上游重做。
- `close()` **不等于** fsync，close 成功也不代表数据落盘。
- 对日志类追加写，`fdatasync` 往往足够：文件变长时大小这一元数据也会被刷。

用 strace 观察一个程序有没有真的 fsync：

```bash
strace -f -e trace=openat,write,fsync,fdatasync,rename,close ./your_program
```

### 2.3 rename 与目录持久性

**问题：** 直接 `open(path, O_TRUNC)` 覆盖写配置，写到一半崩了，文件里就是半份配置。

**解法：写临时文件 + rename。** 在同一文件系统内，`rename(tmp, path)` 是原子的：任何时刻别人 open(path) 要么拿到完整旧文件，要么拿到完整新文件，不会看到中间态。

**但节点特别强调的误区：原子 rename 只保证可见性边界，不自动保证所有崩溃持久性。**

- rename 修改的是**目录**（目录项从指向旧 inode 变为指向新 inode），这个目录修改本身也在缓存里；掉电后可能回到 rename 之前。
- 如果你在 rename 前没 fsync 临时文件，可能出现"rename 已持久、但新文件数据还没落盘"——重启后看到**新名字指向空文件或残缺数据**。

所以完整流程是四步：

```
1. open(tmp) + write 全部内容
2. fsync(tmp_fd)          // 新内容先落盘
3. rename(tmp, path)      // 原子切换可见性
4. fsync(目录 fd)         // 让"这次 rename"这一目录修改也落盘
```

打开目录做 fsync：`int dfd = open(dir, O_RDONLY | O_DIRECTORY); fsync(dfd);`。新建文件时同理：文件本身 fsync 了，但它所在目录没 fsync，掉电后这个文件名可能不存在。

---

## 三、常见误区

1. **"write 返回成功 = 数据安全了"**：只是进了页缓存。进程崩溃不丢，掉电可能丢。
2. **"close 会帮我刷盘"**：不会。
3. **"rename 是原子的，所以就是持久的"**（节点 pitfalls）：rename 只保证可见性原子，持久还要靠 rename 前 fsync 文件、rename 后 fsync 目录。
4. **直接 O_TRUNC 覆盖配置**：写一半崩溃就留下半份配置（下面实践里有反例实测）。
5. **临时文件放在 /tmp**：/tmp 常常是另一个文件系统（甚至 tmpfs），跨文件系统 rename 会失败（EXDEV）。临时文件要和目标放**同一目录**。
6. **fsync 失败后重试当没事**：应当视为数据可能已丢失。

## 四、设计权衡

节点：**批量同步提升吞吐，但扩大允许丢失的数据窗口。**

- 每条日志都 fsync：最安全，但每次都要等设备，吞吐受限于磁盘刷写延迟。
- 每 N 条或每 T 毫秒 fsync 一次（组提交 / group commit）：吞吐高得多，代价是掉电时最多丢最近 N 条或 T 毫秒的数据。
- 完全不 fsync、交给内核回写：最快，丢失窗口最大（量级取决于 `vm.dirty_*` 参数）。

工程上要先回答"这份数据允许丢多少"：业务访问日志丢几秒通常可以接受；资金、关键状态变更通常要求写入确认前就落盘（或依赖带复制的存储），这时宁可降吞吐。配置文件更新频率低，每次都走完整四步流程即可，代价可以忽略。

---

## 五、动手实践：原子替换配置文件

节点 practice：**实现临时文件写入、同步、原子替换和目录同步流程。**

### 步骤 1：写 `atomic_write.c`

```c
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int write_all(int fd, const char *p, size_t n) {
    while (n > 0) {
        ssize_t w = write(fd, p, n);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        p += w; n -= (size_t)w;
    }
    return 0;
}

// crash_at: 0=不崩溃, 1=写一半后崩溃, 2=fsync 后 rename 前崩溃
int atomic_write(const char *path, const char *data, size_t len, int crash_at) {
    char tmp[4096], dirbuf[4096];
    snprintf(tmp, sizeof tmp, "%s.tmp.%d", path, getpid());   // 同目录临时文件
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    if (crash_at == 1) { write_all(fd, data, len / 2); _exit(9); }
    if (write_all(fd, data, len) < 0 || fsync(fd) < 0) { close(fd); unlink(tmp); return -1; }
    if (close(fd) < 0) { unlink(tmp); return -1; }
    if (crash_at == 2) _exit(9);
    if (rename(tmp, path) < 0) { unlink(tmp); return -1; }   // 原子切换
    snprintf(dirbuf, sizeof dirbuf, "%s", path);
    int dfd = open(dirname(dirbuf), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (dfd < 0) return -1;
    int r = fsync(dfd);                                       // 目录持久
    close(dfd);
    return r;
}

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: %s path content crash_at\n", argv[0]); return 2; }
    if (atomic_write(argv[1], argv[2], strlen(argv[2]), atoi(argv[3])) < 0) { perror("atomic_write"); return 1; }
    return 0;
}
```

### 步骤 2：写反例 `naive_write.c`（直接 O_TRUNC 覆盖）

```c
#include <fcntl.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    int fd = open(argv[1], O_WRONLY | O_TRUNC);
    size_t n = strlen(argv[2]);
    write(fd, argv[2], atoi(argv[3]) ? n / 2 : n);
    if (atoi(argv[3])) _exit(9);       // 模拟写一半崩溃
    close(fd); return 0;
}
```

### 步骤 3：崩溃恢复测试 `test.sh`

测试内容：

1. 写一半崩溃 → 目标文件仍是完整旧配置；
2. fsync 后、rename 前崩溃 → 仍是完整旧配置；
3. 正常执行 → 完整新配置；
4. 一个进程反复原子替换 300 次，同时读 3000 次 → 读者从未看到半份配置；
5. 反例 naive_write 写一半崩溃 → 留下半份配置；
6. （装了 strace 时）检查系统调用顺序是 `write → fsync → rename → fsync`。

```bash
gcc -O2 -Wall -fsanitize=address,undefined -o atomic_write atomic_write.c
gcc -O2 -o naive_write naive_write.c
./test.sh
```

本机实测输出：

```
PASS crash_mid_write_keeps_old
PASS crash_before_rename_keeps_old
PASS normal_replace_new
PASS reader_never_sees_partial
PASS naive_truncate_leaves_half_config (len=38)
Dirty:              6776 kB
Writeback:             0 kB
ALL_CHECKS_PASSED
```

（完整 test.sh 见课末附录。）

### 步骤 4：自己观察系统调用

```bash
strace -e trace=openat,write,fsync,rename ./atomic_write /tmp/x/app.conf '{"v":3}' 0
```

确认 `fsync(3)` 出现在 `rename` 之前，`rename` 之后还有一次对目录 fd 的 `fsync`。

### 说明：这个测试测不到什么

`_exit` 模拟的是**进程崩溃**，此时页缓存还在，所以它验证的是"可见性原子"（不会读到半份）。**掉电持久性**（fsync 那两步的价值）进程级测试测不出来，需要虚拟机断电或 dm-flakey 之类的故障注入设备——这正是节点 pitfalls 想让你分清的两件事。

---

## 六、自测题

**1. 进程 write 了 1KB 日志后被 kill -9，这 1KB 会丢吗？如果是机器掉电呢？**

<details><summary>答案</summary>
kill -9 不会丢：write 成功时数据已在内核页缓存里，进程死了内核还会照常回写。掉电可能丢：如果这些脏页还没回写、也没 fsync 过，就会丢失。write 管的是"内核已接收"，fsync 管的是"已落到存储"。
</details>

**2. 某服务更新配置的流程是"写 app.conf.tmp → rename 到 app.conf"，没有任何 fsync。会有什么风险？**

<details><summary>答案</summary>
进程崩溃时没问题，读者只会看到完整旧版或新版。但掉电后可能出现：rename 这一目录修改已落盘，而 tmp 文件的数据还没落盘，重启后 app.conf 是空文件或残缺内容；也可能 rename 本身丢了，回到旧配置。正确做法是 rename 前 fsync 临时文件，rename 后 fsync 所在目录。
</details>

**3. 日志服务吞吐不够，有人提议从"每条 fsync"改成"每 100ms fsync 一次"。你怎么评估？**

<details><summary>答案</summary>
这是节点讲的权衡：批量同步能大幅提升吞吐，但掉电时最多丢最近约 100ms 写入的数据。要看业务能否接受这个丢失窗口；如果日志需要"返回给调用方成功前已持久"，那就只能在 fsync 完成后再确认（组提交：攒一批，一起 fsync，再一起回复），而不能先回复后刷盘。
</details>

---

## 七、推荐资源

- [文件读写、页缓存与持久性：Linux man-pages（fsync(2)）](https://man7.org/linux/man-pages/man2/fsync.2.html)（man7.org，英文，文档，进阶，免费）——重点阅读页缓存与脏页、fsync/fdatasync，用于核对本节点实验的语义和边界。

节点的源码学习建议：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

---

## 附录：test.sh

```bash
#!/bin/bash
set -u
cd "$(dirname "$0")"
gcc -O2 -Wall -fsanitize=address,undefined -o atomic_write atomic_write.c || exit 1
gcc -O2 -o naive_write naive_write.c || exit 1
ok=1; D=$(mktemp -d); F=$D/app.conf
OLD='{"version":1,"timeout_ms":200,"backends":["10.0.0.1","10.0.0.2"]}'
NEW='{"version":2,"timeout_ms":300,"backends":["10.0.0.1","10.0.0.2","10.0.0.3"]}'
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; ok=0; fi; }
printf '%s' "$OLD" > $F
./atomic_write $F "$NEW" 1; check "crash_mid_write_keeps_old" "$(cat $F)" "$OLD"
./atomic_write $F "$NEW" 2; check "crash_before_rename_keeps_old" "$(cat $F)" "$OLD"
./atomic_write $F "$NEW" 0; check "normal_replace_new" "$(cat $F)" "$NEW"
rm -f $D/*.tmp.*
# 并发读者：反复读，只应看到完整旧版或完整新版
printf '%s' "$OLD" > $F
( for i in $(seq 1 300); do ./atomic_write $F "$([ $((i%2)) = 0 ] && echo "$OLD" | tr -d '\n' || echo "$NEW" | tr -d '\n')" 0; done ) &
bad=0; for i in $(seq 1 3000); do c=$(cat $F 2>/dev/null); [ "$c" = "$OLD" ] || [ "$c" = "$NEW" ] || bad=$((bad+1)); done; wait
check "reader_never_sees_partial" "$bad" "0"
# 反例
printf '%s' "$OLD" > $F
./naive_write $F "$NEW" 1
c=$(cat $F); if [ "$c" != "$OLD" ] && [ "$c" != "$NEW" ]; then echo "PASS naive_truncate_leaves_half_config (len=${#c})"; else echo "FAIL naive"; ok=0; fi
# strace 看系统调用顺序
if command -v strace >/dev/null; then
  gcc -O2 -o aw_plain atomic_write.c
  seq=$(strace -f -e trace=openat,write,fsync,rename,close ./aw_plain $F "$NEW" 0 2>&1 | grep -oE '^(openat|write|fsync|rename)' | grep -v '^openat$' | tr '\n' ' ')
  echo "strace sequence: $seq"
  echo "$seq" | grep -q 'write fsync rename fsync' && echo "PASS syscall_order" || { echo "FAIL syscall_order"; ok=0; }
fi
grep -E '^(Dirty|Writeback):' /proc/meminfo
rm -rf $D
[ $ok = 1 ] && echo ALL_CHECKS_PASSED || { echo SOME_CHECKS_FAILED; exit 1; }
```
