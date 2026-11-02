# 第 11/208 课：图、依赖与拓扑排序

> 节点：`a-graph` · 域：A · 子类：数据结构与算法 · 难度：基础 · 前置：`a-sequence`
>
> 一句话：图描述实体之间的关系，有向无环图可以通过拓扑序安排满足前置条件的执行顺序。

## 1. 学习目标与为什么后端要懂

### 1.1 学习目标

完成本课后，你应该能够：

1. **解释邻接表与邻接矩阵**：两种把「点 + 边」落到内存里的方式；说清空间与查边成本的差异。
2. **用 BFS / DFS + 访问标记遍历图**：知道队列与栈（或递归）各自在扫图时的角色，以及「未访问 / 访问中 / 已完成」标记为什么重要。
3. **用入度做拓扑排序，并检出环**：在有向图上输出至少一个合法学习/执行顺序；若存在自环或多节点环，能指出环上节点。
4. **独立完成实践**：实现课程依赖检查——输入若干「A 依赖 B」关系，输出环中的节点（若有）以及合法学习顺序（若无环）。
5. 在微信公众号后台场景里，能把「构建依赖、任务调度、学习路径校验」映射成 DAG 上的拓扑序问题。

### 1.2 验收标准

节点给出的验收标准有两条，本课会反复回扣：

- **能检测自环和多节点环。** 自环：`u → u`，入度算法里通常表现为「永远无法把该点的入度减到可调度」。多节点环：`A → B → C → A`，Kahn 算法结束后仍有入度 > 0 的点；DFS 三色法会在「访问中」再次碰到灰点。验收时两种环都要报得出来。
- **能解释多个合法拓扑序。** DAG 的拓扑序往往不唯一：没有相互依赖的节点可以交换顺序。例如「编译 protobuf」和「拉配置中心」互不依赖时，谁先谁后都合法。能说明「为什么两个序都对」比背一个固定序列更重要。

### 1.3 为什么微信公众号后台要懂图与拓扑

节点对「为什么」写得很干脆：**实现构建依赖、任务调度和本网站的学习依赖校验。** 放到即将入职的腾讯微信公众号后台，几乎处处是「有向依赖」：

- **素材 / 模板消息依赖。** 群发图文要先上传封面、再组稿、再审核、再投递；模板消息要先有模板 id、再填字段、再调发送接口。边的方向是「必须先完成谁」。
- **定时推送流水线。** 选粉丝包 → 渲染文案 → 风控校验 → 投递队列 → 回执汇总。某一步失败不能假装后面已经成功；调度器要按依赖唤醒下游。
- **服务启动顺序。** 配置中心、DB 连接池、access_token 刷新协程、回调 HTTP 服务——谁依赖谁，启动脚本或编排系统就是在跑拓扑序；若配成环，进程会卡死或循环重试。
- **本 Atlas 学习路径。** 站点本身用节点的 `prerequisites` 做依赖校验：你不能在没学 `a-sequence` 的情况下「合法地」宣称已掌握本课的前置。这正是「课程依赖检查」实验的现实版。

所以本课要建立的模型是：**点 = 任务/模块/课程，有向边 = 前置约束；无环才能排期，有环必须先拆掉。**

## 2. 从直觉到原理：三个核心要点

### 2.1 邻接表与邻接矩阵

#### 直觉：通讯录两种写法

把「公众号后台里谁依赖谁」记在本子上，可以有两种记法：

1. **邻接表**：给每个模块开一页，只列出它直接依赖的下游（或上游）。大多数模块只依赖少数几个——本子很薄。
2. **邻接矩阵**：画一张 N×N 表格，行列都是模块，格子里 0/1 表示有没有边。查「A 是否直接依赖 B」一眼看到，但空格子很多。

#### 原理

设有向图 G = (V, E)，|V| = n，|E| = m。

| 表示 | 空间 | 枚举 u 的出边 | 判断边 u→v 是否存在 |
|------|------|---------------|---------------------|
| 邻接表 `dict[u] → list of v` | O(n + m) | O(出度(u)) | O(出度(u))（若出边再建成 set 可 O(1)） |
| 邻接矩阵 `mat[u][v]` | O(n²) | O(n) | O(1) |

