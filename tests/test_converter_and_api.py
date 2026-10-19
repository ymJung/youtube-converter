from pathlib import Path
from fastapi.testclient import TestClient

from app.converter import format_duration, format_file_size, is_valid_youtube_url, sanitize_filename
from app.main import app
from app.telegram_notifier import (
    build_jobs_list_view,
    build_main_menu_keyboard,
    build_notes_archive_view,
    build_persistent_reply_keyboard,
    build_url_format_selector_keyboard,
    get_bot3_credentials,
)

client = TestClient(app)
SAMPLE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def test_url_validation_and_helpers() -> None:
    assert is_valid_youtube_url("https://www.youtube.com/watch?v=jNQXAC9IVRw") is True
    assert is_valid_youtube_url("https://youtu.be/jNQXAC9IVRw") is True
    assert is_valid_youtube_url("https://www.youtube.com/shorts/abc12345") is True
    assert is_valid_youtube_url("https://example.com/video") is False
    assert format_duration(19) == "00:19"
    assert format_duration(3665) == "01:01:05"
    assert "KB" in format_file_size(250_000)
    assert sanitize_filename('Me at the zoo: "test" / video?') == "Me at the zoo test video"


def test_telegram_bot3_interface_builders() -> None:
    chat_id, token = get_bot3_credentials()
    assert bool(chat_id) is True
    assert ":" in token

    main_kb = build_main_menu_keyboard()
    assert "inline_keyboard" in main_kb

    reply_kb = build_persistent_reply_keyboard()
    assert reply_kb["is_persistent"] is True

    fmt_kb = build_url_format_selector_keyboard("jNQXAC9IVRw")
    callbacks = [btn["callback_data"] for row in fmt_kb["inline_keyboard"] for btn in row]
    assert "sum:jNQXAC9IVRw" in callbacks
    assert "conv:jNQXAC9IVRw:mp3:320" in callbacks
    assert "conv:jNQXAC9IVRw:mp4:720p" in callbacks

    list_text, list_kb = build_jobs_list_view("all")
    assert "TubeShift" in list_text
    assert "inline_keyboard" in list_kb

    notes_text, notes_kb = build_notes_archive_view()
    assert "보관함" in notes_text
    assert "inline_keyboard" in notes_kb


def test_protocol_and_dashboard_routes() -> None:
    proto_res = client.get("/api/protocol")
    assert proto_res.status_code == 200
    proto = proto_res.json()
    assert proto["port"] == 8010
    assert proto["isolated_from"]["port"] == 8000
    assert "TELEGRAM_BOT_3" in proto["engine"]["telegram_bot"]
    assert "youtube-transcript-api" in proto["engine"]["transcript_extractor"]

    dash_res = client.get("/")
    assert dash_res.status_code == 200
    assert "TubeShift" in dash_res.text
    assert "스크립트 핵심 정리 노트 보관함" in dash_res.text


def test_extract_video_info_endpoint() -> None:
    res = client.get("/api/info", params={"url": SAMPLE_URL})
    assert res.status_code == 200
    data = res.json()
    assert data["video_id"] == "jNQXAC9IVRw"
    assert "Me at the zoo" in data["title"]
    assert data["duration_sec"] == 19


def test_transcript_extract_summarize_and_archive() -> None:
    res = client.post(
        "/api/summarize",
        json={
            "url": SAMPLE_URL,
            "custom_focus": "핵심 내용을 2문장으로 정리",
            "send_telegram": False,
        },
    )
    assert res.status_code == 200
    note = res.json()
    assert note["video_id"] == "jNQXAC9IVRw"
    assert note["segment_count"] >= 1
    assert len(note["one_line_summary"]) > 0
    assert len(note["key_takeaways"]) >= 1
    assert "elephants" in note["full_transcript_text"].lower()

    # Check archive list & markdown download
    notes_res = client.get("/api/notes")
    assert notes_res.status_code == 200
    assert any(n["note_id"] == note["note_id"] for n in notes_res.json())

    md_res = client.get(f"/api/notes/{note['note_id']}/markdown")
    assert md_res.status_code == 200
    assert "Me at the zoo" in md_res.text


def test_convert_mp3_and_mp4_end_to_end() -> None:
    res_mp3 = client.post(
        "/api/convert",
        json={
            "url": SAMPLE_URL,
            "target_format": "mp3",
            "quality": "128",
            "normalize_audio": True,
            "send_telegram": False,
        },
    )
    assert res_mp3.status_code == 200
    job_mp3 = res_mp3.json()
    assert job_mp3["status"] == "completed"
    assert job_mp3["audio_normalized"] is True
    assert job_mp3["normalized_lufs"] is not None
    assert Path(job_mp3["file_path"]).exists()

    res_mp4 = client.post(
        "/api/convert",
        json={
            "url": SAMPLE_URL,
            "target_format": "mp4",
            "quality": "360p",
            "normalize_audio": True,
            "send_telegram": False,
        },
    )
    assert res_mp4.status_code == 200
    job_mp4 = res_mp4.json()
    assert job_mp4["status"] == "completed"
    assert job_mp4["audio_normalized"] is True
    assert job_mp4["normalized_lufs"] is not None
    assert Path(job_mp4["file_path"]).exists()
