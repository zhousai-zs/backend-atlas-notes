# 第 10/208 课：搜索树、堆与有序索引

> 节点：`a-tree` · 域：A · 子类：数据结构与算法 · 难度：基础 · 前置：`a-sequence`
>
> 一句话：树用层次结构组织搜索路径，堆只维护局部优先关系并不提供全局有序遍历。平衡树、搜索树和堆服务不同访问模式，应依据查询和更新需求选择。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释二叉搜索树（BST）不变量**：对任意节点，左子树键都小于（或 ≤）它，右子树键都大于（或 ≥）它；中序遍历得到有序序列。
2. **说明平衡与树高的关系**：高度决定查找/插入/删除的路径长度；退化成链时复杂度从 O(log n) 变成 O(n)。
3. **区分二叉堆与搜索树**：堆只保证父子之间的局部优先（堆序），**不**提供全局有序遍历，也不支持高效的任意键查找或范围扫描。
4. **独立完成实践**：用最小堆实现定时任务队列，并验证插入与按到期时间取出（删除堆顶）的顺序。
5. 在微信公众号后台场景里，能说清「定时推送 / 优先级任务 / 范围查找」各自更适合堆还是搜索树（或有序索引）。

### 1.2 验收标准

节点给出的验收标准有两条，本课会反复回扣：

- **能区分堆和搜索树的查询能力。** 搜索树（及平衡搜索树、有序索引）支持按键查找、有序遍历、范围查询（如「CreateTime 在 [t1, t2]」）。堆只擅长「反复取出当前最优先的那一个」（最小到期时间、最高优先级），**不能**把堆数组当成已排序数组去二分或范围扫描。
- **能解释树退化为什么影响复杂度。** BST 的查找成本与树高成正比。若插入顺序有序或近似有序，普通 BST 可能退化成一条链，高度变成 n-1，每次操作 O(n)。这就是为什么生产系统里有序索引通常用平衡树（或等价结构），而不是裸 BST。

### 1.3 为什么微信公众号后台要懂树与堆

节点对「为什么」的说明很直接：**支撑定时器、优先级任务和范围查找。** 放到即将入职的腾讯微信公众号后台，这三类需求几乎天天碰到：

- **定时推送 / 延迟消息。** 运营配置「明天下午 3 点群发」、模板消息重试「60 秒后再投」、access_token 刷新「提前 5 分钟」。大量任务按**到期时间**排队，反复要的是「下一个该执行的是谁」——这是**最小堆 / 优先队列**的典型用法。
- **优先级任务队列。** 粉丝互动回调要低延迟；批量打标签、离线报表可以往后排。用堆按优先级取出，比每次全表扫描「找优先级最高的」便宜得多。
- **范围查找。** 「查出某公众号最近 7 天的图文阅读记录」「按 openid 字典序扫一段粉丝做分页」——需要**有序**，哈希表做不到高效范围扫描，搜索树或有序索引（如有序 map、B+ 树叶子链）更合适。
- **定时器堆 vs 有序索引。** 若你只关心「下一个到期事件」，堆足够；若还要「取消某个 mid 的定时」「查询某时间窗内有哪些任务」，仅靠堆会很痛苦（堆不支持按任意键高效查找），往往需要「堆 + 哈希」或「有序树」等组合。

所以本课的目标不是背「树是 O(log n)、堆是 O(log n)」，而是建立可验证的模型：**不变量是什么、高度怎么来的、堆序保证什么、不保证什么、什么时候该选哪一种。**

## 2. 从直觉到原理：三个核心要点

### 2.1 二叉搜索树不变量

#### 直觉：左边更小，右边更大

想象一棵「按数值分叉」的决策树：站在节点 `50` 上，所有比 50 小的数都在左边子树，所有比 50 大的数都在右边子树。找 `23` 时，一路往左/右走，路径上每个节点都在帮你缩小范围——这就是**用层次结构组织搜索路径**。

#### 原理：BST 不变量

对树中任意节点 `u`：

