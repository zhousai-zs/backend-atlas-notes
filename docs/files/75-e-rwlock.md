# 第 75/208 课：读写锁、快照与读密集优化

## 元信息

| 项目 | 内容 |
| --- | --- |
| 阶段 | 第 3 阶段 · 并发数据结构（读密集状态与版本发布） |
| 前置 | e-lock、e-order、b-smart |
| 难度 | 进阶 |
| 领域 | E |
| 标签 | Read Mostly Data、E、C++ |
| 子分类 | 并发数据结构 |
| 节点 ID | e-rwlock |
| 英文名 | Read Mostly Data |

> 承接说明：前置节点 e-lock（互斥锁与锁粒度）解释了互斥锁会把读者也串行化；前置节点 e-order（C++ 内存序与发布订阅）解释了如何用 release/acquire 发布指针让读者看到完整对象；前置节点 b-smart（智能指针与共享所有权）解释了 `shared_ptr` 如何让多个读者共同持有同一份不可变对象、最后一个离开时回收。本课把三者接起来：读密集数据既可以靠 `shared_mutex` 允许多读者并发，也可以靠 copy-on-write + `shared_ptr` 原子发布，让读者拿到完整快照且写失败不影响旧版本。

---

## 导读

先给“读密集数据”一个够用的定义：

> 读密集数据是指**读操作远多于写操作**、并且每个读操作都希望能尽快拿到一份完整、自洽状态的那类共享数据。

配置表、路由表、灰度开关、证书、模板消息开关、限流阈值，都属于这一类。它们的共同点是：每个请求都要读，运营或控制面偶尔才写；读的时候不能看到“改到一半”的状态；写失败的时候线上必须继续用旧状态。

放到微信公众号后台，这些场景非常具体：

1. **每个请求都要查 appid / 路由表 / 灰度开关**。接入层收到一条请求，先根据 appid 找到对应的逻辑集群，再查这个集群是否命中了某个灰度开关，最后决定走新逻辑还是旧逻辑。一条请求可能读三到五次配置，写配置可能几分钟才发生一次。读和写的比例可能是几万比一。
2. **运营热更新模板消息开关或路由配置**。运营在后台点一下“关闭某类模板消息”，控制面会生成一份新的配置。这时正在处理的请求不能看到“一半新、一半旧”的表：不能出现 appid 已经指向新集群、但灰度开关还是旧值的情况，也不能出现 map 正在扩容、读者读到半截桶数组的情况。
3. **写入构建新配置失败时，线上必须继续用旧的完整配置**。新配置可能来自一次错误的发布、一个格式不合法的 JSON、一次校验不通过的灰度规则。构建失败就是构建失败，不能把旧配置改坏，也不能让读者拿到一个“改了一半”的对象。
4. **若用一把互斥锁，读请求会互相排队**。读操作本身不修改数据，却因为要拿同一把独占锁而被迫串行。核数越多，这种浪费越明显：读者明明可以并行，却被锁排成一队。
5. **若用读写锁，写者可能被持续到来的读者饿死**。读者不断进入临界区，写者一直排在队尾；配置热更新“看起来调用了”，但新配置迟迟不生效。更麻烦的是，C++ 标准并不规定读写锁的调度公平性，换一个标准库、换一个平台，行为可能完全不同。

本课要建立三个核心认识：

1. **`shared_mutex` 解决的是“读者之间可以并行”**，但它不解决“写者一定能在有限时间内拿到锁”。公平性是实现相关的，不能把业务正确性建立在“写者优先”这种假设上。
2. **copy-on-write 快照解决的是“读者永远看完整版本”**。写者复制一份、改副本、再整体替换指针；读者拿到的是一个不可变的 `shared_ptr<const T>`，读路径不需要锁。代价是每次写都要复制整个配置。
3. **版本发布与回收是快照方案的另一半**。发布必须用正确的原子操作建立 happens-before，让读者看到新指针时一定看到对象里的全部字段；回收靠 `shared_ptr` 的引用计数，读者持有旧快照期间旧版本不会被释放，历史版本数量必须可控。

---

## 学习目标

### goals

- 能解释 `shared_mutex` 与饥饿风险。
- 能独立完成：实现路由配置不可变快照，比较共享锁与 `shared_ptr` 发布的性能。

### acceptance

- 读者只能观察完整版本。
- 写入失败不影响旧快照。

把这两条验收翻译成可检查的产物：

- **“读者只能观察完整版本”**：在任意一次读取里，读者看到的配置必须来自同一个版本。快速验证可以用一组“必须同时出现”的 key（本课用 `canary_a` / `canary_b`），或者给整份配置加一个版本戳，检查读到的所有字段是否属于同一版本。压力测试中，每次 `audit()` 的结果都不能出现“一个 canary 是旧值、另一个是新值”。
- **“写入失败不影响旧快照”**：构造一个会失败的更新（校验失败、复制失败、序列化失败都行），失败后旧配置的版本号、内容、可见性都不变；读者在失败更新前后读到的仍然是同一份完整的旧配置。

这两条不是“测试通过就行”的口号，而是接口契约：`lookup` 返回的是哪一份版本、`update` 失败时旧版本是否保留、读者持有旧快照期间写者发布新版本会发生什么，都应该写进接口注释，并在测试里用可观察的行为钉死。

---

## 为什么后端要懂

本节点给出的 why 是：**安全实现配置热更新及路由表读取。** “安全”这两个字至少包含四层含义，每一层在线上都能变成一次真实事故。

### 1. 读者不能看到“半成品”

配置更新通常不是“改一个字段”这么简单，而是“改一组互相依赖的字段”。比如路由表里 appid 到集群的映射、集群的权重、灰度开关三者必须一起生效。如果写者直接原地修改 `unordered_map`，读者在修改过程中读到的可能是：

- 旧 appid 还在，但权重已经改成新值；
- map 正在 rehash，读者读到一部分旧桶、一部分新桶；
- 新 key 已经插入，但对应的开关还没写进去。

这类问题用 `shared_mutex` 把读写都锁住可以避免；用 copy-on-write 快照也可以避免，而且读路径更轻。关键是你要明确：**读者看到的最小一致单位是什么**。

### 2. 写失败不能污染线上

配置构建经常失败：JSON 解析失败、字段校验失败、引用了不存在的集群、权限检查失败。失败的更新必须“什么都没发生”：

- 旧配置的版本号不变；
- 旧配置的内容不变；
- 正在读旧配置的请求不受影响；
- 后续更新仍然可以基于旧配置继续尝试。

如果实现是“先把旧 map 清空、再往里填新值”，那么中途抛异常就会留下一张残缺的表。正确做法只有两种：**在副本上构建、成功后整体替换**，或者**在锁内修改但保证回滚**。本课实践统一采用前者，因为它同时适用于共享锁方案和快照方案。

### 3. 读路径的成本会被放大

一条请求读三次配置，QPS 十万，就是三十万次读。读路径上多一次锁竞争、多一次拷贝、多一次原子操作，都会被放大成 CPU 和延迟。反过来说，读路径优化也必须建立在正确性之上：

