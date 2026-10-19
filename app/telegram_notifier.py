import asyncio
import html
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx
from dotenv import load_dotenv

from app.config import HOME_ENV_PATH, settings
from app.converter import (
    execute_conversion,
    extract_video_metadata,
    format_file_size,
)
from app.database import (
    load_all_jobs,
    load_all_transcript_notes,
    load_job,
    load_transcript_note,
)
from app.models import ConversionJob, ConvertRequest, SummarizeRequest, TranscriptNote
from app.transcript_service import extract_and_summarize_video, format_note_as_markdown

YOUTUBE_LINK_EXTRACT_RE = re.compile(
    r"(https?://(?:www\.|m\.|music\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+))",
    re.IGNORECASE,
)


def get_bot3_credentials() -> tuple[str, str]:
    """~/.env 또는 로컬 .env에서 TELEGRAM_USER_ID와 TELEGRAM_BOT_3(@soracrabsss_bot) 토큰 로드"""
    if HOME_ENV_PATH.exists():
        load_dotenv(HOME_ENV_PATH, override=True)
    chat_id = os.getenv("TELEGRAM_USER_ID", settings.TELEGRAM_USER_ID).strip()
    bot3_raw = os.getenv("TELEGRAM_BOT_3", settings.TELEGRAM_BOT_3).strip()
    return chat_id, bot3_raw.replace(" ", "")


def build_main_menu_keyboard() -> Dict[str, Any]:
    """텔레그램 인라인 버튼 메인 메뉴"""
    return {
        "inline_keyboard": [
            [
                {"text": "📚 스크립트 정리 노트 보관함 (원할 때 열람)", "callback_data": "menu:notes"},
            ],
            [
                {"text": "📋 최근 MP3/MP4 변환 내역", "callback_data": "menu:all"},
            ],
            [
                {"text": "🎵 MP3 변환 목록", "callback_data": "menu:mp3"},
                {"text": "🎬 MP4 변환 목록", "callback_data": "menu:mp4"},
            ],
            [
                {"text": "🧪 샘플 스크립트 추출·정리", "callback_data": "sample:sum"},
                {"text": "🧪 샘플 MP3 변환", "callback_data": "sample:mp3:192"},
            ],
            [
                {"text": "📖 사용법 & 명령어 안내", "callback_data": "menu:help"},
            ],
        ]
    }


