import re
from typing import Optional, Any, List

def strip_nulls(value: Any) -> Any:
    """
    Recursively remove keys with value None from dictionaries.

    Lists are preserved, but their elements are also cleaned.
    """
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value

def match_text(value: str, keyword: str, match_type: Optional[str]) -> bool:
    """
    Apply WinGet-style text matching rules to a single value.
    """
    if keyword is None:
        return False
    keyword = keyword or ""
    match = (match_type or "Substring").strip() or "Substring"

    # Exact is case-sensitive; everything else we treat as case-insensitive.
    if match == "Exact":
        return value == keyword

    v = value.lower()
    k = keyword.lower()

    if match in ("CaseInsensitive",):
        return v == k
    if match == "StartsWith":
        return v.startswith(k)
    if match in ("Substring", "Fuzzy", "FuzzySubstring"):
        return k in v
    if match == "Wildcard":
        # Very simple wildcard support: * and ?
        pattern = "^" + re.escape(keyword).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return re.search(pattern, value, flags=re.IGNORECASE) is not None

    # Fallback: case-insensitive substring
    return k in v

