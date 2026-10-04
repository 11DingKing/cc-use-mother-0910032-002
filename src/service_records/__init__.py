"""服务记录防重复归档服务端。

子模块：

- canonical：确定性序列化与内容指纹
- models：状态、事件类型与数据载体
- matching：身份线索打分与候选聚类
- auth：角色与授权
- store：SQLite 持久化（含追加式事件流）
- service：应用服务（用例层）
- httpapi：基于标准库的 JSON HTTP 服务
- manage：管理命令（初始化、导入、重跑去重、起服务）
"""
from .service import (
    DedupService,
    ServiceError,
    ValidationError,
    NotFoundError,
    ConflictError,
)
from .auth import Actor, AuthzError
from .store import Store

__all__ = [
    "DedupService",
    "Store",
    "Actor",
    "AuthzError",
    "ServiceError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
]