def build_persistent_reply_keyboard() -> Dict[str, Any]:
    """채팅창 하단 고정 앱 버튼 (ReplyKeyboardMarkup)"""
    return {
        "keyboard": [
            [{"text": "📚 정리 노트 보관함"}, {"text": "📋 최근 변환 목록"}, {"text": "🧪 샘플 스크립트 정리"}],
            [{"text": "🎵 MP3 목록"}, {"text": "🎬 MP4 목록"}, {"text": "🏠 메인 메뉴"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def build_url_format_selector_keyboard(video_id: str) -> Dict[str, Any]:
    """유튜브 링크 입력 시 [스크립트 추출 & 정리] 또는 [MP3/MP4 변환]을 원클릭 선택하는 인라인 키보드"""
    return {
        "inline_keyboard": [
            [
                {"text": "📝 스크립트 추출 & AI 핵심 정리 (노트 보관함 저장)", "callback_data": f"sum:{video_id}"},
            ],
            [
                {"text": "🎵 MP3 320kbps (최고음질)", "callback_data": f"conv:{video_id}:mp3:320"},
                {"text": "🎵 MP3 192kbps (추천)", "callback_data": f"conv:{video_id}:mp3:192"},
            ],
            [
                {"text": "🎵 MP3 128kbps (경량)", "callback_data": f"conv:{video_id}:mp3:128"},
                {"text": "🎬 MP4 최고화질 (Best)", "callback_data": f"conv:{video_id}:mp4:best"},
            ],
            [
                {"text": "🎬 MP4 1080p FHD", "callback_data": f"conv:{video_id}:mp4:1080p"},
                {"text": "🎬 MP4 720p HD (추천)", "callback_data": f"conv:{video_id}:mp4:720p"},
            ],
            [
                {"text": "🎬 MP4 480p SD", "callback_data": f"conv:{video_id}:mp4:480p"},
                {"text": "🎬 MP4 360p 경량", "callback_data": f"conv:{video_id}:mp4:360p"},
            ],
            [
                {"text": "📚 정리 노트 보관함", "callback_data": "menu:notes"},
                {"text": "🏠 메인 메뉴", "callback_data": "menu:home"},
            ],
        ]
    }


def build_notes_archive_view(query: Optional[str] = None) -> tuple[str, Dict[str, Any]]:
    """저장된 유튜브 스크립트 정리 노트들을 원할 때 언제든 꺼내볼 수 있는 보관함 뷰"""
    notes = load_all_transcript_notes(query=query, limit=8)
    total_notes = len(load_all_transcript_notes(limit=200))

    header = (
        f"📚 <b>[TubeShift 스크립트 정리 노트 보관함] (총 {total_notes}건 저장됨)</b>"
        if not query
        else f"🔍 <b>['{html.escape(query)}' 검색 결과 ({len(notes)}건)]</b>"
    )
    lines = [
        header,
        "내가 원할 때 언제든 아래 버튼을 눌러 <b>핵심 요약 · 타임라인 · 전체 스크립트 원문</b>을 다시 꺼내볼 수 있습니다.",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: List[List[Dict[str, str]]] = []

    if not notes:
        lines.append(
            "\n아직 보관된 스크립트 정리 노트가 없습니다.\n"
            "유튜브 링크를 채팅창에 보내고 <b>[📝 스크립트 추출 & AI 핵심 정리]</b>를 눌러보세요!"
        )
        buttons.append([{"text": "🧪 샘플 영상 스크립트 정리해보기", "callback_data": "sample:sum"}])
    else:
        for idx, n in enumerate(notes, 1):
            short_sum = html.escape(n.one_line_summary[:95]) + ("..." if len(n.one_line_summary) > 95 else "")
            lines.append(
                f"\n<b>{idx}. 📝 {html.escape(n.title)}</b>\n"
                f"👤 {html.escape(n.uploader)} ({n.duration_str}) · 🕒 {n.created_at[:16]}\n"
                f"💡 <i>{short_sum}</i>"
            )
            buttons.append(
                [
                    {
                        "text": f"📖 {idx}번 [{n.title[:18]}] 정리 내용 열람",
                        "callback_data": f"viewnote:{n.note_id}:summary",
                    }
                ]
            )

    buttons.append(
        [
            {"text": "📋 MP3/MP4 변환 목록", "callback_data": "menu:all"},
            {"text": "🏠 메인 메뉴", "callback_data": "menu:home"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_note_detail_view(note_id: str, tab: str = "summary") -> tuple[str, Dict[str, Any]]:
    """개별 스크립트 정리 노트의 [핵심 요약 / 상세 정리 / 타임스탬프 대본 원문] 탭 전환 뷰"""
    note = load_transcript_note(note_id)
    if not note:
        return "❌ 해당 스크립트 정리 노트를 찾을 수 없습니다.", build_main_menu_keyboard()

    title_esc = html.escape(note.title)
    uploader_esc = html.escape(note.uploader)

    if tab == "script":
        script_preview = html.escape(note.timestamped_transcript[:2800])
        if len(note.timestamped_transcript) > 2800:
            script_preview += "\n\n... (전체 대본은 아래 [📄 마크다운 파일 받기] 버튼으로 다운로드 가능)"
        text = (
            f"💬 <b>[전체 타임스탬프 스크립트 원문] {title_esc}</b>\n"
            f"👤 {uploader_esc} ({note.duration_str}) · 언어: <code>{note.language_used}</code> ({note.segment_count}구간)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<pre>{script_preview}</pre>"
        )
    elif tab == "detail":
        detail_clean = html.escape(note.detailed_summary_md[:2800])
        time_lines = "\n".join(f"• {html.escape(t)}" for t in note.timeline_highlights)
        text = (
            f"📖 <b>[상세 맥락 & 타임라인 정리] {title_esc}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>⏱ 타임라인 하이라이트</b>\n{time_lines or '• 타임라인 정보 없음'}\n\n"
            f"<b>📌 상세 정리 내용</b>\n{detail_clean}"
        )
    else:
        takeaways_str = "\n".join(f" ✅ {html.escape(k)}" for k in note.key_takeaways)
        time_str = "\n".join(f" • {html.escape(t)}" for t in note.timeline_highlights[:5])
        text = (
            f"📝 <b>[유튜브 스크립트 핵심 정리 노트]</b>\n"
            f"📌 <b>제목:</b> {title_esc}\n"
            f"👤 <b>채널:</b> {uploader_esc} ({note.duration_str}) · <code>{note.language_used}</code>\n"
            f"🔗 <a href=\"{note.url}\">원본 유튜브 영상 열기</a>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 <b>한 줄 핵심 요약</b>\n"
            f"<b>“{html.escape(note.one_line_summary)}”</b>\n\n"
            f"🎯 <b>핵심 요약 포인트 (Key Takeaways)</b>\n"
            f"{takeaways_str}\n\n"
            f"⏱ <b>주요 타임라인</b>\n"
            f"{time_str}"
        )

    kb = {
        "inline_keyboard": [
            [
                {"text": "💡 핵심 요약 보기", "callback_data": f"viewnote:{note.note_id}:summary"},
                {"text": "📖 상세 정리 보기", "callback_data": f"viewnote:{note.note_id}:detail"},
                {"text": "💬 전체 대본 원문", "callback_data": f"viewnote:{note.note_id}:script"},
            ],
            [
                {"text": "📄 정리본 마크다운(.md) 파일로 받기", "callback_data": f"sendnotemd:{note.note_id}"},
            ],
            [
                {"text": "🎵 이 영상 MP3 변환", "callback_data": f"conv:{note.video_id}:mp3:192"},
                {"text": "🎬 이 영상 MP4 변환", "callback_data": f"conv:{note.video_id}:mp4:720p"},
            ],
            [
                {"text": "📚 노트 보관함 목록으로", "callback_data": "menu:notes"},
                {"text": "🏠 메인 메뉴", "callback_data": "menu:home"},
            ],
        ]
    }
    return text, kb


def build_jobs_list_view(mode: str = "all") -> tuple[str, Dict[str, Any]]:
    target_fmt = mode if mode in ("mp3", "mp4") else None
    jobs = load_all_jobs(target_format=target_fmt, limit=8)
    all_jobs = load_all_jobs(limit=100)
    completed = [j for j in all_jobs if j.status == "completed"]
    total_bytes = sum(j.file_size_bytes for j in completed)

    if mode == "mp3":
        title = "🎵 <b>[TubeShift] 최근 MP3 오디오 변환 목록</b>"
    elif mode == "mp4":
        title = "🎬 <b>[TubeShift] 최근 MP4 비디오 변환 목록</b>"
    else:
        title = "📋 <b>[TubeShift] 최근 유튜브 MP3 · MP4 변환 내역</b>"

    lines = [
        title,
        f"• 누적 변환 완료: <b>{len(completed)}건</b> (총 보관 용량: <b>{format_file_size(total_bytes)}</b>)",
        "• 채팅창에 <b>유튜브 링크</b>를 붙여넣으시면 <b>[스크립트 정리]</b> 또는 <b>[MP3/MP4 변환]</b>을 선택할 수 있습니다!",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    buttons: List[List[Dict[str, str]]] = []

    if not jobs:
        lines.append("\n아직 변환된 파일이 없습니다. 유튜브 링크를 보내주시거나 아래 샘플 버튼을 눌러보세요!")
    else:
        for idx, j in enumerate(jobs, 1):
            badge = "🎵 MP3" if j.target_format == "mp3" else "🎬 MP4"
            qual = f"{j.quality}kbps" if j.target_format == "mp3" else j.quality
            norm_info = ""
            if j.audio_normalized and j.original_lufs is not None and j.normalized_lufs is not None:
                sign = "+" if (j.gain_db or 0) >= 0 else ""
                norm_info = f" · 🔊 <b>{j.original_lufs}→{j.normalized_lufs}LUFS ({sign}{j.gain_db}dB)</b>"
            lines.append(
                f"\n<b>{idx}. [{badge} · {qual}] {html.escape(j.title)}</b>\n"
                f"👤 {html.escape(j.uploader)} ({j.duration_str}) · 📦 <b>{j.file_size_human}</b>{norm_info}"
            )
            if j.status == "completed":
                buttons.append(
                    [
                        {
                            "text": f"📥 {idx}번 [{j.target_format.upper()}] 텔레그램으로 바로 받기",
                            "callback_data": f"sendjob:{j.job_id}",
                        }
                    ]
                )

    buttons.append(
        [
            {"text": "📚 정리 노트 보관함", "callback_data": "menu:notes"},
            {"text": "🏠 메인 메뉴", "callback_data": "menu:home"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": buttons}


def build_help_view() -> tuple[str, Dict[str, Any]]:
    text = "\n".join(
        [
            "📖 <b>[TubeShift 유튜브 스크립트 정리 & MP3·MP4 변환 봇 가이드]</b>",
            "━━━━━━━━━━━━━━━━━━━━",
            "<b>1️⃣ 유튜브 링크만 전송 (가장 편리한 방법)</b>",
            "채팅창에 유튜브 링크를 그냥 붙여넣으면 아래 선택 버튼이 뜹니다:",
            " • <b>📝 스크립트 추출 & AI 핵심 정리</b> (보관함에 영구 저장됨)",
            " • <b>🎵 MP3 320 / 192 / 128kbps</b> 오디오 추출",
            " • <b>🎬 MP4 최고화질 / 1080p / 720p</b> 비디오 변환",
            "",
            "<b>2️⃣ 정리된 내용을 내가 원할 때 다시 보기</b>",
            "• 하단 고정 버튼의 <b>[📚 정리 노트 보관함]</b> 또는 <code>/notes</code> 입력",
            "• 저장된 노트 목록에서 원하는 영상을 클릭하면 언제든 <b>핵심 요약 · 타임라인 · 전체 대본 원문 · 마크다운(.md) 파일</b>을 열람할 수 있습니다.",
            "• <code>검색 키워드</code> 형태로 찾고 싶은 단어를 검색할 수도 있습니다.",
            "",
            "<b>3️⃣ 한 줄 명령어로 즉시 실행</b>",
            "• <code>요약 https://youtu.be/...</code> (또는 <code>/sum 링크</code>) → 즉시 스크립트 추출 & 정리",
            "• <code>mp3 https://youtu.be/...</code> → 즉시 MP3 변환 후 오디오 파일 전송",
            "• <code>mp4 https://youtu.be/...</code> → 즉시 MP4 변환 후 비디오 파일 전송",
        ]
    )
    return text, build_main_menu_keyboard()


async def send_telegram_note_alert(
    note: TranscriptNote,
    target_chat_id: Optional[str] = None,
    send_md_file: bool = False,
) -> Dict[str, Any]:
    """추출·정리된 스크립트 노트를 TELEGRAM_BOT_3(@soracrabsss_bot)로 전송"""
    default_chat_id, token = get_bot3_credentials()
    chat_id = str(target_chat_id or default_chat_id).strip()
    if not chat_id or not token:
        return {"ok": False, "error": "~/.env에 TELEGRAM_BOT_3 또는 TELEGRAM_USER_ID가 없습니다."}

    api_base = f"https://api.telegram.org/bot{token}"
    text, kb = build_note_detail_view(note.note_id, tab="summary")

    async with httpx.AsyncClient(timeout=60.0) as client:
        if send_md_file:
            md_bytes = format_note_as_markdown(note).encode("utf-8")
            safe_name = f"summary_{note.video_id}_{note.note_id}.md"
            files = {"document": (safe_name, md_bytes, "text/markdown")}
            data = {
                "chat_id": chat_id,
                "caption": f"📄 <b>{html.escape(note.title)}</b> — 스크립트 핵심 정리 및 전체 대본 (.md)",
                "parse_mode": "HTML",
            }
            r_doc = await client.post(f"{api_base}/sendDocument", data=data, files=files)
            if r_doc.status_code == 200 and r_doc.json().get("ok"):
                return {"ok": True, "mode": "document", "note_id": note.note_id}

        resp = await client.post(
            f"{api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": kb,
            },
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return {"ok": True, "mode": "summary_view", "note_id": note.note_id}
        return {"ok": False, "error": resp.text}


async def send_telegram_conversion_alert(
    job: ConversionJob,
    base_url: str = f"http://localhost:{settings.PORT}",
    target_chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    """변환 완료된 MP3/MP4 파일을 TELEGRAM_BOT_3(@soracrabsss_bot)로 직접 전송"""
    default_chat_id, token = get_bot3_credentials()
    chat_id = str(target_chat_id or default_chat_id).strip()
    if not chat_id or not token:
        return {
            "ok": False,
            "error": "~/.env에 TELEGRAM_BOT_3 또는 TELEGRAM_USER_ID가 설정되어 있지 않습니다.",
        }

    download_link = f"{base_url.rstrip('/')}/api/jobs/{job.job_id}/download"
    fmt_badge = "🎵 MP3 오디오" if job.target_format == "mp3" else "🎬 MP4 비디오"
    qual_str = f"{job.quality}kbps" if job.target_format == "mp3" else job.quality

    loud_line = "🔊 <b>음량 균일화:</b> EBU R128 표준 적용\n"
    if job.audio_normalized and job.original_lufs is not None and job.normalized_lufs is not None:
        sign = "+" if (job.gain_db or 0) >= 0 else ""
        loud_line = (
            f"🔊 <b>음량 균일화:</b> <code>{job.original_lufs} LUFS → {job.normalized_lufs} LUFS ({sign}{job.gain_db}dB 자동 맞춤)</code>\n"
        )

    caption = (
        f"✅ <b>[TubeShift] 유튜브 {fmt_badge} 변환 완료</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 <b>제목:</b> {html.escape(job.title)}\n"
        f"👤 <b>채널:</b> {html.escape(job.uploader)} ({job.duration_str})\n"
        f"🎛 <b>포맷/품질:</b> {job.target_format.upper()} ({qual_str})\n"
        f"{loud_line}"
        f"📦 <b>파일 크기:</b> {job.file_size_human}\n"
        f"🔗 <b>원본 링크:</b> {job.url}\n"
        f"📥 <b>웹 다운로드:</b> <a href=\"{download_link}\">파일 바로 받기</a>"
    )

    api_base = f"https://api.telegram.org/bot{token}"
    file_path = Path(job.file_path) if job.file_path else None

    async with httpx.AsyncClient(timeout=120.0) as client:
        if file_path and file_path.exists() and job.file_size_bytes <= 48 * 1024 * 1024:
            endpoint = "sendAudio" if job.target_format == "mp3" else "sendVideo"
            field_name = "audio" if job.target_format == "mp3" else "video"
            try:
                with file_path.open("rb") as f:
                    files = {field_name: (job.file_name, f)}
                    data = {
                        "chat_id": chat_id,
                        "caption": caption,
                        "parse_mode": "HTML",
                    }
                    resp = await client.post(f"{api_base}/{endpoint}", data=data, files=files)
                    if resp.status_code == 200 and resp.json().get("ok"):
                        return {
                            "ok": True,
                            "bot_key": "TELEGRAM_BOT_3",
                            "mode": field_name,
                            "job_id": job.job_id,
                        }
            except Exception:
                pass

        resp = await client.post(
            f"{api_base}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
                "reply_markup": build_main_menu_keyboard(),
            },
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            return {
                "ok": True,
                "bot_key": "TELEGRAM_BOT_3",
                "mode": "message",
                "job_id": job.job_id,
            }
        return {"ok": False, "error": resp.text}


async def send_telegram_dashboard_menu(
    base_url: str = f"http://localhost:{settings.PORT}",
) -> Dict[str, Any]:
    """웹 대시보드에서 [텔레그램 봇 메뉴 호출] 클릭 시 TELEGRAM_BOT_3로 대화형 컨트롤러 발송"""
    chat_id, token = get_bot3_credentials()
    if not chat_id or not token:
        return {
            "ok": False,
            "error": "~/.env 파일에 TELEGRAM_USER_ID 또는 TELEGRAM_BOT_3 토큰이 없습니다.",
        }

    welcome_text = "\n".join(
        [
            "🎬 <b>[TubeShift] 유튜브 스크립트 정리 & MP3·MP4 변환 봇 (@soracrabsss_bot)</b>",
            "",
            "✅ <b>채팅창에 유튜브 링크만 붙여넣으세요!</b>",
            " • <b>📝 스크립트 추출 & AI 핵심 정리</b> (노트 보관함에 저장되어 내가 원할 때 언제든 다시 열람 가능!)",
            " • <b>🎵 MP3(320/192/128kbps)</b> 또는 <b>🎬 MP4(최고화질/1080p/720p)</b> 즉시 변환 & 파일 업로드",
            f" • 웹 대시보드 & 노트 보관함: <code>{base_url}</code>",
        ]
    )
    notes_text, notes_kb = build_notes_archive_view()
    sent_ids: List[int] = []
    bot_username = "soracrabsss_bot"

    async with httpx.AsyncClient(timeout=20.0) as client:
        me_resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
        if me_resp.status_code == 200:
            bot_username = me_resp.json().get("result", {}).get("username", bot_username)

        r1 = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": welcome_text,
                "parse_mode": "HTML",
                "reply_markup": build_persistent_reply_keyboard(),
            },
        )
        if r1.status_code == 200 and r1.json().get("ok"):
            sent_ids.append(r1.json()["result"]["message_id"])

        r2 = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": notes_text,
                "parse_mode": "HTML",
                "reply_markup": notes_kb,
            },
        )
        if r2.status_code == 200 and r2.json().get("ok"):
            sent_ids.append(r2.json()["result"]["message_id"])

    return {
        "ok": True,
        "bot_key": "TELEGRAM_BOT_3",
        "bot_username": bot_username,
        "messages_sent": len(sent_ids),
        "message_ids": sent_ids,
    }


class TelegramBot3Poller:
    """TELEGRAM_BOT_3 (@soracrabsss_bot) 실시간 Long-Polling 워커"""

    def __init__(self) -> None:
        self.offset: int = 0
        self.running: bool = False
        self.url_cache: Dict[str, str] = {
            "jNQXAC9IVRw": "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        }

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        _, token = get_bot3_credentials()
        if not token:
            return

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                await client.post(
                    f"https://api.telegram.org/bot{token}/setMyCommands",
                    json={
                        "commands": [
                            {"command": "start", "description": "🏠 메인 메뉴 및 하단 앱 버튼 열기"},
                            {"command": "notes", "description": "📚 저장된 스크립트 정리 노트 보관함 열기"},
                            {"command": "sum", "description": "📝 유튜브 스크립트 추출 & 핵심 정리 (예: /sum 링크)"},
                            {"command": "list", "description": "📋 최근 변환된 MP3/MP4 목록 보기"},
                            {"command": "mp3", "description": "🎵 유튜브 링크를 MP3로 즉시 변환"},
                            {"command": "mp4", "description": "🎬 유튜브 링크를 MP4로 즉시 변환"},
                            {"command": "help", "description": "📖 사용법 및 명령어 안내"},
                        ]
                    },
                )
            except Exception:
                pass

            while self.running:
                try:
                    resp = await client.get(
                        f"https://api.telegram.org/bot{token}/getUpdates",
                        params={"offset": self.offset, "timeout": 15},
                    )
                    if resp.status_code != 200:
                        await asyncio.sleep(2)
                        continue

                    updates = resp.json().get("result", [])
                    for upd in updates:
                        self.offset = upd["update_id"] + 1
                        asyncio.create_task(self.handle_update(upd))
                except asyncio.CancelledError:
                    break
                except Exception:
                    await asyncio.sleep(2)

    async def handle_update(self, upd: Dict[str, Any]) -> None:
        _, token = get_bot3_credentials()
        if not token:
            return

        async with httpx.AsyncClient(timeout=120.0) as client:
            if "callback_query" in upd:
                cq = upd["callback_query"]
                cq_id = cq["id"]
                data = cq.get("data", "")
                msg = cq.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id") or "")
                message_id = msg.get("message_id")

                await client.post(
                    f"https://api.telegram.org/bot{token}/answerCallbackQuery",
                    json={"callback_query_id": cq_id, "text": "⚡ TubeShift 처리 중..."},
                )

                if not chat_id:
                    return

                # A) 스크립트 정리 노트 상세 탭 전환 (viewnote:{note_id}:{tab})
                if data.startswith("viewnote:"):
                    parts = data.split(":", 2)
                    note_id = parts[1]
                    tab = parts[2] if len(parts) > 2 else "summary"
                    text, kb = build_note_detail_view(note_id, tab=tab)
                    if message_id:
                        await client.post(
                            f"https://api.telegram.org/bot{token}/editMessageText",
                            json={
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "text": text,
                                "parse_mode": "HTML",
                                "disable_web_page_preview": True,
                                "reply_markup": kb,
                            },
                        )
                    return

                # B) 스크립트 정리 노트 마크다운(.md) 파일 다운로드 (sendnotemd:{note_id})
                if data.startswith("sendnotemd:"):
                    note_id = data.split(":", 1)[1]
                    note = load_transcript_note(note_id)
                    if note:
                        await send_telegram_note_alert(note, target_chat_id=chat_id, send_md_file=True)
                    return

                # C) 유튜브 링크에서 스크립트 추출 & 정리 실행 (sum:{video_id})
                if data.startswith("sum:"):
                    video_id = data.split(":", 1)[1]
                    target_url = self.url_cache.get(
                        video_id, f"https://www.youtube.com/watch?v={video_id}"
                    )
                    await self._run_summarize_and_reply(client, token, chat_id, target_url)
                    return

                # D) 샘플 실행 (sample:sum 또는 sample:mp3:192)
                if data.startswith("sample:"):
                    sample_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
                    if data == "sample:sum":
                        await self._run_summarize_and_reply(client, token, chat_id, sample_url)
                    else:
                        _, fmt, qual = data.split(":", 2)
                        await self._run_conversion_and_reply(client, token, chat_id, sample_url, fmt, qual)
                    return

                # E) 기존 변환 파일 재전송 (sendjob:{job_id})
                if data.startswith("sendjob:"):
                    job_id = data.split(":", 1)[1]
                    job = load_job(job_id)
                    if job:
                        await send_telegram_conversion_alert(job, target_chat_id=chat_id)
                    return

                # F) 유튜브 MP3/MP4 변환 실행 (conv:{video_id}:{fmt}:{qual})
                if data.startswith("conv:"):
                    parts = data.split(":", 3)
                    if len(parts) == 4:
                        _, video_id, fmt, qual = parts
                        target_url = self.url_cache.get(
                            video_id, f"https://www.youtube.com/watch?v={video_id}"
                        )
                        await self._run_conversion_and_reply(
                            client, token, chat_id, target_url, fmt, qual
                        )
                    return

                # G) 일반 메뉴 전환
                text, reply_markup = self.route_menu_callback(data)
                if message_id:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/editMessageText",
                        json={
                            "chat_id": chat_id,
                            "message_id": message_id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "reply_markup": reply_markup,
                        },
                    )
                return

            if "message" in upd:
                msg = upd["message"]
                chat_id = str(msg.get("chat", {}).get("id") or "")
                user_text = (msg.get("text") or "").strip()
                if not chat_id or not user_text:
                    return

                await self.handle_text_message(client, token, chat_id, user_text)

    def route_menu_callback(self, data: str) -> tuple[str, Dict[str, Any]]:
        if data in ("menu:home", "menu:start"):
            return (
                "🎬 <b>[TubeShift 유튜브 스크립트 정리 & MP3·MP4 변환 메인 메뉴]</b>\n"
                "채팅창에 <b>유튜브 링크</b>를 붙여넣거나, 원하실 때 <b>[📚 스크립트 정리 노트 보관함]</b>을 열어보세요!",
                build_main_menu_keyboard(),
            )
        if data == "menu:notes":
            return build_notes_archive_view()
        if data == "menu:all":
            return build_jobs_list_view("all")
        if data == "menu:mp3":
            return build_jobs_list_view("mp3")
        if data == "menu:mp4":
            return build_jobs_list_view("mp4")
        if data == "menu:help":
            return build_help_view()
        return build_notes_archive_view()

    async def _run_summarize_and_reply(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        url: str,
        custom_focus: str = "",
    ) -> None:
        status_resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    f"📝 <b>유튜브 스크립트(자막) 추출 및 AI 핵심 정리를 진행 중입니다...</b>\n"
                    f"🔗 <code>{url}</code>\n"
                    f"오픈소스 <code>youtube-transcript-api</code>로 대본을 추출한 뒤 핵심 요약 노트를 만들어 보관함에 저장합니다."
                ),
                "parse_mode": "HTML",
            },
        )
        status_msg_id = None
        if status_resp.status_code == 200 and status_resp.json().get("ok"):
            status_msg_id = status_resp.json()["result"]["message_id"]

        try:
            req = SummarizeRequest(url=url, custom_focus=custom_focus, send_telegram=False)
            note = await asyncio.to_thread(extract_and_summarize_video, req)
            await send_telegram_note_alert(note, target_chat_id=chat_id, send_md_file=False)
            if status_msg_id:
                await client.post(
                    f"https://api.telegram.org/bot{token}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": status_msg_id},
                )
        except Exception as exc:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ <b>스크립트 추출·정리 실패:</b> {exc}",
                    "parse_mode": "HTML",
                    "reply_markup": build_main_menu_keyboard(),
                },
            )

    async def _run_conversion_and_reply(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        url: str,
        fmt: str,
        qual: str,
    ) -> None:
        target_format = "mp4" if fmt == "mp4" else "mp3"
        qual_label = f"{qual}kbps" if target_format == "mp3" else qual
        status_resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    f"⏳ <b>유튜브 {target_format.upper()} ({qual_label}) 변환을 시작합니다...</b>\n"
                    f"🔗 <code>{url}</code>\n"
                    f"잠시만 기다려주시면 변환된 미디어 파일을 바로 올려드립니다!"
                ),
                "parse_mode": "HTML",
            },
        )
        status_msg_id = None
        if status_resp.status_code == 200 and status_resp.json().get("ok"):
            status_msg_id = status_resp.json()["result"]["message_id"]

        try:
            req = ConvertRequest(
                url=url,
                target_format=target_format,  # type: ignore[arg-type]
                quality=qual,
                send_telegram=False,
            )
            job = await asyncio.to_thread(execute_conversion, req)
            await send_telegram_conversion_alert(job, target_chat_id=chat_id)
            if status_msg_id:
                await client.post(
                    f"https://api.telegram.org/bot{token}/deleteMessage",
                    json={"chat_id": chat_id, "message_id": status_msg_id},
                )
        except Exception as exc:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"❌ <b>변환 실패:</b> {exc}",
                    "parse_mode": "HTML",
                    "reply_markup": build_main_menu_keyboard(),
                },
            )

    async def handle_text_message(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        user_text: str,
    ) -> None:
        if user_text in ("/start", "🏠 메인 메뉴"):
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": (
                        "🎬 <b>[TubeShift] 유튜브 스크립트 정리 & MP3·MP4 변환 봇 (@soracrabsss_bot)</b>\n\n"
                        "• 채팅창에 <b>유튜브 링크</b>를 보내면 <b>[📝 스크립트 추출 & 핵심 정리]</b> 또는 <b>[🎵 MP3 / 🎬 MP4 변환]</b>을 선택할 수 있습니다.\n"
                        "• 정리된 내용은 <b>[📚 정리 노트 보관함]</b>에 저장되어 원하실 때 언제든 다시 꺼내볼 수 있습니다!"
                    ),
                    "parse_mode": "HTML",
                    "reply_markup": build_persistent_reply_keyboard(),
                },
            )
            text, kb = build_notes_archive_view()
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text in ("/notes", "📚 정리 노트 보관함"):
            text, kb = build_notes_archive_view()
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text in ("/list", "📋 최근 변환 목록"):
            text, kb = build_jobs_list_view("all")
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text == "🎵 MP3 목록":
            text, kb = build_jobs_list_view("mp3")
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text == "🎬 MP4 목록":
            text, kb = build_jobs_list_view("mp4")
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text in ("/help", "📖 사용법 안내"):
            text, kb = build_help_view()
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        if user_text == "🧪 샘플 스크립트 정리":
            await self._run_summarize_and_reply(
                client,
                token,
                chat_id,
                "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            )
            return

        if user_text.startswith(("검색 ", "/search ")):
            kw = re.sub(r"^(검색|/search)\s+", "", user_text).strip()
            text, kb = build_notes_archive_view(query=kw)
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": kb},
            )
            return

        url_match = YOUTUBE_LINK_EXTRACT_RE.search(user_text)
        if url_match:
            yt_url = url_match.group(1).strip()
            lower_text = user_text.lower()

            if lower_text.startswith(("/sum", "sum ", "요약", "정리", "스크립트")):
                await self._run_summarize_and_reply(client, token, chat_id, yt_url)
                return

            if lower_text.startswith(("/mp3", "mp3")):
                qual = "320" if "320" in lower_text else ("128" if "128" in lower_text else "192")
                await self._run_conversion_and_reply(client, token, chat_id, yt_url, "mp3", qual)
                return

            if lower_text.startswith(("/mp4", "mp4")):
                qual = "720p"
                for q_opt in ("1080p", "720p", "480p", "360p", "best"):
                    if q_opt in lower_text:
                        qual = q_opt
                        break
                await self._run_conversion_and_reply(client, token, chat_id, yt_url, "mp4", qual)
                return

            # 링크만 보낸 경우: 영상 분석 + [📝 스크립트 추출 & 핵심 정리] / [🎵 MP3] / [🎬 MP4] 선택창
            try:
                meta = await asyncio.to_thread(extract_video_metadata, yt_url)
                self.url_cache[meta.video_id] = meta.url
                preview_text = (
                    f"🔍 <b>[유튜브 영상 분석 완료]</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 <b>제목:</b> {html.escape(meta.title)}\n"
                    f"👤 <b>채널:</b> {html.escape(meta.uploader)}\n"
                    f"⏱ <b>재생시간:</b> {meta.duration_str} · 조회수 {meta.view_count:,}회\n\n"
                    f"👇 <b>[📝 스크립트 추출 & 핵심 정리] 또는 [MP3 / MP4 변환] 버튼을 눌러주세요!</b>"
                )
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": preview_text,
                        "parse_mode": "HTML",
                        "reply_markup": build_url_format_selector_keyboard(meta.video_id),
                    },
                )
            except Exception as exc:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": f"❌ 유튜브 영상 정보를 가져오지 못했습니다: {exc}",
                    },
                )
            return

        await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": (
                    "💡 <b>유튜브 링크</b>를 붙여넣으시면 <b>[📝 스크립트 추출 & 정리]</b> 및 <b>[🎵 MP3 / 🎬 MP4 변환]</b> 버튼이 뜹니다!\n"
                    "저장된 정리 노트를 보시려면 하단의 <b>[📚 정리 노트 보관함]</b>을 눌러주세요."
                ),
                "parse_mode": "HTML",
                "reply_markup": build_main_menu_keyboard(),
            },
        )


bot3_poller = TelegramBot3Poller()
