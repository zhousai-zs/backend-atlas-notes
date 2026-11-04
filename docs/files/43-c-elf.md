# 第 43/208 课：ELF、装载与符号

> 课程信息
>
> | 项目 | 内容 |
> | --- | --- |
> | 主题 | ELF、装载与符号（ELF and Loader） |
> | 领域 / 阶段 | C / 阶段 3 |
> | 子类 | 通信与运行时 |
> | 难度 | 进阶 |
> | 前置 | a-compile、c-vm |
> | 标签 | ELF and Loader、C、C++ |

ELF 记录段、节、符号和重定位信息，装载器据此映射可执行程序及共享库。对后端工程师来说，这些名词不是编译原理课上的装饰：线上服务起不来、coredump 里只有一串地址、动态库版本不匹配，最后往往都要回到 ELF 文件和装载过程来定位。

---

## 学习目标

完成本课后，你应该能够：

1. 解释 section 与 segment 的区别，说清“链接视图”和“装载视图”各自给谁用。
2. 用 `readelf -S/-l` 把一个 section 归入某个 `PT_LOAD` segment。
3. 用 `readelf -s/-r`、`nm`、`nm -D`、`objdump -d/-R` 读懂符号表、动态符号和重定位。
4. 用 `readelf -d`、`ldd`、`/proc/<pid>/maps` 对照共享库的实际装载地址。
5. 从崩溃地址计算库内偏移，并定位到具体函数或代码行。

验收标准：

- 能区分链接视图和装载视图。
- 能从崩溃地址定位到库偏移。

---

## 为什么后端要懂

### 场景一：服务起不来，报“找不到共享库”

腾讯微信公众号后台这类服务通常依赖多个内部 `.so` 和系统库。某次发布后进程直接退出：

```text
error while loading shared libraries: libwechat_util.so.1:
cannot open shared object file: No such file or directory
```

这时不要只盯着“文件明明在目录里”。需要确认：

- 程序记录的依赖名是什么：`readelf -d app | grep NEEDED`
- 程序期望从哪里找：`readelf -d app | grep -E 'RPATH|RUNPATH'`
- 当前环境实际解析到哪个路径：`ldd ./app`
- 装载器到底搜索了哪些目录：`LD_DEBUG=libs ./app`

常见根因包括：容器镜像里只拷了 `libfoo.so` 没拷 `libfoo.so.1`、`RUNPATH` 用了构建机路径、`LD_LIBRARY_PATH` 被启动脚本覆盖、`ldconfig` 缓存没有更新。

### 场景二：coredump 里只有库内地址

线上 coredump 的栈可能长这样：

```text
#0  0x00007f2a3c0034a2 in ?? () from /opt/app/lib/libfoo.so.1
```

如果库被 strip 过，gdb 不一定能直接显示函数名。你需要：

1. 在 `/proc/<pid>/maps` 或 coredump 的映射信息里找到该库的装载基址。
2. 用“崩溃地址 − 装载基址”得到库内偏移。
3. 用带调试信息的库执行 `addr2line`，或用动态符号表 `nm -D`、反汇编 `objdump -d` 找到最近的函数。

### 场景三：动态库版本不匹配

服务在构建机上正常，到了另一套基础镜像就报：

