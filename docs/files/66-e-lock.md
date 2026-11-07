# 第 66/208 课：互斥锁与锁粒度

- **阶段**：第 2 阶段 · 并发与内存模型（同步与正确性）
- **前置课程**：e-race、b-raii
- **关键词**：互斥锁、mutex、lock_guard、unique_lock、临界区、锁顺序、粗粒度锁、分片锁、锁粒度、吞吐、p99
- **难度**：基础
- **所属领域**：E · 并发与内存模型（子类：同步与正确性）
- **标签**：Mutexes、E、C++

---

## 导读

上一课讲的是**数据竞争与线程安全契约**：把「偶发错误」变成可检查的共享状态规则——先问哪些状态被多个线程共享，再问这些访问之间有没有 happens-before，最后才问用什么同步原语。配套工具是 C++ memory model、`std::mutex`、`std::atomic` 和 ThreadSanitizer。

本课进入最常用的同步工具——**互斥锁（mutex）**。节点把这件事总结得很精确：

> 互斥锁把临界区串行化并建立同步关系，正确性取决于所有相关访问遵守同一契约。

上一课让你能**识别**「这块内存有竞争」；本课让你能**正确地用锁把它护住**，并且在护住之后还问一个后端天天要答的问题：**锁该粗还是该细？**

微信公众号后台里，这类共享结构到处都有：

- **会话表**：多个工作线程同时查、插、删同一张会话 map；
- **本地 KV 缓存**：热点文章元数据、配置片段、鉴权结果缓存；
- **限流桶**：按 appid / openid 计数，先查后扣必须在同一临界区；
- **连接状态表**：IO 线程改状态，业务线程读缓冲与标志；
- **统计聚合**：多个线程往同一张指标表里累加。

它们的共同点是：**必须有一把（或一组）锁把相关访问串起来**；但锁太粗会把吞吐和 p99 拖垮，锁太细又容易死锁、难维护。本课把三件事串起来：

1. **怎么加锁**：`std::lock_guard` / `std::unique_lock`（RAII，作用域释放，后者可延迟、转移、配合条件变量）；
2. **怎么划临界区与锁顺序**：临界区尽量短；同一不变量用同一把锁；多锁时全局固定顺序；
3. **怎么选粒度**：一把大锁易验证但吞吐差；按 key hash 分片提高并行度，同时增加死锁与维护成本。

本课与 e-race（数据竞争、happens-before、逻辑原子性）和 b-raii（资源获取即初始化、作用域释放）直接相连：**互斥锁的正确用法几乎就是「用 RAII 包住临界区」**；而锁粒度选择是在正确性之上做吞吐与可维护性的权衡。

---

## 学习目标

完成本课学习后，你应当能够：

1. **能解释 lock_guard 和 unique_lock**
   - 能说明为什么要用 RAII 加锁，而不是手写 `lock()` / `unlock()`。
   - 能说清 `std::lock_guard` 的语义：构造时加锁，析构时解锁，不可移动、不可手动解锁。
   - 能说清 `std::unique_lock` 相对 `lock_guard` 多出的能力：延迟加锁（`std::defer_lock`）、手动 `lock`/`unlock`、所有权转移、与条件变量配合。
   - 能判断「这段代码该用 `lock_guard` 还是 `unique_lock`」。

2. **能解释临界区与锁顺序**
   - 能把「临界区」定义成：为维护某个不变量，必须互斥执行的那段代码。
   - 能说明「所有访问同一不变量的路径，必须遵守同一把锁的约定」。
   - 能解释多锁死锁的经典形态（AB-BA），并给出「全局固定锁顺序」的解法。
   - 能指出「临界区里做 IO / 调回调 / 拿另一把锁」带来的风险。

3. **能解释粗粒度和分片锁**
   - 能对比「一把大锁护整张表」与「按 key hash 分片多把锁」的正确性与性能差异。
   - 能说明分片锁如何提高并行度，以及它如何引入跨分片操作的死锁与一致性问题。
   - 能用吞吐与 p99 证据选择锁粒度，而不是凭感觉说「细锁一定更快」。

4. **能独立完成实践**
   - 为共享 KV 实现互斥保护（先粗锁，再分片锁）。
   - 压测不同分片锁数量，记录吞吐与 p99。
   - 沿 cppreference 核对 `mutex` / `lock_guard` / `unique_lock` 的定义、示例与错误处理路径，记录适用版本及边界条件。

5. **满足本课验收标准（acceptance）**
   - **所有共享访问遵守锁约定**：读、写、遍历、删除都走同一套锁契约，没有「锁写不锁读」。
   - **锁粒度选择有吞吐与 p99 证据**：至少对比 1 / N / M 种分片数，给出数据和结论。

---

## 为什么后端要懂

节点给的 why 是：

> 保护复合数据结构及不变量。

