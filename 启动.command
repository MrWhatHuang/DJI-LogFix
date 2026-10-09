#!/bin/bash
# DJI LogFix 启动脚本
# 双击本文件即可打开网页界面（源素材全程只读）

cd "$(dirname "$0")" || exit 1

if [ ! -x "tools/ffmpeg" ]; then
    echo "错误：找不到 tools/ffmpeg"
    echo "请确认 DJI-LogFix 文件夹完整。"
    read -r -p "按回车退出…"
    exit 1
fi

echo "正在启动 DJI LogFix…"
exec python3 webgui.py
