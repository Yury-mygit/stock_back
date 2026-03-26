"""
Работа с базой данных SQLite.
Все таблицы создаются здесь. CRUD функции по таблицам.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import logger

_write_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Создаёт таблицы если их нет. Вызывается при старте приложения."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            -- Эмитенты
            CREATE TABLE IF NOT EXISTS emitents (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                inn        TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_emitents_name ON emitents(name);

            -- Сырой буфер с биржи
            CREATE TABLE IF NOT EXISTS screener_bonds (
                secid           TEXT PRIMARY KEY,
                isin            TEXT,
                instrument_type TEXT DEFAULT 'bond',
                exchange        TEXT DEFAULT 'moex',
                shortname       TEXT,
                secname         TEXT,
                emitent_id      INTEGER REFERENCES emitents(id),
                face_value      REAL,
                face_unit       TEXT,
                matdate         TEXT,
                buyback_date    TEXT,
                list_level      INTEGER,
                coupon_pct      REAL,
                coupon_value    REAL,
                nkd             REAL,
                issue_size      INTEGER,
                prev_price      REAL,
                yield_value     REAL,
                coupon_freq     INTEGER,
                bond_type       TEXT,
                bond_subtype    TEXT,
                sector_id       TEXT,
                duration        INTEGER,
                currency        TEXT,
                updated_at      TEXT DEFAULT (datetime('now'))
            );

            -- Облигации (актуальные данные по запросу к MOEX)
            CREATE TABLE IF NOT EXISTS bonds (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                secid              TEXT NOT NULL UNIQUE,
                isin               TEXT,
                emitent_id         INTEGER REFERENCES emitents(id),
                shortname          TEXT,
                boardid            TEXT,
                price              REAL,
                face_value         REAL,
                cost               REAL,
                yield_value        REAL,
                days_to_redemption INTEGER,
                is_amort           INTEGER DEFAULT 0,
                buyback_date       TEXT,
                bond_subtype       TEXT,
                coupon_freq        INTEGER,
                coupon_pct         REAL,
                coupon_value       REAL,
                nkd                REAL,
                currency           TEXT,
                is_qual            INTEGER DEFAULT 0,
                matdate            TEXT,
                list_level         INTEGER,
                open_price         REAL,
                updated_at         TEXT
            );

            -- Акции
            CREATE TABLE IF NOT EXISTS stocks (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                secid      TEXT NOT NULL UNIQUE,
                isin       TEXT,
                emitent_id INTEGER REFERENCES emitents(id),
                shortname  TEXT,
                boardid    TEXT,
                price      REAL,
                open_price REAL,
                cost       REAL,
                currency   TEXT,
                is_qual    INTEGER DEFAULT 0,
                lot_size   INTEGER,
                updated_at TEXT
            );

            -- Фонды / ETF
            CREATE TABLE IF NOT EXISTS funds (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                secid      TEXT NOT NULL UNIQUE,
                isin       TEXT,
                emitent_id INTEGER REFERENCES emitents(id),
                shortname  TEXT,
                boardid    TEXT,
                price      REAL,
                open_price REAL,
                cost       REAL,
                currency   TEXT,
                is_qual    INTEGER DEFAULT 0,
                fund_type  TEXT,
                updated_at TEXT
            );

            -- Портфель — полиморфная связь
            CREATE TABLE IF NOT EXISTS portfolio (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_type TEXT NOT NULL CHECK(instrument_type IN ('bond','stock','fund')),
                instrument_id   INTEGER NOT NULL,
                broker          TEXT,
                qty             INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(instrument_type, instrument_id)
            );

            -- Рейтинги — история в одной таблице
            CREATE TABLE IF NOT EXISTS ratings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                emitent_id   INTEGER NOT NULL REFERENCES emitents(id),
                rating       TEXT,
                agency       TEXT,
                raexpert_url TEXT,
                under_watch  INTEGER DEFAULT 0,
                forecast     TEXT,
                rating_date  TEXT,
                updated_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_ratings_emitent ON ratings(emitent_id, rating_date);

            -- Курсы валют
            CREATE TABLE IF NOT EXISTS exchange_rates (
                date       TEXT PRIMARY KEY,
                usd        REAL,
                eur        REAL,
                cny        REAL,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- Секторы
            CREATE TABLE IF NOT EXISTS sectors (
                sector_id  TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
    logger.info("База данных инициализирована: %s", config.DB_PATH)


# ── Эмитенты ───────────────────────────────────────────────────────────────

def upsert_emitent(name: str, inn: str | None = None) -> int:
    """Создаёт или возвращает id эмитента по имени."""
    with _write_lock, get_conn() as conn:
        row = conn.execute("SELECT id FROM emitents WHERE name = ?", (name,)).fetchone()
        if row:
            if inn:
                conn.execute(
                    "UPDATE emitents SET inn=?, updated_at=datetime('now') WHERE id=?",
                    (inn, row["id"])
                )
                conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO emitents (name, inn) VALUES (?, ?)", (name, inn)
        )
        conn.commit()
        return cur.lastrowid


def get_emitent_by_name(name: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM emitents WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def get_emitent_by_id(emitent_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM emitents WHERE id = ?", (emitent_id,)).fetchone()
    return dict(row) if row else None


# ── Screener bonds (буфер) ─────────────────────────────────────────────────

def upsert_screener_bonds_batch(rows: list[dict]) -> None:
    """Сохраняет батч бумаг в буфер скринера."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.executemany("""
            INSERT INTO screener_bonds (
                secid, isin, instrument_type, exchange, shortname, secname,
                emitent_id, face_value, face_unit, matdate, buyback_date,
                list_level, coupon_pct, coupon_value, nkd, issue_size,
                prev_price, yield_value, coupon_freq,
                bond_type, bond_subtype, sector_id, duration, currency,
                updated_at
            ) VALUES (
                :secid, :isin, :instrument_type, :exchange, :shortname, :secname,
                :emitent_id, :face_value, :face_unit, :matdate, :buyback_date,
                :list_level, :coupon_pct, :coupon_value, :nkd, :issue_size,
                :prev_price, :yield_value, :coupon_freq,
                :bond_type, :bond_subtype, :sector_id, :duration, :currency,
                :updated_at
            )
            ON CONFLICT(secid) DO UPDATE SET
                isin           = excluded.isin,
                shortname      = excluded.shortname,
                secname        = excluded.secname,
                emitent_id     = excluded.emitent_id,
                face_value     = excluded.face_value,
                face_unit      = excluded.face_unit,
                matdate        = excluded.matdate,
                buyback_date   = excluded.buyback_date,
                list_level     = excluded.list_level,
                coupon_pct     = excluded.coupon_pct,
                coupon_value   = excluded.coupon_value,
                nkd            = excluded.nkd,
                issue_size     = excluded.issue_size,
                prev_price     = excluded.prev_price,
                yield_value    = excluded.yield_value,
                coupon_freq    = excluded.coupon_freq,
                bond_type      = excluded.bond_type,
                bond_subtype   = excluded.bond_subtype,
                sector_id      = excluded.sector_id,
                duration       = excluded.duration,
                currency       = excluded.currency,
                updated_at     = excluded.updated_at
        """, [{**r, "updated_at": now,
               "instrument_type": r.get("instrument_type", "bond"),
               "exchange":        r.get("exchange", "moex"),
               "secname":         r.get("secname"),
               } for r in rows])
        conn.commit()


def get_screener_bonds_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM screener_bonds").fetchone()[0]


def query_screener_bonds(filters: dict, page: int = 1, page_size: int = 50) -> dict:
    """Запрос облигаций из буфера с фильтрацией."""
    conditions = ["sb.instrument_type = 'bond'"]
    params: list = []

    if filters.get("yield_min") is not None:
        conditions.append("sb.yield_value >= ?"); params.append(filters["yield_min"])
    if filters.get("yield_max") is not None:
        conditions.append("sb.yield_value <= ?"); params.append(filters["yield_max"])
    if filters.get("coupon_min") is not None:
        conditions.append("sb.coupon_pct >= ?");  params.append(filters["coupon_min"])
    if filters.get("coupon_max") is not None:
        conditions.append("sb.coupon_pct <= ?");  params.append(filters["coupon_max"])
    if filters.get("dur_min") is not None:
        conditions.append("sb.duration >= ?");    params.append(filters["dur_min"])
    if filters.get("dur_max") is not None:
        conditions.append("sb.duration <= ?");    params.append(filters["dur_max"])
    if filters.get("currency"):
        conditions.append("sb.currency = ?");     params.append(filters["currency"])
    if filters.get("list_level") is not None:
        conditions.append("sb.list_level <= ?");  params.append(filters["list_level"])
    if filters.get("has_offer"):
        conditions.append("sb.buyback_date IS NOT NULL AND sb.buyback_date != ''")
    if filters.get("sector_id"):
        conditions.append("sb.sector_id = ?");    params.append(filters["sector_id"])
    if filters.get("years_min") is not None:
        conditions.append("sb.matdate IS NOT NULL AND sb.matdate != '0000-00-00'")
        conditions.append("CAST((julianday(sb.matdate)-julianday('now'))/365 AS REAL) >= ?")
        params.append(filters["years_min"])
    if filters.get("years_max") is not None:
        conditions.append("sb.matdate IS NOT NULL AND sb.matdate != '0000-00-00'")
        conditions.append("CAST((julianday(sb.matdate)-julianday('now'))/365 AS REAL) <= ?")
        params.append(filters["years_max"])

    where  = "WHERE " + " AND ".join(conditions)
    offset = (page - 1) * page_size

    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM screener_bonds sb {where}", params
        ).fetchone()[0]
        rows = conn.execute(f"""
            SELECT sb.*, e.name AS emitent_name,
                   r.rating AS rating, r.agency AS rating_agency
            FROM screener_bonds sb
            LEFT JOIN emitents e ON e.id = sb.emitent_id
            LEFT JOIN ratings r ON r.emitent_id = sb.emitent_id
                AND r.rating_date = (
                    SELECT MAX(r2.rating_date) FROM ratings r2
                    WHERE r2.emitent_id = sb.emitent_id
                )
            {where}
            ORDER BY sb.yield_value DESC NULLS LAST
            LIMIT ? OFFSET ?
        """, params + [page_size, offset]).fetchall()

    return {
        "total": total,
        "page":  page,
        "pages": (total + page_size - 1) // page_size,
        "items": [dict(r) for r in rows],
    }


def sync_screener_to_bonds() -> dict:
    """Синхронизирует screener_bonds → bonds.
    INSERT новых + UPDATE существующих (кроме price/cost — они из прямого запроса MOEX).
    Возвращает {inserted, updated}.
    """
    with _write_lock, get_conn() as conn:
        # INSERT новых (которых ещё нет в bonds)
        conn.execute("""
            INSERT OR IGNORE INTO bonds (
                secid, isin, emitent_id, shortname,
                face_value, matdate, buyback_date, list_level,
                coupon_pct, coupon_value, coupon_freq,
                bond_subtype, currency, updated_at
            )
            SELECT
                sb.secid, sb.isin, sb.emitent_id, sb.shortname,
                sb.face_value, sb.matdate, sb.buyback_date, sb.list_level,
                sb.coupon_pct, sb.coupon_value, sb.coupon_freq,
                sb.bond_subtype, sb.currency, datetime('now')
            FROM screener_bonds sb
            WHERE sb.instrument_type = 'bond'
        """)
        inserted = conn.execute("SELECT changes()").fetchone()[0]

        # UPDATE существующих (не трогаем price, cost, nkd, yield_value — они из MOEX напрямую)
        conn.execute("""
            UPDATE bonds SET
                isin         = (SELECT isin         FROM screener_bonds WHERE secid = bonds.secid),
                shortname    = (SELECT shortname    FROM screener_bonds WHERE secid = bonds.secid),
                emitent_id   = (SELECT emitent_id   FROM screener_bonds WHERE secid = bonds.secid),
                face_value   = (SELECT face_value   FROM screener_bonds WHERE secid = bonds.secid),
                matdate      = (SELECT matdate      FROM screener_bonds WHERE secid = bonds.secid),
                buyback_date = (SELECT buyback_date FROM screener_bonds WHERE secid = bonds.secid),
                list_level   = (SELECT list_level   FROM screener_bonds WHERE secid = bonds.secid),
                coupon_pct   = (SELECT coupon_pct   FROM screener_bonds WHERE secid = bonds.secid),
                coupon_value = (SELECT coupon_value FROM screener_bonds WHERE secid = bonds.secid),
                coupon_freq  = (SELECT coupon_freq  FROM screener_bonds WHERE secid = bonds.secid),
                bond_subtype = (SELECT bond_subtype FROM screener_bonds WHERE secid = bonds.secid),
                currency     = (SELECT currency     FROM screener_bonds WHERE secid = bonds.secid),
                updated_at   = datetime('now')
            WHERE secid IN (SELECT secid FROM screener_bonds WHERE instrument_type = 'bond')
        """)
        updated = conn.execute("SELECT changes()").fetchone()[0] - inserted
        conn.commit()

    return {"inserted": inserted, "updated": max(updated, 0)}


# ── Bonds (облигации) ──────────────────────────────────────────────────────

def upsert_bond(data: dict) -> int:
    """Создаёт или обновляет облигацию. Возвращает id."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO bonds (
                secid, isin, emitent_id, shortname, boardid,
                price, face_value, cost, yield_value, days_to_redemption,
                is_amort, buyback_date, bond_subtype, coupon_freq,
                coupon_pct, coupon_value, nkd, currency, is_qual,
                matdate, list_level, open_price, updated_at
            ) VALUES (
                :secid, :isin, :emitent_id, :shortname, :boardid,
                :price, :face_value, :cost, :yield_value, :days_to_redemption,
                :is_amort, :buyback_date, :bond_subtype, :coupon_freq,
                :coupon_pct, :coupon_value, :nkd, :currency, :is_qual,
                :matdate, :list_level, :open_price, :updated_at
            )
            ON CONFLICT(secid) DO UPDATE SET
                isin               = excluded.isin,
                emitent_id         = excluded.emitent_id,
                shortname          = excluded.shortname,
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
        conn.commit()
        row = conn.execute("SELECT id FROM bonds WHERE secid = ?", (data["secid"],)).fetchone()
    return row["id"]