- 共享锁方案：读者拿 `shared_lock`，多个读者可以并行，但锁本身仍有原子操作和竞争成本；
- 快照方案：读者只做一次 `shared_ptr` 的原子 load，然后读写普通内存，读路径几乎就是一次指针读取加引用计数；
- 代价转到写路径：快照方案每次写都要复制整份配置，配置很大时写延迟和瞬时内存会上升。

### 4. 写者的进度必须有保证

如果读者源源不断，写者一直拿不到锁，配置就永远不生效。更隐蔽的情况是：写者“调用成功”了，但新配置只对后来的一部分读者可见，或者写者在一个队列里排队很久，运维以为已经发布完成。读写锁的公平性不是标准保证的，**不能把热更新的时效性建立在“实现应该会照顾写者”的假设上**。快照方案把写路径缩短为“复制 + 原子替换”，读者不会阻塞写者，写者的进度更容易预期；但写者仍然要承担复制成本。

一句话：**懂读密集数据，你才知道什么时候该用共享锁、什么时候该复制快照；不懂它，你可能会用一把大锁把读请求排成队，或者用“原地改 map”把线上配置改成半成品。**

---

## 直觉 → 原理

先把本课会反复出现的几个词钉住：

| 术语 | 本课口径 | 关键问题 |
| --- | --- | --- |
| 共享锁 | 多个读者可以同时持有的锁 | 读者之间是否真的并行 |
| 独占锁 | 同一时刻只能有一个持有者的锁 | 写者何时能拿到锁 |
| 饥饿 | 某类线程长期拿不到锁 | 公平性由谁保证 |
| copy-on-write | 写时复制：改副本，不改已发布对象 | 复制的成本落在谁身上 |
| 快照 | 某一时刻完整、不可变的一份状态 | 读者如何获得并持有它 |
| 发布 | 把新对象的指针以同步方式交给读者 | 对象内容何时对读者可见 |
| 回收 | 没有任何读者持有时释放旧版本 | 旧版本会保留多久 |

---

### 1. `shared_mutex` 与饥饿风险

#### 1.1 读密集的直觉

先看一个非常常见的错误写法：用一把普通 `std::mutex` 保护路由表。

```cpp
std::mutex mu;
std::unordered_map<std::string, std::string> routes;

std::string lookup(const std::string& appid) {
    std::lock_guard<std::mutex> lock(mu);
    auto it = routes.find(appid);
    return it == routes.end() ? std::string("<miss>") : it->second;
}

void update(const std::string& appid, const std::string& backend) {
    std::lock_guard<std::mutex> lock(mu);
    routes[appid] = backend;
}
```

这当然是对的，也没有数据竞争。但它的代价是：**所有读者都被同一把锁串行化**。读操作不修改共享数据，读者之间本来没有冲突，却要排队进入临界区。对于“每个请求查好几次、写几分钟才一次”的读密集场景，这是纯粹的浪费。

`std::shared_mutex`（C++17，头文件 `<shared_mutex>`）提供了两级访问：

- **共享所有权**：读者用 `std::shared_lock<std::shared_mutex>`，多个 `shared_lock` 可以同时持有；
- **独占所有权**：写者用 `std::unique_lock<std::shared_mutex>` 或 `std::lock_guard<std::shared_mutex>`，它与任何共享锁、独占锁都互斥。

规则可以概括成一句话：**共享锁与共享锁相容，共享锁与独占锁互斥，独占锁与独占锁互斥。**

#### 1.2 一个完整可编译的例子

下面这段程序让 4 个读者并发查表，1 个写者偶尔更新：

```cpp
// shared_mutex_demo.cpp
#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

class RouteTable {
public:
    std::string lookup(const std::string& appid) const {
        // 多个读者可以同时持有 shared_lock
        std::shared_lock<std::shared_mutex> lock(mu_);
        auto it = routes_.find(appid);
        return it == routes_.end() ? std::string("<miss>") : it->second;
    }

    void update(const std::string& appid, const std::string& backend) {
        // 写者独占，和所有读者、其他写者互斥
        std::unique_lock<std::shared_mutex> lock(mu_);
        routes_[appid] = backend;
    }

private:
    mutable std::shared_mutex mu_;
    std::unordered_map<std::string, std::string> routes_{
        {"wx_app_1", "logic-a"},
        {"wx_app_2", "logic-b"},
        {"wx_app_3", "logic-c"},
    };
};

int main() {
    RouteTable table;
    std::atomic<bool> stop{false};
    std::vector<std::thread> readers;

    for (int t = 0; t < 4; ++t) {
        readers.emplace_back([&, t] {
            long hits = 0;
            while (!stop.load(std::memory_order_relaxed)) {
                const std::string key =
                    "wx_app_" + std::to_string(t % 3 + 1);
                if (table.lookup(key) != "<miss>") {
                    ++hits;
                }
            }
            std::printf("reader %d hits=%ld\n", t, hits);
        });
    }

    for (int i = 0; i < 100; ++i) {
        table.update("wx_app_1", "logic-a-" + std::to_string(i));
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }

    stop.store(true, std::memory_order_relaxed);
    for (auto& th : readers) {
        th.join();
    }
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -pthread -O2 -o shared_mutex_demo shared_mutex_demo.cpp
./shared_mutex_demo
```

注意：`lookup` 是 `const` 成员函数，但 `mu_` 需要被锁住（`shared_lock` 会修改锁的内部状态），所以 `mu_` 声明为 `mutable`。这是并发接口里很常见的写法。

#### 1.3 饥饿：标准没有承诺公平

`shared_mutex` 只承诺互斥语义，**不承诺调度公平性**。C++ 标准没有规定它是读者优先、写者优先还是先进先出；`pthread_rwlock_t` 也一样，具体行为取决于实现、平台、甚至负载。这意味着：

- 如果实现偏袒读者，读者不断到来时，写者可能一直排在队尾，拿不到独占锁——这就是**写者饥饿**。配置热更新会表现为“代码调用了 `update`，但新配置迟迟不生效”。
- 如果实现偏袒写者，写者频繁到来时，读者会被堵住，读延迟上升。
- 如果实现是某种 FIFO，看起来更公平，但标准同样不保证。

下面这段程序故意让读者频繁进入、并在共享锁内停留一小段时间（真实代码不应在锁内 sleep / 做 IO），然后用带超时的 `try_lock_for` 观察写者是否能及时拿到锁：

```cpp
// rwlock_starvation.cpp
#include <atomic>
#include <chrono>
#include <cstdio>
#include <shared_mutex>
#include <thread>
#include <vector>

std::shared_timed_mutex g_mu;
int g_table = 0;
std::atomic<bool> g_stop{false};

void reader() {
    while (!g_stop.load(std::memory_order_relaxed)) {
        std::shared_lock<std::shared_timed_mutex> lock(g_mu);
        (void)g_table;
        // 故意放大“读者持续进入”的窗口；真实代码不要在锁内 sleep/IO
        std::this_thread::sleep_for(std::chrono::microseconds(200));
    }
}

int main() {
    std::vector<std::thread> readers;
    for (int i = 0; i < 8; ++i) {
        readers.emplace_back(reader);
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    const auto t0 = std::chrono::steady_clock::now();
    std::unique_lock<std::shared_timed_mutex> lock(g_mu, std::defer_lock);
    const bool got = lock.try_lock_for(std::chrono::milliseconds(500));
    const auto t1 = std::chrono::steady_clock::now();
    const auto waited_us =
        std::chrono::duration_cast<std::chrono::microseconds>(t1 - t0).count();

    if (got) {
        g_table = 1;  // 模拟发布新配置
        lock.unlock();
    }

    g_stop.store(true, std::memory_order_relaxed);
    for (auto& th : readers) {
        th.join();
    }

    std::printf("writer got lock within 500ms: %s, waited %lld us\n",
                got ? "yes" : "no",
                static_cast<long long>(waited_us));
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -pthread -O2 -o rwlock_starvation rwlock_starvation.cpp
./rwlock_starvation
```

