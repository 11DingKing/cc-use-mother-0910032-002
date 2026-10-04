"""稳定哈希与规范化 JSON 工具。

所有去重标识都基于内容哈希：同一批补录数据无论重传多少次，
只要字节内容一致，得到的指纹就一致，管理命令重跑不会产生新结果。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """键排序、无空白的 JSON，保证同一逻辑内容序列化为同一字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(*parts: str) -> str:
    """对若干字符串片段计算稳定的 SHA-256 十六进制摘要。"""
    joined = "\0".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def short_id(prefix: str, *parts: str, length: int = 16) -> str:
    """生成带前缀的稳定短标识。"""
    return f"{prefix}_{digest(*parts)[:length]}"
