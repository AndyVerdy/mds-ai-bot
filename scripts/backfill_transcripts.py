#!/usr/bin/env python3
"""
M3 backfill — submit all eligible videos for AssemblyAI transcription.

Eligible = mux_status='ready' AND transcription_status IS NULL (or 'failed'
if --include-failed). Idempotent: re-running won't re-submit videos already
in 'processing' or 'ready' state unless --force is passed.

Usage:
  # Dry-run (just lists what would happen):
  python3 scripts/backfill_transcripts.py --dry-run

  # Submit all eligible:
  python3 scripts/backfill_transcripts.py

  # Re-submit a specific video by id:
  python3 scripts/backfill_transcripts.py --video-id <uuid>

  # Re-submit including ones marked failed:
  python3 scripts/backfill_transcripts.py --include-failed

After submission, AssemblyAI takes ~5–15% of audio duration to process.
Webhooks fire to https://mds-ai-bot.onrender.com/api/webhooks/assemblyai
to populate transcript_segments + chapters.

Required env (mirrors what Render needs):
  SUPABASE_URL                   = https://nadtudwuwjhckotrngzn.supabase.co
  SUPABASE_SERVICE_ROLE_KEY      = <sb_secret_... or JWT service role>
  ASSEMBLYAI_API_KEY             = <key>
  ASSEMBLYAI_WEBHOOK_SECRET      = <shared secret matching Render>
  PUBLIC_BACKEND_URL             = https://mds-ai-bot.onrender.com
                                    (defaulted, override only for staging)
"""

import argparse
import os
import sys

# Allow running from repo root.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from videos import _supabase_get  # noqa: E402
import transcripts  # noqa: E402


def list_eligible(*, include_failed: bool) -> list[dict]:
    select = ("id,title,mux_status,transcription_status,duration_sec,"
              "mux_playback_id,assemblyai_transcript_id")
    rows = _supabase_get(
        "videos",
        params={
            "mux_status": "eq.ready",
            "deleted_at": "is.null",
            "select": select,
            "order": "created_at.asc",
        },
    )
    out: list[dict] = []
    for r in rows:
        status = r.get("transcription_status")
        if status in (None, "pending"):
            out.append(r)
        elif include_failed and status == "failed":
            out.append(r)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="List eligible videos without submitting.")
    p.add_argument("--video-id",
                   help="Submit only this video id (overrides eligibility).")
    p.add_argument("--include-failed", action="store_true",
                   help="Also resubmit videos in 'failed' state.")
    p.add_argument("--force", action="store_true",
                   help="Submit even if already 'processing' or 'ready'.")
    args = p.parse_args()

    if args.video_id:
        targets = [{"id": args.video_id, "title": "(by id)"}]
    else:
        targets = list_eligible(include_failed=args.include_failed)
        if args.force:
            # Force-mode also picks up processing/ready rows.
            rows = _supabase_get(
                "videos",
                params={
                    "mux_status": "eq.ready",
                    "deleted_at": "is.null",
                    "select": "id,title,transcription_status",
                },
            )
            targets = rows

    if not targets:
        print("No eligible videos. Exiting.")
        return 0

    print(f"{'Would submit' if args.dry_run else 'Submitting'} {len(targets)} video(s):")
    for t in targets:
        marker = "•" if not args.dry_run else "○"
        print(f"  {marker} {t.get('title')!r}  id={t['id']}  "
              f"status={t.get('transcription_status')}")

    if args.dry_run:
        return 0

    failures: list[tuple[str, str]] = []
    for t in targets:
        try:
            resp = transcripts.submit_transcription(t["id"])
            print(f"   → submitted: transcript_id={resp.get('id')} "
                  f"status={resp.get('status')}")
        except Exception as e:
            print(f"   ! error: {e}")
            failures.append((t["id"], str(e)))

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for vid, err in failures:
            print(f"  {vid}  {err}")
        return 1

    print("\nAll submitted. AssemblyAI will POST completion webhooks to:")
    print(f"  {transcripts._webhook_url()}")
    print("Track progress with:")
    print("  SELECT id, title, transcription_status, assemblyai_transcript_id")
    print("  FROM videos WHERE deleted_at IS NULL ORDER BY uploaded_at DESC;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
