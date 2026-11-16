import logging
import threading
import time
from typing import Callable, TypeVar

from metrics import CIRCUIT_OPEN, UPSTREAM_FAILURES

T = TypeVar("T")

logger = logging.getLogger("order-service.resilience")


class UpstreamError(Exception):
    """Удалённый сервис недоступен или ответил ошибкой сервера."""


class CircuitOpenError(UpstreamError):
    """Цепь разомкнута: вызовы к сервису временно блокируются."""


class CircuitBreaker:
    """Размыкает цепь после серии подряд идущих сбоев и пробует восстановиться по таймауту."""

    def __init__(self, name: str, failure_threshold: int = 3, recovery_timeout: float = 10.0) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()
        CIRCUIT_OPEN.labels(service=name).set(0)

    def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        self._ensure_call_allowed()
        try:
            result = func(*args, **kwargs)
        except UpstreamError:
            self._record_failure()
            raise
        self._record_success()
        return result

    def _ensure_call_allowed(self) -> None:
        with self._lock:
            if self._opened_at is None:
                return
            if time.monotonic() - self._opened_at < self.recovery_timeout:
                raise CircuitOpenError(f"Circuit breaker for '{self.name}' is open")
            logger.info("Circuit '%s' half-open: trial request allowed", self.name)

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            UPSTREAM_FAILURES.labels(service=self.name).inc()
            if self._opened_at is not None or self._failures >= self.failure_threshold:
                self._opened_at = time.monotonic()
                CIRCUIT_OPEN.labels(service=self.name).set(1)
                logger.warning("Circuit '%s' opened after %s failures", self.name, self._failures)

    def _record_success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                logger.info("Circuit '%s' closed", self.name)
            self._failures = 0
            self._opened_at = None
            CIRCUIT_OPEN.labels(service=self.name).set(0)
