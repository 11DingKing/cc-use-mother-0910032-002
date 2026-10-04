"""角色与授权。

与领域契约对应：文博中心运营员、志愿者、监护人、场馆负责人。
去重建议可由任意在岗人员触发查看，但合并确认/拒绝、拆分、场次取消、
归档与归档后更正只允许授权岗位执行。
"""
from __future__ import annotations

from dataclasses import dataclass

ROLE_OPERATOR = "operator"          # 文博中心运营员
ROLE_VENUE_MANAGER = "venue_manager"  # 场馆负责人
ROLE_VOLUNTEER = "volunteer"        # 志愿者
ROLE_GUARDIAN = "guardian"          # 监护人

ROLE_LABELS = {
    ROLE_OPERATOR: "文博中心运营员",
    ROLE_VENUE_MANAGER: "场馆负责人",
    ROLE_VOLUNTEER: "志愿者",
    ROLE_GUARDIAN: "监护人",
}

# 可对冲突候选做人工裁决、拆分误合并、归档的岗位
DECISION_ROLES = frozenset({ROLE_OPERATOR, ROLE_VENUE_MANAGER})


@dataclass(frozen=True)
class Actor:
    """一次操作的操作者（由 HTTP 令牌或管理命令注入）。"""

    account: str
    role: str
    name: str = ""

    @property
    def label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)

    def require_decision_role(self, action: str) -> None:
        if self.role not in DECISION_ROLES:
            raise AuthzError(f"无权执行{action}，需要授权岗位（运营员或场馆负责人）")


class AuthzError(PermissionError):
    """授权不足。"""
