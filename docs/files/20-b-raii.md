# 第 20/208 课：RAII与资源所有权

> 节点 id：`b-raii` · 域：B · 子类：生命周期与所有权 · 难度：基础 · 前置：`b-lifetime`
>
> 一句话：RAII将资源获取和对象初始化关联，用确定的析构路径释放文件、锁和内存等资源。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释构造建立不变量。** 说清“对象构造成功 = 资源已获取且对象处于可用状态”（或明确的空状态）；构造失败时不应留下半初始化、半打开的资源；成员子对象如何帮助分层获取资源。
2. **独立完成节点实践。** 实现禁止拷贝、可移动的 `UniqueFd`，覆盖关闭失败（记录错误、析构不抛异常）与移动后状态（源对象置无效、不重复 `close`）。
3. **解释析构释放与 `noexcept`。** 知道为什么析构函数不宜抛异常，栈展开过程中再抛异常会直接 `std::terminate`，以及 `close`/`unlock` 失败应如何报告。
4. **掌握唯一所有权与移动转移。** 能说清“谁拥有 fd / 锁 / 临时文件”，并能用移动语义把所有权交给调用方、会话对象或异步任务，而不是靠裸 `int fd` 和口头约定。

### 1.2 验收标准回扣

节点给出的验收标准有两条，本课与实践会反复验证：

- **提前返回和抛异常后 fd 数量不增长。** 用 `/proc/self/fd` 统计打开的描述符；`UniqueFd` 作为自动对象，无论正常离开作用域、`return`，还是异常栈展开，都应在析构路径上关闭资源。进函数前与出函数后计数相等，就是“没有泄漏”的可观测证据。
- **移动后源对象不重复关闭。** 移动构造 / 移动赋值后，源对象持有无效值（如 `-1`），析构时不再对原 fd 调用 `close`；目标对象是唯一所有者。若源对象仍持有旧 fd，两个析构会双重关闭——第二个常是 `EBADF`，更糟时 fd 号已被内核复用，关到别人的连接。

### 1.3 为什么微信公众号后台要懂

节点对“为什么”写得很直接：**让异常与提前返回不会泄漏资源。** 放进腾讯微信公众号后台的典型 C++ 服务，就是下面这些具体场景：

- **Socket / 连接 fd。** 接入层或与下游（消息存储、用户资料、模板消息投递、回调转发）通信时，`socket` / `accept` / `connect` 得到的是裸 `int`。若在解析 XML/JSON、验签、解密、写日志的中途 `return` 或抛异常，忘记 `close`，短连接高峰下会迅速耗尽 fd 上限（`ulimit -n`），表现为 `EMFILE` / `ENFILE`，整机接入能力被拖垮。线上排查时，`lsof -p <pid> | wc -l` 或看 `/proc/<pid>/fd` 会发现描述符只增不减。
- **互斥锁与临界区。** 推送队列、access_token 缓存、限流计数、会话表常需要加锁。手写 `mutex.lock()` / `unlock()` 一旦中间有 `return`、`continue` 或异常，锁永远不释放，后续请求全部卡住——这是比泄漏 fd 更“静默致命”的故障：进程还在，QPS 却掉到接近 0。`std::lock_guard` / `std::unique_lock` 就是锁上的 RAII。
- **临时文件与上传落盘。** 处理媒体上传、素材转码、导出报表时可能 `mkstemp` 出临时文件。请求被取消、MD5 校验失败、病毒扫描拒绝而提前返回时，若没有析构路径去 `close` + `unlink`，磁盘与 fd 都会慢慢漏，最终打满临时目录 inode 或空间。
- **数据库连接、内存缓冲、mmap。** 连接池借出的连接、大包零拷贝映射、共享内存段，本质都是“获取—使用—归还”。RAII 把“归还”绑在作用域结束上，比靠人工记得每个分支的 `finally` 更稳，也比“注释里写请记得关闭”更可审查。
- **与第 19 课生命周期的衔接。** 第 19 课强调“对象何时生、何时死”；本课强调“死的时候必须把资源还回去”。悬垂引用是“用了已死对象”；资源泄漏是“对象死了但资源没死”。后台故障里两者经常结伴出现：泄漏把 fd 耗尽，随后新连接失败；或错误的生命周期让 RAII 对象过早析构，异步回调 use-after-close。
- **异步回调不能指望局部 RAII 自动跨线程。** 把 `UniqueFd` 或 `lock_guard` 的生命周期想成“过了线程边界还有效”是错的——局部确定性只覆盖当前作用域与当前栈展开。跨线程、跨事件循环仍要明确所有权转移（移动进任务对象、`shared_ptr` 等），这正是节点 tradeoffs 要强调的。

