from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, List, Optional
import pymysql
from pymysql.cursors import DictCursor

from app.config import settings
from app.models import ConversionJob, TranscriptNote


@contextmanager
def get_connection() -> Generator[pymysql.connections.Connection, None, None]:
    conn = pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
    )
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS conversion_jobs (
                    job_id VARCHAR(64) PRIMARY KEY,
                    video_id VARCHAR(64) NOT NULL,
                    target_format VARCHAR(16) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at VARCHAR(64) NOT NULL,
                    payload_json LONGTEXT NOT NULL,
                    INDEX idx_jobs_format_created (target_format, created_at),
                    INDEX idx_jobs_video_format (video_id, target_format)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS transcript_notes (
                    note_id VARCHAR(64) PRIMARY KEY,
                    video_id VARCHAR(64) NOT NULL,
                    title VARCHAR(512) NOT NULL,
                    created_at VARCHAR(64) NOT NULL,
                    payload_json LONGTEXT NOT NULL,
                    INDEX idx_notes_video_created (video_id, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
        conn.commit()


def save_job(job: ConversionJob) -> None:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversion_jobs (job_id, video_id, target_format, status, created_at, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    video_id = VALUES(video_id),
                    target_format = VALUES(target_format),
                    status = VALUES(status),
                    created_at = VALUES(created_at),
                    payload_json = VALUES(payload_json)
                """,
                (
                    job.job_id,
                    job.video_id,
                    job.target_format,
                    job.status,
                    job.created_at,
                    job.model_dump_json(),
                ),
            )
        conn.commit()


def load_job(job_id: str) -> Optional[ConversionJob]:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM conversion_jobs WHERE job_id = %s",
                (job_id,),
            )
            row: Optional[dict[str, Any]] = cur.fetchone()
            if not row:
                return None
            return ConversionJob.model_validate_json(row["payload_json"])


def find_existing_job(
    video_id: str,
    target_format: str,
    quality: Optional[str] = None,
) -> Optional[ConversionJob]:
    """Check DB first to prevent duplicate conversions for the same video_id + target_format (+ quality)."""
    if not video_id or video_id == "unknown":
        return None
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json
                FROM conversion_jobs
                WHERE video_id = %s AND target_format = %s
                ORDER BY created_at DESC
                LIMIT 20
                """,
                (video_id, target_format),
            )
            rows = cur.fetchall()

    jobs = [ConversionJob.model_validate_json(r["payload_json"]) for r in rows]

    # 1. Check if already in queue or actively converting
    for job in jobs:
        if job.status in ("queued", "downloading", "converting"):
            if quality is None or job.quality == quality:
                job.already_in_queue = True
                return job

    # 2. Check if already completed and media file exists on disk
    for job in jobs:
        if job.status == "completed" and job.file_path and Path(job.file_path).exists():
            if quality is None or job.quality == quality:
                job.is_cached = True
                return job

    # 3. Fallback: if any completed job for the same video_id & target_format exists on disk
    for job in jobs:
        if job.status == "completed" and job.file_path and Path(job.file_path).exists():
            job.is_cached = True
            return job

    return None


def load_active_queue_jobs() -> List[ConversionJob]:
    """Return all currently queued, downloading, or converting jobs in FIFO order with queue_position."""
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload_json
                FROM conversion_jobs
                WHERE status IN ('queued', 'downloading', 'converting')
                ORDER BY created_at ASC
                """
            )
            rows = cur.fetchall()

    active_jobs = [ConversionJob.model_validate_json(r["payload_json"]) for r in rows]
    q_pos = 1
    for job in active_jobs:
        if job.status == "queued":
            job.queue_position = q_pos
            q_pos += 1
        else:
            job.queue_position = 0
    return active_jobs


def load_all_jobs(target_format: Optional[str] = None, limit: int = 100) -> List[ConversionJob]:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if target_format in ("mp3", "mp4"):
                cur.execute(
                    "SELECT payload_json FROM conversion_jobs WHERE target_format = %s ORDER BY created_at DESC LIMIT %s",
                    (target_format, int(limit)),
                )
            else:
                cur.execute(
                    "SELECT payload_json FROM conversion_jobs ORDER BY created_at DESC LIMIT %s",
                    (int(limit),),
                )
            rows = cur.fetchall()

    jobs = [ConversionJob.model_validate_json(r["payload_json"]) for r in rows]
    # Populate queue_position for any queued jobs
    queued_in_asc = [j for j in reversed(jobs) if j.status == "queued"]
    pos_map = {j.job_id: idx + 1 for idx, j in enumerate(queued_in_asc)}
    for j in jobs:
        if j.status == "queued":
            j.queue_position = pos_map.get(j.job_id, 1)
    return jobs


def delete_job_record(job_id: str) -> Optional[ConversionJob]:
    init_db()
    job = load_job(job_id)
    if not job:
        return None
    if job.file_path:
        fp = Path(job.file_path)
        if fp.exists():
            try:
                fp.unlink()
            except Exception:
                pass
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversion_jobs WHERE job_id = %s", (job_id,))
        conn.commit()
    return job


def save_transcript_note(note: TranscriptNote) -> None:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transcript_notes (note_id, video_id, title, created_at, payload_json)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    video_id = VALUES(video_id),
                    title = VALUES(title),
                    created_at = VALUES(created_at),
                    payload_json = VALUES(payload_json)
                """,
                (
                    note.note_id,
                    note.video_id,
                    note.title[:500],
                    note.created_at,
                    note.model_dump_json(),
                ),
            )
        conn.commit()


def load_transcript_note(note_id: str) -> Optional[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM transcript_notes WHERE note_id = %s",
                (note_id,),
            )
            row: Optional[dict[str, Any]] = cur.fetchone()
            if not row:
                return None
            return TranscriptNote.model_validate_json(row["payload_json"])


def load_transcript_note_by_video_id(video_id: str) -> Optional[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT payload_json FROM transcript_notes WHERE video_id = %s ORDER BY created_at DESC LIMIT 1",
                (video_id,),
            )
            row: Optional[dict[str, Any]] = cur.fetchone()
            if not row:
                return None
            return TranscriptNote.model_validate_json(row["payload_json"])


def load_all_transcript_notes(query: Optional[str] = None, limit: int = 100) -> List[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        with conn.cursor() as cur:
            if query and query.strip():
                q = f"%{query.strip()}%"
                cur.execute(
                    "SELECT payload_json FROM transcript_notes WHERE title LIKE %s OR payload_json LIKE %s ORDER BY created_at DESC LIMIT %s",
                    (q, q, int(limit)),
                )
            else:
                cur.execute(
                    "SELECT payload_json FROM transcript_notes ORDER BY created_at DESC LIMIT %s",
                    (int(limit),),
                )
            rows = cur.fetchall()
    return [TranscriptNote.model_validate_json(r["payload_json"]) for r in rows]


def delete_transcript_note(note_id: str) -> Optional[TranscriptNote]:
    init_db()
    note = load_transcript_note(note_id)
    if not note:
        return None
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transcript_notes WHERE note_id = %s", (note_id,))
        conn.commit()
    return note
