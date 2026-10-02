"""Deterministic price extraction from (untrusted) freelancer messages.

No LLM is involved in reading money. We accept only explicit HKD amounts ("HKD 1,200",
"HK$1200.50"). If a message has no price, more than one distinct price, or mentions another
currency, the result is not "ok" and the agent/gate must not treat it as a quote.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel

from app.money import decimal_str_to_minor

# "HKD 1,234.50", "HKD1234", "HK$ 1,234", "HK$1234.5"
_HKD = re.compile(r"(?:HKD|HK\$)\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?![\d,.]*\d)", re.IGNORECASE)
# Any other currency marker makes the message ambiguous.
_OTHER_CURRENCY = re.compile(r"(?:USD|US\$|EUR|GBP|RMB|CNY|JPY|€|£|¥|(?<![A-Za-z])\$(?=\s?\d))", re.IGNORECASE)


class QuoteParse(BaseModel):
    status: Literal["ok", "none", "ambiguous"]
    amount_minor: Optional[int] = None
    found: list[int] = []


def parse_quote(text: str) -> QuoteParse:
    try:
        found = sorted({decimal_str_to_minor(m) for m in _HKD.findall(text)})
    except ValueError:
        return QuoteParse(status="ambiguous")
    # Strip HK$ before looking for a bare "$" so "HK$100" is not flagged as another currency.
    if _OTHER_CURRENCY.search(_HKD.sub("", text)):
        return QuoteParse(status="ambiguous", found=found)
    if not found:
        return QuoteParse(status="none")
    if len(found) > 1:
        return QuoteParse(status="ambiguous", found=found)
    return QuoteParse(status="ok", amount_minor=found[0], found=found)