所以本课的工程习惯是：**资源的获取写在构造里，释放写在析构里；需要转移时用移动，而不是复制一个会“双重释放”的句柄；跨异步边界时显式设计所有者，而不是默默引用捕获局部 RAII。**

## 2. 从直觉到原理：三个核心要点

### 2.1 构造建立不变量

#### 直觉：对象一出生就该“能用或明确为空”

RAII（Resource Acquisition Is Initialization）字面意思是：**资源获取即初始化**。更贴工程的说法是：

- 构造函数负责拿到资源（打开文件、加锁、`socket`、`mmap`……）；
- 构造成功后，对象满足一组**不变量**（例如：`fd_ >= 0` 且指向已打开的描述符；或 `fd_ == -1` 表示空对象）；
- 之后成员函数可以假定不变量成立，不必每次都问“资源到底拿到没有”。

对比裸资源写法：

```cpp
int fd = open("/tmp/wx-upload.bin", O_RDWR | O_CREAT | O_CLOEXEC, 0600);
if (fd < 0) {
    return -1;
}
// ... 中间有 20 行解析、验签、写日志 ...
if (!signature_ok) {
    // 忘了 close(fd) —— 泄漏
    return -1;
}
if (!quota_ok) {
    close(fd);  // 这条路径记得了
    return -1;
}
close(fd);
```

问题不在于 `open`/`close` 本身难，而在于**每一条提前返回路径都要记得释放**。路径一多，必漏。代码审查时很难用眼睛扫全部分支。

RAII 写法把不变量收进类型：

```cpp
#include <fcntl.h>
#include <stdexcept>
#include <string>
#include <unistd.h>
#include <utility>

class UniqueFd {
public:
    UniqueFd() noexcept = default;
    explicit UniqueFd(int fd) noexcept : fd_(fd) {}

    ~UniqueFd() noexcept { reset(); }

    UniqueFd(const UniqueFd&) = delete;
    UniqueFd& operator=(const UniqueFd&) = delete;

    UniqueFd(UniqueFd&& other) noexcept : fd_(other.fd_) { other.fd_ = -1; }
    UniqueFd& operator=(UniqueFd&& other) noexcept {
        if (this != &other) {
            reset();
            fd_ = other.fd_;
            other.fd_ = -1;
        }
        return *this;
    }

    int get() const noexcept { return fd_; }
    bool valid() const noexcept { return fd_ >= 0; }

    void reset(int new_fd = -1) noexcept {
        if (fd_ >= 0) {
            int rc;
            do {
                rc = ::close(fd_);
            } while (rc < 0 && errno == EINTR);
            // 失败只记录，见 2.2
            fd_ = -1;
        }
        fd_ = new_fd;
    }

private:
    int fd_{-1};
};

UniqueFd open_upload(const std::string& path) {
    int fd = ::open(path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        throw std::runtime_error("open upload failed");
    }
    return UniqueFd{fd};  // NRVO / 移动，见 2.3
}
```

这里的不变量可以表述为：

1. 若对象处于“持有”状态，则 `fd_ >= 0`，且该 fd 的关闭责任属于本对象；
2. 若对象处于“空 / moved-from”状态，则 `fd_ == -1`；
3. 同一时刻，至多一个 `UniqueFd` 实例对某个 fd 拥有关闭责任。

#### 原理：构造失败与异常安全