这句话落到公众号后台，就是下面几件事。

### 1. 复合结构不是「几个字段各自加锁」就能对

假设会话表这样写：

```cpp
struct Session {
    std::string openid;
    int64_t last_active_ms;
    bool closed;
};

class SessionTable {
public:
    void upsert(const std::string& id, int64_t now) {
        // 危险：无锁
        auto& s = map_[id];
        s.openid = id;
        s.last_active_ms = now;
        s.closed = false;
    }
    bool alive(const std::string& id, int64_t now, int64_t ttl) const {
        auto it = map_.find(id);
        if (it == map_.end()) return false;
        return !it->second.closed && (now - it->second.last_active_ms) < ttl;
    }
private:
    std::unordered_map<std::string, Session> map_;
};
```

单线程完全正确。多线程下：

- 线程 A 正在 `rehash`（插入触发扩容），线程 B 正在 `find` → 数据竞争，可能崩溃；
- 线程 A 写 `closed=true`，线程 B 读 `last_active_ms` 做 TTL 判断 → 可能看到半更新状态；
- 「先查后改」的复合操作被拆开，逻辑不变量（「closed 与 last_active 一致表达会话存活」）被破坏。

互斥锁要保护的不是「某一个字段」，而是**整张表作为复合结构的不变量**。

### 2. 锁建立的是同步关系，不只是「别同时进」

`mutex::unlock` 与后续另一个线程的 `mutex::lock` 之间建立 happens-before。这意味着：

- 临界区内的写，对之后拿到同一把锁的线程可见；
- 你不只是「串行化了」，还「同步了内存视图」。

上一课讲的 happens-before，在本课里最常见的载体就是 mutex。不会用锁，就等于不会建立这条最常用的同步边。

### 3. 锁粒度直接决定吞吐与 p99

公众号后台高峰期，本地缓存可能是每秒数十万次读。如果整张 KV 一把大锁：

- 所有读互相排队；
- 偶发一次写会让整条读路径抖动；
- p99 被锁等待尾巴拖长。

如果按 key 分片成 64 / 256 把锁：

- 不同 key 的访问可以并行；
- 但「遍历全表」「跨 key 事务」「动态调整分片数」都变难；
- 锁顺序一旦写错，死锁概率上升。

后端工程师必须能**用数据**选粒度，而不是「听说细锁快就全细锁」。

### 4. 线上事故常见形态：锁写不锁读、临界区过长、锁顺序反了

值班时你常会看到：

- 「只在 update 时加锁，get 觉得是只读就没加」→ 仍然是数据竞争；
- 「锁里打日志、锁里 RPC、锁里调用户回调」→ 持锁时间爆炸，吞吐塌掉，甚至反向死锁；
- 「线程 1 先锁 A 再锁 B，线程 2 先锁 B 再锁 A」→ 偶发卡死，重启后「好了」。

这三件事本课都会给出可检查的规则。

### 5. 公众号后台你会真正碰到的场景

- 本地文章元数据缓存：读多写少，粗锁可能够用，热点写要用分片或读写锁（读写锁留后续课）；
- 限流桶：按 appid 分片，单 appid 内必须串行「查令牌→扣令牌」；
- 连接注册表：插入/删除要锁，回调最好锁外做；
- 配置热更新：常常是「写端换 shared_ptr，读端无锁读快照」——那是另一类方案；若仍用 mutex，就要保证所有读路径也拿同一把锁。

---

## 直觉 → 原理

### 0. 先建立直觉：洗手间门锁、公共白板、分楼层钥匙

用三个生活类比把本课三块内容钉住：

1. **洗手间门锁 = mutex**  
   同一时间只能一个人进去（临界区串行化）。你进门上锁、出门开锁；如果进门后忘了开锁，后面所有人排队等到天荒地老。RAII 就是「用门把人带进去，人走了门自动开」——对应 `lock_guard`。

2. **公共白板 = 被锁保护的共享结构**  
   白板上写着会话表、KV、限流桶。规则是：**谁要看或改白板，都必须先拿到同一把钥匙**。只改不看也加锁、只看不改也加锁——因为「看的时候别人正在擦」，你看到的就不是一个完整画面。

3. **一栋楼一把总钥匙 vs 每层一把钥匙 = 粗锁 vs 分片锁**  
   总钥匙好管：谁进楼都清楚，不会拿错层。但所有人进任何一个房间都要抢同一把钥匙，高峰期门口大堵车（吞吐差、p99 差）。每层一把钥匙：不同层可以并行，但你要「同时开 3 层门做巡检」就必须规定开锁顺序，否则两个人反向开门会卡死在楼梯间（死锁）。

记住这三句，后面的 API 和代码都是在落实它们。

### 1. lock_guard 和 unique_lock

#### 1.1 为什么不要手写 lock / unlock