这段程序不承诺输出某个固定结果。在偏袒读者的实现上，写者可能等到超时；在偏袒写者或使用公平策略的实现上，写者可能很快拿到锁。**你应该在自己的平台和标准库上跑，记录结果，而不是把某次运行的耗时当成普遍规律。**

实践中有三个直接结论：

1. **不要在持有 `shared_lock` 时做耗时 IO、RPC、日志落盘**。锁的持有时间越长，写者等待越久，饥饿越容易被放大。
2. **读写锁内部通常比普通 `mutex` 重**。它要维护读者计数、写者状态和等待队列；如果读临界区只是查一个很小的表、持锁时间极短，`shared_mutex` 未必比 `mutex` 快，甚至可能更慢。
3. **不要把“写者最终会拿到锁”当作业务保证**。如果热更新有时效要求，需要额外的监控、看门狗或改用快照发布。

---

### 2. copy-on-write 快照

#### 2.1 直觉：写者改副本，读者看旧版

共享锁的思路是“让读者和写者互斥，但读者之间并行”。copy-on-write 的思路更进一步：**根本不让读者和写者碰同一份可变对象**。

具体做法是：

1. 当前配置用 `std::shared_ptr<const RouteConfig>` 持有；
2. 配置对象一旦发布，就**不再修改**（逻辑上不可变）；
3. 写者先读到当前指针，复制构造一份新配置，在副本上修改；
4. 修改和校验都成功后，把新指针原子地替换上去；
5. 任何一步失败，旧指针原封不动，读者继续读旧版本。

读者拿到的永远是一个完整的 `const` 对象：它不会看到 map 正在 rehash，也不会看到“一半新一半旧”。写者也不需要在读者的读路径上加锁：读者只需要做一次原子 load。

这里的关键约定是：**发布之后不再修改对象**。如果你把新配置发布出去以后又去改它，读者就会在无锁读路径上看到数据竞争，甚至看到撕裂状态。

#### 2.2 完整可编译的例子

```cpp
// cow_snapshot.cpp
#include <atomic>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

struct RouteConfig {
    std::unordered_map<std::string, std::string> routes;
    std::uint64_t version = 0;
};

using ConfigPtr = std::shared_ptr<const RouteConfig>;

// 当前版本。所有读写都通过原子操作访问它。
ConfigPtr g_current = std::make_shared<RouteConfig>();

ConfigPtr load() {
    return std::atomic_load_explicit(&g_current, std::memory_order_acquire);
}

void publish(ConfigPtr next) {
    std::atomic_store_explicit(&g_current, std::move(next),
                               std::memory_order_release);
}

void reload(bool make_it_fail) {
    ConfigPtr old = load();                           // 1. 读旧快照
    auto next = std::make_shared<RouteConfig>(*old);  // 2. 复制整份配置

    try {
        if (make_it_fail) {
            throw std::runtime_error("route validation failed");
        }
        next->routes["wx_app_1"] = "logic-a-new";     // 3. 只改副本
        next->version = old->version + 1;
        publish(ConfigPtr(std::move(next)));          // 4. 整体发布
    } catch (const std::exception& e) {
        // 失败：旧指针没有被替换，读者继续看到旧版本
        std::printf("reload failed: %s (old snapshot kept)\n", e.what());
    }
}

std::string lookup(const std::string& key) {
    ConfigPtr cfg = load();  // 读者钉住一份完整快照
    auto it = cfg->routes.find(key);
    return it == cfg->routes.end() ? std::string("<miss>") : it->second;
}

int main() {
    {
        auto init = std::make_shared<RouteConfig>();
        init->routes = {
            {"wx_app_1", "logic-a"},
            {"wx_app_2", "logic-b"},
        };
        init->version = 1;
        publish(ConfigPtr(std::move(init)));
    }

    std::atomic<bool> stop{false};

    std::thread reader([&] {
        while (!stop.load(std::memory_order_relaxed)) {
            const std::string value = lookup("wx_app_1");
            if (value == "<miss>") {
                std::printf("reader observed a partial snapshot!\n");
            }
        }
    });

    reload(false);  // 成功：发布 version=2
    std::printf("after success: %s (version=%llu)\n",
                lookup("wx_app_1").c_str(),
                static_cast<unsigned long long>(load()->version));

    reload(true);   // 失败：旧快照保留
    std::printf("after failure: %s (version=%llu)\n",
                lookup("wx_app_1").c_str(),
                static_cast<unsigned long long>(load()->version));

    stop.store(true, std::memory_order_relaxed);
    reader.join();
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -pthread -O2 -o cow_snapshot cow_snapshot.cpp
./cow_snapshot
```

预期行为（逻辑上的，不是性能数字）：成功更新后 `wx_app_1` 变成 `logic-a-new`，版本变成 2；失败更新后仍然是 `logic-a-new`、版本仍然是 2。读者在整个过程中只会看到 `<miss>` 之外的两个完整版本之一。

#### 2.3 和共享锁方案对比

| 维度 | `shared_mutex` 方案 | copy-on-write 快照方案 |
| --- | --- | --- |
| 读者之间 | 可以并行，但每次都要拿锁 | 完全并行，只做一次原子 load |
| 读者与写者 | 互斥：写者等所有读者退出 | 基本不互斥：写者改副本，读者读旧版 |
| 读者看到的中间状态 | 不会，因为写者独占 | 不会，因为已发布对象不可变 |
| 写失败 | 需要回滚或先构建副本再交换 | 副本直接丢弃，旧指针不变 |
| 写成本 | 只改变化的字段 | 每次复制整份配置 |
| 内存 | 一份配置 | 当前版本 + 仍被读者持有的旧版本 |
| 热更新时效 | 取决于锁调度，可能被读者饿死 | 写者只受复制和发布成本影响 |
| 适用场景 | 配置较小、更新不频繁、实现公平性可接受 | 读极多、写较少、配置可复制、要求读路径极轻 |

一句话总结：**共享锁把复杂度放在“锁怎么调度”，快照把复杂度放在“复制和版本回收”。**

---

### 3. 版本发布与回收

#### 3.1 发布：先写完对象，再发布指针

快照方案能成立，前提是读者看到新指针时，一定能看到对象里的全部字段。这正好是 e-order 里讲的发布/订阅模式：

- 写者先在副本上完成所有字段的写入；
- 然后用 **release** 语义发布新指针；
- 读者用 **acquire** 语义读取指针；
- 一旦读者读到了新指针，写者在 release 之前的全部写入都对读者可见。

C++17 里对 `shared_ptr` 的原子操作有现成的自由函数，声明在 `<memory>`（配合 `<atomic>` 使用）：

