import logging

import httpx
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from resilience import CircuitBreaker, UpstreamError

logger = logging.getLogger("order-service.clients")


class UserNotFoundError(Exception):
    pass


class ServiceClient:
    """HTTP-клиент с повторными попытками и circuit breaker."""

    def __init__(self, name: str, base_url: str, timeout: float = 2.0) -> None:
        self.name = name
        self._http = httpx.Client(base_url=base_url, timeout=timeout)
        self._breaker = CircuitBreaker(name)

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self._breaker.call(self._send, method, path, **kwargs)

    @retry(
        retry=retry_if_exception_type(UpstreamError),
        stop=stop_after_attempt(3),
        wait=wait_fixed(0.3),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{self.name} is unreachable: {exc!r}") from exc
        if response.status_code >= 500:
            raise UpstreamError(f"{self.name} responded with {response.status_code}")
        return response

    def close(self) -> None:
        self._http.close()


class UserClient(ServiceClient):
    def get_user(self, user_id: int) -> dict:
        response = self.request("GET", f"/users/{user_id}")
        if response.status_code == 404:
            raise UserNotFoundError(user_id)
        if response.is_error:
            raise UpstreamError(f"{self.name} responded with {response.status_code}")
        return response.json()


class NotificationClient(ServiceClient):
    def send(self, recipient: str, subject: str, message: str) -> dict:
        response = self.request(
            "POST",
            "/notify",
            json={"recipient": recipient, "subject": subject, "message": message},
        )
        if response.is_error:
            raise UpstreamError(f"{self.name} responded with {response.status_code}")
        return response.json()
