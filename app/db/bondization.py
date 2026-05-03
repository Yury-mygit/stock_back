from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
from app.db.exchange import get_latest_exchange_rates

# ── Купоны и амортизации ───────────────────────────────────────────────────

def has_bondization(secid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM bond_coupons WHERE secid = ? LIMIT 1", (secid,)
        ).fetchone()
    return row is not None


def save_bond_coupons(secid: str, rows: list[dict]) -> int:
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


# ── Пользовательские корректировки купонов ─────────────────────────────────

def upsert_coupon_override(secid: str, coupon_date: str,
                            is_paid: int | None = None,
                            actual_date: str | None = None,
                            actual_value: float | None = None) -> dict:
    """Сохраняет корректировку купона. Закрытие попапа = вызов этой функции."""
    now = datetime.now().isoformat(timespec="seconds")
    with _write_lock, get_conn() as conn:
        conn.execute("""
            INSERT INTO coupon_overrides (secid, coupon_date, is_paid, actual_date, actual_value, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(secid, coupon_date) DO UPDATE SET
                is_paid      = excluded.is_paid,
                actual_date  = excluded.actual_date,
                actual_value = excluded.actual_value,
                updated_at   = excluded.updated_at
        """, (secid, coupon_date, is_paid, actual_date, actual_value, now))
        conn.commit()
    return {
        "secid":        secid,
        "coupon_date":  coupon_date,
        "is_paid":      is_paid,
        "actual_date":  actual_date,
        "actual_value": actual_value,
    }


