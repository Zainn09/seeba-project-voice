#!/usr/bin/env python3
"""
Merge Whisper result shards -> final deliverables:

  roman_urdu_transcriptions_151_onward.txt          (Roman Urdu, requested format)
  roman_urdu_transcriptions_151_onward_with_urdu.txt (Urdu + Roman, mirrors text_voices.docx layout)
  transcription/qc_report.txt                        (status / confidence / OOV / gaps per voice)

Rules enforced here:
  * Every audio file from --start onward gets exactly one entry, keyed by
    its real filename; silent, failed or missing results are written as
    [unclear] entries (never silently skipped, never invented text).
  * text_voices.docx is read-only and never modified. NOTE: sample tests
    (Voices 151/166) showed the .docx entry numbers in the MP3 range do
    NOT match the same-numbered audio files' contents (the document's
    paragraphs are offset), so docx entries are never used as a reason to
    skip an audio file -- every file from 151 onward is transcribed here.
  * Nothing in Voices/ or text_voices.docx is read-write; both are read-only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transliterate import Transliterator  # noqa: E402
from transcribe import discover, md5_of  # noqa: E402


def load_results(results_dir: Path) -> dict[int, dict]:
    """Latest record per voice across shards; an 'ok' record always wins."""
    best: dict[int, dict] = {}
    for shard in sorted(results_dir.glob("*.jsonl")):
        for line in shard.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            v = int(rec["voice"])
            prev = best.get(v)
            if prev is None or (rec.get("status") == "ok" and prev.get("status") != "ok") \
                    or rec.get("status") == prev.get("status"):
                best[v] = rec
    return best


def avg_logprob(rec: dict) -> float | None:
    segs = rec.get("segments") or []
    vals = [s["avg_logprob"] for s in segs if "avg_logprob" in s]
    return round(sum(vals) / len(vals), 3) if vals else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voices-dir", default="Voices")
    ap.add_argument("--results-dir", default="transcription/results")
    ap.add_argument("--existing-json", default="transcription/existing_transcriptions.json")
    ap.add_argument("--dictionary", default="transcription/roman_dictionary.json")
    ap.add_argument("--start", type=int, default=151)
    ap.add_argument("--out-roman", default="roman_urdu_transcriptions_151_onward.txt")
    ap.add_argument("--out-both", default="roman_urdu_transcriptions_151_onward_with_urdu.txt")
    ap.add_argument("--out-qc", default="transcription/qc_report.txt")
    args = ap.parse_args()

    files = dict(discover(Path(args.voices_dir)))
    # Byte-identical audio duplicates across the whole collection (e.g.
    # VOICE484-500.ogg are copies of VOICE7-23.ogg) -- noted, never hidden.
    by_md5: dict[str, list[int]] = {}
    for _n, _p in files.items():
        by_md5.setdefault(md5_of(_p), []).append(_n)
    dup_of = {n: [m for m in group if m != n]
              for group in by_md5.values() if len(group) > 1 for n in group}
    existing = json.loads(Path(args.existing_json).read_text(encoding="utf-8"))
    existing_voices = {int(k) for k, v in existing.items() if v.get("urdu") or v.get("roman")}
    results = load_results(Path(args.results_dir))
    tr = Transliterator.load(Path(args.dictionary))

    scope = sorted(n for n in files if n >= args.start)
    already_in_doc = [n for n in scope if n in existing_voices]
    targets = list(scope)  # transcribe EVERY audio file >= start (see module docstring)
    max_voice = max(files) if files else 0
    missing_audio = [n for n in range(1, max_voice + 1) if n not in files]

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    model_names = sorted({r.get("model", "?") for r in results.values()})

    roman_lines = [
        "Roman Urdu Transcriptions — Voice 151 onward",
        f"Generated: {now} | Engine: faster-whisper ({', '.join(model_names) or 'n/a'}) Urdu ASR "
        "+ dictionary transliteration learned from text_voices.docx",
        "Note: text_voices.docx also contains entries numbered "
        f"{', '.join(map(str, already_in_doc)) or 'none'}, but sample verification showed those entry "
        "numbers do not match the same-numbered audio files in the MP3 range (offset), so every audio "
        "file from 151 onward is transcribed here from the audio itself. The document and all audio files are unchanged.",
        "[unclear] marks recordings with no intelligible speech / no ASR result — see transcription/qc_report.txt.",
        "",
    ]
    both_lines = list(roman_lines)
    qc = [
        "Transcription QC report",
        f"Generated: {now}",
        f"Audio files discovered: {len(files)} (Voices {min(files)}..{max_voice}; missing audio numbers: {missing_audio or 'none'})",
        f"Existing transcriptions in text_voices.docx: {len(existing_voices)} "
        f"(range {min(existing_voices)}..{max(existing_voices)}; gaps: "
        f"{[n for n in range(min(existing_voices), max(existing_voices)+1) if n not in existing_voices] or 'none'})",
        f"Scope (>= {args.start}, audio present): {len(scope)} | numbers that also exist in docx (content verified NOT to match on samples 151/166, still transcribed from audio): {already_in_doc}",
        f"Voices in output: {len(targets)}",
        "",
        "voice | file | status | duration_s | avg_logprob | oov_rate | note",
    ]

    counts: Counter = Counter()
    all_oov: Counter = Counter()
    review: list[int] = []

    for n in targets:
        fname = files[n].name
        rec = results.get(n)
        heading = f"Voice {n} — {fname}"
        if rec is None:
            status, urdu, roman, oov_rate, note = "not_processed", "", "[unclear]", None, "no ASR result yet"
        elif rec.get("status") == "ok" and rec.get("urdu_text"):
            status = "ok"
            urdu = rec["urdu_text"].strip()
            roman, info = tr.transliterate(urdu)
            oov_rate = info["oov_rate"]
            all_oov.update(dict(info["oov_top"]))
            notes = []
            if n in existing_voices:
                notes.append("docx has same-numbered entry (content differs from this audio; see header)")
            if rec.get("duplicate_of_voice"):
                notes.append(f"byte-identical duplicate of Voice {rec['duplicate_of_voice']}")
            if oov_rate and oov_rate > 0.20:
                notes.append("high OOV — review Roman Urdu")
                review.append(n)
            lp = avg_logprob(rec)
            if lp is not None and lp < -1.0:
                notes.append("low ASR confidence — review audio")
                review.append(n)
            note = "; ".join(notes)
        elif rec.get("status") == "silent_or_empty":
            status, urdu, roman, oov_rate, note = "silent_or_empty", "", "[unclear]", None, "no speech recognised"
        else:
            status, urdu, roman, oov_rate = "error", "", "[unclear]", None
            note = f"ASR error: {rec.get('error', 'unknown')}" if rec else ""
        counts[status] += 1

        roman_lines += [heading, roman, ""]
        both_lines += [heading, "Urdu:", urdu if urdu else "[unclear]", "Roman Urdu:", roman, ""]
        dur = rec.get("duration_sec") if rec else None
        qc.append(f"{n} | {fname} | {status} | {dur} | {avg_logprob(rec) if rec else None} | "
                  f"{oov_rate} | {note}")

    qc += ["", f"Status counts (new voices): {dict(counts)}",
           f"Voices flagged for human review: {sorted(set(review)) or 'none'}",
           "", "Most frequent out-of-vocabulary Urdu words in new transcripts "
           "(add reviewed spellings to transcription/manual_roman_overrides.json, then rebuild):"]
    qc += [f"  {w}: {c}" for w, c in all_oov.most_common(60)] or ["  (none)"]

    Path(args.out_roman).write_text("\n".join(roman_lines), encoding="utf-8")
    Path(args.out_both).write_text("\n".join(both_lines), encoding="utf-8")
    Path(args.out_qc).write_text("\n".join(qc) + "\n", encoding="utf-8")
    print("\n".join(qc[:8]))
    print(f"Wrote {args.out_roman}\nWrote {args.out_both}\nWrote {args.out_qc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
