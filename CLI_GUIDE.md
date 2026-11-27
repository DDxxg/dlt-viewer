# DLT AI CLI 使用指南

新用户先看 [QUICK_START.md](QUICK_START.md)。
让 AI 调用工具前，要求它读取 [AGENTS.md](AGENTS.md) 和 [AI_AGENT_GUIDE.md](AI_AGENT_GUIDE.md)。
本指南是完整参数说明；AI 手册提供逐步执行、退出码处理、取证预算和安全边界。
真实输入及 artifacts 中的全部报告仅保留本地，不提交或随源码分发，见 [DATA_HANDLING.md](DATA_HANDLING.md)。

## 1. 实现策略与当前状态

当前目录是 DLT Viewer 2.30.1 的 Windows **二进制发行包**，不是完整主程序源码。
因此没有修改或替换原来的 Viewer/Commander，而是复用自带的 `dlt-commander.exe`
解析与插件能力，在其上增加面向 AI、脚本和 CI 的 CLI。原 GUI 和 DLL 不变。

入口：`dlt-cli.ps1`；Python 入口：`dlt_cli.py`。只需要 **Python 3.10+**，无需 pip 安装依赖。
运行中不弹出 DLT GUI，也不调用 LLM 或上传日志。可从任意工作目录调用，日志相对路径以调用者目录为准。

提供六个命令：

| 命令 | 用途 | 标准输出 |
| --- | --- | --- |
| `doctor` | 检查 Python 和原生命令行 | 一个 JSON 对象 |
| `read` | 筛选并读取日志 | 每条消息一个 JSON 对象，JSONL |
| `summary` | 按级别、来源、上下文汇总 | 一个 JSON 对象 |
| `check` | 使用规则判定测试通过/失败 | 一个 JSON 报告 |
| `capture` | 有时限地从 TCP 接收 DLTv1 | 一个 JSON 采集结果，保存原始 DLT |
| `run` | 建立采集 → 可选触发测试 → 解码 → 断言 | 一个 JSON 闭环报告 |

**公开可复现验证**：真实原生工具的合成 DLT 离线解析、本地模拟 TCP 服务的采集/触发/判定闭环。
真实数据验收证据仅存本地，不作为源码包依赖。
**尚未验证**：你的真实 ECU 联通性、真实硬件测试触发命令和业务测试通过标准。
这是一版闭环基础设施，不代表已经完成整车/ECU 软件测试或烧录、UDS、CAN 控制等硬件集成。

## 2. 快速开始（Windows PowerShell）

在当前发行包目录中：

```powershell
.\dlt-cli.ps1 doctor
.\dlt-cli.ps1 --help
.\dlt-cli.ps1 read --help
```

doctor 使用一条合成 DLTv1 验证实际 UTF-8 导出、消息数、payload 和元数据；
返回 capability_probe.status=passed 才成功。原生帮助为空不导致失败。
缺失输出、乱码、错误元数据、超时或非零退出仍报错；不证明 FIBEX/ECU 联通性或软件验收通过。

不依赖 ECU 的演示：

```powershell
py -3 .\examples\demo.py
.\dlt-cli.ps1 read .\examples\demo-output\sample.dlt --limit 5
.\dlt-cli.ps1 summary .\examples\demo-output\sample.dlt
.\dlt-cli.ps1 check .\examples\demo-output\sample.dlt --rules .\examples\smoke.rules.json
$LASTEXITCODE
```

演示生成的是**合成 DLT 日志**，包括中文、引号、逗号和预期测试标记。
默认写入 examples/demo-output；重复演示请通过 `--output-dir` 指定新目录，避免覆盖。

生产日志建议由 CLI 直接写 UTF-8 文件，避免 Windows PowerShell 5.1 的 `>` 默认 UTF-16 编码：

```powershell
.\dlt-cli.ps1 read C:\logs\test.dlt --output C:\logs\records.jsonl
.\dlt-cli.ps1 summary C:\logs\test.dlt --output C:\logs\summary.json
```