C++ 里若构造函数抛出异常，**析构函数不会被调用**（对象没构造成功）。因此：

- 构造过程中已经拿到的资源，必须在抛出前就地释放；或
- 用成员子对象的 RAII 分层获取——先构造成功的成员会在后续成员/构造体失败时按已构造逆序析构。

```cpp
int connect_fd(const char* host);  // 失败抛异常或返回 -1（此处假设抛）

struct Connection {
    UniqueFd sock_;
    // 假设 MetricsReporter 也是 RAII，构造时注册，析构时反注册
    // MetricsReporter metrics_;

    explicit Connection(const char* host)
        : sock_(connect_fd(host)) {
        // 若构造函数体随后抛异常，sock_ 会先被析构并 close
        if (!handshake(sock_.get())) {
            throw std::runtime_error("handshake failed");
        }
    }
};
```

对后台服务而言，“构造建立不变量”意味着：**业务函数拿到一个活着的对象时，可以相信资源已就绪；失败以异常或错误码在边界处理，而不是满世界检查半开连接。**

工厂函数比“先默认构造再 `open` 成员函数”更容易维护不变量：要么返回可用对象，要么失败（抛异常 / 返回 `std::optional` / 返回错误码），不存在“对象在但 fd 无效却被当成已连接”的灰态——除非你**显式**支持空状态（如默认构造的 `UniqueFd`），并让 `valid()` 可查。

#### 后台例子：加锁的临界区

```cpp
#include <mutex>
#include <string>
#include <unordered_map>

std::mutex g_token_mu;
std::unordered_map<std::string, std::string> g_access_token;

std::string get_token(const std::string& appid) {
    std::lock_guard<std::mutex> lock(g_token_mu);  // 构造 = lock
    auto it = g_access_token.find(appid);
    if (it == g_access_token.end()) {
        return {};  // 提前返回也解锁——析构 = unlock
    }
    if (it->second.empty()) {
        throw std::runtime_error("empty token");  // 异常同样解锁
    }
    return it->second;
}  // lock 析构，解锁
```

`lock_guard` 的不变量是“持有锁”；离开作用域（含异常）一定解锁。这与 `UniqueFd` 同构，只是资源从 fd 换成了互斥锁。公众号后台刷新 `access_token`、更新回调配置缓存时，这类写法是默认选项。

#### 对比：手动释放 vs RAII 的控制流

| 控制流 | 手动 `close` | `UniqueFd` |
|--------|--------------|------------|
| 正常走到末尾 | 要写一次 `close` | 析构自动 |
| 中间 `return` | 每个分支都要 `close` | 析构自动 |
| `continue` / `break` 出带资源的块 | 容易漏 | 析构自动 |
| `throw` | 要靠上层 `catch` 里关，或重复清理 | 栈展开自动 |
| 移动给调用方 | 靠约定“谁关闭” | 类型强制唯一所有者 |

### 2.2 析构释放和 noexcept

#### 直觉：离开作用域 = 语言保证会走的清理点

自动存储期对象在离开作用域时析构；异常导致栈展开时，**已经构造成功的自动对象也会析构**。RAII 正是把释放动作放进这个“语言保证会走”的路径——这比依赖程序员记得每一条 `return` 可靠得多。

```cpp
void handle_callback(const std::string& body) {
    UniqueFd fd{::open("/tmp", O_RDONLY | O_DIRECTORY | O_CLOEXEC)};
    if (!parse_ok(body)) {
        return;  // 析构 close
    }
    if (need_auth(body) && !authorized(body)) {
        throw std::runtime_error("unauthorized");  // 栈展开同样 close
    }
    write_audit_log(fd.get(), body);
    // 正常路径结束也 close
}
```

无论三条路中的哪一条，`fd` 都应被关闭——这就是节点 why 所说：**让异常与提前返回不会泄漏资源。**

结合第 19 课：栈展开按与构造相反的顺序销毁自动对象；成员按声明逆序析构。只要释放写在析构里，顺序就与对象图一致，而不是与你手写的 `goto cleanup` 标签一致。

