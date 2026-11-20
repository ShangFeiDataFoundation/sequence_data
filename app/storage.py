"""统一 Agent 流程使用的通用文件与标识工具。"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._一-鿿-]")


def new_id(prefix: str) -> str:
    """生成短 ID，例如 run_7f3a9c1d2b4e。"""
    return f"{prefix}_{os.urandom(6).hex()}"


def now() -> datetime:
    return datetime.now(timezone.utc)


def safe_filename(raw: str) -> str:
    """只保留基名并剔除路径和危险字符。"""
    base = Path(raw.replace("\\", "/")).name.strip().lstrip(".") or "unnamed"
    cleaned = _UNSAFE_NAME.sub("_", base)
    return cleaned[:180] or "unnamed"
