from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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