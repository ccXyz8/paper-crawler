from __future__ import annotations

import re
import unicodedata

from .models import Paper


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(?<=\w)[\-‐‑‒–—]\s*(?=\w)", "", text)
    return " ".join(text.split())


def keyword_matches(paper: Paper, keywords: list[str | list[str]], mode: str = "any",
                    exclude: list[str] | None = None, word_boundary: bool = True) -> tuple[bool, list[dict]]:
    if mode not in {"any", "all"}:
        raise ValueError("keyword_mode must be any or all")
    fields = {"title": _normalize(paper.title), "abstract": _normalize(paper.abstract)}

    def hits(term: str) -> list[dict]:
        normalized = _normalize(term)
        if not normalized:
            return []
        pattern = re.escape(normalized)
        if word_boundary:
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
        found = []
        for field, text in fields.items():
            match = re.search(pattern, text)
            if match:
                found.append({"keyword": term, "field": field,
                              "snippet": text[max(0, match.start()-60):match.end()+60]})
        return found

    if any(hits(term) for term in exclude or []):
        return False, []
    evidence, results = [], []
    for group in keywords:
        terms = [group] if isinstance(group, str) else group
        group_hits = [hit for term in terms for hit in hits(term)]
        evidence.extend(group_hits)
        results.append(bool(group_hits))
    return (not keywords or (all(results) if mode == "all" else any(results))), evidence


def matches_keywords(paper: Paper, keywords: list[str | list[str]], mode: str = "any") -> bool:
    return keyword_matches(paper, keywords, mode)[0]
