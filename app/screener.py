"""
Скринер облигаций.
GET  /screener/bonds          — поиск из локального кеша
GET  /screener/sectors        — список секторов
POST /screener/sync           — синхронизировать кеш с MOEX
GET  /screener/sync/status    — статус синхронизации
GET  /screener/count          — количество облигаций в кеше
"""
from __future__ import annotations

import asyncio
import re
from datetime import date as dt_date
from typing import Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import db
from app.logger import logger
from app.ratings_api import _extract_emitent_from_secname

router = APIRouter(prefix="/screener", tags=["screener"])

MOEX_BASE = "https://iss.moex.com/iss"
BOARD     = "TQCB"
BATCH     = 100
PAGE_SIZE = 50

SEC_COLS = (
    "SECID,SHORTNAME,SECNAME,ISIN,FACEVALUE,FACEUNIT,MATDATE,BUYBACKDATE,"
    "LISTLEVEL,COUPONPERCENT,COUPONVALUE,ACCRUEDINT,"
    "ISSUESIZE,PREVPRICE,YIELDATPREVWAPRICE,COUPONPERIOD,"
    "BONDTYPE,BONDSUBTYPE,SECTORID,CURRENCYID"
)
MD_COLS = "SECID,DURATION"

# ── Состояние синхронизации ────────────────────────────────────────────────
_sync_state = {
    "running": False,
    "total":   0,
    "done":    0,
    "errors":  0,
    "status":  "idle",
}


# ── Утилиты ────────────────────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x != 0.0 else None
    except (TypeError, ValueError):
        return None