```text
./app: /lib/x86_64-linux-gnu/libc.so.6:
version `GLIBC_2.34' not found
```

或者：

```text
./app: symbol lookup error: ./app: undefined symbol: some_func
```

这可能是编译时用了较新的 glibc，运行时环境较旧；也可能是 `libfoo.so.1` 这个 soname 被替换成了不兼容的实现。需要看 `readelf -V`、`objdump -T`、`readelf -d`，而不是只看文件名。

### 场景四：缺符号与动态加载

使用 `dlopen` 的插件系统，启动时可能遇到：

```text
undefined symbol: register_handler
```

`nm -D --undefined-only`、`readelf -sW`、`ldd -r` 可以帮助判断是“依赖库没加载”“符号没有导出”还是“版本不匹配”。注意 `ldd` 在某些场景会触发装载器执行，对不可信文件要谨慎；生产环境优先用 `readelf`/`objdump` 静态查看。

---

## 0. 一张图看懂 ELF 的两套视图

```text
ELF 文件
┌──────────────────────────────┐
│ ELF header                   │  e_type、e_entry、e_phoff、e_shoff
├──────────────────────────────┤
│ Program header table         │  → segment（装载视图）
├──────────────────────────────┤
│ .interp .text .rodata ...    │  真正的文件内容
│ .data .bss .dynsym ...       │
├──────────────────────────────┤
│ Section header table         │  → section（链接视图）
└──────────────────────────────┘
```

直觉上可以这样记：

- **链接器、调试器、`readelf -S`** 关心“文件里有哪些节、每个节的类型和大小”。
- **内核和动态装载器** 关心“哪些字节要映射到哪个虚拟地址、用什么权限”。
- 程序头表（program header table）是装载器真正读的表；节头表（section header table）在运行时不是必需的。

---

## 1. section 与 segment：链接视图 vs 装载视图

### 1.1 链接视图：section

section 是 ELF 文件内部的“分区”。链接器把多个目标文件的同名或同属性 section 合并，调试器也按 section 找信息。

常见 section：

| section | 内容 | 运行时是否映射 |
| --- | --- | --- |
| `.text` | 可执行指令 | 是，通常 R-X |
| `.rodata` | 只读常量、字符串字面量 | 是，通常 R-- |
| `.data` | 已初始化的全局/静态变量 | 是，通常 RW- |
| `.bss` | 未初始化或零初始化的全局/静态变量 | 是，RW-，但文件里通常不占字节 |
| `.symtab` / `.strtab` | 静态符号表和字符串表 | 通常不映射 |
| `.dynsym` / `.dynstr` | 动态符号表和字符串表 | 需要，供动态链接器使用 |
| `.rela.text` / `.rela.dyn` / `.rela.plt` | 重定位信息 | 动态重定位需要 |
| `.plt` / `.got` / `.got.plt` | 跳转桩和地址表 | 是 |
| `.dynamic` | 动态链接标签数组 | 是 |
| `.interp` | 程序解释器路径，如 `/lib64/ld-linux-x86-64.so.2` | 是 |
| `.debug_*` | 调试信息 | 不映射 |
| `.init_array` / `.fini_array` | 构造函数/析构函数指针数组 | 是 |

用 `readelf -S` 可以查看节头表。每一行大致包含：

- `Name`：节名
- `Type`：节类型，如 `PROGBITS`、`NOBITS`、`SYMTAB`、`RELA`
- `Address`：链接时的虚拟地址；对 PIE/共享库通常是相对基址的偏移
- `Off`：在文件中的偏移
- `Size`：大小
- `Flg`：标志，常见 `A`（分配）、`W`（可写）、`X`（可执行）
- `Link` / `Info` / `Align` / `EntSize`：关联表、附加信息、对齐、表项大小

一个容易忽略的点：`.bss` 的类型通常是 `NOBITS`，文件里没有对应的零字节，装载时由内核或装载器把 `p_memsz - p_filesz` 的部分清零。

### 1.2 装载视图：segment

segment 是“要被映射到内存的一段连续区域”。装载器读的是程序头表，不是节头表。

常见 segment：

| segment | 作用 |
| --- | --- |
| `PT_LOAD` | 把文件中的一段映射到内存，是最关键的装载信息 |
| `PT_DYNAMIC` | 指向 `.dynamic`，动态链接器据此找依赖、符号表、重定位表 |
| `PT_INTERP` | 指定动态装载器路径 |
| `PT_NOTE` | 附加信息，如 build-id |
| `PT_GNU_EH_FRAME` | 异常展开信息 |
| `PT_GNU_STACK` | 栈是否可执行 |
| `PT_GNU_RELRO` | 重定位完成后将部分区域设为只读 |

`PT_LOAD` 的关键字段：

- `Offset`：文件偏移
- `VirtAddr`：虚拟地址
- `FileSiz`：文件中实际占用的大小
- `MemSiz`：内存中占用的大小
- `Flg`：权限，如 `R E`、`RW`
- `Align`：对齐，通常是页大小

`MemSiz > FileSiz` 的差额就是运行时清零的区域，典型就是 `.bss`。

`readelf -l` 的示意输出（实际地址以本机为准）：

```text
Program Headers:
  Type           Offset   VirtAddr           PhysAddr           FileSiz  MemSiz   Flg Align
  PHDR           0x000040 0x0000000000000040 0x0000000000000040 0x0002d8 0x0002d8 R   0x8
  INTERP         0x000318 0x0000000000000318 0x0000000000000318 0x00001c 0x00001c R   0x1
  LOAD           0x000000 0x0000000000000000 0x0000000000000000 0x0006b0 0x0006b0 R   0x1000
  LOAD           0x001000 0x0000000000001000 0x0000000000001000 0x0001a5 0x0001a5 R E 0x1000
  LOAD           0x002000 0x0000000000002000 0x0000000000002000 0x000120 0x000120 R   0x1000
  LOAD           0x002dd0 0x0000000000003dd0 0x0000000000003dd0 0x000250 0x000258 RW  0x1000
  DYNAMIC        0x002dd0 0x0000000000003dd0 0x0000000000003dd0 0x0001f0 0x0001f0 RW  0x8
```

注意 `Offset` 和 `VirtAddr` 不一定相等，尤其是后面的 RW segment。计算装载基址时不能想当然，后面会给出稳妥公式。

### 1.3 两者的关系

| 维度 | section | segment |
| --- | --- | --- |
| 使用者 | 链接器、调试器、`readelf -S` | 内核、动态装载器、`readelf -l` |
| 典型单位 | `.text`、`.data`、`.dynsym` | `PT_LOAD`、`PT_DYNAMIC` |
| 运行时是否必需 | 节头表不必需；部分节内容仍需要 | 程序头表必需 |
| 数量关系 | 多个 section 可以属于一个 segment | 一个 segment 可以包含多个 section |
| strip 影响 | 可删除 `.symtab`、`.debug_*` 等 | 不能删除程序头表 |

例如 `.text`、`.rodata` 可能一起落在第一个可执行 `PT_LOAD` 中；`.data`、`.bss`、`.got` 可能落在可写 `PT_LOAD` 中。非分配的 section（如 `.symtab`、`.comment`、`.debug_info`）不属于任何 `PT_LOAD`。

### 1.4 命令对照

```bash
# 链接视图：看节头表
readelf -SW ./demo | grep -E '\.(text|rodata|data|bss|dynsym|rela|plt|got|dynamic)\b'

