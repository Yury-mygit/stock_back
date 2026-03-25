"""
Работа с базой данных SQLite.

Две таблицы:
  bonds     — данные из MOEX ISS API (обновляются при синхронизации)
  portfolio — данные портфеля, заполняемые вручную
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from app import config
from app.logger import logger

# Один lock на запись — sqlite3 не любит параллельные write из разных потоков
_write_lock = threading.Lock()


# ── Подключение ────────────────────────────────────────────────────────────

@contextmanager
def get_conn():
    """Контекстный менеджер — открывает соединение и закрывает после блока."""
    conn = sqlite3.connect(
        config.DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row   # строки как словари
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Инициализация схемы ────────────────────────────────────────────────────

def init_db() -> None:
    """Создаёт таблицы если их нет. Вызывается при старте приложения."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bonds (
                secid               TEXT PRIMARY KEY,
                isin                TEXT,
                shortname           TEXT,
                name                TEXT,
                boardid             TEXT,

                -- Цена и доходность
                price               REAL,
                face_value          REAL,
                cost                REAL,
                yield_value         REAL,
                days_to_redemption  INTEGER,
                is_amort            INTEGER DEFAULT 0,
                buyback_date        TEXT,

                -- Купон
                bond_subtype        TEXT,
                coupon_freq         INTEGER,
                coupon_pct          REAL,
                coupon_value        REAL,
                nkd                 REAL,

                -- Параметры
                currency            TEXT,
                is_qual             INTEGER DEFAULT 0,
                matdate             TEXT,
                list_level          INTEGER,
                open_price          REAL,

                updated_at          TEXT
            );

            CREATE TABLE IF NOT EXISTS portfolio (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                secid       TEXT NOT NULL REFERENCES bonds(secid) ON DELETE CASCADE,
                broker      TEXT,
                qty         INTEGER DEFAULT 0,
                rating      TEXT,
                sector      TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(secid)
            );

            CREATE TABLE IF NOT EXISTS exchange_rates (
                date        TEXT PRIMARY KEY,
                usd         REAL,
                eur         REAL,
                cny         REAL,
                updated_at  TEXT DEFAULT (datetime('now'))
            );
        """)

    logger.info("База данных инициализирована: %s", config.DB_PATH)


# ── bonds ──────────────────────────────────────────────────────────────────

def upsert_bond(data: dict) -> None:
    """Создаёт или обновляет запись в bonds."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO bonds (
                secid, isin, shortname, name, boardid,
                price, face_value, cost, yield_value, days_to_redemption,
                is_amort, buyback_date,
                bond_subtype, coupon_freq, coupon_pct, coupon_value, nkd,
                currency, is_qual, matdate, list_level, open_price,
                updated_at
            ) VALUES (
                :secid, :isin, :shortname, :name, :boardid,
                :price, :face_value, :cost, :yield_value, :days_to_redemption,
                :is_amort, :buyback_date,
                :bond_subtype, :coupon_freq, :coupon_pct, :coupon_value, :nkd,
                :currency, :is_qual, :matdate, :list_level, :open_price,
                :updated_at
            )
            ON CONFLICT(secid) DO UPDATE SET
                isin               = excluded.isin,
                shortname          = excluded.shortname,
                name               = excluded.name,
                boardid            = excluded.boardid,
                price              = excluded.price,
                face_value         = excluded.face_value,
                cost               = excluded.cost,
                yield_value        = excluded.yield_value,
                days_to_redemption = excluded.days_to_redemption,
                is_amort           = excluded.is_amort,
                buyback_date       = excluded.buyback_date,
                bond_subtype       = excluded.bond_subtype,
                coupon_freq        = excluded.coupon_freq,
                coupon_pct         = excluded.coupon_pct,
                coupon_value       = excluded.coupon_value,
                nkd                = excluded.nkd,
                currency           = excluded.currency,
                is_qual            = excluded.is_qual,
                matdate            = excluded.matdate,
                list_level         = excluded.list_level,
                open_price         = excluded.open_price,
                updated_at         = excluded.updated_at
        """, {**data, "updated_at": now})


def get_bond(secid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bonds WHERE secid = ?", (secid,)).fetchone()
        return dict(row) if row else None


def get_all_bonds() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM bonds ORDER BY shortname").fetchall()
        return [dict(r) for r in rows]


# ── portfolio ──────────────────────────────────────────────────────────────

def upsert_portfolio(secid: str, broker: str = "", qty: int = 0,
                     rating: str = "", sector: str = "") -> None:
    """Создаёт или обновляет запись в portfolio."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO portfolio (secid, broker, qty, rating, sector, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(secid) DO UPDATE SET
                broker     = excluded.broker,
                qty        = excluded.qty,
                rating     = excluded.rating,
                sector     = excluded.sector,
                updated_at = excluded.updated_at
        """, (secid, broker, qty, rating, sector, now))


def get_portfolio() -> list[dict]:
    """Возвращает портфель с джойном данных из bonds."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                p.id, p.secid, p.broker, p.qty, p.rating, p.sector,
                p.created_at, p.updated_at,
                b.isin, b.shortname, b.name, b.boardid,
                b.price, b.face_value, b.cost, b.yield_value,
                b.days_to_redemption, b.is_amort, b.buyback_date,
                b.bond_subtype, b.coupon_freq, b.coupon_pct, b.coupon_value, b.nkd,
                b.currency, b.is_qual, b.matdate, b.list_level, b.open_price,
                b.updated_at AS bond_updated_at
            FROM portfolio p
            LEFT JOIN bonds b ON b.secid = p.secid
            ORDER BY b.shortname
        """).fetchall()
        return [dict(r) for r in rows]


def delete_portfolio_item(secid: str) -> bool:
    """Удаляет бумагу из портфеля (bonds не трогает)."""
    with _write_lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM portfolio WHERE secid = ?", (secid,))
        return cur.rowcount > 0


# ── Курсы валют ────────────────────────────────────────────────────────────

def upsert_exchange_rates(date: str, usd: float, eur: float, cny: float) -> None:
    """Сохраняет курсы валют на дату."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO exchange_rates (date, usd, eur, cny, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(date) DO UPDATE SET
                usd        = excluded.usd,
                eur        = excluded.eur,
                cny        = excluded.cny,
                updated_at = excluded.updated_at
        """, (date, usd, eur, cny))
        conn.commit()


def get_exchange_rates(date: str) -> dict | None:
    """Возвращает курсы на дату или None если нет данных."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT date, usd, eur, cny FROM exchange_rates WHERE date = ?",
            (date,)
        ).fetchone()
    return dict(row) if row else None


def get_latest_exchange_rates() -> dict | None:
    """Возвращает последние доступные курсы."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT date, usd, eur, cny FROM exchange_rates ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None