**稀疏图**（m ≪ n²）——微服务依赖、课程前置、构建 DAG——几乎总是邻接表。**稠密图**（边接近 n²）矩阵才可能划算，后端业务图很少这么稠。

#### 短代码：两种表示互转

```python
def edges_to_adj_list(nodes, edges):
    """edges: list of (u, v) 表示 u → v（u 完成后才能做 v，或 u 依赖 v，约定要统一）"""
    adj = {u: [] for u in nodes}
    for u, v in edges:
        adj[u].append(v)
    return adj

def adj_list_to_matrix(nodes, adj):
    idx = {u: i for i, u in enumerate(nodes)}
    n = len(nodes)
    mat = [[0] * n for _ in range(n)]
    for u, vs in adj.items():
        for v in vs:
            mat[idx[u]][idx[v]] = 1
    return mat

if __name__ == "__main__":
    nodes = ["配置", "DB", "Token", "HTTP"]
    # 边：前置 → 后继（做完左边才能做右边）
    edges = [("配置", "DB"), ("配置", "Token"), ("DB", "HTTP"), ("Token", "HTTP")]
    adj = edges_to_adj_list(nodes, edges)
    mat = adj_list_to_matrix(nodes, adj)
    print("邻接表:", adj)
    print("矩阵行=源, 列=目标:")
    for row in mat:
        print(row)
```

约定很重要：本课统一 **边 `u → v` 表示「u 是 v 的前置，必须先做 u 再做 v」**（构建系统常见写法）。有的系统写成「v 依赖 u」的反向边——读代码时先对一下方向，否则拓扑序会反。

### 2.2 BFS、DFS 与访问标记

#### 直觉：扫楼两种走法

- **BFS（广度优先）**：像按层通知依赖你的服务——先处理「距离起点 1 跳」的，再 2 跳。队列实现。
- **DFS（深度优先）**：沿着一条依赖链钻到底，再回溯。栈或递归实现。

无论哪种，都需要**访问标记**，否则有向图里的分叉、汇合、环会让你重复处理甚至死循环。

#### 原理：三色标记（对环检测尤其有用）

1. **白 / 0**：未访问  
2. **灰 / 1**：正在栈上（递归路径上）  
3. **黑 / 2**：已完全处理完  

DFS 时：若走到**灰点**，说明当前路径构成有向环。这是有向图环检测的经典手法；后面拓扑的 DFS 版会用到。

BFS 更常配合「入度」做 Kahn 拓扑（下一节），本身不直接用三色，但同样要标记「已入队」，避免重复入队。

#### 短代码：DFS 三色检有向环

```python
def has_directed_cycle(nodes, adj):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {u: WHITE for u in nodes}

    def dfs(u):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color[v] == GRAY:
                return True  # 回到路径上的点 → 有向环
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(color[u] == WHITE and dfs(u) for u in nodes)

if __name__ == "__main__":
    nodes = ["A", "B", "C"]
    ok = {"A": ["B"], "B": ["C"], "C": []}
    bad = {"A": ["B"], "B": ["C"], "C": ["A"]}
    self_loop = {"A": ["A"], "B": []}
    print("无环?", not has_directed_cycle(nodes, ok))
    print("三角环?", has_directed_cycle(nodes, bad))
    print("自环?", has_directed_cycle(["A", "B"], self_loop))
```

**误区预告（第 3 节展开）：** 无向图里「回到父节点」不是环，环检测规则不同；不要把无向图的 visited 写法直接套到有向依赖图上。

### 2.3 入度、拓扑序与环检测

#### 直觉：谁已经没有未完成的前置了？

想象流水线看板：每个任务卡片上写着「还差几个前置没做完」（入度）。入度为 0 的可以开工；做完后，通知所有后继「你们的前置又少了一个」。一直如此，直到全部做完——这就是 **Kahn 算法**，输出的顺序就是**拓扑序**。

若最后还剩卡片入度 > 0，说明它们互相卡死——**图里有环**（含自环：自己入度减不掉）。

#### 原理

