# 第 173/208 课：MCP 授权与信任边界

> 序号 173/208 · id `l-mcp-auth` · 领域 L · 阶段 4 · 难度 深入 · 子类 MCP互操作
> 前置：[`l-mcp-transport`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/172.html)、[`j-security`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/156.html)
> 节点摘要：远程 MCP 授权首先是资源服务器的安全工程：客户端取得的令牌必须适用于当前服务器，服务器必须验证 audience、有效期和权限范围，并对每个具体工具及业务资源再次授权。

## 导读

第 172 课把 MCP 的消息语义与传输生命周期分开；本课进入下一层：一条已建立的连接并不等于一张可以调用任意工具、读写任意租户的通行证。正式节点把 `l-mcp-transport` 与 `j-security` 同时列为前置，原因正在这里——传输只保证“消息能到达”，安全边界要由资源服务器自己执行。

教学假设：一个后台诊断控制台通过 MCP 向排障服务查询只读诊断，偶尔也允许写入标记。这是为讲解授权与信任边界设计的场景，不代表腾讯或微信的内部接口、协议或实现。实践用本机 RS256 教学授权服务器签发 JWT，并实现 RFC 9728 受保护资源元数据与 RFC 8414 授权服务器发现；`is_live_oauth_login=false`，没有做完整浏览器登录或生产级 PKCE 流程，也不调用付费 API。

## 学习目标

1. 说明资源服务器、授权服务器与客户端的职责及其适用边界。
2. 独立完成实践：用五类测试身份发出请求，并让 HTTP 与 Agent 走同一套 `authorize()`。
3. 用负例确认：错误 audience 不能进入业务处理，只读 scope 不能写，日志中不出现完整令牌。

## 为什么后端要懂

节点给出的理由是「防止只读诊断接口演变成越权代理，保证 Agent 与普通客户端遵循同一套后台授权规则」。把工具从客户端界面隐藏起来，或把模型说的“用户已经同意”写进提示词，都挡不住一次直接打到服务端的 HTTP 调用。一旦诊断 MCP 能读到别的租户、或只读令牌能改配置，问题就不在模型循环里，而在后台授权缺口上。

## 核心知识点

### 1. 资源服务器、授权服务器与客户端

**直觉。** 授权服务器（AS）负责签发令牌，资源服务器（RS，这里是 MCP 服务）负责验证并执行业务，客户端（MCP 客户端或 Agent 宿主）负责按规范获取令牌并把请求打到正确的 RS。三者不能互相替代。

**原理。** 2026-11-15 23:53:05（UTC+8）抓取的 [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)（Last-Modified 2026-11-15 03:17:31，UTC+8；规范版本 2026-07-28）写明：受保护的 MCP 服务器是 OAuth 2.1 资源服务器，MCP 客户端是 OAuth 2.1 客户端，令牌由授权服务器签发。服务器必须实现 RFC 9728 Protected Resource Metadata，客户端必须用它发现授权服务器。实践里教学 URL 是 `http://127.0.0.1:0/as` 与 `http://127.0.0.1:0/mcp`，只用于本机元数据与 audience 绑定，不是可在公网访问的 AS。

**边界。** 本课验证的是 RS 如何校验令牌并做业务授权，不是“我已经在浏览器里走完生产登录”。`is_live_oauth_login=false`。

### 2. resource 参数与 audience 绑定

**直觉。** 令牌必须是“发给当前这台 MCP 服务”的，而不是“发给随便某个 API”的通用通行证。

**原理。** 同日抓取的 [Authorization security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations)（Date 2026-11-15 23:52:57，Last-Modified 2026-11-15 03:15:14，UTC+8）要求按 RFC 8707 做 audience 绑定：资源服务器必须拒绝 `aud` 中不包含自身的令牌。实践用 PyJWT 2.15.1 在校验阶段抛出 `InvalidAudienceError`，并且把这次拒绝记在进入业务函数之前。错误 audience 的 `business_entered=false`，HTTP 与 Agent 两条路径结论相同。

