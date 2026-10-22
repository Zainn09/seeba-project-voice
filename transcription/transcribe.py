#!/usr/bin/env python3
"""
Batch speech-to-text for the Voices/ collection (Urdu -> Urdu script).

Design goals (see TRANSCRIPTION_README.md):
  * Original audio files are opened read-only and never modified.
  * Resumable: one JSON line is appended (and flushed) per finished file;
    voices already present with status "ok" in any existing result shard
    are skipped on re-runs.
  * Failures never abort the batch: silent / empty / corrupt files are
    recorded with an explicit status and processing continues.
  * Duplicate audio (identical MD5) is transcribed once and reused.

Engine: faster-whisper (CTranslate2). Decoding is handled internally by
PyAV, so no system ffmpeg is required and .ogg/Opus + .mp3 both work.
The recognised text is Urdu script; Roman Urdu conversion is a separate,
auditable step (transliterate.py) so the raw ASR output is never lost.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

AUDIO_RE = re.compile(r"^voice(\d+)\.(ogg|mp3|wav|m4a|flac|opus|webm)$", re.IGNORECASE)


def discover(voices_dir: Path) -> list[tuple[int, Path]]:
    """Return [(voice_number, path), ...] sorted numerically."""
    found: dict[int, Path] = {}
    for p in sorted(voices_dir.iterdir()):
        m = AUDIO_RE.match(p.name)
        if not m or not p.is_file():
            continue
        n = int(m.group(1))
        if n in found:
            raise SystemExit(f"Duplicate voice number {n}: {found[n].name} and {p.name}")
        found[n] = p
    return sorted(found.items())


def load_done(results_dir: Path) -> dict[int, dict]:
    """Voices already transcribed OK in any existing *.jsonl shard."""
    done: dict[int, dict] = {}
    if not results_dir.exists():
        return done
    for shard in sorted(results_dir.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok" and rec.get("urdu_text"):
                done[int(rec["voice"])] = rec
    return done


def load_existing_voices(existing_json: Path | None) -> set[int]:
    """Voice numbers that already have a (human) transcription in the docx."""
    if not existing_json or not existing_json.exists():
        return set()
    data = json.loads(existing_json.read_text(encoding="utf-8"))
    return {int(k) for k, v in data.items() if (v.get("urdu") or v.get("roman"))}


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices-dir", default="Voices")
    ap.add_argument("--out", default="transcription/results/whisper.jsonl",
                    help="JSONL shard to append results to")
    ap.add_argument("--results-dir", default="transcription/results",
                    help="Directory scanned for already-completed voices (resume)")
    ap.add_argument("--model", default="small",
                    help="faster-whisper model: tiny|base|small|medium|large-v3 or a local CT2 dir")
    ap.add_argument("--language", default="ur",
                    help="'ur' forces Urdu; 'auto' lets Whisper detect the language")
    ap.add_argument("--start", type=int, default=151)
    ap.add_argument("--end", type=int, default=10**9)
    ap.add_argument("--only", default="", help="Comma-separated voice numbers to process (overrides range)")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N files (0 = no limit)")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip voices already transcribed in text_voices.docx")
    ap.add_argument("--existing-json", default="transcription/existing_transcriptions.json")
    ap.add_argument("--device", default="auto", help="auto|cpu|cuda")
    ap.add_argument("--compute-type", default="auto", help="auto|int8|float16|float32")
    ap.add_argument("--cpu-threads", type=int, default=0, help="0 = let CTranslate2 decide")
    ap.add_argument("--beam-size", type=int, default=5)
    ap.add_argument("--no-vad", action="store_true", help="Disable VAD silence filtering")
    ap.add_argument("--retry-failed", action="store_true",
                    help="Re-process voices whose previous status was not ok (default behaviour; kept for clarity)")
    ap.add_argument("--force", action="store_true", help="Re-transcribe even voices already done")
    args = ap.parse_args()

    voices_dir = Path(args.voices_dir)
    out_path = Path(args.out)
    results_dir = Path(args.results_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_files = discover(voices_dir)
    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    existing = load_existing_voices(Path(args.existing_json)) if args.skip_existing else set()
    done = {} if args.force else load_done(results_dir)
    # MD5 -> finished record, so byte-identical duplicates are reused.
    by_md5: dict[str, dict] = {r["md5"]: r for r in done.values() if r.get("md5")}

    todo: list[tuple[int, Path]] = []
    skipped_existing, skipped_done = [], []
    for n, path in all_files:
        if only is not None:
            if n not in only:
                continue
        elif not (args.start <= n <= args.end):
            continue
        if n in existing:
            skipped_existing.append(n)
            continue
        if n in done:
            skipped_done.append(n)
            continue
        todo.append((n, path))
    if args.limit:
        todo = todo[: args.limit]

    print(f"Discovered {len(all_files)} audio files in {voices_dir}/", flush=True)
    print(f"To process: {len(todo)} | skipped (already in docx): {len(skipped_existing)} "
          f"| skipped (already transcribed): {len(skipped_done)}", flush=True)
    if skipped_existing:
        print(f"  existing-docx voices skipped: {skipped_existing}", flush=True)
    if not todo:
        print("Nothing to do.", flush=True)
        return 0

    # Import here so --help / discovery work without the heavy dependency.
    from faster_whisper import WhisperModel

    device = args.device
    compute = args.compute_type
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"

    print(f"Loading model '{args.model}' device={device} compute={compute} ...", flush=True)
    t0 = time.time()
    model = WhisperModel(args.model, device=device, compute_type=compute,
                         cpu_threads=args.cpu_threads)
    print(f"Model loaded in {time.time()-t0:.1f}s", flush=True)

    language = None if args.language == "auto" else args.language
    counts = {"ok": 0, "silent_or_empty": 0, "error": 0, "duplicate_reused": 0}

    with out_path.open("a", encoding="utf-8") as out:
        for idx, (n, path) in enumerate(todo, 1):
            started = time.time()
            rec: dict = {
                "voice": n,
                "filename": path.name,
                "model": args.model,
                "engine": "faster-whisper",
                "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            }
            try:
                rec["md5"] = md5_of(path)
                dup = by_md5.get(rec["md5"])
                if dup is not None and dup["voice"] != n:
                    rec.update({
                        "status": "ok",
                        "urdu_text": dup["urdu_text"],
                        "segments": dup.get("segments", []),
                        "language": dup.get("language"),
                        "language_probability": dup.get("language_probability"),
                        "duration_sec": dup.get("duration_sec"),
                        "duplicate_of_voice": dup["voice"],
                    })
                    counts["duplicate_reused"] += 1
                else:
                    segments, info = model.transcribe(
                        str(path),
                        language=language,
                        task="transcribe",
                        beam_size=args.beam_size,
                        vad_filter=not args.no_vad,
                        vad_parameters={"min_silence_duration_ms": 500},
                        condition_on_previous_text=False,
                        word_timestamps=False,
                    )
                    seg_list = []
                    for s in segments:  # generator: inference happens here
                        seg_list.append({
                            "start": round(s.start, 2),
                            "end": round(s.end, 2),
                            "text": s.text.strip(),
                            "avg_logprob": round(s.avg_logprob, 3),
                            "no_speech_prob": round(s.no_speech_prob, 3),
                        })
                    text = " ".join(s["text"] for s in seg_list).strip()
                    rec.update({
                        "language": info.language,
                        "language_probability": round(float(info.language_probability), 3),
                        "duration_sec": round(float(info.duration), 2),
                        "segments": seg_list,
                        "urdu_text": text,
                        "status": "ok" if text else "silent_or_empty",
                    })
                    if text:
                        by_md5[rec["md5"]] = rec
            except Exception as exc:  # never abort the whole batch for one file
                rec.update({"status": "error", "error": f"{type(exc).__name__}: {exc}",
                            "urdu_text": "", "segments": []})
            rec["elapsed_sec"] = round(time.time() - started, 2)
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            preview = (rec.get("urdu_text") or "")[:70]
            print(f"[{idx}/{len(todo)}] Voice {n} ({path.name}) status={rec['status']} "
                  f"dur={rec.get('duration_sec')}s elapsed={rec['elapsed_sec']}s :: {preview}",
                  flush=True)

    print(f"Done. {counts} -> {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
