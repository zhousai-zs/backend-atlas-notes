# 第 23/208 课：STL容器与迭代器

> 领域 B（C++）· 阶段 1 · 难度：基础 · 子类：泛型与标准库
> 前置：b-move（移动语义）、a-hash（哈希）、a-tree（树）

**一句话**：标准容器提供不同的数据布局和复杂度保证，迭代器规则是正确使用容器的重要契约。

## 一、学习目标与为什么后端要懂

学完你应该能：
1. 讲清 `vector` 和 `deque` 的区别；
2. 独立实现一个**有上限的 LRU 缓存**，并说清 `list + 哈希表` 组合的成本；
3. （验收）指出 `vector` 扩容后哪些引用失效；为"有序范围查询"和"单点查询"各选对容器。

**为什么后端要懂**：避免扩容、删除及 rehash 后的悬垂访问。后台服务里，容器是最常用的数据结构：连接表、会话表、本地热点缓存、按时间排序的消息队列……一旦在"容器变化后还拿着旧指针/迭代器"，轻则读到脏数据，重则偶发 core，而且往往只在高峰期（扩容最频繁时）出现，极难复现。

## 二、从直觉到原理

### 1. vector 与 deque

**直觉**：`vector` 是一整块连续数组；`deque` 是"一排固定大小的块 + 一张块指针表"。

| | vector | deque |
|---|---|---|
| 内存 | 单块连续 | 分段连续 |
| 尾部插入 | 均摊 O(1)，满了要整体搬家 | O(1)，满了加一块，不搬旧元素 |
| 头部插入 | O(n) | O(1) |
| 随机访问 | O(1)，最快（一次寻址） | O(1)，但要两次寻址 |
| 缓存局部性 | 最好 | 稍差 |

**原理**：`vector` 容量用完时会申请更大的一块（常见实现约 1.5 或 2 倍）、把元素移动/拷贝过去、释放旧块。所以：
- **扩容后，所有指向旧元素的指针、引用、迭代器全部失效**；
- 没扩容的 `push_back` 只让 `end()` 失效。

```cpp
std::vector<int> v{1, 2, 3};
int& first = v[0];
v.push_back(4);        // 若触发扩容，first 已悬垂
std::cout << first;    // 未定义行为，ASan 会报 heap-use-after-free
```

（复习第 21 课：元素的移动构造是 `noexcept` 时，扩容才会用移动而不是拷贝。）

`deque` 的头尾插入不会搬动已有元素，**指向元素的引用保持有效**，但迭代器会失效——这是个容易记反的细节。

**后台场景**：存一批请求做批处理，用 `vector` + 预先 `reserve`；需要两头进出的滑动窗口（比如最近 N 秒的请求时间戳），用 `deque`。

### 2. map 与 unordered_map

**直觉**：`map` 是排好序的书架（红黑树），`unordered_map` 是带编号的储物柜（哈希表）。

| | map | unordered_map |
|---|---|---|
| 底层 | 红黑树（a-tree） | 哈希桶（a-hash） |
| 查找 | O(log n) | 平均 O(1)，最坏 O(n) |
| 有序遍历 / 范围查询 | 支持 `lower_bound/upper_bound` | 不支持 |
| 插入对迭代器 | 都不失效 | rehash 时**迭代器全部失效**（元素引用仍有效） |
| 内存 | 每节点三指针 + 颜色 | 桶数组 + 节点 |

```cpp
std::map<int64_t, Msg> by_time;            // key = 发送时间戳
auto lo = by_time.lower_bound(t1);
auto hi = by_time.upper_bound(t2);
for (auto it = lo; it != hi; ++it) { /* [t1, t2] 内的消息 */ }

std::unordered_map<uint64_t, Session> sessions;  // 按 uid 单点查
sessions.reserve(100000);                         // 预留桶，减少 rehash
```

**选型规则**（对应验收第 2 条）：要"某时间段内的数据""排行榜前 N"等**有序范围**用 `map`；只按 key 查一条（如按 openid 找会话）用 `unordered_map`。

### 3. 迭代器失效和 reserve

迭代器本质是"指向容器内部位置的东西"，容器结构一变，它可能就指向了错的地方。必背规则：

- **vector**：扩容 → 全部失效；`insert/erase` → 该位置及之后失效。
- **unordered_map**：rehash → 迭代器全部失效；`erase` → 只有被删那个失效。
- **map / list**：`erase` → 只有被删那个失效，其余都不受影响。

边遍历边删除的正确写法（`erase` 返回下一个有效迭代器）：

```cpp
for (auto it = m.begin(); it != m.end(); ) {
    if (expired(it->second)) it = m.erase(it);
    else ++it;
}
```

`reserve(n)` 只预分配**容量**，保证在容量内 `push_back` 不扩容、指针稳定：

```cpp
std::vector<int> r;
r.reserve(100);
// r.size() == 0！r[0] = 1; 是越界写，未定义行为
r.push_back(1);  // 正确
```

### 4. 综合：LRU 缓存为什么是 list + 哈希表

