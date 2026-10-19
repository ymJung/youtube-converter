import asyncio
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.converter import (
    create_or_reuse_queued_job,
    execute_conversion,
    extract_video_metadata,
    format_file_size,
    is_valid_youtube_url,
    normalize_all_completed_jobs,
    normalize_existing_job,
    process_queued_job,
    sanitize_filename,
)
from app.database import (
    delete_job_record,
    delete_transcript_note,
    init_db,
    load_active_queue_jobs,
    load_all_jobs,
    load_all_transcript_notes,
    load_job,
    load_transcript_note,
)
from app.models import (
    ConversionJob,
    ConvertRequest,
    SummarizeRequest,
    TranscriptNote,
    VideoMetadata,
)
from app.telegram_notifier import (
    bot3_poller,
    send_telegram_conversion_alert,
    send_telegram_dashboard_menu,
    send_telegram_note_alert,
)
from app.transcript_service import extract_and_summarize_video, format_note_as_markdown

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=(
        "유튜브 링크를 입력받아 오픈소스(youtube-transcript-api + yt-dlp + FFmpeg 7.0 + Gemini 2.5 Flash)로 "
        "스크립트 추출·핵심 정리 노트 보관 및 고음질 MP3 / 고화질 MP4 변환을 수행하는 서버. "
        "기존 git/auction-radar(포트 8000, TELEGRAM_BOT_2)와 충돌하지 않도록 전용 포트(8010) 및 TELEGRAM_BOT_3(@soracrabsss_bot)로 동작합니다."
    ),
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

conversion_queue: asyncio.Queue[Tuple[str, bool, str]] = asyncio.Queue()
_worker_task: Optional[asyncio.Task[None]] = None


async def _conversion_queue_worker() -> None:
    """Sequential background worker that processes queued conversion jobs one by one."""
    while True:
        job_id, send_tg, base_url = await conversion_queue.get()
        try:
            current = load_job(job_id)
            if not current or current.status == "completed":
                continue
            finished_job = await asyncio.to_thread(process_queued_job, job_id)
            if send_tg and finished_job.status == "completed":
                await send_telegram_conversion_alert(finished_job, base_url=base_url)
        except Exception:
            pass
        finally:
            conversion_queue.task_done()


def _ensure_worker_running() -> None:
    global _worker_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _worker_task is None or _worker_task.done():
        _worker_task = loop.create_task(_conversion_queue_worker())


def _compute_stats(jobs: List[ConversionJob], notes: List[TranscriptNote]) -> Dict[str, Any]:
    completed = [j for j in jobs if j.status == "completed"]
    active_queue = [j for j in jobs if j.status in ("queued", "downloading", "converting")]
    mp3_jobs = [j for j in completed if j.target_format == "mp3"]
    mp4_jobs = [j for j in completed if j.target_format == "mp4"]
    failed_jobs = [j for j in jobs if j.status == "failed"]
    total_bytes = sum(j.file_size_bytes for j in completed)

    return {
        "total_count": len(jobs),
        "completed_count": len(completed),
        "active_queue_count": len(active_queue),
        "mp3_count": len(mp3_jobs),
        "mp4_count": len(mp4_jobs),
        "failed_count": len(failed_jobs),
        "notes_count": len(notes),
        "total_size_human": format_file_size(total_bytes),
    }


@app.on_event("startup")
async def startup_event() -> None:
    init_db()
    settings.DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_worker_running()
    # Re-enqueue any jobs that were left in 'queued' state
    for pending in load_active_queue_jobs():
        if pending.status == "queued":
            conversion_queue.put_nowait((pending.job_id, False, ""))
    asyncio.create_task(bot3_poller.start())


