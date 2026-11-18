"""Company-name cleaning shared by fetch_jobs.py (step 1) and build_accounts.py (step 3).

Form D gives legal names ("BottomHalf Labs, Inc."), while TheirStack uses the name a
company goes by ("BottomHalf"). Step 1 sends a few spellings of each Form D name to
TheirStack's exact-name filter; step 3 joins jobs to filings on the same spellings.
"""

from __future__ import annotations

import re

# Legal-form words dropped from the end of a name: "Acme, Inc." -> "Acme".
LEGAL_SUFFIXES = {"inc", "incorporated", "llc", "corp", "corporation", "co", "ltd", "limited",
                  "pbc", "lp", "llp", "plc"}
# A name ending in one of these is also tried without it: "Harvey AI" -> "Harvey".
GENERIC_LAST_WORDS = {"ai", "labs", "lab", "technologies", "technology", "systems", "software",
                      "robotics", "intelligence", "platform", "platforms"}
# Shorter short forms ("Eco" from "Eco Labs") are too likely to be another company's name.
MIN_SHORT_LETTERS = 4


def clean_name(name: str) -> str:
    """Join key: lowercase, "&" read as "and", dotted initials joined ("L.P." -> "lp"),
    other punctuation removed, legal suffixes dropped from the end."""
    text = str(name).lower().replace("&", " and ")
    text = re.sub(r"(?<=\b[a-z])\.", "", text)  # "l.l.c." -> "llc", but "idox.ai" stays
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def strip_legal_suffix(name: str) -> str:
    """The name as filed, minus trailing legal-form words: "iDox.ai Corp." -> "iDox.ai"."""
    words = str(name).split()
    while len(words) > 1 and clean_name(words[-1]) in LEGAL_SUFFIXES | {""}:
        words.pop()
    return " ".join(words).rstrip(" ,")


def short_name(name: str) -> str | None:
    """The name without a generic last word or a ".ai" ending, or None if there is none
    or what is left is too short: "BottomHalf Labs" -> "BottomHalf", "iDox.ai" -> "iDox"."""
    if name.lower().endswith(".ai"):
        rest = name[:-3]
    else:
        words = name.split()
        if len(words) < 2 or clean_name(words[-1]) not in GENERIC_LAST_WORDS:
            return None
        rest = " ".join(words[:-1])
    rest = rest.rstrip(" ,.-")
    return rest if len(clean_name(rest).replace(" ", "")) >= MIN_SHORT_LETTERS else None


def search_names(legal_name: str) -> list[str]:
    """Spellings of one Form D name for TheirStack's exact-name filter, the full name first:
    the name without its legal suffix, each half of a two-part name ("fal - Features &
    Labels" -> "fal"), and short forms. Case-insensitive duplicates are dropped."""
    full = strip_legal_suffix(legal_name)
    parts = [strip_legal_suffix(part) for part in full.split(" - ")]
    spellings = [full] + (parts if len(parts) > 1 else [])
    for name in list(spellings):
        short = short_name(name) if " - " not in name else None  # two-part names: shorten each part
        if short:
            spellings.append(short)
    unique: dict[str, str] = {}
    for spelling in spellings:
        if spelling.strip() and spelling.lower() not in unique:
            unique[spelling.lower()] = spelling
    return list(unique.values())