- `unordered_map<key, list::iterator>` 负责 O(1) 找到节点；
- `std::list` 负责 O(1) 把节点挪到表头（`splice`）、O(1) 从表尾淘汰；
- 关键：**`list` 的迭代器在 splice/其他元素增删时都不失效**，所以哈希表里存它的迭代器才安全。换成 `vector` 存就不行。

**成本**：每个元素两份开销——链表节点（前后两个指针 + 数据，一次单独的堆分配）+ 哈希节点（key 拷贝 + 迭代器 + 桶）。节点分散在堆上，缓存局部性差。条目很多、value 很小时，元数据可能比数据还大；对性能要求高时可考虑预分配节点池或侵入式链表。

## 三、常见误区

1. **reserve 只改变容量，不构造元素**：`reserve` 后直接 `v[i] = x` 是越界。想要有元素请用 `resize`。
2. 在 `push_back` 之后继续用之前取的 `&v[0]`、`v.begin()`。
3. 遍历 `unordered_map` 时插入元素，触发 rehash 后继续 `++it`。
4. 以为 `unordered_map` 一定比 `map` 快：小数据量、需要有序时 `map` 更合适；哈希冲突严重时退化到 O(n)。
5. 用 `map[key]` 做"查找"：key 不存在时会插入一个默认值，用 `find` 或 `count`。

## 四、设计权衡

复杂度、局部性、稳定性与内存开销需要一起比较。大 O 相同时，连续内存的 `vector` 往往因 CPU 缓存友好而实际更快；需要"元素地址稳定"（被别处长期引用）时，要选 `list`/`map`，或者存 `unique_ptr`；内存紧张时要算上每节点的指针开销。

## 五、动手实践：有上限的 LRU 缓存

代码在附件 `practice23/`（`lru.cpp` + `test.sh`），已在 g++ C++17 + ASan/UBSan 下验证通过。

1. 新建目录，写 `LruCache<K,V>`：成员 `std::list<std::pair<K,V>> items_`（头部 = 最近使用）和 `std::unordered_map<K, list::iterator> idx_`。
2. `get(k)`：哈希表找不到返回空；找到就 `items_.splice(items_.begin(), items_, it)` 移到表头再返回。
3. `put(k, v)`：已存在就更新并移到表头；不存在时若已满，先 `idx_.erase(items_.back().first)` 再 `pop_back()`，然后 `emplace_front` 并记录迭代器。
4. 测试：容量 2，put a、b，get a，put c ⇒ b 被淘汰，a、c 仍在。
5. 加一段观察 `vector` 扩容：记录 `data()`，`push_back` 到容量变化，比较地址。
6. 加一段 `reserve(100)`：确认 `size()==0`，再 push 100 个元素，`data()` 不变。
7. 运行：

```bash
cd practice23 && ./test.sh
```

期望输出：

```
LRU OK: b evicted, size=2
vector realloc: capacity 1 -> 2, data moved=yes
after reserve(100): size=0 capacity>=100=yes
100 push_back within reserved capacity: data stable=yes
map range [200,400]: 2 items
ALL_TESTS_PASSED
```

8. 用自己的话写下 `list + unordered_map` 每个条目的内存成本，以及为什么不能用 `vector` 替代 `list`。

**进阶**：故意在 `get` 前先取 `auto& ref = vec[0]`，然后 push 到扩容再读 `ref`，看 ASan 报 heap-use-after-free。

## 六、自测题

**1. `std::vector<int> v(4); v.reserve(4); int* p = &v[1]; v.push_back(5);` 之后 p 还能用吗？**
<details><summary>答案</summary>
不能保证。容量为 4、size 为 4，push_back 第 5 个元素会触发扩容，所有指针、引用、迭代器全部失效，p 悬垂。若之前 `reserve(8)`，则不扩容，p 仍有效（只有 end() 失效）。
</details>

**2. 要支持"查某用户 uid 的会话"和"查 10:00–10:05 之间创建的所有会话"，分别用什么容器？**
<details><summary>答案</summary>
单点按 uid 查用 `unordered_map<uid, Session>`，平均 O(1)；时间范围查询用 `map<创建时间, ...>`（或 multimap，时间可能重复），借助 `lower_bound/upper_bound` 做 O(log n + k) 的范围遍历。实际可两者并存，一个做主索引一个做二级索引。
</details>

**3. LRU 实现里，哈希表存 `list::iterator` 为什么安全？换成存 `vector` 下标或迭代器会怎样？**
<details><summary>答案</summary>
`std::list` 的迭代器只有在指向的那个元素被删除时才失效，`splice` 移动节点和其他元素的增删都不影响它，所以可以长期存。`vector` 中间删除会让后面元素前移（下标和迭代器都错位），扩容还会让全部迭代器失效，而且把元素移到头部是 O(n)。
</details>

## 七、推荐资源

- [cppreference：Containers library](https://en.cppreference.com/w/cpp/container.html)（英文文档，基础，免费）：重点阅读 vector 与 deque、map 与 unordered_map，用于核对本节点实验的语义和边界；每个容器页都有 "Iterator invalidation" 小节，建议对照本课规则过一遍。
