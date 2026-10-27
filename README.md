# easyChat：微信群消息发送模块

本项目提供 Python 发送函数和本地 HTTP 联调服务。调用方负责单据比对、确定要 @ 的人和生成消息正文；本模块操作 Windows 桌面微信，向指定群聊发送文字及图片。

旧版 GUI、定时发送和自动回复代码仍保留在仓库中供迁移参考，但不在新发送入口的调用链中。旧说明见 [README.legacy.md](README.legacy.md)。

## 安装与前提

- Windows、已登录的桌面微信、可交互的用户桌面。不能在无桌面的服务会话中使用。
- Python 3.10 或以上；安装依赖：`pip install -r requirements-sender.txt`。
- 微信“显示/隐藏窗口”快捷键与 `SenderConfig.hotkey` 一致。4.1.15.56 的控件树如果未暴露，服务会尝试热激活。
- 建议使用专门负责自动发送的微信会话。发送前会清空当前输入框草稿，避免上一次失败留下的文字混入新消息。
- 内置配置：微信 4.1.15.56（默认）、4.1.15.8、4.1.9.21、4.1.8.107。4.1.15.56 的控件锚点取自本机实测控件树；其他版本保留原项目的定位配置，发送仍需在对应客户端验证。

微信 4.1.15.56 在本机未热激活时只暴露 `Qt51514QWindowIcon` 外壳。发送前若检测到这种情况，服务会调用随项目固定的 [上游 UIA 热激活脚本](vendor/wechatauto_replica/README.md)：扫描当前 `Weixin.dll`，尝试修改运行中微信进程内的无障碍标志，再检查 `mmui::` 控件树。激活失败时返回 `wechat_uia_activation_failed`，不会继续发送。微信彻底重启后会在下次请求时重新检查。该步骤会写入微信进程内存。

本机验证过热激活后的主界面、搜索界面和 @ 候选界面控件树（分别为 151、166 和 155 个控件）；4.1.15.56 的搜索、@ 和发送定位已据此实现。**完整的文字、@、图片发送尚未实机验证成功**，请先在测试群联调。最近一次实机请求在清空草稿时遇到不受 `uiautomation` 支持的 `{Backspace}` 键名，已改为受支持的 `{Delete}`；这次请求发送数为 0。

可先单独诊断：`python -X utf8 vendor/wechatauto_replica/wake_uia.py --status` 只读状态；`python -X utf8 vendor/wechatauto_replica/wake_uia.py --cache diagnostics/gate_cache.json` 尝试激活并校验。运行这两条命令时，微信主窗口需处于可见状态。激活成功后，运行下文的控件树采集脚本查看实际控件结构。

## Python 调用

```python
from sender import SenderConfig, SenderService, send_group_message

payload = {
    "request_id": "compare-20261027-001",
    "group_name": "资产核对群",
    "at_users": ["张三", "李四"],
    "content": "两张单子的对比结果：新增 2 条，变更 3 条。",
    "images": [],
}

# 使用默认配置（微信 4.1.15.56、Ctrl+Alt+W）
result = send_group_message(payload)

# 需要其他已适配版本或快捷键时，复用一个 SenderService 实例
service = SenderService(SenderConfig(
    version="微信 4.1.9.21",
    hotkey="{Ctrl}{Alt}e",
    locale="zh-CN",
))
result = service.send_group_message(payload)
```

`content` 是正文，真正的群 @ 由 `at_users` 单独指定。`images` 中每项是 `filename` 和 `data_base64`；最多 5 张，每张不超过 8 MB，支持 PNG、JPEG、WEBP、BMP。服务先发送文本消息（包含 @），再按顺序分别发送图片消息。可只发图片，此时 `at_users` 必须为空。一次调用只发送到一个群。`request_id` 可省略；提供后，同一运行目录的 `sender_state.sqlite3` 会记录其状态，同一个编号不会再次执行。若同编号用于不同内容，返回 `invalid_request`。

## 本地联调页面与 HTTP 接口

在项目目录运行：

```powershell
python -m pip install -r requirements-sender.txt
python server.py --version "微信 4.1.15.56" --log-level DEBUG
```

浏览器打开 `http://127.0.0.1:8765/`，即可填写群名、@ 人员、正文并选择图片。页面会调用 `POST /api/send`，返回 JSON。先访问 `http://127.0.0.1:8765/api/health`，确认 `version` 是实际使用的微信版本。每次重试请生成新的 `request_id`，并先核对上一次是否已部分发送。

不经过页面时，也可用 Python 发出一个仅含文字的本地联调请求：

```python
import json
import urllib.request

payload = {
    "request_id": "test-001",
    "group_name": "测试群",
    "at_users": ["张三"],
    "content": "联调消息",
    "images": [],
}
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/send",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json; charset=utf-8"},
    method="POST",
)
with urllib.request.urlopen(request) as response:
    print(json.load(response))
```

服务同时把关键步骤打印在启动它的终端，并写入 `logs/easychat.log`（5 MB 轮转）。日志记录请求编号、微信版本、前台进程和控件类、群搜索、@ 候选、发送进度与错误；不会记录正文或图片数据。要看每次随机等待，使用 `python server.py --log-level DEBUG`。修改代码后需重启服务，再用**新的请求编号**做联调。