1. 计算每个点入度 `indeg[v] = 有多少条边指向 v`。  
2. 把所有 `indeg == 0` 的点放入队列。  
3. 取出 u，追加到序里；对每条 `u → v`，`indeg[v] -= 1`，若变为 0 则入队。  
4. 若最终序的长度 < n，则存在环；环上节点 ⊆ 那些最终 `indeg > 0` 的点（精确还原环边需要再 DFS，实践里先报出这些点通常足够排查）。

**多个合法拓扑序：** 队列里同时有多个入度为 0 的点时，取出顺序任意——每一个都对应一种合法排期。验收要求你能解释这一点，而不是死记某一次 BFS 的偶然顺序。

#### 短代码：Kahn 拓扑 + 环上残留点

```python
from collections import deque

def topo_sort(nodes, edges):
    """edges: (pre, nxt) 表示 pre → nxt"""
    adj = {u: [] for u in nodes}
    indeg = {u: 0 for u in nodes}
    for u in nodes:
        adj.setdefault(u, [])
        indeg.setdefault(u, 0)
    for pre, nxt in edges:
        adj[pre].append(nxt)
        indeg[nxt] += 1

    q = deque([u for u in nodes if indeg[u] == 0])
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)

    if len(order) == len(nodes):
        return order, []  # 合法序，无环
    stuck = [u for u in nodes if indeg[u] > 0]
    return None, stuck

if __name__ == "__main__":
    nodes = ["序列", "哈希", "树", "图"]
    edges = [("序列", "哈希"), ("序列", "树"), ("序列", "图"), ("哈希", "图")]
    order, stuck = topo_sort(nodes, edges)
    print("合法序示例:", order)  # 例如 序列,哈希,树,图 或 序列,树,哈希,图
    print("环残留:", stuck)

    # 自环
    print("自环:", topo_sort(["X"], [("X", "X")]))
    # 多节点环
    print("三角环:", topo_sort(["A", "B", "C"], [("A", "B"), ("B", "C"), ("C", "A")]))
```

跑第一组时多跑几次、或把队列改成栈、或打乱初始入度为 0 的入队顺序，你会看到**不同但都合法**的拓扑序——回扣验收第二条。

## 3. 常见误区与设计权衡

### 3.1 误区：有向图与无向图的环检测方式不同

节点明确指出：**有向图与无向图的环检测方式不同。**

| | 有向图 | 无向图 |
|--|--------|--------|
| 环的含义 | 沿箭头走一圈回到起点 | 不考虑方向的闭合路径 |
| 常见检法 | DFS 三色（遇灰点）；或 Kahn 后仍有入度 > 0 | DFS/BFS 时「访问到已访问且非父节点」 |
| 自环 | `u → u` 直接是环 | 通常也视为环（实现时单独判断） |
| 双向边 | `A→B` 与 `B→A` 构成两点有向环 | 无向里 `A—B` 只是一条边，不是环 |

公众号后台的依赖、构建、启动顺序几乎都是**有向**的。用无向图教材里的「排除父节点」逻辑去检有向环，会漏掉「A→B、B→A」或误判。

### 3.2 权衡：稀疏图用邻接表，稠密图才考虑矩阵

节点权衡原文：**稀疏图用邻接表节省空间，稠密图可能适合矩阵。**

落到工程：

- **课程依赖 / 服务依赖 / Make 构建图**：n 可到几百上千，但每个点出度往往个位数 → **邻接表 + Kahn/DFS** 是默认选择。  
- **邻接矩阵**适合需要极高频「任意两点是否有边」、且 n 不大（例如几十）的场景；n = 5000 时矩阵就是 2500 万格，内存与缓存都不友好。  
- 若既要枚举出边又要 O(1) 判边：邻接表的出边用 `set`，或「表 + 哈希边集」混合，通常仍优于整张矩阵。

## 4. 动手实践：课程依赖检查

目标（节点 practice）：**实现课程依赖检查，输出环中的节点和合法学习顺序。**

### 步骤 1：准备目录

```bash
mkdir -p /workspace/atlas/practice11
cd /workspace/atlas/practice11
```

### 步骤 2：写入完整脚本

将下面保存为 `course_deps.py`：

