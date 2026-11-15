# 第 160/208 课：Embedding 与相似度

> 序号 160/208 · id `k-embedding` · 领域 K · 阶段 2 · 难度 基础 · 子类 模型接口与输出
> 前置：[`k-tokens`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/159.html)
> 节点摘要：向量近邻和关键词检索解决的问题不同；换模型必须重建或隔离索引。

## 导读

「语义相似」不能靠哈希撞桶来证明。哈希向量可以用来讲余弦公式和「维度不一致就不能混」，但它不是 Sentence Transformers，也测不出同义句。本课修订后，语义检索用的是官方 `sentence-transformers/all-MiniLM-L6-v2` 的 ONNX 权重，在本机用 onnxruntime 1.32.0 跑完 50 篇教学文档；词法检索用 BM25；哈希只作为对照，明确 `is_semantic_model=false`。

教学假设：50 条后台故障短文档。`is_live_model=false` 表示没有调用托管大模型或付费 embedding API。本地 MiniLM 是真实的句子向量模型。未使用 `sentence_transformers` 这个 Python 包（当日 PyPI 当前为 6.3.0，完整链路通常还要 PyTorch；本机为控制安装体积，改用模型仓库自带的 `onnx/model_quint8_avx2.onnx`，sha256 `b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21`，2026-11-15 自 Hugging Face 官方模型页下载）。

## 学习目标

1. 解释余弦相似度及适用边界
2. 独立完成实践：给 50 条后台故障文档建立向量索引，与关键词检索对比。

## 为什么后端要懂

检索错了，后面的生成只会一本正经地引用错证据。换了向量模型却继续查旧索引，分数没有意义。精确符号（如 `kv-node-7`）和同义改写，往往要两种检索对照，而不是只信一种。

## 核心知识点

### 1. 余弦相似度

**直觉。** 把一段文本变成向量以后，「方向是否接近」比「绝对长度」更常用来表示相似。若向量已经做了 L2 归一化，内积就等于余弦。

**原理。** 2026-11-15 抓取官方页 *Semantic Textual Similarity*（`https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html`，Date 2026-11-15 22:58:55 CST，Last-Modified 2026-11-11 22:35:30 CST，166292 字节）。文档展示用 SentenceTransformer 编码后比较余弦。本课的 MiniLM 路径与之对应，只是运行时换成 ONNX：

1. 用官方 `tokenizer.json` 得到 `input_ids` / `attention_mask` / `token_type_ids`（必须用 mask，不能把 `[PAD]` 当真实 token 做平均）；
2. ONNX 输出 `last_hidden_state`，形状 `[batch, seq, 384]`；
3. 按 `1_Pooling/config.json`：`pooling_mode_mean_tokens=true`（对 mask 为 1 的位置做平均）；
4. 按 `modules.json` 的 `Normalize` 做 L2 归一化。

注意：该导出自带的 tokenizer 把序列 pad/truncate 到 128，而同目录 `sentence_bert_config.json` 写的是 `max_seq_length: 256`。本课遵循**实际参与计算的导出**（128），并在结果里记录，避免把未使用的 256 写成已验证。

本机对同义查询 *the cache machine ran out of ram and killed the kv process*，MiniLM 最近邻是 D01（0.8078），第二是 D02（0.7634）。这是语义模型的结果。

### 2. 向量维度与版本

**直觉。** 32 维哈希和 48 维哈希、384 维 MiniLM 不在同一个空间。把不同模型的向量塞进同一张表，近邻没有定义。

**实践对照。** `embed.py` 里 `hash32-v1` 与 `tri48-v2` 做余弦时抛出 `dimension_or_version_mismatch`。换模型必须重建或隔离索引：`rebuild_or_isolate_after_model_change=true` 指的是这条工程规则，本课用维度冲突作为最小可运行证明。不要在 MiniLM 的 384 维索引里混入哈希向量。

### 3. 语义与词法检索

**直觉。** 语义模型擅长同义改写；关键词（BM25）擅长精确符号。哈希向量两者都不应当被当作证据。

**实践对照。**

