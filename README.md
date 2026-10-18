# 🎬 TubeShift (유튜브 MP3 · MP4 오픈소스 변환 서버)

> **프로젝트 아키텍처 및 서버 프로토콜 가이드**
> 본 서비스는 유튜브 링크(`youtube.com`, `youtu.be`, `shorts`, `music`)를 입력받아 오픈소스 엔진(`yt-dlp` + `Deno EJS` + `FFmpeg 7.0`)으로 고음질 **MP3** 또는 고화질 **MP4** 파일로 변환·스트리밍·다운로드하는 서버입니다.
> 기존 워크스페이스의 [`git/auction-radar`](../auction-radar/README.md) (포트 `8000`)와 충돌하지 않도록 **전용 포트(`8010`) 및 독립 서버 프로토콜**로 동작하며, 웹 대시보드 인터페이스 역시 `AuctionRadar`의 다크모드 Tailwind UI 디자인 시스템을 계승했습니다.

---

## 1. 서버 프로토콜 정의 (`Port 8010` — 기존 프로젝트와 완전 분리)

| 항목 | 기존 `git/auction-radar` | 신규 `git/youtube-converter` (TubeShift) |
| :--- | :--- | :--- |
| **바인딩 주소 / 포트** | `0.0.0.0:8000` | **`0.0.0.0:8010`** |
| **핵심 오픈소스 엔진** | `FastAPI` + `BeautifulSoup4` | **`FastAPI` + `yt-dlp[default]` + `Deno EJS` + `FFmpeg 7.0.2-static`** |
| **데이터베이스** | `Docker MySQL 8.0` (`auction_radar` DB) | **`Docker MySQL 8.0` (`127.0.0.1:3306/youtube_converter` DB)** |
| **텔레그램 연동 봇** | `TELEGRAM_BOT_2` (`@gurucrabsss_bot`) | **`TELEGRAM_BOT_3` (`@soracrabsss_bot` · 소라크랩bot)** |

### REST API & 텔레그램 봇 인터페이스 명세
- **텔레그램 대화형 봇 (`TELEGRAM_BOT_3` · `@soracrabsss_bot`)**:
  - 채팅창에 **유튜브 링크만 붙여넣으면** 영상 메타데이터 분석 후 **`📝 스크립트 추출 & AI 핵심 정리`**, **MP3(`320`/`192`/`128kbps`)**, **MP4(`최고화질`/`1080p`/`720p`/`480p`/`360p`)** 원클릭 인라인 버튼이 즉시 표시됩니다.
  - **정리된 내용 원할 때 언제든 열람 (`📚 정리 노트 보관함`)**: 추출된 스크립트와 AI 요약본(한줄 요약, 핵심 포인트, 타임라인 하이라이트, 전체 대본 원문)은 SQLite DB(`transcript_notes`)에 영구 저장되며, 하단 고정 버튼 **`📚 정리 노트 보관함`** 또는 `/notes`를 눌러 언제든 다시 꺼내보고 마크다운(`.md`) 파일로 받을 수 있습니다.
  - 버튼을 누르거나 `요약 <링크>`, `mp3 <링크>`, `mp4 1080p <링크>` 형태로 입력하면 즉시 처리 후 결과를 채팅방에 보내줍니다.
