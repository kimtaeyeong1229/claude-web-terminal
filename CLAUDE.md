# Claude Web Terminal

## Overview
Claude CLI의 웹 기반 멀티세션 터미널 UI. 브라우저에서 여러 Claude CLI 인스턴스를 탭으로 관리할 수 있다.

## Tech Stack
- **Backend**: Python 3.10+ / aiohttp (async) / PTY (pseudo-terminal) / WebSocket
- **Frontend**: Vanilla HTML/CSS/JS + XTerm.js 5.5.0 (no framework)
- **Language**: Korean localization (ko-KR)

## Project Structure
```
server.py              # Python 백엔드 (aiohttp, PTY 관리, WebSocket, REST API)
static/index.html      # 프론트엔드 전체 (HTML + CSS + JS, 단일 파일)
```

## Architecture
- `Session` 클래스: PTY fork/exec으로 Claude CLI 프로세스 생성, I/O 관리
- `SessionManager`: 세션 생성/삭제/목록, 10ms 간격 read loop로 WebSocket broadcast
- Frontend: XTerm.js 터미널 + WebSocket으로 실시간 I/O, 사이드바/탭 UI

## Key API Routes
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/sessions` | 세션 생성 (name, working_dir, extra_args) |
| GET | `/api/sessions` | 세션 목록 |
| DELETE | `/api/sessions/{id}` | 세션 삭제 |
| GET | `/ws/{id}` | 터미널 WebSocket |
| GET | `/api/external` | 외부 Claude 프로세스 감지 |

## Running
```bash
python3 server.py --host 0.0.0.0 --port 8080
```

## Environment Variables
- `CLAUDE_CMD`: Claude CLI 경로 (default: `claude`)

## Development Notes
- 프론트엔드는 `static/index.html` 단일 파일에 HTML/CSS/JS 모두 포함
- 인증 없음 — 신뢰할 수 있는 네트워크/로컬 사용 전제
- 스크롤백 버퍼 200KB, I/O 폴링 10ms