```cpp
std::mutex mu;
void bad_incr() {
    mu.lock();
    ++count_;          // 若这里抛异常，unlock 永远不会执行
    mu.unlock();
}
```

问题：

- 异常路径漏解锁 → 整进程永久卡住；
- 早退 `return` 漏解锁 → 同上；
- 代码审查时很难一眼看出「每一条路径都解锁了」。

b-raii 课已经强调过：**资源获取即初始化**。锁就是一种资源。

#### 1.2 std::lock_guard：最常用的默认选择

```cpp
#include <mutex>

std::mutex mu;
int count_ = 0;

void incr() {
    std::lock_guard<std::mutex> lk(mu); // 构造时 lock
    ++count_;
} // 析构时 unlock，无论正常返回还是异常
```

要点：

- 构造函数调用 `mu.lock()`，析构函数调用 `mu.unlock()`；
- 不可拷贝、不可移动（C++17 起有 `scoped_lock` 可锁多把，语义类似「只管理作用域」）；
- **不能**中途手动解锁；
- 临界区 = `lk` 这个对象的生命周期。

编译与最小验证：

```bash
# 文件：lock_guard_demo.cpp
c++ -std=c++17 -O2 -pthread lock_guard_demo.cpp -o lock_guard_demo
./lock_guard_demo
```

#### 1.3 std::unique_lock：需要灵活性时再用

```cpp
#include <mutex>
#include <thread>
#include <condition_variable>

std::mutex mu;
std::condition_variable cv;
bool ready = false;

void wait_ready() {
    std::unique_lock<std::mutex> lk(mu); // 默认构造即加锁
    cv.wait(lk, [] { return ready; });   // wait 内部会解锁再加锁
    // 此处仍持锁
}

void signal_ready() {
    {
        std::lock_guard<std::mutex> lk(mu);
        ready = true;
    } // 先解锁再 notify，避免无效唤醒后立刻抢锁失败的抖动（风格问题）
    cv.notify_one();
}
```

`unique_lock` 相对 `lock_guard` 多出的能力：

| 能力 | 用法 | 何时需要 |
|------|------|----------|
| 延迟加锁 | `std::unique_lock<std::mutex> lk(mu, std::defer_lock);` 然后 `std::lock(lk1, lk2);` | 多锁避免死锁 |
| 手动解锁 | `lk.unlock(); /* 非临界区工作 */ lk.lock();` | 缩短持锁时间 |
| 所有权转移 | `auto lk2 = std::move(lk);` | 把锁的持有权交给别的作用域/对象 |
| 配合条件变量 | `cv.wait(lk, pred);` | 等待时必须能临时释放锁 |

经验法则：

- **默认用 `lock_guard`（或 C++17 `scoped_lock`）**；
- 只有当你需要「中途解锁」「defer_lock + std::lock」「condition_variable」时，才上 `unique_lock`；
- 不要因为「看起来更高级」就处处 `unique_lock`——多出来的状态（是否拥有锁）本身就是心智负担。

#### 1.4 与 adopt_lock / try_to_lock

```cpp
std::mutex a, b;

void transfer() {
    // std::lock 按算法同时锁住多把，避免 AB-BA 死锁
    std::lock(a, b);
    // 告诉 lock_guard：锁已经持有，析构时负责 unlock，不要再 lock
    std::lock_guard<std::mutex> lk1(a, std::adopt_lock);
    std::lock_guard<std::mutex> lk2(b, std::adopt_lock);
    // ... 临界区 ...
}

bool try_work() {
    std::unique_lock<std::mutex> lk(a, std::try_to_lock);
    if (!lk.owns_lock()) {
        return false; // 没抢到，去做别的事或稍后重试
    }
    // ... 临界区 ...
    return true;
}
```

边界条件（对照 cppreference 时要记下）：

- 对同一 `std::mutex`，同一线程二次 `lock` → **未定义行为**（非 recursive_mutex）；
- `lock_guard` / `unique_lock` 管理的是「锁的所有权」，不是业务不变量本身——不变量仍靠你在临界区内维护；
- `mutex` 不可移动、不可拷贝；把它放进容器要用 `std::unique_ptr<std::mutex>` 或 `std::vector` + 原地构造时注意实现限制（常见做法是固定大小数组或 `std::unique_ptr<mutex[]>`）。

### 2. 临界区与锁顺序

#### 2.1 什么是临界区

临界区（critical section）不是「被大括号包起来的任意代码」，而是：

> 为了维护某个**不变量**，必须互斥执行的最短代码片段。

例子：限流桶的不变量是「令牌数从不小于 0，且扣减基于最新值」。

```cpp
class TokenBucket {
public:
    bool try_take(int n) {
        std::lock_guard<std::mutex> lk(mu_);
        if (tokens_ < n) return false;
        tokens_ -= n;
        return true;
    }
private:
    std::mutex mu_;
    int tokens_ = 0;
};
```

