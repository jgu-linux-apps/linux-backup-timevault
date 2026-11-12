"""Dwa języki: polski i angielski.

Użycie: L("tekst po polsku", "English text") — zwraca wersję w bieżącym języku.
Język: ustawienie użytkownika (config "language": auto|pl|en), a przy "auto" ustawienia
systemu (LANGUAGE / LC_ALL / LC_MESSAGES / LANG): polski dla pl_*, w pozostałych angielski.
"""
from __future__ import annotations

import os

_lang: str | None = None


def system_lang() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var, "")
        if v and v not in ("C", "C.UTF-8", "POSIX"):
            return "pl" if v.lower().startswith("pl") else "en"
    return "en"


def set_lang(lang: str | None):
    """lang: 'pl', 'en' albo 'auto'/None (z systemu)."""
    global _lang
    _lang = lang if lang in ("pl", "en") else system_lang()


def lang() -> str:
    if _lang is None:
        set_lang(os.environ.get("TIMEVAULT_LANG"))
    return _lang


def L(pl: str, en: str) -> str:
    return pl if lang() == "pl" else en
