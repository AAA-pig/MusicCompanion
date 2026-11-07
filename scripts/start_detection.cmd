@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

where uv >nul 2>nul
if errorlevel 1 (
  echo 未找到 uv，请先安装：https://docs.astral.sh/uv/
  pause
  exit /b 1
)

echo 请先在网易云音乐里播放歌曲，然后按 Ctrl+C 停止检测。
echo 仅记录本地声学特征，不保存原始音频，不上传。
uv run music-monitor --seconds 0
pause
endlocal
