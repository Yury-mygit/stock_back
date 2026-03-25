"""
API эндпоинты для работы с портфелем.

GET  /portfolio            — список всех бумаг портфеля (с данными из bonds)
POST /portfolio            — добавить бумагу в портфель
PUT  /portfolio/{secid}    — обновить ручные поля (брокер, кол-во, рейтинг, сектор)
DELETE /portfolio/{secid}  — удалить бумагу из портфеля

POST /portfolio/{secid}/sync  — синхронизировать данные бумаги с MOEX ISS
POST /portfolio/sync-all      — синхронизировать весь портфель
"""
from __future__ import annotations

from datetime import date as dt_date
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import config, db
from app.logger import logger
from app.proxy import get_client

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


# ── Pydantic-схемы запросов ────────────────────────────────────────────────

class PortfolioItem(BaseModel):
    secid:  str
    broker: str = ""
    qty:    int = 0
    rating: str = ""
    sector: str = ""


class PortfolioUpdate(BaseModel):
    broker: str = ""
    qty:    int = 0
    rating: str = ""
    sector: str = ""


# ── Курсы валют ────────────────────────────────────────────────────────────

async def fetch_and_store_rates() -> dict:
    """Запрашивает курсы валют с MOEX ISS и сохраняет в БД."""
    today = dt_date.today().isoformat()
    try:
        async with httpx.AsyncClient(base_url="https://iss.moex.com/iss") as client:
            r = await client.get(
                "/statistics/engines/currency/markets/selt/rates.json?iss.meta=off",
                timeout=10
            )
            r.raise_for_status()
            data = r.json()

        cbrf_cols = data["cbrf"]["columns"]
        cbrf_row  = data["cbrf"]["data"][0] if data["cbrf"]["data"] else []
        cbrf      = dict(zip(cbrf_cols, cbrf_row))

        usd = float(cbrf.get("CBRF_USD_LAST") or 0) or None
        eur = float(cbrf.get("CBRF_EUR_LAST") or 0) or None

        wap_cols  = data["wap_rates"]["columns"]
        secid_idx = wap_cols.index("secid")
        price_idx = wap_cols.index("price")
        cny = None
        for row in data["wap_rates"]["data"]:
            if row[secid_idx] == "CNYRUB_TOM":
                cny = float(row[price_idx])
                break

        if usd and eur and cny:
            db.upsert_exchange_rates(today, usd, eur, cny)
            logger.info("Курсы сохранены на %s: USD=%.4f EUR=%.4f CNY=%.4f", today, usd, eur, cny)
            return {"usd": usd, "eur": eur, "cny": cny}
        logger.warning("Не удалось получить все курсы: USD=%s EUR=%s CNY=%s", usd, eur, cny)
        return {}
    except Exception as e:
        logger.error("Ошибка получения курсов: %s", e)
        return {}


def get_rates_for_calc() -> dict:
    """Возвращает актуальные курсы для расчётов. Сначала сегодняшние, затем последние."""
    today = dt_date.today().isoformat()
    rates = db.get_exchange_rates(today) or db.get_latest_exchange_rates()
    if rates:
        return {"usd": rates["usd"], "eur": rates["eur"], "cny": rates["cny"]}
    return {"usd": 90.0, "eur": 98.0, "cny": 12.0}  # фолбэк


# ── Получение данных с MOEX ISS ────────────────────────────────────────────