这里临界区必须覆盖「读 tokens_ + 写 tokens_」整段。若拆成：

```cpp
// 错误示范
bool try_take_broken(int n) {
    int cur;
    {
        std::lock_guard<std::mutex> lk(mu_);
        cur = tokens_;
    } // 解锁后别的线程可能改 tokens_
    if (cur < n) return false;
    std::lock_guard<std::mutex> lk(mu_);
    tokens_ -= n; // 可能减成负数
    return true;
}
```

看起来「每次访问都加了锁」，但**逻辑原子性**已经碎了。上一课的「复合操作」在这里用锁重新表达：**不变量相关的多个步骤必须在同一临界区内完成**。

#### 2.2 所有相关访问遵守同一契约

节点 summary 的后半句是关键：

> 正确性取决于所有相关访问遵守同一契约。

契约至少包含：

1. **哪把锁保护哪些数据**（文档或注释写清：`mu_` 保护 `map_`）；
2. **读也要拿同一把锁**（下一节误区会展开）；
3. **不允许「临时解锁做点事再锁回来」时假设数据没变**——除非你重新读取并重新验证不变量；
4. **回调 / 日志 / IO 默认放锁外**。

反例：锁内回调导致重入死锁。

```cpp
class Registry {
public:
    void add(int id, std::function<void()> cb) {
        std::lock_guard<std::mutex> lk(mu_);
        map_[id] = std::move(cb);
    }
    void notify_all() {
        std::lock_guard<std::mutex> lk(mu_);
        for (auto& [id, cb] : map_) {
            cb(); // 若 cb 内部又调用 add()，同线程二次 lock → UB / 死锁
        }
    }
private:
    std::mutex mu_;
    std::map<int, std::function<void()>> map_;
};
```

修法：锁内拷贝快照，锁外回调。

```cpp
void notify_all_safe() {
    std::vector<std::function<void()>> snapshot;
    {
        std::lock_guard<std::mutex> lk(mu_);
        snapshot.reserve(map_.size());
        for (auto& [id, cb] : map_) snapshot.push_back(cb);
    }
    for (auto& cb : snapshot) cb();
}
```

#### 2.3 多锁时的全局固定顺序

经典死锁：

```cpp
std::mutex mu_a, mu_b;

void thread1() {
    std::lock_guard<std::mutex> lk1(mu_a);
    std::lock_guard<std::mutex> lk2(mu_b); // 等待 B
    // ...
}

void thread2() {
    std::lock_guard<std::mutex> lk1(mu_b);
    std::lock_guard<std::mutex> lk2(mu_a); // 等待 A → 与 thread1 互相等待
    // ...
}
```

三种常见解法：

1. **全局固定顺序**：规定永远先锁 `mu_a` 再锁 `mu_b`（按地址、按分片下标、按资源层级）。
2. **`std::lock` / `std::scoped_lock`**：一次性锁多把，标准库保证不因加锁顺序死锁。
3. **避免同时持有多锁**：能串行化成「锁 A 做事 → 解锁 → 锁 B 做事」就不要嵌套。

公众号后台分片 KV 里，跨分片操作最容易踩死锁。规则示例：

> 若一次操作需要锁分片 i 与 j，则先锁 `min(i,j)`，再锁 `max(i,j)`。

```cpp
void move_item(ShardedKV& kv, const std::string& from, const std::string& to) {
    auto& s1 = kv.shard_for(from);
    auto& s2 = kv.shard_for(to);
    if (&s1 == &s2) {
        std::lock_guard<std::mutex> lk(s1.mu);
        // 同片内移动
        return;
    }
    std::mutex* first = &s1.mu;
    std::mutex* second = &s2.mu;
    if (first > second) std::swap(first, second); // 按地址排序
    std::lock(*first, *second);
    std::lock_guard<std::mutex> lk1(*first, std::adopt_lock);
    std::lock_guard<std::mutex> lk2(*second, std::adopt_lock);
    // 跨片移动
}
```

#### 2.4 临界区尽量短

短临界区不是「少写几行」，而是：

- 锁内只碰共享状态；
- 内存分配、JSON 解析、磁盘 IO、网络 RPC、打日志、用户回调 → **锁外**；
- 需要根据共享状态做重活时：锁内拷贝必要数据 → 解锁 → 锁外计算 → 若需写回再加锁并**重新检查**前提条件。

```cpp
std::string build_response(Cache& cache, const std::string& key) {
    std::string value;
    {
        std::lock_guard<std::mutex> lk(cache.mu);
        auto it = cache.map.find(key);
        if (it == cache.map.end()) return "miss";
        value = it->second; // 拷贝出去
    }
    // 锁外做昂贵工作
    return heavy_format(value);
}
```