#### 原理：析构为什么不宜抛异常

节点 pitfalls 明确：**析构函数不宜抛出异常。** C++ 工程实践与 Core Guidelines 都把它当作硬约束。

原因：

1. **栈展开中再抛异常会 `std::terminate`。** 若因异常正在展开栈，某个析构又抛出新异常，程序无法同时处理两个异常，直接终止。对后台服务来说，这等于一个本可降级的 `close` 失败把整个进程拉死。
2. **`close` / `fclose` / `unlock` 失败通常不可恢复或不宜以异常表达。** 对 fd 而言，失败多是 `EINTR`（可重试）或 `EBADF`（本就无效）。记录日志、打点或写入 `last_close_errno` 比抛异常更合适。
3. **标准库假定元素析构不抛。** `std::vector` 在重新分配、销毁范围时若元素析构抛异常，会破坏异常安全保证，甚至在已经处于异常路径时触发 `terminate`。
4. **C++11 起析构函数在多数情况下是 `noexcept` 的。** 若你在析构里抛出，会直接调用 `std::terminate`，连“上层还能接住”的机会都没有。

因此 `UniqueFd` 的关闭逻辑应写成：

```cpp
void UniqueFd::reset(int new_fd = -1) noexcept {
    if (fd_ >= 0) {
        int rc;
        do {
            rc = ::close(fd_);
        } while (rc < 0 && errno == EINTR);
        if (rc < 0) {
            last_close_errno_ = errno;  // 记录，不抛
        } else {
            last_close_errno_ = 0;
        }
        fd_ = -1;
    }
    fd_ = new_fd;
}

~UniqueFd() noexcept { reset(); }
```

实践目录里的用例故意先 `close` 底层 fd，再让 `UniqueFd::reset` 遇到 `EBADF`：断言**不抛异常**且 `last_close_errno() == EBADF`。这覆盖了节点 practice 要求的“关闭失败”分支。

#### 坑：异步资源生命周期

节点 pitfalls 后半句同样重要：**也不能自动保证异步资源生命周期。**

错误示范：

```cpp
void bad_async(ThreadPool& pool) {
    UniqueFd fd{accept_client()};
    pool.submit([&] {
        // 危险：fd 可能在任务运行前就析构关闭了
        char buf[64];
        ::read(fd.get(), buf, sizeof(buf));
    });
}  // UniqueFd 在这里析构——任务里变成 use-after-close
```

正确做法是**移动所有权进任务**：

```cpp
void good_async(ThreadPool& pool) {
    UniqueFd fd{accept_client()};
    pool.submit([fd = std::move(fd)]() mutable {
        // 任务对象持有 UniqueFd，任务结束（lambda 析构）才 close
        char buf[64];
        ::read(fd.get(), buf, sizeof(buf));
    });
}
```

局部 RAII 管的是**当前作用域**；跨异步边界必须显式转移所有权。事件循环里注册的 `on_readable` 回调同理：要么让会话对象活到回调注销，要么把 `UniqueFd` 放进会话并由会话统一析构。

#### 小实验：感受“析构必走”

```cpp
#include <iostream>
#include <stdexcept>

struct LoudClose {
    const char* name;
    ~LoudClose() { std::cout << "close " << name << "\n"; }
};

void demo(bool boom) {
    LoudClose a{"a"};
    if (boom) {
        throw std::runtime_error("boom");
    }
    LoudClose b{"b"};
}

int main() {
    try { demo(false); } catch (...) {}
    std::cout << "----\n";
    try { demo(true); } catch (...) {}
}
```

输出类似：

```text
close b
close a
----
close a
```

`boom==true` 时 `b` 尚未构造，只有 `a` 被析构——这与“只有构造成功的对象才会析构”一致，也解释了为什么构造中途拿到的裸资源必须立刻交给 RAII 成员。

### 2.3 唯一所有权与移动转移

#### 直觉：一把钥匙不能复制，只能转交

