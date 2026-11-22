#!/usr/bin/env bash
set -euo pipefail

# 进入项目根目录（脚本所在目录）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
REQ_FILE="requirements.txt"
ENV_EXAMPLE=".env.example"
ENV_FILE=".env"

echo "[1/4] 检查 python3 与虚拟环境..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未检测到 python3，请先安装 Python 3。" >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "未发现 $VENV_DIR，正在使用 python3 创建虚拟环境..."
  python3 -m venv "$VENV_DIR"
fi

# 激活虚拟环境（兼容 Linux/macOS 的 bin/activate 与 Windows Git Bash 的 Scripts/activate）
if [ -f "$VENV_DIR/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/bin/activate"
elif [ -f "$VENV_DIR/Scripts/activate" ]; then
  # shellcheck disable=SC1091
  source "$VENV_DIR/Scripts/activate"
else
  echo "错误：无法找到虚拟环境激活脚本。" >&2
  exit 1
fi

echo "[2/4] 检查并安装依赖..."
if [ -f "$REQ_FILE" ]; then
  python3 -m pip install --upgrade pip >/dev/null
  python3 -m pip install -r "$REQ_FILE"
else
  echo "提示：未找到 $REQ_FILE，跳过依赖安装。"
fi

echo "[3/4] 检查环境变量文件..."
if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_EXAMPLE" ]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "已从 $ENV_EXAMPLE 生成 $ENV_FILE，请按需填入配置后再运行业务脚本。"
fi

echo "[4/4] 运行基础初始化与导出检查..."
if [ -f "scripts/00_init_db.py" ]; then
  python3 scripts/00_init_db.py
fi

if [ -f "scripts/04_export_json.py" ]; then
  python3 scripts/04_export_json.py
fi

echo "启动准备完成（当前已处于虚拟环境中）。"