- `u` 的左子树中所有键 < `u.key`（有的实现用 ≤，但同一棵树必须统一）
- `u` 的右子树中所有键 > `u.key`
- 左右子树各自也是 BST

由此立刻得到：

1. **查找**：从根比较，小于走左、大于走右，路径长度 ≤ 树高。
2. **中序遍历**（左 → 根 → 右）得到**升序**序列——搜索树天然带有序性。
3. **范围查询**：可剪枝。例如查 [20, 40]，若当前节点 < 20，整棵左子树可跳过。

#### 短代码：手工验证不变量

```python
class Node:
    def __init__(self, key):
        self.key = key
        self.left = None
        self.right = None

def insert(root, key):
    if root is None:
        return Node(key)
    if key < root.key:
        root.left = insert(root.left, key)
    elif key > root.key:
        root.right = insert(root.right, key)
    # 相等：本示例忽略重复键
    return root

def inorder(root, out=None):
    if out is None:
        out = []
    if root:
        inorder(root.left, out)
        out.append(root.key)
        inorder(root.right, out)
    return out

def is_bst(root, lo=None, hi=None):
    # 用 (lo, hi) 开区间检查不变量
    if root is None:
        return True
    if lo is not None and not (lo < root.key):
        return False
    if hi is not None and not (root.key < hi):
        return False
    return is_bst(root.left, lo, root.key) and is_bst(root.right, root.key, hi)

if __name__ == "__main__":
    keys = [50, 30, 70, 20, 40, 60, 80]
    root = None
    for k in keys:
        root = insert(root, k)
    print("中序:", inorder(root))          # [20, 30, 40, 50, 60, 70, 80]
    print("是 BST?", is_bst(root))         # True
```

在公众号后台里，可以把「消息 CreateTime」或「粉丝 openid」想成键：BST（或更常见的平衡有序结构）让你既能点查，也能按时间窗扫一段。

#### 和哈希的对比（点一下，后文权衡再展开）

| 能力 | 哈希表 | 搜索树 |
|------|--------|--------|
| 单点查找 | 平均 O(1) | O(h)，平衡时 O(log n) |
| 有序遍历 | 不支持（或要全取出再排） | 中序即有序 |
| 范围查询 | 不适合 | 适合，可剪枝 |

### 2.2 平衡与树高

#### 直觉：同样多的节点，瘦高树更难找

8 个节点如果长成完美二叉树，高度大约 3，找一个键最多比 3 次。如果每次插入的都是递增键 `1,2,3,...,8`，普通 BST 会退化成右链，高度 7，最坏每次比 8 次。**节点数相同，树形不同，成本天差地别。**

#### 原理：复杂度跟着高度走

设树高为 h（根到最深叶的边数，或层数减一，约定一致即可）：

- 查找 / 插入 / 删除的路径长度都是 O(h)
- 若树「平衡」，h = Θ(log n)，操作为 O(log n)
- 若退化成链，h = Θ(n)，操作为 O(n)

**树退化为什么影响复杂度？** 因为算法并没有魔法：它必须沿着父子指针走，每走一步做一次比较。高度就是最多步数。退化 = 高度线性增长 = 每步操作的最坏成本线性增长。验收标准里的这句话，指的就是这个因果链。

#### 演示：有序插入导致退化

```python
def height(root):
    if root is None:
        return -1  # 空树高度 -1，单节点 0
    return 1 + max(height(root.left), height(root.right))

# 随机/打散插入：较矮
root_ok = None
for k in [50, 30, 70, 20, 40, 60, 80]:
    root_ok = insert(root_ok, k)
print("打散插入 高度:", height(root_ok), "中序:", inorder(root_ok))

# 严格递增：退化成链
root_bad = None
for k in range(1, 8):
    root_bad = insert(root_bad, k)
print("递增插入 高度:", height(root_bad), "中序:", inorder(root_bad))
```

预期：打散插入高度约 2；递增插入高度 6。中序都是有序的——**中序有序只说明不变量还在，不说明树是平衡的。**

#### 平衡树在说什么