文件描述符、`FILE*`、很多 OS 句柄都是**唯一所有权**资源：两个所有者各自 `close` 一次，就会双重关闭（第二个常是 `EBADF`，或更糟——fd 号被复用后关错别人的连接，造成随机串话、半包、安全问题）。

因此 `UniqueFd` 必须：

- **禁止拷贝**（`= delete` 拷贝构造 / 拷贝赋值）；
- **允许移动**：把 fd 数值转给新对象，源对象置为 `-1`。

```cpp
UniqueFd a{::open("/dev/null", O_RDONLY | O_CLOEXEC)};
UniqueFd b = std::move(a);
assert(a.get() == -1);   // 源无效
assert(b.valid());       // 目标是唯一所有者
// a 析构：什么也不关
// b 析构：close 一次
```

这与 `std::unique_ptr` 同构：`unique_ptr` 管内存，`UniqueFd` 管描述符；`std::unique_lock` 管锁的所有权转移。学会一个，其余的可按同一骨架写。

#### 原理：移动构造与移动赋值的不变量

移动后必须维持：

| 对象 | 移动后状态 | 析构行为 |
|------|------------|----------|
| 源（moved-from） | `fd_ == -1`（或文档约定的空状态） | 不调用 `close` |
| 目标 | 持有原来的 fd | 负责 `close` |

移动赋值还要注意自移动与先释放旧资源：

```cpp
UniqueFd& operator=(UniqueFd&& other) noexcept {
    if (this != &other) {
        reset();           // 先释放自己旧的 fd
        fd_ = other.fd_;
        other.fd_ = -1;
    }
    return *this;
}
```

`release()` 是另一类转移：把裸 fd 交还给调用方，对象自身置空且**不** `close`——用于与只接受 `int` 的 C API 交互，但责任立刻回到人工约定，应尽量缩短裸 fd 暴露窗口。

验收标准第二句——**移动后源对象不重复关闭**——在实践里通过“移动后源为 `-1`，目标 `reset` 关闭后，再对原 raw fd 手动 `close` 得到 `EBADF`（证明只关过一次）”来验证。

#### 后台例子：把连接所有权交给会话对象

```cpp
struct ClientSession {
    UniqueFd conn_;
    explicit ClientSession(UniqueFd conn) noexcept : conn_(std::move(conn)) {}
};

ClientSession accept_session(int listen_fd) {
    int raw = ::accept4(listen_fd, nullptr, nullptr, SOCK_CLOEXEC);
    if (raw < 0) {
        throw std::runtime_error("accept failed");
    }
    return ClientSession{UniqueFd{raw}};  // 移动进会话；会话析构时关连接
}
```

再看“借出连接池连接”的 RAII guard（所有权仍在池中，guard 只负责归还）：

```cpp
class ConnectionPool;

class PooledConn {
public:
    PooledConn(ConnectionPool& pool, UniqueFd* slot) : pool_(&pool), slot_(slot) {}
    ~PooledConn() { if (pool_ && slot_) pool_->release(slot_); }
    PooledConn(const PooledConn&) = delete;
    PooledConn(PooledConn&& o) noexcept : pool_(o.pool_), slot_(o.slot_) {
        o.pool_ = nullptr;
        o.slot_ = nullptr;
    }
    int get() const { return slot_->get(); }
private:
    ConnectionPool* pool_;
    UniqueFd* slot_;
};
```

公众号后台的长连接、下游 RPC channel、与微信服务器的回调 HTTP 连接，都可以用同一模式：**边界上获得裸 fd → 立刻装进 RAII → 只通过移动转移，不拷贝；池化资源用 guard 归还而不是双重拥有。**

#### 和 `shared_ptr` 的边界

| 需求 | 更合适的工具 |
|------|----------------|
| 单一所有者、可转移 | `UniqueFd` / `unique_ptr` |
| 多处回调共享、最后一人释放 | `shared_ptr`（注意环） |
| 只读借用、寿命由调用方保证 | `const UniqueFd&` 或裸 `int` + 文档 |
| 锁 | `lock_guard`（不可移）/ `unique_lock`（可移） |

