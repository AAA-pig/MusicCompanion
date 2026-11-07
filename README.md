# 音乐伴侣 (music_companion)

让 AI 陪你听歌。本地抓取网易云音乐的播放音频，提取声学变化，结合 Windows 媒体会话
（GSMTC）的歌曲信息，输出结构化事件流；再由 AstrBot 插件把事件接进 LLM 与 QQ。

全程离线。不上传原始音频，不保存原始音频，不读取歌词、账号或歌单。

## 它做了什么

| 模块 | 职责 |
| --- | --- |
| `native/ProcessLoopback.cs` | WASAPI process loopback，只抓 `cloudmusic.exe` 进程树的音频，输出 PCM16 流 |
| `audio_changes.py` | 有界历史的有状态检测器，算 RMS/dBFS 与频带能量，产出 `silence_start`、`audio_resumed`、`capture_gap` 事件 |
| `music_context.py` | 惰性调用 GSMTC，读歌名、歌手、专辑、播放状态、进度；系统不给进度时返回 `null` |
| `monitor.py` | 把捕获器、检测器、上下文轮询串起来，写 JSONL 并按需打印 |
| `cli/` | `music-context`（一次性诊断）、`music-probe`（裸捕获/离线分析 WAV） |
| `astrbot_plugin_music_companion/` | AstrBot 插件，消费 JSONL 事件，触发 LLM 反应并通过 QQ 发送 |

## 明确边界

- 只捕获指定进程树的音频，不碰麦克风，不做系统全局录音。
- 日志里只有特征值、事件时间与歌曲元数据；原始音频不落盘、不出本机。
- 检测到的是「声音有变化」，不是歌曲识别，也不是播放进度。
- GSMTC 不提供 position 时就写 `null`，绝不用采集会话时长伪造歌曲进度。
- 主动发消息默认关闭，需要手动在插件配置里打开。

## 目录结构

```text
.
├─ src/music_companion/
│  ├─ audio_changes.py      # 声学特征与变化检测
│  ├─ capture.py            # 找网易云主进程 / 编译捕获器
│  ├─ music_context.py      # GSMTC 歌曲上下文
│  ├─ monitor.py            # 主循环（命令行入口 music-monitor）
│  ├─ cli/
│  │  ├─ context_check.py   # music-context
│  │  └─ probe.py           # music-probe
│  └─ native/
│     └─ ProcessLoopback.cs # 原生捕获器源码
├─ tests/                   # 单元测试（全部基于模拟数据）
├─ scripts/start_detection.cmd
├─ docs/                    # 迭代说明、AI 接手文档、验证摘要
├─ astrbot_plugin_music_companion/
├─ pyproject.toml
└─ LICENSE
```

## 环境要求

- Windows 11（依赖 WASAPI process loopback）
- .NET Framework 4.x 自带的 `csc.exe`（编译原生捕获器，首次运行自动触发）
- Python 3.13
- [uv](https://docs.astral.sh/uv/)

## 安装

```powershell
uv sync                 # 基础依赖：numpy
uv sync --extra gsmtc   # 需要读歌曲信息时再装（PyWinRT）
```

## 使用

```powershell
# 实时检测 60 秒（先打开网易云并播放）
uv run music-monitor --seconds 60

# 一直跑到 Ctrl+C，并附加歌曲上下文
uv run music-monitor --seconds 0 --music-context

# 只看当前歌曲信息，用来排查 GSMTC 兼容性
uv run music-context

# 裸捕获一段 WAV，或分析已有 WAV（不需要网易云在跑）
uv run music-probe --seconds 15
uv run music-probe --analyze clip.wav
```

双击 `scripts/start_detection.cmd` 等价于 `music-monitor --seconds 0`。

日志默认写到当前工作目录的 `audio_events_<时间戳>.jsonl`，可用 `--log` 指定。
每行一个 JSON 记录：

```json
{"kind": "event", "type": "audio_resumed", "event_time_s": 12.4, "confirmation_seconds": 0.3,
 "music_context": {"title": "晴天", "artist": "周杰伦", "playback_status": "playing", "position_seconds": 42.1}}
```

## AstrBot 插件

把 `astrbot_plugin_music_companion/` 放进 AstrBot 的 `data/plugins/`，在配置里指定
`log_dir` 指向 `music-monitor` 的输出目录即可。

| 配置项 | 作用 |
| --- | --- |
| `enabled` | 插件总开关 |
| `log_dir` | 监听的事件日志目录 |
| `proactive_enabled` | 是否允许主动发起对话（默认关闭） |
| `target_umo` | 主动消息的会话标识 |
| `provider_id` | 使用哪个 LLM Provider |
| `proactive_cooldown_seconds` | 主动发言冷却 |
| `llm_timeout_seconds` | LLM 调用超时 |

## 开发

```powershell
uv run python -m pytest
```

测试全部使用模拟的音频与 GSMTC 对象，跑通不代表真机（WASAPI、网易云版本差异）已验证。
真机行为请以 `music-context` 的实际 JSON 输出为准，记录在 `docs/` 下。

## 许可证

[MIT](LICENSE) © 2026 Yang Chenghan (AAA-pig)