「平衡」通常指用额外约束把高度压在 O(log n)，例如：

- AVL：左右子树高度差 ≤ 1
- 红黑树：着色约束 → 最长路径不超过最短路径的两倍量级
- B 树 / B+ 树：多路、适合磁盘与数据库有序索引

公众号后台里你不一定手写 AVL，但会用到：

- 语言库的有序 map（如 C++ `std::map`，通常红黑树）
- 数据库主键 / 二级索引（常见 B+ 树）
- 需要范围扫描的内存索引

**要点：需要稳定的 O(log n) 点查 + 有序/范围能力时，选平衡搜索树或有序索引；不要赌裸 BST 的插入顺序永远很随机。**

### 2.3 二叉堆与优先队列

#### 直觉：只关心「现在最要紧的那个」

定时器场景里，你不需要把所有任务按时间排成完整名单再展示；你只需要反复问：**下一个到期的是谁？** 堆就是为这种「局部优先」设计的：只保证堆顶是最优先，其余位置只要满足父子关系即可。

#### 原理：堆序 + 完全二叉树形状

**二叉堆**通常用数组表示一棵完全二叉树：

- 下标从 0 开始时：左孩子 `2i+1`，右孩子 `2i+2`，父节点 `(i-1)//2`
- **最小堆不变量**：任意节点值 ≤ 其子女（父比子更「优先」）
- 插入：放到末尾，向上 sift_up
- 删除堆顶：用末尾覆盖堆顶，再向下 sift_down
- 时间：插入 / 删堆顶 O(log n)；看堆顶 O(1)

**优先队列**是抽象数据类型：push / pop_min（或 pop_max）/ peek。二叉堆是最常见实现；C++ 的 `std::priority_queue`、Python 的 `heapq` 都是堆。

#### 关键：堆不是搜索树

| | 搜索树 | 二叉堆 |
|--|--------|--------|
| 不变量 | 全局左右有序（BST） | 仅父子局部优先 |
| 中序/数组顺序 | 中序有序 | **数组下标顺序 ≠ 排序顺序** |
| 找任意键 | O(h) | 通常要 O(n) 扫 |
| 范围查询 | 擅长 | 不擅长 |
| 反复取最小 | 可删最小，但常数与实现更重 | 本职工作 |

节点 summary 说得很准：**堆只维护局部优先关系，并不提供全局有序遍历。**

#### 短代码：最小堆核心

```python
class MinHeap:
    def __init__(self):
        self.a = []

    def peek(self):
        return self.a[0] if self.a else None

    def push(self, x):
        self.a.append(x)
        self._sift_up(len(self.a) - 1)

    def pop(self):
        if not self.a:
            return None
        if len(self.a) == 1:
            return self.a.pop()
        top = self.a[0]
        self.a[0] = self.a.pop()
        self._sift_down(0)
        return top

    def _sift_up(self, i):
        while i > 0:
            p = (i - 1) // 2
            if self.a[p] <= self.a[i]:
                break
            self.a[p], self.a[i] = self.a[i], self.a[p]
            i = p

    def _sift_down(self, i):
        n = len(self.a)
        while True:
            l, r = 2 * i + 1, 2 * i + 2
            best = i
            if l < n and self.a[l] < self.a[best]:
                best = l
            if r < n and self.a[r] < self.a[best]:
                best = r
            if best == i:
                break
            self.a[i], self.a[best] = self.a[best], self.a[i]
            i = best

h = MinHeap()
for x in [5, 3, 8, 1, 4]:
    h.push(x)
print("堆数组(非全序):", h.a)   # 例如 [1, 3, 8, 5, 4]，不是 [1,3,4,5,8]
print("依次 pop:", [h.pop() for _ in range(5)])  # [1,3,4,5,8]
```

注意：`h.a` 打印出来**不是**排序结果；只有不断 `pop` 才能得到有序序列（堆排序思想）。**不能把堆数组直接当作已排序数组**——这是本课核心误区。

#### 公众号后台例子

