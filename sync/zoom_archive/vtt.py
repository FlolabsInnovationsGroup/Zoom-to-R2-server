"""Zoom WebVTT -> plain transcript lines "[HH:MM:SS] Speaker: text" (one per cue, each ending with a newline).

Cue ids are dropped, start times floored to whole seconds, multi-line cue text joined with one space,
<v Name>text voice tags become "Name: text". Matches contracts/fixtures/transcript.expected.txt.
"""
import re

TIMING_RE = re.compile(r"^((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\s+-->\s+")
VOICE_RE = re.compile(r"^<v(?:\.[^\s>]+)?\s+([^>]+)>")


def _seconds(stamp: str) -> int:
    total = 0
    for p in re.split(r"[.,]", stamp)[0].split(":"):
        total = total * 60 + (int(p) if p.isdigit() else 0)
    return total


def format_timestamp(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _decode(text: str) -> str:
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&lrm;", ""), ("&rlm;", ""), ("&amp;", "&")):
        text = text.replace(a, b)
    return text


def _cue_text(lines: list[str]) -> str:
    speaker, parts = None, []
    for line in lines:
        v = VOICE_RE.match(line)
        if v and speaker is None:
            speaker = v.group(1).strip()
        line = _decode(re.sub(r"<[^>]*>", "", line)).strip()
        if line:
            parts.append(line)
    text = " ".join(parts)
    return f"{speaker}: {text}" if speaker else text


def parse_vtt(vtt: str) -> str:
    text = vtt.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for block in re.split(r"\n{2,}", text):
        lines = block.split("\n")
        idx = next((i for i, l in enumerate(lines) if TIMING_RE.match(l.strip())), -1)
        if idx == -1:
            continue
        body = _cue_text(lines[idx + 1:])
        if body:
            out.append(f"[{format_timestamp(_seconds(TIMING_RE.match(lines[idx].strip()).group(1)))}] {body}\n")
    return "".join(out)
