# Padlet Sender 웹 패널

제공된 `padlet.py`의 **브라우저 로그인 → 게시 요청 캡처 → 반복 전송** 흐름을 GitHub Pages + Render로 옮긴 프로젝트입니다. 공식 Padlet API 키는 사용하지 않습니다.

- 웹 패널: https://dev-cat0.github.io/padlet/
- 서버: https://padlet-sender-panel.onrender.com
- 서버 관리: https://dashboard.render.com/web/srv-dbae7t0m7kps738gj5c0

- `web/`: 빌드 없이 GitHub Pages에서 실행하는 한국어 웹 패널
- `server/`: FastAPI, Playwright Chromium, curl_cffi 서버
- `desktop/padlet.py`: 전달받은 데스크톱 원본
- `Dockerfile`: Chromium 포함 Render 실행 환경
- `render.yaml`: 수동 Blueprint 배포를 선택할 때 사용할 구성

## 사용 순서

1. GitHub Pages의 웹 패널을 엽니다.
2. **패널 접속 키**만 입력하고 연결합니다. 서버 주소는 고정되어 있습니다. 접속 키는 Render 서비스의 Environment에 있는 `PANEL_TOKEN` 값입니다. Padlet 비밀번호나 Padlet API 키가 아닙니다.
3. 대상 `https://padlet.com/...` 주소를 입력하고 **브라우저 열기 & 로그인**을 누릅니다.
4. 패널 하단에 나타난 서버 브라우저 화면에서 로그인합니다. 입력란을 클릭한 뒤 별도 텍스트 입력창에 내용을 입력하고 **텍스트 입력**을 누릅니다. 한글, 이메일, 비밀번호를 이 방식으로 입력할 수 있습니다. Tab/Enter/스크롤 버튼과 팝업 탭 선택도 지원합니다.
5. 해당 게시판에서 게시물을 한 번 작성합니다. 이 글은 실제로 게시됩니다. 성공한 `/api/9/wishes` POST 요청을 캡처하면 브라우저가 닫히고 시작 버튼이 활성화됩니다.
6. 제목, 본문, 전송 횟수, 간격을 설정하고 시작합니다. 기본값은 원본과 같은 50회 / 0.3초이며 **0회는 무제한**입니다.
7. 중지를 누르면 진행 중인 요청이 끝난 뒤 멈춥니다(요청 제한시간 10초). 로그를 파일로 내려받을 수 있습니다.
8. **로그인·캡처 정보 삭제**로 서버의 로그인 세션과 캡처 데이터를 지웁니다.

화면을 닫는 것만으로 서버의 전송 작업이 중지되지는 않습니다. 다시 연결해 중지하거나 세션을 종료하세요. 같은 접속 키를 사용하는 창들은 같은 실행 상태를 공유합니다.

## 배포

### GitHub Pages

저장소 Settings → Pages → Source를 **GitHub Actions**로 설정합니다. `main`의 `web/` 변경 시 `.github/workflows/pages.yml`이 배포합니다. 예상 URL은 `https://dev-cat0.github.io/padlet/`입니다.

### Render

기존 서비스와 별도로 `padlet-sender-panel` Docker 웹 서비스를 사용합니다. 직접 생성한 서비스가 있다면 `render.yaml`로 중복 생성하지 마세요. Blueprint로 새로 배포할 경우:

[Render에서 Blueprint 열기](https://dashboard.render.com/blueprint/new?repo=https://github.com/Dev-Cat0/padlet)

- Runtime: Docker
- Branch: main
- Root directory: 비워 둠
- Dockerfile: `./Dockerfile`
- Health check: `/healthz`
- Region: Singapore
- Instance: Free (단일 인스턴스)
- `PANEL_TOKEN`: 32자 이상의 임의 접속 키. Blueprint는 자동 생성합니다.
- `ALLOWED_ORIGINS`: `https://dev-cat0.github.io` (경로 `/padlet/`는 포함하지 않음)

서버 주소는 `web/config.js`의 `serverUrl`로 고정되며 패널에서 변경할 수 없습니다. 서버 URL만 공개하고 접속 키나 쿠키는 저장소에 넣지 마세요. 직접 생성한 Render 서비스 설정은 Dashboard가 관리하며 `render.yaml`은 대체 배포용입니다.

Chromium은 메모리를 사용하므로 무료 인스턴스에서 메모리 부족이 발생하면 Render 로그를 확인하세요. 유료 전환은 사용자가 선택해야 합니다. 서버가 재시작되거나 재배포되면 로그인, 캡처, 로그, 진행 중인 전송은 복구되지 않습니다. 인메모리 상태를 사용하므로 Uvicorn worker와 Render 인스턴스는 각각 하나로 유지합니다.

## 로컬 실행

Python 3.12와 Node.js(선택적 JS 문법 확인)를 사용합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m playwright install chromium
$env:PANEL_TOKEN = '<32자 이상의 로컬 접속 키>'
$env:ALLOWED_ORIGINS = 'http://127.0.0.1:4173'
.\.venv\Scripts\python -m uvicorn server.app:app --host 127.0.0.1 --port 4174 --no-access-log
```

다른 터미널:

```powershell
.\.venv\Scripts\python -m http.server 4173 --bind 127.0.0.1 --directory web
```

로컬 검증 시에만 `web/config.js`의 `serverUrl`을 `http://127.0.0.1:4174`로 바꾸고 `http://127.0.0.1:4173`을 엽니다. 배포 전에는 Render 주소로 되돌립니다.

```powershell
.\.venv\Scripts\python -m pytest -q
node --check web/app.js
```

## 원본과의 차이 및 제한

- 로컬 팝업 브라우저 대신 Render의 headless Chromium을 이미지·클릭·키보드로 조작합니다. Google/Microsoft 등의 로그인 제공자가 서버 브라우저 로그인을 제한할 수 있습니다. CAPTCHA나 로그인 정책을 자동 우회하지 않습니다.
- 헤더는 Playwright `all_headers()`로 읽어 쿠키를 포함합니다. 캡처한 헤더·본문은 서버 메모리에만 두며 상태 응답이나 로그로 반환하지 않습니다. 접속 키는 브라우저 메모리에만 보관합니다.
- 429에는 원본의 지수 아닌 3초씩 증가하는 대기(최대 30초)를 유지하며 숫자형 `Retry-After`가 더 길면 이를 따릅니다. 인증 오류나 다른 HTTP 오류에는 중지합니다. 타임아웃은 게시 성공 여부가 불명확하므로 자동 재전송하지 않습니다.
- 본문은 일반 텍스트로 HTML 이스케이프하며 줄바꿈을 보존합니다.
- 30분 동안 전송이나 브라우저 조작이 없으면 로그인·캡처 세션을 삭제합니다.
- 패널은 1인 운영용입니다. 모든 제어 API에 접속 키가 필요합니다. CORS만으로 인증을 대신하지 않습니다.
- Padlet 내부 요청 형식은 변경될 수 있습니다. 캡처 또는 전송이 실패하면 다시 로그인·캡처해야 하며, 내부 API가 바뀌면 코드 수정이 필요할 수 있습니다.

자동 테스트는 모의 응답으로 요청 캡처, 인증, CORS, 전송 횟수, 429 대기, 중지, 오류 처리를 확인합니다. 실제 계정 로그인과 게시 성공은 사용자의 Padlet 계정·게시판으로 확인해야 합니다.
