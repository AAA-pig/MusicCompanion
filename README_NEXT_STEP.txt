本次迭代：GSMTC 歌曲上下文读取器（第一版）

先运行现有检测器 + 新模块的模拟单元测试：
  .venv\Scripts\python.exe -m unittest -v test_audio_changes test_music_context

在 Windows 上先安装可选 GSMTC 依赖（不会改动原 requirements.txt）：
  .venv\Scripts\python.exe -m pip install -r requirements-gsmtc.txt

然后启动网易云音乐并播放一首歌，再运行：
  .venv\Scripts\python.exe check_music_context.py

当前状态：
- 原 cloudmusic 进程树 WASAPI 音频捕获与 ChangeEvent 检测器未修改。
- 新增 music_context.py，惰性导入 Windows.Media.Control GSMTC。
- 默认严格过滤 source_app_user_model_id 不包含 cloudmusic 的会话。
- 读取歌曲名、歌手、专辑、播放状态及 GSMTC timeline（如果存在）。
- 如果系统不提供 position，则返回 null；绝不把采集会话时长伪装成歌曲进度。
- 新模块尚未接入 monitor_audio.py，避免未经实机验证就改变现有实时链路。

本次在当前执行环境运行了 43 项自动化测试（37 项原测试 + 6 项新测试），全部通过。
这些新测试使用模拟 GSMTC 对象，不代表真实 Windows / 网易云客户端兼容性已经验证。
下一步请把 check_music_context.py 的实际 JSON 输出反馈给我，重点看 source_app_id、
title、playback_status 和 position_seconds 是否存在、是否随播放变化。