```text
任务 = (到期时间戳, 任务名)
push (1730000005, "群发A")
push (1730000001, "重试回调")
push (1730000003, "刷新token")
反复 pop → 先拿到 1730000001 的重试，再 token，再群发
```

这就是定时任务队列的骨架。

## 3. 常见误区与设计权衡

### 3.1 误区：把堆数组直接当作已排序数组

堆数组满足的是**堆序**，不是**全序**。常见错误：

1. 对 `heap.a` 做二分查找，假设单调——**错误**，中间可能忽大忽小。
2. 以为 `heap.a[1]` 是第二小——**错误**，第二小只保证在堆顶的子节点之中，不一定是下标 1。
3. 把堆数组直接返回给前端当「按时间排序的任务列表」——**错误**，应多次取出，或另维护有序结构。

反例：

```text
某次最小堆内部可能是：
       1
     /   \
    4     2
   / \
  5   7
数组: [1, 4, 2, 5, 7]
若当成已排序: 会以为 4 是第二小，其实第二小是 2。
```

### 3.2 其他常见误区

- **「有了堆就不需要搜索树」。** 堆解决优先取出；搜索树解决有序与范围。定时器取消指定 id、按时间窗统计，堆单独不够。
- **「BST 中序有序所以一定很快」。** 中序有序只说明不变量成立；退化链上中序仍有序，但每次插入已是 O(n)。
- **「平衡树和堆都是树，用法差不多」。** 形状都是树，**服务的访问模式不同**：一个为搜索路径，一个为局部优先。

### 3.3 设计权衡：有序查询 vs 哈希

节点 tradeoffs：**有序查询比哈希更适合范围扫描，但单点查找成本不同。**

| 场景 | 更合适 | 原因 |
|------|--------|------|
| openid → 会话，纯点查 | 哈希 | 平均 O(1)，不需要序 |
| 按 CreateTime 扫最近 7 天消息 | 有序索引 / 搜索树 | 范围扫描、有序推进 |
| 下一个到期定时器 | 最小堆 | 只要全局最小 |
| 既要按 id 取消、又要按时间触发 | 堆 + 哈希，或有序树 | 单一结构难兼顾 |

单点查找成本：

- 哈希：平均 O(1)，最坏退化风险（上一课）
- 平衡树：稳定 O(log n)
- 堆：找任意键 O(n)

**选择依据是查询与更新模式**（节点 summary），不是「哪种数据结构更高级」。

## 4. 动手实践

节点实践：**实现最小堆定时任务队列并验证插入删除顺序。**

下面把实践拆成可执行步骤。建议目录：`/workspace/atlas/practice10`。

### 步骤 1：建立实验目录

```bash
mkdir -p /workspace/atlas/practice10
cd /workspace/atlas/practice10
```

### 步骤 2：写入定时任务队列

将下列代码保存为 `timer_heap.py`：

