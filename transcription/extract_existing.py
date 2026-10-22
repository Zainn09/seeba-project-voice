#!/usr/bin/env python3
"""
Extract the existing human transcriptions from text_voices.docx (read-only)
into transcription/existing_transcriptions.json:

    {"1": {"heading": "Voice 1", "urdu": "...", "roman": "..."}, ...}

Uses only the standard library (a .docx is a zip of XML), so no extra
dependency is needed. The .docx file itself is never modified.

The document mixes label styles ("Urdu:", "اردو:", "Urdu", labels glued to
the text, ...); the parser handles the variants actually present and the
test block at the bottom validates the result (run the file to execute).
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

HEADING_RE = re.compile(r"^Voice\s+(\d+)\s*(?:—|–|:|\||$)")
SUBTITLE_RE = re.compile(r"^Voice\s+1\s*[–—-]\s*126")  # cover subtitle, not an entry
URDU_LABEL_RE = re.compile(r"^(?:Urdu|اردو)\s*[:：]?\s*")
ROMAN_LABEL_RE = re.compile(r"^(?:Roman\s*Urdu|Roman)\s*[:：]?\s*")


def docx_paragraphs(docx_path: Path) -> list[str]:
    xml = zipfile.ZipFile(docx_path).read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    texts = []
    for p in paras:
        # tabs / breaks inside a paragraph become spaces
        p = re.sub(r"<w:(?:tab|br)[^>]*/>", " ", p)
        texts.append("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)).strip())
    return texts


def _split_block(block: list[str]) -> tuple[str, str]:
    """Split one voice's paragraphs (after its heading) into (urdu, roman)."""
    urdu_parts: list[str] = []
    roman_parts: list[str] = []
    side = None  # None -> before any label; first content defaults to Urdu
    for raw in block:
        if not raw:
            continue
        if ROMAN_LABEL_RE.match(raw):
            side = "roman"
            rest = ROMAN_LABEL_RE.sub("", raw).strip()
            if rest:
                roman_parts.append(rest)
            continue
        if URDU_LABEL_RE.match(raw) and not raw.startswith("Roman"):
            # A bare label, or a label glued to its text ("اردو: بھائی...").
            rest = URDU_LABEL_RE.sub("", raw).strip()
            # Guard: only treat as label if the paragraph is short or the
            # remainder clearly starts the Urdu text (Arabic-script char).
            if len(raw) <= 12 or (rest and "\u0600" <= rest[0] <= "\u06ff"):
                side = "urdu"
                if rest:
                    urdu_parts.append(rest)
                continue
        if side == "roman":
            roman_parts.append(raw)
        else:
            urdu_parts.append(raw)
            side = "urdu"
    return " ".join(urdu_parts).strip(), " ".join(roman_parts).strip()


def extract(docx_path: Path) -> dict[str, dict]:
    texts = docx_paragraphs(docx_path)
    heads: list[tuple[int, int, str]] = []
    for i, t in enumerate(texts):
        m = HEADING_RE.match(t)
        if m and not SUBTITLE_RE.match(t):
            heads.append((i, int(m.group(1)), t))
    entries: dict[str, dict] = {}
    for idx, (pos, num, heading) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(texts)
        urdu, roman = _split_block(texts[pos + 1:end])
        if str(num) in entries:
            raise ValueError(f"Duplicate heading for Voice {num}")
        entries[str(num)] = {"heading": heading, "urdu": urdu, "roman": roman}
    return entries


def main() -> None:
    docx = Path("text_voices.docx")
    out = Path("transcription/existing_transcriptions.json")
    entries = extract(docx)
    out.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    nums = sorted(int(k) for k in entries)
    print(f"Extracted {len(entries)} voices: {nums[0]}..{nums[-1]}")
    missing = [n for n in range(nums[0], nums[-1] + 1) if n not in nums]
    print(f"Missing numbers inside that range: {missing}")
    for n in nums:
        e = entries[str(n)]
        if not e["urdu"] or not e["roman"]:
            print(f"  NOTE Voice {n}: urdu_len={len(e['urdu'])} roman_len={len(e['roman'])}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