# 装载视图：看程序头表
readelf -lW ./demo

# 只看 LOAD
readelf -lW ./demo | grep LOAD
```

判断方法：

1. 从 `readelf -S` 找到 `.text` 的 `Address` 和 `Off`。
2. 从 `readelf -l` 找哪个 `LOAD` 的虚拟地址范围覆盖它。
3. 确认该 `LOAD` 的权限包含 `E`，通常就是代码段。
4. 再用 `readelf -S` 看 `.data`、`.bss`，它们通常落在 `RW` 的 `LOAD` 中。

---

## 2. 符号表与重定位

### 2.1 静态符号表与动态符号表

ELF 里常见两张符号表：

| 表 | 对应 section | 主要用途 | strip 后 |
| --- | --- | --- | --- |
| 静态符号表 | `.symtab` + `.strtab` | 链接、调试、`nm` 默认查看 | 通常被删除 |
| 动态符号表 | `.dynsym` + `.dynstr` | 动态链接、`dlopen`/`dlsym` | 通常保留 |

这是本课最重要的误区之一：**“strip 后 `nm` 没输出”不等于“程序没有符号”**。`strip --strip-all` 通常删除 `.symtab` 和调试节，但动态链接器仍然需要 `.dynsym`，所以 `.dynsym` 一般会保留。此时 `nm` 可能说没有符号，但 `nm -D` 仍然能看到动态符号。

`readelf -s` 的常见列：

- `Num`：符号编号
- `Value`：符号值。对可执行文件/共享库通常是虚拟地址；对 ET_DYN（PIE、共享库）是相对装载基址的偏移
- `Size`：符号大小
- `Type`：`FUNC`、`OBJECT`、`TLS`、`IFUNC`、`NOTYPE` 等
- `Bind`：`LOCAL`、`GLOBAL`、`WEAK`
- `Vis`：`DEFAULT`、`HIDDEN`、`PROTECTED`
- `Ndx`：所在 section 索引；`UND` 表示未定义，`ABS` 表示绝对值
- `Name`：符号名

### 2.2 一个具体例子

```c
/* libmathx.c */
int counter = 7;

int add(int a, int b) {
    return a + b + counter;
}

void boom(void) {
    volatile int *p = 0;
    *p = 42;          /* 故意触发 SIGSEGV，后面实践会用到 */
}
```

编译成共享库：

```bash
gcc -fPIC -shared -g -Wl,-soname,libmathx.so.1 \
    -o libmathx.so.1 libmathx.c
ln -sf libmathx.so.1 libmathx.so
```

查看动态符号：

```bash
nm -D --defined-only ./libmathx.so.1
```

示意输出：

```text
0000000000001119 T add
0000000000004028 D counter
0000000000001136 T boom
```

这里的 `0000000000001119` 不是进程运行时的绝对地址，而是共享库内的偏移。程序运行时，真实地址约等于：

```text
runtime_addr = load_base + symbol_value
```

查看未定义符号：

```bash
nm -D --undefined-only ./demo
```

示意输出：

```text
                 U add
                 U boom
                 U printf@GLIBC_2.2.5
```

`U` 表示这个符号在当前文件里没有定义，需要链接时或装载时由其他库提供。

### 2.3 重定位是什么

编译器生成代码时，如果遇到外部函数或全局变量，往往不能直接写死地址：

- 共享库会被映射到不同地址（ASLR）。
- 同一份共享库要被多个进程共享，不能把某个进程的绝对地址写进代码页。
- 外部符号的最终地址要到链接时甚至装载时才能确定。

于是编译器先留下占位，并生成一条“重定位记录”：**在哪个位置、按什么规则、填入哪个符号的地址**。

重定位发生的时机大致有三类：

1. **链接时**：静态链接，链接器直接算出地址并改写指令/数据。
2. **装载时**：动态链接器在程序启动时处理 `.rela.dyn`、`.rela.plt` 等。
3. **延迟绑定时**：函数第一次被调用时再解析，典型就是 PLT/GOT。

查看重定位：

```bash
readelf -rW ./demo
readelf -rW ./libmathx.so.1
objdump -R ./demo
```

`readelf -r` 的示意输出：

```text
Relocation section '.rela.dyn' at offset 0x... contains 8 entries:
  Offset          Info           Type           Sym. Value    Sym. Name + Addend
0000000000003fe0  0000000000000008 R_X86_64_RELATIVE                    1120
...

Relocation section '.rela.plt' at offset 0x... contains 2 entries:
  Offset          Info           Type           Sym. Value    Sym. Name + Addend