```cpp
std::shared_ptr<const RouteConfig> load() {
    return std::atomic_load_explicit(&g_current, std::memory_order_acquire);
}

void publish(std::shared_ptr<const RouteConfig> next) {
    std::atomic_store_explicit(&g_current, std::move(next),
                               std::memory_order_release);
}
```

也可以使用默认的 `std::atomic_load` / `std::atomic_store`，它们的默认内存序是 `seq_cst`，同样能建立所需的可见性，只是约束更强。C++20 起还可以使用 `std::atomic<std::shared_ptr<const RouteConfig>>`，把原子性直接做进类型里。

需要特别注意：**不要**把 `shared_ptr` 当普通变量，一边用普通赋值写、一边用普通读取读。`shared_ptr` 的引用计数本身是原子的，但指针和引用计数的组合操作不是；普通读写并发是数据竞争，必须用上面的原子函数或 `atomic<shared_ptr>`。

#### 3.2 回收：最后一个读者离开时释放

快照的回收非常自然：

- 旧版本不需要手动 `delete`；
- 当前指针被替换后，如果没有读者还持有旧版本，旧对象的引用计数归零，自动析构；
- 正在读旧版本的请求持有一个 `shared_ptr`，它会把旧版本“钉住”，读完释放后旧版本才可能被回收。

这就是“版本回收”的全部机制。它带来的边界是：**历史版本的数量等于“当前指针 + 仍被读者持有的旧版本”**。如果读者只是短暂地持有快照，旧版本很快回收；如果读者把 `shared_ptr` 存进缓存、队列、异步任务，长时间不释放，旧版本就会一直占着内存。

如果业务额外保存了一份版本列表（比如为了回滚保留最近 N 个版本），那必须**设置上限**，否则内存会随着热更新次数线性增长。纯 `shared_ptr` 发布不需要版本列表，但前提是读者不长期握着指针。

下面这段程序演示“读者钉住旧版本”：

```cpp
// version_reclaim.cpp
#include <atomic>
#include <cstdio>
#include <memory>
#include <string>

struct Config {
    int version = 0;
    std::string payload;
};

using Ptr = std::shared_ptr<const Config>;

Ptr g_current = std::make_shared<Config>(Config{1, "old"});
std::weak_ptr<const Config> g_old_weak;

Ptr load() {
    return std::atomic_load_explicit(&g_current, std::memory_order_acquire);
}

void publish(Ptr next) {
    std::atomic_store_explicit(&g_current, std::move(next),
                               std::memory_order_release);
}

int main() {
    Ptr pinned = load();  // 读者持有一份旧快照
    g_old_weak = pinned;

    publish(std::make_shared<Config>(Config{2, "new"}));

    std::printf("pinned reader still sees version=%d payload=%s\n",
                pinned->version, pinned->payload.c_str());
    std::printf("old version alive while pinned: %s\n",
                g_old_weak.expired() ? "no" : "yes");

    pinned.reset();  // 读者读完了，释放旧快照

    std::printf("old version alive after release: %s\n",
                g_old_weak.expired() ? "no" : "yes");
    Ptr now = load();
    std::printf("current version=%d payload=%s\n",
                now->version, now->payload.c_str());
    return 0;
}
```

编译运行：

```bash
g++ -std=c++17 -pthread -O2 -o version_reclaim version_reclaim.cpp
./version_reclaim
```

这段程序的输出是确定的：钉住期间旧版本不会被回收；`pinned.reset()` 后旧版本才可能析构；当前版本始终是新的。

#### 3.3 观察内存

想验证“旧版本是否被读者长期钉住”，可以观察进程的 RSS。Linux 上可以这样做（把 `$PID` 换成实际进程号）：

```bash
# 每秒打印一次 RSS（单位 KB）
watch -n 1 'ps -o rss= -p $PID'

# 或者直接读 /proc
grep VmRSS /proc/$PID/status
```

不要期待某个固定数字：RSS 取决于配置大小、分配器行为、读者持有时间和更新频率。正确的观察方式是**对比**：在稳定的更新频率下，如果 RSS 持续单调上升且不回落，就要怀疑有读者长期持有旧快照，或者存在额外的版本列表没有限制长度。

---

## 常见误区与设计权衡

### 4.1 误区：假设读写锁“一定公平”

本课节点的 pitfalls 说得很清楚：**读写锁不保证所有实现都公平。** 具体来说：

- 不要假设 `shared_mutex` 一定写者优先、读者优先或 FIFO；
- 不要假设 `pthread_rwlock_t` 一定和 `std::shared_mutex` 行为相同；
- 不要用一次压测的结果去推断另一个平台的行为；
- 不要把“热更新应该很快生效”建立在“写者应该能拿到锁”的假设上。

写者饥饿的线上表现往往不是报错，而是“发布系统显示成功，但配置没有生效”。如果业务对生效时间有要求，应该监控发布到生效的延迟，或者直接改用快照发布，让写者不必和读者争同一把锁。

### 4.2 误区：持有 `shared_lock` 时再去拿别的锁

```cpp
std::shared_lock<std::shared_mutex> lock1(router_mu);
std::lock_guard<std::mutex> lock2(audit_mu);  // 锁顺序不当可能死锁
```

两个线程如果以相反顺序获取两把锁，就可能互相等待。规则是：**所有线程用一致的锁顺序**，或者干脆不要在持锁期间再去获取其他锁。快照方案的一个附带好处是读路径不持锁，天然少了一类死锁风险。

### 4.3 误区：把 `shared_ptr` 当普通变量并发读写

```cpp
// 错误：普通读写并发，数据竞争
std::shared_ptr<const Config> current;
current = std::make_shared<Config>(...);  // 写线程
auto cfg = current;                        // 读线程
```

`shared_ptr` 的引用计数是原子的，但“读取指针 + 增加引用计数”这一组操作不是原子的。正确做法是 `std::atomic_load` / `std::atomic_store`，或者 `std::atomic<std::shared_ptr<T>>`。

### 4.4 误区：发布后继续修改对象

```cpp
auto next = std::make_shared<Config>(*old);
publish(next);              // 已经发布
next->version = 2;          // 错误：读者可能正在读
next->routes["k"] = "v";    // 错误：破坏不可变约定
```

快照方案的“不可变”不是建议，而是正确性前提。发布之后只能通过“复制一份新的、改副本、再发布”来更新。

### 4.5 误区：在锁外做“检查再操作”

```cpp
if (router.size() < limit) {       // 某一瞬间的快照
    router.update(key, value);     // 中间可能已被别人改过
}
```

`size()` / `empty()` 返回的是某一瞬间的状态，检查和操作之间存在竞态。应该把判断放进同一个临界区，或者让 `update` 自己返回成功/失败。

### 4.6 误区：以为锁能保护“读完之后继续使用的引用”

```cpp
const std::string* p;
{
    std::shared_lock<std::shared_mutex> lock(mu);
    p = &table.at(key);   // 指向 map 内部
}
use(*p);                  // 锁已释放，写者可能已经 rehash/删除
```

锁只保护临界区内的访问。锁释放后，指针/引用可能悬空。这正是快照方案要解决的问题：读者持有 `shared_ptr`，把对象的生命周期绑在自己身上，读多久都不会被写者回收。

