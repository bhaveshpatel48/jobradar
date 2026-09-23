"""Salary extraction and normalisation.

Two sources: structured compensation fields some ATS expose, and pay ranges
written into descriptions (common under US pay-transparency laws). Parsing is
deliberately strict -- only an explicit currency-marked *range* with plausible
values is accepted, so "$5M-$10M raised" or "401(k)" never read as pay.
"""

import re

_SYMBOL_CURRENCY = {"$": "USD", "US$": "USD", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "€": "EUR", "£": "GBP", "₹": "INR"}
_CODES = ("USD", "EUR", "GBP", "CAD", "AUD", "INR", "CHF", "SEK", "NOK", "DKK", "PLN")

_CUR = r"(?:US\$|CA\$|C\$|A\$|\$|€|£|₹|" + "|".join(_CODES) + r")"
_NUM = r"\d{1,3}(?:[,.  ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CODE_ALT = "|".join(_CODES)
_DASH = r"\s*(?:-|–|—|to)\s*"
# "$150K – $200K USD", "€ 77k-103k", "CA$120,000 to CA$150,000"
_RANGE = re.compile(
    rf"(?P<c1>{_CUR})\s?(?P<a>{_NUM})\s?(?P<ka>[kK])?{_DASH}"
    rf"(?P<c2>{_CUR})?\s?(?P<b>{_NUM})\s?(?P<kb>[kK])?"
    rf"(?:\s*(?P<c3>{_CODE_ALT})\b)?",
)
# Currency code after the numbers: "248,000 USD - 396,750 USD", "180K - 220K USD"
_RANGE_SUFFIX = re.compile(
    rf"(?<![\w$€£₹.,])(?P<a>{_NUM})\s?(?P<ka>[kK])?(?:\s*(?P<c1>{_CODE_ALT}))?{_DASH}"
    rf"(?P<b>{_NUM})\s?(?P<kb>[kK])?\s*(?P<c3>{_CODE_ALT})\b",
)
# A range counts only with salary wording before it or a pay period after it,
# so "$200k to $500k contract values" and funding figures are ignored.
_CONTEXT = re.compile(r"salary|compensation|\bpay\b|base|wage|\bote\b|rate|earn", re.I)
_POST = re.compile(
    r"^\W{0,4}(?:[A-Z]{3}\s*)?(?:/\s*|per\s+|a\s+|an\s+)?"
    r"(?:year|yr|annum|annually|hour|hr|hourly|month|monthly|maand|gross)",
    re.I,
)
_HOURLY = re.compile(r"(?:/|per\s+|an\s+)(?:hour|hr|h)\b|hourly", re.I)
_MONTHLY = re.compile(r"(?:/|per\s+|a\s+)(?:month|mo|maand)\b|monthly", re.I)

# (min, max) plausibility bounds per period.
_BOUNDS = {"year": (10_000, 2_000_000), "month": (500, 150_000), "hour": (7, 1_000)}


def _to_number(raw: str, k: str | None) -> float:
    s = raw.replace(" ", "").replace(" ", "")
    # "60.000" / "150,000" are thousands separators; "1.5" (with k) is a decimal.
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    else:
        s = s.replace(",", "")
    value = float(s)
    return value * 1000 if k else value


def _currency(*tokens: str | None) -> str | None:
    for token in tokens:
        if token:
            return _SYMBOL_CURRENCY.get(token, token.upper())
    return None


def _period(context: str, lo: float) -> str:
    if _HOURLY.search(context):
        return "hour"
    if _MONTHLY.search(context):
        return "month"
    return "year" if lo >= 1000 else "hour"


def parse_salary_text(text: str | None) -> dict | None:
    """Find a pay range in free text. Returns a salary dict or None.

    Prefers a range near salary wording ("Annual Salary: $320,000—$405,000 USD")
    over the first range anywhere in the text.
    """
    if not text:
        return None
    matches = sorted(
        [*_RANGE.finditer(text), *_RANGE_SUFFIX.finditer(text)], key=lambda m: m.start()
    )
    for match in matches:
        try:
            lo = _to_number(match["a"], match["ka"] or match["kb"])
            hi = _to_number(match["b"], match["kb"])
        except ValueError:
            continue
        if lo > hi:
            continue
        before = text[max(0, match.start() - 80) : match.start()]
        after = text[match.end() : match.end() + 30]
        if not (_CONTEXT.search(before) or _POST.search(after)):
            continue
        # The period word may precede the range ("Gross monthly salary ranging from").
        period = _period(after + " " + before, lo)
        low_bound, high_bound = _BOUNDS[period]
        if not (low_bound <= lo and hi <= high_bound):
            continue
        groups = match.groupdict()
        return {
            "salary_min": lo,
            "salary_max": hi,
            "salary_currency": _currency(groups.get("c3"), groups.get("c1"), groups.get("c2")),
            "salary_period": period,
            "salary_text": match.group(0).strip(),
            "salary_source": "description",
        }
    return None


def structured(lo=None, hi=None, currency=None, period=None, text=None) -> dict | None:
    """Normalise a structured compensation field from an ATS."""
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    lo, hi = num(lo), num(hi)
    text = (text or "").strip() or None
    # Placeholders like "£1 — £1" (seen on Greenhouse) carry no information.
    if (hi or lo or 0) and (hi or lo) < 5:
        lo = hi = None
        text = None
    if lo is None and hi is None and not text:
        return None
    period = (period or "").lower()
    period = next((p for p in ("hour", "month", "year") if p in period), None) or (
        "year" if any(w in period for w in ("annual", "yearly")) else None
    )
    if period is None and (lo or hi):
        # No period stated: amounts under 1,000 are hourly rates, not salaries.
        period = "year" if (hi or lo) >= 1000 else "hour"
    return {
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": (currency or "").upper() or None,
        "salary_period": period,
        "salary_text": text,
        "salary_source": "ats",
    }


EMPTY = {k: None for k in (
    "salary_min", "salary_max", "salary_currency", "salary_period", "salary_text", "salary_source",
)}
