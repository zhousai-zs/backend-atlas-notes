# 第 164/208 课：RAG 与可追溯回答

> 序号 164/208 · id `k-rag` · 领域 K · 阶段 3 · 难度 进阶 · 子类 检索增强
> 前置：[`k-embedding`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/160.html)、[`k-structured`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/163.html)
> 节点摘要：RAG 在回答前检索外部证据，效果受文档解析、分块、召回和引用绑定共同影响。

## 导读

RAG 不是「先搜再让模型随便写」。后端要保证两件事：检索真的把能支撑结论的段落拿回来，以及回答里的每一处引用都能在已返回的原文中找到依据。检索不到，只能说「这批结果里没有」，不能宣布「事实不存在」。

## 学习目标

1. 解释 chunk 与元数据及适用边界。
2. 独立完成实践：导入 20 份技术文档，为回答附文档段落 ID，并评估无答案问题。

## 为什么后端要懂

引用一旦脱离原文，生成只会把错误写得更流畅。只调提示词、不看召回，是在给幻觉加封面。无答案问题若被说成「世界上没有这个电话号码」，会把「我没找到」偷换成「它不存在」。

## 核心知识点

### 1. 分块与元数据

**直觉。** 检索的单位是段落（chunk），不是整本手册。块太大，定位变粗；块太小，一句结论所需的上下文被切开。每块要有稳定 ID、所属文档、标签，以便引用和过滤。

**实践对照。** `corpus/docs.json` 收录 20 篇后台文档、40 个段落，ID 为 `D01-P01` 这种「文档-段落」形式，正文来自真实运维命题（kv OOM、cgroup 与 RLIMIT、ENOSPC、SIGTERM drain、E_CONN_RESET、canary、readiness 与 liveness、tiktoken、Structured Outputs、BM25 与 embedding、HNSW、prompt cache、MCP stdio、call_id、workflow 与 agent、vLLM 文档路径迁移、Evals 下线窗口等）。元数据带 `tags`，供第 165 课做过滤。本课的引用绑定发生在段落级，而不是文档级「大概看过这篇」。

**边界。** 分块策略本身不会在本课被搜索到最优；我们固定段落，把验收放在「引用是否支持结论」。

### 2. 召回：词法 + 语义，再交给绑定器

**原理。** `retrieve.py` 对同一批段落同时算 BM25 和官方 MiniLM（`all-MiniLM-L6-v2` 的 ONNX 导出，与第 160 课同一套权重与 mask 平均）。`driver.py` 把两组分数各自 z-score 后相加，取 top-5。这是混合召回，不是「哈希也算语义」。本机 8 条查询里，6 条可回答问题的 gold 段落都进入了 top-5；2 条不可回答问题没有可用 gold。

**为什么要混合。** 精确符号和同义改写失败模式不同。只调生成提示、忽略召回缺失，是本课官方误区：生成模型看不见的段落，再好的提示也引用不到。

**未在本机验证。** 没有使用 LangChain 生产检索器，也没有建 pgvector 索引。官方 Retrieval 页 2026-11-15 抓取自节点给出的 `https://docs.langchain.com/oss/python/langchain/retrieval`（Date 2026-11-15 23:12:33 Asia/Shanghai，Last-Modified 2026-11-15 03:54:55 Asia/Shanghai）；当日内容仍是 RAG 综述，不能把它写成已在本机跑通的 LangChain 流水线。

### 3. 引用必须支持结论；检索不到 ≠ 事实不存在

**原理。** `binder.py` 的 `span_supported` 检查：被引用段落的原文是否包含支撑结论的跨度。`check_answer` 拒绝两类错误：`cite_not_retrieved`（引用了根本没返回的 ID）和 `citation_does_not_support`（ID 返回了，但文字撑不住结论）。组答函数 `extractive_compose` 只从已命中段落抽取，并标明 `composer=extractive_not_llm`。它不是大模型，本课也不把它的通顺程度写成「RAG 生成质量」。

**反例（已注入并抓住）。**

- 把 DNS 故障段落 `D20-P01` 当作 kv-node-7 OOM 的出处 → `citation_does_not_support:D20-P01`；
- 引用正确的 `D01-P01` 但该段不在本轮检索结果里 → `cite_not_retrieved:D01-P01`；
- 对「上海 NOC 经理个人手机号」给出 `reason=fact_absent` → `not_retrieved_must_not_be_called_fact_absent`。正确拒答理由是 `not_retrieved`。

可回答问题必须是 `status=answered` 且 gold 在 top-k；不可回答问题必须是 `abstain` + `not_retrieved`。

## 常见误区

- 只调生成提示而忽略召回缺失。
- 引用一个返回了、但与结论无关的段落，当作「已追溯」。
- 把「这 20 篇里没有」写成「事实不存在」。
- 用哈希向量的近邻证明「语义 RAG 有效」。

## 设计权衡

较大分块保留上下文，较小分块提高定位精度。本课按自然段落切开，引用检查走跨度匹配，而不是再训练一个生成模型。抽取式组答可复验，但覆盖不了需要跨段改写的提问；那种能力必须在真实模型上评，本机未测。

## 动手实践

```bash
cd practice164
export MINILM_HOME=/path/to/all-MiniLM-L6-v2   # 可指向第 160 课已下载的缓存
bash test.sh
```

模型不要打进课程 tar。本机缓存：`/workspace/atlas/gen-batch-153-162-revision/models/all-MiniLM-L6-v2`。依赖与第 160 课相同：onnxruntime 1.32.0、tokenizers 0.23.3、numpy 2.5.4。

## 源码与文档阅读

`corpus/docs.json`；`retrieve.py` 的 BM25 与 top-k；`binder.py` 的跨度支持检查与抽取式组答；`queries.json` 的可回答 / 不可回答集；`minilm.py` / `fetch_model.py`。

## 自测题

**1. 为什么「引用了检索结果里的某段」仍可能不合格？**

答案：该段可能与结论无关。本课把讲 DNS 的 `D20-P01` 接到 kv-node-7 的结论上，检查器给出 `citation_does_not_support`。引用必须支持结论，而不是「出现在引用列表里」。

**2. 知识库没检索到经理手机号，能否回答「该号码不存在」？**

答案：不能。只能 abstain，理由是 `not_retrieved`。本课把 `fact_absent` 标成错误。

**3. 抽取式组答通过了，能否宣传「RAG 生成质量已验证」？**

答案：不能。组答被明确标为 `extractive_not_llm`。生成式 RAG 的质量属于 `not_verified_on_this_machine`。

## 推荐资源

| 资源 | 抓取时间 (Asia/Shanghai) | Last-Modified (Asia/Shanghai) | 说明 |
| --- | --- | --- | --- |
| [LangChain — Retrieval](https://docs.langchain.com/oss/python/langchain/retrieval) | 2026-11-15 23:12:33 | 2026-11-15 03:54:55 | 节点指定入口。当日页仍是 RAG 综述，本课未把 LangChain 检索器当作已跑通的验收。英文，进阶，免费。 |
| [pgvector README](https://github.com/pgvector/pgvector) | 2026-11-15（同批抓取） | — | 默认精确近邻（完全召回）；HNSW 是近似，加索引后结果可能变化。本课未部署 Postgres。 |

## 本课小结

RAG 的可追溯性是一条链：分块有 ID，召回把 gold 带回来，引用必须能在已返回原文中找到支持跨度，检索失败不得改写成「事实不存在」。本课用 MiniLM + BM25 混合召回和抽取式绑定器验证了这条链；生成式 RAG 与 LangChain / pgvector 生产路径需在你的环境复现。
