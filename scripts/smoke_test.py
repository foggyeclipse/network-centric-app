"""Сквозная проверка работающей системы: python scripts/smoke_test.py"""
import sys
import uuid

import httpx

USER_URL = "http://localhost:5000"
NOTIFICATION_URL = "http://localhost:5001"
ORDER_URL = "http://localhost:5002"

failures = 0


def check(description: str, condition: bool) -> None:
    global failures
    failures += not condition
    print(f"[{'PASS' if condition else 'FAIL'}] {description}")


def main() -> int:
    with httpx.Client(timeout=5) as http:
        email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"

        response = http.post(f"{USER_URL}/users", json={"name": "Smoke Test", "email": email})
        check("User Service: пользователь создан (201)", response.status_code == 201)
        user_id = response.json()["id"]

        response = http.post(f"{USER_URL}/users", json={"name": "Smoke Test", "email": email})
        check("User Service: дубликат email отклонён (409)", response.status_code == 409)

        response = http.post(
            f"{ORDER_URL}/orders",
            json={"user_id": user_id, "item": "Книга", "quantity": 2, "price": 450.0},
        )
        order = response.json()
        check("Order Service: заказ создан (201)", response.status_code == 201)
        check("Order Service: сумма заказа рассчитана верно", order.get("total") == 900.0)
        check("Order Service: уведомление отправлено", order.get("notification_status") == "sent")

        response = http.get(f"{NOTIFICATION_URL}/notifications")
        recipients = [item["recipient"] for item in response.json()]
        check("Notification Service: уведомление сохранено", email in recipients)

        response = http.post(
            f"{ORDER_URL}/orders",
            json={"user_id": 999999, "item": "Книга", "quantity": 1, "price": 100},
        )
        check("Order Service: несуществующий пользователь (404)", response.status_code == 404)

        response = http.get(f"{ORDER_URL}/orders", params={"user_id": user_id})
        check("Order Service: список заказов пользователя", len(response.json()) == 1)

        for name, url in (("User", USER_URL), ("Notification", NOTIFICATION_URL), ("Order", ORDER_URL)):
            response = http.get(f"{url}/metrics")
            check(f"{name} Service: метрики Prometheus доступны", "http_requests_total" in response.text)

        response = http.get(f"{ORDER_URL}/metrics")
        check("Order Service: бизнес-метрика заказов опубликована", "orders_created_total" in response.text)

    print(f"\nИтого: {'все проверки пройдены' if not failures else f'провалено проверок: {failures}'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except httpx.ConnectError:
        print("Сервисы недоступны. Запустите систему: docker compose up --build")
        sys.exit(2)