async def _fetch_bond_data(secid: str) -> dict:
    """
    Делает 2 запроса к MOEX ISS и возвращает словарь для upsert_bond().
    """
    client = get_client()

    # Запрос 2 — описание эмиссии
    r2 = await client.get(f"/securities/{secid}.json?iss.meta=off")
    r2.raise_for_status()
    j2 = r2.json()

    dm: dict = {}
    for row in j2["description"]["data"]:
        dm[row[0]] = row[2]

    boards = j2.get("boards", {}).get("data", [])
    board_cols = j2.get("boards", {}).get("columns", [])
    is_primary_idx = board_cols.index("is_primary") if "is_primary" in board_cols else 14
    boardid_idx    = board_cols.index("boardid")    if "boardid"    in board_cols else 1

    board_row = next((r for r in boards if r[is_primary_idx] == 1), None)
    boardid   = board_row[boardid_idx] if board_row else "TQCB"

    # Определяем тип рынка по boardid
    BOND_BOARDS  = {"TQCB", "TQOB", "TQOD", "TQOY", "TQIF", "EQOB", "EQIF"}
    SHARE_BOARDS = {"TQBR", "TQBS", "TQDE", "TQNL", "TQNE", "EQBR", "EQBS"}
    FUND_BOARDS  = {"TQTF", "TQTE", "EQTF"}

    if boardid in SHARE_BOARDS:
        market = "shares"
    elif boardid in FUND_BOARDS:
        market = "shares"  # фонды тоже на рынке shares
    else:
        market = "bonds"

    # Запрос 3 — котировки
    r3 = await client.get(
        f"/engines/stock/markets/{market}/boards/{boardid}/securities/{secid}.json"
        f"?iss.meta=off&iss.only=securities,marketdata"
    )
    r3.raise_for_status()
    j3 = r3.json()

    def to_dict(section: str) -> dict:
        cols = j3[section]["columns"]
        rows = j3[section]["data"]
        return dict(zip(cols, rows[0])) if rows else {}

    sec = to_dict("securities")
    md  = to_dict("marketdata")

    def g(d: dict, *keys, default=None):
        for k in keys:
            if d.get(k) not in (None, "", "0000-00-00"):
                return d[k]
        return default

    price    = g(md, "LAST") or g(sec, "PREVPRICE")
    face_val = float(dm.get("FACEVALUE") or 0)
    nkd_num  = float(g(sec, "ACCRUEDINT") or 0)
    currency = dm.get("FACEUNIT", "SUR")

    # Получаем курсы для расчёта
    rates = get_rates_for_calc()
    fx = {"RUB": 1.0, "USD": rates["usd"], "EUR": rates["eur"], "CNY": rates["cny"]}
    rate = fx.get(currency, 1.0)

    # Для акций цена в рублях, не в % от номинала
    if market == "shares":
        cost = round(float(price) * rate, 2) if price else None
    else:
        bond_price_val = float(price) / 100 * face_val * rate if price else None
        # НКД (ACCRUEDINT) всегда приходит в рублях с MOEX ISS
        # Для рублёвых облигаций прибавляем напрямую
        # Для валютных — body уже переведено в рубли через курс, НКД тоже в рублях → просто складываем
        cost = round(bond_price_val + nkd_num, 2) if bond_price_val is not None else None

    return {
        "secid":               secid,
        "isin":                dm.get("ISIN", secid),
        "shortname":           dm.get("SHORTNAME"),
        "name":                dm.get("NAME"),
        "boardid":             boardid,
        "price":               float(price) if price else None,
        "face_value":          face_val or None,
        "cost":                cost,
        "yield_value":         float(g(md, "YIELD") or 0) or None,
        "days_to_redemption":  int(dm["DAYSTOREDEMPTION"]) if dm.get("DAYSTOREDEMPTION") else None,
        "is_amort":            1 if "Амортиз" in (dm.get("BOND_TYPE") or "") else 0,
        "buyback_date":        g(sec, "BUYBACKDATE"),
        "bond_subtype":        dm.get("BOND_SUBTYPE"),
        "coupon_freq":         int(dm["COUPONFREQUENCY"]) if dm.get("COUPONFREQUENCY") else None,
        "coupon_pct":          float(dm["COUPONPERCENT"]) if dm.get("COUPONPERCENT") else None,
        "coupon_value":        float(dm["COUPONVALUE"]) if dm.get("COUPONVALUE") else None,
        "nkd":                 nkd_num or None,
        "currency":            "RUB" if currency == "SUR" else currency,
        "is_qual":             1 if dm.get("ISQUALIFIEDINVESTORS") == "1" else 0,
        "matdate":             dm.get("MATDATE"),
        "open_price":          float(md.get("OPEN")) if md.get("OPEN") else None,
        "list_level":          int(dm["LISTLEVEL"]) if dm.get("LISTLEVEL") else None,
    }


# ── Эндпоинты ──────────────────────────────────────────────────────────────

@router.get("")
async def list_portfolio():
    """Возвращает весь портфель с данными из bonds."""
    return JSONResponse(db.get_portfolio())


@router.post("", status_code=201)
async def add_to_portfolio(item: PortfolioItem):
    """
    Добавляет бумагу в портфель.
    Сразу синхронизирует данные с MOEX ISS.
    """
    try:
        bond_data = await _fetch_bond_data(item.secid)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"MOEX ISS вернул {e.response.status_code} для {item.secid}")
    except Exception as e:
        raise HTTPException(502, f"Ошибка запроса к MOEX ISS: {e}")

    db.upsert_bond(bond_data)
    db.upsert_portfolio(item.secid, item.broker, item.qty, item.rating, item.sector)
    logger.info("Добавлена бумага в портфель: %s", item.secid)
    return JSONResponse({"status": "ok", "secid": item.secid})


@router.put("/{secid}")
async def update_portfolio_item(secid: str, data: PortfolioUpdate):
    """Обновляет ручные поля: брокер, кол-во, рейтинг, сектор."""
    if not db.get_bond(secid):
        raise HTTPException(404, f"Бумага {secid} не найдена в базе")
    db.upsert_portfolio(secid, data.broker, data.qty, data.rating, data.sector)
    return JSONResponse({"status": "ok"})


@router.delete("/{secid}")
async def remove_from_portfolio(secid: str):
    """Удаляет бумагу из портфеля."""
    if not db.delete_portfolio_item(secid):
        raise HTTPException(404, f"Бумага {secid} не найдена в портфеле")
    logger.info("Удалена бумага из портфеля: %s", secid)
    return JSONResponse({"status": "ok"})


@router.post("/{secid}/sync")
async def sync_bond(secid: str):
    """Обновляет данные одной бумаги с MOEX ISS."""
    if not db.get_bond(secid):
        raise HTTPException(404, f"Бумага {secid} не найдена в базе")
    try:
        bond_data = await _fetch_bond_data(secid)
    except Exception as e:
        raise HTTPException(502, f"Ошибка синхронизации {secid}: {e}")
    db.upsert_bond(bond_data)
    logger.info("Синхронизирована бумага: %s", secid)
    return JSONResponse({"status": "ok", "secid": secid})


@router.post("/sync-all")
async def sync_all():
    """Обновляет данные всех бумаг портфеля с MOEX ISS."""
    portfolio = db.get_portfolio()
    results = {"ok": [], "error": []}

    for item in portfolio:
        secid = item["secid"]
        try:
            bond_data = await _fetch_bond_data(secid)
            db.upsert_bond(bond_data)
            results["ok"].append(secid)
            logger.info("Синхронизирована: %s", secid)
        except Exception as e:
            results["error"].append({"secid": secid, "error": str(e)})
            logger.error("Ошибка синхронизации %s: %s", secid, e)

    return JSONResponse(results)