### 3. 粗粒度和分片锁

#### 3.1 一把大锁：易验证，吞吐差

```cpp
class CoarseKV {
public:
    void set(const std::string& k, const std::string& v) {
        std::lock_guard<std::mutex> lk(mu_);
        map_[k] = v;
    }
    bool get(const std::string& k, std::string& out) const {
        std::lock_guard<std::mutex> lk(mu_);
        auto it = map_.find(k);
        if (it == map_.end()) return false;
        out = it->second;
        return true;
    }
private:
    mutable std::mutex mu_;
    std::unordered_map<std::string, std::string> map_;
};
```

优点：

- 正确性直观：所有路径一把锁；
- 遍历、清空、快照都简单；
- 死锁面极小（只有一把锁）。

缺点：

- 读与读也互斥（`std::mutex` 不区分读写）；
- 核数上去后，锁成为扩展瓶颈；
- 单次慢操作会拖高全表 p99。

适用：表很小、并发不高、正确性优先的控制面结构（配置表、少量连接元数据）。

#### 3.2 按 key hash 分片：提高并行度

```cpp
class ShardedKV {
public:
    explicit ShardedKV(size_t nshards)
        : shards_(nshards) {}

    void set(const std::string& k, const std::string& v) {
        auto& s = shard_for(k);
        std::lock_guard<std::mutex> lk(s.mu);
        s.map[k] = v;
    }

    bool get(const std::string& k, std::string& out) const {
        auto& s = shard_for(k);
        std::lock_guard<std::mutex> lk(s.mu);
        auto it = s.map.find(k);
        if (it == s.map.end()) return false;
        out = it->second;
        return true;
    }

    // 全表粗略 size：需要锁所有分片，注意顺序
    size_t size() const {
        size_t total = 0;
        // 按下标顺序加锁，避免死锁
        std::vector<std::unique_lock<std::mutex>> locks;
        locks.reserve(shards_.size());
        for (auto& s : shards_) {
            locks.emplace_back(s.mu);
        }
        for (auto& s : shards_) total += s.map.size();
        return total;
    }

private:
    struct Shard {
        mutable std::mutex mu;
        std::unordered_map<std::string, std::string> map;
    };

    Shard& shard_for(const std::string& k) {
        size_t h = std::hash<std::string>{}(k);
        return shards_[h % shards_.size()];
    }
    const Shard& shard_for(const std::string& k) const {
        size_t h = std::hash<std::string>{}(k);
        return shards_[h % shards_.size()];
    }

    std::vector<Shard> shards_;
};
```

注意：`std::vector<Shard>` 在移动/扩容时会出问题，因为 `mutex` 不可移动。上面构造时一次性定好 `nshards`，之后不再 `push_back`，这样是安全的。更稳妥可以用 `std::unique_ptr<Shard[]>` 或固定数组。

优点：

- 不同 key 落在不同分片时可并行；
- 热点若均匀分布，吞吐近似随分片数上升（直到别的瓶颈）。

代价：

- 跨分片操作要多锁 + 固定顺序；
- `size()` / 全局迭代变贵；
- 分片数、hash 函数、热点 key 倾斜都会影响效果；
- 代码与测试复杂度上升。

#### 3.3 用吞吐与 p99 说话

压测思路（实践环节会落地）：

1. 固定线程数（如 8）、固定操作混合（如 90% get / 10% set）、固定 key 空间；
2. 对比 `shards = 1, 8, 64, 256`；
3. 记录 QPS（吞吐）与延迟分位（p50 / p99）；
4. 观察：分片从 1→8 通常明显升；到某一档后收益变平甚至因缓存行/调度开销下降；
5. 再测「热点 key」（1% key 承担 50% 流量）——分片对热点帮助有限，需要别的手段（如热点拆分、本地缓存、读写锁）。

结论模板：

> 在 8 线程、90/10 读写、均匀 key 下，分片 64 相对粗锁吞吐 +X%，p99 -Yµs；分片 256 相对 64 收益 <Z%，但 `size()` 延迟明显上升。故默认选 64，并在文档中写明跨分片锁顺序约定。

---

## 常见误区与设计权衡

### 误区 1：锁住写不锁读通常仍然存在竞争

这是节点 pitfalls 的原话，也是线上最高频的错觉：

> 「get 是只读的，不会改数据，为什么要加锁？」

因为：

- 写线程可能正在 `rehash`，读线程的 `find` 会撞上未定义行为；
- 写线程可能正在改 `string` 的内部缓冲，读线程解引用会崩溃或读到撕破的值；
- 即便「看起来像原子的指针赋值」，在 C++ 里普通读写没有 happens-before 就是数据竞争。

正确契约：**凡是与写路径共享的数据，读路径也必须通过同一同步机制**（同一把 mutex，或 atomic / RCU / 锁外快照等明确方案）。

