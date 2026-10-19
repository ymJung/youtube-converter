import json
import math
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import yt_dlp

from app.config import settings
from app.database import (
    find_existing_job,
    load_active_queue_jobs,
    load_all_jobs,
    load_job,
    save_job,
)
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

LOUDNORM_JSON_RE = re.compile(r"\{\s*\"input_i\"\s*:.*?\}", re.DOTALL)


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


def _parse_loudnorm_json(stderr_text: str) -> Dict[str, float]:
    matches = LOUDNORM_JSON_RE.findall(stderr_text)
    if not matches:
        return {}
    try:
        raw = json.loads(matches[-1])
        parsed: Dict[str, float] = {}
        for k, v in raw.items():
            try:
                val = float(v)
                if math.isfinite(val):
                    parsed[k] = val
            except (ValueError, TypeError):
                continue
        return parsed
    except Exception:
        return {}


def measure_audio_loudness(file_path: Path, target_lufs: float = -16.0) -> Dict[str, float]:
    """FFmpeg EBU R128 loudnorm 1차 패스로 원본 미디어의 통합 음량(LUFS) 및 피크(dBTP) 측정"""
    cmd = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-vn",
        "-i",
        str(file_path),
        "-af",
        f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return _parse_loudnorm_json(proc.stderr)


