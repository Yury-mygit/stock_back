from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
from app.db.exchange import get_latest_exchange_rates

# ── Купоны и амортизации ───────────────────────────────────────────────────

def has_bondization(secid: str) -> bool:
    """Проверяет есть ли уже расписание для бумаги."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM bond_coupons WHERE secid = ? LIMIT 1", (secid,)
        ).fetchone()
    return row is not None


def save_bond_coupons(secid: str, rows: list[dict]) -> int:
    """Сохраняет купоны. Пропускает уже существующие (INSERT OR IGNORE).
    Возвращает количество вставленных строк.
    """
    today = datetime.now().date().isoformat()
    records = []
    for r in rows:
        coupon_date = r.get("coupon_date")
        if not coupon_date:
            continue
        records.append({
            "secid":       secid,
            "coupon_date": coupon_date,
            "value":       r.get("value"),
            "value_rub":   r.get("value_rub"),
            "face_value":  r.get("face_value"),
            "is_paid":     1 if coupon_date <= today else 0,
        })

    if not records:
        return 0

    with _write_lock, get_conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO bond_coupons
                (secid, coupon_date, value, value_rub, face_value, is_paid)
            VALUES
                (:secid, :coupon_date, :value, :value_rub, :face_value, :is_paid)
        """, records)
        inserted = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
    return inserted


def save_bond_amortizations(secid: str, rows: list[dict]) -> int:
    """Сохраняет амортизации/погашения. Пропускает уже существующие.
    Возвращает количество вставленных строк.
    """
    today = datetime.now().date().isoformat()
    records = []
    for r in rows:
        pay_date = r.get("pay_date")
        if not pay_date:
            continue
        records.append({
            "secid":       secid,
            "pay_date":    pay_date,
            "value":       r.get("value"),
            "value_pct":   r.get("value_pct"),
            "face_value":  r.get("face_value"),
            "data_source": r.get("data_source"),
            "is_paid":     1 if pay_date <= today else 0,
        })

    if not records:
        return 0

    with _write_lock, get_conn() as conn:
        conn.executemany("""
            INSERT OR IGNORE INTO bond_amortizations
                (secid, pay_date, value, value_pct, face_value, data_source, is_paid)
            VALUES
                (:secid, :pay_date, :value, :value_pct, :face_value, :data_source, :is_paid)
        """, records)
        inserted = conn.execute("SELECT changes()").fetchone()[0]
        conn.commit()
    return inserted


def get_yield_calendar(months: int = 12) -> list[dict]:
    """Возвращает будущие выплаты по облигациям из портфеля,
    сгруппированные по месяцам. Учитывает qty из portfolio и курсы валют.
    """
    today     = datetime.now().date().isoformat()
    from datetime import date as dt_date, timedelta
    end_date  = (dt_date.today().replace(day=1))
    # Вычисляем конечную дату периода
    y = dt_date.today().year + (dt_date.today().month - 1 + months) // 12
    m = (dt_date.today().month - 1 + months) % 12 + 1
    end_date  = dt_date(y, m, 1).isoformat()

    rates = get_latest_exchange_rates() or {}
    usd_rate = rates.get("usd", 1.0) or 1.0
    eur_rate = rates.get("eur", 1.0) or 1.0
    cny_rate = rates.get("cny", 1.0) or 1.0

    def to_rub(value, currency):
        if value is None:
            return None
        cur = (currency or "RUB").upper()
        if cur in ("SUR", "RUB"):
            return value
        if cur == "USD":
            return value * usd_rate
        if cur == "EUR":
            return value * eur_rate
        if cur == "CNY":
            return value * cny_rate
        return value

    with get_conn() as conn:
        # Купоны
        coupons = conn.execute("""
            SELECT
                bc.secid, bc.coupon_date AS pay_date, 'coupon' AS pay_type,
                bc.value, bc.value_rub, bc.face_value,
                b.shortname, b.currency, b.emitent_id,
                p.qty,
                e.name AS emitent_name
            FROM bond_coupons bc
            JOIN bonds b ON b.secid = bc.secid
            JOIN portfolio p ON p.instrument_id = b.id AND p.instrument_type = 'bond'
            LEFT JOIN emitents e ON e.id = b.emitent_id
            WHERE bc.is_paid = 0
              AND bc.coupon_date >= ?
              AND bc.coupon_date <  ?
            ORDER BY bc.coupon_date
        """, (today, end_date)).fetchall()

        # Амортизации
        amorts = conn.execute("""
            SELECT
                ba.secid, ba.pay_date, ba.data_source AS pay_type,
                ba.value, ba.value AS value_rub, ba.face_value,
                b.shortname, b.currency, b.emitent_id,
                p.qty,
                e.name AS emitent_name
            FROM bond_amortizations ba
            JOIN bonds b ON b.secid = ba.secid
            JOIN portfolio p ON p.instrument_id = b.id AND p.instrument_type = 'bond'
            LEFT JOIN emitents e ON e.id = b.emitent_id
            WHERE ba.is_paid = 0
              AND ba.pay_date >= ?
              AND ba.pay_date <  ?
            ORDER BY ba.pay_date
        """, (today, end_date)).fetchall()

    # Группируем по месяцам
    months_dict: dict = {}
    for row in list(coupons) + list(amorts):
        d      = dict(row)
        month  = d["pay_date"][:7]  # "2026-04"
        value_rub = to_rub(d.get("value"), d.get("currency"))
        total_rub = round(value_rub * d["qty"], 2) if value_rub is not None else None

        if month not in months_dict:
            months_dict[month] = {"month": month, "total": 0.0, "items": []}

        months_dict[month]["items"].append({
            "secid":       d["secid"],
            "shortname":   d.get("shortname"),
            "emitent_name": d.get("emitent_name"),
            "pay_date":    d["pay_date"],
            "pay_type":    d["pay_type"],
            "value":       d.get("value"),
            "value_rub":   value_rub,
            "total_rub":   total_rub,
            "qty":         d["qty"],
            "currency":    d.get("currency"),
        })
        if total_rub is not None:
            months_dict[month]["total"] = round(
                months_dict[month]["total"] + total_rub, 2
            )

    return sorted(months_dict.values(), key=lambda x: x["month"])