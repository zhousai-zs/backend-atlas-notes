# 第 145/208 课：单元测试与边界设计

> 序号 145/208 · id `j-unit` · 领域 J · 阶段 1 · 难度 基础 · 子类 测试与调试
> 前置：[`j-cmake`](https://zhousai-zs.github.io/backend-atlas-notes/lessons/143.html)
> 节点摘要：围绕可观察行为测试模块，使用依赖注入控制时间、网络和故障，避免测试只复述实现。

## 导读

上一课用 CMake 把库、服务和测试拆成三个 target，消费者不用手写公共头路径。这一课把「测试」本身当作设计对象：断言检查的是可观察行为，还是实现步骤？夹具（fixture）能不能保证用例互不影响？边界值是被单独测到了，还是只覆盖了成功输入？

教学场景是一个后台消息解析器。微信公众号后台这类服务里，模块之间常常用长度前缀帧传配置或内部消息（下面的 2 字节大端长度 + payload 是教学假设，不是真实微信协议）。空包、截断包、超大长度和连续粘在一起的包，都是线上会碰到的输入；只拿一条完整的 `"hello"` 去测，重构时没有回归保护。

## 学习目标

1. 解释 fixture 与断言，以及它们的适用边界。
2. 能按等价类和边界挑选用例，而不是只堆成功路径。
3. 独立完成实践：为消息解析器测试空包、截断包、超大长度和连续包。

## 为什么后端要懂

节点给出的理由是「让重构有回归保护」。后台代码里，解析、重试、超时这类逻辑改起来很勤：你可能只是把 `>=` 改成 `>`，或把两次短读合成一次 `read`。没有针对行为的测试，改完只能靠线上暴露；测试若写成「必须先 `read(2)` 再 `read(payload)`」，实现一优化，测试反而红了。单元测试要保护的是对外契约，不是某一版源码的调用形状。

## 核心知识点

### 1. fixture 与断言

**直觉。** 断言是对「这一刻行为该是什么」的检查；fixture 是多个用例共享的准备方式。两者都是为了让每个用例独立、可重复，而不是为了少写几行重复代码那么简单。

**原理。** GoogleTest Primer 把断言分成两类：`ASSERT_*` 失败后中止当前函数，`EXPECT_*` 失败后继续，以便一次跑出更多问题。本课在取 `messages[0]` 之前用 `ASSERT_EQ(r.messages.size(), 1u)`，属于「失败后再往下走会越界」的情形，与 Primer 里对空指针先 `ASSERT_NE` 再解引用的规则一致。

`TEST_F` 的 fixture 必须派生自 `testing::Test`。Primer 写得很明确：每个 `TEST_F` 都会在运行时拿到一个全新的 fixture 对象，先 `SetUp()`，再跑测试，再 `TearDown()`，然后销毁；框架不会在两个用例之间复用同一个对象。本课的 `FrameTest` 把缓冲放在成员里：

```cpp
class FrameTest : public testing::Test {
 protected:
  void SetUp() override { buf_.clear(); scratch_ = 0; }
  std::string buf_;
  int scratch_ = 0;
};
```

`FrameTest.FixtureMutationDoesNotLeak` 把 `scratch_` 写成 42，`FrameTest.FixtureStartsFresh` 则断言它仍是 0。用 `--gtest_shuffle --gtest_random_seed=1` 时，`FixtureStartsFresh` 先于 `FixtureMutationDoesNotLeak` 运行；种子为 2 时顺序反过来。两次都是 `[  PASSED  ] 12 tests.`，说明新鲜对象这一条在真实运行里成立。

**适用边界。** fixture 适合「每个用例都要一份干净的解析缓冲、假时钟或假数据源」。它不适合把进程级静态残留当「会话」在用例之间接力——那会把执行顺序变成隐式输入。GoogleTest 可以换种子打乱顺序，但 `--gtest_filter` 只筛选用例、并不改注册顺序，不能靠它来证明独立性。

### 2. 等价类和边界

**直觉。** 不可能穷举所有字节串。先按行为把输入分成几类，再在每类的边界上取样：刚好合法、刚好非法、缺 1 字节、长度为 0。

**原理。** 本课帧格式是 2 字节大端 payload 长度（不含头）+ payload，最大 payload 为 `kMaxPayload = 1024`，恰好 1024 合法；长度为 0 的空包也合法，得到一条空字符串消息。这和「缓冲区里一个字节都没有」不是同一类：

| 等价类 | 例子 | 期望 |
| --- | --- | --- |
| 空缓冲区 | `""` | `kOk`，0 条消息，无 leftover |
| 空包 | 两字节 `0x00 0x00` | 1 条空消息 |
| 截断（头不完整） | 只有 1 字节 | 0 条消息，字节留在 leftover |
| 截断（payload 缺 1 字节） | 头声明 8 字节、少给 1 字节 | 整帧留在 leftover，不交付残缺消息 |
| 恰好上限 | payload 1024 字节 | 1 条消息，长度 1024 |
| 超大长度 | 长度字段 1025 | `kTooLarge`，不能当成截断留下等后续字节 |
| 连续完整包 | `"hi"` 再接 `"ok"` | 两条消息，无 leftover |
| 连续包 + 截断尾巴 | 两完整帧后再跟 6 字节残帧 | 先交付完整的，尾巴进 leftover |

人为注入的缺陷写在 `parse_frames` 里：有 `MSGPARSE_BUGGY` 时用 `if (len >= kMaxPayload)`，把恰好等于 1024 的合法包也判成超大。`-DMSGPARSE_BUGGY=ON` 构建后，12 个用例里 11 个通过，唯一失败的是 `FrameTest.ExactMaxPayloadAccepted`：

```text
Expected equality of these values:
  r.status
    Which is: 4-byte object <01-00 00-00>   # Status::kTooLarge
  Status::kOk
    Which is: 4-byte object <00-00 00-00>
payload of exactly kMaxPayload must be accepted
[  FAILED  ] FrameTest.ExactMaxPayloadAccepted
[  PASSED  ] 11 tests.
```

空包、截断、1025 的超大长度、连续包在有缺陷版本上仍然是绿的——它们测的不是这条 `>=` / `>` 边界。改成 `len > kMaxPayload` 之后，同一条用例通过，`frame_test` 为 `[  PASSED  ] 12 tests.`

**适用边界。** 等价类帮你决定「要哪几发输入」，断言仍然要写在可观察结果上（状态、消息内容、leftover），而不是「走了哪条 `if`」。边界选错（只测 1025、不测 1024）时，这次注入的缺陷会漏掉。

### 3. fake 与 mock

**直觉。** 解析超时不该在测试里真的 `sleep`，读网络也不该连到真实套接字。把时钟和字节源做成接口，测试里注入可控的替身。

**原理。** gMock for Dummies 把两个词区分得很清楚：fake 有能工作的实现，只是走了捷径（例如内存文件系统）；mock 则预先写好期望，规定会被调用哪些方法、几次、以什么参数。本课的 `FakeClock` 可以 `advance(199)` 再 `advance(1)`，`FakeSource` 能追加字节、限制每次只吐 1 字节，或 `set_fault(true)` 返回 -1。`StreamAssembler::poll()` 每次最多 `read(kReadChunk)`（256 字节），不完整帧从首次出现起超过 `timeout_ms` 才报超时。

一条典型的行为测试是：先写入 1 字节不完整头，`poll()` 得到 `kNeedMore`；时间拨到超时前 1 毫秒仍是 `kNeedMore`；再拨 1 毫秒得到 `kTimeout`。故障注入同样只看结果：`set_fault(true)` 之后 `poll()` 为 `kSourceError`。这些都不需要真实时间和真实网络。

作为对照，`brittle_mock_demo` 里的 `ScriptedMockSource` 规定必须先 `read(2)` 再 `read(5)`（按一帧的头和 payload 拆两次读）。真实汇编器一次 `read(256)`，这个 mock 失败，报 `read size mismatch: got 256 want 2`，状态变成 `kSourceError`。同一组字节交给 `FakeSource`，`FakeSource.SameBytesAreAssembled` 得到 `kGotMessage` 和 `"hello"`。

**适用边界。** 需要控制时间、短读或故障时，用 fake 注入状态就够了。只有交互本身是契约（例如「必须先协商再发数据」）时，才值得用 mock 去规定调用。把 `read` 的分片尺寸写进期望，就是在测试实现细节。

## 常见误区

1. **只测试成功输入（节点 pitfalls）。** 一条完整的 `"cfg"` 通过，并不说明空包、缺 1 字节或长度 1025 是对的。本课有缺陷的解析器在 11 个用例上是绿的，唯独在恰好 1024 这个边界上暴露。
2. **把静态残留当成会话，让用例互相接力。** `order_trap_demo` 用进程级 `g_buf` 把「先写 2 字节头、再补 `hello`」拆到两个测试里。按注册顺序跑是 `[  PASSED  ] 2 tests.`；`--gtest_shuffle --gtest_random_seed=1` 让 `CompleteWithPayload` 先跑，`"hello"` 被当成长度字段（`0x6865`），`messages.size()` 为 0，两个用例都失败。
3. **以为筛选用例等于改变顺序。** `--gtest_filter` 只决定跑哪些测试，默认仍按注册顺序执行。要检验独立性，应使用 `--gtest_shuffle` 并换种子，而不是调整 filter 字符串的写法。

## 设计权衡

节点给出的权衡是：「过多 mock 绑定实现细节，真实依赖又使测试慢而脆弱。」本课两端都做了对照。一边是 `FakeClock` / `FakeSource`：不睡真实时间、不连套接字，但仍然行使真实的解析与超时逻辑，`assembler_test` 7 个用例在种子 7 的 shuffle 下全部通过。另一边是按 `read(2)+read(payload)` 写死的 mock：汇编器改成一次读较大缓冲后，行为仍然正确，测试却红了。若把 `ByteSource` 换成真实 TCP，短读、延迟和故障都难以稳定复现，测试会既慢又脆。折中是依赖注入加 fake，把可观察结果当作断言对象。

## 动手实践

实践包在 `practice145/`（`proj/` 为源码，含解析库、GoogleTest 1.16.0 与四个测试目标）。在 `practice145/` 下执行 `bash test.sh`。本机没有 g++，实验用 cmake 4.4.4、ninja 1.13.2 和 zig c++（clang 22.1.8 前端）编译；读者用自己机器上的 g++ / clang++ 即可，脚本会优先选系统编译器。

1. **带缺陷构建。** `-DMSGPARSE_BUGGY=ON` 配置 14.8s，ninja 构建 `[1/11]` 至 `[11/11]`，得到 `frame_test` 与 `assembler_test`。
2. **让测试发现边界缺陷（验收：测试能发现一个人为注入的边界缺陷）。** 有缺陷版本的 `frame_test` 退出码为 1：12 个用例中 11 个通过，`FrameTest.ExactMaxPayloadAccepted` 失败，断言恰好 1024 字节的 payload 应被接受。空包、截断、超大长度和连续包在这一版本上仍然通过。
3. **修复后再跑。** `-DMSGPARSE_BUGGY=OFF` 配置 0.9s，构建 `[1/15]` 至 `[15/15]`（多了两个反例目标）。修复后 `frame_test` 为 `[  PASSED  ] 12 tests.`，`assembler_test` 为 `[  PASSED  ] 7 tests.`
4. **不依赖执行顺序（验收）。** `frame_test --gtest_shuffle --gtest_random_seed=1` 与 `seed=2` 顺序不同（种子 1 以 `EquivalenceHints.SingleNonEmptyPacket` 开头，种子 2 以 `FrameTest.EmptyBufferYieldsNoMessage` 开头），两次都是 12 个用例通过；`assembler_test` 在种子 7 下 7 个用例通过。
5. **反例。** `order_trap_demo` 默认注册顺序通过；种子 1 下 `CompleteWithPayload` 先跑，2 个用例全部失败。`brittle_mock_demo` 中 Fake 用例通过，规定 `read(2)+read(payload)` 的 mock 报 `read size mismatch: got 256 want 2`。

最后一行应为 `ALL_CHECKS_PASSED`，并生成 `results.json`。延伸练习：给解析器再注入一处「把长度字段理解成整帧长度」的缺陷，看现有截断与连续包用例能不能抓住；或把 `kReadChunk` 改成 2，观察那个 brittle mock 是否反而变绿——变绿本身就说明它绑的是读法，不是行为。

## 源码与文档阅读

节点建议阅读 GoogleTest — Primer。2026-11-14 北京时间核对：

- [GoogleTest Primer](https://google.github.io/googletest/primer.html) 返回 200，无跳转。HTML 43172 字节，标题为 "GoogleTest Primer | GoogleTest"。Date 为 2026-11-14 12:18:12 GMT（北京时间 20:18:12），Last-Modified 为 2026-11-13 21:40:56 GMT（北京时间 05:40:56）；抓取的 Markdown 约 18028 字节。
- 跟随阅读 [gMock for Dummies](https://google.github.io/googletest/gmock_for_dummies.html)（Primer 本身几乎不谈 fake / mock）。200，HTML 63887 字节，标题为 "gMock for Dummies | GoogleTest"。Date 为 2026-11-14 12:19:44 GMT（北京时间 20:19:44），Last-Modified 与 Primer 相同（北京时间 05:40:56）。

Primer 中与本课直接相关的定义：测试必须独立可重复，每个用例跑在不同的对象上；`ASSERT_*` 是致命失败，`EXPECT_*` 是非致命失败；`TEST()` 的第一个参数是 test suite 名，第二个是用例名，两者都不应包含下划线；`TEST_F` 在每个用例上构造新的 fixture，先 `SetUp()` 再运行，再 `TearDown()` 并销毁；多数情况应链接 `gtest_main`，而不手写 `main`。文中还提醒历史上的 Test Case / Test Suite 用语与 ISTQB 不一致，当前推荐 `TestSuite`。

与本课实验的差异：

1. Primer 的入门例子是 `Factorial(0)` 与正整数，没有覆盖空包与空缓冲区的区分，也没有截断、上限边界和连续帧；这些是本节点的实践，实验额外补上。
2. Primer 不定义 fake 与 mock。gMock for Dummies 写的是：fake 有可工作但走捷径的实现，mock 则预置对调用的期望。本课用手工写出的 `FakeSource` / `ScriptedMockSource` 对照这一点，没有使用 `MOCK_METHOD` / `EXPECT_CALL` 宏。
3. Primer 假定系统已能编译并链接 GoogleTest；本实验把 1.16.0 源码放在 `third_party/`，在没有 g++ 的机器上用 zig c++（clang 22.1.8）和 cmake 4.4.4 编过。

## 自测题

**1. 为什么 `FrameTest.ExactMaxPayloadAccepted` 能抓住缺陷，而 `OversizedRejected` 和 `EmptyPacketAccepted` 在有缺陷版本里仍然是绿的？**

答：注入的缺陷是 `len >= kMaxPayload`，只误伤恰好 1024 的合法包。`OversizedRejected` 用的是 1025，两种比较都会拒绝；`EmptyPacketAccepted` 的长度是 0，两种比较都会接受。等价类不同，测不到同一条边界。修复后改成 `len > kMaxPayload`，1024 的用例才变为通过。

**2. 同事说「我用 `--gtest_filter=A:B` 指定了顺序，所以这两个用例的依赖没有问题」。用本课内容说明问题。**

答：filter 只筛选要跑的用例，并不改变注册顺序。本课 `order_trap_demo` 按注册顺序（先头后负载）是 2 个用例通过；用 `--gtest_shuffle --gtest_random_seed=1` 让 `CompleteWithPayload` 先跑，共享的 `g_buf` 里没有那 2 字节头，`"hello"` 被当成长度字段，两个用例都失败。独立性要用新的 fixture（或每个用例自己的局部缓冲）来保证，并用 shuffle 换种子验证，而不是靠 filter 的写法。

**3. 为什么「先 `read(2)` 再 `read(5)`」这类 mock 会在行为仍然正确时失败？**

答：`StreamAssembler` 的契约是「源里有一帧完整字节就交出消息」，并没有保证按头和 payload 分两次读。实现一次 `read(256)`，手工 mock 报 `read size mismatch: got 256 want 2`，测试失败；同一输入用 `FakeSource` 却得到 `"hello"`。这就是过多 mock 绑定实现细节：改的是读的分片，不是对外行为。

## 推荐资源

- [GoogleTest — Primer](https://google.github.io/googletest/primer.html)（GoogleTest 官方文档，英文，入门，免费）：节点推荐理由是用于核对 fixture 与断言与等价类和边界，将结论落实到本节点实验。
- 文中跟随阅读：[gMock for Dummies](https://google.github.io/googletest/gmock_for_dummies.html)（fake 与 mock 的定义，以及「mock 检查的是交互」这一用法）。

## 本课小结

单元测试保护的是可观察行为：fixture 为每个用例提供干净对象，断言写在状态和输出上，等价类要覆盖空包、截断、上限和连续包，而不能只送成功输入。本课在真实的 GoogleTest 1.16.0 里注入了一处 `>=` 边界缺陷，由 `ExactMaxPayloadAccepted` 抓住；修复后 12+7 个用例在不同 shuffle 种子下仍然通过。时间、短读和故障用 fake 注入即可；把 `read` 尺寸写进 mock，测试就会绑死实现。下一课是第 146 课 j-integration（集成与故障测试）。