0000000000003fd8  0000000000000007 R_X86_64_JUMP_SLOT 0000000000000000 printf@GLIBC_2.2.5 + 0
```

关键字段：

- `Offset`：要修补的位置，通常是 GOT 表项或代码中的地址字段。
- `Type`：重定位类型，决定“怎么算”。
- `Sym. Value` / `Sym. Name`：目标符号。
- `Addend`：加数。

x86-64 上常见类型：

| 类型 | 常见用途 |
| --- | --- |
| `R_X86_64_PC32` | PC 相对寻址，常用于本地数据/函数 |
| `R_X86_64_PLT32` | 调用外部函数，链接器可能生成 PLT 跳转 |
| `R_X86_64_64` | 绝对 64 位地址 |
| `R_X86_64_GLOB_DAT` | GOT 中的全局数据地址 |
| `R_X86_64_JUMP_SLOT` | GOT 中的函数地址，配合 PLT 延迟绑定 |
| `R_X86_64_RELATIVE` | 基址相关地址，运行时填 `load_base + addend` |

不同架构的重定位类型名不同，例如 ARM64 是 `R_AARCH64_*`。本课命令以 x86-64 Linux 为主。

### 2.4 重定位与 PIE/共享库

对 PIE 可执行文件和共享库（都是 ET_DYN），很多地址是“相对基址”的。于是有：

```text
runtime_addr = load_base + st_value          # 对普通符号
runtime_value = load_base + addend           # 对 R_X86_64_RELATIVE
offset_in_library = runtime_addr - load_base
```

这就是后面从崩溃地址还原函数的基础。

---

## 3. PLT/GOT 与动态依赖

### 3.1 为什么需要 PLT/GOT

共享库的代码页要被多个进程共享，不能因为某个进程的装载地址不同就修改代码页。因此，代码访问外部函数/数据时，通常先经过一张“地址表”：

- **GOT**（Global Offset Table）：存放运行时地址的表，位于可写数据区。
- **PLT**（Procedure Linkage Table）：一小段跳转桩，代码调用 `foo@plt`，桩再从 GOT 里取 `foo` 的真实地址并跳过去。

直觉图：

```text
call add@plt
     |
     v
add@plt: jmp *add@got(%rip)
     |              ^
     |              |
     +--- 第一次调用：跳到解析器，解析后把真实地址写回 GOT
```

### 3.2 第一次调用发生了什么

以延迟绑定（lazy binding）为例：

1. 程序调用 `add@plt`。
2. PLT 桩执行 `jmp *GOT_entry`。
3. 第一次调用时，GOT 表项还没有真实地址，指回 PLT 的下一条指令。
4. 该指令把重定位索引压栈，跳到 PLT0。
5. PLT0 跳到动态链接器的解析例程 `_dl_runtime_resolve`。
6. 解析器找到 `add` 的真实地址，写入 GOT。
7. 之后再次调用 `add@plt`，直接通过 GOT 跳到真实函数。

反汇编示意：

```asm
0000000000001030 <add@plt>:
    1030: ff 25 02 2f 00 00    jmp    *0x2f02(%rip)   # GOT 表项
    1036: 68 00 00 00 00       push   $0x0            # 重定位索引
    103b: e9 c0 ff ff ff       jmp    1020 <plt0>
```

查看方法：

```bash
objdump -d ./demo | grep -A8 '<add@plt>'
objdump -R ./demo | grep -E 'add|boom|printf'
readelf -rW ./demo | grep -A10 'rela.plt'
```

注意：GOT 不只给函数用。全局数据也可能通过 `GLOB_DAT` 重定位写入 GOT，所以不要把 GOT 理解成“函数专用表”。

### 3.3 延迟绑定与立即绑定

- 默认常见是延迟绑定：启动快，第一次调用某个函数时有解析开销。
- 立即绑定：启动时解析全部 PLT 符号，启动稍慢，但运行期行为更确定。

控制方式：

```bash
LD_BIND_NOW=1 ./demo
```

链接时也可以：

```bash
gcc -Wl,-z,now -Wl,-z,relro -o demo demo.c ...
```

`-z now` 常与 `-z relro` 一起使用，形成 Full RELRO：重定位完成后 GOT 变为只读，降低 GOT 被篡改的风险。代价是启动时要做更多重定位。

### 3.4 动态依赖：`.dynamic` 与 `DT_NEEDED`

动态链接器需要知道程序依赖哪些库。这些信息在 `.dynamic` 节里，由 `PT_DYNAMIC` segment 指向。

```bash
readelf -dW ./demo
objdump -p ./demo | grep -E 'NEEDED|RPATH|RUNPATH|SONAME'
```

示意输出：

```text
Dynamic section at offset 0x2dd0 contains 29 entries:
  Tag        Type                         Name/Value
 0x0000000000000001 (NEEDED)             Shared library: [libmathx.so.1]
 0x0000000000000001 (NEEDED)             Shared library: [libc.so.6]
 0x000000000000001d (RUNPATH)            Library runpath: [$ORIGIN]
 0x000000000000000e (SONAME)             Library soname: [libmathx.so.1]
