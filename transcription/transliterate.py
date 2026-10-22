#!/usr/bin/env python3
"""
Urdu script -> Roman Urdu transliteration, learned from the project itself.

Urdu script normally omits short vowels, so a purely character-by-letter
conversion cannot produce the readable Roman Urdu used in text_voices.docx
("مجھے" must become "mujhe", not "mjhy"). Instead this module *learns* a
word-level Urdu->Roman dictionary by aligning the 164 existing
Urdu/Roman paragraph pairs in the document, so the output automatically
follows the document's own spelling conventions (hai, hain, kya, nahi,
kyun, aap, mujhe, ...). Words never seen in the document fall back to a
phonetic character transliterator and are reported as out-of-vocabulary
(OOV) so they can be reviewed / added to manual_roman_overrides.json.

Usage:
    python3 transliterate.py build            # learn + save dictionary, with hold-out evaluation
    python3 transliterate.py apply "اردو جملہ"  # transliterate one string (uses saved dictionary)
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
DICT_PATH = HERE / "roman_dictionary.json"
OVERRIDES_PATH = HERE / "manual_roman_overrides.json"
EXISTING_PATH = HERE / "existing_transcriptions.json"

# ---------------------------------------------------------------- normalise
_DIACRITICS = re.compile(r"[\u064b-\u065f\u0670\u06d6-\u06ed]")
_CHAR_FIX = str.maketrans({
    "ي": "ی", "ك": "ک", "ة": "ت", "ۀ": "ہ", "ؤ": "و",
    # ASR often writes ئ where the document writes ی (e.g. لئے / لیے,
    # روئیے / رویے); normalising merges those dictionary keys.
    "ئ": "ی",
    "أ": "ا", "إ": "ا", "آ": "آ", "ـ": "", "‌": " ", "‍": "",
})
PUNCT_MAP = {"۔": ".", "،": ",", "؟": "?", "؛": ";", "：": ":", "«": '"', "»": '"',
             "“": '"', "”": '"', "‘": "'", "’": "'", "۔۔": "."}
URDU_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def norm_urdu_word(w: str) -> str:
    w = w.translate(_CHAR_FIX)
    w = _DIACRITICS.sub("", w)
    return w.strip(".,،۔؟?!;؛:\"'()[]«»“”‘’-")


TOKEN_RE = re.compile(r"[\w']+|[^\w\s]", re.UNICODE)


def tokenize(text: str) -> list[str]:
    # Strip vowel diacritics (zabar/zer/pesh/tanween/shadda) first: they are
    # not word characters and would otherwise leak through as stray tokens.
    text = _DIACRITICS.sub("", text.translate(_CHAR_FIX))
    return TOKEN_RE.findall(text)


def words_only(text: str) -> list[str]:
    return [t for t in tokenize(text) if re.search(r"[\w]", t)]


# ---------------------------------------------------------------- skeleton
_UR2LAT = {
    "ا": "a", "آ": "a", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z",
    "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q",
    "ک": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n", "ں": "n", "و": "w",
    "ہ": "h", "ھ": "h", "ی": "y", "ئ": "y", "ے": "e", "ء": "", "ۃ": "h",
}


def _collapse(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


def urdu_skeleton(w: str) -> str:
    lat = "".join(_UR2LAT.get(c, "") for c in norm_urdu_word(w))
    return _collapse(re.sub(r"[aeiou]", "", lat))


def roman_skeleton(w: str) -> str:
    w = w.lower().strip(".,?!;:\"'()[]-")
    w = w.replace("ph", "f")
    w = re.sub(r"c(?!h)", "k", w)
    return _collapse(re.sub(r"[aeiou]", "", w))


def similarity(urdu_w: str, roman_w: str) -> float:
    a, b = urdu_skeleton(urdu_w), roman_skeleton(roman_w)
    if not a or not b:
        return 0.0
    r1 = difflib.SequenceMatcher(None, a, b).ratio()
    # variant ignoring 'y' (ی is often a vowel in Roman: "ki", "bhi", ...)
    r2 = difflib.SequenceMatcher(None, a.replace("y", ""), b.replace("y", "")).ratio()
    # variant ignoring 'w' (و is often "o/oo" in Roman)
    r3 = difflib.SequenceMatcher(None, a.replace("w", ""), b.replace("w", "")).ratio()
    return max(r1, r2, r3)


# ---------------------------------------------------------------- alignment
def align_words(ur: list[str], ro: list[str]) -> list[tuple[str, str]]:
    """Monotonic DP alignment; allows 1-1, 1-2, 2-1 and skips."""
    n, m = len(ur), len(ro)
    if n == 0 or m == 0:
        return []
    GAP = -0.55
    sim_cache: dict[tuple[int, int], float] = {}

    def sim(i: int, j: int) -> float:
        if (i, j) not in sim_cache:
            sim_cache[(i, j)] = similarity(ur[i], ro[j])
        return sim_cache[(i, j)]

    def sim2(urdu_words: list[str], roman_words: list[str]) -> float:
        return similarity("".join(urdu_words), "".join(roman_words))

    NEG = float("-inf")
    dp = [[NEG] * (m + 1) for _ in range(n + 1)]
    bk = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == NEG:
                continue
            moves = []
            if i < n and j < m:
                moves.append((i + 1, j + 1, dp[i][j] + 2 * sim(i, j) - 0.25, (1, 1)))
            if i < n and j + 1 < m:
                moves.append((i + 1, j + 2, dp[i][j] + 2 * sim2([ur[i]], [ro[j], ro[j + 1]]) - 0.45, (1, 2)))
            if i + 1 < n and j < m:
                moves.append((i + 2, j + 1, dp[i][j] + 2 * sim2([ur[i], ur[i + 1]], [ro[j]]) - 0.45, (2, 1)))
            if i < n:
                moves.append((i + 1, j, dp[i][j] + GAP, (1, 0)))
            if j < m:
                moves.append((i, j + 1, dp[i][j] + GAP, (0, 1)))
            for ni, nj, sc, mv in moves:
                if sc > dp[ni][nj]:
                    dp[ni][nj] = sc
                    bk[ni][nj] = (i, j, mv)
    pairs: list[tuple[str, str]] = []
    i, j = n, m
    while (i, j) != (0, 0):
        prev = bk[i][j]
        if prev is None:
            break
        pi, pj, (di, dj) = prev
        if di and dj:
            pairs.append((" ".join(ur[pi:i]), " ".join(ro[pj:j])))
        i, j = pi, pj
    return list(reversed(pairs))


def split_sentences(text: str, pattern: str) -> list[str]:
    return [s.strip() for s in re.split(pattern, text) if s.strip()]


def learn_votes(pairs_text: list[tuple[str, str]]) -> tuple[Counter, Counter]:
    votes: Counter = Counter()
    cap_votes: Counter = Counter()  # (urdu, roman_lower) seen capitalised mid-sentence
    for urdu_text, roman_text in pairs_text:
        ur_sents = split_sentences(urdu_text, r"[۔؟!]")
        ro_sents = split_sentences(roman_text, r"[.?!]")
        chunks = list(zip(ur_sents, ro_sents)) if len(ur_sents) == len(ro_sents) and len(ur_sents) > 1 \
            else [(urdu_text, roman_text)]
        for ur_s, ro_s in chunks:
            ur_w = [norm_urdu_word(w) for w in words_only(ur_s)]
            ro_w_raw = words_only(ro_s)
            ro_w = [w.lower() for w in ro_w_raw]
            for u, r in align_words(ur_w, ro_w):
                if u and r:
                    votes[(u, r)] += 1
            # capitalised (proper-noun) evidence: non-initial capitalised words
            for w in ro_w_raw[1:]:
                if w[:1].isupper():
                    cap_votes[w.lower()] += 1
    return votes, cap_votes


def build_dictionary(existing: dict, holdout: set[int] | None = None) -> dict:
    train = [(v["urdu"], v["roman"]) for k, v in existing.items()
             if v.get("urdu") and v.get("roman") and int(k) not in (holdout or set())]
    votes, cap_votes = learn_votes(train)
    by_urdu: dict[str, Counter] = defaultdict(Counter)
    for (u, r), c in votes.items():
        by_urdu[u][r] += c
    dictionary: dict[str, str] = {}
    stats = {"accepted": 0, "rejected_low_evidence": 0}
    for u, cand in by_urdu.items():
        (best, best_c), = cand.most_common(1)
        total = sum(cand.values())
        ok = best_c >= 2 or best_c / total >= 0.6 and similarity(u.split()[0], best.split()[0]) >= 0.4 \
            or similarity(u.replace(" ", ""), best.replace(" ", "")) >= 0.62
        if ok:
            dictionary[u] = best
            stats["accepted"] += 1
        else:
            stats["rejected_low_evidence"] += 1
    # manual overrides always win
    overrides = {}
    if OVERRIDES_PATH.exists():
        overrides = {norm_urdu_word(k): v for k, v in
                     json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")).items()
                     if not k.startswith("_")}
        dictionary.update(overrides)
    proper = sorted(w for w, c in cap_votes.items() if c >= 2)
    return {"dictionary": dictionary, "proper_nouns": proper,
            "overrides_applied": len(overrides), "stats": stats,
            "train_voices": len(train)}


# ---------------------------------------------------------------- fallback
_FULL = {  # fuller phonetic map used only for out-of-vocabulary words
    "ا": "a", "آ": "aa", "ب": "b", "پ": "p", "ت": "t", "ٹ": "t", "ث": "s",
    "ج": "j", "چ": "ch", "ح": "h", "خ": "kh", "د": "d", "ڈ": "d", "ذ": "z",
    "ر": "r", "ڑ": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q",
    "ک": "k", "گ": "g", "ل": "l", "م": "m", "ن": "n", "ں": "n", "ہ": "h",
    "ھ": "h", "ے": "e", "ء": "'",
}
_VOWELISH = set("اآویےع")


def fallback_word(w: str) -> str:
    w = norm_urdu_word(w)
    if not w:
        return ""
    if re.fullmatch(r"[A-Za-z0-9'-]+", w):  # already Latin (English word)
        return w
    out: list[str] = []
    for i, c in enumerate(w):
        if c == "و":
            out.append("w" if i == 0 else "o")
        elif c == "ی":
            out.append("y" if i == 0 else "i")
        elif c == "ئ":
            out.append("i")
        elif c in _FULL:
            # insert a default short vowel between two bare consonants
            if out and w[i - 1] not in _VOWELISH and not out[-1].endswith(("a", "i", "o", "e")) \
                    and c != "ھ":
                out.append("a")
            out.append(_FULL[c])
        else:
            out.append(c)
    s = "".join(out)
    return s.replace("aa", "a") if s.startswith("aa") and len(s) > 3 else s


# ---------------------------------------------------------------- apply
class Transliterator:
    def __init__(self, dict_data: dict):
        self.dictionary: dict[str, str] = dict_data["dictionary"]
        self.proper = set(dict_data.get("proper_nouns", []))

    @classmethod
    def load(cls, path: Path = DICT_PATH) -> "Transliterator":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def transliterate(self, urdu_text: str) -> tuple[str, dict]:
        tokens = tokenize(urdu_text.translate(URDU_DIGITS))
        out: list[str] = []
        oov: Counter = Counter()
        total_words = 0
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if not re.search(r"\w", tok):  # punctuation
                out.append(PUNCT_MAP.get(tok, tok))
                i += 1
                continue
            total_words += 1
            # greedy two-word dictionary match
            if i + 1 < len(tokens) and re.search(r"\w", tokens[i + 1]):
                key2 = f"{norm_urdu_word(tok)} {norm_urdu_word(tokens[i + 1])}"
                if key2 in self.dictionary:
                    out.append(self.dictionary[key2])
                    total_words += 1
                    i += 2
                    continue
            key = norm_urdu_word(tok)
            if key in self.dictionary:
                out.append(self.dictionary[key])
            elif re.fullmatch(r"[A-Za-z][A-Za-z0-9'-]*", tok) or tok.isdigit():
                out.append(tok)  # English word / number spoken as-is: keep it
            else:
                oov[key] += 1
                out.append(fallback_word(tok))
            i += 1
        text = _post_process(" ".join(out), self.proper)
        info = {"words": total_words, "oov_words": sum(oov.values()),
                "oov_rate": round(sum(oov.values()) / total_words, 4) if total_words else 0.0,
                "oov_top": oov.most_common(40)}
        return text, info


def _post_process(text: str, proper: set[str]) -> str:
    text = re.sub(r"\s+([.,?!;:%])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    # proper nouns learned from the document keep their capital
    def fix_proper(m: re.Match) -> str:
        w = m.group(0)
        return w[:1].upper() + w[1:] if w.lower() in proper else w
    text = re.sub(r"[A-Za-z][A-Za-z'-]*", fix_proper, text)
    # capitalise sentence starts, matching the document's style
    text = re.sub(r"(^|[.?!]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text


# ---------------------------------------------------------------- CLI
def evaluate(transliterator: Transliterator, existing: dict, holdout: set[int]) -> None:
    print(f"\n=== Hold-out evaluation (voices never used to learn the dictionary) ===")
    tot_eq = tot_ref = 0
    for n in sorted(holdout):
        e = existing.get(str(n))
        if not e or not e.get("roman"):
            continue
        pred, info = transliterator.transliterate(e["urdu"])
        ref_w = [w.lower().strip(".,?!;:") for w in e["roman"].split()]
        pred_w = [w.lower().strip(".,?!;:") for w in pred.split()]
        sm = difflib.SequenceMatcher(None, ref_w, pred_w)
        eq = sum(b.size for b in sm.get_matching_blocks())
        tot_eq += eq
        tot_ref += len(ref_w)
        print(f"Voice {n}: word-match {eq}/{len(ref_w)} ({100*eq/max(len(ref_w),1):.0f}%)  OOV {info['oov_rate']*100:.0f}%")
        print(f"  ref : {e['roman'][:160]}")
        print(f"  pred: {pred[:160]}")
    if tot_ref:
        print(f"OVERALL hold-out word match: {tot_eq}/{tot_ref} = {100*tot_eq/tot_ref:.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--holdout", default="141-162,166,167",
                   help="Voices excluded from learning for evaluation; 'none' to learn from all")
    b.add_argument("--save-holdout-model", action="store_true",
                   help="Save the hold-out dictionary instead of the full one (not recommended)")
    a = sub.add_parser("apply")
    a.add_argument("text")
    args = ap.parse_args()

    if args.cmd == "apply":
        t = Transliterator.load()
        text, info = t.transliterate(args.text)
        print(text)
        print(f"-- OOV rate: {info['oov_rate']*100:.1f}%  OOV: {info['oov_top']}", file=sys.stderr)
        return 0

    existing = json.loads(EXISTING_PATH.read_text(encoding="utf-8"))
    holdout: set[int] = set()
    if args.holdout != "none":
        for part in args.holdout.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-")
                holdout.update(range(int(lo), int(hi) + 1))
            elif part:
                holdout.add(int(part))

    if holdout:
        eval_model = Transliterator(build_dictionary(existing, holdout))
        evaluate(eval_model, existing, holdout)
        if args.save_holdout_model:
            data = build_dictionary(existing, holdout)
            DICT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"\nSaved HOLD-OUT dictionary ({len(data['dictionary'])} entries) -> {DICT_PATH}")
            return 0
    data = build_dictionary(existing, set())
    DICT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSaved FULL dictionary: {len(data['dictionary'])} entries, "
          f"{len(data['proper_nouns'])} proper nouns, overrides={data['overrides_applied']}, "
          f"learned from {data['train_voices']} voices -> {DICT_PATH}")
    # spot-check the spellings called out in the task
    t = Transliterator(data)
    sample, _ = t.transliterate("مجھے نہیں پتا کہ آپ کیا کہہ رہے ہیں، یہ کیوں ہے؟")
    print(f"Spot-check: {sample}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