### 4.7 权衡：复制成本 vs 读路径成本

本课节点的 tradeoffs 是：**快照读简洁但复制成本高，需限制历史版本保留。**

- 读路径：快照明显更轻，一次原子 load 加一次引用计数递增；共享锁每次都要锁操作。
- 写路径：快照要复制整份配置。配置有几十 MB、每秒更新多次时，复制会拉高写延迟和瞬时内存。
- 内存：当前版本 + 读者持有的旧版本；如果额外保留历史版本列表，必须设上限。
- 适用性：配置小、更新不频繁、读极多，快照往往合适；配置巨大、更新非常频繁，可能需要分片、增量更新或专门的配置服务。

没有一种方案是“永远更快”的。共享锁和快照的对比必须带着具体负载去跑，而不是凭直觉下结论。

---

## 动手实践

实践目标是：**实现路由配置不可变快照，比较共享锁与 `shared_ptr` 发布的性能。**

我们写一个程序，包含两个实现、同一套功能测试和同一套性能测试：

- `SharedLockRouter`：`std::shared_mutex` + `unordered_map`。`lookup` 用 `shared_lock`，`update` 先在共享锁下复制一份表，在副本上修改，再在 `unique_lock` 下替换。
- `SnapshotRouter`：`std::atomic_load/store` 发布 `shared_ptr<const RouteConfig>`。`update` 复制、修改副本、成功才发布。

两个实现都遵守同一套接口约定：

- `lookup(key, out)`：成功返回 `true` 并写出值；失败返回 `false`。
- `update(key, value, simulate_failure)`：成功返回 `true`；失败返回 `false`，且旧版本不变。
- `audit()`：返回一份完整的 `RouteConfig` 快照，用于检查“读者只能观察完整版本”。

一致性检查用一组“必须同时出现”的 key：`canary_a` 和 `canary_b`。每次成功更新都会把两个 canary 一起改成新版本号，读者检查两个 canary 是否相等、是否等于配置的版本号。如果实现让读者看到了“一个 canary 新、一个 canary 旧”，测试就会记录一次不一致。

### 5.1 完整代码

```cpp
// router_bench.cpp
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <mutex>
#include <shared_mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

using Table = std::unordered_map<std::string, std::string>;

struct RouteConfig {
    Table routes;              // 发布后不再修改
    std::uint64_t version = 0; // 整份配置的版本戳
};

using ConfigPtr = std::shared_ptr<const RouteConfig>;

static const char* kCanaryA = "canary_a";
static const char* kCanaryB = "canary_b";

// ---------------------------------------------------------------------------
// 方案一：shared_mutex
//
// lookup 返回当前表里的值，读期间持共享锁。
// update 先在共享锁下复制当前表，在副本上修改；校验失败直接返回 false，
// 旧表不变。成功后用 unique_lock 替换表，替换是一个整体操作。
// 读者持有共享锁期间，写者会等待；写者替换期间，新读者会等待。
// ---------------------------------------------------------------------------
class SharedLockRouter {
public:
    SharedLockRouter() {
        for (int i = 0; i < 8; ++i) {
            table_["route:" + std::to_string(i)] = "v1";
        }
        table_[kCanaryA] = "v1";
        table_[kCanaryB] = "v1";
        version_ = 1;
    }

    bool lookup(const std::string& key, std::string& out) const {
        std::shared_lock<std::shared_mutex> lock(mu_);
        auto it = table_.find(key);
        if (it == table_.end()) {
            return false;
        }
        out = it->second;
        return true;
    }

    ConfigPtr audit() const {
        std::shared_lock<std::shared_mutex> lock(mu_);
        auto cfg = std::make_shared<RouteConfig>();
        cfg->routes = table_;
        cfg->version = version_;
        return cfg;
    }

    bool update(const std::string& key, const std::string& value,
                bool simulate_failure) {
        Table next;
        std::uint64_t base_version = 0;
        {
            std::shared_lock<std::shared_mutex> lock(mu_);
            next = table_;
            base_version = version_;
        }

        // 复制完成后模拟校验失败：副本丢弃，旧表不变
        if (simulate_failure) {
            return false;
        }

        const std::uint64_t next_version = base_version + 1;
        next[key] = value;
        const std::string canary = "v" + std::to_string(next_version);
        next[kCanaryA] = canary;
        next[kCanaryB] = canary;

        {
            std::unique_lock<std::shared_mutex> lock(mu_);
            // 单写者场景下不会触发；多写者时避免覆盖别人刚发布的版本
            if (version_ != base_version) {
                return false;
            }
            table_.swap(next);
            version_ = next_version;
        }
        return true;
    }

private:
    mutable std::shared_mutex mu_;
    Table table_;
    std::uint64_t version_ = 0;
};

// ---------------------------------------------------------------------------
// 方案二：copy-on-write + shared_ptr 原子发布
//
// lookup 只做一次 atomic_load，拿到一份不可变快照，读路径不持锁。
// update 复制旧快照，在副本上修改；失败直接返回 false，旧指针不变。
// 成功后 atomic_store 发布新指针。读者持有旧快照期间，旧版本不会被回收；
// 写者发布新版本不会阻塞读者，读者也不会阻塞写者。
// ---------------------------------------------------------------------------
class SnapshotRouter {
public:
    SnapshotRouter() {
        auto cfg = std::make_shared<RouteConfig>();
        for (int i = 0; i < 8; ++i) {
            cfg->routes["route:" + std::to_string(i)] = "v1";
        }
        cfg->routes[kCanaryA] = "v1";
        cfg->routes[kCanaryB] = "v1";
        cfg->version = 1;
        std::atomic_store_explicit(&current_, ConfigPtr(std::move(cfg)),
                                   std::memory_order_release);
    }

    bool lookup(const std::string& key, std::string& out) const {
        ConfigPtr cfg = load();
        auto it = cfg->routes.find(key);
        if (it == cfg->routes.end()) {
            return false;
        }
        out = it->second;
        return true;
    }

    ConfigPtr audit() const { return load(); }

    bool update(const std::string& key, const std::string& value,
                bool simulate_failure) {
        ConfigPtr old = load();
        auto next = std::make_shared<RouteConfig>(*old);  // 复制整份配置

        if (simulate_failure) {
            return false;  // 副本丢弃，旧快照不变
        }

        const std::uint64_t next_version = old->version + 1;
        next->routes[key] = value;
        const std::string canary = "v" + std::to_string(next_version);
        next->routes[kCanaryA] = canary;
        next->routes[kCanaryB] = canary;
        next->version = next_version;

        std::atomic_store_explicit(&current_, ConfigPtr(std::move(next)),
                                   std::memory_order_release);
        return true;
    }

private:
    ConfigPtr load() const {
        return std::atomic_load_explicit(&current_, std::memory_order_acquire);
    }

    mutable ConfigPtr current_;
};

// ---------------------------------------------------------------------------
// 功能测试：并发读者 + 成功更新 + 失败更新
//
// 读到的 audit() 是一份完整配置。检查两个 canary 是否相等且等于版本号。
// 更新失败后，版本号和 route:0 的值都必须保持不变。
// ---------------------------------------------------------------------------
template <typename Router>
void run_functional_test(const char* name) {
    Router router;

    std::atomic<bool> stop{false};
    std::atomic<int> ready{0};
    std::atomic<std::uint64_t> observations{0};
    std::atomic<std::uint64_t> inconsistent{0};

    std::vector<std::thread> readers;
    for (int t = 0; t < 4; ++t) {
        readers.emplace_back([&] {
            ready.fetch_add(1, std::memory_order_release);
            while (!stop.load(std::memory_order_relaxed)) {
                ConfigPtr cfg = router.audit();
                observations.fetch_add(1, std::memory_order_relaxed);

                auto a = cfg->routes.find(kCanaryA);
                auto b = cfg->routes.find(kCanaryB);
                const std::string expected =
                    "v" + std::to_string(cfg->version);

                if (a == cfg->routes.end() || b == cfg->routes.end() ||
                    a->second != b->second || a->second != expected) {
                    inconsistent.fetch_add(1, std::memory_order_relaxed);
                }
            }
        });
    }

    // 等 4 个读者都进入循环，避免更新在读者启动前就跑完
    while (ready.load(std::memory_order_acquire) < 4) {
        std::this_thread::yield();
    }

    for (int i = 0; i < 50; ++i) {
        router.update("route:0", "gen-" + std::to_string(i), false);
    }

    stop.store(true, std::memory_order_relaxed);
    for (auto& th : readers) {
        th.join();
    }

    ConfigPtr before = router.audit();
    const bool ok = router.update("route:0", "must-not-appear", true);
    ConfigPtr after = router.audit();

    auto it = after->routes.find("route:0");
    const bool old_preserved =
        (after->version == before->version) &&
        (it != after->routes.end()) &&
        (it->second == "gen-49");

    std::printf(
        "[%s] observations=%llu inconsistent=%llu version=%llu "
        "failed_update_returned_false=%s old_snapshot_preserved=%s\n",
        name,
        static_cast<unsigned long long>(observations.load()),
        static_cast<unsigned long long>(inconsistent.load()),
        static_cast<unsigned long long>(after->version),
        ok ? "no" : "yes",
        old_preserved ? "yes" : "no");
}

// ---------------------------------------------------------------------------
// 性能测试：8 个读者做固定次数 lookup，1 个写者并发更新
//
// 两个实现跑同一套负载。这里不打印任何预设的性能结论，只打印实际测到的
// 耗时和吞吐，供你在自己的机器上对比。
// ---------------------------------------------------------------------------
template <typename Router>
void run_benchmark(const char* name) {
    Router router;

    constexpr int kReaders = 8;
    constexpr std::uint64_t kLookupsPerReader = 500'000;
    constexpr int kUpdates = 100;

    const std::vector<std::string> keys = {
        "route:0", "route:1", "route:2", "route:3",
        "route:4", "route:5", "route:6", "route:7"};

    std::atomic<bool> start{false};
    std::atomic<std::uint64_t> hits{0};

    std::vector<std::thread> readers;
    for (int t = 0; t < kReaders; ++t) {
        readers.emplace_back([&] {
            while (!start.load(std::memory_order_acquire)) {
                std::this_thread::yield();
            }

            std::uint64_t local_hits = 0;
            std::string out;
            for (std::uint64_t i = 0; i < kLookupsPerReader; ++i) {
                if (router.lookup(keys[i % keys.size()], out)) {
                    ++local_hits;
                }
            }
            hits.fetch_add(local_hits, std::memory_order_relaxed);
        });
    }

    std::thread writer([&] {
        while (!start.load(std::memory_order_acquire)) {
            std::this_thread::yield();
        }
        for (int i = 0; i < kUpdates; ++i) {
            router.update("route:0", "gen-" + std::to_string(i), false);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    });

    const auto t0 = std::chrono::steady_clock::now();
    start.store(true, std::memory_order_release);

    for (auto& th : readers) {
        th.join();
    }
    const auto t1 = std::chrono::steady_clock::now();
    writer.join();

    const double elapsed_ms =
        std::chrono::duration<double, std::milli>(t1 - t0).count();
    const std::uint64_t total_lookups = kReaders * kLookupsPerReader;
    const double mlookup_per_sec =
        (static_cast<double>(total_lookups) / 1e6) /
        (elapsed_ms / 1000.0);

    std::printf(
        "[%s] readers=%d lookups=%llu hits=%llu elapsed=%.2f ms "
        "throughput=%.2f Mlookup/s\n",
        name,
        kReaders,
        static_cast<unsigned long long>(total_lookups),
        static_cast<unsigned long long>(hits.load()),
        elapsed_ms,
        mlookup_per_sec);
}

int main() {
    std::printf("=== functional test ===\n");
    run_functional_test<SharedLockRouter>("SharedLockRouter");
    run_functional_test<SnapshotRouter>("SnapshotRouter");

    std::printf("=== benchmark ===\n");
    run_benchmark<SharedLockRouter>("SharedLockRouter");
    run_benchmark<SnapshotRouter>("SnapshotRouter");
    return 0;
}
```