```

常见标签：

| 标签 | 含义 |
| --- | --- |
| `DT_NEEDED` | 直接依赖的共享库 soname |
| `DT_SONAME` | 当前共享库自己的 soname |
| `DT_RPATH` | 旧式搜索路径 |
| `DT_RUNPATH` | 新式搜索路径；存在时通常忽略 `DT_RPATH` |
| `DT_INIT` / `DT_FINI` | 初始化/结束函数 |
| `DT_INIT_ARRAY` / `DT_FINI_ARRAY` | 构造函数/析构函数数组 |
| `DT_STRTAB` / `DT_SYMTAB` | 动态字符串表/符号表地址 |
| `DT_RELA` / `DT_PLTREL` | 重定位表信息 |

`ldd` 会显示当前环境下实际解析到的路径：

```bash
ldd ./demo
```

示意输出：

```text
linux-vdso.so.1 (...)
libmathx.so.1 => /home/you/elf-lab/libmathx.so.1 (0x00007f2a3c000000)
libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f2a3be00000)
/lib64/ld-linux-x86-64.so.2 (...)
```

但 `ldd` 只代表“当前 shell 环境下，装载器会怎么解析”。进程真正加载了什么，要以 `/proc/<pid>/maps` 为准，尤其是在容器、chroot、不同 mount namespace 或启动脚本修改了环境变量时。

### 3.5 搜索路径与版本风险

glibc 的共享库搜索顺序（简化，具体以 `ld.so(8)` 和发行版配置为准）：

1. `DT_RPATH`（如果不存在 `DT_RUNPATH`）
2. `LD_LIBRARY_PATH`
3. `DT_RUNPATH`
4. `/etc/ld.so.cache`
5. 默认目录，如 `/lib`、`/usr/lib` 及多架构目录

`$ORIGIN` 在 `RPATH`/`RUNPATH` 中表示“可执行文件或共享库所在目录”，常用于把库和程序放在同一发布目录：

```bash
gcc -o demo demo.c -L. -lmathx -Wl,-rpath,'$ORIGIN'
```

注意单引号：要让链接器看到字面量 `$ORIGIN`，不要让 shell 先展开。

共享库还有 soname 和符号版本：

- soname 例如 `libfoo.so.1`，表示 ABI 主版本。
- 实际文件可能是 `libfoo.so.1.2.3`，通过符号链接提供 `libfoo.so.1`。
- 符号版本例如 `foo@VERS_1.2`，用于同一库内多版本符号共存。

查看版本信息：

```bash
readelf --version-info ./libfoo.so.1
objdump -T ./libfoo.so.1 | grep foo
```

版本不匹配的典型错误：

```text
version `GLIBC_2.34' not found
undefined symbol: foo@VERS_1.2
```

这类问题不是“把 `.so` 拷过去”就能解决的，必须保证运行环境的 ABI 与构建环境兼容。

---

## 4. 常见误区

1. **“strip 后没有符号，所以程序不能运行”**
   错。`strip` 通常删除 `.symtab` 和调试信息，但 `.dynsym` 仍保留，动态链接器靠它工作。`nm` 看不到静态符号时，试试 `nm -D`、`readelf -sW`、`objdump -T`。

2. **“`readelf -S` 看不到节头，程序就不能装载”**
   错。运行时装载器读的是程序头表。节头表主要服务于链接和调试；某些裁剪/加固场景可以去掉节头表，程序仍可运行。

3. **“`ldd` 的输出就是进程实际加载的库”**
   不一定。`ldd` 受当前环境变量、工作目录、容器挂载、chroot 等影响；进程运行后应看 `/proc/<pid>/maps`。另外，对不可信二进制不要随便 `ldd`，它可能触发装载器执行。

4. **“coredump 里的地址就是文件偏移”**
   错。那是进程的虚拟地址。需要先减装载基址，得到库内偏移，再用 `addr2line`、`nm -D`、`objdump -d` 定位。

5. **“`nm` 显示的地址就是运行时地址”**
   对 PIE 和共享库，通常是相对基址的偏移。运行时地址 = 装载基址 + 符号值。

6. **“共享库放在可执行文件同目录就一定能找到”**
   不一定。默认搜索路径不包含当前目录；需要 `RPATH`/`RUNPATH`（例如 `$ORIGIN`）、`LD_LIBRARY_PATH`、`ldconfig` 缓存或系统目录。

7. **“`.bss` 在文件里占很大空间”**
   通常不占。`.bss` 是 `NOBITS`，文件里只有大小信息，运行时由 `MemSiz - FileSiz` 清零。

8. **“GOT 只和函数有关”**
   错。全局数据也可能通过 GOT 和 `GLOB_DAT` 重定位访问。

9. **“动态库更新总是安全的”**
   错。soname 相同不代表 ABI 兼容；符号版本、结构体布局、异常处理、线程模型都可能变化。

10. **“只要编译通过，运行时就不会缺符号”**
    链接时可能允许未定义符号（尤其是共享库），运行时才由装载器解析；`dlopen` 场景更晚。需要检查 `DT_NEEDED`、`nm -D --undefined-only` 和实际运行环境。

---

## 5. 设计权衡

### 5.1 共享库 vs 静态链接

共享库的收益：

- 多个进程共享同一份代码页，节省物理内存。
- 磁盘上只存一份，发布包更小。
- 修复库后可以独立更新，不必重编所有程序。
- 插件化、按需加载更方便。

共享库的代价：

- 引入版本、soname、符号版本和搜索路径问题。
- 启动时需要装载、重定位，依赖越多启动越慢。
- 运行环境必须提供兼容的依赖，容器镜像和基础镜像升级都可能破坏兼容性。
- “依赖地狱”与不可控的间接依赖。

静态链接的收益是自包含、可预测、部署简单；代价是二进制更大、多个进程不能共享代码页、库更新需要重新构建和发布。

### 5.2 PIC 与地址无关