不要为了“图省事”把所有 fd 都做成 `shared_ptr`：共享所有权会让“谁该关、何时关”变模糊，泄漏与周期更难查。默认唯一所有权，共享是例外且要写清释放点。

## 3. 常见误区与设计权衡

### 3.1 误区（pitfalls）

节点 pitfalls：**析构函数不宜抛出异常，也不能自动保证异步资源生命周期。**

展开成可检查的清单：

1. **在析构里抛异常“报告 close 失败”。** 会导致栈展开中 `terminate`，或破坏容器保证。应记录 `errno` / 指标。
2. **以为 RAII 能管到别的线程。** 引用捕获局部 `UniqueFd` 丢进线程池，是典型 use-after-close。
3. **提供拷贝却浅拷贝 fd。** 两个对象 `close` 同一 fd，或一个关闭后另一个以为还有效；fd 复用后变成关错连接。
4. **移动后仍使用源对象的 get()。** moved-from 对象只保证“可析构、可再赋值”，不保证还能读写下层资源。
5. **构造失败却留下全局副作用。** 例如构造里 `open` 成功后又 `new` 失败抛异常，却没关 fd——应用成员 RAII 或 `unique_ptr` 分层。
6. **用 `int` 到处传并约定“调用方关闭”。** 约定靠人，人会忘；类型系统靠编译器。
7. **`dup` 后两个 fd 都塞进各自 `UniqueFd` 却误以为是“同一所有权的拷贝”。** `dup` 出的是新描述符，两个都要关——这是两个资源，不是一个资源的双所有者；与浅拷贝同一 fd 不同。
8. **在信号处理函数里做复杂 RAII。** 异步信号上下文有严格限制；不要把复杂锁与堆操作放进 handler。

### 3.2 权衡（tradeoffs）

节点 tradeoffs：**RAII提供局部确定性，跨线程回调仍需设计所有权。**

| 方案 | 优点 | 代价 |
|------|------|------|
| 局部 `UniqueFd` / `lock_guard` | 异常安全、提前返回不漏、代码短 | 只覆盖当前栈与作用域 |
| 移动进异步任务 / 会话对象 | 所有权清晰，生命周期跟随任务 | 要在类型里显式 `std::move` |
| `shared_ptr` 共享所有权 | 多处回调都能安全持有 | 开销、循环引用、所有权变模糊 |
| 裸 fd + 人工 `close` | 无封装成本 | 高泄漏率，难审代码 |
| 连接池 + RAII guard | 归还确定性好 | 要区分“池拥有”与“借用方不拥有” |

微信公众号后台常见结论：

- **请求内同步路径**：优先局部 RAII（fd、锁、临时缓冲）。
- **跨线程 / 跨事件循环**：移动唯一所有权，或明确 `shared_ptr` 的释放点；不要“引用捕获局部 RAII”。
- **连接池**：池子拥有连接；借出时用 RAII guard 保证归还（类似 `lock_guard` 对锁），而不是把池内连接的唯一所有权随便 `move` 走后丢失。
- **与脚本语言 `with` / `defer` 对比**：思想相同——绑定作用域与释放；C++ 用析构与类型系统把释放变成编译期结构，而不是运行时约定。

## 4. 动手实践

实践目录：`/workspace/atlas/lessons/practice20/`

### 4.1 要实现什么

按节点 practice：**实现禁止拷贝、可移动的 UniqueFd，覆盖关闭失败与移动后状态。**

最低要求：

1. `unique_fd.hpp`：`UniqueFd` 禁止拷贝、可移动；`reset`/`析构` 中 `close` 失败只记 `errno` 不抛；移动后源为 `-1`。
2. 用 `/proc/self/fd`（或等价手段）证明：提前 `return`、抛异常后打开 fd 数量不增长。
3. 证明移动后不双重关闭。
4. `test.sh` 用 `g++ -std=c++17 -Wall` 编译运行，最后打印 `ALL checks passed`。

### 4.2 文件一览

