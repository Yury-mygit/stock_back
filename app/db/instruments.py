from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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