若返回 `wechat_not_ready`，先看日志中的 `activation_initial`、`activation_hotkey` 和 `activation_no_focus`：它们会显示快捷键执行前后的前台进程与控件类。确认微信已经登录，并使 `--hotkey` 与微信设置中的“显示/隐藏窗口”一致。当前程序同时检查焦点控件的父级和前台进程名，避免焦点落在微信子控件时误报。

## 采集微信 4.1.15.56 控件树

在交互式 PowerShell 中运行以下命令。每条命令开始后有 10–15 秒倒计时；倒计时期间切到微信并准备对应界面，脚本只读取控件，不会点击或发送：

```powershell
python inspect_wechat_ui.py --label main --delay 10 --target '测试群' --member '张三'
python inspect_wechat_ui.py --label search --delay 15 --target '测试群' --member '张三'
python inspect_wechat_ui.py --label mention --delay 15 --target '测试群' --member '张三'
```

- `main`：显示微信主聊天界面。
- `search`：在微信搜索框输入目标群名，保持搜索结果可见。
- `mention`：进入目标群，在消息框输入 `@成员名`，保持候选列表可见，**不要按回车或发送**。

输出保存在 `diagnostics/` 下的三个 UTF-8 文本文件。默认只保留常见按钮文字，其余名称会遮蔽；命令中提供的群名和成员名会显示为 `<TARGET>`、`<MEMBER>`。分享前仍请检查文件内容。若只采到 10 个以内的控件，先用上面的热激活命令恢复 `mmui::` 控件树，再重跑采集。

其他程序也可直接发送 JSON：

```json
{
  "request_id": "compare-001",
  "group_name": "资产核对群",
  "at_users": ["张三"],
  "content": "本次单据对比发现 2 处差异。",
  "images": [
    {"filename": "差异截图.png", "data_base64": "<图片文件的 base64 内容>"}
  ]
}
```

服务默认只监听本机 `127.0.0.1:8765`。如需从其他机器访问，可设置环境变量 `EASYCHAT_API_TOKEN`，再用 `python server.py --host 0.0.0.0` 启动；请求头携带 `X-API-Token`。可用 `--version "微信 4.1.9.21"`、`--hotkey "{Ctrl}{Alt}e"` 和 `--locale en-US` 指定本机微信配置。

## 返回结果

返回字典始终包含 `request_id`、`ok`、`status`、`message`。正常执行时示例：

```python
{
    "request_id": "compare-20261027-001",
    "ok": True,
    "status": "submitted",
    "message": "所有消息均已在目标会话点击发送；当前无法确认对方是否收到",
    "mention_selection": "ui_candidate_selected",
    "sent_messages": 1,
    "total_messages": 1,
}
```

`submitted` 仅表示客户端发送按钮已点击，**不是送达或已读确认**。在 4.1.15.56 中，发送模块只从 `chat_mention_list` 弹层选取名称精确匹配的成员；旧版通过输入 @ 前后的候选差异定位。找不到或有多个同名候选时停止发送。真实 @ 仍须在测试群中验证。

其他状态包括 `invalid_request`、`duplicate_request`、`group_not_found`、`group_ambiguous`、`mention_not_found`、`mention_ambiguous`、`mention_selection_uncertain`、`wechat_not_ready`、`wechat_uia_activation_failed`、`wechat_ui_changed`、`automation_unavailable`、`send_failed`、`partial_send`、`send_uncertain` 和 `state_error`。`partial_send` 表示前面的文字或图片已提交，后续消息失败；`send_uncertain` 表示某次发送点击结果无法确认。两者均不应直接用新编号重试整组内容。

## 等待与并发

- 每次微信界面操作前独立随机等待 2–10 秒。
- 每条文字或图片消息点击发送前额外随机等待 2–6 秒。
- 所有发送在进程内串行执行，因为微信窗口和系统剪贴板是共享资源。多个进程同时操作同一个桌面仍需由上层协调。

等待范围可通过 `SenderConfig.action_delay_range` 和 `message_delay_range` 调整。随机等待会降低连续操作速度，但不能保证规避微信风控。一次调用可能耗时数十秒，接口层应配置相应超时或任务队列。

## 代码分层

| 文件 | 职责 |
| --- | --- |
| `sender.py` | 对外导入入口 |
| `easychat_sender/contracts.py` | 输入校验和错误类型 |
| `easychat_sender/delay.py` | 每步独立随机等待 |
| `easychat_sender/profiles.py` | 微信版本配置与 4.1.15.56 的锚点定位开关 |
| `easychat_sender/driver.py` | 唯一操作桌面微信的模块 |
| `easychat_sender/uia_wake.py` | 控件树缺失时调用固定版本的热激活脚本 |
| `easychat_sender/service.py` | 串行执行、请求编号去重、结构化返回 |
| `server.py`、`web/index.html` | 本地 HTTP 接口和联调页面 |

运行 `python -m pytest tests/test_sender.py` 可验证不操作真实微信的逻辑。真实发送需要在对应微信客户端中做集成验证。
