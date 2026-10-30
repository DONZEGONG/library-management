"""Strip passwords and credentials from text before it is stored or sent
to the AI (SPEC Layer 4 "Never store", PLAN test 20)."""
import re

REDACTED = "[REDACTED]"

# Words like "secret" are left out on purpose: they appear in book titles.
_SECRET_WORDS = r"(?:password|passwd|pwd|passcode|pass\s*word|otp|login\s*code)"
_WORD_PATTERN = re.compile(rf"(\b{_SECRET_WORDS}\b\s*(?:is|was|=|:|-)?\s*)(\S+)", re.IGNORECASE)
_PIN_PATTERN = re.compile(r"(\bpin\b\s*(?:is|was|=|:|-)?\s*)(\d{3,})", re.IGNORECASE)
_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")


def redact(text: str) -> str:
    if not text:
        return text
    text = _WORD_PATTERN.sub(lambda m: m.group(1) + REDACTED, text)
    text = _PIN_PATTERN.sub(lambda m: m.group(1) + REDACTED, text)
    return _KEY_PATTERN.sub(REDACTED, text)


def redact_obj(value):
    """Redact every string inside a dict/list (used for tool arguments)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value
