import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "notifications.db")
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | notification-service | %(message)s",
)
logger = logging.getLogger("notification-service")

NOTIFICATIONS_SENT = Counter("notifications_sent_total", "Number of sent notifications", ["channel"])


class NotificationCreate(BaseModel):
    recipient: str = Field(pattern=EMAIL_PATTERN)
    subject: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2000)


class Notification(NotificationCreate):
    id: int
    channel: str
    created_at: str


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel    TEXT NOT NULL,
                recipient  TEXT NOT NULL,
                subject    TEXT NOT NULL,
                message    TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Database initialised at %s", DB_PATH)
    yield


app = FastAPI(
    title="Notification Service",
    description="Отправка уведомлений (email-заглушка: запись в лог и в собственную БД).",
    version="1.0.0",
    lifespan=lifespan,
)

Instrumentator(excluded_handlers=["/metrics", "/health"]).instrument(app).expose(
    app, include_in_schema=False
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "notification-service"}


@app.post("/notify", response_model=Notification, status_code=status.HTTP_201_CREATED, tags=["notifications"])
def notify(payload: NotificationCreate):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO notifications (channel, recipient, subject, message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("email", payload.recipient, payload.subject, payload.message, created_at),
        )
        row = conn.execute("SELECT * FROM notifications WHERE id = ?", (cursor.lastrowid,)).fetchone()
    NOTIFICATIONS_SENT.labels(channel="email").inc()
    logger.info(
        "EMAIL #%s -> %s | %s | %s", row["id"], row["recipient"], row["subject"], row["message"]
    )
    return dict(row)


@app.get("/notifications", response_model=list[Notification], tags=["notifications"])
def list_notifications():
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM notifications ORDER BY id").fetchall()
    return [dict(row) for row in rows]


@app.get("/notifications/{notification_id}", response_model=Notification, tags=["notifications"])
def get_notification(notification_id: int):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Notification {notification_id} not found")
    return dict(row)


@app.delete("/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["notifications"])
def delete_notification(notification_id: int):
    with closing(connect()) as conn, conn:
        deleted = conn.execute("DELETE FROM notifications WHERE id = ?", (notification_id,)).rowcount
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Notification {notification_id} not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