PIC（Position-Independent Code）让共享库可以被映射到任意地址，并支持 ASLR。代价是少量间接访问（GOT）和寄存器/指令开销。对绝大多数后端服务，这点开销通常远小于它带来的部署和共享收益。

### 5.3 延迟绑定 vs 立即绑定

- 延迟绑定：启动快，第一次调用有解析开销，运行期 GOT 可写。
- 立即绑定 + Full RELRO：启动时完成重定位，运行期 GOT 只读，行为更确定、安全性更好，但启动稍慢。

### 5.4 strip vs 可诊断性

strip 能减小体积、减少信息暴露，但会丢失 `.symtab` 和调试信息。生产环境常见做法是：

- 发布 stripped 的二进制/库；
- 单独保存带调试信息的文件，或用 build-id 关联调试包；
- 至少保留 `.dynsym`，否则动态链接会出问题。

### 5.5 节头表 vs 程序头表

程序头表是运行时必需；节头表可以裁剪。裁剪节头表能减小体积、增加一点逆向难度，但会让部分工具失去信息。不要为了“瘦身”误删程序头表或动态符号。

---

## 6. 动手实践：对照共享库装载地址

本实践的目标：

1. 用 `readelf -S` 和 `readelf -l` 区分链接视图与装载视图。
2. 用 `nm -D`、`readelf -r`、`objdump -d/-R` 观察符号、重定位和 PLT/GOT。
3. 用 `/proc/<pid>/maps` 找到共享库装载基址，验证“基址 + 符号偏移 = 运行时地址”。
4. 从一个人为构造的崩溃地址，计算库内偏移并定位函数。

> 以下命令以 x86-64 Linux、GNU binutils、glibc 为基准。不同发行版、编译器版本和架构的输出字段可能不同，但方法一致。

### 6.1 准备实验目录

```bash
mkdir -p ~/elf-lab
cd ~/elf-lab
```

写入共享库源码：

```c
/* libmathx.c */
int counter = 7;

int add(int a, int b) {
    return a + b + counter;
}

void boom(void) {
    volatile int *p = 0;
    *p = 42;          /* 故意触发 SIGSEGV */
}
```

写入主程序：

```c
/* demo.c */
#include <stdio.h>
#include <unistd.h>

extern int add(int, int);
extern void boom(void);

int main(int argc, char **argv) {
    printf("add(2,3)=%d\n", add(2, 3));
    fflush(stdout);

    if (argc > 1) {
        boom();       /* 带参数运行时立即崩溃 */
    }

    sleep(30);        /* 不带参数时留出时间观察 /proc/<pid>/maps */
    return 0;
}
```

编译：

```bash
gcc -fPIC -shared -g -Wl,-soname,libmathx.so.1 \
    -o libmathx.so.1 libmathx.c

ln -sf libmathx.so.1 libmathx.so

gcc -g -o demo demo.c -L. -lmathx -Wl,-rpath,'$ORIGIN'
```

检查文件类型：

```bash
readelf -h ./demo | grep 'Type:'
```

如果输出 `DYN (Position-Independent Executable file)`，说明是 PIE；如果输出 `EXEC`，说明是非 PIE。两者都能做本实验，只是地址计算方式略有不同。

### 6.2 步骤一：看链接视图

```bash
readelf -SW ./demo | grep -E '\.(text|rodata|data|bss|dynsym|dynstr|rela|plt|got|dynamic)\b'
```

记录：

- `.text` 的 `Address` 和 `Off`
- `.data`、`.bss` 的 `Address`
- `.dynsym`、`.rela.dyn`、`.rela.plt` 是否存在

然后看装载视图：

```bash
readelf -lW ./demo
readelf -lW ./demo | grep LOAD
```

完成下面的对照表：

| 观察项 | 命令 | 你的记录 |
| --- | --- | --- |
| `.text` 属于哪个 `LOAD` | `readelf -S` + `readelf -l` |  |
| 该 `LOAD` 的权限 | `readelf -l` |  |
| `.data/.bss` 属于哪个 `LOAD` | 同上 |  |
| 是否存在 `PT_INTERP` | `readelf -l` |  |
| 是否存在 `PT_DYNAMIC` | `readelf -l` |  |

判断标准：`.text` 应该落在带 `E` 权限的 `LOAD` 中；`.data`、`.bss` 应该落在 `RW` 的 `LOAD` 中。如果你能解释“为什么 `readelf -S` 有很多节，而 `readelf -l` 只有几个 `LOAD`”，就已经掌握了链接视图与装载视图的区别。

### 6.3 步骤二：看符号表

```bash
nm -D --defined-only ./libmathx.so.1
nm -D --undefined-only ./demo
readelf -sW ./libmathx.so.1 | grep -E ' add$| counter$| boom$'
objdump -T ./libmathx.so.1 | grep -E 'add|counter|boom'
```

记录 `add` 的 `Value`。例如：

```text
0000000000001119 T add
```

这里的 `0x1119` 是共享库内的偏移，不是绝对地址。后面用 `/proc/<pid>/maps` 验证。

再做一次 strip 实验：

```bash
cp libmathx.so.1 libmathx.stripped.so.1
strip --strip-all libmathx.stripped.so.1

nm libmathx.stripped.so.1            # 可能提示 no symbols
nm -D libmathx.stripped.so.1         # 动态符号通常仍在
readelf -sW libmathx.stripped.so.1 | head
```

