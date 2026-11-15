# 第 190/208 课：后台知识库与版本证据

> 序号 190/208 · id `m-knowledge` · 领域 M · 阶段 3 · 难度 进阶 · 子类 知识与诊断
> 前置：[`k-rag`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/164.html)、[`j-security`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/156.html)
> 节点摘要：把代码、runbook、设计文档与公开手册统一索引，回答需绑定版本、权限与原文位置。无权限文档不得进入模型上下文；陈旧文档要显著标记。只在展示层过滤权限会提前泄漏检索内容。

## 导读

第 164 课讲检索增强，第 156 课讲安全边界。本课把 KV 的 README、接口说明和故障手册放进本地索引，回答时返回路径和版本，并在**召回阶段**按 ACL 过滤。节点原来引用的 LangChain Retrieval URL 在当日已重定向，必须引用最终地址，而不能假装旧路径仍托管该文档。

教学假设：文档中的手册和「法律/值班」条目是为 ACL 实验构造的，不代表任何公司的内部知识库。检索器是词项重叠，**不是**嵌入模型，也不是把哈希当作 SBERT。没有调用付费模型，`is_live_model=false`。

## 学习目标

1. 说明文档解析、符号/正文索引、ACL 与版本失效的适用边界。
2. 独立完成实践：索引 README、接口说明和故障手册，回答时给出文件路径及版本。
3. 无权限文档不进入模型上下文；陈旧文档有显著标记。

## 为什么后端要懂

统一索引降低跨系统找资料的成本，但也把权限和过期文本变成检索问题。若先把受限文档打进候选再在界面上藏掉，模型上下文或日志里可能已经出现不该出现的句子。

## 核心知识点

### 1. 权限过滤发生在召回，不是展示

**直觉。** 进不了候选，才进不了提示。

**原理。** 每篇文档有 `acl`（public / eng / legal）。角色 `eng` 只能召回 public+eng。用「settlement clause ... LEGAL_SECRET_DO_NOT_RETRIEVE」查询时，无 ACL 的词项重叠会命中 `legal-oncall`；角色为 eng 时该文档在打分前就被去掉，上下文里既没有该 id，也没有 `LEGAL_SECRET_DO_NOT_RETRIEVE`。同一查询在角色 `legal` 下可以命中。`eng` 查询 EXISTS 手册时同样不含这条受限正文。这是检索期过滤，不是事后擦掉答案里的字。

Viewer 只能看到 public 的 README，看不到 eng 的 runbook。

### 2. 版本、陈旧标记与诚实的检索器

**直觉。** 过期手册必须自己开口说自己过期。

**原理。** `runbook-timeout-old` 带 `stale: true` 和 `superseded_by: runbook-exists`。查询旧的「restart kv process」建议时，命中文档的 body 以 `[STALE] version=1.0 as_of=2025-01-15 superseded_by=runbook-exists` 开头。每条命中携带 `path` 与 `version`。新手册 `runbook-exists` 把「先重启进程」从首选步骤里拿掉，这正是保留旧文档却必须标记的原因。

检索器按 token 重叠排序，结果里写明 `retriever: lexical token overlap (not an embedding model, not SBERT, not a hash-as-vector stand-in)`。本课没有用哈希向量冒充语义检索，也没有调用 LangChain 托管的生产 retriever。

## 常见误区

- 只在展示答案时过滤权限，却把受限文档提前送进模型上下文或调试日志。
- 陈旧文档不标记，或与现行手册混排且不加版本。
- 用文本哈希当嵌入，却汇报「语义检索已验证」。
- 继续引用已重定向的 `docs.langchain.com/oss/python/langchain/retrieval`，不写最终 URL。

## 设计权衡

统一索引易用，但权限、版本和更新流水线更复杂。本地词项检索可复现、不依赖模型，召回质量有限；嵌入检索更「语义」，却要另做模型、版本和评测，且仍不能替代 ACL。

## 动手实践

实践包在 `practice190/`，语料在 `docs/`。

```bash
tmp_dir=$(mktemp -d)
cp -a practice190 "$tmp_dir/"
cd "$tmp_dir/practice190"
bash test.sh
```

本次：eng 召回不含法律秘密，legal 角色可见；旧 runbook 被标 `[STALE]`；命中含路径与版本。`is_live_model=false`。未实测：LangChain 生产检索器或嵌入索引、真实模型的答题质量。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26，节点资源仍写着 `https://docs.langchain.com/oss/python/langchain/retrieval`。2026-11-16（Asia/Shanghai，UTC+8）抓取该 URL：HTTP 响应最终落到 [`/oss/python/deepagents/retrieval`](https://docs.langchain.com/oss/python/deepagents/retrieval)，正文 523303 字节，`Date` 2026-11-15 16:13:56 GMT（Asia/Shanghai 2026-11-16 00:13:56），`Last-Modified` 2026-11-14 19:54:55 GMT（Asia/Shanghai 2026-11-15 03:54:55）。课文与 `results.json` 同时记录请求 URL 与最终 URL。最终文档讨论的是 Deep Agents 检索组件，不能把它的托管实现细节写成「本课本地词项索引已经等价跑通」。

## 自测题

**1. 为什么「答案里没出现秘密句子」还不够？**

答：若检索阶段已经把受限文档送进上下文，模型或日志仍可能泄漏。本次在打分前就去掉 `acl=legal` 的候选，eng 的上下文 JSON 不含该标记。

**2. 旧 runbook 为什么还要留在索引里？**

答：便于对照历史建议，但必须显著标记过期和替代文档，以免模型把它当现行步骤。

**3. 当天核对 LangChain 文档时要注意什么？**

答：旧路径会重定向到 `https://docs.langchain.com/oss/python/deepagents/retrieval`，应引用最终 URL，并说明本课检索器与该托管实现不是同一套。

## 推荐资源

- [LangChain / Deep Agents Retrieval](https://docs.langchain.com/oss/python/deepagents/retrieval)（英文，文档，进阶，免费；由旧 Retrieval 路径重定向）：核对检索组件的官方位置，不要沿用失效路径。
- 第 164 课、第 156 课：检索质量与安全边界。

## 本课小结

知识库答案必须带路径、版本和权限。ACL 在召回时生效，过期文本要自己标明过期，检索方法要如实标注。官方文档路径变了就要跟着改。下一课是第 191 课 m-logs（Agent 日志查询工具）。
