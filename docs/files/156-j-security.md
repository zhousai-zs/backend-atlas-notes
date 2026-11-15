# 第 156/208 课：后台安全与威胁建模

> 序号 156/208 · id `j-security` · 领域 J · 阶段 4 · 难度 深入 · 子类 交付与安全
> 前置：[`j-release`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/155.html)
> 节点摘要：后台接口要把认证、授权和输入边界画清楚，避免 SSRF 与命令注入。

## 导读

诊断 HTTP 接口往往「为了方便」：随便填一个 URL 帮我抓日志，或拼一条 ping 命令看网络。这些方便一旦暴露，就是 SSRF 和命令注入。本课用一个**真实的本机 HTTP 服务器**对比 broken / fixed 两种实现，而不是用假的安全扫描器分数代替验收。

教学假设：内部诊断接口。密钥 `atlas-diag-secret`、允许的主机名单都是教学设计。`is_live_network_ssrf=false`：实验**不会**真的访问链路本地或云元数据，只记录「若按 broken 逻辑将会发出的目标」。

## 学习目标

1. 解释认证与授权及适用边界
2. 独立完成实践：为诊断 HTTP 接口绘制信任边界，阻止任意 URL 与 shell 参数。

## 为什么后端要懂

有登录不等于可以碰所有资源。错误响应里的堆栈和密钥会把一次失败变成一次泄露。威胁建模要先画边界，再写代码。

## 核心知识点

### 1. 认证与授权

**直觉。** 认证回答「你是谁」，授权回答「你能做什么」。只检查「带了 token」却不检查「这个 token 能不能触发副作用」，未授权请求仍可能留下痕迹。

**原理。** 2026-11-15 抓取 OWASP *REST Security Cheat Sheet*（`https://cheatsheetseries.owasp.org/cheatsheets/REST-Security-Cheat-Sheet.html` 实际 URL 为节点给出的 `.../REST_Security_Cheat_Sheet.html`，Date 2026-11-15 22:58:53 CST，Last-Modified 2026-11-15 20:35:41 CST，98209 字节）。备忘录强调认证、授权、输入校验和错误信息最小化。本课 fixed 模式：没有合法 token 的请求一律 401，并且**不调用** `record_side_effect("unauthorized")`；broken 模式则先记副作用再回 401。验收：`unauth_side_effects == 0`，`unauth_codes == [401, 401]`。

### 2. SSRF 与命令注入

**直觉。** 「帮我抓这个 URL」若目标由调用方决定，服务器就会变成对内网的跳板。「帮我 ping 这个主机」若字符串进了 shell，`;id` 就会变成第二条命令。

**原理与实践对照。** `server.py` 的 broken `/fetch` 接受任意 URL，但实现只把目标记入副作用日志，**不发起**对 `169.254.169.254` 或 `file://` 的真实请求。fixed 模式只允许名单内的主机，其余 400，且响应体不含密钥。broken `/ping` 把主机拼进 `ping -c 1 {host}` 字符串并返回 `composed` 字段（用于证明注入已到达命令行）；fixed 拒绝非法字符，不经过 shell。`driver.py` 断言：broken 下注入字符串可见，fixed 下 `shell_inject` 为 400 且不泄露密钥。

这验证的是**服务端边界逻辑**。它不是一次对云厂商元数据接口的实网 SSRF 利用，也不执行真实 shell。不要把「composed 字段里出现 `;id`」写成「已经在操作系统里执行了 id」。

### 3. 密钥轮换与最小权限、错误不泄漏

**直觉。** 报错时把 secret 和 traceback 回给客户端，等于把钥匙和内部结构一起送出去。

**实践对照。** broken 的错误路径会把 `SECRET` 和堆栈放进 JSON；fixed 路径断言响应文本不含密钥、不含 `Traceback`。`results.json` 的 `errors_do_not_leak_secret_or_stack_fixed=true` 针对 fixed 模式。信任边界写在 `reports/boundary.json`：客户端不可信，诊断接口是边界，允许的 fetch/ping 目标在名单内。

## 常见误区

- 有登录即认为拥有所有资源权限。
- 用「我们没有真的打到元数据服务」来回避 allowlist；相反，本课正是因为不实网利用，才更要在代码里把名单写死并测试拒绝路径。
- 把命令字符串拼出来就以为「已经执行」，或反过来以为「没执行就不算注入漏洞」。broken 的缺陷是把不可信输入交给 shell；测试用 `composed` 证明构造已经完成。

## 设计权衡

严格边界可能降低灵活性（诊断接口不能随便抓任意 URL）。可以用受控的调试角色和短暂 token 做补偿，而不是把接口做成全能代理。

## 动手实践

```bash
cd practice156
bash test.sh
# 对照 reports/side-broken.txt 与 side-fixed.txt、boundary.json
```

## 源码与文档阅读

`server.py` 的 broken/fixed 分支；`driver.py` 对未授权副作用、SSRF、注入和泄露的断言。阅读时跟一遍未授权请求为什么在 fixed 模式碰不到 `record_side_effect`。

## 自测题

**1. 为什么 401 仍然可能「有副作用」？**

答案：若代码先执行 fetch/ping/写审计以外的变更，再检查 token，未授权调用已经产生了效果。本课 broken 路径会记录 `unauthorized` 副作用；fixed 先拒绝。

**2. 本机实验是否证明了「云元数据 SSRF 已被打通」？**

答案：没有。`is_live_network_ssrf=false`。证明的是 broken 逻辑会接受危险 URL、fixed 逻辑会拒绝，并且没有把密钥写进错误响应。

**3. 命令注入测试为什么返回 `composed` 而不调用 shell？**

答案：避免在教学环境执行任意命令；同时仍能证明不可信输入已经进入命令字符串。这足以验收「阻止 shell 参数」，不等于替代操作系统上的利用复现。

## 推荐资源

| 资源 | 字节 | 抓取时间 (CST) | Last-Modified (CST) | 说明 |
| --- | --- | --- | --- | --- |
| [OWASP — REST Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html) | 98209 | 2026-11-15 22:58:53 | 2026-11-15 20:35:41 | 核对认证、授权与输入边界。英文，深入，免费。 |

## 本课小结

威胁建模先画信任边界，再分别处理认证、授权、SSRF、注入和错误泄露。本课用真实本机 HTTP 服务验证了固定实现的副作用与泄露约束，但没有对外部元数据做实网利用，也没有真的执行 shell。
