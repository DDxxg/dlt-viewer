# 本地数据与提交边界

## 不得提交的内容

- 全部 artifacts 输出，包括 JSONL、摘要、审计、错误证据和分析 Markdown；大小不是判断依据。
- 原始真实 DLT、真实 FIBEX 和其他内部数据库；离线解析不等于获准公开。
- 账号、内部设备地址、台架配置、真实业务 payload，以及包含这些内容的附件或截图。

[.gitignore](.gitignore) 已合并公开上游构建/IDE 规则及本地 Python 规则，并隔离 artifacts、DLT、JSONL、FIBEX。
数据库例外仅限两个明确文件：独立编写的合成测试夹具 [tests/nonverbose.fibex](tests/nonverbose.fibex)，
以及上游早于本策略引入的 QDLT 单元测试夹具 `qdlt/tests/testfile.dlt`（71 字节，非真实日志）。
真实输入、已有历史报告只保留在本地，不因清理提交而删除或改写。
可提交的示例必须来自合成数据；自行打包、邮件附件和云盘上传也应排除真实数据，不能只依赖 Git 忽略。

## 已跟踪内容不会自动被忽略

忽略规则只阻止普通添加；已经进入 Git 索引/历史的文件，或强制添加的文件仍可能提交。
提交和推送前运行只读检查器 [examples/check_repository_data.py](examples/check_repository_data.py)：

```powershell
py -3 .\examples\check_repository_data.py --repository .
$dataCode = $LASTEXITCODE
if ($dataCode -ne 0) { throw "数据边界检查未通过：$dataCode；不得提交或推送" }
```

退出码 0=当前索引路径未发现受禁数据，1=发现，2=无法检查（包括当前目录不是 Git 仓库）。
检查器不读日志内容、不联网、不改索引、不删除文件，输出最多 20 个违规路径。
它不是自动安装的 Git hook；应接入提交前流程/CI，并结合人工检查暂存 diff。
它不识别任意源码中的秘密，也不审计过去的提交历史。

如果有已跟踪真实数据：先停止提交/推送，审核并移出索引，保留本地原件。
如果数据已进入远端历史，仅新增忽略规则或删除最新版本不够；需按组织数据事件流程评估访问范围，
经授权处理历史和远端副本。AI 不得擅自重写历史或宣称泄露已解除。

## 可复现的公开验证

运行合成演示及 unittest 即可验证接口，不需要提交整车样本或历史报告。
真实输入/报告不作为 README 必需链接；在授权环境内通过用户提供的本地路径读取。
