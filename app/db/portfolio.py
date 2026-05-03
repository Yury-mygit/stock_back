from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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

def change_portfolio_qty(instrument_type: str, instrument_id: int, delta: int) -> dict | None:
    """Изменяет qty на delta. Возвращает новое состояние или None если не найдено."""
    with _write_lock, get_conn() as conn:
        row = conn.execute("""
            SELECT id, qty FROM portfolio
            WHERE instrument_type = ? AND instrument_id = ?
        """, (instrument_type, instrument_id)).fetchone()

        if not row:
            return None

        new_qty = row["qty"] + delta

        if new_qty < 0:
            return {"qty": new_qty}  # вернём отрицательное — контроллер отклонит

        conn.execute("""
            UPDATE portfolio SET qty = ?, updated_at = datetime('now')
            WHERE id = ?
        """, (new_qty, row["id"]))
        conn.commit()
        return {"qty": new_qty}