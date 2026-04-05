from __future__ import annotations

import os


DEFAULT_SAFE_ENV_KEYS = {
    # POSIX
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
    # Windows — required for process startup and for CLIs that read user
    # config (claude, codex, git). Without SystemRoot, DLL loads fail with
    # STATUS_BREAKPOINT before the process even starts.
    "APPDATA",
    "ComSpec",
    "LOCALAPPDATA",
    "PATHEXT",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "SystemDrive",
    "SystemRoot",
    "TEMP",
    "TMP",
    "USERNAME",
    "USERPROFILE",
    "windir",
}


def build_subprocess_env(extra: dict[str, str], passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    # On Windows, env var names are case-insensitive; os.environ returns them
    # uppercased. Match case-insensitively so the same safe-key list works on
    # both POSIX and Windows.
    allowed_casefold = {key.casefold() for key in DEFAULT_SAFE_ENV_KEYS}
    allowed_casefold.update(key.casefold() for key in passthrough if key)
    sanitized = {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in allowed_casefold
    }
    sanitized.update(extra)
    return sanitized
