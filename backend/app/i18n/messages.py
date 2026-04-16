"""
app/i18n/messages.py — i18n message catalog for MBB ya Kin.

Loads JSON template files at import time and provides a single
`t(key, language)` function to retrieve localized strings.

Usage:
    from app.i18n.messages import t
    msg = t("opt_out_ack", "lingala")
"""
from __future__ import annotations

import json
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_SUPPORTED_LANGUAGES = ("french", "lingala", "swahili")
_DEFAULT_LANGUAGE = "french"

# Load all templates at import time (fast — 3 small JSON files).
_catalog: dict[str, dict[str, str]] = {}
for lang in _SUPPORTED_LANGUAGES:
    path = _TEMPLATE_DIR / f"{lang}.json"
    if path.exists():
        _catalog[lang] = json.loads(path.read_text(encoding="utf-8"))
    else:
        _catalog[lang] = {}


def t(key: str, language: str = _DEFAULT_LANGUAGE) -> str:
    """
    Translate a message key into the target language.

    Falls back to French if the key is missing in the target language,
    then returns the raw key if missing entirely.
    """
    lang = language if language in _catalog else _DEFAULT_LANGUAGE
    msg = _catalog.get(lang, {}).get(key)
    if msg is not None:
        return msg
    # Fallback to French
    msg = _catalog.get(_DEFAULT_LANGUAGE, {}).get(key)
    if msg is not None:
        return msg
    return key