```python
#!/usr/bin/env python3
"""课程依赖检查：Kahn 拓扑序 + 自环/多节点环残留点。"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Tuple, Optional


def check_course_deps(
    courses: List[str],
    prereq_edges: List[Tuple[str, str]],
) -> Tuple[Optional[List[str]], List[str]]:
    """
    prereq_edges: (pre, course) 表示必须先学 pre，再学 course（边 pre → course）。
    返回 (合法拓扑序或 None, 环相关节点列表)。
    """
    nodes = list(dict.fromkeys(courses))  # 保序去重
    adj: Dict[str, List[str]] = {u: [] for u in nodes}
    indeg: Dict[str, int] = {u: 0 for u in nodes}

    for pre, course in prereq_edges:
        if pre not in adj:
            adj[pre] = []
            indeg.setdefault(pre, 0)
            nodes.append(pre)
        if course not in adj:
            adj[course] = []
            indeg.setdefault(course, 0)
            nodes.append(course)
        adj[pre].append(course)
        indeg[course] += 1

    # 自环：边 pre → pre
    self_loops = [u for u, vs in adj.items() for v in vs if v == u]

    q = deque(sorted([u for u in indeg if indeg[u] == 0]))  # sorted 让演示序稳定
    order: List[str] = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in adj[u]:
            if v == u:
                continue  # 自环无法通过减入度消除
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)

    if self_loops or len(order) != len(indeg):
        stuck = sorted(set(self_loops) | {u for u, d in indeg.items() if d > 0})
        return None, stuck
    return order, []


def demo() -> None:
    courses = ["a-sequence", "a-hash", "a-tree", "a-graph", "a-shell"]
    # 合法 DAG：多个合法序（hash 与 tree 可互换）
    ok_edges = [
        ("a-sequence", "a-hash"),
        ("a-sequence", "a-tree"),
        ("a-sequence", "a-graph"),
        ("a-hash", "a-graph"),
        ("a-shell", "a-sequence"),
    ]
    order, stuck = check_course_deps(courses, ok_edges)
    print("=== 用例1：无环 ===")
    print("  合法学习顺序:", order)
    print("  环节点:", stuck)
    assert stuck == [] and order is not None
    assert order.index("a-shell") < order.index("a-sequence")
    assert order.index("a-sequence") < order.index("a-graph")
    assert order.index("a-hash") < order.index("a-graph")
    print("  OK: 依赖约束均满足；hash/tree 相对顺序可因实现而变（皆合法）。")

    print("\n=== 用例2：自环 ===")
    order2, stuck2 = check_course_deps(["X", "Y"], [("X", "X"), ("Y", "X")])
    print("  序:", order2, "环节点:", stuck2)
    assert order2 is None and "X" in stuck2
    print("  OK: 检出自环 X。")

    print("\n=== 用例3：多节点环 A→B→C→A ===")
    order3, stuck3 = check_course_deps(
        ["A", "B", "C", "D"],
        [("A", "B"), ("B", "C"), ("C", "A"), ("D", "A")],
    )
    print("  序:", order3, "环节点:", stuck3)
    assert order3 is None
    assert set(stuck3) >= {"A", "B", "C"}
    print("  OK: 检出多节点环；D 指向环，通常也会留在残留集或仅环上三点（本实现含入度未清零者）。")

    print("\n全部用例通过。")


if __name__ == "__main__":
    demo()
```

### 步骤 3：运行

```bash
cd /workspace/atlas/practice11
python3 course_deps.py
```

期望要点：

1. 用例1 打印一条完整学习顺序，`a-shell` 在 `a-sequence` 前，`a-sequence`/`a-hash` 在 `a-graph` 前；环节点为空。  
2. 用例2 序为 `None`，环节点含 `X`。  
3. 用例3 序为 `None`，环节点至少含 `A,B,C`。  
4. 最后一行 `全部用例通过。`

### 步骤 4：亲手解释「多个合法拓扑序」

在用例1 上，把 `q = deque(sorted(...))` 改成 `deque(reversed(sorted(...)))` 再跑一次，对比两次 `order` 中 `a-hash` 与 `a-tree` 的先后——两者都合法，因为它们之间没有边。**这就是验收要求的「能解释多个合法拓扑序」。**

### 步骤 5：验收自查