def get_bond_by_secid(secid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM bonds WHERE secid = ?", (secid,)).fetchone()
    return dict(row) if row else None


# ── Stocks (акции) ─────────────────────────────────────────────────────────

def upsert_stock(data: dict) -> int:
    """Создаёт или обновляет акцию. Возвращает id."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO stocks (
                secid, isin, emitent_id, shortname, boardid,
                price, open_price, cost, currency, is_qual, lot_size, updated_at
            ) VALUES (
                :secid, :isin, :emitent_id, :shortname, :boardid,
                :price, :open_price, :cost, :currency, :is_qual, :lot_size, :updated_at
            )
            ON CONFLICT(secid) DO UPDATE SET
                isin       = excluded.isin,
                emitent_id = excluded.emitent_id,
                shortname  = excluded.shortname,
                boardid    = excluded.boardid,
                price      = excluded.price,
                open_price = excluded.open_price,
                cost       = excluded.cost,
                currency   = excluded.currency,
                is_qual    = excluded.is_qual,
                lot_size   = excluded.lot_size,
                updated_at = excluded.updated_at
        """, {**data, "updated_at": now})
        conn.commit()
        row = conn.execute("SELECT id FROM stocks WHERE secid = ?", (data["secid"],)).fetchone()
    return row["id"]


