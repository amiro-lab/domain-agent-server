#!/bin/bash
# 로컬 테스트 환경 초기 셋업 (팀 생성 + API 키 발급)
set -e

BASE_URL="${1:-http://localhost:8000}"
TEAM_NAME="${2:-test-team}"

echo "=== domain-agent 로컬 테스트 셋업 ==="
echo "서버: $BASE_URL"
echo ""

# 팀 생성
RESPONSE=$(curl -sf -X POST "$BASE_URL/admin/team?name=$TEAM_NAME")
if [ $? -ne 0 ]; then
  echo "서버에 연결할 수 없습니다. dev.sh로 서버를 먼저 실행하세요."
  exit 1
fi

TEAM_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['team_id'])")
API_KEY=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['api_key'])")

echo "팀 ID:  $TEAM_ID"
echo "API 키: $API_KEY"
echo ""
echo "domain-agent install 시 아래를 입력하세요:"
echo "  SaaS URL: $BASE_URL"
echo "  팀 API 키: $API_KEY"
echo ""
echo "# 또는 바로 config에 저장:"
echo "python3 -c \""
echo "import json, pathlib"
echo "p = pathlib.Path.home() / '.claude/domain-agent/config.json'"
echo "p.parent.mkdir(parents=True, exist_ok=True)"
echo "p.write_text(json.dumps({'mode':'team','saas_url':'$BASE_URL','team_api_key':'$API_KEY'}, indent=2))"
echo "print('저장 완료')\""
