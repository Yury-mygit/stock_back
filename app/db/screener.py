from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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