- 同义句：MiniLM 在 top-3 命中 D01/D02（本机第 1）；BM25 在这个短语上也命中了 D01（词有重叠），但这不能反过来证明哈希是语义模型。
- 精确符号 `kv-node-7`：BM25 第 1、第 2 就是含该符号的 D02/D01。MiniLM 这次也找到了它们。**哈希**的第 1 名却是完全无关的 D49（「order service ignored a canceled request...」），分数 0.6405。把哈希当语义检索，会在这里得出错误结论。
- 「近邻不等于正确答案」：查询 *writes started failing on the gateway* 的**本意**是磁盘满的 D03。MiniLM 最近邻正是 D03（0.6642），说明真实语义模型可以作对；同一查询的哈希最近邻是讲 DNS 的 D14。旧版实践若只用哈希来讲「近邻不是答案」，实验本身是成立的，但**不能**把它写成语义检索的失败。修订后：语义结论只引用 MiniLM；哈希只作为「错误空间会给出错误近邻」的工程对照。

官方 Python API `SentenceTransformer(...).encode(...)` 本机未跑，列为 `not_verified_on_this_machine`。付费或托管 embedding API 同样未调用。有 GPU/PyTorch 的环境可以用官方包复现，并与本课 ONNX 向量对照（量化模型与全精度不会逐位相同）。

## 常见误区

- 混用不同模型产生的向量。
- 把哈希或 bag-of-trigram 向量称为 SBERT / 语义模型。
- 认为分数最高的文档一定是要找的那条 runbook。
- 忽略 attention mask，把 padding 平均进句子向量。

## 设计权衡

语义召回覆盖表达差异，词法检索更擅长精确符号。生产系统常用两者融合，本课只做对照，没有做融合调权。量化 ONNX 更轻（约 23MB），与全精度 90MB 模型会有分数差，本课只用已下载并校验校验和的量化文件。

## 动手实践

```bash
cd practice160
bash test.sh
# 若本机没有模型缓存，fetch_model.py 会按官方 URL 与 sha256 下载
# 可用 MINILM_HOME 指向已下载目录，避免把 23MB 权重打进课程 tar
```

本机模型目录：`/workspace/atlas/gen-batch-153-162-revision/models/all-MiniLM-L6-v2`。依赖：onnxruntime 1.32.0、tokenizers 0.23.3、numpy 2.5.4。

## 源码与文档阅读

`minilm.py` 的 mask 平均与 L2；`fetch_model.py` 的校验和；`embed.py` 的 BM25 与哈希（读注释，不要跨文件把哈希当 MiniLM）；`docs.json` 的 50 条文档；`reports/retrieval.txt`。

## 自测题

**1. 为什么不能把 hash32 的近邻表写进「语义检索更好」的结论？**

答案：hash32 只是按词哈希累加的教学向量，没有在句子对上进行语义训练。本课中它对 `kv-node-7` 的第一名是无关文档 D49。语义结论只能引用 MiniLM 或你在真实 SBERT API 上的复现。

**2. MiniLM 已经把 D03 排第一，为什么还要保留「近邻不等于正确答案」？**

答案：这是验收原则，不是这一条查询的失败记录。换一条查询或一个未对齐的索引，最高分仍可能是错的。要用标注而不是「第一名」本身当真理。

**3. 换到另一个维度或另一个版本的模型，旧索引能继续用吗？**

答案：不能直接混用。应重建或隔离。维度不同时，本课的余弦函数会拒绝计算。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | Last-Modified (CST) | 说明 |
| --- | --- | --- | --- | --- |
| [Sentence Transformers — Semantic Similarity](https://www.sbert.net/docs/sentence_transformer/usage/semantic_textual_similarity.html) | 166292 | 2026-11-15 22:58:55 | 2026-11-11 22:35:30 | 官方用法。本课用同系列 MiniLM 权重的 ONNX 导出复现编码与余弦。英文，基础，免费。 |
| [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | 模型卡 API 200（2026-11-15 23:19:51 CST） | 2026-11-15 | — | 权重来源。使用 `onnx/model_quint8_avx2.onnx` 与 `tokenizer.json`。 |

## 本课小结

余弦相似度只在同一模型、同一版本的向量空间里有意义。本课用官方 MiniLM ONNX 建立了 50 条文档的语义索引，并与 BM25、哈希对照；哈希不是语义模型。近邻仍需人工或标注确认。托管 API 与官方 Python 包路径已给出，本机未跑。
