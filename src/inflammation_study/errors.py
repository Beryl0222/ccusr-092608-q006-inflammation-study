"""登记服务的领域错误。

所有错误都携带稳定的机器可读代码与中文说明，
CLI 据此输出字段/代码/消息三列，与契约校验保持一致。
"""

from __future__ import annotations


class RegistryError(Exception):
    code = "registry_error"

    def __init__(self, message: str, *, field: str = "$") -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class AuthorizationError(RegistryError):
    code = "forbidden"


class ContractViolation(RegistryError):
    code = "contract_violation"


class ConflictError(RegistryError):
    """重复提交但内容与原记录不一致。"""

    code = "conflict"


class QuotaExhausted(RegistryError):
    code = "quota_exhausted"


class StateError(RegistryError):
    """聚合当前状态不允许该操作（如已签发后改写、引用已失效版本）。"""

    code = "state_error"


class NotFound(RegistryError):
    code = "not_found"
