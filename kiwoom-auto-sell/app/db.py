"""
SQLite 영속화.
- config: appkey/secretkey/is_mock/token 같은 전역 설정 key-value
- strategies: 종목별 전략
- sell_slots: 한 전략당 보통 4개의 매도 슬롯 (분할 매도)
- activity_log: 감사 로그
"""
from __future__ import annotations

import sqlite3
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("db")

import os as _os
# DB 위치: 프로젝트 폴더 밖 (재배포해도 데이터 보존)
# 1순위: 환경변수 KIWOOM_DB_PATH
# 2순위: ~/kiwoom-data.db (홈 디렉토리)
# 3순위: 프로젝트 폴더 내 data.db (하위호환)
_env_db = _os.environ.get("KIWOOM_DB_PATH")
if _env_db:
    DB_PATH = Path(_env_db)
else:
    _home_db = Path.home() / "kiwoom-data.db"
    _legacy_db = Path(__file__).resolve().parent.parent / "data.db"
    if _legacy_db.exists() and not _home_db.exists():
        # 기존 사용자: 한 번만 이동
        try:
            import shutil
            shutil.move(str(_legacy_db), str(_home_db))
        except Exception:
            pass
    DB_PATH = _home_db
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS strategies (
            stock_code TEXT PRIMARY KEY,
            stock_name TEXT,
            strategy_type TEXT NOT NULL,        -- day | target | swing1 | swing2 | swing3
            params TEXT NOT NULL DEFAULT '{}',  -- JSON: {target_price, trigger_price, thresholds ...}
            total_qty INTEGER NOT NULL,         -- 4등분 대상 짝수 수량
            reserved_qty INTEGER NOT NULL DEFAULT 0, -- 홀수면 1
            state TEXT NOT NULL DEFAULT 'active', -- active | completed | cancelled
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            initialized INTEGER NOT NULL DEFAULT 0  -- 슬롯 생성 완료 여부 (장 시작 후 초기화)
        );

        CREATE TABLE IF NOT EXISTS sell_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            slot_index INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', -- pending | ordered | partial | filled | cancelled | error
            order_no TEXT,
            order_price INTEGER,
            order_tp TEXT,             -- limit | market
            scheduled_time TEXT,       -- ISO datetime KST. 이 시각 이후 발사.
            ordered_at TEXT,
            filled_at TEXT,
            filled_qty INTEGER DEFAULT 0,   -- 실제 체결된 수량 (부분 체결 추적)
            filled_price INTEGER DEFAULT 0, -- 평균 체결가
            trigger TEXT NOT NULL DEFAULT '{}',
            fallback_deadline TEXT,
            notes TEXT DEFAULT '',
            FOREIGN KEY(stock_code) REFERENCES strategies(stock_code) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL DEFAULT 'INFO',   -- INFO | WARN | ERROR
            stock_code TEXT,
            action TEXT NOT NULL,
            detail TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_slots_stock ON sell_slots(stock_code);
        CREATE INDEX IF NOT EXISTS idx_slots_status ON sell_slots(status);
        CREATE INDEX IF NOT EXISTS idx_log_ts ON activity_log(timestamp);
        """)

        # 마이그레이션: 기존 DB에 새 컬럼 추가 (있으면 무시)
        for col_sql in [
            "ALTER TABLE sell_slots ADD COLUMN filled_qty INTEGER DEFAULT 0",
            "ALTER TABLE sell_slots ADD COLUMN filled_price INTEGER DEFAULT 0",
        ]:
            try:
                c.execute(col_sql)
            except sqlite3.OperationalError:
                pass  # 이미 있음

        log.info("DB 초기화 완료: %s", DB_PATH)


# ---------- config ----------

def config_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock, _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def config_set(key: str, value: str) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def config_all() -> dict[str, str]:
    with _lock, _conn() as c:
        return {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM config")}


# ---------- strategies ----------

def upsert_strategy(
    stock_code: str,
    stock_name: str,
    strategy_type: str,
    params: dict,
    total_qty: int,
    reserved_qty: int,
) -> None:
    now = _now()
    with _lock, _conn() as c:
        c.execute("""
        INSERT INTO strategies(stock_code,stock_name,strategy_type,params,total_qty,reserved_qty,
                               state,created_at,updated_at,initialized)
        VALUES(?,?,?,?,?,?, 'active', ?, ?, 0)
        ON CONFLICT(stock_code) DO UPDATE SET
            stock_name=excluded.stock_name,
            strategy_type=excluded.strategy_type,
            params=excluded.params,
            total_qty=excluded.total_qty,
            reserved_qty=excluded.reserved_qty,
            state='active',
            updated_at=excluded.updated_at,
            initialized=0
        """, (stock_code, stock_name, strategy_type, json.dumps(params, ensure_ascii=False),
              total_qty, reserved_qty, now, now))


def get_strategy(stock_code: str) -> Optional[dict]:
    with _lock, _conn() as c:
        row = c.execute("SELECT * FROM strategies WHERE stock_code=?", (stock_code,)).fetchone()
        return _row_to_strategy(row) if row else None


def list_strategies(active_only: bool = False) -> list[dict]:
    with _lock, _conn() as c:
        sql = "SELECT * FROM strategies"
        if active_only:
            sql += " WHERE state='active'"
        sql += " ORDER BY updated_at DESC"
        rows = c.execute(sql).fetchall()
        return [_row_to_strategy(r) for r in rows]


def mark_strategy_initialized(stock_code: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE strategies SET initialized=1, updated_at=? WHERE stock_code=?",
                  (_now(), stock_code))


def set_strategy_state(stock_code: str, state: str) -> None:
    with _lock, _conn() as c:
        c.execute("UPDATE strategies SET state=?, updated_at=? WHERE stock_code=?",
                  (state, _now(), stock_code))


def delete_strategy(stock_code: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM strategies WHERE stock_code=?", (stock_code,))


# ---------- slots ----------

def add_slot(
    stock_code: str,
    slot_index: int,
    qty: int,
    order_tp: str,                   # 'limit' | 'market'
    scheduled_time: Optional[str] = None,
    order_price: Optional[int] = None,
    trigger: Optional[dict] = None,
    fallback_deadline: Optional[str] = None,
    notes: str = "",
) -> int:
    with _lock, _conn() as c:
        cur = c.execute("""
        INSERT INTO sell_slots
        (stock_code,slot_index,qty,status,order_tp,scheduled_time,order_price,trigger,
         fallback_deadline,notes)
        VALUES(?,?,?, 'pending', ?,?,?,?,?,?)
        """, (stock_code, slot_index, qty, order_tp, scheduled_time, order_price,
              json.dumps(trigger or {}, ensure_ascii=False), fallback_deadline, notes))
        return cur.lastrowid


def get_slots(stock_code: str) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM sell_slots WHERE stock_code=? ORDER BY slot_index",
            (stock_code,)
        ).fetchall()
        return [_row_to_slot(r) for r in rows]


def update_slot(slot_id: int, **fields) -> None:
    if not fields:
        return
    # 허용된 컬럼만 업데이트 (오타 방지)
    ALLOWED_COLS = {"status", "order_no", "order_price", "order_tp",
                    "scheduled_time", "ordered_at", "filled_at",
                    "filled_qty", "filled_price", "trigger",
                    "fallback_deadline", "notes", "qty"}
    unknown = set(fields.keys()) - ALLOWED_COLS
    if unknown:
        log.warning("update_slot 알 수 없는 컬럼 무시: %s", unknown)
        fields = {k: v for k, v in fields.items() if k in ALLOWED_COLS}
        if not fields:
            return
    if "trigger" in fields and isinstance(fields["trigger"], dict):
        fields["trigger"] = json.dumps(fields["trigger"], ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [slot_id]
    with _lock, _conn() as c:
        c.execute(f"UPDATE sell_slots SET {cols} WHERE id=?", vals)


def delete_slots(stock_code: str) -> None:
    with _lock, _conn() as c:
        c.execute("DELETE FROM sell_slots WHERE stock_code=?", (stock_code,))


def get_all_active_ordered_slots() -> list[dict]:
    """전체 종목에서 'ordered' 상태인 슬롯 (체결 감시용)."""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM sell_slots WHERE status='ordered'"
        ).fetchall()
        return [_row_to_slot(r) for r in rows]


# ---------- activity log ----------

def log_activity(action: str, stock_code: str = "", detail: str = "", level: str = "INFO") -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO activity_log(timestamp,level,stock_code,action,detail) VALUES(?,?,?,?,?)",
            (_now(), level, stock_code, action, detail),
        )


def recent_logs(limit: int = 200) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- 헬퍼 ----------

def _now() -> str:
    from datetime import timezone, timedelta
    KST = timezone(timedelta(hours=9))
    return datetime.now(KST).isoformat(timespec="seconds")


def _row_to_strategy(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["params"] = json.loads(d.get("params") or "{}")
    except Exception:
        d["params"] = {}
    return d


def _row_to_slot(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["trigger"] = json.loads(d.get("trigger") or "{}")
    except Exception:
        d["trigger"] = {}
    return d
