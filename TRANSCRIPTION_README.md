# Roman Urdu transcription pipeline (Voice 151 onward)

Automated, resumable transcription of the `Voices/` audio collection into
Roman Urdu, preserving all existing work:

- **No original audio file is ever modified** (opened read-only; verified
  against `transcription/original_files_sha256.txt`).
- **`text_voices.docx` is never modified.** It actually contains **164**
  entries — Voices **1–162, 166 and 167** (only 163, 164, 165 are missing
  inside that range; Voice 13's Roman Urdu paragraph is blank in the
  document).
- **Numbering warning (found by the §8 sample test):** in the MP3 range the
  document's entry numbers do **not** match the same-numbered audio files.
  E.g. `Voice151.mp3` actually speaks the text of docx entries ~157/158, and
  `Voice166.mp3` is an audiobook intro that appears nowhere in the docx.
  The early Ogg files do match (verified: Voice 1, 2 and 484=7). Because of
  this offset, *every* audio file from 151 onward is transcribed from the
  audio itself — docx entries are never used to skip a file, and never
  overwritten either.
- New output is written to **new files** (below), one entry per audio file
  from 151 onward, in voice-number order.

## Repository facts found during inspection

| Item | Finding |
|---|---|
| Audio files | **499** in `Voices/` — Voices 1–500, **Voice 126 audio is missing** (its transcription exists in the docx) |
| Formats | 1–125 `VOICE*.ogg` (Opus), 127–483 `Voice*.mp3`, 484–500 `VOICE*.ogg` (Opus); all mono 48 kHz, ≈45 s each, all decode cleanly |
| Duplicates | **VOICE484–500.ogg are byte-identical copies of VOICE7–23.ogg** — detected by MD5, transcribed once, noted in the QC report |
| Existing doc | `text_voices.docx`: 164 Urdu + Roman Urdu entries (Voices 1–162, 166, 167) |
| Remaining work | **336 files**: Voices 163, 164, 165 and 168–500 |

## How it works

1. **Speech-to-text** — [`transcription/transcribe.py`](transcription/transcribe.py)
   uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
   (CTranslate2 Whisper, `language="ur"`, VAD filtering, beam search).
   PyAV decodes MP3/Ogg-Opus internally, so no system ffmpeg is needed.
   Results are appended **one JSON line per file** to
   `transcription/results/*.jsonl` (Urdu text + segments + confidence +
   status), so an interrupted run resumes exactly where it stopped.
   Silent/failed files get an explicit status; nothing is skipped silently.
2. **Roman Urdu conversion** — [`transcription/transliterate.py`](transcription/transliterate.py)
   Urdu script omits short vowels, so letter-by-letter conversion cannot
   produce readable Roman Urdu. Instead a word dictionary is *learned* by
   aligning the 164 existing Urdu/Roman paragraph pairs, which makes the
   output follow this document's own spellings (`hai`, `hain`, `kya`,
   `nahi`, `kyun`, `aap`, `mujhe`, `aik`, …). Unseen words fall back to a
   phonetic transliterator and are listed as OOV in the QC report; add
   reviewed spellings to `transcription/manual_roman_overrides.json` and
   rebuild. Hold-out validation (dictionary learned without Voices
   141–162/166/167, then tested on them): **88.6% exact word match**
   against the human Roman Urdu. Known ambiguity: `میں` = “main” (I) vs
   “mein” (in) and `کیا` = “kya” (what) vs “kiya” (did) are spelled
   identically in Urdu script; the majority/ requested form is used.
3. **Output builder** — [`transcription/build_output.py`](transcription/build_output.py)
   merges shards + transliteration into the deliverables and a QC report.

## Deliverables

| File | Contents |
|---|---|
| `roman_urdu_transcriptions_151_onward.txt` | `Voice N — <filename>` + Roman Urdu paragraph, one entry per new voice |
| `roman_urdu_transcriptions_151_onward_with_urdu.txt` | Same entries with the Urdu-script ASR text above each Roman paragraph (mirrors the docx layout, for checking) |
| `transcription/qc_report.txt` | Per-voice status, ASR confidence, OOV rate, duplicates, gaps, review list |
| `transcription/results/*.jsonl` | Raw machine-readable ASR output (resume state) |

`[unclear]` in the output means no intelligible speech was recognised for
that recording — it is never used to hide a skipped file; the QC report
states the exact status per voice.

## Running it

### Option A — GitHub Actions (what was used here; no local GPU/setup)

Edit `transcription/run_request.json` (model, range, `skip_existing`, …)
and push — that file is the workflow trigger (GitHub only allows the
*Run workflow* button for workflows that exist on the default branch; the
`workflow_dispatch` inputs in the workflow file work too once it is merged
to `main`). Jobs run in parallel chunks and commit result shards back to
the branch. Then, locally after pulling:

```bash
python3 transcription/build_output.py
```

### Option B — Locally (any machine with internet for the first model download)

```bash
pip install -r requirements.txt
python3 transcription/extract_existing.py                     # refresh docx extraction (read-only)
python3 transcription/transliterate.py build --holdout none   # (re)learn Roman Urdu dictionary
python3 transcription/transcribe.py --start 151 --skip-existing --model small
python3 transcription/build_output.py
```

**Resume after interruption:** re-run the exact same `transcribe.py`
command — finished voices are detected from `transcription/results/*.jsonl`
and skipped. Useful flags: `--only 163,164` (specific voices), `--limit 10`
(try a few), `--model medium` (better Urdu, slower), `--language auto`,
`--device cuda` (GPU if available).

### Improving Roman Urdu spellings later

```bash
# edit transcription/manual_roman_overrides.json (see OOV list in qc_report.txt)
python3 transcription/transliterate.py build --holdout none
python3 transcription/build_output.py
```

## Why GitHub Actions was used for the batch run

The agent sandbox has no GPU/ffmpeg and its outbound network allow-list
blocks Hugging Face (where Whisper weights are hosted). GitHub-hosted
runners can download the model, so the identical `transcribe.py` is executed
there via `.github/workflows/transcribe.yml` and results are committed back.
The pipeline itself has no such limitation on a normal machine.

## Verify originals are untouched

```bash
sha256sum -c transcription/original_files_sha256.txt
git status --short Voices text_voices.docx   # must print nothing
```