def _i(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

def _clean_date(v) -> Optional[str]:
    return v if v and v != "0000-00-00" else None


# ── Синхронизация с MOEX ───────────────────────────────────────────────────

async def _sync_task() -> None:
    global _sync_state
    _sync_state.update({"running": True, "done": 0, "errors": 0,
                         "total": 0, "status": "running"})

    async with httpx.AsyncClient(base_url=MOEX_BASE, timeout=60) as client:
        try:
            # TQCB небольшая (~3000 бумаг) — MOEX отдаёт всё одним запросом
            r = await client.get(
                f"/engines/stock/markets/bonds/boards/{BOARD}/securities.json",
                params={
                    "iss.meta":           "off",
                    "iss.only":           "securities,marketdata",
                    "securities.columns": SEC_COLS,
                    "marketdata.columns": MD_COLS,
                    "limit":              10000,
                },
            )
            r.raise_for_status()
            data = r.json()

            sec_cols = data["securities"]["columns"]
            sec_rows = data["securities"]["data"]
            md_rows  = data["marketdata"]["data"]

            logger.info("Screener sync: got %d rows from MOEX", len(sec_rows))

            if not sec_rows:
                _sync_state.update({"running": False, "status": "done"})
                return

            _sync_state["total"] = len(sec_rows)

            # Индексы колонок
            ci     = {col: i for i, col in enumerate(sec_cols)}
            md_dur = {row[0]: row[1] for row in md_rows if row[1]}

            batch = []
            # Получаем все secid которые уже есть в screener_bonds — для них не трогаем эмитента
            with db.get_conn() as _conn:
                _existing_secids = set(
                    r[0] for r in _conn.execute(
                        "SELECT secid FROM screener_bonds"
                    ).fetchall()
                )

            for row in sec_rows:
                secid     = row[ci["SECID"]]
                shortname = row[ci["SHORTNAME"]] if "SHORTNAME" in ci else None
                isin      = row[ci["ISIN"]]      if "ISIN"      in ci else None
                secname   = shortname or secid

                # Определяем эмитента ТОЛЬКО для новых бумаг
                # Для существующих берём emitent_id из screener_bonds (не пересоздаём)
                emitent_id = None
                if secid not in _existing_secids:
                    emitent_name = _extract_emitent_from_secname(secname, "bonds")
                    if emitent_name:
                        existing = db.get_emitent_by_name(emitent_name)
                        if existing:
                            emitent_id = existing["id"]
                        else:
                            emitent_id = db.upsert_emitent(emitent_name)
                else:
                    # Берём существующий emitent_id из screener_bonds
                    with db.get_conn() as _conn:
                        _row = _conn.execute(
                            "SELECT emitent_id FROM screener_bonds WHERE secid = ?",
                            (secid,)
                        ).fetchone()
                    emitent_id = _row[0] if _row else None

                # Валюта
                face_unit = row[ci["FACEUNIT"]]   if "FACEUNIT"   in ci else None
                currency  = row[ci["CURRENCYID"]] if "CURRENCYID" in ci else None
                if face_unit in ("SUR", "RUB"):
                    currency = "RUB"
                elif not currency:
                    currency = face_unit

                batch.append({
                    "secid":        secid,
                    "isin":         isin,
                    "shortname":    shortname,
                    "secname":      row[ci["SECNAME"]] if "SECNAME" in ci else None,
                    "emitent_id":   emitent_id,
                    "face_value":   _f(row[ci["FACEVALUE"]])          if "FACEVALUE"          in ci else None,
                    "face_unit":    face_unit,
                    "matdate":      _clean_date(row[ci["MATDATE"]])   if "MATDATE"            in ci else None,
                    "buyback_date": _clean_date(row[ci["BUYBACKDATE"]]) if "BUYBACKDATE"      in ci else None,
                    "list_level":   _i(row[ci["LISTLEVEL"]])          if "LISTLEVEL"          in ci else None,
                    "coupon_pct":   _f(row[ci["COUPONPERCENT"]])      if "COUPONPERCENT"      in ci else None,
                    "coupon_value": _f(row[ci["COUPONVALUE"]])        if "COUPONVALUE"        in ci else None,
                    "nkd":          _f(row[ci["ACCRUEDINT"]])         if "ACCRUEDINT"         in ci else None,
                    "issue_size":   _i(row[ci["ISSUESIZE"]])          if "ISSUESIZE"          in ci else None,
                    "prev_price":   _f(row[ci["PREVPRICE"]])          if "PREVPRICE"          in ci else None,
                    "yield_value":  _f(row[ci["YIELDATPREVWAPRICE"]]) if "YIELDATPREVWAPRICE" in ci else None,
                    "coupon_freq":  _i(row[ci["COUPONPERIOD"]])       if "COUPONPERIOD"       in ci else None,
                    "bond_type":    row[ci["BONDTYPE"]]               if "BONDTYPE"           in ci else None,
                    "bond_subtype": row[ci["BONDSUBTYPE"]]            if "BONDSUBTYPE"        in ci else None,
                    "sector_id":    row[ci["SECTORID"]]               if "SECTORID"           in ci else None,
                    "duration":     md_dur.get(secid),
                    "currency":     currency,
                })

            db.upsert_screener_bonds_batch(batch)
            result = db.sync_screener_batch_to_bonds(batch)
            logger.info("Bonds sync: inserted=%d updated=%d",
                        result["inserted"], result["updated"])
            _sync_state["done"] = len(batch)

        except Exception as e:
            import traceback
            logger.error("Screener sync error: %s\n%s", e, traceback.format_exc())
            _sync_state["errors"] += 1

    _sync_state.update({"running": False, "status": "done"})
    logger.info("Screener sync done: %d bonds, %d errors",
                _sync_state["done"], _sync_state["errors"])

# ── Эндпоинты ──────────────────────────────────────────────────────────────

@router.post("/sync")
async def start_sync():
    if _sync_state["running"]:
        return JSONResponse({"error": "Синхронизация уже запущена"}, status_code=409)
    # Запускаем напрямую (не в фоне) чтобы дождаться результата
    await _sync_task()
    return JSONResponse({"ok": True, "done": _sync_state["done"], "errors": _sync_state["errors"]})


@router.get("/sync/status")
async def sync_status():
    return JSONResponse(_sync_state)


@router.post("/sync-to-system")
async def sync_to_system():
    """Синхронизирует screener_bonds → bonds/stocks/funds."""
    count = db.get_screener_bonds_count()
    if count == 0:
        return JSONResponse({"error": "Сначала синхронизируйте с Мосбиржей"}, status_code=400)
    result = db.sync_screener_to_bonds()
    return JSONResponse({"ok": True, **result})


@router.get("/count")
async def screener_count():
    return JSONResponse({"count": db.get_screener_bonds_count()})


@router.get("/sectors")
async def list_sectors():
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT sector_id, sector_id AS name
            FROM screener_bonds
            WHERE sector_id IS NOT NULL AND sector_id != ''
            ORDER BY sector_id
        """).fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.get("/bonds")
async def screener_bonds(
    yield_min:   Optional[float] = None,
    yield_max:   Optional[float] = None,
    years_min:   Optional[float] = None,
    years_max:   Optional[float] = None,
    dur_min:     Optional[int]   = None,
    dur_max:     Optional[int]   = None,
    coupon_min:  Optional[float] = None,
    coupon_max:  Optional[float] = None,
    currency:    Optional[str]   = None,
    is_amort:    Optional[int]   = None,
    has_offer:   Optional[int]   = None,
    list_level:  Optional[int]   = None,
    sector_id:   Optional[str]   = None,
    page:        int             = 1,
):
    count = db.get_screener_bonds_count()
    if count == 0:
        return JSONResponse({
            "total": 0, "page": 1, "pages": 0,
            "items": [],
            "hint": "Нажмите «Синхронизировать» для загрузки данных с MOEX",
        })

    filters = {
        "yield_min":  yield_min,
        "yield_max":  yield_max,
        "years_min":  years_min,
        "years_max":  years_max,
        "dur_min":    dur_min,
        "dur_max":    dur_max,
        "coupon_min": coupon_min,
        "coupon_max": coupon_max,
        "currency":   currency,
        "is_amort":   is_amort,
        "has_offer":  has_offer,
        "list_level": list_level,
        "sector_id":  sector_id,
    }
    # Убираем None
    filters = {k: v for k, v in filters.items() if v is not None}

    result = db.query_screener_bonds(filters, page=page, page_size=PAGE_SIZE)
    return JSONResponse(result)