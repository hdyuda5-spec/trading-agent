"""Secret redaction — never let API keys/service tokens reach the logs.

``redact_secrets`` scrubs any configured secret values out of free-form text;
``redact_common`` additionally masks the obvious token shapes. Apply around
any log/notifier call that interpolates config or exchange payloads.
"""

import re
from typing import Iterable, List

_TOKEN_PATTERNS = [
    r"\b(?:[A-Za-z0-9]{24,})\b",          # long alphanum blobs (API keys, tokens)
    r"\b(?:eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{4,})\b",  # JWT-like
    r"(?i)\b(class|lol)\.(?:[a-zA-Z0-9]{10,})\b",          # classic trading keys
    r"(?i)(api[-_ ]?key|secret|token|passphrase|private[-_ ]?key)\s*[=:]\s*\S+",
]


def redact_common(text: str) -> str:
    out = text or ""
    for pattern in _TOKEN_PATTERNS:
        out = re.sub(pattern, "<redacted>", out)
    return out


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(str(secret).strip(), "<redacted>")
    return out


def redact_api_keys(text: str, env: dict) -> str:
    """Redact the values of known API-key env names (whatever they hold)."""
    keys = [env.get(k) for k in ("BINANCE_API_KEY", "BINANCE_SECRET", "TELEGRAM_BOT_TOKEN",
                                 "TELEGRAM_CHAT_ID", "OPENAI_API_KEY", "GEMINI_API_KEY") if env.get(k)]
    return redact_secrets(text, keys)