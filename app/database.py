import sqlite3
from pathlib import Path
from typing import List, Optional
from app.config import settings
from app.models import ConversionJob, TranscriptNote


def get_connection() -> sqlite3.Connection:
    settings.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversion_jobs (
                job_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                target_format TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transcript_notes (
                note_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def save_job(job: ConversionJob) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO conversion_jobs (job_id, video_id, target_format, status, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
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
        row = conn.execute(
            "SELECT payload_json FROM conversion_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not row:
            return None
        return ConversionJob.model_validate_json(row["payload_json"])


def load_all_jobs(target_format: Optional[str] = None, limit: int = 100) -> List[ConversionJob]:
    init_db()
    with get_connection() as conn:
        if target_format in ("mp3", "mp4"):
            rows = conn.execute(
                "SELECT payload_json FROM conversion_jobs WHERE target_format = ? ORDER BY created_at DESC LIMIT ?",
                (target_format, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT payload_json FROM conversion_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [ConversionJob.model_validate_json(r["payload_json"]) for r in rows]


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
        conn.execute("DELETE FROM conversion_jobs WHERE job_id = ?", (job_id,))
        conn.commit()
    return job


def save_transcript_note(note: TranscriptNote) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO transcript_notes (note_id, video_id, title, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                note.note_id,
                note.video_id,
                note.title,
                note.created_at,
                note.model_dump_json(),
            ),
        )
        conn.commit()


def load_transcript_note(note_id: str) -> Optional[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM transcript_notes WHERE note_id = ?", (note_id,)
        ).fetchone()
        if not row:
            return None
        return TranscriptNote.model_validate_json(row["payload_json"])


def load_transcript_note_by_video_id(video_id: str) -> Optional[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM transcript_notes WHERE video_id = ? ORDER BY created_at DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        if not row:
            return None
        return TranscriptNote.model_validate_json(row["payload_json"])


def load_all_transcript_notes(query: Optional[str] = None, limit: int = 100) -> List[TranscriptNote]:
    init_db()
    with get_connection() as conn:
        if query and query.strip():
            q = f"%{query.strip()}%"
            rows = conn.execute(
                "SELECT payload_json FROM transcript_notes WHERE title LIKE ? OR payload_json LIKE ? ORDER BY created_at DESC LIMIT ?",
                (q, q, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT payload_json FROM transcript_notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [TranscriptNote.model_validate_json(r["payload_json"]) for r in rows]


def delete_transcript_note(note_id: str) -> Optional[TranscriptNote]:
    init_db()
    note = load_transcript_note(note_id)
    if not note:
        return None
    with get_connection() as conn:
        conn.execute("DELETE FROM transcript_notes WHERE note_id = ?", (note_id,))
        conn.commit()
    return note