观察结果：静态符号表可能没了，但 `.dynsym` 仍在。这个实验直接对应本课的常见误区。

### 6.4 步骤三：看动态依赖与重定位

```bash
readelf -dW ./demo | grep -E 'NEEDED|RPATH|RUNPATH|SONAME'
objdump -p ./demo | grep -E 'NEEDED|RPATH|RUNPATH|SONAME'
ldd ./demo
```

确认：

- `NEEDED` 里是否有 `libmathx.so.1`
- `RUNPATH` 是否是 `$ORIGIN`
- `ldd` 是否解析到了 `~/elf-lab/libmathx.so.1`

看重定位：

```bash
readelf -rW ./demo | head -60
objdump -R ./demo | grep -E 'add|boom|printf'
```

看 PLT：

```bash
objdump -d ./demo | grep -A8 '<add@plt>'
objdump -d ./demo | grep -A8 '<boom@plt>'
```

你应该能看到类似：

```asm
0000000000001030 <add@plt>:
    1030: ff 25 ...    jmp    *...(%rip)
    1036: 68 ...       push   $0x...
    103b: e9 ...       jmp    ...
```

以及 `objdump -R` 中的 `R_X86_64_JUMP_SLOT`。如果链接器做了优化，某个调用可能没有走 PLT，这也是正常现象；换一个外部函数（如 `printf`）再观察即可。

### 6.5 步骤四：用 `/proc/<pid>/maps` 对照装载地址

运行程序并让它睡眠：

```bash
./demo &
PID=$!
cat /proc/$PID/maps | grep -E 'demo|libmathx|libc'
```

示意输出（地址每次都会变）：

```text
7f2a3c000000-7f2a3c001000 r--p 00000000 ... /home/you/elf-lab/libmathx.so.1
7f2a3c001000-7f2a3c005000 r-xp 00001000 ... /home/you/elf-lab/libmathx.so.1
7f2a3c005000-7f2a3c006000 r--p 00005000 ... /home/you/elf-lab/libmathx.so.1
7f2a3c006000-7f2a3c007000 r--p 00006000 ... /home/you/elf-lab/libmathx.so.1
7f2a3c007000-7f2a3c008000 rw-p 00007000 ... /home/you/elf-lab/libmathx.so.1
```

取可执行映射那一行：

```text
7f2a3c001000-7f2a3c005000 r-xp 00001000 ... /home/you/elf-lab/libmathx.so.1
```

计算装载基址：

```text
load_base = mapping_start - mapping_offset
          = 0x7f2a3c001000 - 0x1000
          = 0x7f2a3c000000
```

再验证 `add` 的运行时地址：

```text
runtime_add = load_base + symbol_value
            = 0x7f2a3c000000 + 0x1119
            = 0x7f2a3c001119
```

可以用 gdb 验证（可选）：

```bash
gdb -p $PID -batch -ex 'info symbol 0x7f2a3c001119'
```

如果 gdb 没装，用 `readelf -sW`、`nm -D` 和 maps 的对照也足够。观察完后结束进程：

```bash
kill $PID
```

> 关于公式的边界：上面使用了 `load_base = mapping_start - mapping_offset`，这在常见的 x86-64 ELF 中成立，因为 `PT_LOAD` 的 `p_vaddr` 与 `p_offset` 通常相等（或相差同一个常量）。如果你遇到特殊链接脚本或非标准布局，先用 `readelf -l` 找到该映射对应的 `LOAD`，再用 `p_vaddr` 修正。稳妥原则是：**不要假设文件偏移等于虚拟地址，先查程序头表。**

### 6.6 步骤五：从崩溃地址定位到库偏移

先确认 core dump 是否可用：

```bash
ulimit -c unlimited
cat /proc/sys/kernel/core_pattern
```

如果系统允许生成 core，运行：

```bash
./demo crash
```

然后用 gdb 打开 core：

```bash
gdb -q ./demo core
```

在 gdb 中：

```gdb
bt
p/x $rip
info proc mappings
info symbol $rip
```

如果系统没有生成 core，也可以直接在 gdb 下运行：

```bash
gdb -q --args ./demo crash
```

```gdb
run
bt
p/x $rip
info proc mappings
```

假设 gdb 给出：

```text
$rip = 0x7f2a3c0034a2
```

并且映射中有：

```text
7f2a3c001000-7f2a3c005000 r-xp 00001000 ... /home/you/elf-lab/libmathx.so.1
```

那么：

```text
load_base = 0x7f2a3c001000 - 0x1000
          = 0x7f2a3c000000

offset = 0x7f2a3c0034a2 - 0x7f2a3c000000
       = 0x34a2
```

用偏移定位函数：

```bash
addr2line -f -C -e ./libmathx.so.1 0x34a2
```

如果库带调试信息，会输出类似：

```text
boom
/home/you/elf-lab/libmathx.c:9
```

如果库被 strip 了，`addr2line` 可能只能给出 `??`。这时用动态符号和反汇编：

```bash
nm -D -n --defined-only ./libmathx.so.1
objdump -d --start-address=0x3400 --stop-address=0x3500 ./libmathx.so.1
```