- **`GET /`** : 반응형 웹 대시보드 UI (유튜브 링크 입력, 스크립트 정리 노트 보관함 검색·열람, MP3/MP4 변환 및 내장 플레이어)
- **`GET /api/protocol`** : 서버 프로토콜 및 기존 프로젝트와의 포트/리소스 분리 명세(JSON) 반환
- **`GET /api/info?url={youtube_url}`** : 다운로드 전 유튜브 영상의 제목, 채널명, 재생시간, 조회수, 썸네일 메타데이터 추출
- **`POST /api/summarize`** : 오픈소스 `youtube-transcript-api`(+ `yt-dlp` 자막 폴백)로 스크립트를 추출하고 `Gemini 2.5 Flash`로 한국어 핵심 요약·타임라인을 정리해 보관함에 저장
- **`GET /api/notes`** : 저장된 스크립트 정리 노트 보관함 목록 조회 (`?q=검색어` 지원)
- **`GET /api/notes/{note_id}`** : 단일 스크립트 정리 노트 상세 조회
- **`GET /api/notes/{note_id}/markdown`** : 정리된 요약본과 전체 타임스탬프 대본을 마크다운(`.md`) 문서로 다운로드
- **`DELETE /api/notes/{note_id}`** : 보관함 내 스크립트 정리 노트 삭제
- **`POST /api/telegram/send-note/{note_id}`** : 보관된 스크립트 정리 노트를 `TELEGRAM_BOT_3`로 즉시 전송
- **`POST /api/convert`** : JSON 바디(`url`, `target_format`: `"mp3"|"mp4"`, `quality`, `send_telegram`)를 받아 변환 수행 및 작업 레코드 반환
- **`GET /api/convert/direct?url={youtube_url}&format={mp3|mp4}&quality={...}`** : 쿼리스트링 한 줄로 즉시 변환 후 바이너리 미디어 파일 다운로드
- **`GET /api/jobs`** / **`GET /api/jobs/{job_id}`** / **`GET /api/jobs/{job_id}/download`** / **`DELETE /api/jobs/{job_id}`** : 미디어 변환 작업 관리
- **`POST /api/telegram/menu`** : `TELEGRAM_BOT_3` (`@soracrabsss_bot`) 채팅방으로 하단 앱 키보드 및 정리 노트 보관함 호출
- **`POST /api/telegram/send/{job_id}`** : 변환 완료된 MP3/MP4 파일을 텔레그램(`TELEGRAM_BOT_3`)으로 즉시 전송

---

## 2. 디렉토리 및 파일 구조

```text
youtube-converter/
├── .env.example               # 환경변수 설정 예시 (PORT=8010, TELEGRAM_BOT_3 등)
├── app/
│   ├── config.py              # 환경변수 로드, 포트(8010), FFmpeg/Deno 바이너리 경로 설정
│   ├── models.py              # Pydantic 스키마 (VideoMetadata, ConvertRequest, ConversionJob)
│   ├── converter.py           # 오픈소스 yt-dlp + FFmpeg MP3/MP4 추출 및 병합 핵심 엔진
│   ├── database.py            # SQLite(data/youtube_converter.db) 작업 이력 영속화
│   ├── telegram_notifier.py   # 텔레그램 Bot 3(@soracrabsss_bot) Long-Polling 대화형 인터페이스 & 미디어 업로드
│   ├── main.py                # FastAPI 서버 라우터 및 REST 프로토콜 엔드포인트
│   └── templates/
│       └── index.html         # AuctionRadar 스타일 다크모드 Tailwind 웹 대시보드 & 플레이어
├── bin/                       # 로컬 FFmpeg 7.0.2 정적 바이너리 심볼릭 링크 및 Deno 런타임
├── scripts/
│   └── keepalive.sh           # 포트 8010 서버 무중단 유지 워치독 데몬 스크립트
├── tests/
│   └── test_converter_and_api.py # 메타데이터 파싱 및 실제 유튜브 MP3/MP4 변환 E2E 테스트
├── data/
│   └── youtube_converter.db   # SQLite 데이터베이스 (자동 생성)
├── downloads/                 # 변환된 MP3/MP4 미디어 파일 저장소
└── logs/                      # 서버 실행 로그 및 PID 파일
```

---

## 3. 서버 실행 및 테스트 방법

```bash
# 1. 무중단 데몬 스크립트로 서버 기동 (포트 8010)
./scripts/keepalive.sh

# 2. 서버 상태 및 프로토콜 확인
curl -s http://localhost:8010/api/protocol | python3 -m json.tool

# 3. CLI에서 유튜브 링크를 MP3 또는 MP4로 즉시 변환·다운로드
curl -OJ "http://localhost:8010/api/convert/direct?url=https://www.youtube.com/watch?v=jNQXAC9IVRw&format=mp3&quality=192"
curl -OJ "http://localhost:8010/api/convert/direct?url=https://www.youtube.com/watch?v=jNQXAC9IVRw&format=mp4&quality=720p"

# 4. 전체 자동화 테스트 실행
~/.local/bin/uv run pytest -v
```