### 误区 2：临界区里「顺便」做重活

锁内打日志、格式化字符串、分配大块内存、调 RPC，都会把持锁时间从纳秒级拉到毫秒级。表现是：

- CPU 不高但吞吐上不去；
- p99 尖刺与锁等待监控正相关；
- 偶发「全线卡住」其实是某次锁内慢调用。

修法：锁内只碰共享状态；重活锁外；写回时重新验证。

### 误区 3：细锁一定更快

分片过多时：

- 每把锁自己的缓存行可能 false sharing（可用对齐缓解）；
- 跨分片操作概率上升；
- 代码路径更长，分支更多；
- 若负载本身是单 key 热点，分片等于没分。

**没有压测数字就不要宣称细锁胜利。**

### 误区 4：用 `recursive_mutex` 掩盖重入设计问题

同线程重入锁有时能「先跑起来」，但会：

- 让「谁持锁」的推理变难；
- 掩盖本该锁外做的回调；
- 性能通常差于普通 mutex。

优先重构为「锁内快照 + 锁外回调」，而不是默认 recursive。

### 误区 5：解锁后继续使用临界区内取得的引用/迭代器

```cpp
std::string& bad_get(CoarseKV& kv, const std::string& k) {
    std::lock_guard<std::mutex> lk(kv.mu_);
    return kv.map_[k]; // 返回引用，锁已释放
} // 调用方读引用时，别人可能正在 erase / rehash
```

应返回拷贝，或在锁外使用的数据结构是快照。

### 设计权衡：粗锁易验证，细锁增加并行度也增加死锁与维护风险

节点 tradeoffs：

> 粗锁易验证，细锁增加并行度也增加死锁与维护风险。

落地选择清单：

| 维度 | 偏粗锁 | 偏分片/细锁 |
|------|--------|-------------|
| 正确性验证 | 易 | 难（锁顺序、跨片不变量） |
| 单核/低并发 | 足够 | 可能过度设计 |
| 多核均匀负载 | 易成瓶颈 | 通常更好 |
| 热点 key | 都难受 | 仍难受，需额外策略 |
| 全局操作 | 简单 | 贵且易错 |
| 人员维护成本 | 低 | 高 |

工程建议：

1. **先粗锁做对**（TSan 干净、不变量清晰）；
2. **用压测证明粗锁是瓶颈**（锁等待、p99）；
3. **再分片**，并写下锁顺序与跨片 API 规范；
4. **持续用 TSan / 死锁检测 / 指标**守护回归。

---

## 动手实践

实践任务（节点 practice）：

> 为共享KV实现互斥保护，压测不同分片锁数量。

验收（acceptance）：

- 所有共享访问遵守锁约定；
- 锁粒度选择有吞吐与 p99 证据。

并呼应 sourceStudy：

> 沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。

### 环境与版本记录（先做）

```bash
mkdir -p /tmp/atlas-e-lock && cd /tmp/atlas-e-lock
c++ --version
# 记录：编译器版本、libstdc++/libc++、-std=c++17 或更高
cat > versions.txt << EOF
date: $(date)
cxx: $(c++ --version | head -1)
std: c++17
mutex: std::mutex / lock_guard / unique_lock per cppreference
EOF
```

打开并阅读（至少浏览定义与示例）：

- https://en.cppreference.com/w/cpp/thread/mutex.html
- 同页延伸：`lock_guard`、`unique_lock`、`scoped_lock`、`lock`、`adopt_lock`、`defer_lock`

在 `versions.txt` 追加：

```text
notes:
- std::mutex: non-recursive; double-lock by same thread = UB
- lock_guard: lock on ctor, unlock on dtor; not movable
- unique_lock: can defer/try/adopt; movable; works with condition_variable
- unlock/lock on mutex establishes synchronizes-with / happens-before
```

### 步骤 1：无锁 KV，制造可观测竞争

```cpp
// kv_race.cpp
#include <iostream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

class NaiveKV {
public:
    void set(const std::string& k, const std::string& v) { map_[k] = v; }
    bool get(const std::string& k, std::string& out) const {
        auto it = map_.find(k);
        if (it == map_.end()) return false;
        out = it->second;
        return true;
    }
    size_t size() const { return map_.size(); }
private:
    std::unordered_map<std::string, std::string> map_;
};

int main() {
    NaiveKV kv;
    constexpr int T = 8, N = 100000;
    std::vector<std::thread> th;
    for (int t = 0; t < T; ++t) {
        th.emplace_back([&, t] {
            for (int i = 0; i < N; ++i) {
                auto k = "k" + std::to_string((t * N + i) % 1000);
                kv.set(k, "v");
                std::string out;
                kv.get(k, out);
            }
        });
    }
    for (auto& x : th) x.join();
    std::cout << "size=" << kv.size() << "\n";
}
```

