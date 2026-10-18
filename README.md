# Padlet Sender 웹 패널

GitHub Pages 웹 패널과 고정된 Render 서버로 익명 Padlet 게시판에 글을 전송합니다. 원본 Python의 제목·본문, 횟수·간격, 중지 기능을 유지하고 브라우저 캡처를 **HTTP 익명 세션 초기화**로 교체했습니다. 공식 Padlet API 키를 사용하지 않습니다.

사용자는 웹사이트만 열면 됩니다. 도우미, Python, 확장 프로그램 설치·실행이나 로그인, 가상 브라우저 조작이 필요하지 않습니다. Render도 Chromium을 실행하지 않습니다.

- 웹 패널: https://dev-cat0.github.io/padlet/
- 서버: https://padlet-sender-panel.onrender.com
- Render 관리: https://dashboard.render.com/web/srv-dbae7t0m7kps738gj5c0

## 사용 방법

1. 웹 패널을 열면 기존 Render 서버에 자동으로 연결됩니다.
2. 대상 게시판 주소를 입력하고 **익명 게시판 연결**을 누릅니다. 게시판 HTML과 초기 상태를 GET으로 읽어 익명 쿠키·CSRF 토큰·쓰기 권한을 확인합니다. 이 단계에서는 글을 작성하지 않습니다.
3. 게시판 이름과 연결 완료 표시를 확인합니다. 섹션이 있는 게시판은 기본 섹션에 작성하며, 승인 필요 여부를 표시합니다.
4. 제목·본문·횟수·간격을 입력하고 **전송 시작**을 누르면 실제 글을 게시합니다. 기본값은 원본과 같은 50회 / 0.3초, 0회는 무제한입니다.
5. **중지**를 누르면 진행 중인 요청이 끝난 뒤 멈춥니다. **익명 세션 종료**는 작업을 중지하고 서버의 쿠키·토큰·연결 정보를 삭제합니다.

서버에서 작업하므로 탭을 닫아도 전송이 자동으로 중지되지 않습니다. 먼저 중지하세요. 같은 탭 새로고침은 상태를 복구하지만 탭을 닫아 세션 식별자가 사라지면 다시 접근하지 못할 수 있습니다. 서버 재시작·재배포 시 모든 세션과 작업은 사라집니다. 30분간 전송·게시판 연결이 없으면 익명 연결을 정리합니다.

## 구성과 배포

- `web/`: 빌드 없는 정적 웹 패널. `web/config.js`의 Render 주소를 사용합니다.
- `server/anonymous.py`: 공개 웹 클라이언트의 익명 초기화 및 글쓰기 요청 필드 구성.
- `server/engine.py`: 메모리 내 HTTP 세션과 전송·중지 루프.
- `server/sessions.py`: 사용자별 세션 분리와 만료.
- `desktop/padlet.py`: 전달받은 원본 보존용.
- `Dockerfile`: Python 3.12 slim 실행 환경. 브라우저 의존성 없음.

GitHub Pages Source는 **GitHub Actions**입니다. `main`의 `web/` 변경 시 `.github/workflows/pages.yml`이 배포합니다. Render는 기존 `padlet-sender-panel` 무료 Docker 서비스를 사용합니다. `render.yaml`은 대체 Blueprint용이므로 중복 서비스를 생성하지 마세요.

Render 설정: `main`, Dockerfile `./Dockerfile`, 루트 디렉터리 없음, Singapore, Free, health check `/healthz`, `ALLOWED_ORIGINS=https://dev-cat0.github.io`. 서버 상태가 메모리에 있으므로 Uvicorn worker와 Render 인스턴스는 각각 하나로 유지합니다. 무료 서버 휴면 후 첫 연결에는 기동 시간이 걸릴 수 있습니다.

## 개발 및 검증

아래 설치·실행은 개발자용이며 웹 패널 사용자에게는 필요하지 않습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
$env:ALLOWED_ORIGINS = 'http://127.0.0.1:4173'
.\.venv\Scripts\python -m uvicorn server.app:app --host 127.0.0.1 --port 4174 --no-access-log
```

별도 터미널에서 `python -m http.server 4173 --bind 127.0.0.1 --directory web`을 실행합니다. 로컬 검증 때만 `web/config.js`의 주소를 `http://127.0.0.1:4174`로 바꾸고 배포 전 되돌립니다.

```powershell
.\.venv\Scripts\python -m pytest -q
node --check web/app.js
```

테스트는 초기화의 GET 전용 동작, 익명 쓰기 권한, 외부 리디렉션 거부, 세션 분리·종료, 인증·CORS, 횟수·중지·오류 및 429 대기를 확인합니다. **2026-10-19 검증에서 로컬 HTTP 익명 연결은 성공했지만, Render 배포 환경에서는 Padlet이 HTTP 403으로 거부했습니다. 현재 Render에서 대상 게시판 연결·전송은 불가능한 상태입니다. 실제 글 생성은 테스트하지 않았습니다.**

## 동작 범위

- 익명 방문자에게 쓰기를 허용한 일반 게시판을 대상으로 합니다. 로그인·비밀번호·보안 확인이 필요하면 연결을 중단합니다.
- 익명 쿠키와 CSRF 토큰은 사용자별 서버 메모리에만 보관하며 상태 API와 로그에 노출하지 않습니다. 초기화와 전송은 같은 HTTP 세션을 사용해 갱신된 쿠키도 유지합니다.
- 게시판 주소는 HTTPS Padlet 호스트만 허용하고 리디렉션도 매번 검사합니다. 게시 요청은 리디렉션을 따르지 않습니다.
- 세션 생성은 분당 20회, 총 32개로 제한합니다. 공개 자동 연결이므로 사용자 계정 인증을 제공하는 것은 아닙니다.
- 본문은 일반 텍스트를 HTML 이스케이프해 줄바꿈을 보존합니다. 관리자 승인이 필요한 게시판은 미승인 상태로 요청합니다.
- 429는 3초씩 증가하는 대기(최대 30초)와 숫자형 Retry-After를 따릅니다. 다른 HTTP 오류나 타임아웃은 중지합니다. 응답 유실 시 중복 게시를 피하도록 자동 재전송하지 않습니다.
- Padlet의 비공개 웹 요청 형식은 변경될 수 있습니다. 연결 성공만으로 게시 성공이 보장되지는 않습니다. 필수 사용자 지정 필드 등 추가 조건이 있는 게시판은 지원하지 않을 수 있습니다.
