"""确定性序列化与内容指纹。

去重建议与结果稳定性的基础：相同内容永远得到相同序列化文本与相同指纹，
与字典顺序、到达先后、运行时间均无关。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(obj: Any) -> str:
    """键排序、无空白、不转义中文的规范化 JSON。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_id(prefix: str, parts: Any, length: int = 16) -> str:
    """由内容生成带前缀的稳定标识。"""
    return f"{prefix}{sha256_text(canonical_dumps(parts))[:length]}"
