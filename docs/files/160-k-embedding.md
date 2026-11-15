# 第 160/208 课：Embedding 与相似度

> 序号 160/208 · id `k-embedding` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型能力与表示
> 前置：[`k-ml`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/157.html)
> 节点摘要：Embedding 将输入映射到向量空间；相似度取决于训练目标、归一化与领域，不代表事实相同。

## 导读

上一课按词元管理窗口。检索故障文档时，既可以用词法匹配精确符号，也可以用向量捕捉同义表述。两者都会出错：最近邻不等于正确答案，不同模型的向量也不能混在一个索引里。

教学假设：为 50 条教学故障文档建两个不兼容的本地向量模型，并与 BM25 对照。以下文档和向量算法均为教学设计，不代表腾讯或微信内部知识库。没有加载 SentenceTransformer 或任何远程嵌入接口，`is_live_model=false`，`used_sentence_transformers=false`。

## 学习目标

1. 解释余弦相似度及适用边界
2. 独立完成实践：给 50 条后台故障文档建立向量索引，与关键词检索对比。

## 为什么后端要懂

节点给出的理由是「为文档和代码构建语义检索。」同义说法可能避开关键词，精确主机名却可能被粗糙的哈希向量排错；上线检索要同时理解这两种失败。

## 核心知识点

### 1. 余弦相似度

**原理。** Sentence-Transformers 文档 *Semantic Textual Similarity* 的做法是：为文本生成嵌入，再计算相似度，分数最高的被认为语义最接近。页面示例对两个句子列表调用 `model.encode`，再做余弦相似度。余弦关注方向而非绝对长度，前提是参与比较的向量来自同一模型、同一维度，并且经过约定的归一化。

**直觉。** 「靠得近」只表示在该模型的训练目标下更相似，不等于事实相同，也不等于运维手册选对了。

**实践对照。** 教学模型把向量 L2 归一化后做点积。同义查询「the cache machine ran out of ram and killed the kv process」在 `hash32-v1` 下最近为 D01（0.7432）和 D02（0.6299）。这是本地哈希嵌入，不是页面示例中的 `all-MiniLM-L6-v2`。

### 2. 向量维度与版本

**原理。** 模型或版本一变，维度或坐标语义就可能变。混用不同模型的向量会得到无意义的距离，甚至无法对齐。更换模型后应重建或隔离索引。

**实践对照。** `hash32-v1` 为 32 维，`tri48-v2` 为 48 维。对二者做余弦时抛出 `dimension_or_version_mismatch`。结果将 `rebuild_or_isolate_after_model_change` 记为真，并没有尝试把两套向量写进同一张表。

### 3. 语义与词法检索

**原理。** 语义检索覆盖表述差异；词法检索更擅长精确符号和罕见标识符。工作手册式的结论是两者互补，而不是互相替代。

**实践对照。** 查询精确符号 `kv-node-7` 时，BM25 前两名是含该标识的 D02 与 D01；哈希向量的最近邻却是无关的 D49（0.6405），真正含该符号的 D02 只排第二。另一查询「writes started failing on the gateway」的最近邻是 D14（网关 DNS 失败，0.6455），预定相关文档 D03（日志盘写失败）排第二（0.6155）。最近邻因此不等于正确答案。

## 常见误区

1. **混用不同模型产生的向量。** 节点原句即此。本课在维度不一致时直接拒绝计算。
2. **把第一名当成唯一答案。** D14 比 D03 更近，只说明在该哈希空间里更近，不能自动采用 D14 作为处置手册。

## 设计权衡

节点原句是「语义召回覆盖表达差异，词法检索更擅长精确符号。」同义「ran out of ram」可以被向量和 BM25 都找到 D01/D02；精确主机名上 BM25 更稳，粗糙哈希会把无关文档排到第一。生产系统常做融合，本课只对照，不强行给出融合公式。

## 动手实践

实践目录为 `practice160/`，需要 Python 3 和 NumPy。本次为 CPython 3.13.5、NumPy 2.5.4。不要把结果说成已经调用 SBERT 或在线嵌入 API。

```bash
cd practice160
bash test.sh
cat results.json
```

成功最后一行为 `ALL_CHECKS_PASSED`。50 条文档固定在 `docs.json`。

两项验收是「模型更换后重建或隔离索引」以及「解释近邻不等同正确答案」。前者对应混用被拒绝；后者对应 D14 近于预定的 D03，以及 `kv-node-7` 的向量第一名并不是含该符号的文档。

## 源码与文档阅读

阅读 `embed.py`、`docs.json` 与 `results.json`。SBERT 文档示例使用 `SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")`。本环境未安装该库，也未下载该模型；教学哈希/三元语法只用来落实「同一空间才能比余弦、近邻未必正确」，不能当成该示例模型的复现。

2026-11-15 在本机核对节点 URL。

| 页面及标题 | 字节数 | Date（北京时间） | Last-Modified（北京时间） |
| --- | ---: | --- | --- |
| [Sentence-Transformers — Semantic Textual Similarity](https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html) | 166292 | 2026-11-15 22:58:55 | 2026-11-11 22:35:30 |

最终 URL 与节点所列相同，HTTP 200。节点的 `verifiedAt` 是 2026-10-26，独立于以上抓取时间。

## 自测题

1. **为什么不能把 32 维和 48 维向量放进同一个最近邻表？** 坐标语义和维度都不同，距离没有定义。本课混用会得到 `dimension_or_version_mismatch`。
2. **最近邻是 D14，是否说明 D03 不相关？** 不是。D03 仍在前几名；近邻排序不是标准答案。
3. **什么查询更适合关键词？** 精确符号和专有标识，如本课的 `kv-node-7`。

## 推荐资源

- [Sentence-Transformers — Semantic Textual Similarity](https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html)：用于核对余弦相似度与语义检索的基本做法，将结论落实到本节点实验。英文，基础，免费。

## 本课小结

余弦相似度只在同一模型、同一版本的向量空间里有意义；换模型要重建或隔离索引。语义检索能抓住同义表述，词法检索更稳于精确符号，最近邻不能自动当成正确答案。下一课是第 161 课 k-api（模型 API 与可靠客户端）。
