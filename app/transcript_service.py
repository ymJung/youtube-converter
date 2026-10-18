import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yt_dlp
from dotenv import load_dotenv
from google import genai
from youtube_transcript_api import YouTubeTranscriptApi

from app.config import HOME_ENV_PATH, settings
from app.converter import extract_video_metadata, format_duration
from app.database import save_transcript_note
from app.models import SummarizeRequest, TranscriptNote, TranscriptSegment, VideoMetadata


def _format_sec_timestamp(sec: float) -> str:
    return format_duration(int(sec))


def fetch_transcript_segments(
    video_id: str, url: str
) -> Tuple[List[TranscriptSegment], str, str]:
    """
    1차: 오픈소스 youtube-transcript-api 로 자막/스크립트 추출 (한국어 -> 영어 -> 자동생성 순)
    2차 폴백: yt-dlp 내장 자막 추출기(.vtt/.json3) 및 설명란 폴백
    Returns: (segments, language_code, engine_name)
    """
    ytt = YouTubeTranscriptApi()
    preferred_langs = ["ko", "en", "ja", "zh-Hans", "es"]

    # 1. youtube-transcript-api 시도
    try:
        transcript_list = ytt.list(video_id)
        chosen_transcript = None
        for lang in preferred_langs:
            try:
                chosen_transcript = transcript_list.find_transcript([lang])
                break
            except Exception:
                continue

        if chosen_transcript is None:
            for tr in transcript_list:
                chosen_transcript = tr
                break

        if chosen_transcript is not None:
            fetched = chosen_transcript.fetch()
            segments: List[TranscriptSegment] = []
            for item in fetched:
                cleaned = re.sub(r"\s+", " ", str(item.text)).strip()
                if cleaned:
                    segments.append(
                        TranscriptSegment(
                            start_sec=round(float(item.start), 1),
                            timestamp_str=_format_sec_timestamp(float(item.start)),
                            text=cleaned,
                        )
                    )
            if segments:
                return (
                    segments,
                    str(chosen_transcript.language_code),
                    "youtube-transcript-api",
                )
    except Exception:
        pass

    # 2. yt-dlp 자막 추출 폴백 (자동 자막 포함)
    with tempfile.TemporaryDirectory() as tmpdir:
        opts: Dict[str, Any] = {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["ko", "en"],
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "ffmpeg_location": settings.ffmpeg_path,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                vtt_files = list(Path(tmpdir).glob("*.vtt"))
                if vtt_files:
                    vtt_path = vtt_files[0]
                    lang_used = "ko" if ".ko." in vtt_path.name else "en"
                    segs = _parse_vtt_file(vtt_path)
                    if segs:
                        return segs, lang_used, "yt-dlp-subtitles"

                # 자막이 아예 없는 영상인 경우 영상 제목 + 설명란(description)으로 구성
                desc = (info or {}).get("description") or ""
                title = (info or {}).get("title") or ""
                fallback_text = f"{title}. {desc}".strip()
                if fallback_text:
                    return (
                        [
                            TranscriptSegment(
                                start_sec=0.0,
                                timestamp_str="00:00",
                                text=fallback_text[:3000],
                            )
                        ],
                        "meta-description",
                        "yt-dlp-metadata-fallback",
                    )
        except Exception:
            pass

    raise RuntimeError("해당 유튜브 영상에서 추출 가능한 자막(스크립트)을 찾을 수 없습니다.")


def _parse_vtt_file(vtt_path: Path) -> List[TranscriptSegment]:
    content = vtt_path.read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines()
    segments: List[TranscriptSegment] = []
    time_re = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.\d+\s+-->")
    seen_texts: List[str] = []

    curr_sec = 0.0
    for line in lines:
        line_str = line.strip()
        if not line_str or line_str.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        m = time_re.match(line_str)
        if m:
            h, mins, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            curr_sec = float(h * 3600 + mins * 60 + s)
            continue
        cleaned = re.sub(r"<[^>]+>", "", line_str).strip()
        if cleaned and (not seen_texts or seen_texts[-1] != cleaned):
            seen_texts.append(cleaned)
            segments.append(
                TranscriptSegment(
                    start_sec=curr_sec,
                    timestamp_str=_format_sec_timestamp(curr_sec),
                    text=cleaned,
                )
            )
    return segments


def _summarize_with_gemini_or_fallback(
    meta: VideoMetadata,
    segments: List[TranscriptSegment],
    custom_focus: str = "",
) -> Tuple[str, List[str], List[str], str, str]:
    """
    Gemini 2.5 Flash 오픈소스 요약 프롬프트를 사용해 한국어로 깔끔하게 구조화 정리.
    Returns: (one_line_summary, key_takeaways, timeline_highlights, detailed_summary_md, engine_used)
    """
    if HOME_ENV_PATH.exists():
        load_dotenv(HOME_ENV_PATH, override=True)
    api_key = os.getenv("GEMINI_API_KEY", "").strip()

    timestamped_lines = [f"[{s.timestamp_str}] {s.text}" for s in segments]
    joined_script = "\n".join(timestamped_lines)
    # 컨텍스트 길이 안전 보정
    script_for_prompt = joined_script[:45000]

    if api_key:
        try:
            client = genai.Client(api_key=api_key)
            focus_clause = (
                f"\n추가 요청 관점: {custom_focus.strip()}\n" if custom_focus.strip() else ""
            )
            prompt = f"""당신은 유튜브 영상 스크립트를 핵심만 일목요연하게 정리해주는 전문 요약가입니다.
아래 유튜브 영상 정보와 타임스탬프 스크립트 대본을 읽고, 반드시 한국어로 명확하게 정리해 주세요.
(영문이나 외국어 스크립트라도 한국어로 번역·요약하세요.)
{focus_clause}
[영상 제목]: {meta.title}
[채널명]: {meta.uploader}
[재생 시간]: {meta.duration_str}

[추출된 스크립트]:
{script_for_prompt}

반드시 아래 JSON 스키마 형식으로만 응답하세요 (마크다운 코드블록 없이 순수 JSON 또는 ```json 블록):
{{
  "one_line_summary": "영상 전체를 관통하는 한 줄 핵심 요약 (1~2문장)",
  "key_takeaways": [
    "핵심 요약 포인트 1",
    "핵심 요약 포인트 2",
    "핵심 요약 포인트 3"
  ],
  "timeline_highlights": [
    "[00:00] 도입부 및 핵심 주제 소개",
    "[00:10] 주요 내용 전개 요약"
  ],
  "detailed_summary_md": "### 📌 전체 맥락 및 상세 정리\\n- 단락별로 읽기 쉽게 정리한 마크다운 본문"
}}"""
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            raw_text = (resp.text or "").strip()
            cleaned_json = re.sub(r"^```(?:json)?\s*", "", raw_text)
            cleaned_json = re.sub(r"\s*```$", "", cleaned_json).strip()
            parsed = json.loads(cleaned_json)

            one_line = str(parsed.get("one_line_summary") or "").strip()
            takeaways = [str(x).strip() for x in (parsed.get("key_takeaways") or []) if str(x).strip()]
            timelines = [
                str(x).strip() for x in (parsed.get("timeline_highlights") or []) if str(x).strip()
            ]
            detailed = str(parsed.get("detailed_summary_md") or "").strip()
            if one_line and takeaways:
                return one_line, takeaways, timelines, detailed, "gemini-2.5-flash"
        except Exception:
            pass

    # 오프라인/폴백 규칙 기반 구조화 요약
    full_plain = " ".join(s.text for s in segments)
    one_line = f"'{meta.title}' ({meta.uploader}) 영상의 추출 스크립트 핵심 요약본입니다."
    takeaways = [
        f"총 {len(segments)}개 구간({meta.duration_str})의 음성/자막 대본이 추출되었습니다.",
        f"도입부 발언: {segments[0].text[:120]}" if segments else "자막 데이터 없음",
    ]
    if len(segments) > 2:
        takeaways.append(f"결론부 발언: {segments[-1].text[:120]}")

    step = max(1, len(segments) // 5)
    timelines = [f"[{s.timestamp_str}] {s.text[:90]}" for s in segments[::step][:6]]
    detailed = (
        f"### 📌 스크립트 핵심 발췌\n\n"
        f"{full_plain[:800]}{'...' if len(full_plain) > 800 else ''}"
    )
    return one_line, takeaways, timelines, detailed, "extractive-fallback"


def extract_and_summarize_video(req: SummarizeRequest) -> TranscriptNote:
    """유튜브 링크에서 스크립트를 추출하고 핵심 내용을 요약·정리하여 SQLite DB 보관함에 저장"""
    meta = extract_video_metadata(req.url)
    segments, lang_code, ext_engine = fetch_transcript_segments(meta.video_id, meta.url)

    one_line, takeaways, timelines, detailed_md, sum_engine = _summarize_with_gemini_or_fallback(
        meta=meta,
        segments=segments,
        custom_focus=req.custom_focus,
    )

    full_plain = " ".join(s.text for s in segments)
    timestamped_text = "\n".join(f"[{s.timestamp_str}] {s.text}" for s in segments)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    note_id = uuid.uuid4().hex[:10]

    note = TranscriptNote(
        note_id=note_id,
        video_id=meta.video_id,
        url=meta.url,
        title=meta.title,
        uploader=meta.uploader,
        duration_sec=meta.duration_sec,
        duration_str=meta.duration_str,
        thumbnail_url=meta.thumbnail_url,
        language_used=lang_code,
        extraction_engine=ext_engine,
        summarizer_engine=sum_engine,
        one_line_summary=one_line,
        key_takeaways=takeaways,
        timeline_highlights=timelines,
        detailed_summary_md=detailed_md,
        full_transcript_text=full_plain,
        timestamped_transcript=timestamped_text,
        segment_count=len(segments),
        created_at=now_iso,
    )
    save_transcript_note(note)
    return note


def format_note_as_markdown(note: TranscriptNote) -> str:
    """원할 때 파일(.md)로 내려받거나 복사할 수 있는 마크다운 문서 포맷"""
    takeaways_md = "\n".join(f"- {item}" for item in note.key_takeaways)
    timeline_md = "\n".join(f"- {item}" for item in note.timeline_highlights)
    return f"""# 📝 {note.title}

- **채널**: {note.uploader} (`{note.duration_str}`)
- **원본 링크**: {note.url}
- **스크립트 언어 / 엔진**: `{note.language_used}` (`{note.extraction_engine}` + `{note.summarizer_engine}`)
- **정리 일시**: {note.created_at}

---

## 💡 한 줄 핵심 요약
> {note.one_line_summary}

## ✅ 핵심 포인트 (Key Takeaways)
{takeaways_md}

## ⏱ 타임라인별 요약
{timeline_md}

## 📖 상세 정리
{note.detailed_summary_md}

---

## 💬 전체 타임스탬프 스크립트 원문 ({note.segment_count}구간)
```text
{note.timestamped_transcript}
```
"""