- [ ] 能画出邻接表，并说明相对矩阵省在哪里  
- [ ] 能说明有向环用三色或 Kahn 残留检测，且与无向图不同  
- [ ] 自环、三角环都能报出节点  
- [ ] 能举出至少两个都合法的拓扑序并说明为何都合法  
- [ ] `course_deps.py` 三个用例全绿  

## 5. 自测题（3 道，附答案）

### 题 1：自环和多节点环怎么被 Kahn「抓到」？

**题：** 依赖边为 `Token → Token`（配置写错），以及另一处 `审核 → 投递 → 回执 → 审核`。用入度算法时，队列和最终残留分别会怎样？

**答案：**

- 自环：`Token` 的入度至少为 1，且减入度时若跳过/无法消除指向自己的边，它永远不会变成可调度的 0（或显式列入自环集）。最终不在完整拓扑序里，出现在残留/环节点中。  
- 三角（多节点）环：算法开始时若环外没有入边为 0 的突破口，环上三点入度都 ≥ 1；相互减度也减不完。结束后 `order` 长度 < n，残留含环上节点。  
- 两种都导致「无法输出覆盖全部点的拓扑序」——回扣验收：**能检测自环和多节点环**。

### 题 2：为什么会有多个合法拓扑序？

**题：** 课程边只有 `序列 → 哈希`、`序列 → 树`、`序列 → 图`、`哈希 → 图`。写出两个不同的合法学习顺序，并说明为什么都对。

**答案（示例）：**

- 序1：`序列 → 哈希 → 树 → 图`  
- 序2：`序列 → 树 → 哈希 → 图`  
- `哈希` 与 `树` 之间没有依赖边，入度同时可为 0（在 `序列` 学完后），谁先学都可以；只要保证有向边的前置在后继之前即可。  
- 回扣验收：**能解释多个合法拓扑序**。

### 题 3：邻接表还是矩阵？再加一道有向/无向陷阱

**题：** 微信公众号后台有 200 个微服务，平均每个只依赖 3 个其它服务。应选邻接表还是矩阵？若有人用「无向图排除父节点」的方法检环，会漏掉哪种致命配置？

**答案：**

- 选**邻接表**：m ≈ 600，n = 200，远稀疏于 n²；矩阵浪费空间，枚举出边还要扫整行。  
- 无向检环法会漏掉或误处理**有向两点互依赖**（`A→B` 且 `B→A`，例如错误的循环 RPC 依赖）以及依赖方向语义；后台依赖必须按**有向**规则（三色或 Kahn）检测。

## 6. 推荐资源

只使用节点给出的资源：

1. **图、依赖与拓扑排序：参考文档 / 原始资料**
   - URL：https://ocw.mit.edu/courses/6-006-introduction-to-algorithms-fall-2011/pages/lecture-notes/
   - 来源：ocw.mit.edu · 英文 · 文档 · 基础 · 免费
   - 阅读建议：重点对照邻接表与邻接矩阵的空间/时间，以及 BFS、DFS 与访问标记；用讲义里的定义核对本课实验（课程依赖检查）的边方向、环与拓扑序边界。

## 7. 小结与前后置关系

### 7.1 本课要点回顾

1. **图**刻画实体关系；后端里最常见的是**有向依赖图**。  
2. **邻接表**适合稀疏依赖；**矩阵**偏稠密或重「判边」。  
3. **BFS/DFS + 标记**是遍历与检环的基础；有向环用三色或 Kahn 残留。  
4. **入度 + 拓扑序**给出满足前置的执行/学习顺序；有环则无法排完。  
5. **误区与权衡**记住两句：有向/无向检环不同；稀疏用表、稠密才考虑矩阵。

### 7.2 验收回扣

- **自环与多节点环：** 实践用例 2、3 与自测题 1。  
- **多个合法拓扑序：** 无边约束的节点可交换；实践步骤 4 与自测题 2。

### 7.3 前后置

- **前置：** `a-sequence`（顺序与基本结构直觉）。  
- **本节点：** `a-graph`（图、依赖与拓扑排序）。  
- **后续：** 调度系统、构建图、服务编排都会反复用到 DAG 与拓扑；先把表示法、遍历标记和环检测练熟。

### 7.4 一句话带走

**依赖是有向边，排期是拓扑序；图里有环就别假装能排期，稀疏依赖请优先邻接表。**
