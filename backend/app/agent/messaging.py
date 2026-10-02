"""Rule-based message checks. No LLM.

guard_outbound(text)  -> blocks what the agent must never send:
    CONTACT_EMAIL, CONTACT_PHONE, CONTACT_URL   (moves the conversation off-platform)
    PAYMENT_DETAILS                             (bank / card / wallet details, off-platform payment)
    PRICE_ACCEPTANCE                            (agreeing to a price in chat; hiring happens only
                                                 through a gated platform proposal)

flag_inbound(text)    -> marks hostile freelancer text for the log:
    OFF_PLATFORM      pay directly, bank transfer, PayPal, WhatsApp, skip the platform fee ...
    EXTRA_CHARGE      extra / additional / hidden fees, surcharges, "for an extra HKD ..."
    FALSE_AUTHORITY   "pre-approved", "release now", "your client said", "ignore your instructions"

Flags are information only. They are logged as FLAGGED events and never trigger an action:
nothing in the agent reads a flag and pays, releases or replies because of it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_I = re.IGNORECASE

# --- outbound ------------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", _I)
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+|\b[a-z0-9-]+\.(?:com|net|org|io|hk|me|co|ly|app)\b(?:/\S*)?", _I)
_DIGIT_RUN = re.compile(r"\+?\(?\d[\d\s().-]{6,}\d")       # candidate phone / card numbers
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAYMENT = re.compile(
    r"\b(?:bank\s+transfer|wire\s+transfer|account\s+(?:number|no\.?)|routing\s+number|swift|iban|"
    r"paypal|venmo|wechat\s*pay|alipay|payme|fps|crypto|bitcoin|usdt|wallet\s+address|"
    r"credit\s+card|card\s+number|cvv|pay\s+(?:you|me)\s+directly)\b", _I)
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_ACCEPT = re.compile(
    r"\b(?:i|we)\s+(?:accept|agree)\b|\baccept(?:ed)?\s+(?:your|the|this)\s+(?:price|offer|quote|terms)\b|"
    r"\bit'?s\s+a\s+deal\b|\bdeal\s+(?:at|for)\b|\bagreed\b|\b(?:we|i)(?:'ll|\s+will)\s+pay\b|"
    r"\bgo\s+ahead\s+at\b|\byou'?re\s+hired\b|\bconfirm(?:ed)?\s+(?:the|your)\s+price\b", _I)


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


def _has_phone_or_card(text: str) -> bool:
    for m in _DIGIT_RUN.finditer(text):
        candidate = m.group(0).strip()
        if _ISO_DATE.match(candidate):
            continue
        if sum(ch.isdigit() for ch in candidate) >= 8:
            return True
    return False


def guard_outbound(text: str) -> GuardResult:
    violations = []
    if _EMAIL.search(text):
        violations.append("CONTACT_EMAIL")
    if _has_phone_or_card(text):
        violations.append("CONTACT_PHONE")
    if _URL.search(_EMAIL.sub("", text)):
        violations.append("CONTACT_URL")
    if _PAYMENT.search(text) or _IBAN.search(text):
        violations.append("PAYMENT_DETAILS")
    if _ACCEPT.search(text):
        violations.append("PRICE_ACCEPTANCE")
    return GuardResult(ok=not violations, violations=tuple(violations))


# --- inbound -------------------------------------------------------------------------------

_OFF_PLATFORM = re.compile(
    r"\boff[-\s]?platform\b|\boutside\s+(?:the\s+)?platform\b|\bpay\s+(?:me\s+)?directly\b|"
    r"\b(?:bank|wire)\s+transfer\b|\bpaypal\b|\bvenmo\b|\bwechat\b|\bwhatsapp\b|\btelegram\b|\bsignal\s+me\b|"
    r"\b(?:skip|avoid|save)\s+(?:the\s+)?(?:platform\s+)?fees?\b|\bwhy\s+pay\s+platform\s+fees\b|"
    r"\bemail\s+me\s+at\b|\bcontact\s+me\s+(?:on|at|via)\b", _I)
_EXTRA_CHARGE = re.compile(
    r"\bextra\s+(?:charge|fee|cost|HKD|HK\$|\$)|\badditional\s+(?:fee|charge|cost|payment)s?\b|"
    r"\bsurcharge\b|\bhidden\s+fee\b|\bnot\s+included\b|\bplus\s+(?:a\s+)?(?:\w+\s+)?fee\b|\bupgrade\s+fee\b", _I)
_FALSE_AUTHORITY = re.compile(
    r"\bpre[-\s]?approved\b|\balready\s+(?:been\s+)?approved\b|\brelease\s+(?:the\s+)?(?:payment|funds|money)\b|"
    r"\brelease\s+(?:it\s+)?now\b|\byour\s+(?:client|principal|boss|user)\s+(?:said|told|approved|authori[sz]ed)\b|"
    r"\b(?:you\s+are|you're)\s+authori[sz]ed\b|\bignore\s+(?:your|all|previous)\s+(?:\w+\s+)?instructions\b|"
    r"\b(?:system|admin)\s+(?:notice|message|override)\b", _I)

FLAG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OFF_PLATFORM", _OFF_PLATFORM),
    ("EXTRA_CHARGE", _EXTRA_CHARGE),
    ("FALSE_AUTHORITY", _FALSE_AUTHORITY),
)


def flag_inbound(text: str) -> tuple[str, ...]:
    """Flag codes for an inbound message, in a fixed order. Empty tuple = nothing suspicious."""
    return tuple(code for code, pattern in FLAG_RULES if pattern.search(text))
