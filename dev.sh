#!/bin/bash
# 로컬 개발 서버 실행
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# .env 로드
if [ -f .env ]; then
  set -a; source .env; set +a
fi

# venv 생성 (없으면)
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -e ".[dev]" 2>/dev/null || .venv/bin/pip install -e .
fi

export PYTHONPATH="$SCRIPT_DIR/src"

echo "=== domain-agent server (local) ==="
echo "API 문서: http://localhost:8000/docs"
echo ""

.venv/bin/uvicorn server.main:app --reload --host 0.0.0.0 --port 8000
