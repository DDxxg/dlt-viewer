# 你应该怎么用：5 分钟上手

## 先明确谁在做什么

- **DLT CLI**：采集、解码、筛选、汇总、按规则判定。
- **AI（Copilot/其他 Agent）**：调用 CLI、读少量证据、解释结果、提出下一步。
- **你/台架脚本**：定义软件通过标准、提供 ECU 地址和真正的测试触发动作。

CLI 本身没有内置 LLM。当前最方便的方式是在 VS Code 打开这个工程，让有终端/文件工具的 AI 使用它。

## 方式一：让 AI 操作（推荐）

把下面这段话发给 AI：

> 先读取工程根目录 AGENTS.md 和 AI_AGENT_GUIDE.md，按手册操作。
> 先运行 doctor，再在新目录运行合成演示，验证摘要、有限取证和合成规则。
> 如需真实分析，先向我确认日志、匹配版本 FIBEX 和批准规则的本地路径。
> 报告退出码、消息数量、解码覆盖率及证据索引，区分事实与假设。
> 不连接 ECU、不触发硬件、不修改输入，也不要把解码成功当成软件测试通过。
> 真实输入和 artifacts 所有报告只保留本地，不提交或随源码分发。

这段任务不需要 IP。合成演示不需要真实数据；真实文件和历史报告不随源码包提供。
若 AI 没有文件/终端权限，只能解释你粘贴的摘要；不能声称已经实际执行测试。

工程已有 [AGENTS.md](AGENTS.md) 和 [.github/copilot-instructions.md](.github/copilot-instructions.md)
帮助支持这些约定的 Agent/Copilot 加载说明。不同工具的自动发现与设置不同，
**最可靠的做法仍是明确要求 AI 先读取 [AI_AGENT_GUIDE.md](AI_AGENT_GUIDE.md)**。

## 方式二：你在 PowerShell 手工运行

在 VS Code 中打开本工程的 PowerShell 终端。以下相对路径要求终端位于工程根目录。
每条命令后的 $LASTEXITCODE 应立即查看；下一条外部命令会覆盖它。

### 1. 检查环境

```powershell
.\dlt-cli.ps1 doctor
$LASTEXITCODE
```

预期 JSON 中 status=ok，退出码 0。需要 Python 3.10+，不需要 pip 安装包。
doctor 实际导出一条合成 DLTv1，capability_probe.status 应为 passed；原生帮助为空不再阻断。
仅验证 UTF-8 verbose 导出，不检测真实 FIBEX、TCP 或 ECU 业务。
如果 PS1 被系统策略阻止，按公司政策处理；不要全局放宽策略。可使用 Python 入口，见完整指南。

### 2. 创建合成样本并看摘要

```powershell
$session = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$out = Join-Path (Get-Location).Path ('artifacts\session-' + $session)
$demo = Join-Path $out 'synthetic'
py -3 .\examples\demo.py --output-dir $demo
$demoCode = $LASTEXITCODE
if ($demoCode -ne 0) { throw "合成演示失败：$demoCode" }
$log = Join-Path $demo 'sample.dlt'
.\dlt-cli.ps1 summary $log --evidence-limit 3
$LASTEXITCODE
```

合成样本预期 scanned_messages=5、undecoded_messages=0，types 中 log=5。
此命令成功只证明读取/汇总成功，合成演示不是 ECU 测试。

### 3. 只看前几条 ERROR

```powershell
.\dlt-cli.ps1 read $log --level error --limit 5
$LASTEXITCODE
```

每一行是一个 JSON 对象，可直接给 AI 阅读。包含 payload、message_id、index、source_offset。
合成样本没有 ERROR，结果为空；不能把“未匹配到错误”独立当作软件测试通过。

### 4. 执行合成规则

```powershell
.\dlt-cli.ps1 check $log --rules '.\examples\smoke.rules.json' --evidence-limit 1
$LASTEXITCODE
```

合成样本应 passed，退出码 0。**这是合成接口回归，不是 ECU 软件测试通过。**
真实日志必须使用用户批准的规则和匹配版本 FIBEX，不照搬合成 BOOT_OK/TEST_DONE 标记。

