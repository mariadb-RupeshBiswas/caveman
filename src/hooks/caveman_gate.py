#!/usr/bin/env python3
"""Stop hook: lint last assistant reply for caveman-ultra prose violations.

Runs only in Claude Code cloud sessions (CLAUDE_CODE_REMOTE=true), where no
personal settings.json gate exists. CAVEMAN_GATE=1 forces it on anywhere,
CAVEMAN_GATE=0 turns it off.
"""
import json
import os
import re
import string
import sys
from collections import deque
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
CAVEMAN_FILE = CLAUDE_DIR / ".caveman-active"
VALID_LEVELS = {"lite", "full", "ultra"}
MAX_LINES = 6
MAX_SENTENCES_PER_PARA = 2
MAX_WORDS_PER_SENTENCE = 15
MAX_SCAN_LINES = 200


def gate_enabled():
    # a local machine keeps its own settings.json gate; running both double-blocks
    forced = os.environ.get("CAVEMAN_GATE")
    if forced in {"0", "1"}:
        return forced == "1"
    return os.environ.get("CLAUDE_CODE_REMOTE") == "true"


def strip_exempt(text):
    """Remove fenced code blocks and table lines, keep blank-line separators."""
    lines = text.split("\n")
    kept = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if in_fence:
            if stripped.startswith("```"):
                in_fence = False
            continue
        if stripped.startswith("```"):
            in_fence = True
            continue
        kept.append(line)
    # table_re: pipe or plus at line start (ascii box tables / md tables)
    table_re = re.compile(r"^\s*[|+]")
    kept = [line for line in kept if not table_re.match(line)]
    return "\n".join(kept)


# list item: -, *, +, or "1." marker, optional [x]/[ ] task marker after
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s+)?")
HEADER_LINE_RE = re.compile(r"^\s*#")

# task checkbox item only (- [x] / - [ ]), not plain bullets: exempt from line-count check
TASK_ITEM_RE = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s*")


