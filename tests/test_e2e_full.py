from pathlib import Path
from typing import Any, Dict
import httpx
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_connection, load_job, load_transcript_note
from app.main import app
from app.telegram_notifier import bot3_poller

client = TestClient(app)
SAMPLE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
LIVE_BASE_URL = f"http://127.0.0.1:{settings.PORT}"


def test_e2e_1_protocol_and_mysql_database_connectivity() -> None:
    """E2E 1: 서버 프로토콜 분리 검증 및 Docker MySQL 8.0 연결·테이블 스키마 검증"""
    proto_res = client.get("/api/protocol")
    assert proto_res.status_code == 200
    proto = proto_res.json()
    assert proto["port"] == 8010
    assert proto["isolated_from"]["port"] == 8000
    assert "Docker MySQL 8.0" in proto["engine"]["database"]
    assert "TELEGRAM_BOT_3" in proto["engine"]["telegram_bot"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE() AS db_name")
            row = cur.fetchone()
            assert row is not None
            assert row["db_name"] == settings.MYSQL_DATABASE

            cur.execute("SHOW TABLES")
            tables = {list(r.values())[0] for r in cur.fetchall()}
            assert "conversion_jobs" in tables
            assert "transcript_notes" in tables


def test_e2e_2_video_info_and_invalid_url_handling() -> None:
    """E2E 2: 유튜브 영상 메타데이터 조회 및 잘못된 링크 예외 처리 검증"""
    bad_res = client.get("/api/info", params={"url": "https://invalid-domain.com/watch?v=123"})
    assert bad_res.status_code == 400

    info_res = client.get("/api/info", params={"url": SAMPLE_URL})
    assert info_res.status_code == 200
    info = info_res.json()
    assert info["video_id"] == "jNQXAC9IVRw"
    assert "Me at the zoo" in info["title"]
    assert info["duration_sec"] == 19
    assert "320" in info["available_mp3_qualities"]
    assert "720p" in info["available_mp4_qualities"]


def test_e2e_3_transcript_extraction_summarization_archive_and_cleanup() -> None:
    """E2E 3: 유튜브 스크립트 추출 -> AI 요약 -> Docker MySQL 보관함 저장 -> 키워드 검색 -> 마크다운 다운로드 -> 삭제"""
    # 1. 스크립트 추출 및 요약 생성
    sum_res = client.post(
        "/api/summarize",
        json={
            "url": SAMPLE_URL,
            "custom_focus": "E2E 테스트용 핵심 요약",
            "send_telegram": False,
        },
    )
    assert sum_res.status_code == 200
    note = sum_res.json()
    note_id = note["note_id"]
    assert note["video_id"] == "jNQXAC9IVRw"
    assert note["segment_count"] >= 1
    assert len(note["one_line_summary"]) > 0
    assert len(note["key_takeaways"]) >= 1
    assert "elephants" in note["full_transcript_text"].lower()

    # 2. Docker MySQL DB 직접 조회 검증
    db_note = load_transcript_note(note_id)
    assert db_note is not None
    assert db_note.note_id == note_id

    # 3. 보관함 키워드 검색 API 검증
    search_res = client.get("/api/notes", params={"q": "elephants"})
    assert search_res.status_code == 200
    assert any(n["note_id"] == note_id for n in search_res.json())

    # 4. 단일 노트 상세 조회 및 마크다운(.md) 다운로드 검증
    detail_res = client.get(f"/api/notes/{note_id}")
    assert detail_res.status_code == 200
    assert detail_res.json()["note_id"] == note_id

    md_res = client.get(f"/api/notes/{note_id}/markdown")
    assert md_res.status_code == 200
    assert "text/markdown" in md_res.headers["content-type"]
    assert "# 📝" in md_res.text
    assert "elephants" in md_res.text.lower()

    # 5. 텔레그램 Bot 3 노트 뷰 렌더링 검증
    text_sum, kb_sum = bot3_poller.route_menu_callback("menu:notes")
    assert "보관함" in text_sum
    assert "inline_keyboard" in kb_sum

    # 6. 테스트 생성 노트 삭제 및 MySQL 반영 확인
    del_res = client.delete(f"/api/notes/{note_id}")
    assert del_res.status_code == 200
    assert del_res.json()["deleted_note_id"] == note_id
    assert load_transcript_note(note_id) is None


def test_e2e_4_mp3_and_mp4_conversion_streaming_and_cleanup() -> None:
    """E2E 4: 유튜브 MP3/MP4 실제 변환 -> MySQL 기록 -> 스트리밍/다운로드 -> 파일 및 DB 삭제 전체 라이프사이클"""
    # 1. MP3 변환 및 다운로드
    mp3_res = client.post(
        "/api/convert",
        json={
            "url": SAMPLE_URL,
            "target_format": "mp3",
            "quality": "128",
            "send_telegram": False,
        },
    )
    assert mp3_res.status_code == 200
    mp3_job = mp3_res.json()
    mp3_id = mp3_job["job_id"]
    mp3_path = Path(mp3_job["file_path"])
    assert mp3_job["status"] == "completed"
    assert mp3_path.exists()
    assert load_job(mp3_id) is not None

    # MP3 인라인 스트리밍 및 첨부 다운로드 헤더 검증
    stream_mp3 = client.get(f"/api/jobs/{mp3_id}/download", params={"inline": "true"})
    assert stream_mp3.status_code == 200
    assert stream_mp3.headers["content-type"] == "audio/mpeg"
    assert "inline;" in stream_mp3.headers["content-disposition"]
    assert stream_mp3.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")

    # 2. MP4 변환 및 다운로드
    mp4_res = client.post(
        "/api/convert",
        json={
            "url": SAMPLE_URL,
            "target_format": "mp4",
            "quality": "360p",
            "send_telegram": False,
        },
    )
    assert mp4_res.status_code == 200
    mp4_job = mp4_res.json()
    mp4_id = mp4_job["job_id"]
    mp4_path = Path(mp4_job["file_path"])
    assert mp4_job["status"] == "completed"
    assert mp4_path.exists()

    dl_mp4 = client.get(f"/api/jobs/{mp4_id}/download")
    assert dl_mp4.status_code == 200
    assert dl_mp4.headers["content-type"] == "video/mp4"
    assert b"ftyp" in dl_mp4.content[:32]

    # 3. 필터링 조회 검증 (/api/jobs?target_format=mp3)
    jobs_mp3 = client.get("/api/jobs", params={"target_format": "mp3"}).json()
    assert any(j["job_id"] == mp3_id for j in jobs_mp3)

    # 4. 작업 삭제 시 로컬 파일 및 MySQL 레코드 동시 삭제 검증
    assert client.delete(f"/api/jobs/{mp3_id}").status_code == 200
    assert not mp3_path.exists()
    assert load_job(mp3_id) is None

    assert client.delete(f"/api/jobs/{mp4_id}").status_code == 200
    assert not mp4_path.exists()
    assert load_job(mp4_id) is None


def test_e2e_5_live_running_server_http_verification() -> None:
    """E2E 5: 실제 기동 중인 8010번 포트 데몬 서버에 실제 HTTP 요청을 보내 응답 검증"""
    with httpx.Client(base_url=LIVE_BASE_URL, timeout=20.0) as http_client:
        r_dash = http_client.get("/")
        assert r_dash.status_code == 200
        assert "TubeShift" in r_dash.text
        assert "스크립트 핵심 정리 노트 보관함" in r_dash.text

        r_proto = http_client.get("/api/protocol")
        assert r_proto.status_code == 200
        data: Dict[str, Any] = r_proto.json()
        assert data["port"] == 8010
        assert data["engine"]["telegram_poller_running"] is True
        assert "Docker MySQL 8.0" in data["engine"]["database"]