### 5.2 编译与运行

普通编译，性能对比用这一条：

```bash
g++ -std=c++17 -pthread -O2 -o router_bench router_bench.cpp && ./router_bench
```

如果要查数据竞争，用 ThreadSanitizer 单独编译一份：

```bash
g++ -std=c++17 -pthread -O1 -g -fsanitize=thread -o router_bench_tsan router_bench.cpp
./router_bench_tsan
```

**性能对比不要和 TSAN 混在一起。** ThreadSanitizer 会插桩、改变时序并显著拖慢程序，用它跑出来的耗时没有性能参考价值；它只用来发现数据竞争。正确顺序是：先用普通 `-O2` 版本看功能和大致吞吐，再用 TSAN 版本验证没有数据竞争。

### 5.3 测试覆盖说明

功能测试对应两条验收：

1. **“读者只能观察完整版本”**：4 个读者不停调用 `audit()`，每次拿到一份完整配置，检查 `canary_a == canary_b == "v" + version`。只要出现一次不一致，`inconsistent` 就会大于 0。两个实现都应该输出 `inconsistent=0`。
2. **“写入失败不影响旧快照”**：先做 50 次成功更新，最后一次 `route:0` 的值是 `gen-49`，版本是 51。然后调用一次 `simulate_failure=true` 的更新，检查：
   - `update` 返回 `false`；
   - 版本号仍然是 51；
   - `route:0` 仍然是 `gen-49`；
   - 读者仍然能读到完整的旧配置。

性能测试只提供同一套负载下的对比，不提供预设结论。你可能会观察到快照方案在读路径上更轻，也可能在配置很小、读者临界区极短时看到共享锁并不慢；实际结果取决于机器、标准库实现、核数、更新频率和配置大小。请记录你的运行环境，再解释差异。

### 5.4 接口注释里要写清楚的四件事

