from __future__ import annotations
from datetime import datetime
from app.db import get_conn, _write_lock
from app.logger import logger
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