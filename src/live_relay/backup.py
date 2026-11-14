"""Portable, explicit backups of the relay settings.

The dashboard stores settings in a local ``.env``. That is convenient until
the disk dies or the machine is replaced. This module builds a small JSON
document with a fixed whitelist of keys — no arbitrary environment variables —
and validates an imported document before anything is written.

Secrets are opt-in. A default export is safe to keep in a synced folder; an
export with secrets is clearly marked and should be treated like a password.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping

FORMAT = "live-relay-config"
SCHEMA = 1


def build_export(
    values: Mapping[str, Any],
    *,
    config_keys: Iterable[str],
    secret_keys: Iterable[str],
    version: str,
    include_secrets: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    config = {key: str(values.get(key, "") or "") for key in config_keys}
    payload: dict[str, Any] = {
        "app": FORMAT,
        "schema": SCHEMA,
        "version": str(version),
        "exported_at": float(now if now is not None else time.time()),
        "secrets_included": bool(include_secrets),
        "config": config,
    }
    if include_secrets:
        payload["secrets"] = {
            key: str(values.get(key, "") or "") for key in secret_keys
        }
    return payload


def _clean_value(key: str, value: Any, *, secret: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise ValueError(f"قيمة غير صالحة للمفتاح {key}.")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise ValueError(f"المفتاح {key} يحتوي سطراً جديداً.")
    if len(text) > 4000:
        raise ValueError(f"قيمة المفتاح {key} أطول من المسموح.")
    if secret and text and len(text) < 4:
        # Short secrets are usually an editing mistake; refuse to import them.
        raise ValueError(f"قيمة المفتاح السري {key} قصيرة جداً.")
    return text


def validate_import(
    payload: Any,
    *,
    config_keys: Iterable[str],
    secret_keys: Iterable[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return (config, secrets) after strict validation.

    Only whitelisted keys are accepted; unknown keys abort the whole import so
    a typo cannot silently half-apply a backup.
    """
    if not isinstance(payload, Mapping):
        raise ValueError("ملف النسخة الاحتياطية يجب أن يكون كائن JSON.")
    app = str(payload.get("app") or "")
    if app and app != FORMAT:
        raise ValueError("هذا الملف ليس نسخة احتياطية من live-relay.")
    allowed_config = {str(key) for key in config_keys}
    allowed_secrets = {str(key) for key in secret_keys}
    raw_config = payload.get("config") or {}
    raw_secrets = payload.get("secrets") or {}
    if not isinstance(raw_config, Mapping) or not isinstance(raw_secrets, Mapping):
        raise ValueError("بنية الملف غير صحيحة: config/secrets يجب أن تكون كائنات.")
    if not raw_config and not raw_secrets:
        raise ValueError("الملف لا يحتوي أي إعداد.")

    unknown = (set(map(str, raw_config)) - allowed_config) | (
        set(map(str, raw_secrets)) - allowed_secrets
    )
    if unknown:
        names = "، ".join(sorted(unknown)[:6])
        raise ValueError(f"مفاتيح غير معروفة في الملف: {names}")

    config = {
        str(key): _clean_value(str(key), value, secret=False)
        for key, value in raw_config.items()
    }
    secrets = {
        str(key): _clean_value(str(key), value, secret=True)
        for key, value in raw_secrets.items()
    }
    return config, secrets