def get_paragraphs(text):
    """Split on blank lines, then split each block so every list-item/header line starts a new paragraph."""
    blocks = re.split(r"\n\s*\n+", text)
    paragraphs = []
    for block in blocks:
        if block.strip() == "":
            continue
        current = []
        for line in block.split("\n"):
            if LIST_ITEM_RE.match(line) or HEADER_LINE_RE.match(line):
                if current:
                    paragraphs.append("\n".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            paragraphs.append("\n".join(current))
    return [p for p in paragraphs if p.strip() != ""]


# split on .!? then whitespace/end; skip "..." ellipsis and a decimal wrapped across a line break (".\n5")
SENTENCE_END_RE = re.compile(r"(?<!\.\.)[.!?](?=\s|$)(?!\n\d)")


def get_sentences(paragraph):
    # drop leading list marker first: "1." marker period is not a sentence end
    paragraph = LIST_ITEM_RE.sub("", paragraph, count=1)
    parts = SENTENCE_END_RE.split(paragraph)
    return [p.strip() for p in parts if p.strip() != ""]


def count_words(sentence):
    # strip leading list marker: -, *, +, or "1."
    cleaned = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", sentence)
    tokens = cleaned.split()
    words = [t for t in tokens if not all(c in string.punctuation for c in t)]
    return len(words)


def truncate(sentence, n=40):
    if len(sentence) <= n:
        return sentence
    return sentence[:n] + " ..."


def resolve_level():
    """Read caveman level. Missing/empty/unreadable/unknown -> ultra (fail toward enforcement)."""
    try:
        raw = CAVEMAN_FILE.read_text(encoding="utf-8")
    except OSError:
        return "ultra"
    level = raw.strip()
    if level in VALID_LEVELS:
        return level
    if level.lower() in {"off", "none", "stop", "disabled"}:
        return None
    return "ultra"


def find_last_assistant_text(transcript_path):
    """Tail last MAX_SCAN_LINES lines, walk backwards for first non-blank assistant text."""
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            tail = deque(f, maxlen=MAX_SCAN_LINES)
    except OSError:
        return None
    for line in reversed(tail):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        # A user entry newer than the newest assistant text means that text is NOT the
        # reply that just finished: the new one is not flushed yet. Linting the old one
        # blocks a reply that was never over the limit, so bail instead.
        if obj.get("type") == "user":
            return None
        if obj.get("type") != "assistant":
            continue
        content = obj.get("message", {}).get("content", [])
        texts = [item.get("text", "") for item in content if item.get("type") == "text"]
        combined = "".join(texts)
        if combined.strip() != "":
            return combined
    return None


def main():
    if not gate_enabled():
        sys.exit(0)

    raw = sys.stdin.read()
    hook_input = json.loads(raw)

    # Already blocked once this cycle. Exiting 2 again can loop, so warn instead
    # of blocking -- but still lint. Bailing outright left every reply after the
    # first block unchecked, which is how a 14-line reply shipped on 2026-08-21.
    warn_only = bool(hook_input.get("stop_hook_active"))

    level = resolve_level()
    if level is None:
        sys.exit(0)

    # transcript flush lags the Stop event, so prefer the reply text the hook is handed.
    text = hook_input.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        text = find_last_assistant_text(hook_input.get("transcript_path", ""))
    if text is None:
        sys.exit(0)

    stripped = strip_exempt(text)

    # check 1: prose line count. task-checklist lines exempt, same as tables/code.
    prose_lines = [l for l in stripped.split("\n") if l.strip() != "" and not TASK_ITEM_RE.match(l)]
    line_count = len(prose_lines)
    line_violation = line_count > MAX_LINES

    # check 2 + 3: sentences per paragraph, words per sentence
    paragraphs = get_paragraphs(stripped)
    para_violations = []  # (index, sentence_count)
    sentence_violations = []  # (sentence_text, word_count)
    for idx, para in enumerate(paragraphs, start=1):
        sentences = get_sentences(para)
        if len(sentences) > MAX_SENTENCES_PER_PARA:
            para_violations.append((idx, len(sentences)))
        for sentence in sentences:
            wc = count_words(sentence)
            if wc > MAX_WORDS_PER_SENTENCE:
                sentence_violations.append((sentence, wc))

    if not line_violation and not para_violations and not sentence_violations:
        sys.exit(0)

    header = "CAVEMAN GATE WARNING" if warn_only else "CAVEMAN GATE BLOCKED"
    msg_lines = [f"{header} (level={level})"]
    if line_violation:
        msg_lines.append(f"- prose lines: {line_count} (max {MAX_LINES})")

    shown_paras = para_violations[:3]
    for idx, cnt in shown_paras:
        msg_lines.append(f"- paragraph {idx} has {cnt} sentences (max {MAX_SENTENCES_PER_PARA})")
    extra_paras = len(para_violations) - len(shown_paras)
    if extra_paras > 0:
        msg_lines.append(f"- (and {extra_paras} more)")

    shown_sentences = sentence_violations[:3]
    for sentence, wc in shown_sentences:
        msg_lines.append(f'- sentence "{truncate(sentence)}" has {wc} words (max {MAX_WORDS_PER_SENTENCE})')
    extra_sentences = len(sentence_violations) - len(shown_sentences)
    if extra_sentences > 0:
        msg_lines.append(f"- (and {extra_sentences} more)")

    msg_lines.append(
        "Rewrite the reply shorter. Drop articles, filler, hedging. Fragments OK. "
        "Tables and code blocks are exempt and do not count."
    )
    if warn_only:
        msg_lines.append(
            "Not blocking: already blocked once this cycle. Keep the next reply inside the limits."
        )
    sys.stderr.write("\n".join(msg_lines) + "\n")
    sys.exit(0 if warn_only else 2)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        # visible, non-blocking failure: exit 1 surfaces to user, does not block turn
        sys.stderr.write(f"CAVEMAN GATE ERROR: {type(e).__name__}: {e}\n")
        sys.exit(1)
