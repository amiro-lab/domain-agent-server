# domain-agent server

팀 도메인 지식을 수집·분석·공유하는 SaaS 백엔드.  
ChatGPT, Claude.ai, Claude Code 대화에서 자동으로 팀 지식을 추출합니다.

## 설치

### 방법 1: Docker (권장)

```bash
git clone https://github.com/your-org/domain-agent-server.git
cd domain-agent-server

cp .env.example .env
# .env 파일에서 LLM API 키 설정

docker compose up -d
```

브라우저에서 `http://localhost:8000/admin` 접속 → 관리자 계정 생성

### 방법 2: 직접 설치 (Linux/macOS)

```bash
git clone https://github.com/your-org/domain-agent-server.git
cd domain-agent-server
./install.sh
```

systemd(Linux) 또는 launchd(macOS) 서비스로 자동 등록됩니다.

## HTTPS 프로덕션 배포

```bash
# 1. nginx.conf에서 YOUR_DOMAIN, YOUR_EMAIL 교체
sed -i 's/YOUR_DOMAIN/api.yourcompany.com/g' nginx/nginx.conf
sed -i 's/YOUR_EMAIL/admin@yourcompany.com/g' docker-compose.prod.yml

# 2. SSL 인증서 발급
docker compose -f docker-compose.prod.yml --profile certbot run certbot

# 3. 프로덕션 서버 시작
docker compose -f docker-compose.prod.yml up -d
```

## 환경변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `LLM_PROVIDER` | `openai` 또는 `anthropic` | `openai` |
| `OPENAI_API_KEY` | OpenAI API 키 | - |
| `OPENAI_MODEL` | 사용 모델 | `gpt-4o-mini` |
| `ANTHROPIC_API_KEY` | Anthropic API 키 | - |
| `JWT_SECRET` | 관리자 토큰 서명 키 (고정 권장) | 자동 생성 |
| `DATABASE_URL` | DB 연결 문자열 | SQLite |

## 페이지

| URL | 설명 |
|-----|------|
| `/admin` | 관리자 페이지 (팀·멤버·프로바이더·토큰·감사로그) |
| `/dashboard` | 팀 모니터링 대시보드 (`?key=API키`) |
| `/member` | 팀원 개인 도메인 대시보드 |
| `/docs` | API 문서 (Swagger) |

## 관리자 초기 설정

1. `http://YOUR_SERVER/admin` 접속
2. 아이디/비밀번호 설정 (최초 1회)
3. **프로바이더** 탭 → LLM API 키 입력
4. **팀 관리** 탭 → 팀 생성 → 초대 코드 발급
5. 팀원들에게 초대 코드 전달
