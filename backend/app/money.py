"""Money helpers. All money is an int of HKD cents ("minor units"). No floats, ever."""

CURRENCY = "HKD"


def require_minor(value: object) -> int:
    """Return value if it is a plain int (not bool, not float), else raise TypeError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"money must be int minor units, got {type(value).__name__}")
    return value


def format_money(minor: int, currency: str = CURRENCY) -> str:
    """12345 -> 'HKD 123.45'. Used in rule templates and UI text. Assumes 2 decimal places."""
    require_minor(minor)
    sign = "-" if minor < 0 else ""
    whole, cents = divmod(abs(minor), 100)
    return f"{sign}{currency} {whole:,}.{cents:02d}"


def format_hkd(minor: int) -> str:
    return format_money(minor, CURRENCY)


def convert_micros(amount_minor: int, rate_micros: int) -> int:
    """Convert with an integer FX rate (rate x 1,000,000), rounding UP so cost is never under-counted."""
    require_minor(amount_minor)
    require_minor(rate_micros)
    return -(-amount_minor * rate_micros // 1_000_000)


def decimal_str_to_minor(text: str) -> int:
    """'1,234.5' -> 123450. Pure string arithmetic so no float rounding can creep in."""
    text = text.replace(",", "")
    if text.count(".") > 1:
        raise ValueError(f"bad amount: {text!r}")
    whole, _, frac = text.partition(".")
    if not whole.isdigit() or (frac and not frac.isdigit()) or len(frac) > 2:
        raise ValueError(f"bad amount: {text!r}")
    return int(whole) * 100 + int(frac.ljust(2, "0") or "0")


def apply_bps(amount_minor: int, bps: int) -> int:
    """Fee of `bps` basis points on amount, rounded UP to the next cent (never under-count cost)."""
    require_minor(amount_minor)
    require_minor(bps)
    return -(-amount_minor * bps // 10_000)
