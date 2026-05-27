#!/usr/bin/env bash
# boss-hr-agent-toolkit 环境配置脚本
# 每次打开新的终端窗口后，先 source 此文件再运行 boss 命令
# 用法: source setup_env.sh

export PYTHONHOME=""
export PATH="$PATH:$HOME/.local/bin"

echo "[boss-env] PYTHONHOME 已清空"
echo "[boss-env] PATH 已追加 \$HOME/.local/bin"

if command -v boss.exe &>/dev/null; then
    echo "[boss-env] boss CLI 可用: $(boss.exe --version 2>&1)"
elif command -v boss &>/dev/null; then
    echo "[boss-env] boss CLI 可用: $(boss --version 2>&1)"
else
    echo "[boss-env] ⚠️ boss CLI 未在 PATH 中找到"
fi
