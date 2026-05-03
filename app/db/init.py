"""
Пакет db — работа с базой данных SQLite.

Импортируй нужные функции напрямую из подмодулей или через этот __init__.
"""
from __future__ import annotations

import sqlite3
import threading

from app import config

_write_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Создаёт все таблицы если их нет. Вызывается при старте."""
    from app.db.schema import create_tables
    create_tables()


# Реэкспорт всех публичных функций для обратной совместимости
from app.db.emitents import (
    upsert_emitent,
    get_emitent_by_name,
    get_emitent_by_id,
    get_emitent_instruments,
    rename_emitent,
    merge_emitents,
)
from app.db.instruments import (
    upsert_bond, get_bond_by_secid,
    upsert_stock, get_stock_by_secid,
    upsert_fund, get_fund_by_secid,
)
from app.db.screener import (
    upsert_screener_bonds_batch,
    get_screener_bonds_count,
    query_screener_bonds,
    sync_screener_to_bonds,
    sync_screener_batch_to_bonds,
)
from app.db.portfolio import (
    upsert_portfolio,
    delete_portfolio_item,
    get_portfolio_with_instruments,
)
from app.db.ratings import (
    add_rating,
    set_raexpert_url,
    get_latest_rating,
    get_rating_history,
    get_all_emitents_for_ratings,
)
from app.db.bondization import (
    has_bondization,
    save_bond_coupons,
    save_bond_amortizations,
    get_yield_calendar,
)
from app.db.exchange import (
    upsert_exchange_rates,
    get_exchange_rates,
    get_latest_exchange_rates,
)

__all__ = [
    "get_conn", "_write_lock", "init_db",
    "upsert_emitent", "get_emitent_by_name", "get_emitent_by_id",
    "get_emitent_instruments", "rename_emitent", "merge_emitents",
    "upsert_bond", "get_bond_by_secid",
    "upsert_stock", "get_stock_by_secid",
    "upsert_fund", "get_fund_by_secid",
    "upsert_screener_bonds_batch", "get_screener_bonds_count",
    "query_screener_bonds", "sync_screener_to_bonds", "sync_screener_batch_to_bonds",
    "upsert_portfolio", "delete_portfolio_item", "get_portfolio_with_instruments",
    "add_rating", "set_raexpert_url", "get_latest_rating",
    "get_rating_history", "get_all_emitents_for_ratings",
    "has_bondization", "save_bond_coupons", "save_bond_amortizations",
    "get_yield_calendar",
    "upsert_exchange_rates", "get_exchange_rates", "get_latest_exchange_rates",
]