def normalize_media_audio(
    file_path: Path,
    target_format: str,
    quality: str,
    target_lufs: float = -16.0,
) -> Dict[str, Optional[float]]:
    """
    소리가 너무 작거나 큰 MP3/MP4 파일의 전체 음량을 방송·스트리밍 표준(기본 -16.0 LUFS, TP -1.5 dBTP)으로 균일화.
    - 1차 패스: 원본 통합 음량(input_i)과 피크(input_tp), 음량 범위(input_lra)를 정밀 측정
    - 2차 패스:
      * 원본이 너무 작은 경우(input_i < -20 LUFS): dynaudnorm + loudnorm 결합으로 작은 소리 증폭 및 피크 클리핑 방지
      * 일반/큰 음량 영상: 측정된 파라미터 기반 2-Pass EBU R128 loudnorm 적용
      * MP4는 비디오 스트림 무손실 복사(-c:v copy)로 화질 저하 없이 오디오만 고속 보정
    """
    if not file_path.exists():
        raise FileNotFoundError(f"음량 보정 대상 파일이 없습니다: {file_path}")

    pass1 = measure_audio_loudness(file_path, target_lufs=target_lufs)
    orig_i = pass1.get("input_i")
    if orig_i is None:
        return {"original_lufs": None, "normalized_lufs": None, "gain_db": None}

    sample_rate = 44100 if target_format == "mp3" else 48000

    if orig_i < -20.0:
        # 소리가 매우 작은 영상: dynaudnorm으로 미세 음성을 끌어올린 뒤 loudnorm으로 -16 LUFS 타겟 고정
        af_chain = (
            f"dynaudnorm=f=150:g=15,"
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json,"
            f"aresample={sample_rate}"
        )
    elif all(k in pass1 for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        af_chain = (
            f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:"
            f"measured_I={pass1['input_i']:.2f}:"
            f"measured_TP={pass1['input_tp']:.2f}:"
            f"measured_LRA={pass1['input_lra']:.2f}:"
            f"measured_thresh={pass1['input_thresh']:.2f}:"
            f"offset={pass1['target_offset']:.2f}:"
            f"print_format=json,"
            f"aresample={sample_rate}"
        )
    else:
        af_chain = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11:print_format=json,aresample={sample_rate}"

    tmp_path = file_path.with_name(f"{file_path.stem}_norm_tmp{file_path.suffix}")
    try:
        if target_format == "mp3":
            bitrate = quality if quality in ("320", "192", "128") else "192"
            cmd = [
                settings.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(file_path),
                "-af",
                af_chain,
                "-c:a",
                "libmp3lame",
                "-b:a",
                f"{bitrate}k",
                str(tmp_path),
            ]
        else:
            cmd = [
                settings.ffmpeg_path,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(file_path),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-af",
                af_chain,
                "-movflags",
                "+faststart",
                str(tmp_path),
            ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg 음량 균일화 실패: {proc.stderr[-300:]}")

        pass2 = _parse_loudnorm_json(proc.stderr)
        norm_i = pass2.get("output_i", target_lufs)
        tmp_path.replace(file_path)

        orig_rounded = round(orig_i, 1)
        norm_rounded = round(norm_i, 1)
        gain_rounded = round(norm_rounded - orig_rounded, 1)
        return {
            "original_lufs": orig_rounded,
            "normalized_lufs": norm_rounded,
            "gain_db": gain_rounded,
        }
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


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
    2) If found and not force_reconvert:
       - If normalize_audio is requested but the cached file was not normalized yet, normalize it in-place.
       - Return (existing_job, False) without duplicate download.
    3) Otherwise extract metadata, register a 'queued' job in DB, and return (new_job, True).
    """
    norm_quality = normalize_quality(req.target_format, req.quality)

    if not req.force_reconvert:
        fast_vid = extract_video_id_fast(req.url)
        if fast_vid:
            existing = find_existing_job(fast_vid, req.target_format, norm_quality)
            if existing:
                if (
                    req.normalize_audio
                    and existing.status == "completed"
                    and not existing.audio_normalized
                    and existing.file_path
                    and Path(existing.file_path).exists()
                ):
                    existing = normalize_existing_job(existing.job_id, target_lufs=req.target_lufs)
                    existing.is_cached = True
                return existing, False

    meta = extract_video_metadata(req.url)
    if not req.force_reconvert:
        existing = find_existing_job(meta.video_id, req.target_format, norm_quality)
        if existing:
            if (
                req.normalize_audio
                and existing.status == "completed"
                and not existing.audio_normalized
                and existing.file_path
                and Path(existing.file_path).exists()
            ):
                existing = normalize_existing_job(existing.job_id, target_lufs=req.target_lufs)
                existing.is_cached = True
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
        normalize_audio=req.normalize_audio,
        audio_normalized=False,
        target_lufs=req.target_lufs,
        created_at=now_iso,
    )
    save_job(job)
    return job, True


def process_queued_job(job_id: str) -> ConversionJob:
    """Execute the actual download + FFmpeg conversion + EBU R128 loudness normalization for a queued job_id."""
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
                pct = min(80.0, round((downloaded / total) * 80.0, 1))
                if pct > job.progress_percent:
                    job.progress_percent = pct
                if pct - last_saved_pct >= 5.0:
                    last_saved_pct = pct
                    save_job(job)
        elif status == "finished":
            job.status = "converting"
            job.progress_percent = 85.0
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

        if job.normalize_audio:
            job.status = "converting"
            job.progress_percent = 92.0
            save_job(job)
            loud_stats = normalize_media_audio(
                file_path=expected_file,
                target_format=job.target_format,
                quality=job.quality,
                target_lufs=job.target_lufs,
            )
            if loud_stats.get("normalized_lufs") is not None:
                job.audio_normalized = True
                job.original_lufs = loud_stats["original_lufs"]
                job.normalized_lufs = loud_stats["normalized_lufs"]
                job.gain_db = loud_stats["gain_db"]

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


def normalize_existing_job(job_id: str, target_lufs: float = -16.0) -> ConversionJob:
    """이미 변환 완료된 MP3/MP4 작업 파일에 대해 음량 균일화(-16 LUFS)를 수행하고 DB를 갱신"""
    job = load_job(job_id)
    if not job:
        raise ValueError(f"해당 변환 작업({job_id})을 찾을 수 없습니다.")
    if job.status != "completed" or not job.file_path:
        raise ValueError(f"완료된 변환 작업만 음량 보정이 가능합니다 (현재 상태: {job.status}).")

    fp = Path(job.file_path)
    if not fp.exists():
        raise FileNotFoundError(f"로컬 미디어 파일이 존재하지 않습니다: {fp}")

    prev_orig = job.original_lufs
    loud_stats = normalize_media_audio(
        file_path=fp,
        target_format=job.target_format,
        quality=job.quality,
        target_lufs=target_lufs,
    )
    size_bytes = fp.stat().st_size
    job.normalize_audio = True
    job.audio_normalized = True
    job.target_lufs = target_lufs
    # 이미 이전에 보정된 적이 있다면 최초 원본 LUFS를 보존
    job.original_lufs = prev_orig if prev_orig is not None else loud_stats.get("original_lufs")
    job.normalized_lufs = loud_stats.get("normalized_lufs")
    if job.original_lufs is not None and job.normalized_lufs is not None:
        job.gain_db = round(job.normalized_lufs - job.original_lufs, 1)
    else:
        job.gain_db = loud_stats.get("gain_db")
    job.file_size_bytes = size_bytes
    job.file_size_human = format_file_size(size_bytes)
    save_job(job)
    return job


def normalize_all_completed_jobs(
    target_lufs: float = -16.0,
    only_unnormalized: bool = True,
) -> List[ConversionJob]:
    """보관된 완료 상태의 MP3/MP4 파일들을 일괄 음량 균일화(-16 LUFS)"""
    jobs = load_all_jobs(limit=200)
    updated: List[ConversionJob] = []
    for job in jobs:
        if job.status != "completed" or not job.file_path:
            continue
        if only_unnormalized and job.audio_normalized:
            continue
        fp = Path(job.file_path)
        if not fp.exists():
            continue
        try:
            norm_job = normalize_existing_job(job.job_id, target_lufs=target_lufs)
            updated.append(norm_job)
        except Exception:
            continue
    return updated


def execute_conversion(req: ConvertRequest) -> ConversionJob:
    """DB-first deduplicated conversion pipeline (checks DB first, converts only if not already converted)."""
    job, is_new = create_or_reuse_queued_job(req)
    if not is_new and job.status == "completed":
        return job
    return process_queued_job(job.job_id)