若系统脚本执行策略禁止运行 PS1，可按单位安全政策处理，或直接使用 Python 入口：

```powershell
py -3 .\dlt_cli.py doctor
```

## 3. AI 读取、搜索与摘要

```powershell
# 限制上下文大小；先摘要，后取证。
.\dlt-cli.ps1 summary C:\logs\test.dlt --evidence-limit 5
.\dlt-cli.ps1 read C:\logs\test.dlt --max-level warn --limit 50

# 精确匹配 ID；同一参数重复是 OR，不同参数之间是 AND。
.\dlt-cli.ps1 read C:\logs\test.dlt --apid APP1 --apid APP2 --ctid DIAG --limit 100

# 内容过滤支持中文和正则。
.\dlt-cli.ps1 read C:\logs\test.dlt --contains '测试完成'
.\dlt-cli.ps1 read C:\logs\test.dlt --regex 'timeout|assert|exception' --ignore-case

# 可以输入多个文件；保持输入文件顺序及每个文件内部顺序，不自动按时间排序。
.\dlt-cli.ps1 summary C:\logs\part1.dlt C:\logs\part2.dlt
```

日志级别从严重到轻微：fatal、error、warn、info、debug、verbose。
`--max-level warn` 表示 fatal/error/warn，并且只匹配 type=log。
`--ignore-case` 只作用于内容过滤，不改变 ID 匹配方式。
`read` 没有匹配项时返回 0 和空输出，不能把它当作测试通过依据；判定请用 `check`。

JSONL 消息结构示例：

```json
{"schema_version":1,"source":"C:\\logs\\test.dlt","index":0,"time":"2023/11/15 06:13:20.123456","timestamp":0.0,"counter":0,"ecu":"ECU1","apid":"TEST","ctid":"MAIN","session_id":0,"type":"log","level":"info","mode":"verbose","argument_count":1,"payload":"BOOT_OK","decoded":true}
```

字段说明：

- `source`：原始输入文件绝对路径；`index`：原生导出索引，通常从 0 开始，每个文件独立。
- `source_offset`：消息 storage header 在原始 DLT 中的字节偏移。
- `message_id`：原始 Non Verbose 的 Message ID；原本 Verbose/control 消息为 null。
- `service_id`：原始 control 服务编号；非 control 为 null。未知服务仅保留编号，不猜测含义。
- `time`：原生工具渲染的 storage 时间，**本机当地时间且无时区偏移**；不要当作 UTC。
- `timestamp`：ECU DLT timestamp 的秒值，不是 Unix 时间；可能回绕/复位。
- `counter`：DLT 8-bit message counter，可回绕，不保证跨文件连续。
- `ecu` / `apid` / `ctid`：上下文标识；缺失时可能为空字符串。
- `type` / `level`：原生类型/子类型；非 log 消息的 level 不是日志严重度。
- `payload`：**原生工具格式化后的文本**，不是二进制原始载荷；换行/控制字符会被原生工具规范化。
- `decoded`：原生导出是否是 verbose 模式；不是独立验证整个应用协议的声明。
  Non Verbose 显示可能包含 Message ID 与十六进制，但不能据此推断真实业务含义。

注意：实测此发行版 CSV 导出把中文替换为问号；本实现使用 `-u` 原生文本导出后转换 JSON，保留中文。

## 4. 确定性测试规则

示例规则位于 examples/smoke.rules.json，要求：

1. 至少有两条消息；
2. TEST/MAIN 出现 BOOT_OK；
3. TEST 出现 TEST_DONE；
4. 整个日志没有 fatal/error；
5. 非 control 消息必须是已解码 verbose 格式。

```powershell
.\dlt-cli.ps1 check C:\logs\test.dlt --rules .\examples\smoke.rules.json --output C:\logs\report.json
$code = $LASTEXITCODE
if ($code -ne 0) { exit $code }
```

规则文件支持：

