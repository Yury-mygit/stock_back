from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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