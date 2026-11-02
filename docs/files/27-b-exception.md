# 第 27/208 课：异常安全与错误模型

> 领域 B · 阶段 2 · 难度：基础 · 前置：RAII（b-raii）、类型系统（b-types）

## 一、学习目标和为什么要懂

**一句话**：异常安全说的是**操作失败时，对象和系统还能保住哪些不变量**；而“错误用异常还是错误码传”是**另一个问题**。两件事要分开想。

**为什么后台要懂**：要设计出**不会只改了一半、不会泄漏资源、不会丢掉错误上下文**的库接口。比如公众号后台的配置热加载：新配置解析到一半失败，如果超时已经改成新值、连接数还是旧值，服务就跑在一个谁也没设计过的“混合配置”上。

**学完你应该能**：
1. 讲清楚基本保证、强保证、不抛保证的区别；
2. 独立完成：为配置热加载实现“先解析、再替换”，注入失败后旧配置依然完整可用。

**验收标准**：能说出一个操作提供的是哪种保证；失败路径不会留下部分生效的状态。

---

## 二、核心要点：从直觉到原理

### 1. 基本、强与不抛保证

直觉：把一个操作想成银行转账。失败时你能接受什么样的结果？

| 保证 | 失败后的状态 | 转账类比 |
|---|---|---|
| **不抛（nothrow）** | 根本不会失败 | 读余额 |
| **强（strong）** | 和调用前**完全一样**，像没发生过 | 转账失败，两边余额都不动 |
| **基本（basic）** | 不泄漏、对象仍**有效、可析构、可再用**，但值可能变了 | 账户没坏，但金额不一定是原来的 |
| 无保证 | 可能泄漏、不变量破坏 | 钱扣了没到账 |

**原理**：强保证的通用套路是 **“在旁边做完所有可能失败的事 → 用一个不抛的操作提交”**（copy-and-swap）：

```cpp
void Holder::reload(const std::string& text) {
    Config next = parse(text);   // 可能抛：此时还没动 *this
    std::swap(cur_, next);       // noexcept：提交，不会失败
}                                // 旧值随 next 析构
```

关键点：**强保证依赖不抛操作**。`swap`、移动构造、析构函数应该是 `noexcept`。这也是上一阶段讲的：`vector` 扩容时，只有元素的移动构造是 `noexcept`，才敢用移动，否则退回拷贝来维持强保证。

后台例子：给用户写“关注列表 + 计数”两份数据，先改列表、再改计数，中间抛异常就只有基本保证（甚至没有）；先把两份新值都算好再一起提交，才接近强保证。

### 2. 栈展开与 RAII

异常抛出后，运行时会**一层层退出栈帧，并析构途中所有已构造好的局部对象**，这叫栈展开（stack unwinding）。所以 RAII 对象在失败路径上会被自动清理：

```cpp
struct Guard { const char* n; ~Guard() { std::printf("~Guard(%s)\n", n); } };
void inner()  { Guard g{"inner"};  throw std::runtime_error("boom"); }
void middle() { Guard g{"middle"}; inner(); std::puts("never printed"); }
int main() {
    try { Guard g{"main-try"}; middle(); }
    catch (const std::exception& e) { std::printf("caught: %s\n", e.what()); }
}
```

实测输出（g++ -std=c++23）：

```
~Guard(inner)
~Guard(middle)
~Guard(main-try)
caught: boom
```

要记住的推论：
- **资源全交给 RAII**（锁用 `lock_guard`，fd/内存用智能指针或封装类），失败路径就不用手写清理，自然拿到基本保证。
- **析构函数不要抛**：栈展开中析构再抛异常，会直接 `std::terminate`。析构函数默认就是 `noexcept`。
- RAII 只负责“资源不漏”，**不负责“状态回滚”**。值改了一半，RAII 帮不了你——这得靠第 1 点的“先算后提交”。

### 3. 异常、错误码和 expected

这是“错误怎么**传递**”的问题，和第 1 点“失败后状态是否完好”正交：用错误码一样可以写出半更新的 bug。

```cpp
// 异常：自动跨层传播，调用方不处理就往上抛
Config parse_or_throw(const std::string&);

// 错误码：显式，但容易被忽略
int parse(const std::string&, Config* out);   // 返回 0 成功

// std::expected（C++23）：值或错误二选一，类型里写明“可能失败”
std::expected<Config, std::string> parse_config(const std::string&);

auto r = parse_config(text);
if (!r) { log(r.error()); return; }
use(*r);
```

| | 异常 | 错误码 / expected |
|---|---|---|
| 传播 | 自动跨多层 | 每层手动传 |
| 可见性 | 签名里看不出会失败 | 返回类型就写着 |
| 适合 | 跨层的、少见的失败 | 预期内的失败（用户输入、配置格式错） |

常见的组合方式：**模块内部用异常，在边界（RPC handler、线程入口、C 接口）处统一捕获并转换成错误码 / expected**，不让异常越过边界。

---

## 三、常见误区与设计权衡

**误区（节点原文）**：**捕获所有异常并忽略，会把失败变成“错误的成功”**。

```cpp
try { holder.reload(text); } catch (...) {}   // 错！调用方以为热加载成功了
```

配置没生效、监控也看不到，问题会拖到线上才暴露。正确做法：至少记录日志、上报，并把失败返回给调用方。

其他容易踩的坑：
- 以为“用了 RAII 就是强保证”——RAII 只保证不泄漏，值仍可能改了一半。
- 移动构造 / `swap` 没写 `noexcept`，导致容器扩容退回拷贝，或者无法实现强保证。
- 在析构函数里做可能失败的事并抛出。