| 层级 | 字段 | 含义 |
| --- | --- | --- |
| 文档 | schema_version | 必须为整数 1 |
| 文档 | name | 测试名称 |
| 文档 | min_messages | 消息总量下限，默认 1 |
| 文档 | require_decoded | 默认 true；Non Verbose 未解码时判失败，control 不参与此检查 |
| 文档 | rules | 至少一条规则；名称唯一 |
| 规则 | name | 断言名称 |
| 规则 | match | 筛选条件，空对象表示所有消息 |
| 规则 | min_count / max_count | 至少给出一个；包含边界；max_count=0 表示禁止出现 |

match 支持 ecu/apid/ctid/type/level（字符串或非空字符串数组）、contains、regex、ignore_case 和 max_level。
所有给出的条件同时满足才计数；正则语法错误、未知字段、重复 JSON 键、非法上下限均返回错误。
报告保存每条断言的实际计数和最多 `--evidence-limit` 条匹配证据。max_count 超限的证据就是实际违规日志。

**不要用 AI 的自然语言解释代替 PASS/FAIL 规则**。AI 可生成/审阅规则、读取失败证据和提出修复，
但最终通过条件由规则与退出码决定。若需要顺序、延时、性能阈值或跨信号时序断言，应进一步增加专用规则；当前仅支持计数断言。

## 5. 限时实时采集

```powershell
.\dlt-cli.ps1 capture --host 192.168.1.100 --port 3490 --duration 30 --output C:\logs\capture.dlt
.\dlt-cli.ps1 summary C:\logs\capture.dlt
```

要求 ECU 已运行 DLT daemon，网络可达，并向新 TCP 连接发送标准 DLTv1 消息。
当前是 **TCP Client**，对应 GUI 的 TCP DLT，不是 TCP Server DLT。
远端端口默认 3490；支持 TCP 分包/粘包及可选 DLS\x01 前缀。不是 SOME/IP/UDP/串口接收器，不发送 DLT 控制命令。

### 指定上位机本地 IP/端口

`capture` 和 `run` 均支持本地绑定。以下仅为已获授权后的命令示例，不会修改 ECU 或系统网络配置：

```powershell
# ECU 的监听端口为 50181，上位机固定使用 TCP 源端口 50182。
.\dlt-cli.ps1 capture --host 192.168.1.100 --port 50181 --local-host 0.0.0.0 --local-port 50182 --duration 30 --output C:\logs\capture-bound.dlt
```

| 参数 | 含义 | 默认 |
| --- | --- | --- |
| --host | 远端 ECU/daemon IP 或主机名；不能把 0.0.0.0 当作真实 ECU 地址 | 必填 |
| --port | 远端 ECU 的 TCP 监听端口，1–65535 | 3490 |
| --local-host | 上位机本地 IPv4/IPv6 地址；须与目标地址族匹配 | 不指定时按系统路由选网卡 |
| --local-port | 上位机 TCP 源端口，0–65535；0 表示系统分配 | 0 |

--local-host 0.0.0.0 是 IPv4 通配绑定，仍由路由选择出站网卡，并不是开启服务器监听。
多网卡时可改为电脑实际持有的网卡 IP；IPv6 可使用 :: 或具体本地 IPv6 地址。
只需固定端口时可省略 --local-host。本地 IP 必须是地址字面量，不接受主机名。
绑定失败、地址不属于本机、端口占用或连接失败均退出 2，不会偷偷回退随机端口；run 不会在失败后执行触发。
不要通过开启端口复用来绕过冲突；关闭占用该端口的 GUI/其他进程，或按批准的网络配置更换端口。
短时间重复使用同一固定源端口可能受 TCP TIME_WAIT 限制，不自动重试或修改系统设置。
采集成功 JSON 的 local_host/local_port 是实际本地端点；host/port 仍为请求的远端目标。
run 报告中的 capture 对象包含同样字段。自动分配时也会报告实际源端口。

### 其他采集选项

