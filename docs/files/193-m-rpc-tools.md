# 第 193/208 课：为 C++ RPC 封装安全工具

> 序号 193/208 · id `m-rpc-tools` · 领域 M · 阶段 4 · 难度 深入 · 子类 工具基础设施
> 前置：[`l-mcp-auth`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/173.html)、[`j-security`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/156.html)、[`f-security`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/93.html)、[`f-backpressure`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/91.html)
> 节点摘要：通过受控网关将少量后台 RPC 映射为工具，执行身份、资源授权、deadline 与审计由服务端决定。

## 导读

第 173 课把授权责任放在服务端，第 93、156 课讨论服务身份。本课把一个**真实编译**的 C++ KV 服务放到网关后面，只向 Agent 暴露 `get`/`exists`，写操作必须另走审批。教学假设：这是一条行协议教学 RPC，不是腾讯或微信的内部接口，也不是生产 gRPC 集群。`is_live_model=false`。

开工前先盘点 `/workspace/atlas/cmvenv`：本次使用 cmake 4.4.4、ninja 与 zig c++（clang 22.1.8）编译并链接了 `kv_server`，再用 Python 网关经 TCP 调用它。本机没有系统 `g++` 或 `grpc_cli`，但不把「系统 PATH 没有 g++」写成无法构建。

## 学习目标

1. 说明 schema 与协议适配的边界：工具参数必须映射到受控的后端命令，而不是把内部网络交给模型。
2. 独立完成实践：为 KV 暴露 get/exists，只允许白名单 key 前缀，写操作单独审批。
3. 验收：越权前缀无法读取；Agent 不能修改网关强制 deadline。服务身份来自凭证，而不是模型填写的 `user_id`。

## 为什么后端要懂

让 Agent 直接访问内网等于把授权交给提示词。网关多一次跳转，但身份、前缀、deadline、限额和审计都可以在服务端强制。

## 核心知识点

### 1. 身份在凭证上，不在工具参数里

**直觉。** 模型写 `user_id=bob` 并不使它成为 bob。

**原理。** [gRPC Authentication](https://grpc.io/docs/guides/auth/) 把客户端/服务端身份放在传输层凭证（TLS、ALTS、JWT 等）上。 [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) 同样把授权责任放在服务端。本课网关用一张 token→principal 表模拟服务身份：`tok-alice` 只能访问以 `alice/` 为前缀的键。alice 携带 `user_id=bob` 去 `GET bob/secret` 时，网关返回 `forbidden_prefix`；同时直连 C++ 后端仍能读到 `VALUE should-not-leak`，说明挡住的是网关而不是数据消失。`EXISTS bob/secret` 同样被拒，避免用存在性探测越权。

**边界。** 本次没有部署真实的 gRPC TLS/ALTS 或 SPIFFE，也没有跑 MCP 授权服务器。结论落在「模型字段不能当作认证身份」这一责任划分上。

### 2. 写操作单独审批，deadline 由网关封顶

**直觉。** 「approved: true」不是审批。更大的 `deadline_ms` 也不能让服务端多等一会儿。

**原理。** `SET` 必须持有网关签发的票据，且票据的 `key` 与 `issued_to` 必须匹配。缺少票据、或把 `approved: true` 当作票据、或 bob 重放 alice 的票据，都会得到 `write_requires_approval`，原值不变。deadline 方面，网关把请求中的 `deadline_ms` 截断到 `max_deadline_ms=150`。实践里 Agent 申请 5000ms 并让后端 `SLEEP 800`，调用在约 150ms 返回 `deadline_exceeded`，审计记录 `requested_deadline_ms=5000`、`enforced_deadline_ms=150`、`deadline_clamped=true`。同一接口在 `SLEEP 20` 时成功，说明截断不会误杀短调用。

限额与幂等是同一层的服务端策略：本课对每个 principal 做了简单计数限流，并保留审计。生产上还会有幂等键与更细的配额。

## 常见误区

- 把模型提供的 `user_id` 当作认证身份。
- 只拦截 `GET`，忽略 `EXISTS` 或其它只读变体。
- 让 Agent 在参数里改 deadline 或把「我已审批」写成布尔字段。
- 未盘点 cmvenv 就宣称 C++ 无法编译。

## 设计权衡

网关增加一跳延迟，但信任边界清晰。工具越少越好：本课只开放 get/exists，写路径独立审批，比把整个 KV 方言暴露给模型更安全。

## 动手实践

实践包在 `practice193/`。需要 cmvenv 中的 cmake、ninja、ziglang。

```bash
tmp_dir=$(mktemp -d)
cp -a practice193 "$tmp_dir/"
cd "$tmp_dir/practice193"
bash test.sh
```

本次实测：`kv_server` 由 clang 22.1.8 构建；越权前缀被拒；写无票据被拒；Agent 的 5000ms deadline 被截到 150ms，实际等待约 150.3ms。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [gRPC Authentication](https://grpc.io/docs/guides/auth/)：HTTP 200，`Date` 2026-11-15 16:41:26 GMT（Asia/Shanghai 2026-11-16 00:41:26），未改 URL。同日抓取 [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)：HTTP 200，`Last-Modified` 2026-11-14 19:17:31 GMT（Asia/Shanghai 2026-11-15 03:17:31）。本课未运行生产 gRPC 凭证或 MCP 授权服务器。

## 自测题

**1. 为什么 alice 带上 `user_id=bob` 仍不能读取 `bob/secret`？**

答：身份来自网关校验过的 token，而不是工具参数。本次该调用返回 `forbidden_prefix`，后端数据仍在。

**2. Agent 把 deadline 改成 5000ms，调用会等多久？**

答：以网关封顶为准。本次封顶 150ms，SLEEP 800 的调用在约 150ms 返回 `deadline_exceeded`，没有等到 5 秒。

**3. 怎样才算一次合法写入？**

答：调用者必须持有网关签发、且与 principal 和 key 匹配的审批票据。`approved: true` 或他人票据都不会改数据。

## 推荐资源

- [gRPC — Authentication](https://grpc.io/docs/guides/auth/)（英文，文档，深入，免费）：传输层身份与本课教学 token 表的差异需写明。
- [MCP — Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)（英文，文档，进阶，免费）：授权在服务端，不在模型侧。

## 本课小结

把少量 RPC 变成工具时，身份、前缀、deadline 和写审批都必须由网关强制。下一课是第 194 课 m-jobs（长任务服务与 Agent Worker）。
