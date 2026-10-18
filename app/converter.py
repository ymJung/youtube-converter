import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple
import yt_dlp

from app.config import settings
from app.database import find_existing_job, load_active_queue_jobs, load_job, save_job
from app.models import ConversionJob, ConvertRequest, VideoMetadata

YOUTUBE_URL_PATTERN = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/.+$",
    re.IGNORECASE,
)

VIDEO_ID_PATTERNS = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"youtube\.com/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})"),
]


def is_valid_youtube_url(url: str) -> bool:
    return bool(YOUTUBE_URL_PATTERN.match(url.strip()))


def extract_video_id_fast(url: str) -> Optional[str]:
    clean = url.strip()
    for pat in VIDEO_ID_PATTERNS:
        m = pat.search(clean)
        if m:
            return m.group(1)
    return None


def normalize_quality(target_format: str, quality: str) -> str:
    if target_format == "mp3":
        return quality if quality in ("320", "192", "128") else "192"
    return quality if quality in ("best", "1080p", "720p", "480p", "360p") else "best"


def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "00:00"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_file_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def sanitize_filename(title: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:80] if cleaned else "youtube_media"


def extract_video_metadata(url: str) -> VideoMetadata:
    clean_url = url.strip()
    if not is_valid_youtube_url(clean_url):
        raise ValueError("유효한 유튜브(youtube.com / youtu.be) 링크가 아닙니다.")

    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ffmpeg_location": settings.ffmpeg_path,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(clean_url, download=False)
        if not info:
            raise RuntimeError("유튜브 영상 메타데이터를 가져올 수 없습니다.")

        if "entries" in info and info["entries"]:
            info = info["entries"][0]

        duration_sec = int(info.get("duration") or 0)
        desc = (info.get("description") or "").strip()
        if len(desc) > 200:
            desc = desc[:200] + "..."

        return VideoMetadata(
            video_id=str(info.get("id") or "unknown"),
            url=info.get("webpage_url") or clean_url,
            title=str(info.get("title") or "Untitled Video"),
            uploader=str(info.get("uploader") or info.get("channel") or "Unknown Channel"),
            duration_sec=duration_sec,
            duration_str=format_duration(duration_sec),
            view_count=int(info.get("view_count") or 0),
            upload_date=str(info.get("upload_date") or ""),
            thumbnail_url=str(info.get("thumbnail") or ""),
            description_snippet=desc,
        )


def build_yt_dlp_options(
    target_format: str,
    quality: str,
    output_template: str,
    progress_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": settings.ffmpeg_path,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    if target_format == "mp3":
        bitrate = quality if quality in ("320", "192", "128") else "192"
        opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": bitrate,
                    }
                ],
            }
        )
    else:
        height_map = {
            "1080p": 1080,
            "720p": 720,
            "480p": 480,
            "360p": 360,
        }
        max_h = height_map.get(quality)
        if max_h:
            fmt = (
                f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={max_h}]+bestaudio/"
                f"best[height<={max_h}][ext=mp4]/best[height<={max_h}]/best"
            )
        else:
            fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"

        opts.update(
            {
                "format": fmt,
                "merge_output_format": "mp4",
            }
        )

    return opts


def create_or_reuse_queued_job(req: ConvertRequest) -> Tuple[ConversionJob, bool]:
    """
    1) Check DB first for an existing completed or active job for (video_id, target_format, quality).
    2) If found and not force_reconvert, return (existing_job, False) without duplicate conversion.
    3) Otherwise extract metadata, register a 'queued' job in DB, and return (new_job, True).
    """
    norm_quality = normalize_quality(req.target_format, req.quality)

    if not req.force_reconvert:
        fast_vid = extract_video_id_fast(req.url)
        if fast_vid:
            existing = find_existing_job(fast_vid, req.target_format, norm_quality)
            if existing:
                return existing, False

    meta = extract_video_metadata(req.url)
    if not req.force_reconvert:
        existing = find_existing_job(meta.video_id, req.target_format, norm_quality)
        if existing:
            return existing, False

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    job_id = uuid.uuid4().hex[:10]
    active_queued = [j for j in load_active_queue_jobs() if j.status == "queued"]

    job = ConversionJob(
        job_id=job_id,
        video_id=meta.video_id,
        url=meta.url,
        title=meta.title,
        uploader=meta.uploader,
        duration_sec=meta.duration_sec,
        duration_str=meta.duration_str,
        thumbnail_url=meta.thumbnail_url,
        target_format=req.target_format,
        quality=norm_quality,
        status="queued",
        progress_percent=0.0,
        queue_position=len(active_queued) + 1,
        is_cached=False,
        already_in_queue=False,
        created_at=now_iso,
    )
    save_job(job)
    return job, True


def process_queued_job(job_id: str) -> ConversionJob:
    """Execute the actual download + FFmpeg conversion for a queued job_id and update DB progress."""
    job = load_job(job_id)
    if not job:
        raise ValueError(f"변환 작업({job_id})을 DB에서 찾을 수 없습니다.")

    if job.status == "completed" and job.file_path:
        return job

    job.status = "downloading"
    job.progress_percent = 10.0
    job.queue_position = 0
    save_job(job)

    safe_title = sanitize_filename(job.title)
    base_stem = f"{safe_title}_{job.video_id}_{job.job_id}"
    output_template = str(settings.DOWNLOADS_DIR / f"{base_stem}.%(ext)s")
    last_saved_pct = 10.0

    def _hook(d: Dict[str, Any]) -> None:
        nonlocal last_saved_pct
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            if total > 0:
                pct = min(85.0, round((downloaded / total) * 85.0, 1))
                if pct > job.progress_percent:
                    job.progress_percent = pct
                if pct - last_saved_pct >= 5.0:
                    last_saved_pct = pct
                    save_job(job)
        elif status == "finished":
            job.status = "converting"
            job.progress_percent = 90.0
            save_job(job)

    opts = build_yt_dlp_options(
        target_format=job.target_format,
        quality=job.quality,
        output_template=output_template,
        progress_hook=_hook,
    )

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([job.url.strip()])

        expected_file = settings.DOWNLOADS_DIR / f"{base_stem}.{job.target_format}"
        if not expected_file.exists():
            candidates = list(settings.DOWNLOADS_DIR.glob(f"{base_stem}.*"))
            if not candidates:
                raise FileNotFoundError("변환된 결과 파일이 생성되지 않았습니다.")
            expected_file = candidates[0]

        size_bytes = expected_file.stat().st_size
        job.status = "completed"
        job.progress_percent = 100.0
        job.queue_position = 0
        job.file_name = f"{safe_title}.{job.target_format}"
        job.file_path = str(expected_file)
        job.file_size_bytes = size_bytes
        job.file_size_human = format_file_size(size_bytes)
        job.download_url = f"/api/jobs/{job.job_id}/download"
        job.completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        return job

    except Exception as exc:
        job.status = "failed"
        job.queue_position = 0
        job.error_message = str(exc)
        job.completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        save_job(job)
        raise


def execute_conversion(req: ConvertRequest) -> ConversionJob:
    """DB-first deduplicated conversion pipeline (checks DB first, converts only if not already converted)."""
    job, is_new = create_or_reuse_queued_job(req)
    if not is_new and job.status == "completed":
        return job
    return process_queued_job(job.job_id)
