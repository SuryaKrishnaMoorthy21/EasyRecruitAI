"""
Whole-word term matching for the scoring heuristics (Task 6).

The India-aware detectors used to test `term in text_lower`, a bare substring test. That made short tokens
fire inside unrelated words: "nit " inside "unit tests", "mica" inside "chemical", "iit" inside "HIIT", "cred"
inside "credit", "cka" (a Kubernetes certification) inside "package", "vp" inside "VPN", "head of" inside
"ahead of schedule", and even the first names "Sairam" / "Sriram". Each false hit silently added score points.

A term now matches only as a whole word: it may not be glued to a letter or digit on either side. Spaces inside a
multi-word term match any run of whitespace. Matching is case-insensitive.
"""
import re
from functools import lru_cache
from typing import Iterable, List


@lru_cache(maxsize=4096)
def term_pattern(term: str, plural: bool = False) -> "re.Pattern":
    body = r"\s+".join(re.escape(part) for part in term.strip().split())
    if plural:
        body += r"(?:'?s)?"            # bachelor / bachelors / bachelor's
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])", re.IGNORECASE)


def has_term(text: str, term: str, *, plural: bool = False) -> bool:
    return term_pattern(term, plural).search(text) is not None


def find_terms(text: str, terms: Iterable[str], *, plural: bool = False) -> List[str]:
    return [t for t in terms if has_term(text, t, plural=plural)]