- `--duration`：连接成功后的采集时限，默认 10 秒；连接时限另由 `--connect-timeout` 控制。
- `--max-messages`：可提前按消息数停止；默认 0 表示不限制。
- storage 时间由主机接收时刻生成，ECU 标识优先取原始标准头，缺失时使用 `--ecu`。
- 不覆盖已有采集文件；目录必须先存在。每次运行请使用新的文件名。
- EOF 截断、错误帧、零完整消息返回错误，已收到的完整消息保留供排查。
- 到时限后未完整接收的尾部不会写入 DLT 文件，结果通过 discarded_tail_bytes 报告。
- 退出时关闭 TCP，不自动重连；重复测试由外层控制器管理。

## 6. 单命令闭环：采集 → 测试触发 → 判定

先准备真实测试命令的 JSON argv 文件；examples/trigger.example.json **只是打印提示的占位命令，不驱动 ECU**。
把它替换成你已有的 CAN/UDS/诊断台架/设备控制测试脚本的可执行文件和参数。
触发程序工作目录是调用 CLI 时的目录；建议 executable 和脚本都用绝对路径。

```powershell
.\dlt-cli.ps1 run --host 192.168.1.100 --port 3490 --duration 30 --output C:\logs\run-001.dlt --rules .\examples\smoke.rules.json --trigger-file .\examples\trigger.example.json --trigger-timeout 20 --report C:\logs\run-001.report.json
$LASTEXITCODE
```

执行顺序：

1. 加载并验证规则与触发命令。
2. 建立 TCP 并打开新采集文件。
3. 开始采集线程，然后执行触发命令；触发失败或超时不能判为通过。
4. 达到 duration、max-messages 或 ECU 断开后结束采集。
5. 调用原生工具解码并执行断言；输出组合报告。

需要固定上位机端口时，在 run 命令中同样加入 --local-host 和 --local-port，含义与 capture 完全一致。

采集和触发报告附带主机 Unix 纳秒时间范围。如果采集在测试触发结束前已经停止，
即使已找到成功标记也判失败，避免观察窗口不足。请保证 duration 覆盖测试执行与必要的后续观察时间。

也支持 `--trigger-json` 内联 JSON argv 数组；PowerShell 入口已处理 Windows 引号/反斜杠转发。
建议 AI/CI 使用 `--trigger-file`，减少多层转义。不会使用 shell=True，数组元素不会自动解释为管道或重定向。
触发超时会终止直接子进程；触发程序自己启动的后台进程/硬件动作仍需其自身提供清理与取消契约。

**测试窗口与关联必须由业务定义**：DLT daemon 可能在新连接时发送历史缓冲日志。
BOOT_OK/TEST_DONE 示例不能区分“本次测试”和历史测试。生产测试应在日志中打印唯一 run_id，
规则通过 contains/regex 匹配该 run_id，并设置合理时限/消息下限。当前不自动清空 ECU 缓冲，不自动对齐 ECU 时间。
`--max-messages` 会缩短观察窗口，不能用过小的限制证明“后续无错误”。

## 7. Non Verbose/FIBEX

```powershell
.\dlt-cli.ps1 read C:\logs\nonverbose.dlt --fibex C:\fibex --output C:\logs\decoded.jsonl
.\dlt-cli.ps1 check C:\logs\nonverbose.dlt --fibex C:\fibex --rules C:\tests\test.rules.json
```

复用 Non Verbose Mode Plugin：先 `-dd` 导出 decoded DLT，再 `-u` 格式化。
实测 `-dd` 会损坏 control 帧并把未知 ID 的占位文本包装成 verbose，因此适配器会流式对照原始输入：
保留 control/原本 verbose/未知 ID 帧，只接纳 FIBEX 覆盖且不是占位文本的成功解码业务帧，
同时核验顺序、storage 时间和 ECU/session/timestamp。FIBEX 帧 ID 须使用 `ID_<十进制>`；目录递归读取 .fibex/.xml。
不仅检查插件命令返回值，还检查解码文件存在、帧格式及消息数；仍未解码的应用消息不能默认通过。
FIBEX 必须与 ECU 软件版本一致。提供的示例规则对已完成真实离线验收；这不等同于数据库版本或所有业务值均正确。
若明确只测试非文本元数据，可以手动设置 require_decoded=false；这不代表解码成功。

