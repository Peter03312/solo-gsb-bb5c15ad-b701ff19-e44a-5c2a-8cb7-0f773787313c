"""SQLite 连接管理与建表。

并发策略：
- 开启 WAL，允许签发（读事务）与修订（写事务）并发；
- 写操作使用 BEGIN IMMEDIATE 立即拿写锁，busy_timeout 期间等待，
  避免升级死锁；
- 签发在一个事务内读取“同一已提交状态”的全部活动读数并生成快照，
  事务期间写入方要么在签发前提交（修订可见，签发与修订并发时以
  同一已提交状态计算），要么在签发提交后才生效（快照不受影响）。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request

from .config import settings

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    start_at TEXT NOT NULL,                 -- ISO-8601 UTC
    end_at TEXT NOT NULL,
    allowed_temp_min REAL NOT NULL,
    allowed_temp_max REAL NOT NULL,
    max_continuous_exceed_seconds REAL NOT NULL,
    max_sample_interval_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 每个（窗口, 读数 ID）只有一行“当前活动”读数
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id),
    -- 同一窗口内不同读数不允许同一时刻
    UNIQUE(window_id, measured_at)
);

CREATE INDEX IF NOT EXISTS idx_readings_window_time
    ON readings(window_id, measured_at);

-- 每次提交（含同修订号幂等重放）都留痕，用于内容冲突检测；
-- 作废也是该 ID 的一条修订（status='voided'）：作废行的
-- measured_at/temperature/prev_revision 留存被作废的前一版本，
-- void_reason/voided_at 留存作废原因与操作时间
CREATE TABLE IF NOT EXISTS reading_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    reading_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    measured_at TEXT NOT NULL,
    temperature REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',  -- active | superseded | voided
    void_reason TEXT,                       -- 作废原因（仅 voided 行）
    voided_at TEXT,                         -- 作废操作时间 ISO-8601 UTC（仅 voided 行）
    prev_revision INTEGER,                  -- 被作废的前一版本修订号（仅 voided 行）
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, reading_id, revision)
);

-- 证书版本：同一窗口每次签发新增一行，快照不可变
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    version INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    note TEXT,
    issued_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(window_id, version)
);

CREATE INDEX IF NOT EXISTS idx_certificates_window
    ON certificates(window_id, version);
"""

# 既有库（CREATE TABLE IF NOT EXISTS 不会变更已有表）需要补的列
_READING_REVISIONS_NEW_COLUMNS = {
    "void_reason": "void_reason TEXT",
    "voided_at": "voided_at TEXT",
    "prev_revision": "prev_revision INTEGER",
}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or settings.db_path
    conn = sqlite3.connect(
        path,
        timeout=settings.db_timeout,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms};")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """幂等建表；对既有库补充作废相关列（幂等）。"""
    conn.executescript(SCHEMA_SQL)
    _migrate_reading_revisions(conn)
    conn.commit()


def _migrate_reading_revisions(conn: sqlite3.Connection) -> None:
    """为既有数据库的 reading_revisions 表补充作废列（ALTER TABLE 幂等）。"""
    existing = {
        row["name"] for row in conn.execute("PRAGMA table_info(reading_revisions)")
    }
    for name, ddl in _READING_REVISIONS_NEW_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE reading_revisions ADD COLUMN {ddl}")


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE 立即取写锁，提交/回滚由上下文控制。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个独立连接。

    FastAPI 同步端点运行在线程池中，必须使用每请求连接，避免多线程
    共享同一连接造成事务嵌套错误；WAL + BEGIN IMMEDIATE 负责跨连接的
    并发控制。
    """
    conn = connect()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
