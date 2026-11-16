import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

from clients import NotificationClient, UserClient, UserNotFoundError
from metrics import ORDERS_CREATED
from resilience import UpstreamError

DB_PATH = os.getenv("DB_PATH", "orders.db")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://localhost:5000")
NOTIFICATION_SERVICE_URL = os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:5001")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | order-service | %(message)s",
)
logger = logging.getLogger("order-service")

user_client = UserClient("user-service", USER_SERVICE_URL)
notification_client = NotificationClient("notification-service", NOTIFICATION_SERVICE_URL)

OrderStatus = Literal["created", "paid", "shipped", "cancelled"]


class OrderCreate(BaseModel):
    user_id: int = Field(gt=0)
    item: str = Field(min_length=1, max_length=200)
    quantity: int = Field(default=1, ge=1)
    price: float = Field(gt=0, description="Цена за единицу")


class OrderStatusUpdate(BaseModel):
    status: OrderStatus


class Order(BaseModel):
    id: int
    user_id: int
    item: str
    quantity: int
    price: float
    total: float
    status: OrderStatus
    notification_status: Literal["sent", "failed"]
    created_at: str


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id             INTEGER NOT NULL,
                item                TEXT NOT NULL,
                quantity            INTEGER NOT NULL,
                price               REAL NOT NULL,
                total               REAL NOT NULL,
                status              TEXT NOT NULL,
                notification_status TEXT NOT NULL,
                created_at          TEXT NOT NULL
            )
            """
        )


def find_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Order {order_id} not found")
    return row


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Database initialised at %s", DB_PATH)
    yield
    user_client.close()
    notification_client.close()


app = FastAPI(
    title="Order Service",
    description="Создание и просмотр заказов. Проверяет пользователя через User Service "
    "и отправляет уведомления через Notification Service.",
    version="1.0.0",
    lifespan=lifespan,
)

Instrumentator(excluded_handlers=["/metrics", "/health"]).instrument(app).expose(
    app, include_in_schema=False
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "order-service"}


@app.post("/orders", response_model=Order, status_code=status.HTTP_201_CREATED, tags=["orders"])
def create_order(payload: OrderCreate):
    try:
        user = user_client.get_user(payload.user_id)
    except UserNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {payload.user_id} not found")
    except UpstreamError as exc:
        logger.error("Cannot validate user: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "User service is unavailable")

    total = round(payload.quantity * payload.price, 2)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO orders (user_id, item, quantity, price, total, status, notification_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'created', 'failed', ?)",
            (payload.user_id, payload.item, payload.quantity, payload.price, total, created_at),
        )
        order_id = cursor.lastrowid
    logger.info("Order created: id=%s user_id=%s total=%.2f", order_id, payload.user_id, total)

    try:
        notification_client.send(
            recipient=user["email"],
            subject=f"Заказ №{order_id} оформлен",
            message=f"{user['name']}, ваш заказ «{payload.item}» × {payload.quantity} "
            f"на сумму {total:.2f} принят.",
        )
        notification_status = "sent"
    except UpstreamError as exc:
        logger.warning("Order %s saved, but notification was not delivered: %s", order_id, exc)
        notification_status = "failed"

    ORDERS_CREATED.labels(notification_status=notification_status).inc()
    with closing(connect()) as conn, conn:
        conn.execute(
            "UPDATE orders SET notification_status = ? WHERE id = ?", (notification_status, order_id)
        )
        order = find_order(conn, order_id)
    return dict(order)


@app.get("/orders", response_model=list[Order], tags=["orders"])
def list_orders(user_id: int | None = None):
    query, params = "SELECT * FROM orders", ()
    if user_id is not None:
        query, params = query + " WHERE user_id = ?", (user_id,)
    with closing(connect()) as conn:
        rows = conn.execute(query + " ORDER BY id", params).fetchall()
    return [dict(row) for row in rows]


@app.get("/orders/{order_id}", response_model=Order, tags=["orders"])
def get_order(order_id: int):
    with closing(connect()) as conn:
        return dict(find_order(conn, order_id))


@app.patch("/orders/{order_id}/status", response_model=Order, tags=["orders"])
def update_order_status(order_id: int, payload: OrderStatusUpdate):
    with closing(connect()) as conn, conn:
        find_order(conn, order_id)
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (payload.status, order_id))
        order = find_order(conn, order_id)
    logger.info("Order %s status -> %s", order_id, payload.status)
    return dict(order)


@app.delete("/orders/{order_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["orders"])
def delete_order(order_id: int):
    with closing(connect()) as conn, conn:
        find_order(conn, order_id)
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    logger.info("Order deleted: id=%s", order_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
