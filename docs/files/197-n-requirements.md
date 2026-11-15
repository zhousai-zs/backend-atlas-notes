# 第 197/208 课：系统设计与需求澄清

> 序号 197/208 · id `n-requirements` · 领域 N · 阶段 3 · 难度 进阶 · 子类 架构决策
> 前置：[`i-metrics`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/128.html)
> 节点摘要：把业务目标转成负载、数据、安全和可靠性约束，再决定组件；设计首先服务于可验证需求。

## 导读

从本课起进入领域 N（架构与技术领导力）。设计不是先开一张组件清单再补理由。[SRE Workbook — Non-Abstract Large System Design](https://sre.google/workbook/non-abstract-design/) 要求从可计算的数量出发。本课为一套路侧事件查询服务写一页需求，并让设计里的每个组件引用需求编号。教学假设：数据量与热点是带验证计划的假设，不是已经测量过的全国路网。`is_live_model=false`。

## 学习目标

1. 说明功能与非功能需求及适用边界。
2. 独立完成实践：为路侧事件查询服务写一页需求，列出数据量、热点和可接受陈旧度。
3. 验收：每个组件有需求依据；未知量被标为假设并有验证计划。

## 为什么后端要懂

先决定「上 Redis 和 Kafka」再找理由，会把架构讨论变成偏好讨论。需求页把流量分布、陈旧度和安全约束写成可检查的条目，组件才能被质疑。

## 核心知识点

### 1. 先写可验证约束，再放组件

**直觉。** 组件是答案，需求是题目。

**原理。** 需求页包含业务目标、功能（F1 范围查询、F2 详情）、非功能（N1 数据量、N2 高峰读 QPS 800 且 60% 落在 12 个热点格子、N3 可接受陈旧度 30 秒、N4 可用性、N5 写入口身份与日志约束）。合格设计里，`hotspot_cache` 引用 N2 与 N3——缓存存在是因为热点和陈旧度，而不是因为「大家都用缓存」。校验器拒绝 `requires: []` 的组件。

对照设计 `bad-cache-first.yaml` 先放 Redis 和 Kafka，没有需求编号，校验结果为失败，问题包括 `component_redis_cache_has_no_requirement`。

**边界。** N1/N2 的具体数字是假设（A1、A3），不能写成已经盘过库。N4 的 99.9% 也标了与运营确认的验证计划（A2）。

### 2. 未知量必须带验证计划

**直觉。** 「大概很多」不是假设，是缺口。

**原理。** 每个 `assumptions[]` 都有 `unknown` 与 `validation_plan`，例如用一周样例统计回写 N1，用查询日志做 geohash 热力图后才确定缓存分区。没有计划的假设会被拒绝。允许迭代，但关键风险要先验证，而不是用组件把缺口盖住。

## 常见误区

- 先决定使用缓存和消息队列再寻找理由。
- 把假设写成已测量事实。
- 组件职责与需求对不上，或一个组件引用了不存在的编号。

## 设计权衡

需求不完全时允许迭代，但关键风险（数据量数量级、陈旧度、写路径身份）要先有验证计划。写太厚的需求文档会停住迭代；本课把它们压到一页可校验的条目。

## 动手实践

实践包在 `practice197/`。需要 cmvenv 中的 PyYAML 6.0.3。

```bash
tmp_dir=$(mktemp -d)
cp -a practice197 "$tmp_dir/"
cd "$tmp_dir/practice197"
bash test.sh
```

本次实测：带引用的设计通过；缓存先行的设计被拒绝；A1–A3 均有验证计划。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [Non-Abstract Large System Design](https://sre.google/workbook/non-abstract-design/)：HTTP 200，`Date` 2026-11-15 16:41:27 GMT（Asia/Shanghai 2026-11-16 00:41:27），`Last-Modified` 2026-10-08 04:48:00 GMT（Asia/Shanghai 2026-10-08 12:48:00）。该页把容量规划与组件隔离作为 NALSD 的一部分；本课先落需求页，容量计算在第 199 课继续。

## 自测题

**1. 为什么热点缓存必须引用 N2 和 N3？**

答：N2 给出热点分布，N3 给出 30 秒陈旧度。没有这两条，缓存只是一个先选中的组件。

**2. 怎样识别「先选组件再补理由」的设计？**

答：组件的 `requires` 为空，或理由写不出对应编号。本次 Redis/Kafka 先行设计被校验器拒绝。

**3. 数据量数字从哪里来？**

答：目前是假设 A1，必须有用样例统计回写 N1 的计划，不能当成已测量值。

## 推荐资源

- [Google SRE Workbook — Non-Abstract Large System Design](https://sre.google/workbook/non-abstract-design/)（英文，文档，进阶，免费）：从数量出发的设计。

## 本课小结

系统设计从可验证的需求和带计划的假设开始，组件必须能回答「它满足哪一条」。下一课是第 198 课 n-slo（SLI、SLO 与错误预算）。