**边界。** 教学令牌由本机 RSA 密钥签发；不要把这套密钥或签发逻辑搬到生产后还自称完成了企业 IdP 对接。

### 3. scope 与租户权限的双层校验

**直觉。** 有 `read` 不等于能 `write`，能读租户 A 不等于能读租户 B。scope 是第一层，具体资源上的 ACL 是第二层。

**原理。** 实践里 `TOOL_SCOPES` 规定诊断只需要只读 scope，写工具需要写 scope；租户 ACL 再限制 token 里的主体能碰哪份资源。只读令牌调用写工具会被拒，跨租户查询同样被拒。未在客户端暴露的隐藏工具 `drop_table` 即便被直接调用，服务端仍按缺 scope 拒绝，且 `business_entered=false`。

### 4. issuer、重定向允许列表与发现元数据

**直觉。** 客户端必须确认“我正在跟哪个 AS 说话、回调会回到哪里”，不能跟着一份随便返回的 JSON 跳到攻击者的授权页。

**原理。** 规范要求校验证书签发者、保护资源元数据，以及重定向 URI 允许列表。实践导出 JWKS 时使用 `jwt.algorithms.RSAAlgorithm.to_jwk`（PyJWT 2.15.1 没有 `jwt.helper`），并校验 issuer 与允许的 redirect。这些检查发生在发令牌与接受令牌的路径上，而不是只写在文档里。

### 5. 禁止令牌透传

**直觉。** 用户交给客户端的访问令牌，不能原样转交给上游再当作通用凭证。

**原理。** 安全考虑文档把 token passthrough 列为明确禁止：不得把客户端令牌直接传给上游服务。MCP 服务若需要调用别的 API，应使用自己的凭证或经过正确 audience 的令牌，而不是“用户刚拿到的那一张”。

### 6. 脱敏、过期、撤销与拒绝审计

**直觉。** 过期和撤销必须立刻生效；日志只要足以审计，不要变成令牌备份。

**原理。** 实践用 `RedactingFilter` 把 JWT 写成 `tok_fp=` 指纹，运行日志中没有原始 `eyJ` 串。过期令牌、已被撤销的令牌都在授权阶段失败，不会进入业务。拒绝原因使用稳定代码（`bad_audience`、`expired`、`missing_scope`、`cross_tenant`、`revoked`），便于对照验收，而不把完整令牌写进 trace。

### 代码路径：同一道授权门

`tokens.py` 负责签发与发现：本地 RSA 密钥、RFC 9728 PRM、issuer 元数据、撤销集合。`authorize.py` 是唯一入口，先做 audience / 有效期 / issuer 校验，再检查 `TOOL_SCOPES` 和租户 ACL；失败时只返回原因码和 `tok_fp`。`service.py` 里，Starlette 的 HTTP 处理函数和 `agent_call()` 都走到同一个 `dispatch()`，因此“客户端没画出写工具”并不能让 Agent 得到额外权限。`driver.py` 用同一枚正常令牌分别走 HTTP 与 Agent，所以正常用例的业务进入次数是 2，而不是 1；写验收断言时要按双路径计数，否则会把正确实现误判为失败。

## 常见误区

仅在客户端隐藏工具却不在服务端授权，直接 HTTP 调用会绕过这层“看不见”。把一次连接授权，或模型生成的“用户已同意”字句，当成后续所有写操作的权限，会把同意范围扩到整台服务。这两条都是节点列出的陷阱，实践用隐藏工具的直接请求与只读写拒绝来对应它们。

## 设计权衡

细粒度 scope 和按资源的策略能提高隔离性，但配置和兼容成本更高。权限模型必须按业务风险来设计，不能让模型“判断一下该不该做”来填补缺口。教学实现把五类身份和双路径（HTTP / Agent）收口到同一个 `authorize()`，是为了让这条规则可验证，而不是演示一个完整的企业 IAM 产品。

