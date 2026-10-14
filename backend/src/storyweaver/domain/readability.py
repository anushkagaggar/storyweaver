"""Flesch-Kincaid grade level, implemented directly to avoid a heavy NLP dependency.

FK grade = 0.39 * (words / sentences) + 11.8 * (syllables / words) - 15.59.
Syllables use a vowel-group heuristic (common in readability tools). It is approximate, which is acceptable
for a threshold check on a whole story; it is not meant to match any specific library exactly.
"""

import re

_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_END = re.compile(r"[.!?]+(?:[\"')\]]*)(?:\s+|$)")
_VOWEL_GROUPS = re.compile(r"[aeiouy]+")


def count_syllables(word: str) -> int:
    word = word.lower().replace("'", "")
    if len(word) <= 3:
        return 1
    if word.endswith("e") and not word.endswith(("le", "ee", "ye")):
        word = word[:-1]
    if word.endswith(("es", "ed")) and not word.endswith(("ted", "ded", "ses", "zes", "ches", "shes")):
        word = word[:-2]
    return max(1, len(_VOWEL_GROUPS.findall(word)))


def flesch_kincaid_grade(text: str) -> float:
    words = _WORD.findall(text)
    if not words:
        return 0.0
    sentences = max(1, len(_SENTENCE_END.findall(text.strip())))
    syllables = sum(count_syllables(word) for word in words)
    return 0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59