```python
#!/usr/bin/env python3
# 最小堆定时任务队列：按到期时间取出。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


@dataclass(order=True)
class Task:
    # 比较时只看 due；name/payload 不参与比较。
    due: int
    name: str = field(compare=False)
    payload: Any = field(default=None, compare=False)


class TimerMinHeap:
    def __init__(self) -> None:
        self._a: List[Task] = []

    def __len__(self) -> int:
        return len(self._a)

    def peek(self) -> Optional[Task]:
        return self._a[0] if self._a else None

    def push(self, task: Task) -> None:
        self._a.append(task)
        self._sift_up(len(self._a) - 1)

    def pop(self) -> Optional[Task]:
        if not self._a:
            return None
        if len(self._a) == 1:
            return self._a.pop()
        top = self._a[0]
        self._a[0] = self._a.pop()
        self._sift_down(0)
        return top

    def as_array_names(self) -> List[Tuple[int, str]]:
        # 暴露内部数组，用于证明「不是已排序」。
        return [(t.due, t.name) for t in self._a]

    def _sift_up(self, i: int) -> None:
        a = self._a
        while i > 0:
            p = (i - 1) // 2
            if a[p] <= a[i]:
                break
            a[p], a[i] = a[i], a[p]
            i = p

    def _sift_down(self, i: int) -> None:
        a = self._a
        n = len(a)
        while True:
            l, r = 2 * i + 1, 2 * i + 2
            best = i
            if l < n and a[l] < a[best]:
                best = l
            if r < n and a[r] < a[best]:
                best = r
            if best == i:
                break
            a[i], a[best] = a[best], a[i]
            i = best


def demo() -> None:
    # 模拟公众号后台：插入顺序乱序，取出必须按 due 升序
    jobs = [
        Task(due=1730000005, name="群发图文A", payload={"appid": "wx1"}),
        Task(due=1730000001, name="回调重试", payload={"msgid": "m42"}),
        Task(due=1730000003, name="刷新token", payload={"appid": "wx1"}),
        Task(due=1730000002, name="延迟模板消息", payload={"openid": "oABC"}),
        Task(due=1730000004, name="清理过期会话", payload=None),
    ]

    heap = TimerMinHeap()
    print("=== 插入（乱序）===")
    for t in jobs:
        heap.push(t)
        print(f"  push due={t.due} {t.name}")
        print(f"    内部数组: {heap.as_array_names()}")

    print("\n=== 验证：内部数组通常不是按 due 全序 ===")
    arr = heap.as_array_names()
    dues = [d for d, _ in arr]
    print(f"  内部 dues: {dues}")
    print(f"  是否已排序: {dues == sorted(dues)}")

    print("\n=== 依次 pop（删除堆顶）===")
    got = []
    while len(heap):
        t = heap.pop()
        assert t is not None
        got.append(t.due)
        print(f"  pop due={t.due} {t.name}")

    expect = sorted(j.due for j in jobs)
    print("\n=== 校验删除顺序 ===")
    print(f"  得到: {got}")
    print(f"  期望: {expect}")
    assert got == expect, "pop 顺序必须按 due 升序"
    print("  OK: 插入乱序，删除（取出）按到期时间有序。")


if __name__ == "__main__":
    demo()
```

### 步骤 3：运行并记录结果

```bash
cd /workspace/atlas/practice10
python3 timer_heap.py
```

你应当看到：

1. 每次 `push` 后内部数组变化，堆顶始终是当前最小 `due`。
2. 「是否已排序」多为 `False`（除非碰巧）。
3. 五次 `pop` 的 `due` 严格为 `1730000001 … 1730000005`。
4. 最后打印 `OK: 插入乱序，删除（取出）按到期时间有序。`

### 步骤 4：对照标准库 heapq（可选）

```bash
python3 -c 'import heapq; h=[];
[(heapq.heappush(h,(d,n))) for d,n in [(5,"A"),(1,"重试"),(3,"token"),(2,"模板"),(4,"清理")]];
print("内部", h);
print("弹出", [heapq.heappop(h) for _ in range(5)])'
```

注意：`heapq` 文档同样不保证列表 `h` 全序，只保证 `h[0]` 最小。

### 步骤 5：对照有序结构的「范围」能力（加深区分）

```bash
python3 -c 'events=sorted([(1730000001,"重试"),(1730000002,"模板"),(1730000003,"token"),(1730000004,"清理"),(1730000005,"群发")]); lo,hi=1730000002,1730000004; print([e for e in events if lo<=e[0]<=hi])'
```

有序结构能直接回答「窗内有哪些」；堆只能反复取最小。这直接回扣验收：**堆擅长下一次触发；搜索树/有序索引擅长范围。**

### 步骤 6：验收自查

- [ ] 能说出 BST 不变量，并用中序/区间检查解释
- [ ] 能说明递增插入如何抬高树高、为何变成 O(n)
- [ ] 能说明堆数组为何不是已排序数组
- [ ] `timer_heap.py` 跑通，pop 顺序等于按 `due` 排序
- [ ] 能区分：定时器取下一任务用堆；按时间窗列消息用有序结构

## 5. 自测题（3 道，附答案）

### 题 1：堆和搜索树的查询能力差在哪里？