@app.get("/api/protocol")
async def get_server_protocol() -> Dict[str, Any]:
    """기존 git/auction-radar와 분리된 서버 프로토콜 명세 조회"""
    return {
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "protocol": "HTTP/1.1 REST + Async Conversion Queue + File Stream + Telegram Bot 3 Long-Polling",
        "host": settings.HOST,
        "port": settings.PORT,
        "isolated_from": {
            "project": "git/auction-radar",
            "port": 8000,
            "telegram_bot": "TELEGRAM_BOT_2 (@gurucrabsss_bot)",
        },
        "engine": {
            "database": f"Docker MySQL 8.0 ({settings.MYSQL_HOST}:{settings.MYSQL_PORT}/{settings.MYSQL_DATABASE})",
            "extractor": "yt-dlp (open-source)",
            "transcript_extractor": "youtube-transcript-api + yt-dlp subtitle parser",
            "summarizer": "Gemini 2.5 Flash (structured Korean summary + timeline)",
            "js_runtime": "deno (local binary)",
            "transcoder": "FFmpeg 7.0.2-static (libmp3lame / h264+aac)",
            "audio_normalizer": "FFmpeg EBU R128 2-Pass loudnorm (-16.0 LUFS, TP -1.5 dBTP) + dynaudnorm",
            "telegram_bot": "TELEGRAM_BOT_3 (@soracrabsss_bot - 소라크랩bot)",
            "telegram_poller_running": bot3_poller.running,
        },
        "endpoints": {
            "GET /": "Tailwind CSS 다크모드 반응형 웹 대시보드 (실시간 변환 큐 & 스크립트 정리 노트 보관함 & MP3/MP4 플레이어)",
            "GET /api/info?url={youtube_url}": "유튜브 영상 제목·채널·길이·썸네일 메타데이터 조회",
            "POST /api/summarize": "유튜브 스크립트 추출 및 AI 핵심 요약·타임라인 정리 후 노트 보관함 저장",
            "GET /api/notes": "저장된 스크립트 정리 노트 보관함 목록 조회 (?q=키워드 검색 지원)",
            "GET /api/notes/{note_id}": "단일 스크립트 정리 노트 상세 조회",
            "GET /api/notes/{note_id}/markdown": "스크립트 정리 노트 마크다운(.md) 파일 다운로드",
            "DELETE /api/notes/{note_id}": "저장된 스크립트 정리 노트 삭제",
            "POST /api/telegram/send-note/{note_id}": "정리된 스크립트 노트를 TELEGRAM_BOT_3로 전송",
            "POST /api/convert/enqueue": "DB 중복 확인 후 미변환 영상만 비동기 변환 대기열(큐)에 등록 (기본 -16 LUFS 음량 균일화)",
            "GET /api/queue": "실시간 변환 대기열(queued/downloading/converting) 및 최근 작업 상태 조회",
            "POST /api/convert": "DB 중복 확인 후 MP3(320/192/128kbps) 또는 MP4(best/1080p/720p/480p/360p) 변환 및 음량 균일화",
            "GET /api/convert/direct?url={youtube_url}&format={mp3|mp4}&quality={...}": "브라우저/curl 원클릭 즉시 변환 & 파일 다운로드",
            "GET /api/jobs": "전체 변환 작업 내역 조회",
            "GET /api/jobs/{job_id}": "단일 변환 작업 상태 조회",
            "POST /api/jobs/{job_id}/normalize": "단일 MP3/MP4 파일 음량 균일화 (-16 LUFS 방송 표준 보정)",
            "POST /api/jobs/normalize-all": "보관된 전체 MP3/MP4 파일 일괄 음량 균일화 (-16 LUFS 맞춤)",
            "GET /api/jobs/{job_id}/download": "변환 완료된 MP3/MP4 파일 다운로드 및 스트리밍",
            "DELETE /api/jobs/{job_id}": "변환 작업 및 로컬 미디어 파일 삭제",
            "POST /api/telegram/menu": "TELEGRAM_BOT_3(@soracrabsss_bot)로 대화형 앱 버튼 & 노트 보관함 호출",
            "POST /api/telegram/send/{job_id}": "변환된 미디어 파일을 TELEGRAM_BOT_3(@soracrabsss_bot)로 직접 전송",
        },
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    init_db()
    all_jobs = load_all_jobs(limit=100)
    active_queue_jobs = load_active_queue_jobs()
    all_notes = load_all_transcript_notes(limit=100)
    stats = _compute_stats(all_jobs, all_notes)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "all_jobs": all_jobs,
            "active_queue_jobs": active_queue_jobs,
            "all_notes": all_notes,
            "stats": stats,
            "port": settings.PORT,
        },
    )


@app.get("/api/info", response_model=VideoMetadata)
async def get_video_info(
    url: str = Query(..., description="조회할 유튜브 링크 (youtube.com / youtu.be)"),
) -> VideoMetadata:
    if not is_valid_youtube_url(url):
        raise HTTPException(status_code=400, detail="유효한 유튜브(youtube.com 또는 youtu.be) 링크를 입력해주세요.")
    try:
        meta = await asyncio.to_thread(extract_video_metadata, url)
        return meta
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"영상 정보 추출 실패: {exc}") from exc


@app.post("/api/summarize", response_model=TranscriptNote)
async def summarize_youtube_video(payload: SummarizeRequest) -> TranscriptNote:
    """유튜브 링크에서 스크립트를 추출하고 요약·정리하여 노트 보관함에 저장"""
    if not is_valid_youtube_url(payload.url):
        raise HTTPException(status_code=400, detail="유효한 유튜브(youtube.com 또는 youtu.be) 링크를 입력해주세요.")
    try:
        note = await asyncio.to_thread(extract_and_summarize_video, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"스크립트 추출 및 정리 중 오류가 발생했습니다: {exc}") from exc

    if payload.send_telegram:
        await send_telegram_note_alert(note, send_md_file=False)

    return note