def get_stock_by_secid(secid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM stocks WHERE secid = ?", (secid,)).fetchone()
    return dict(row) if row else None


# ── Funds (фонды) ──────────────────────────────────────────────────────────

def upsert_fund(data: dict) -> int:
    """Создаёт или обновляет фонд. Возвращает id."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO funds (
                secid, isin, emitent_id, shortname, boardid,
                price, open_price, cost, currency, is_qual, fund_type, updated_at
            ) VALUES (
                :secid, :isin, :emitent_id, :shortname, :boardid,
                :price, :open_price, :cost, :currency, :is_qual, :fund_type, :updated_at
            )
            ON CONFLICT(secid) DO UPDATE SET
                isin       = excluded.isin,
                emitent_id = excluded.emitent_id,
                shortname  = excluded.shortname,
                boardid    = excluded.boardid,
                price      = excluded.price,
                open_price = excluded.open_price,
                cost       = excluded.cost,
                currency   = excluded.currency,
                is_qual    = excluded.is_qual,
                fund_type  = excluded.fund_type,
                updated_at = excluded.updated_at
        """, {**data, "updated_at": now})
        conn.commit()
        row = conn.execute("SELECT id FROM funds WHERE secid = ?", (data["secid"],)).fetchone()
    return row["id"]


def get_fund_by_secid(secid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM funds WHERE secid = ?", (secid,)).fetchone()
    return dict(row) if row else None


# ── Portfolio ──────────────────────────────────────────────────────────────

def upsert_portfolio(instrument_type: str, instrument_id: int,
                     broker: str | None, qty: int) -> None:
    """Добавляет или обновляет запись портфеля."""
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO portfolio (instrument_type, instrument_id, broker, qty, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(instrument_type, instrument_id) DO UPDATE SET
                broker     = excluded.broker,
                qty        = excluded.qty,
                updated_at = excluded.updated_at
        """, (instrument_type, instrument_id, broker, qty))
        conn.commit()


def delete_portfolio_item(instrument_type: str, instrument_id: int) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute(
            "DELETE FROM portfolio WHERE instrument_type=? AND instrument_id=?",
            (instrument_type, instrument_id)
        )
        conn.commit()


def get_portfolio_with_instruments() -> list[dict]:
    """Возвращает все позиции портфеля с данными инструментов и эмитентов."""
    with get_conn() as conn:
        # Облигации
        bonds = conn.execute("""
            SELECT p.id, p.instrument_type, p.instrument_id, p.broker, p.qty,
                   b.secid, b.isin, b.shortname, b.boardid,
                   b.price, b.face_value, b.cost, b.yield_value,
                   b.days_to_redemption, b.is_amort, b.buyback_date,
                   b.bond_subtype, b.coupon_freq, b.coupon_pct, b.coupon_value,
                   b.nkd, b.currency, b.is_qual, b.matdate, b.list_level,
                   b.open_price, b.updated_at,
                   e.name AS emitent_name,
                   r.rating, r.agency AS rating_agency
            FROM portfolio p
            JOIN bonds b ON b.id = p.instrument_id
            LEFT JOIN emitents e ON e.id = b.emitent_id
            LEFT JOIN ratings r ON r.emitent_id = b.emitent_id
                AND r.rating_date = (
                    SELECT MAX(r2.rating_date) FROM ratings r2
                    WHERE r2.emitent_id = b.emitent_id
                )
            WHERE p.instrument_type = 'bond'
        """).fetchall()

        # Акции
        stocks = conn.execute("""
            SELECT p.id, p.instrument_type, p.instrument_id, p.broker, p.qty,
                   s.secid, s.isin, s.shortname, s.boardid,
                   s.price, s.open_price, s.cost, s.currency, s.is_qual, s.lot_size,
                   s.updated_at,
                   e.name AS emitent_name,
                   r.rating, r.agency AS rating_agency
            FROM portfolio p
            JOIN stocks s ON s.id = p.instrument_id
            LEFT JOIN emitents e ON e.id = s.emitent_id
            LEFT JOIN ratings r ON r.emitent_id = s.emitent_id
                AND r.rating_date = (
                    SELECT MAX(r2.rating_date) FROM ratings r2
                    WHERE r2.emitent_id = s.emitent_id
                )
            WHERE p.instrument_type = 'stock'
        """).fetchall()

        # Фонды
        funds = conn.execute("""
            SELECT p.id, p.instrument_type, p.instrument_id, p.broker, p.qty,
                   f.secid, f.isin, f.shortname, f.boardid,
                   f.price, f.open_price, f.cost, f.currency, f.is_qual, f.fund_type,
                   f.updated_at,
                   e.name AS emitent_name
            FROM portfolio p
            JOIN funds f ON f.id = p.instrument_id
            LEFT JOIN emitents e ON e.id = f.emitent_id
            WHERE p.instrument_type = 'fund'
        """).fetchall()

    result = []
    for row in list(bonds) + list(stocks) + list(funds):
        result.append(dict(row))
    return result


# ── Ratings ────────────────────────────────────────────────────────────────

def add_rating(emitent_id: int, rating: str, agency: str,
               raexpert_url: str | None = None,
               under_watch: int = 0,
               forecast: str | None = None,
               rating_date: str | None = None) -> None:
    """Добавляет новую запись рейтинга (история накапливается)."""
    with _write_lock, get_conn() as conn:
        # Не дублируем если такая же запись уже есть
        exists = conn.execute("""
            SELECT 1 FROM ratings
            WHERE emitent_id=? AND rating=? AND rating_date=?
        """, (emitent_id, rating, rating_date)).fetchone()
        if not exists:
            conn.execute("""
                INSERT INTO ratings
                    (emitent_id, rating, agency, raexpert_url,
                     under_watch, forecast, rating_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (emitent_id, rating, agency, raexpert_url,
                  under_watch, forecast, rating_date))
            conn.commit()


def set_raexpert_url(emitent_id: int, url: str) -> None:
    """Сохраняет ссылку raexpert — обновляет последнюю запись или создаёт заглушку."""
    with _write_lock, get_conn() as conn:
        # Обновляем url во всех записях этого эмитента
        conn.execute("""
            UPDATE ratings SET raexpert_url=?, updated_at=datetime('now')
            WHERE emitent_id=?
        """, (url, emitent_id))
        if conn.execute("SELECT changes()").fetchone()[0] == 0:
            # Нет записей — создаём пустую с url
            conn.execute("""
                INSERT INTO ratings (emitent_id, raexpert_url) VALUES (?, ?)
            """, (emitent_id, url))
        conn.commit()


def get_latest_rating(emitent_id: int) -> dict | None:
    """Возвращает последний рейтинг эмитента."""
    with get_conn() as conn:
        row = conn.execute("""
            SELECT * FROM ratings WHERE emitent_id=?
            ORDER BY rating_date DESC NULLS LAST, updated_at DESC
            LIMIT 1
        """, (emitent_id,)).fetchone()
    return dict(row) if row else None


def get_rating_history(emitent_id: int) -> list[dict]:
    """Возвращает всю историю рейтингов эмитента."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM ratings WHERE emitent_id=?
            ORDER BY rating_date DESC NULLS LAST, updated_at DESC
        """, (emitent_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_emitents_for_ratings(in_portfolio: bool = False) -> list[dict]:
    """Все эмитенты с последним рейтингом."""
    portfolio_filter = """
        AND EXISTS (
            SELECT 1 FROM bonds b JOIN portfolio p ON p.instrument_id=b.id
                AND p.instrument_type='bond' WHERE b.emitent_id=e.id
            UNION
            SELECT 1 FROM stocks s JOIN portfolio p ON p.instrument_id=s.id
                AND p.instrument_type='stock' WHERE s.emitent_id=e.id
        )
    """ if in_portfolio else ""

    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT
                e.id AS emitent_id, e.name, e.inn,
                r.id AS rating_row_id,
                r.rating, r.agency, r.raexpert_url,
                r.under_watch, r.forecast, r.rating_date,
                r.updated_at AS rating_updated_at,
                EXISTS (
                    SELECT 1 FROM bonds b JOIN portfolio p ON p.instrument_id=b.id
                        AND p.instrument_type='bond' WHERE b.emitent_id=e.id
                    UNION
                    SELECT 1 FROM stocks s JOIN portfolio p ON p.instrument_id=s.id
                        AND p.instrument_type='stock' WHERE s.emitent_id=e.id
                ) AS in_portfolio
            FROM emitents e
            LEFT JOIN ratings r ON r.emitent_id=e.id
                AND r.id = (
                    SELECT r2.id FROM ratings r2
                    WHERE r2.emitent_id=e.id
                    ORDER BY r2.rating_date DESC NULLS LAST, r2.updated_at DESC
                    LIMIT 1
                )
            WHERE 1=1 {portfolio_filter}
            ORDER BY e.name
        """).fetchall()
    return [dict(r) for r in rows]


# ── Курсы валют ────────────────────────────────────────────────────────────

def upsert_exchange_rates(date: str, usd: float, eur: float, cny: float) -> None:
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO exchange_rates (date, usd, eur, cny)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                usd=excluded.usd, eur=excluded.eur,
                cny=excluded.cny, updated_at=datetime('now')
        """, (date, usd, eur, cny))
        conn.commit()


def get_latest_exchange_rates() -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exchange_rates ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_exchange_rates(date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM exchange_rates WHERE date=?", (date,)
        ).fetchone()
    return dict(row) if row else None


# ── Управление эмитентами ──────────────────────────────────────────────────

def get_emitent_instruments(emitent_id: int) -> list[dict]:
    """Возвращает все бумаги эмитента из bonds, stocks, funds.
    emitent_name берётся из таблицы emitents, не из shortname бумаги.
    """
    with get_conn() as conn:
        emitent = conn.execute(
            "SELECT name FROM emitents WHERE id = ?", (emitent_id,)
        ).fetchone()
        emitent_name = emitent["name"] if emitent else None

        bonds = conn.execute("""
            SELECT 'bond' AS instrument_type, id, secid, isin, shortname, currency
            FROM bonds WHERE emitent_id = ?
        """, (emitent_id,)).fetchall()
        stocks = conn.execute("""
            SELECT 'stock' AS instrument_type, id, secid, isin, shortname, currency
            FROM stocks WHERE emitent_id = ?
        """, (emitent_id,)).fetchall()
        funds = conn.execute("""
            SELECT 'fund' AS instrument_type, id, secid, isin, shortname, currency
            FROM funds WHERE emitent_id = ?
        """, (emitent_id,)).fetchall()

    result = []
    for r in list(bonds) + list(stocks) + list(funds):
        d = dict(r)
        d["emitent_name"] = emitent_name
        result.append(d)
    return result


def rename_emitent(emitent_id: int, new_name: str) -> dict:
    """Переименовывает эмитента.
    Если имя уже занято — возвращает данные существующего для подтверждения.
    Возвращает: {action: 'renamed'|'conflict', target?: dict}
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM emitents WHERE name = ? AND id != ?",
            (new_name, emitent_id)
        ).fetchone()

    if existing:
        return {"action": "conflict", "target": dict(existing)}

    with _write_lock, get_conn() as conn:
        conn.execute(
            "UPDATE emitents SET name=?, updated_at=datetime('now') WHERE id=?",
            (new_name, emitent_id)
        )
        conn.commit()
    return {"action": "renamed"}


def merge_emitents(duplicate_id: int, target_id: int) -> dict:
    """Объединяет дубль в легитимного эмитента.
    - Переставляет emitent_id во всех таблицах с duplicate_id → target_id
    - Рейтинги дубля НЕ переносим (берём рейтинги легитимного)
    - Удаляем дубль из emitents
    Возвращает {bonds, stocks, funds, screener_bonds} — кол-во обновлённых записей.
    """
    with _write_lock, get_conn() as conn:
        # Обновляем ссылки во всех таблицах с инструментами
        conn.execute("UPDATE bonds SET emitent_id=? WHERE emitent_id=?",
                     (target_id, duplicate_id))
        b = conn.execute("SELECT changes()").fetchone()[0]

        conn.execute("UPDATE stocks SET emitent_id=? WHERE emitent_id=?",
                     (target_id, duplicate_id))
        s = conn.execute("SELECT changes()").fetchone()[0]

        conn.execute("UPDATE funds SET emitent_id=? WHERE emitent_id=?",
                     (target_id, duplicate_id))
        f = conn.execute("SELECT changes()").fetchone()[0]

        conn.execute("UPDATE screener_bonds SET emitent_id=? WHERE emitent_id=?",
                     (target_id, duplicate_id))
        sb = conn.execute("SELECT changes()").fetchone()[0]

        # Рейтинги дубля удаляем (берём у легитимного)
        conn.execute("DELETE FROM ratings WHERE emitent_id=?", (duplicate_id,))

        # Удаляем дубль
        conn.execute("DELETE FROM emitents WHERE id=?", (duplicate_id,))
        conn.commit()

    return {"bonds": b, "stocks": s, "funds": f, "screener_bonds": sb}


def sync_screener_batch_to_bonds(rows: list[dict]) -> dict:
    """После записи батча в screener_bonds — синхронизируем с bonds.
    Если secid есть в bonds → обновляем только изменяемые поля.
    Если нет → вставляем новую запись (без эмитента — emitent_id из screener_bonds).
    """
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    updated  = 0

    with _write_lock, get_conn() as conn:
        for row in rows:
            secid = row["secid"]
            exists = conn.execute(
                "SELECT id FROM bonds WHERE secid = ?", (secid,)
            ).fetchone()

            if exists:
                conn.execute("""
                    UPDATE bonds SET
                        prev_price   = :prev_price,
                        yield_value  = :yield_value,
                        nkd          = :nkd,
                        coupon_pct   = :coupon_pct,
                        coupon_value = :coupon_value,
                        buyback_date = :buyback_date,
                        matdate      = :matdate,
                        updated_at   = :updated_at
                    WHERE secid = :secid
                """, {
                    "secid":        secid,
                    "prev_price":   row.get("prev_price"),
                    "yield_value":  row.get("yield_value"),
                    "nkd":          row.get("nkd"),
                    "coupon_pct":   row.get("coupon_pct"),
                    "coupon_value": row.get("coupon_value"),
                    "buyback_date": row.get("buyback_date"),
                    "matdate":      row.get("matdate"),
                    "updated_at":   now,
                })
                updated += 1
            else:
                conn.execute("""
                    INSERT OR IGNORE INTO bonds (
                        secid, isin, emitent_id, shortname,
                        face_value, matdate, buyback_date, list_level,
                        coupon_pct, coupon_value, coupon_freq,
                        bond_subtype, currency,
                        prev_price, yield_value, nkd,
                        updated_at
                    ) VALUES (
                        :secid, :isin, :emitent_id, :shortname,
                        :face_value, :matdate, :buyback_date, :list_level,
                        :coupon_pct, :coupon_value, :coupon_freq,
                        :bond_subtype, :currency,
                        :prev_price, :yield_value, :nkd,
                        :updated_at
                    )
                """, {
                    "secid":        secid,
                    "isin":         row.get("isin"),
                    "emitent_id":   row.get("emitent_id"),
                    "shortname":    row.get("shortname"),
                    "face_value":   row.get("face_value"),
                    "matdate":      row.get("matdate"),
                    "buyback_date": row.get("buyback_date"),
                    "list_level":   row.get("list_level"),
                    "coupon_pct":   row.get("coupon_pct"),
                    "coupon_value": row.get("coupon_value"),
                    "coupon_freq":  row.get("coupon_freq"),
                    "bond_subtype": row.get("bond_subtype"),
                    "currency":     row.get("currency"),
                    "prev_price":   row.get("prev_price"),
                    "yield_value":  row.get("yield_value"),
                    "nkd":          row.get("nkd"),
                    "updated_at":   now,
                })
                inserted += 1

        conn.commit()

    return {"inserted": inserted, "updated": updated}