def get_coupon_override(secid: str, coupon_date: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT secid, coupon_date, is_paid, actual_date, actual_value, updated_at
            FROM coupon_overrides
            WHERE secid = ? AND coupon_date = ?
        """, (secid, coupon_date)).fetchone()
    return dict(row) if row else None


# ── Yield calendar ─────────────────────────────────────────────────────────

def get_yield_calendar(months: int = 12) -> list[dict]:
    today    = datetime.now().date().isoformat()
    from datetime import date as dt_date
    y        = dt_date.today().year + (dt_date.today().month - 1 + months) // 12
    m        = (dt_date.today().month - 1 + months) % 12 + 1
    end_date = dt_date(y, m, 1).isoformat()

    rates    = get_latest_exchange_rates() or {}
    usd_rate = rates.get("usd", 1.0) or 1.0
    eur_rate = rates.get("eur", 1.0) or 1.0
    cny_rate = rates.get("cny", 1.0) or 1.0

    def to_rub(value, currency):
        if value is None:
            return None
        cur = (currency or "RUB").upper()
        if cur in ("SUR", "RUB"): return value
        if cur == "USD":           return value * usd_rate
        if cur == "EUR":           return value * eur_rate
        if cur == "CNY":           return value * cny_rate
        return value

    with get_conn() as conn:
        coupons = conn.execute("""
            SELECT
                bc.secid, bc.coupon_date AS pay_date, 'coupon' AS pay_type,
                bc.value, bc.value_rub, bc.face_value,
                b.shortname, b.currency, b.emitent_id,
                p.qty, e.name AS emitent_name,
                co.is_paid      AS override_is_paid,
                co.actual_date  AS override_date,
                co.actual_value AS override_value
            FROM bond_coupons bc
            JOIN bonds b ON b.secid = bc.secid
            JOIN portfolio p ON p.instrument_id = b.id AND p.instrument_type = 'bond'
            LEFT JOIN emitents e ON e.id = b.emitent_id
            LEFT JOIN coupon_overrides co
                   ON co.secid = bc.secid AND co.coupon_date = bc.coupon_date
            WHERE bc.is_paid = 0
              AND bc.coupon_date >= ?
              AND bc.coupon_date <  ?
            ORDER BY bc.coupon_date
        """, (today, end_date)).fetchall()

        amorts = conn.execute("""
            SELECT
                ba.secid, ba.pay_date, ba.data_source AS pay_type,
                ba.value, ba.value AS value_rub, ba.face_value, ba.value_pct,
                b.shortname, b.currency, b.emitent_id,
                p.qty, e.name AS emitent_name,
                NULL AS override_is_paid,
                NULL AS override_date,
                NULL AS override_value
            FROM bond_amortizations ba
            JOIN bonds b ON b.secid = ba.secid
            JOIN portfolio p ON p.instrument_id = b.id AND p.instrument_type = 'bond'
            LEFT JOIN emitents e ON e.id = b.emitent_id
            WHERE ba.is_paid = 0
              AND ba.pay_date >= ?
              AND ba.pay_date <  ?
            ORDER BY ba.pay_date
        """, (today, end_date)).fetchall()

    months_dict: dict = {}
    for row in list(coupons) + list(amorts):
        d = dict(row)

        # Применяем оверрайд: фактическая дата или плановая
        pay_date       = d.get("override_date") or d["pay_date"]
        override_value = d.get("override_value")
        if override_value is not None:
            raw_value = override_value
            value_rub = override_value  # actual_value хранится в рублях
        else:
            raw_value = d.get("value")
            value_rub = to_rub(raw_value, d.get("currency"))
        total_rub = round(value_rub * d["qty"], 2) if value_rub is not None else None

        month = pay_date[:7]
        if month not in months_dict:
            months_dict[month] = {"month": month, "total": 0.0, "items": []}

        months_dict[month]["items"].append({
            "secid":           d["secid"],
            "shortname":       d.get("shortname"),
            "emitent_name":    d.get("emitent_name"),
            "pay_date":        pay_date,
            "planned_date":    d["pay_date"],  # исходная плановая дата
            "pay_type":        d["pay_type"],
            "value":           raw_value,
            "value_pct":       d.get("value_pct"),
            "value_rub":       value_rub,
            "total_rub":       total_rub,
            "qty":             d["qty"],
            "currency":        d.get("currency"),
            "override_is_paid": d.get("override_is_paid"),
        })
        if total_rub is not None:
            months_dict[month]["total"] = round(
                months_dict[month]["total"] + total_rub, 2
            )

    return sorted(months_dict.values(), key=lambda x: x["month"])


# ── Portfolio cashflow (для Calendar/Cashflow таблицы) ─────────────────────

def get_portfolio_cashflow() -> list[dict]:
    """Возвращает все купоны и амортизации по облигациям портфеля,
    сгруппированные по бумаге. Учитывает coupon_overrides:
    - actual_date  → позиция купона в таблице
    - actual_value → сумма выплаты
    - is_paid=1    → купон подтверждён (зелёная рамка на фронте)
    """
    rates    = get_latest_exchange_rates() or {}
    usd_rate = rates.get("usd", 1.0) or 1.0
    eur_rate = rates.get("eur", 1.0) or 1.0
    cny_rate = rates.get("cny", 1.0) or 1.0

    def to_rub(value, currency):
        if value is None:
            return None
        cur = (currency or "RUB").upper()
        if cur in ("SUR", "RUB"): return value
        if cur == "USD":           return value * usd_rate
        if cur == "EUR":           return value * eur_rate
        if cur == "CNY":           return value * cny_rate
        return value

    with get_conn() as conn:
        portfolio = conn.execute("""
            SELECT
                b.secid, b.shortname, b.matdate,
                b.face_value, b.currency,
                p.qty, p.broker,
                e.name AS emitent_name
            FROM portfolio p
            JOIN bonds b ON b.id = p.instrument_id
            LEFT JOIN emitents e ON e.id = b.emitent_id
            WHERE p.instrument_type = 'bond'
        """).fetchall()

        result = []
        for bond in portfolio:
            secid   = bond["secid"]
            qty     = bond["qty"] or 1
            matdate = bond["matdate"]
            cur     = bond["currency"] or "RUB"

            # Купоны с оверрайдами
            coupons = conn.execute("""
                SELECT
                    bc.coupon_date,
                    bc.value,
                    bc.value_rub,
                    co.is_paid      AS override_is_paid,
                    co.actual_date  AS override_date,
                    co.actual_value AS override_value
                FROM bond_coupons bc
                LEFT JOIN coupon_overrides co
                       ON co.secid = bc.secid AND co.coupon_date = bc.coupon_date
                WHERE bc.secid = ? AND bc.coupon_date IS NOT NULL
                ORDER BY bc.coupon_date
            """, (secid,)).fetchall()

            # Амортизации
            amorts = conn.execute("""
                SELECT pay_date, value AS value_rub, value, value_pct,
                       CASE WHEN pay_date = ? THEN 'maturity' ELSE 'amortization' END AS pay_type
                FROM bond_amortizations
                WHERE secid = ? AND pay_date IS NOT NULL
                ORDER BY pay_date
            """, (matdate, secid)).fetchall()

            payments = []
            for row in coupons:
                # Фактическая дата: оверрайд или плановая
                pay_date     = row["override_date"] or row["coupon_date"]
                # Фактическая сумма: оверрайд (уже в рублях) или value из MOEX × курс
                if row["override_value"] is not None:
                    vr = row["override_value"]
                else:
                    vr = to_rub(row["value"], cur) or 0
                is_paid_flag = row["override_is_paid"]  # None/0/1

                payments.append({
                    "pay_date":     pay_date,
                    "planned_date": row["coupon_date"],  # исходная плановая дата
                    "pay_type":     "coupon",
                    "value_pct":    None,
                    "value_rub":    round(vr, 2),
                    "total_rub":    round(vr * qty, 2),
                    "is_paid":      is_paid_flag,        # для зелёной рамки на фронте
                })

            for row in amorts:
                vr = to_rub(row["value_rub"], cur) or 0
                payments.append({
                    "pay_date":     row["pay_date"],
                    "planned_date": row["pay_date"],
                    "pay_type":     row["pay_type"],
                    "value_pct":    row["value_pct"],
                    "value_rub":    round(vr, 2),
                    "total_rub":    round(vr * qty, 2),
                    "is_paid":      None,
                })

            payments.sort(key=lambda x: x["pay_date"] or "")

            result.append({
                "secid":        secid,
                "shortname":    bond["shortname"],
                "emitent_name": bond["emitent_name"] or "",
                "broker":       bond["broker"] or "",
                "matdate":      matdate,
                "qty":          qty,
                "currency":     cur,
                "payments":     payments,
            })

    return result