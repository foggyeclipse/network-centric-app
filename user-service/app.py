import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response, status
from prometheus_client import Counter
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DB_PATH", "users.db")
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | user-service | %(message)s",
)
logger = logging.getLogger("user-service")

USERS_CREATED = Counter("users_created_total", "Number of registered users")


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=EMAIL_PATTERN)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN)


class User(BaseModel):
    id: int
    name: str
    email: str
    created_at: str


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                email      TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )


def find_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"User {user_id} not found")
    return row


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("Database initialised at %s", DB_PATH)
    yield


app = FastAPI(
    title="User Service",
    description="Управление пользователями: регистрация и получение данных.",
    version="1.0.0",
    lifespan=lifespan,
)

Instrumentator(excluded_handlers=["/metrics", "/health"]).instrument(app).expose(
    app, include_in_schema=False
)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "user-service"}


@app.post("/users", response_model=User, status_code=status.HTTP_201_CREATED, tags=["users"])
def create_user(payload: UserCreate):
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with closing(connect()) as conn, conn:
            cursor = conn.execute(
                "INSERT INTO users (name, email, created_at) VALUES (?, ?, ?)",
                (payload.name, payload.email, created_at),
            )
            user = find_user(conn, cursor.lastrowid)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered")
    USERS_CREATED.inc()
    logger.info("User created: id=%s email=%s", user["id"], user["email"])
    return dict(user)


@app.get("/users", response_model=list[User], tags=["users"])
def list_users():
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


@app.get("/users/{user_id}", response_model=User, tags=["users"])
def get_user(user_id: int):
    with closing(connect()) as conn:
        return dict(find_user(conn, user_id))


@app.put("/users/{user_id}", response_model=User, tags=["users"])
def update_user(user_id: int, payload: UserUpdate):
    changes = payload.model_dump(exclude_none=True)
    try:
        with closing(connect()) as conn, conn:
            find_user(conn, user_id)
            if changes:
                assignments = ", ".join(f"{column} = ?" for column in changes)
                conn.execute(
                    f"UPDATE users SET {assignments} WHERE id = ?",
                    (*changes.values(), user_id),
                )
            user = find_user(conn, user_id)
    except sqlite3.IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email is already registered")
    logger.info("User updated: id=%s", user_id)
    return dict(user)


@app.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["users"])
def delete_user(user_id: int):
    with closing(connect()) as conn, conn:
        find_user(conn, user_id)
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    logger.info("User deleted: id=%s", user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
