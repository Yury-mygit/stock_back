"""
Схема базы данных — создание всех таблиц.
"""
from __future__ import annotations

from app import config
from app.db import get_conn
from app.logger import logger


def create_tables() -> None:
    """Создаёт все таблицы если их нет. Вызывается при старте."""
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

            -- Купоны облигаций
            CREATE TABLE IF NOT EXISTS bond_coupons (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                secid       TEXT NOT NULL,
                coupon_date TEXT NOT NULL,
                value       REAL,
                value_rub   REAL,
                face_value  REAL,
                is_paid     INTEGER DEFAULT 0,
                UNIQUE(secid, coupon_date)
            );
            CREATE INDEX IF NOT EXISTS idx_bond_coupons_secid ON bond_coupons(secid);

            -- Амортизации и погашения облигаций
            CREATE TABLE IF NOT EXISTS bond_amortizations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                secid       TEXT NOT NULL,
                pay_date    TEXT NOT NULL,
                value       REAL,
                value_pct   REAL,
                face_value  REAL,
                data_source TEXT,
                is_paid     INTEGER DEFAULT 0,
                UNIQUE(secid, pay_date, data_source)
            );
            CREATE INDEX IF NOT EXISTS idx_bond_amort_secid ON bond_amortizations(secid);

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

            -- Пользовательские корректировки купонов.
            -- Хранит фактические данные отдельно от плановых (MOEX).
            -- Выживает при пересинхронизации bond_coupons.
            CREATE TABLE IF NOT EXISTS coupon_overrides (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                secid        TEXT NOT NULL,
                coupon_date  TEXT NOT NULL,   -- плановая дата = ключ связи
                is_paid      INTEGER,         -- 1=подтверждён, 0=не получен
                actual_date  TEXT,            -- фактическая дата (если перенесён)
                actual_value REAL,            -- фактическая сумма (если скорректирована)
                updated_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(secid, coupon_date)
            );
            CREATE INDEX IF NOT EXISTS idx_coupon_overrides ON coupon_overrides(secid, coupon_date);
        """)
    logger.info("База данных инициализирована: %s", config.DB_PATH)