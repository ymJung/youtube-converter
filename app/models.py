from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class VideoFormatOption(BaseModel):
    format_id: str
    ext: str
    resolution: str
    filesize_approx: Optional[int] = None
    note: str = ""


class VideoMetadata(BaseModel):
    video_id: str
    url: str
    title: str
    uploader: str = "Unknown"
    duration_sec: int = 0
    duration_str: str = "00:00"
    view_count: int = 0
    upload_date: str = ""
    thumbnail_url: str = ""
    description_snippet: str = ""
    available_mp3_qualities: List[str] = Field(default_factory=lambda: ["320", "192", "128"])
    available_mp4_qualities: List[str] = Field(default_factory=lambda: ["best", "1080p", "720p", "480p", "360p"])


class ConvertRequest(BaseModel):
    url: str = Field(..., description="유튜브 동영상 또는 쇼츠/뮤직 링크")
    target_format: Literal["mp3", "mp4"] = Field(default="mp3", description="변환 포맷: mp3 또는 mp4")
    quality: str = Field(
        default="192",
        description="MP3 비트레이트(320/192/128) 또는 MP4 해상도(best/1080p/720p/480p/360p)",
    )
    normalize_audio: bool = Field(
        default=True,
        description="소리가 너무 작거나 큰 영상을 방송/스트리밍 표준(-16 LUFS)으로 전체 음량 균일화 여부",
    )
    target_lufs: float = Field(
        default=-16.0,
        description="목표 통합 음량(LUFS, 기본값 -16.0 LUFS)",
    )
    send_telegram: bool = Field(default=False, description="변환 완료 후 텔레그램 알림/전송 여부")
    force_reconvert: bool = Field(default=False, description="DB에 이미 변환된 항목이 있어도 강제 재변환 여부")


class ConversionJob(BaseModel):
    job_id: str
    video_id: str
    url: str
    title: str
    uploader: str
    duration_sec: int
    duration_str: str
    thumbnail_url: str
    target_format: Literal["mp3", "mp4"]
    quality: str
    status: Literal["queued", "downloading", "converting", "completed", "failed"] = "queued"
    progress_percent: float = 0.0
    queue_position: int = 0
    is_cached: bool = False
    already_in_queue: bool = False
    normalize_audio: bool = True
    audio_normalized: bool = False
    target_lufs: float = -16.0
    original_lufs: Optional[float] = None
    normalized_lufs: Optional[float] = None
    gain_db: Optional[float] = None
    file_name: str = ""
    file_path: str = ""
    file_size_bytes: int = 0
    file_size_human: str = "0 MB"
    download_url: str = ""
    error_message: str = ""
    created_at: str = ""
    completed_at: str = ""


class TranscriptSegment(BaseModel):
    start_sec: float
    timestamp_str: str
    text: str


class SummarizeRequest(BaseModel):
    url: str = Field(..., description="유튜브 동영상 링크")
    custom_focus: str = Field(default="", description="특정 관점으로 정리하고 싶을 때 추가 지시사항 (선택)")
    send_telegram: bool = Field(default=False, description="정리 완료 후 텔레그램 전송 여부")


class TranscriptNote(BaseModel):
    note_id: str
    video_id: str
    url: str
    title: str
    uploader: str
    duration_sec: int
    duration_str: str
    thumbnail_url: str
    language_used: str = "ko"
    extraction_engine: str = "youtube-transcript-api"
    summarizer_engine: str = "gemini-2.5-flash"
    one_line_summary: str = ""
    key_takeaways: List[str] = Field(default_factory=list)
    timeline_highlights: List[str] = Field(default_factory=list)
    detailed_summary_md: str = ""
    full_transcript_text: str = ""
    timestamped_transcript: str = ""
    segment_count: int = 0
    created_at: str = ""