在 `nm -D -n` 的输出中找到小于等于 `0x34a2` 的最近符号，通常就是 `boom`；再用 `objdump -d` 看该地址附近的指令，确认是空指针写入。

如果要模拟“线上 stripped 库”，可以这样做：

```bash
mkdir -p stripped
cp libmathx.so.1 stripped/
strip --strip-all stripped/libmathx.so.1

LD_LIBRARY_PATH=./stripped ./demo crash
```

再用 gdb 加载 `./stripped/libmathx.so.1` 观察。此时静态符号和调试信息少了，但 `.dynsym` 通常还在，仍然可以用偏移 + `nm -D`/`objdump -d` 定位。

### 6.7 验收清单

完成实践后，逐项检查：

- [ ] 我能解释 `readelf -S` 和 `readelf -l` 分别展示什么。
- [ ] 我能指出 `.text` 落在哪个 `PT_LOAD`，以及该 segment 的权限。
- [ ] 我能说明 `.symtab` 和 `.dynsym` 的区别，并演示 strip 后 `nm -D` 仍可用。
- [ ] 我能从 `/proc/<pid>/maps` 计算共享库装载基址。
- [ ] 我能验证 `load_base + symbol_value` 等于符号的运行时地址。
- [ ] 我能从崩溃地址计算库内偏移，并用 `addr2line` 或 `nm -D` + `objdump -d` 找到函数。
- [ ] 我能用 `readelf -d` 和 `ldd` 解释一个“找不到共享库”的问题。

---

## 7. 自测题

### 题 1：strip 与动态符号

程序执行 `strip --strip-all` 后，`nm demo` 提示没有符号。这是否意味着程序没有符号、动态链接会失败？为什么？

**答案：** 不意味着。`strip` 通常删除 `.symtab`、`.strtab` 和调试节，但动态链接器需要的是 `.dynsym`/`.dynstr`，这些通常会保留。`nm` 默认看静态符号表，所以可能没有输出；用 `nm -D`、`readelf -sW` 或 `objdump -T` 仍能看到动态符号。只有当动态符号表或动态依赖被破坏时，装载才会失败。

### 题 2：从崩溃地址算库内偏移

某进程的 `/proc/<pid>/maps` 中有：

```text
7f2a3c001000-7f2a3c005000 r-xp 00001000 ... /opt/app/lib/libfoo.so.1
```

崩溃时 `$rip = 0x7f2a3c0034a2`。请计算装载基址和库内偏移，并说明如何定位函数。

**答案：**

```text
load_base = 0x7f2a3c001000 - 0x1000 = 0x7f2a3c000000
offset    = 0x7f2a3c0034a2 - 0x7f2a3c000000 = 0x34a2
```

定位方法：

```bash
addr2line -f -C -e /opt/app/lib/libfoo.so.1 0x34a2
```

如果库被 strip，可以先用 `readelf -l` 确认 `p_vaddr`/`p_offset` 的关系，再用 `nm -D -n --defined-only` 找最近的动态符号，最后用 `objdump -d --start-address=... --stop-address=...` 查看该偏移附近的指令。

### 题 3：链接视图与装载视图

为什么 `readelf -S` 能看到很多 section，而 `readelf -l` 只有几个 `LOAD`？运行时 `.dynsym` 是怎么被找到的？

**答案：** section 是链接/调试视图，描述文件内部的分区；多个 section 会被合并进同一个 `PT_LOAD` segment，所以 `LOAD` 数量少。运行时装载器读程序头表，按 `PT_LOAD` 把文件映射到内存；节头表不是必需的。`.dynsym` 的内容位于某个被映射的 `PT_LOAD` 中，`PT_DYNAMIC` 指向的 `.dynamic` 里记录了 `DT_SYMTAB`、`DT_STRTAB` 等地址，动态链接器据此找到动态符号表，而不依赖节头表。

---

## 8. 推荐资源

- [ELF、装载与符号：Linux man-pages](https://man7.org/linux/man-pages/man5/elf.5.html)（man7.org，英文，文档，进阶，免费）

  重点阅读 section 与 segment、符号表、重定位相关定义，并用本课实验对照其语义和边界。学习时建议沿资源中的定义、示例与错误处理路径核对实现，并记录适用版本及边界条件；本课命令以 x86-64 Linux + GNU binutils/glibc 为基准，ARM64 等架构的重定位类型名和输出格式会有所不同。

---

## 9. 本课小结

- ELF 有两套视图：section 面向链接/调试，segment 面向装载；运行时真正重要的是程序头表。
- 符号表分静态 `.symtab` 和动态 `.dynsym`；strip 通常删除前者，但动态链接仍依赖后者。
- 重定位决定“在哪个位置填入哪个符号的地址”；PIE/共享库中大量地址是“基址 + 偏移”。
- PLT/GOT 让共享库代码保持地址无关；延迟绑定把函数解析推迟到第一次调用。
- 动态依赖由 `DT_NEEDED`、`RPATH`/`RUNPATH`、soname 和符号版本共同决定，搜索路径和版本是线上故障的高发区。
- 排查 coredump 的通用公式：`库内偏移 = 崩溃地址 - 装载基址`，再用 `addr2line`、`nm -D`、`objdump -d` 还原函数。
