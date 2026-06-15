from __future__ import annotations

import re
import unicodedata
from typing import Any

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_SPACE_RE = re.compile(r"[ \t]+")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")

_TRANSLATION_TABLE = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2013": '-',
    "\u2014": '-',
    "\u2212": '-',
    "\u00a0": ' ',
})


def detect_language_bucket(text: str) -> str:
    has_cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
    has_latin = bool(re.search(r"[A-Za-z]", text))
    if has_cyrillic and has_latin:
        return 'mixed'
    if has_cyrillic:
        return 'cyrillic'
    if has_latin:
        return 'latin'
    return 'unknown'


def normalize_text(value: Any, *, max_chars: int = 6000) -> str:
    text = str(value or '')
    text = unicodedata.normalize('NFKC', text)
    text = text.translate(_TRANSLATION_TABLE)
    text = text.replace('\r\n', '\n').replace('\r', '\n').replace('\t', ' ')
    text = _ZERO_WIDTH_RE.sub('', text)
    text = _CONTROL_RE.sub('', text)
    lines = []
    for line in text.split('\n'):
        compact = _MULTI_SPACE_RE.sub(' ', line).strip(' ')
        lines.append(compact)
    text = '\n'.join(lines).strip()
    text = _MULTI_BLANK_RE.sub('\n\n', text)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + '...'
    return text


def normalize_text_list(value: Any, *, max_items: int = 64, item_max_chars: int = 400) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        normalized = normalize_text(value, max_chars=max_items * item_max_chars)
        raw_items = normalized.replace('\r', '\n').split('\n')
    items: list[str] = []
    for raw in raw_items:
        cleaned = normalize_text(raw, max_chars=item_max_chars)
        if cleaned:
            items.append(cleaned)
        if len(items) >= max_items:
            break
    return items
