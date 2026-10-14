"""Stage 1 of the input checks (plan §6.6): fast, free, deterministic rules.

Deliberately narrow: only patterns that are never acceptable in a 4-6 story idea. Anything subtler is left to
the policy model (gpt-oss-safeguard), so ordinary words in kind stories ("the leaves fell", "a sharp pencil")
are not blocked here. Word boundaries keep "killer whale", "heroine" and "shooting star" allowed.
"""

import re
from dataclasses import dataclass

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|net|org|io|co|uk|in|app|xyz|info)\b")
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 7+ digits, optionally separated by spaces, dots or dashes: phone numbers and long IDs.
_LONG_NUMBER = re.compile(r"(?<!\w)(?:\+?\d[\s.-]?){7,}\d(?!\w)")
_BLOCKED_TERMS = re.compile(
    r"(?i)\b(?:kill(?:s|ed|ing)?|murder\w*|suicide|stab(?:s|bed|bing)?|"
    r"behead\w*|torture\w*|rape\w*|sex(?:y|ual)?|nude|naked|porn\w*|cocaine|heroin)\b"
)


@dataclass(frozen=True)
class RuleMatch:
    category: str
    reason: str


def check_rules(text: str) -> RuleMatch | None:
    """Return the first rule the text breaks, or None."""
    # Emails first: their domain part would otherwise match the URL rule.
    if _EMAIL.search(text):
        return RuleMatch("personal_data", "Email addresses can't be used in a story idea.")
    if _URL.search(text):
        return RuleMatch("urls", "Links and web addresses can't be used in a story idea.")
    if _LONG_NUMBER.search(text):
        return RuleMatch("personal_data", "Phone numbers and long numbers can't be used in a story idea.")
    if _BLOCKED_TERMS.search(text):
        return RuleMatch("blocked_term", "This idea includes words that aren't suitable for young children.")
    return None