| 文件 | 作用 |
|------|------|
| `unique_fd.hpp` | `UniqueFd` 实现：禁止拷贝、可移动；close 失败记 errno |
| `fd_count.hpp` | `/proc/self/fd` 计数 |
| `demo_early_return.cpp` | 提前 return / 抛异常后 fd 数量不增长 |
| `demo_move.cpp` | 移动后源对象为 `-1`，不重复 close |
| `demo_close_fail.cpp` | close 失败（EBADF）只记录不抛 |
| `test.sh` | 一键验证 |
| `README.md` | 步骤说明 |

### 4.3 建议步骤

```bash
cd /workspace/atlas/lessons/practice20
# 阅读 unique_fd.hpp / demo_early_return.cpp / demo_move.cpp / demo_close_fail.cpp
./test.sh
```

自行实现时的顺序建议：

1. 先写默认构造、`explicit UniqueFd(int)`、`get`/`valid`、析构 `close`。
2. `= delete` 拷贝；实现移动构造与移动赋值（源置 `-1`）。
3. `reset` / `release`；`close` 遇 `EINTR` 重试，其它错误写入 `last_close_errno`。
4. 写测试：计数 fd → early return / try-catch → move → 人为先 `close` 再 `reset`。
5. 确认 `./test.sh` 输出 `ALL checks passed`。

### 4.4 关键实现要点（对照验收）

```cpp
// 提前返回 / 异常：作用域结束必析构
UniqueFd fd{open_something()};
if (bad) return;                  // fd 数应回到进入前
throw std::runtime_error("x");  // 栈展开同样关闭

// 移动
UniqueFd b = std::move(a);        // a.get()==-1，仅 b close

// close 失败
::close(fd.get());                // 人为关掉
fd.reset();                       // EBADF 被记录，绝不 throw
```

手动编译：

```bash
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O0 demo_early_return.cpp -o demo_early_return && ./demo_early_return
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O0 demo_move.cpp -o demo_move && ./demo_move
g++ -std=c++17 -Wall -Wextra -Wpedantic -Werror -g -O0 demo_close_fail.cpp -o demo_close_fail && ./demo_close_fail
```

## 5. 自测题（附答案）

### 题 1：为什么析构里抛异常很危险？

下面代码在“正常路径”和“已经在栈展开”时分别可能发生什么？

```cpp
struct NaughtyFd {
    int fd;
    ~NaughtyFd() {
        if (::close(fd) != 0) {
            throw std::runtime_error("close failed");
        }
    }
};

void f() {
    NaughtyFd a{open_devnull()};
    throw std::runtime_error("biz error");
}
```

**答案：**

- `f` 抛出 `biz error` 后开始栈展开，需要调用 `a` 的析构。
- 若此时 `close` 失败并再次抛出，C++ 无法同时传播两个异常，将调用 `std::terminate` 结束进程。
- 即便在正常路径，析构抛异常也会让调用方很难写 `try/catch`（对象生命周期结束点不在业务代码里），并破坏标准库容器对“析构不抛”的假定；C++11 后析构多为 `noexcept`，抛出即 `terminate`。
- 正确做法：析构/`reset` 内吞掉 `close` 错误并记录，保持不抛。这对应节点 pitfalls 前半句。

### 题 2：下列哪些写法满足“唯一所有权”？

```cpp
UniqueFd a{open_devnull()};

// A
UniqueFd b = a;

// B
UniqueFd c = std::move(a);

// C
void take(UniqueFd fd);
take(std::move(c));

// D
void borrow(const UniqueFd& fd);
borrow(c);  // 假设 c 仍有效
```

**答案：**

- A **不满足**，且正确实现中应无法编译（拷贝已删除）。若强行浅拷贝 fd，会双重关闭。
- B **满足**：移动后 `a` 无效，`c` 唯一持有。
- C **满足**：按值传参再移入形参，所有权交给 `take`；函数结束时形参析构关闭。
- D **不转移所有权**：调用期间借用，调用方仍负责生命周期。只要保证 `borrow` 不把引用或 `fd.get()` 存到调用之后，就是安全的只读借用；它本身不是“唯一所有权转移”，而是“借用”，需另用文档/类型约束寿命。若 `borrow` 启动异步任务并捕获引用，则踩中 pitfalls 后半句。