## 8. 退出码与自动化契约

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功；check/run 为断言通过 |
| 1 | check/run 的断言失败或测试触发失败 |
| 2 | 参数、输入、采集、解码、文件或环境错误 |
| 130 | 用户中断 |

正常数据只写 stdout；原生命令行的进度/调试文本被隔离。错误是 stderr 上的 JSON。
`--output`/`--report` 把正常结果改为写文件，stdout 留空；报告/JSONL 文件用同目录临时文件成功后原子替换。
capture 的 output 是原始 DLT，不是报告。

AI/CI 必须先检查退出码，不能仅根据是否有文件或是否找到某一条成功标记判断通过。
stdout JSONL 是流式输出，后续错误发生时可能已有前面文件的部分消息输出；非 0 退出时不应把它当作完整结果。
保持失败原始 DLT 和 JSON 报告，便于 Viewer 人工复核以及 AI 追溯证据。

## 9. 支持范围与限制

- 当前 AI 适配器只接收 **DLTv1 storage .dlt**，严格校验帧边界、版本、长度和导出消息总数。
  这不是应用级 payload 校验或密码学完整性检查；异常布局会报错，不自动“修复”坏日志。
- raw .dlt streams、DLTv2、PCAP、MF4 尚未进入本适配器验收；需要时先用原生工具转换，或另行扩展。
- read/summary 可使用 `--native-filter` 复用 .dlf；check/run **禁止**预过滤，以免漏掉其他上下文的错误。
  测试范围应在 rule.match 中表达。
- 日志与原生进度采用磁盘暂存，逐条解析，不把全部消息载入 Python 内存；摘要按来源/上下文聚合。
  原生工具仍会创建索引，需要足够磁盘/内存；暂存可通过进程环境 TEMP/TMP 配置。
- 解码超时 `--timeout` 默认每文件 120 秒，可按日志体积调整。
- 没有修改 COVESA 原始二进制，不自动下载源码、安装 Qt、改变系统执行策略或网络设置。

## 10. 验证与后续接入

```powershell
py -3 -m unittest discover -s tests -v
```

测试使用合成 DLTv1 消息、真实 bundled Commander 与本地 TCP 模拟，覆盖：
中文及大端 payload、空 ID、引号/逗号、原生换行规范化、消息总数、损坏/截断、JSON 参数、
过滤、空日志失败、规则错误、Non Verbose 失败优先、原子写入、退出码、TCP 分包、采集时限、
IPv4/IPv6 本地绑定、固定源端口、端口冲突不回退/不触发、触发成功/失败/超时和完整 run 报告。

接入你的实际软件测试还需要三项输入：真实日志样本（及必要的 FIBEX）、ECU 地址/端口、
可独立执行的测试触发命令。先用真实样本验收 read/check，再连台架验收 capture/run。
后续 AI 控制器可以按“执行测试 → 检查退出码 → 读取有限证据 → 修复 → 重跑”迭代；
硬件复位、烧录、取消动作和跨轮次隔离必须由台架脚本承担。

## 11. 真实文件验收与提交隔离

真实日志、FIBEX、历史 JSONL/摘要/审计/分析报告由用户在本地另行提供，不作为源码包必需文件。
使用 [AI_AGENT_GUIDE.md](AI_AGENT_GUIDE.md) 的本地输入流程，每轮新输出目录，核对哈希和退出码。
解码/上下文规则不作为 ECU 软件通过标准；ERROR 是否属于预期注入须由用例确认。
审计按 ECU/APID/CTID/session/Message ID/rawData 长度区分序号流；缺口不是网络丢包的直接证明。
提交前必须按 [DATA_HANDLING.md](DATA_HANDLING.md) 检查 Git 索引，不能认为新增忽略规则会移除已跟踪数据。