**设计权衡（节点原文）**：异常适合跨层传播，错误码便于显式控制；**团队需要统一边界约定**。进团队后先看看现有代码怎么约定（比如是否禁用异常、错误码怎么定义），跟着约定写，不要在一个模块里两套混用。

另外，强保证往往要多一份拷贝（先构造新对象），代价是内存和时间；不是所有操作都值得强保证，**至少基本保证，关键状态变更再上强保证**。

---

## 四、动手实践：配置热加载“解析后替换”

目标：为配置热加载实现“解析后替换”，注入失败后确保旧配置完整可用。

**步骤 1：把解析写成纯函数**，只读输入、返回新对象，不碰任何全局状态：

```cpp
std::expected<Config, std::string> parse_config(const std::string& text);
// 缺 '='、缺字段、stoi 抛异常、timeout_ms<=0，都返回 unexpected
```

内部 `std::stoi` 可能抛，在函数内捕获后转成 `unexpected`，异常不越过这个边界。

**步骤 2：用 `shared_ptr<const Config>` 持有当前配置**，读者拿快照：

```cpp
std::shared_ptr<const Config> get() const {
    std::lock_guard lk(mu_);
    return cur_;
}
```

**步骤 3：reload = 先在旁边做完，再 noexcept 提交**：

```cpp
std::expected<void, std::string> reload(const std::string& text) {
    auto parsed = parse_config(text);                        // 可能失败
    if (!parsed) return std::unexpected(parsed.error());
    auto next = std::make_shared<const Config>(std::move(*parsed)); // 可能抛 bad_alloc，仍未改动
    std::lock_guard lk(mu_);
    cur_.swap(next);                                          // noexcept 提交
    return {};
}
```

**步骤 4：注入失败**，每次都检查指针没变、值还是旧的：

```cpp
const char* bad[] = {
    "timeout_ms=500\nmax_conn=abc\n",            // stoi 抛 invalid_argument
    "timeout_ms=500\n",                          // 缺字段
    "timeout_ms=500\nnoequal\n",                 // 格式错
    "timeout_ms=-1\nmax_conn=10\n",              // 校验失败
    "timeout_ms=500\nmax_conn=99999999999999\n", // out_of_range
};
for (auto t : bad) {
    CHECK(!h.reload(t));
    CHECK(h.get() == before);
    CHECK(h.get()->timeout_ms == 200 && h.get()->max_conn == 100);
}
```

**步骤 5：写一个反例对照**——边解析边写字段的 `bad_reload`，喂 `timeout_ms=999\nmax_conn=abc`，观察到 `timeout_ms` 已改成 999 而 `max_conn` 还是旧值，就是“部分生效”。

**步骤 6：编译运行（开 ASan/UBSan）**：

```bash
g++ -std=c++23 -Wall -Wextra -fsanitize=address,undefined -g config_reload.cpp -o config_reload
./config_reload
```

我实际跑出来的结果：

```
reload rejected: bad or missing field: stoi
reload rejected: bad or missing field: map::at
reload rejected: line 2: missing '='
reload rejected: timeout_ms must be > 0
reload rejected: bad or missing field: stoi
bad_reload threw: stoi; now timeout_ms=999 max_conn=50 (half-applied)
ALL_CHECKS_PASSED
```

完整代码在附件 `practice27/`（`config_reload.cpp`、`unwind.cpp`、`test.sh`）。

**自检**：你的 `reload` 提供哪种保证？答：强保证；`get()` 和 `swap` 不抛。另外，成功 reload 之后，之前拿到的旧快照依然有效（shared_ptr 保活），正在处理请求的线程不会被换掉手里的配置。

---

## 五、自测题

**1. `std::vector::push_back` 在需要扩容时，如果元素的移动构造没有标 `noexcept`、拷贝构造可用，vector 会怎么做？为什么？**

<details><summary>答案</summary>
会用拷贝构造把旧元素搬到新内存。因为 push_back 要提供强保证：如果用可能抛的移动，搬到一半抛了，旧缓冲区里的元素已被移走，无法恢复；而拷贝失败时旧缓冲区完好，直接丢掉新缓冲区即可。标上 noexcept 后才会用移动（第 21 课实测过 copy/move 次数的变化）。
</details>

**2. 下面的函数提供哪种异常保证？怎么改成强保证？**

```cpp
void set_both(User& u, const std::string& name, const std::string& nick) {
    u.name = name;   // string 赋值可能抛 bad_alloc
    u.nick = nick;
}
```

<details><summary>答案</summary>
基本保证：不泄漏、u 仍有效，但可能 name 已改、nick 没改。改法：先在旁边构造好新值，再用不抛操作提交：
`std::string n = name, k = nick; u.name.swap(n); u.nick.swap(k);`
所有可能抛的拷贝都在 swap 之前完成，swap 是 noexcept。
</details>

**3. 一个 RPC handler 里写了 `try { reload(text); } catch (...) {}`，并给调用方返回成功。问题在哪？应该怎么处理？**

<details><summary>答案</summary>
这是节点点名的误区：吞掉所有异常，把失败变成“错误的成功”，调用方和监控都以为配置已生效。应该至少记录日志和上报，并把失败（错误码或 expected 的 error）返回给调用方。在 handler 这个边界统一把异常转换成错误码是可以的，但不能丢掉“失败”这个事实和它的上下文。
</details>

---

## 六、推荐资源

- [cppreference：Exceptions](https://en.cppreference.com/w/cpp/language/exceptions.html)（英文文档，进阶，免费）：重点读异常安全保证（基本、强、不抛）和栈展开部分，用来核对本课实验的语义与边界条件。

**源码学习建议（节点原文）**：沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件。比如本课用的 `std::expected` 是 C++23，用 g++ 编译要加 `-std=c++23`。