### 题 3：提前返回与异常后如何证明 fd 不泄漏？移动后如何证明不双重关闭？

用文字写出你在实践中的验证步骤，并说明为什么“`/proc/self/fd` 绝对值减少了几个”不重要。

**答案：**

1. 调用 `count_open_fds()`（读 `/proc/self/fd`）得到 `before`。
2. 在函数内构造 `UniqueFd`（内部 `open`），然后 `return` 或 `throw`。
3. 回到调用方后再测 `after`，断言 `after == before`。
4. `/proc/self/fd` 的计数包含 `.`、`..` 以及 `opendir` 自身占用的 fd，**绝对值**依赖实现与调用方式；验收关心的是**相对不变**——操作前后打开数量不增长，说明没有泄漏。
5. 移动验证：记录 raw → `b = std::move(a)` → 断言 `a.get()==-1` → `b.reset()` 正常关闭 → 再 `::close(raw)` 应得 `EBADF`，证明没有“没关”也没有“关了两次还以为有效”。人为先 `close(raw)` 再 `UniqueFd::reset`，应记录 `EBADF` 且不抛。
6. 对照节点 acceptance：提前返回和抛异常后 fd 数量不增长；移动后源对象不重复关闭。

## 6. 推荐资源

- **RAII与资源所有权：参考文档 / 原始资料**
  - 链接：<https://en.cppreference.com/w/cpp/language/raii.html>
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：基础
  - 说明：重点阅读构造建立不变量与析构释放和 noexcept，用于核对本节点实验的语义和边界。对照本课的 `UniqueFd`：构造是否建立“持有或为空”的不变量、析构是否无条件释放、失败是否不抛；再把例子映射到锁与临时文件。

- **C++ Core Guidelines · R.1 RAII**
  - 链接：<https://isocpp.github.io/CppCoreGuidelines/CppCoreGuidelines#Rr-raii>
  - 来源：ISO C++ Core Guidelines
  - 语言：英文
  - 类型：工程指南
  - 难度：进阶
  - 说明：对照资源管理规则，检查资源获取、异常退出和所有权转移。用 R.1 审查公众号后台里 socket、锁、临时文件是否都有明确所有者，以及跨线程回调是否仍错误依赖局部 RAII（节点 tradeoffs）。

## 7. 验收回扣

完成本课后，用下面这张清单做最终自检：

| 验收项 | 你应该能给出的证据 |
|--------|-------------------|
| 提前返回和抛异常后 fd 数量不增长 | 能运行 `demo_early_return` / `demo_move` / `demo_close_fail`，展示 `/proc/self/fd` 计数前后相等 |
| 移动后源对象不重复关闭 | 能说明移动后源为 `-1`，且只有目标对象 `close` 一次 |
| 能解释构造建立不变量 | 能说出“构造成功 ⇒ 资源就绪或明确为空”，并举 `UniqueFd` / `lock_guard` 例子 |
| 析构释放与 noexcept | 能说明栈展开中再抛会 `terminate`，并能指出 `close` 失败应记录不抛 |
| 唯一所有权与移动转移 | 能写出禁止拷贝、可移动的类型，并在会话 / 异步任务间 `std::move` |
| 理解 pitfalls / tradeoffs | 能指出异步回调不能靠局部 RAII，跨线程需设计所有权 |
| 能独立完成节点实践 | `./test.sh` 打印 `ALL checks passed` |

最后再回到节点 summary：

> RAII将资源获取和对象初始化关联，用确定的析构路径释放文件、锁和内存等资源。

以及节点 why：

> 让异常与提前返回不会泄漏资源。

只要你能用本课的 `UniqueFd` 实验把这两句话讲清楚，并能解释移动后为何不会双重关闭、析构为何不能抛异常、跨线程为何仍要设计所有权，本节点就算真正掌握了。
