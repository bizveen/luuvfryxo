"""One local key for Sri Lankan customer phones and their stored aliases."""
import re


def normalize_mobile(value):
    value = str(value or "").strip()
    if not value:
        return ""
    if re.search(r"[^0-9+()\s-]", value):
        return ""
    digits = re.sub(r"[^0-9]", "", value)
    if digits.startswith("94") and len(digits) == 11:
        digits = "0" + digits[2:]
    elif digits.startswith("7") and len(digits) == 9:
        digits = "0" + digits
    return digits if re.fullmatch(r"07[0-9]{8}", digits) else ""


def phone_variants(value):
    local = normalize_mobile(value)
    if not local:
        return [str(value or "").strip()]
    international = "94" + local[1:]
    return [local, international, "+" + international, local[1:]]