**题：** 微信公众号后台有两个需求：① 取出「下一个到期」的群发任务；② 列出 CreateTime 在 [t1, t2] 内的全部消息 id。只给一个二叉堆或一棵平衡搜索树，分别怎么选？为什么？

**答案：**

- ① 用**最小堆（优先队列）**：只需维护局部堆序，反复 `pop` 堆顶即可，插入/删除 O(log n)。
- ② 用**平衡搜索树 / 有序索引**：需要范围扫描与有序遍历；堆不支持高效范围查询，按键查找也是 O(n)。
- 若两者都要，通常组合结构（例如按时间的有序树，或堆 + 任务 id 哈希），而不是「一个堆打天下」。

### 题 2：树退化为什么影响复杂度？

**题：** 向空 BST 依次插入 `1..n`（严格递增）。查找键 `n` 的比较次数大约多少？若改用平衡树呢？请用「高度」解释。

**答案：**

- 严格递增插入使每个新节点都挂在最右孩子上，树退化成右链，高度 Θ(n)。
- 查找 `n` 要从根走到最深叶，约 n 次比较，O(n)。
- 平衡树通过旋转等操作把高度压在 Θ(log n)，查找为 O(log n)。
- **本质：** BST 操作成本由路径长度（高度）决定；退化抬高高度，直接抬高复杂度。

### 题 3：为什么不能把堆数组直接当已排序数组？

**题：** 最小堆内部数组为 `[1, 4, 2, 5, 7]`。有人说「已经差不多排好了，第二小就是 a[1]=4」。错在哪里？正确拿到升序的方式是什么？

**答案：**

- 堆只保证父 ≤ 子，不保证同层或下标序。第二小只可能在堆顶的孩子里，这里是 `4` 与 `2`，真正第二小是 `2`，不是 `a[1]`。
- 也不能对堆数组做二分。
- 正确方式：反复 `pop`（或复制后再堆排序），得到 `1,2,4,5,7`；若需要持久有序视图，应使用搜索树等有序结构。

## 6. 推荐资源

只使用节点给出的资源：

1. **搜索树、堆与有序索引：参考文档 / 原始资料**
   - URL：https://en.cppreference.com/w/cpp/container/priority_queue.html
   - 来源：en.cppreference.com · 英文 · 文档 · 基础 · 免费
   - 阅读建议：对照 `priority_queue` 的 `push` / `pop` / `top` 语义，理解「只暴露堆顶、不提供全局有序遍历」；并结合本课 BST 不变量、平衡与树高，核对本节点实验（定时任务最小堆）的边界——例如默认比较器、元素可比较性、以及「容器内部顺序不可当作已排序序列依赖」。

## 7. 小结与前后置关系

### 7.1 本课要点回顾

1. **BST 不变量**组织搜索路径；中序有序；范围查询可剪枝。
2. **平衡与树高**决定复杂度；退化成链 → O(n)；生产有序索引需平衡保证。
3. **二叉堆 / 优先队列**只维护局部优先，胜任定时器与优先级任务；**不是**已排序数组，也不替代搜索树的范围能力。
4. **权衡**：范围与有序看树/有序索引；纯点查看哈希；「下一个最优先」看堆。

### 7.2 验收回扣

- **区分堆与搜索树的查询能力：** 堆 = 反复取最优先；搜索树 = 点查 + 有序 + 范围。
- **树退化影响复杂度：** 高度变线性 ⇒ 路径变线性 ⇒ 操作变 O(n)。

### 7.3 前后置

- **前置：** `a-sequence`（顺序结构与基本复杂度直觉）。
- **本节点：** `a-tree`（搜索树、堆与有序索引）。
- **后续：** 在更高阶段会把这里的「有序索引 / 优先队列」接到真实存储、定时器实现与调度系统中；先把不变量与适用边界吃透。

### 7.4 一句话带走

**树管搜索路径与范围，堆管下一个最要紧的任务；数组长得像树不等于已经排好序，高度失控复杂度就失控。**
