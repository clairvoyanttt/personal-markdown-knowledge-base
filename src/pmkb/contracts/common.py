"""Typed boundary objects; services must additionally validate runtime inputs."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

T = TypeVar("T")

class ResultStatus(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"
    PARTIAL = "PARTIAL"
    UNCONFIRMED = "UNCONFIRMED"

@dataclass(frozen=True, slots=True)
class CallContext:
    collection_id: str | None
    context_generation: int

@dataclass(frozen=True, slots=True)
class AppError:
    code: str
    reason: str
    suggested_action: str

@dataclass(frozen=True, slots=True)
class OperationResult(Generic[T]):
    status: ResultStatus
    request_context: CallContext
    delivery_context: CallContext
    value: T | None
    error: AppError | None
    warnings: tuple[AppError, ...] = ()
