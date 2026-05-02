#!/bin/bash
# domain-agent-server 설치 스크립트
# 사용: curl -fsSL https://your-domain/install.sh | bash
#   또는: ./install.sh
set -e

INSTALL_DIR="${DOMAIN_AGENT_DIR:-/opt/domain-agent-server}"
SERVICE_USER="${SERVICE_USER:-$(whoami)}"
PORT="${PORT:-8000}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}▸${NC} $*"; }
warn()  { echo -e "${YELLOW}!${NC} $*"; }
error() { echo -e "${RED}✗${NC} $*"; exit 1; }

echo ""
echo "  domain-agent server 설치"
echo "  설치 경로: $INSTALL_DIR"
echo ""

# ── 의존성 확인 ─────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || error "Python 3.11+ 필요"

PY_VER=$(python3 -c "import sys; print(sys.version_info >= (3,11))")
[ "$PY_VER" = "True" ] || error "Python 3.11 이상 필요 (현재: $(python3 --version))"

info "Python $(python3 --version) 확인"

# ── 설치 디렉토리 ────────────────────────────────────────────
if [ ! -d "$INSTALL_DIR" ]; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo chown "$SERVICE_USER" "$INSTALL_DIR"
fi

# 현재 디렉토리 기준 또는 지정 경로 복사
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    info "파일 복사: $INSTALL_DIR"
    cp -r "$SCRIPT_DIR/." "$INSTALL_DIR/"
fi

cd "$INSTALL_DIR"

# ── 가상환경 + 패키지 설치 ────────────────────────────────────
info "가상환경 생성"
python3 -m venv .venv

info "패키지 설치"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .

# ── .env 설정 ────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env

    # JWT_SECRET 자동 생성
    JWT=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s/^JWT_SECRET=$/JWT_SECRET=$JWT/" .env
    else
        sed -i "s/^JWT_SECRET=$/JWT_SECRET=$JWT/" .env
    fi

    warn ".env 파일이 생성됐습니다. LLM API 키를 설정하세요:"
    echo "       $INSTALL_DIR/.env"
    echo ""
fi

# ── 데이터 디렉토리 ──────────────────────────────────────────
mkdir -p "$INSTALL_DIR/data"

# ── systemd 서비스 등록 (Linux) ───────────────────────────────
if command -v systemctl >/dev/null 2>&1 && [ "$(uname)" = "Linux" ]; then
    SERVICE_FILE="/etc/systemd/system/domain-agent-server.service"
    info "systemd 서비스 등록: $SERVICE_FILE"

    sudo tee "$SERVICE_FILE" > /dev/null << EOF
[Unit]
Description=domain-agent Server
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=DATABASE_URL=sqlite:///$INSTALL_DIR/data/domain_agent.db
Environment=PYTHONPATH=$INSTALL_DIR/src
ExecStart=$INSTALL_DIR/.venv/bin/python -m uvicorn server.main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable domain-agent-server
    sudo systemctl start domain-agent-server

    info "서비스 시작됨"
    echo ""
    echo "  서비스 관리:"
    echo "    sudo systemctl status domain-agent-server"
    echo "    sudo systemctl restart domain-agent-server"
    echo "    sudo journalctl -u domain-agent-server -f"

# ── launchd 서비스 등록 (macOS) ───────────────────────────────
elif [ "$(uname)" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.domain-agent.server.plist"
    info "launchd 서비스 등록: $PLIST"

    cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.domain-agent.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$INSTALL_DIR/.venv/bin/python</string>
        <string>-m</string>
        <string>uvicorn</string>
        <string>server.main:app</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DATABASE_URL</key>
        <string>sqlite:///$INSTALL_DIR/data/domain_agent.db</string>
        <key>PYTHONPATH</key>
        <string>$INSTALL_DIR/src</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/data/server.log</string>
    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/data/server.log</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

    launchctl load "$PLIST" 2>/dev/null || launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null || true
    info "서비스 시작됨"
    echo ""
    echo "  서비스 관리:"
    echo "    launchctl stop com.domain-agent.server"
    echo "    launchctl start com.domain-agent.server"
    echo "    tail -f $INSTALL_DIR/data/server.log"
fi

# ── 완료 ─────────────────────────────────────────────────────
echo ""
echo -e "  ${GREEN}설치 완료!${NC}"
echo ""
echo "  관리자 페이지:  http://localhost:$PORT/admin"
echo "  팀 대시보드:    http://localhost:$PORT/dashboard"
echo "  팀원 페이지:    http://localhost:$PORT/member"
echo "  API 문서:       http://localhost:$PORT/docs"
echo ""
echo "  다음 단계:"
echo "  1. .env 파일에 LLM API 키 설정"
echo "  2. http://localhost:$PORT/admin 에서 관리자 계정 생성"
echo "  3. 팀 생성 후 팀원들에게 설치 가이드 전달"
echo ""
