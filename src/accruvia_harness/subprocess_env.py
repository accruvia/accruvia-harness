from __future__ import annotations

import os


DEFAULT_SAFE_ENV_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "PWD",
    "PYTHONPATH",
    "SHELL",
    "SHLVL",
    "TERM",
    "TMPDIR",
    "TZ",
    "USER",
}


def build_subprocess_env(extra: dict[str, str], passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    allowed = set(DEFAULT_SAFE_ENV_KEYS)
    allowed.update(key for key in passthrough if key)
    sanitized = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("ACCRUVIA_")
    }
    sanitized.update(extra)
    return sanitized
