# 第 201/208 课：架构演进与迁移策略

> 序号 201/208 · id `n-evolution` · 领域 N · 阶段 5 · 难度 专家 · 子类 架构决策
> 前置：[`n-adr`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/200.html)、[`h-evolution`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/127.html)、[`j-release`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/155.html)
> 节点摘要：演进采用兼容接口、逐步流量切换和可观测验证降低风险，迁移本身是一套运行中的系统。

## 导读

第 200 课留下决策记录，第 127、155 课讨论演进与发布。本课把内存 KV 迁到 RocksDB：先双写双读，再切换，并分清应用回滚与数据回滚。教学假设：客户端协议不变；RocksDB 通过 `rocksdict` 0.3.29（嵌入 RocksDB 的 Python 绑定，manylinux wheel）访问，而不是直接调用 Facebook 的 C++ API。开工前已盘点：系统里没有 `librocksdb`，cmvenv 可安装并成功 `put/get`。`is_live_model=false`。

节点推荐阅读是 [Configuration Design](https://sre.google/workbook/configuration-design/)。该页讨论配置即设计，并不单独讲授 strangler；本课把兼容接口、双轨与开关当作迁移的配置面，并写明这一差异。

## 学习目标

1. 说明 strangler / expand-contract 及适用边界。
2. 独立完成实践：把 KV 内存后端迁到 RocksDB，保留双读比对和可控切换。
3. 验收：迁移中旧客户端仍可用；明确数据回滚与应用回滚的边界。

## 为什么后端要懂

切核心存储时，若立刻删除旧路径，回滚就只剩「祈祷备份可用」。双轨运行更贵，但给对账和回滚留了时间。

## 核心知识点

### 1. 先扩大，再切换，旧协议保持不动

**直觉。** 迁移是给运行中的系统再加一条路，而不是停机换引擎。

**原理。** 客户端始终调用 `get/set/exists`。模式为 `memory → dual → rocks`。进入 dual 时先回填已有键，再双写；读以内存为准，同时对比 RocksDB，不一致就记入 `mismatches`。实践中故意把 rocks 里的 `b` 写成 `WRONG`，比对抓到差异，修复后再切到 `rocks`。切换后 `get/set` 仍可用，新写入的 `d=4` 出现在 RocksDB。这是 expand/contract 的「先扩大表面、再收缩主路径」。

**边界。** 没有接入真实的线上影子流量。rocksdict 是绑定而不是直接的 C++ API，课文与 `results.json` 都写明了版本。

### 2. 应用回滚和数据回滚不是一回事

**直觉。** 把开关拨回去，并不自动把新引擎上才有的数据带回旧引擎。

**原理。** `rollback_app()` 只把 mode 拨回 `memory`，RocksDB 文件保留。此时内存里没有切换后才写入的 `d`，但 rocks 中仍有 `d=4`。要把数据也退回去（或追上来），必须显式从 rocks 拷回 memory。相反，若只想撤销应用切换、同时保留新数据在新引擎中，就不要做那次拷贝。实践证明两条路径都在，而且切换后没有删除旧后端或模式开关——这是「立刻删除旧路径」的反例。

## 常见误区

- 上线新架构后立刻删除旧路径和回滚证据。
- 以为拨回应用开关等于数据也回到切之前的状态。
- 双读不对账，切过去才发现系统性差异。
- 未盘点绑定或库，就宣称本机无法使用 RocksDB。

## 设计权衡

双轨运行增加存储和对账成本，但为正确性验证和回滚争取时间。收缩旧路径要等对账干净，并且回滚步骤仍然可执行。

## 动手实践

实践包在 `practice201/`。需要 cmvenv 中的 `rocksdict==0.3.29`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice201 "$tmp_dir/"
cd "$tmp_dir/practice201"
bash test.sh
```

本次实测：双读能抓住故意写入的不一致；切换后旧客户端仍可用；应用回滚保留 rocks 中的新键；显式数据拷贝后内存也能读到该键。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Configuration Design](https://sre.google/workbook/configuration-design/)：HTTP 200，`Date` 2026-11-15 16:41:28 GMT（Asia/Shanghai 2026-11-16 00:41:28），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。该文讲配置设计；strangler 与双轨是本课的实践结构，不是该页的原标题。

## 自测题

**1. 为什么切换到 rocks 之后，旧客户端不用改？**

答：协议仍是 get/set/exists，变的是 mode。本次切换后读旧键、写新键都成功。

**2. 应用回滚后，为什么 `d` 会在内存里消失？**

答：`d` 只在切到 rocks 之后写入。应用回滚只改开关，不自动搬数据。这正是数据回滚要单独做的原因。

**3. 双读比对失败说明什么？**

答：两条路径已经分叉，不能切流量。本次故意写错 rocks 后比对命中，修复后再切。

## 推荐资源

- [Google SRE Workbook — Configuration Design](https://sre.google/workbook/configuration-design/)（英文，文档，专家，免费）：把开关和配置当作设计问题。
- rocksdict 0.3.29：本次实际使用的 RocksDB Python 绑定。

## 本课小结

迁移要保持旧客户端可用，先对账再切换，并分清应用回滚与数据回滚。下一课是第 202 课 n-incident（事故响应与复盘）。