@app.get("/api/notes", response_model=List[TranscriptNote])
async def list_transcript_notes(
    q: Optional[str] = Query(default=None, description="제목 또는 내용 검색 키워드"),
) -> List[TranscriptNote]:
    return load_all_transcript_notes(query=q, limit=100)


@app.get("/api/notes/{note_id}", response_model=TranscriptNote)
async def get_transcript_note(note_id: str) -> TranscriptNote:
    note = load_transcript_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="해당 스크립트 정리 노트를 찾을 수 없습니다.")
    return note


@app.get("/api/notes/{note_id}/markdown")
async def download_note_markdown(note_id: str) -> PlainTextResponse:
    note = load_transcript_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="해당 스크립트 정리 노트를 찾을 수 없습니다.")
    md_content = format_note_as_markdown(note)
    safe_title = sanitize_filename(note.title)
    encoded_name = quote(f"{safe_title}_정리노트.md")
    return PlainTextResponse(
        content=md_content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
    )


@app.delete("/api/notes/{note_id}")
async def remove_transcript_note(note_id: str) -> Dict[str, Any]:
    deleted = delete_transcript_note(note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="삭제할 스크립트 정리 노트를 찾을 수 없습니다.")
    return {"ok": True, "deleted_note_id": note_id, "title": deleted.title}


@app.post("/api/telegram/send-note/{note_id}")
async def trigger_telegram_send_note(
    note_id: str,
    md_file: bool = Query(default=False, description="마크다운 문서 파일로 전송 여부"),
) -> Dict[str, Any]:
    note = load_transcript_note(note_id)
    if not note:
        raise HTTPException(status_code=404, detail="해당 스크립트 정리 노트를 찾을 수 없습니다.")
    result = await send_telegram_note_alert(note, send_md_file=md_file)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "텔레그램 전송 실패"))
    return result


@app.post("/api/convert/enqueue", response_model=ConversionJob)
async def enqueue_youtube_conversion(payload: ConvertRequest, request: Request) -> ConversionJob:
    """
    1) 먼저 DB(conversion_jobs)를 조회해 동일 영상·포맷이 이미 변환 완료되었거나 대기열에 있는지 확인합니다.
    2) 이미 완료된 파일이 있으면 중복 변환하지 않고 기존 작업(is_cached=True)을 즉시 반환합니다.
    3) 이미 큐/변환 중이면 해당 작업(already_in_queue=True)을 반환합니다.
    4) 신규 요청이면 status='queued'로 DB에 등록하고 비동기 변환 큐에 넣은 뒤 즉시 반환합니다.
    """
    if not is_valid_youtube_url(payload.url):
        raise HTTPException(status_code=400, detail="유효한 유튜브(youtube.com 또는 youtu.be) 링크를 입력해주세요.")

    _ensure_worker_running()
    try:
        job, is_new = await asyncio.to_thread(create_or_reuse_queued_job, payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"변환 큐 등록 실패: {exc}") from exc

    base_url = str(request.base_url).rstrip("/")
    if is_new:
        conversion_queue.put_nowait((job.job_id, payload.send_telegram, base_url))
    return job


@app.get("/api/queue")
async def get_conversion_queue_status() -> Dict[str, Any]:
    """실시간 변환 대기열(queued/downloading/converting) 및 최근 작업 목록 조회"""
    active = load_active_queue_jobs()
    recent = load_all_jobs(limit=20)
    return {
        "active_count": len(active),
        "active_jobs": active,
        "recent_jobs": recent,
    }


@app.post("/api/convert", response_model=ConversionJob)
async def convert_youtube_video(payload: ConvertRequest, request: Request) -> ConversionJob:
    if not is_valid_youtube_url(payload.url):
        raise HTTPException(status_code=400, detail="유효한 유튜브(youtube.com 또는 youtu.be) 링크를 입력해주세요.")
    try:
        job = await asyncio.to_thread(execute_conversion, payload)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"변환 중 오류가 발생했습니다: {exc}") from exc

    if payload.send_telegram and job.status == "completed" and not job.is_cached:
        base_url = str(request.base_url).rstrip("/")
        await send_telegram_conversion_alert(job, base_url=base_url)

    return job


