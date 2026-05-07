"""
MDS Video Platform — chapter detection via Anthropic's Claude API.

After transcripts.py finishes inserting segments, it calls
generate_and_apply_chapters() here. We:

  1. Build a compact, timestamped prompt from the utterance list.
  2. POST to api.anthropic.com/v1/messages with claude-haiku-4-5 —
     cheapest decent model, ~$0.005 per 90-min video.
  3. Parse the JSON chapter list it returns.
  4. UPDATE each transcript_segments row whose start_ms falls inside a
     given chapter to set chapter_title.

Chapters live ON the segments (per Page 3 schema) — iOS retrieves them by
selecting distinct chapter_title ordered by min(start_ms) per chapter.

NOTE: We originally targeted AssemblyAI's LLM Gateway, but Andy's free
AssemblyAI account doesn't include LLM Gateway access (returns 401:
"Your account does not have access to LLM Gateway. Please upgrade..."),
and mds-ai-bot already has an Anthropic API key wired up for the
existing /api/ask route. Calling Anthropic directly is the cleaner path
here — same outcome, no extra paid AssemblyAI tier needed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import requests

from videos import _supabase_base, _supabase_key

log = logging.getLogger(__name__)


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5"
MIN_CHAPTERS = 4
MAX_CHAPTERS = 12


def _api_key() -> str:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured.")
    return key


def _supabase_patch_segment_chapter(video_id: str, start_ms: int, end_ms: int,
                                     chapter_title: str) -> None:
    """PATCH transcript_segments where video_id matches and start_ms is in
    [chapter.start, next_chapter.start). Uses PostgREST range filters."""
    url = f"{_supabase_base()}/rest/v1/transcript_segments"
    key = _supabase_key()
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    params = {
        "video_id": f"eq.{video_id}",
        "start_ms": f"gte.{start_ms}",
        "end_ms": f"lte.{end_ms}",
    }
    body = {"chapter_title": chapter_title}
    r = requests.patch(url, headers=headers, params=params, json=body, timeout=15)
    r.raise_for_status()


def _build_prompt_input(utterances: list[dict], total_duration_ms: int) -> str:
    """Compact representation of the transcript with periodic timestamps.
    We don't need every word — just enough that Claude can decide where
    chapters start. Strategy: take the first 2 sentences of each minute
    of audio, prefixed with [mm:ss], so the model sees ~total_duration/60
    anchors.

    For very long videos (90 min) this produces ~90 lines of input — well
    within Claude Haiku's context."""
    lines: list[str] = []
    last_min_emitted = -1
    for utt in utterances:
        start_ms = int(utt.get("start") or 0)
        minute = start_ms // 60_000
        if minute == last_min_emitted:
            continue
        last_min_emitted = minute
        ts = f"{minute // 60:d}:{minute % 60:02d}"
        # Truncate each utterance line so the overall prompt stays compact.
        text = (utt.get("text") or "").strip()
        if len(text) > 220:
            text = text[:220].rsplit(" ", 1)[0] + "…"
        lines.append(f"[{ts}] {text}")

    return "\n".join(lines)


def _system_prompt(min_chapters: int, max_chapters: int) -> str:
    return (
        "You split video transcripts into chapters for navigation. "
        "Given a transcript with [mm:ss] timestamps, return ONLY a JSON "
        "array of chapter objects. Each object has exactly two keys: "
        "\"title\" (1–6 words capturing the topic; concise, no marketing "
        "fluff) and \"start_ms\" (integer milliseconds, the timestamp the "
        "chapter begins). Constraints:\n"
        f"- Produce between {min_chapters} and {max_chapters} chapters total.\n"
        "- The first chapter MUST have start_ms = 0.\n"
        "- Chapter starts MUST be strictly increasing.\n"
        "- Chapters must cover meaningful topic shifts, not arbitrary "
        "time slices. Combine adjacent minutes if they're on the same "
        "topic.\n"
        "- Output JSON only. No markdown fences, no commentary, no prose."
    )


def _parse_chapters(text: str) -> list[dict]:
    """Extract the JSON array from Claude's response. Tolerates surrounding
    whitespace and accidental ```json fences."""
    raw = text.strip()
    # Strip code fences if present.
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError(f"expected list, got {type(parsed).__name__}")
    out: list[dict] = []
    last_start = -1
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        title = (entry.get("title") or "").strip()
        start = entry.get("start_ms")
        if not title or start is None:
            continue
        try:
            start_ms = int(start)
        except (TypeError, ValueError):
            continue
        if start_ms <= last_start:
            continue  # enforce increasing
        out.append({"title": title, "start_ms": start_ms})
        last_start = start_ms
    if not out:
        raise ValueError("no valid chapters parsed")
    if out[0]["start_ms"] != 0:
        # Force the first chapter to start at 0 (model occasionally drifts).
        out[0] = {"title": out[0]["title"], "start_ms": 0}
    return out


def _call_anthropic(transcript_compact: str, video_title: str,
                     model: str = DEFAULT_MODEL) -> str:
    headers = {
        "x-api-key": _api_key(),
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    user_content = (
        f"Video title: {video_title}\n\n"
        f"Transcript with [mm:ss] anchors:\n{transcript_compact}"
    )
    body = {
        "model": model,
        "max_tokens": 1500,
        "temperature": 0.2,
        "system": _system_prompt(MIN_CHAPTERS, MAX_CHAPTERS),
        "messages": [
            {"role": "user", "content": user_content},
        ],
    }
    r = requests.post(ANTHROPIC_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    # Anthropic Messages API returns content as a list of content blocks.
    blocks = data.get("content") or []
    for blk in blocks:
        if blk.get("type") == "text":
            return blk.get("text") or ""
    raise ValueError(f"no text block in Anthropic response: {data!r}")


def generate_and_apply_chapters(video_id: str, video_title: str,
                                 transcript_text: str,
                                 utterances: list[dict]) -> Optional[list[dict]]:
    """Top-level entry called by transcripts.py. Returns the chapter list
    that was applied, or None on failure (caller swallows the exception
    so transcript persistence is not blocked)."""
    if not utterances:
        log.info("chapters: no utterances for video=%s — skipping", video_id)
        return None

    last = utterances[-1]
    total_duration_ms = int(last.get("end") or 0)
    if total_duration_ms < 60_000:
        log.info("chapters: video=%s shorter than 1 min — skipping", video_id)
        return None

    transcript_compact = _build_prompt_input(utterances, total_duration_ms)

    raw_text = _call_anthropic(transcript_compact, video_title)
    chapters = _parse_chapters(raw_text)
    log.info("chapters: video=%s got %d chapters", video_id, len(chapters))

    # Append a sentinel end so the last chapter's range is well-defined.
    boundaries = [(c["title"], c["start_ms"]) for c in chapters]
    boundaries.append(("__end__", total_duration_ms + 1))

    # PATCH transcript_segments per chapter.
    for i in range(len(boundaries) - 1):
        title, start_ms = boundaries[i]
        _, next_start_ms = boundaries[i + 1]
        # We tag segments whose entire span sits within [start_ms, next_start_ms).
        # Using start_ms >= chapter_start and end_ms < next_chapter_start so
        # cross-boundary segments don't get mis-tagged.
        try:
            _supabase_patch_segment_chapter(
                video_id=video_id,
                start_ms=start_ms,
                end_ms=next_start_ms - 1,
                chapter_title=title,
            )
        except Exception:
            log.exception("chapters: failed to PATCH segment range "
                          "for video=%s chapter=%r", video_id, title)

    return chapters