## 报告在哪里与数据边界

合成演示会在 $demo 中写入 JSONL、摘要和规则报告；真实报告只在用户授权的本地目录读取。
先看摘要，再看有限证据，不把完整 JSONL 粘贴给 AI。
artifacts 全部内容（包括 Markdown 和小型 JSON）及真实日志/数据库**不得提交或分发**。
忽略规则不保护已经跟踪的文件；提交前按 [DATA_HANDLING.md](DATA_HANDLING.md) 运行只读索引检查。

## 保存新一轮结果

使用 --output，由 CLI 写 UTF-8。不要用 Windows PowerShell 5.1 的 > 保存 JSON，默认编码可能不同。
也不要直接把 PS1 调用接到 ConvertFrom-Json 或赋给变量来获取数据；先 --output 写文件再读取最可靠。
沿用上面的新输出目录 $out，不覆盖历史报告；真实分析先按 AI 手册确认本地输入路径：

```powershell
.\dlt-cli.ps1 summary $log --evidence-limit 3 --output (Join-Path $out 'summary.json')
$code = $LASTEXITCODE
if ($code -ne 0) { throw "摘要失败，退出码 $code" }
Get-Content -LiteralPath (Join-Path $out 'summary.json') -Encoding UTF8 | ConvertFrom-Json
```

## 什么条件下能实时闭环

现在无需连 ECU 即可完成日志分析。实时测试还需要你提供：

1. ECU 的 IP/端口、网络已连通且 DLT daemon 正常发送。
2. 可独立执行的测试触发脚本或命令，而不是只有日志文件。
3. 真正的通过/失败条件、测试时限、run_id 关联与硬件取消/清理方式。

提供这些后，AI 按手册调用 run：先连接采集、再触发测试、最后生成规则判定报告。
仅有 DLT 文件和 FIBEX，不能自动完成刷写、复位或重新运行 ECU 软件。

## 固定上位机本地端口（TCP Client）

当前 CLI 相当于 GUI 的 **TCP DLT（客户端）**，不是 TCP Server DLT。
现已支持 capture/run 指定上位机本地 IP/端口：

| GUI/网络含义 | CLI 参数 | 示例 |
| --- | --- | --- |
| 远端 ECU 的 IP/主机名 | --host | ECU 实际 IP |
| ECU 监听端口（IP Port） | --port | 50181 |
| 上位机源端口（Local Port） | --local-port | 50182 |
| 上位机出站网卡 IP | --local-host | 本机实际 IP；0.0.0.0 表示 IPv4 通配绑定 |

例如：ECU 监听 50181，电脑固定使用源端口 50182。以下仅为示例，**将远端 IP 替换为已授权 ECU 的实际地址后才运行**；
先按上文建立新输出目录 $out：

```powershell
.\dlt-cli.ps1 capture --host 192.168.1.100 --port 50181 --local-host 0.0.0.0 --local-port 50182 --duration 30 --output (Join-Path $out 'capture.dlt')
$captureCode = $LASTEXITCODE
if ($captureCode -ne 0) { throw "采集失败，退出码 $captureCode；检查 stderr JSON" }
```

只要固定端口时可以省略 --local-host；多网卡时可指定电脑实际持有的网卡 IP。
**不要把截图中的 0.0.0.0 直接填到远端 --host**；它不能代替 ECU 的真实地址。
不指定 --local-port 或设为 0 时自动分配。结果 JSON 的 local_host/local_port 会报告实际本地端点。
端口占用或绑定失败会返回 2，不会换成随机端口；请先关闭占用端口的 GUI/其他采集实例。
固定源端口连续重连可能受 TCP TIME_WAIT 影响；当前不自动重连，不修改系统网络设置。
这些参数也可加入 run 命令，连接成功后才触发测试。TCP Server、UDP 和串口仍未实现。

更多参数：[CLI_GUIDE.md](CLI_GUIDE.md)。AI 的详细操作契约：[AI_AGENT_GUIDE.md](AI_AGENT_GUIDE.md)。
