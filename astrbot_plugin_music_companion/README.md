# 音乐伴侣

将本机网易云音乐的结构化事件接入 AstrBot 现有的 LLM 与 QQ 闭环。

## 功能

- 只读跟踪 `audio_events_*.jsonl` 新日志，插件启动时不回放历史事件。
- 普通 QQ 回复可注入最近一次歌曲、歌手和播放状态快照。
- 切歌、播放状态及明显声学变化事件经过去重、2 秒合并和冷却后，由文本 LLM 决定是否主动交流。
- 主动发送默认关闭，必须显式配置目标 UMO；不会猜测最近聊天对象。
- 把快照折算成可数的听歌行为（听多久、听完还是跳过、几点在听），可按段查询。
- 不发送 WAV、PCM、音频包或音频 URL，不控制播放器。

## 前置条件

先在音乐项目中运行检测，并启用 GSMTC 歌曲上下文：

```bash
uv run --no-project --directory "<音乐项目目录>" python monitor_audio.py --music-context --seconds 0
```

实时捕获仅针对 cloudmusic 进程树。插件只读取检测程序生成的 JSONL，不自行捕获音频。

## 配置

| 配置项 | 说明 |
| --- | --- |
| `enabled` | 启用日志跟踪和普通 QQ 回复的音乐上下文注入。 |
| `log_dir` | `monitor_audio.py` 生成 `audio_events_*.jsonl` 的目录。 |
| `memory_enabled` | 启用音乐行为记忆，默认 `true`。 |
| `memory_db` | 记忆库路径，留空时用 `log_dir` 下的 `music.db`。 |
| `session_gap_seconds` | 听歌会话切分间隔，默认 300 秒。 |
| `proactive_enabled` | 是否允许音乐事件触发真实模型调用和主动 QQ 消息，默认 `false`。 |
| `target_umo` | 主动消息的完整 AstrBot unified message origin，启用主动发送前必须确认；同时用作记忆查询的归属校验。 |
| `provider_id` | 可选的文本 Provider ID；留空时使用目标会话当前 Provider。 |
| `proactive_cooldown_seconds` | 主动分析冷却时间，默认 45 秒。 |
| `llm_timeout_seconds` | 单次音乐决策模型超时，默认 12 秒，最大 25 秒。 |

开启主动发送可能产生模型费用并发送真实 QQ 消息。请先确认 Provider、目标 UMO 和测试范围。

## 查询音乐记忆

- `/音乐记忆`：最近几段听歌记录。
- `/音乐记忆 常听`：完整听过次数最多的曲子。
- `/音乐记忆 跳过`：老被跳过的曲子。
- `/音乐记忆 日记`：今天的听歌日记。

配了 `target_umo` 时，命令只在该会话内可用；其他会话会被拒。

同时注册了 LLM 工具 `music_companion_recall`（`action` 取 `recent` / `favourite` / `skipped` / `diary` / `digest`），让模型在对话里主动查，而不是把记忆全量塞进提示词。

## 事件语义

- `track_observed` 只更新快照，不主动调用模型。
- 能量、织体和节奏变化只是变化候选，不等于副歌。
- 缺少音频数据不等于静音，采集时间不等于歌曲播放进度。
- 系统未提供 Timeline 时，播放进度保持不可用，不进行推算。
- 采集中断的快照不进记忆层，避免把断档算成一直在听。
- 模型拒绝、输出无效、调用失败或发送超时均不自动重试。
- 插件在 `log_dir` 下追加 `music_companion_plugin_trace.jsonl` 诊断轨迹（启动配置、候选批次、模型决策、发送结果），不含消息内容，仅用于实机排障。

## 测试

在音乐项目根目录运行：

```bash
uv run --no-project --directory "<音乐项目目录>" python -m pytest -q
```

模拟测试不会打开音频捕获、调用真实模型或发送 QQ。真实 AstrBot 加载及真实 QQ 端到端结果应以单独实机验证为准。