1. **`lookup` 返回的是哪个版本**：共享锁方案返回“持锁期间当前表”的某个值，读者无法跨多次 `lookup` 保证同一版本；快照方案返回“load 那一瞬间”的完整版本。若业务需要跨多个 key 的一致性，应使用一次 `audit()`/整表快照，而不是连续多次 `lookup`。
2. **`update` 失败时旧版本是否保留**：两个实现都保证保留。共享锁方案失败时副本丢弃，旧表未动；快照方案失败时旧指针未替换。
3. **读者持有快照期间写者发布新版本会发生什么**：共享锁方案下，读者持锁时写者等待；快照方案下，写者照常发布新版本，读者继续读旧版本，旧版本在读者释放 `shared_ptr` 后才可能回收。
4. **写者是否需要串行化**：两个实现都按“单写者”演示。`SharedLockRouter` 用版本号检查避免多写者互相覆盖，冲突时后到者返回 `false`；`SnapshotRouter` 的 `load -> 复制 -> store` 不是 CAS，多写者并发时后发布的会覆盖先发布的，因此写者必须由上层串行化，或改用比较交换（CAS）循环。接口注释必须写清这个前提。

### 5.5 自检清单

- [ ] 两个实现都能通过功能测试，`inconsistent=0`，`old_snapshot_preserved=yes`
- [ ] 故意把 `SnapshotRouter::update` 改成“先发布、再修改对象”，功能测试能发现问题
- [ ] 故意把 `SharedLockRouter::update` 改成“在 `unique_lock` 下原地逐 key 修改”，说明它为什么仍然可能满足单次 `lookup` 的完整性，但会拉长写者阻塞时间
- [ ] 用普通 `-O2` 和 TSAN 两种方式编译，记录 TSAN 无报告
- [ ] 记录编译器、标准库、平台、核数和运行日期，不把一次结果当普遍结论

---

## sourceStudy：对照文档核对实现

本节点的 sourceStudy 要求：**沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。**

推荐资源是 cppreference 的 `std::shared_mutex` 页面。建议按下面的清单逐项读，不要只扫一眼类声明：

### 6.1 定义与适用版本

- [ ] 确认 `std::shared_mutex` 定义在 `<shared_mutex>`，**C++17 起**可用；C++14 对应的是 `std::shared_timed_mutex`。
- [ ] 用自己的话写出它的两级所有权：多个线程可以同时持有共享所有权；独占所有权同一时刻只能有一个线程持有。
- [ ] 核对页面上的互斥规则：某线程持有独占锁时，其他线程不能获得共享锁或独占锁；有线程持有共享锁时，其他线程不能获得独占锁。把这些规则和本课第 1 节的例子逐条对上。
- [ ] 记录：本课代码使用了 `std::shared_mutex`、`std::shared_lock`、`std::unique_lock`，它们分别来自哪个头文件。

### 6.2 成员函数与锁类型

- [ ] 列出 `shared_mutex` 的成员函数：`lock`、`try_lock`、`unlock`、`lock_shared`、`try_lock_shared`、`unlock_shared`。
- [ ] 说明 `shared_lock` 是“共享所有权”的 RAII 包装，`unique_lock` / `lock_guard` 是“独占所有权”的 RAII 包装；确认 `shared_lock` 不能用于独占加锁。
- [ ] 如果页面提到 `native_handle` / `native_handle_type`，记录它返回的是底层实现句柄，属于平台相关接口，不应在可移植代码里依赖它的具体类型或语义。
- [ ] 核对 `try_lock` 与 `try_lock_shared` 的返回值：成功为 `true`，失败为 `false`；失败时没有持有锁，不能调用对应的 `unlock`。
- [ ] 注意：`shared_mutex` 不提供带超时的 `try_lock_for`；本课的饥饿演示用的是 `shared_timed_mutex`。不要把两者的接口混用。

### 6.3 公平性与调度

- [ ] 在页面上查找与调度、优先级、公平性有关的表述。如果页面没有规定读者优先/写者优先/FIFO，就明确记录：**标准未规定读者/写者优先级，行为实现相关。**
- [ ] 不要因为某次运行中写者很快拿到锁，就推断标准保证写者优先；也不要把“读者优先”写进接口契约。
- [ ] 记录你实际使用的标准库实现和版本（例如 libstdc++、libc++、MSVC STL），以及运行平台。

### 6.4 和快照方案的对照

- [ ] `std::atomic_load` / `std::atomic_store` 对 `shared_ptr` 的重载，适用版本在 `<memory>` / `shared_ptr` 的原子操作相关页面，不在 `shared_mutex` 页面；阅读时注意区分页面。
- [ ] 确认本课使用的是 `std::atomic_load_explicit` / `std::atomic_store_explicit`（C++11 起可用）；C++20 起也可以使用 `std::atomic<std::shared_ptr<T>>`。
- [ ] 核对内存序：发布用 release，读取用 acquire；如果使用默认的 `atomic_load` / `atomic_store`，默认是 `seq_cst`，同样满足可见性要求。
- [ ] 记录边界：`shared_ptr` 的引用计数是原子的，但普通赋值/读取不是原子的；必须使用原子函数。

### 6.5 记录模板

建议在实验记录里留下这样一段（示例字段，内容按实际填写）：

```text
实现：<库名与版本>
平台：<OS / CPU / 核数>
编译器：<编译器与版本>
编译参数：-std=c++17 -pthread -O2 / -fsanitize=thread
验证日期：<日期>
shared_mutex 公平性：记录：标准未规定读者/写者优先级，实现相关
本课代码用到的接口：shared_mutex, shared_lock, unique_lock,
                     atomic_load_explicit, atomic_store_explicit
未验证项：<例如某平台上的写者饥饿表现、配置大小对复制成本的影响>
```

记录“未验证项”很重要：它把“我读过的”和“我实际验证过的”分开，避免把文档语义和某次实验现象混为一谈。

---

## 自测题

### 题 1：读写锁的公平性与饥饿

某服务用 `std::shared_mutex` 保护路由表：读者请求非常频繁，写者每隔几分钟更新一次配置。上线后发现运维点了“发布”，但新配置很久没有生效。

请回答：

1. 为什么可能出现“写者拿不到锁”？标准对 `shared_mutex` 的公平性做了什么保证？
2. 如果换成 `pthread_rwlock_t`，能否假定一定写者优先？为什么？
3. 在共享锁方案下，有哪些手段可以降低写者饥饿的风险？
4. 如果业务要求“配置必须在数秒内生效”，共享锁方案还缺少什么保障？你会怎么改？

**答案：**

1. 读者持续到来并持有共享锁时，写者的独占锁请求可能一直排在后面；标准只规定互斥语义，不规定读者/写者优先级或调度公平性，因此写者饥饿是允许的实现行为。不能把“写者最终会拿到锁”当成标准保证。
2. 不能。`pthread_rwlock_t` 的调度策略同样是实现相关的，不同平台、不同版本可能不同；可移植代码不能依赖某种公平性。
3. 缩短读者临界区（不在共享锁内做 IO/RPC/日志）、降低读者持锁时间、限制同时进入的读者数量、给写者留出窗口（例如读者侧做批量读取）、监控发布到生效的延迟；必要时改用快照发布，让写者不和读者争同一把锁。
4. 共享锁方案无法保证写者在限定时间内获得锁，因此无法保证生效时限。可以改用 copy-on-write 快照：写者复制、修改、原子发布，读者不阻塞写者；同时监控发布延迟和旧版本回收情况。

### 题 2：完整版本可见性

下面是一段“快照”更新代码：