```bash
c++ -std=c++17 -O1 -g -fsanitize=thread -pthread kv_race.cpp -o kv_race_tsan
./kv_race_tsan 2>&1 | head -80
```

预期：TSan 报告 data race。把报告关键栈保存到 `tsan_naive.txt`。

### 步骤 2：粗锁修复，确认 TSan 干净

把 `NaiveKV` 改成上一节的 `CoarseKV`（所有 `get`/`set`/`size` 持同一把 `mu_`）。

```bash
c++ -std=c++17 -O1 -g -fsanitize=thread -pthread kv_coarse.cpp -o kv_coarse_tsan
./kv_coarse_tsan 2>&1 | tee tsan_coarse.txt
# 应无 data race 报告
```

自检清单：

- [ ] 读路径也加锁；
- [ ] 没有返回指向 map 内部的引用；
- [ ] 异常路径由 `lock_guard` 自动解锁。

### 步骤 3：实现分片 KV

实现上一节 `ShardedKV`。注意：

- 构造时固定分片数；
- `shard_for` 用 `std::hash`；
- `size()` 按下标顺序锁全部分片；
- 不要在持锁时打印大量日志。

单测：单线程 set/get；多线程随机 key；故意写一个「锁写不锁读」的坏版本对比 TSan。

### 步骤 4：压测不同分片数

```cpp
// bench_kv.cpp 核心逻辑伪代码
// 对 shards in {1, 8, 64, 256}:
//   预热 1s
//   跑 5s：每线程循环随机 get/set（90/10），key 空间 100000
//   汇总 ops，计算 QPS
//   抽样延迟（可用 chrono::steady_clock 包住单次 get）估算 p99
```

建议输出表：

```text
shards  threads  mix    qps      p50_us  p99_us
1       8        90/10  ...      ...     ...
8       8        90/10  ...      ...     ...
64      8        90/10  ...      ...     ...
256     8        90/10  ...      ...     ...
```

再跑一轮**热点负载**（key 从很小集合抽），观察分片收益是否消失。

### 步骤 5：锁顺序与死锁演练

写一个跨分片 `move`：错误版按「先 from 再 to」加锁（当 from/to 分片号反向时会死锁）；正确版按 `min/max` 或 `std::lock`。

用两个线程反复反向 move，错误版应能复现卡住；正确版长时间跑不卡。可用 `timeout` 命令包裹：

```bash
timeout 10s ./deadlock_demo_bad || echo "bad version stuck/timed out as expected"
timeout 10s ./deadlock_demo_good && echo "good version finished"
```

### 步骤 6：sourceStudy 核对清单

对照 https://en.cppreference.com/w/cpp/thread/mutex.html 及关联页，逐项打勾：

1. [ ] `mutex::lock` / `unlock` / `try_lock` 的语义与异常声明；
2. [ ] 同线程对非递归 mutex 二次 lock 为 UB；
3. [ ] `lock_guard` 构造/析构；`adopt_lock` 含义；
4. [ ] `unique_lock` 的 `defer_lock` / `try_to_lock` / `owns_lock` / 可移动；
5. [ ] `std::lock` 对多 Lockable 的死锁避免算法；
6. [ ] 记录你使用的 `-std=` 版本与实现（libstdc++/libc++）是否完整支持所用 API；
7. [ ] 把「边界条件」写进 `versions.txt`（空临界区、try_lock 失败、异常中解锁）。

### 步骤 7：验收清单

- [ ] 所有共享访问（get/set/size/move）遵守同一锁约定；故意去掉读锁时 TSan 能抓住；
- [ ] 粗锁与至少 3 档分片数有 QPS / p99 表；
- [ ] 有书面结论：默认分片数选多少、为什么、热点场景怎么办；
- [ ] 跨分片锁顺序有测试；
- [ ] sourceStudy 笔记完整。

---

## 自测题

### 题目 1：`lock_guard` 和 `unique_lock` 怎么选？

说明二者的核心差异，并判断下列场景该用哪个（或 `scoped_lock`）：

1. 函数开头加锁，结束时解锁，中途不需要解锁；
2. 需要 `condition_variable::wait`；
3. 需要同时锁两把 mutex 且不想自己定顺序；
4. 临界区中间要做一段不碰共享状态的重活，希望临时解锁。

### 题目 2：为什么「锁写不锁读」仍然有数据竞争？

结合 `std::unordered_map` 的 `find` 与可能触发 `rehash` 的 `operator[]`，解释只在写路径加锁为何不够。正确契约是什么？

### 题目 3：粗锁 vs 分片锁如何取舍？

节点 tradeoffs 说「粗锁易验证，细锁增加并行度也增加死锁与维护风险」。请给出一个你会采用的决策流程（至少 4 步），并说明跨分片操作如何避免死锁。

