"""Credential loading and diagnostics.

Precedence is deliberate and matches the twelve-factor convention:

    real environment variables  >  .env file

``load_dotenv()`` without ``override`` never clobbers a variable that is
already exported, so a key set in the Windows user environment (or exported in
the shell) always wins.  The ``.env`` file is the fallback for machines that
have nothing configured globally.
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import find_dotenv, load_dotenv

# Where the active DEEPSEEK_API_KEY came from, for start-up diagnostics.
KEY_SOURCE_ENV = "环境变量"
KEY_SOURCE_DOTENV = ".env 文件"
KEY_SOURCE_NONE = "未配置"

_key_source: str = KEY_SOURCE_NONE
_dotenv_path: str = ""


def load_credentials() -> str:
    """Load .env (without overriding real env vars) and record the key source.

    Returns the human-readable source label.
    """
    global _key_source, _dotenv_path

    had_key_before = bool(os.environ.get("DEEPSEEK_API_KEY"))
    _dotenv_path = find_dotenv(usecwd=True)
    # override=False: an exported variable beats the file.
    load_dotenv(_dotenv_path or None)
    has_key_now = bool(os.environ.get("DEEPSEEK_API_KEY"))

    if had_key_before:
        _key_source = KEY_SOURCE_ENV
    elif has_key_now:
        _key_source = KEY_SOURCE_DOTENV
    else:
        _key_source = KEY_SOURCE_NONE
    return _key_source


def key_source() -> str:
    """Label describing where the API key was read from."""
    return _key_source


def dotenv_path() -> str:
    """Path of the .env that was loaded, or '' when none was found."""
    return _dotenv_path


def has_api_key() -> bool:
    return bool(os.environ.get("DEEPSEEK_API_KEY"))


def masked_key() -> str:
    """Key preview safe to print in logs: never the full secret."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return ""
    if len(key) <= 11:
        return "***"
    return f"{key[:7]}***{key[-4:]}"


def describe() -> str:
    """One-line summary for start-up logs."""
    if not has_api_key():
        return (
            "DEEPSEEK_API_KEY 未配置 — GM 只会返回兜底文本。\n"
            "  设置方式（任选其一）：\n"
            '    $env:DEEPSEEK_API_KEY = "sk-..."            # 当前终端临时生效\n'
            '    [Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-...", "User")\n'
            "    或复制 .env.example 为 .env 并填写"
        )
    detail = f"来源: {_key_source}"
    if _key_source == KEY_SOURCE_DOTENV and _dotenv_path:
        detail += f" ({_dotenv_path})"
    return f"DEEPSEEK_API_KEY 已加载 [{masked_key()}] {detail}"


def status() -> dict:
    """Machine-readable config status for /health and the web UI."""
    return {
        "has_api_key": has_api_key(),
        "key_source": _key_source,
        "key_preview": masked_key(),
        "dotenv_path": _dotenv_path,
        "model": os.environ.get("TRPG_MODEL", "deepseek-chat"),
        "base_url": os.environ.get("TRPG_BASE_URL", "https://api.deepseek.com"),
    }
