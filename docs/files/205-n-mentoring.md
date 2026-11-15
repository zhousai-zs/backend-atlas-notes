# 第 205/208 课：技术传承与人才培养

> 序号 205/208 · id `n-mentoring` · 领域 N · 阶段 5 · 难度 专家 · 子类 组织与技术影响
> 前置：[`n-collaboration`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/204.html)
> 节点摘要：通过代码审查、结对调试、设计评审与复盘，把隐性经验转为可实践能力而非只传授结论。

## 导读

把「epoll 半关闭要记得处理 EOF」直接告诉新人，他们多半只会背结论。本课设计一套渐进任务包和评分标准：先复现，再写证据链，最后才修复。教学假设：脚本化学员用于验收量规，不是一次真实的带教周期。`is_live_model=false`。开工前已盘点 cmvenv 中的 cmake 4.4.4、zig c++ / clang 22.1.8；本机没有 strace，复现改用 `SHUT_WR` 后的超时，而不是宣称无法实验。

## 学习目标

1. 说明能力评估的边界：看证据链，不看行数或背诵。
2. 独立完成实践：设计新人完成一次 epoll 半关闭缺陷定位的任务包和评分标准。
3. 验收：学员能独立解释证据链；任务难度逐步提升且不过度代做。

## 为什么后端要懂

团队不能依赖单一专家。[Code Review](https://google.github.io/eng-practices/review/reviewer/) 把审查当作传授判断的方式，关注的是变更是否正确、是否可维护，而不是形式。带教同样如此。

## 核心知识点

### 1. 先让缺陷被编译后的程序抓住，再谈教学

**直觉。** 任务包如果不能复现，评分就只能靠印象。

**原理。** 起始 `echo_server` 用 epoll 监听 `EPOLLIN`，读到完整的 `PING\n` 后仍等待后续输入，并忽略 `read()==0`。客户发送 `PING\n` 后 `shutdown(SHUT_WR)`，1 秒内收不到 `PONG`，复现脚本把超时写入 `reports/reproduce.log`。打开 `-DFIX=1` 后，完整一行或对端半关闭都会立即回复并关闭，复现脚本收到 `PONG`。缺陷是被真实编译的服务器和超时抓住的，不是口头案例。

### 2. 评分看证据链，阶段 1–2 不代做

**直觉。** 导师如果在第一阶段就贴出补丁，新人只是在提交别人的修复。

**原理。** 任务包分三阶段：复现、写证据、再修复。阶段 1–2 的文本不得出现补丁要点（本课检查了 `TASK.md` 前半没有 `-DFIX=1` 与「立即回复并关闭」）。合格证据必须写出现象（超时、无 PONG）、半关闭事实，以及忽略 EOF 的代码位置。只写「我改了 25 行」或只背「epoll 是边缘触发」且不看日志，量规会拒绝。这就是「能独立解释证据链」的可检查含义。

**边界。** 没有 strace，也没有覆盖数周的真实成长曲线。clang-tidy 已在本机（22.1.8），但本课以复现与证据为主，不以 tidy 代替半关闭实验。

## 常见误区

- 以代码行数或背诵概念评价成长。
- 第一阶段就给出完整补丁，变成代做。
- 只讲「半关闭要注意」，却没有可超时的复现程序。

## 设计权衡

导师要在短期投入时间设计任务包和看证据，换来的是团队以后能自己定位同类问题。把结论直接抄给对方更快，但带不走判断力。

## 动手实践

实践包在 `practice205/`。需要 cmvenv 的 cmake、ninja、ziglang。

```bash
tmp_dir=$(mktemp -d)
cp -a practice205 "$tmp_dir/"
cd "$tmp_dir/practice205"
bash test.sh
```

本次实测：错误二进制在半关闭后超时；修复后收到 PONG；合格证据通过，行数评分和纯背诵被拒。`is_live_model=false`。

## 源码与文档阅读

节点 `verifiedAt` 为 2026-10-26。2026-11-16（Asia/Shanghai，UTC+8）抓取 [How to do a code review](https://google.github.io/eng-practices/review/reviewer/) 及其 [Standard of Code Review](https://google.github.io/eng-practices/review/reviewer/standard.html)：HTTP 200，`Date` 2026-11-15 16:53:22 GMT（Asia/Shanghai 2026-11-16 00:53:22），`Last-Modified` 2024-09-19 05:28:40 GMT（Asia/Shanghai 2024-09-19 13:28:40）。官方索引页较短，标准页补充了审查应关注的正确性与可维护性；本课把同样的标准用到带教量规上。

## 自测题

**1. 为什么阶段 1–2 不能给出补丁？**

答：否则学员无法独立走完复现和证据。本次任务包前半不包含修复要点。

**2. 「改了 25 行」为什么不能得分？**

答：量规拒绝按行数评分。没有超时现象和半关闭证据，就不能说明他定位了缺陷。

**3. 本机没有 strace，实验是否就做不了？**

答：不可以这样写。本次用客户半关闭加 1 秒超时抓住了同一类缺陷，并写明未使用 strace。

## 推荐资源

- [Google Engineering Practices — Code Review](https://google.github.io/eng-practices/review/reviewer/)（英文，文档，专家，免费）：审查标准与应关注的证据。

## 本课小结

把隐性经验做成可复现的渐进任务，并用证据链而不是行数来评分。下一课是第 206 课 n-strategy（技术战略与组合投入）。