## 动手实践

实践包是 `practice173.tar.gz`。需要 `PyJWT`、`cryptography` 和 Starlette 测试客户端（本环境用 `/workspace/atlas/cmvenv`）。不调用付费 API，也不做真实浏览器登录：

```bash
tmp_dir=$(mktemp -d)
tar -xzf practice173.tar.gz -C "$tmp_dir"
cd "$tmp_dir/practice173"
bash test.sh
```

脚本写入 `run.log` 与 `results.json`，最后一行应为 `ALL_CHECKS_PASSED`。本次机器环境为 CPython 3.13.5。独立复跑后：

1. **五类令牌。** `normal` 允许进入业务；`wrong_audience`、`expired`、`missing_scope`、`cross_tenant` 均拒绝，且错误 audience 的 `business_entered=false`。
2. **只读不能写，跨租户不能写。** `readonly_cannot_write=true`。
3. **同一授权逻辑。** HTTP 与 Agent 都调用 `dispatch()` → `authorize()`，`http_and_agent_same_logic=true`。正常用例的业务进入计数按两条路径各一次统计。
4. **隐藏工具。** 直接请求 `drop_table` 得到 `missing_scope`，未进入业务。
5. **日志脱敏。** `logs_contain_raw_tokens=false`，只见 `tok_fp=`。
6. **撤销。** 已撤销令牌的原因码为 `revoked`。

未在本机验证的边界写入了 `not_verified_on_this_machine`：完整的浏览器 OAuth / PKCE 生产登录、以及跨主机的真实授权服务器。`is_live_oauth_login=false`。

## 源码与文档阅读

2026-11-15（UTC+8）亲自抓取 MCP 2026-07-28 授权规范与安全考虑页，两条均为 200。授权页首次用默认库抓取时遇到 TLS `UNEXPECTED_EOF`，改用 `curl -L` 后得到 253542 字节，Last-Modified 为 2026-11-15 03:17:31（UTC+8）。安全考虑页 Last-Modified 为 2026-11-15 03:15:14（UTC+8）。与本课直接相关的当日结论：受保护 MCP 服务器是 OAuth 2.1 资源服务器；必须实现 RFC 9728；必须做 audience 绑定；禁止令牌透传。实践是教学 RS256 JWT 与本地发现元数据，不能把这次验收写成“已经对接生产 AS”。

## 自测题

**1. 为什么错误 audience 必须在进入业务函数之前被拒绝？**

答：audience 绑定要保证令牌是发给当前这台资源服务器的。若先跑业务再看 `aud`，越权查询或写入可能已经发生。本次 `wrong_audience` 的 `business_entered=false`，拒绝原因是 `jwt.InvalidAudienceError`。

**2. 为什么只在客户端把危险工具隐藏起来不够？**

答：隐藏只影响“模型或 UI 能不能看见”，不阻止直接 HTTP 调用。本次对未暴露的 `drop_table` 直接发请求，服务端仍返回 `missing_scope`，没有进入业务。

**3. 用户同意建立 MCP 连接，是否等于允许写所有租户？**

答：不等于。连接授权和每次工具调用的 scope、租户 ACL 是不同层。本次只读令牌不能写，跨租户请求也被拒；规范还禁止把给其他服务的令牌直接透传过来当通行证。

## 推荐资源

- [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)：节点主资源，核对 RS / AS / 客户端职责和 resource 参数。
- [MCP Authorization security considerations](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations)：核对 audience、令牌存储与禁止透传等强制要求。

## 本课小结

MCP 授权是资源服务器的职责：校验 audience、有效期、scope，再对具体工具和租户做第二层授权；HTTP 与 Agent 必须走同一道门；日志只保留指纹。本课用本机 RS256 教学令牌验证了五类身份和脱敏，没有做生产 OAuth 登录。下一课是第 174 课 l-harness（Agent Harness 的组成）。