```cpp
std::shared_ptr<RouteConfig> current = std::make_shared<RouteConfig>();

void update(const std::string& key, const std::string& value) {
    auto next = current;          // 读当前指针
    next->routes[key] = value;    // 直接改“副本”
    current = next;               // 发布
}

std::string lookup(const std::string& key) {
    auto cfg = current;           // 普通读取
    auto it = cfg->routes.find(key);
    return it == cfg->routes.end() ? "" : it->second;
}
```

请回答：

1. `auto next = current;` 之后，`next` 和 `current` 指向的是同一份对象吗？直接改 `next->routes` 会有什么后果？
2. `current = next;` 和 `lookup` 里的 `auto cfg = current;` 并发时，有什么问题？
3. 应该怎么改，才能做到“读者只能观察完整版本”和“写入失败不影响旧快照”？
4. 如果更新过程中校验失败并抛异常，旧版本会不会被破坏？按你的修改说明。

**答案：**

1. 是同一份对象。`shared_ptr` 的拷贝只增加引用计数，不复制对象。直接改 `next->routes` 改的就是当前读者正在读的对象，违反“发布后不可变”的约定，读者可能看到写到一半的 map。
2. `shared_ptr` 的普通赋值/读取不是原子的；“读指针 + 增加引用计数”和“写指针 + 减少引用计数”并发是数据竞争，可能导致引用计数错乱、悬空指针或崩溃。
3. 正确做法：用 `std::atomic_load_explicit` / `std::atomic_store_explicit`（或 `std::atomic<std::shared_ptr<T>>`）访问当前指针；`update` 里用 `std::make_shared<RouteConfig>(*old)` 复制一份新对象，在副本上修改，全部成功后再用 release 语义发布；`lookup` 用 acquire 语义 load，拿到 `shared_ptr<const RouteConfig>` 后只读。
4. 按修改后的实现，校验失败时新副本直接丢弃，旧指针从未被替换，旧版本不会被破坏；正在读旧版本的读者也不受影响。注意：如果先发布再校验，就无法保证这一点。

### 题 3：复制成本与版本回收

某服务的路由配置大约有 50 MB，读请求非常多，配置每 10 秒热更新一次。团队决定采用 copy-on-write 快照方案。

请回答：

1. 每次更新的直接成本是什么？对写延迟和瞬时内存有什么影响？
2. 旧版本什么时候被回收？哪些做法会让旧版本长期不被回收？
3. 如果为了回滚额外保存最近 100 个版本，会有什么问题？应该怎么限制？
4. 在“配置很大、更新较频繁”的情况下，快照方案还合适吗？可以采取哪些折中？

**答案：**

1. 每次更新都要复制整份配置（本例约 50 MB），然后在副本上修改。复制会拉高写延迟，并在短时间内同时存在旧版本和新版本，瞬时内存至少增加一份配置的大小；如果复制期间还有读者持有更旧的版本，内存会更高。
2. 旧版本在当前指针被替换后，如果没有读者仍持有它，引用计数归零即析构回收。让旧版本长期不回收的常见做法：读者把 `shared_ptr` 存进缓存/队列/异步任务长时间不释放；额外保存版本列表且不设上限；循环引用导致引用计数无法归零。
3. 会保留大量历史快照，内存随更新次数增长；100 个 50 MB 版本就是很大的常驻内存。应设置版本数量上限或保留时间上限，优先保留元数据/差异而不是完整副本，并明确回滚窗口。
4. 仍然可以，但需要折中：缩小配置、按 appid/租户分片、使用增量更新或专门配置服务、降低更新频率、用双缓冲/对象池减少分配开销。是否合适要用实际配置大小、更新频率和读负载去测量，不能只看“读多写少”就下结论。

---

## 推荐资源

- **读写锁、快照与读密集优化：参考文档 / 原始资料**
  - 链接：https://en.cppreference.com/w/cpp/thread/shared_mutex.html
  - 来源：en.cppreference.com
  - 语言：英文
  - 类型：文档
  - 难度：进阶
  - 是否免费：是
  - 推荐理由：重点阅读 shared_mutex 与饥饿风险与 copy-on-write 快照，用于核对本节点实验的语义和边界。阅读时按本课第 6 节的清单核对：`shared_mutex` 的 C++17 适用版本、`lock`/`lock_shared`/`try_lock`/`try_lock_shared`/`unlock`/`unlock_shared` 的语义、`shared_lock` 与 `unique_lock` 的配套用法，以及标准是否规定调度公平性。记录结论时以页面实际表述为准，不把“某次运行中写者很快拿到锁”当成写者优先的证据。`shared_ptr` 的原子发布接口在 `<memory>` / `shared_ptr` 相关页面，不在本页面，读的时候注意区分。

---

## 本课小结

1. **读密集数据的关键矛盾是“读者要并行、写者要生效、读者不能看到半成品”。** 普通 `mutex` 把读者串行化；`shared_mutex` 让读者并行；copy-on-write 快照让读者与写者几乎不互斥。
2. **`shared_mutex` 只保证互斥语义，不保证调度公平。** 读者持续到来时写者可能饥饿；实现可能偏袒读者、偏袒写者或采用其他策略。可移植代码不能依赖公平性假设。
3. **共享锁适合读多写少、临界区短、配置较小的场景。** 读者用 `shared_lock`，写者用 `unique_lock`；读者之间并行，但写者仍要等待所有读者退出。不要在持锁期间做耗时 IO。
4. **快照方案用“不可变对象 + 原子发布指针”消除读路径锁。** 写者复制、改副本、成功后整体发布；失败时旧指针不变。读者拿到 `shared_ptr<const T>` 后，整段读路径只读这份不可变对象。
5. **发布必须建立 happens-before。** 写者先写完对象，再以 release 语义发布指针；读者以 acquire 语义读取。C++17 可用 `atomic_load`/`atomic_store` 的 `shared_ptr` 重载；C++20 起可用 `std::atomic<std::shared_ptr<T>>`。不要用普通赋值/读取并发访问 `shared_ptr`。
6. **回收靠引用计数，旧版本的数量等于“当前版本 + 仍被读者持有的旧版本”。** 读者持有旧快照期间旧版本不会释放；把 `shared_ptr` 长期存进缓存/队列会阻止回收；额外保存版本列表必须设上限。
7. **两条验收要能测试。** “读者只能观察完整版本”用 canary key 或版本戳验证；“写入失败不影响旧快照”用一次故意失败的更新验证版本号和内容都不变。
8. **共享锁与快照的对比要带负载跑。** 快照读路径更轻，但每次写复制整份配置；配置很大或更新频繁时，复制会拉高写延迟和瞬时内存。不要编造性能数字，记录环境、负载和实际测量值。
9. **锁不能保护“读完之后继续使用的引用”。** 锁释放后指针/引用可能悬空；快照的 `shared_ptr` 把对象生命周期绑在读者身上，这是它相对共享锁方案的重要优势。
10. **本课的工作台是：先定一致性单位，再选共享锁或快照，然后用“成功更新 + 失败更新 + 并发读者”三类测试钉死契约，最后再谈性能。**

下一课会继续在并发数据结构里处理更细的同步与生命周期问题；本课留下的检查顺序是：**读者看到的最小完整单位是什么 → 写失败要保证什么 → 选择锁方案还是快照方案 → 发布与回收是否闭合 → 用测试和文档把契约固定下来。**