@app.get("/api/convert/direct")
async def convert_and_download_direct(
    url: str = Query(..., description="유튜브 링크"),
    format: Literal["mp3", "mp4"] = Query(default="mp3", description="mp3 또는 mp4"),
    quality: str = Query(default="192", description="MP3: 320/192/128, MP4: best/1080p/720p/480p/360p"),
    normalize: bool = Query(default=True, description="EBU R128 (-16 LUFS) 음량 균일화 적용 여부"),
) -> FileResponse:
    """쿼리스트링 하나로 DB 중복 확인 후 변환된 MP3/MP4 파일을 응답으로 내려주는 엔드포인트"""
    req = ConvertRequest(
        url=url,
        target_format=format,
        quality=quality,
        normalize_audio=normalize,
        send_telegram=False,
    )
    if not is_valid_youtube_url(req.url):
        raise HTTPException(status_code=400, detail="유효한 유튜브 링크가 아닙니다.")
    try:
        job = await asyncio.to_thread(execute_conversion, req)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"직접 변환 실패: {exc}") from exc

    return _build_file_response(job, inline=False)


@app.get("/api/jobs", response_model=List[ConversionJob])
async def list_jobs(
    target_format: Optional[str] = Query(default=None, description="mp3 또는 mp4 필터"),
) -> List[ConversionJob]:
    return load_all_jobs(target_format=target_format, limit=100)


@app.post("/api/jobs/normalize-all")
async def normalize_all_jobs_endpoint(
    target_lufs: float = Query(default=-16.0, description="목표 통합 음량 (LUFS, 기본 -16.0)"),
    force_all: bool = Query(default=False, description="이미 보정된 파일도 재보정 여부"),
) -> Dict[str, Any]:
    """보관된 완료 상태의 MP3/MP4 미디어 파일들을 일괄 음량 균일화(-16 LUFS)"""
    try:
        updated = await asyncio.to_thread(
            normalize_all_completed_jobs,
            target_lufs,
            not force_all,
        )
        return {
            "ok": True,
            "target_lufs": target_lufs,
            "normalized_count": len(updated),
            "jobs": updated,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"일괄 음량 균일화 실패: {exc}") from exc


@app.post("/api/jobs/{job_id}/normalize", response_model=ConversionJob)
async def normalize_single_job_endpoint(
    job_id: str,
    target_lufs: float = Query(default=-16.0, description="목표 통합 음량 (LUFS, 기본 -16.0)"),
) -> ConversionJob:
    """특정 MP3/MP4 변환 작업의 오디오 음량을 방송 표준(-16 LUFS)으로 균일화"""
    try:
        job = await asyncio.to_thread(normalize_existing_job, job_id, target_lufs)
        return job
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"음량 균일화 실패: {exc}") from exc


@app.get("/api/jobs/{job_id}", response_model=ConversionJob)
async def get_job_status(job_id: str) -> ConversionJob:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="해당 변환 작업을 찾을 수 없습니다.")
    return job


def _build_file_response(job: ConversionJob, inline: bool = False) -> FileResponse:
    if not job.file_path or not Path(job.file_path).exists():
        raise HTTPException(status_code=404, detail="변환된 로컬 파일이 존재하지 않습니다.")

    media_type = "audio/mpeg" if job.target_format == "mp3" else "video/mp4"
    encoded_name = quote(job.file_name)
    disposition_type = "inline" if inline else "attachment"
    headers = {
        "Content-Disposition": f"{disposition_type}; filename*=UTF-8''{encoded_name}",
    }
    return FileResponse(
        path=job.file_path,
        media_type=media_type,
        headers=headers,
    )


@app.get("/api/jobs/{job_id}/download")
async def download_converted_file(
    job_id: str,
    inline: bool = Query(default=False, description="브라우저 내장 플레이어 재생 여부"),
) -> FileResponse:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="해당 변환 작업을 찾을 수 없습니다.")
    return _build_file_response(job, inline=inline)


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> Dict[str, Any]:
    deleted = delete_job_record(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="삭제할 변환 작업을 찾을 수 없습니다.")
    return {"ok": True, "deleted_job_id": job_id, "file_name": deleted.file_name}


@app.post("/api/telegram/menu")
async def trigger_telegram_bot3_menu(request: Request) -> Dict[str, Any]:
    """TELEGRAM_BOT_3 (@soracrabsss_bot) 채팅방으로 대화형 컨트롤러 메뉴 및 하단 고정 키보드 전송"""
    base_url = str(request.base_url).rstrip("/")
    result = await send_telegram_dashboard_menu(base_url=base_url)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "텔레그램 메뉴 전송 실패"))
    return result


@app.post("/api/telegram/send/{job_id}")
async def trigger_telegram_send(job_id: str, request: Request) -> Dict[str, Any]:
    job = load_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="해당 변환 작업을 찾을 수 없습니다.")
    base_url = str(request.base_url).rstrip("/")
    result = await send_telegram_conversion_alert(job, base_url=base_url)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error", "텔레그램 전송 실패"))
    return result