---

## 自测题参考答案

### 答案 1

核心差异：

- `lock_guard`：极简 RAII，构造加锁、析构解锁，不可移动，不可手动解锁；
- `unique_lock`：可延迟/尝试/接管加锁，可手动 lock/unlock，可移动，可与条件变量配合；多了「是否拥有锁」的状态。

场景判断：

1. **`lock_guard`（或单锁时的 `scoped_lock`）**——默认选择；
2. **`unique_lock`**——`wait` 需要在等待时释放锁；
3. **`std::scoped_lock` 或 `std::lock` + `adopt_lock` 的 `lock_guard`/`unique_lock`**——避免 AB-BA；
4. **`unique_lock`**——中途 `unlock()`，回来再 `lock()`，并重新读取共享状态。

### 答案 2

`unordered_map` 的读（`find`、迭代）与写（插入导致 rehash、改节点）若无 happens-before，就是数据竞争：读线程可能正遍历桶链，写线程正在搬桶，表现为崩溃、死循环或错误结果。即便元素是直观「赋值」，`std::string` 的非原子写也会让并发读读到撕破状态。

正确契约：所有相关访问（读、写、遍历、删除）遵守同一同步约定——通常是同一把（或同一组分片）mutex；或者改用有明确并发语义的结构/算法，而不是「我觉得只读就安全」。

### 答案 3

决策流程示例：

1. 先用粗锁把正确性做对（TSan 干净、不变量清晰）；
2. 压测量化锁是否成为瓶颈（锁等待、QPS、p99）；
3. 若是瓶颈且负载较均匀，引入分片，从较小档位逐步增大；
4. 用数据选默认分片数，并文档化跨分片锁顺序；热点场景单独治理；
5. 回归时保留 TSan 与死锁/超时测试。

跨分片避免死锁：对需要同时持有的分片锁按**全局固定顺序**（分片下标或 mutex 地址）加锁，或使用 `std::lock` / `std::scoped_lock` 一次性锁定。

---

## 推荐资源

- **互斥锁与锁粒度：参考文档 / 原始资料**  
  https://en.cppreference.com/w/cpp/thread/mutex.html  
  - 来源：en.cppreference.com  
  - 语言：英文 · 类型：文档 · 难度：进阶 · 免费  
  - 阅读重点：`lock_guard` 和 `unique_lock` 与临界区与锁顺序，用于核对本节点实验的语义和边界。  
  - 建议同步打开同站点的 `lock_guard`、`unique_lock`、`scoped_lock`、`std::lock` 页面，把「错误处理 / UB 边界」抄进 `versions.txt`。

---

## 本课小结

1. **互斥锁把临界区串行化并建立同步关系**；正确性取决于所有相关访问遵守同一契约。
2. **默认用 `lock_guard`（RAII）**；需要延迟、手动解锁、条件变量或所有权转移时再用 `unique_lock`。
3. **临界区为不变量服务**：复合操作放同一临界区；回调与重活放锁外；多锁遵循全局顺序。
4. **粗锁易验证，分片锁换并行度**：用吞吐与 p99 证据选择粒度，并承认细锁带来的死锁与维护成本。
5. **锁写不锁读通常仍然存在竞争**——读也是相关访问的一部分。
6. 下一课起，你会在更多同步原语（如条件变量、原子、内存序）上继续扩展；但「契约 + RAII + 粒度有据」这三板斧在后端并发里会反复出现。

---

## 配套练习建议

1. **完整实现**：把 `CoarseKV` / `ShardedKV` / bench 做成可编译仓库，README 含复现命令与结果表。
2. **TSan 对照**：保留「锁写不锁读」坏版本，作为教学用例归档报告。
3. **死锁复现**：AB-BA 与跨分片反向加锁各一则，配 `timeout` 脚本。
4. **持锁时间审计**：用简单 RAII 计时器统计持锁分位，故意在锁内 `sleep` 看 p99 崩坏。
5. **热点 key**：测量分片对热点无效，尝试「热点拆到独立分片」或本地线程缓存（注意一致性）。
6. **`scoped_lock` 重写**：用 C++17 `std::scoped_lock` 替换手写 `std::lock` + `adopt_lock`。
7. **源码级 sourceStudy**：把 cppreference 中 mutex 相关 UB 条目与自己的测试行号对应表做进笔记。
8. **代码审查清单**：新增共享结构时必答——锁是谁、读路径是否覆盖、临界区是否最短、多锁顺序、跨片 API、压测证据。
9. **与 e-race 串联**：同一份 KV，先演示无锁竞争，再演示粗锁，再演示分片，形成一条完整故事线。
10. **微信场景小品**：选「会话表」或「限流桶」之一，写一页设计说明：不变量、锁契约、粒度选择与降级策略。
