"""
API портфеля.

POST /portfolio/add          — добавить инструмент в портфель
DELETE /portfolio/{type}/{id} — удалить из портфеля
GET  /portfolio              — список всех позиций
POST /portfolio/sync/{secid} — обновить данные одной бумаги с MOEX
POST /portfolio/sync-all     — обновить все бумаги портфеля
GET  /rates                  — текущие курсы валют
"""
from __future__ import annotations

import re
from datetime import date as dt_date

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db
from app.logger import logger

router = APIRouter(tags=["portfolio"])

MOEX_BASE = "https://iss.moex.com/iss"

# Определение типа инструмента по boardid
BOARD_TO_TYPE = {
    "TQCB": "bond", "TQOB": "bond", "TQOD": "bond",
    "TQBR": "stock", "TQBS": "stock",
    "TQTF": "fund",  "TQTE": "fund",
}


# ── Утилиты ────────────────────────────────────────────────────────────────

def _get_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=MOEX_BASE, timeout=15)


def _extract_emitent(name: str) -> str:
    """Извлекает название эмитента из полного названия бумаги."""
    if not name:
        return name
    patterns = [
        r'\s+БО[\s\-]\S+', r'\s+БО$',
        r'\s+\d{3}[РP][\-\s]\S+', r'\s+\d{3}[РP]$',
        r'\d{3}[РP][\-\s]\S+',
        r'\s+[Сс]ерии?\s+\S+', r'\s+[Пп]\d+$',
    ]
    s = re.sub(r'\s+\d{2}[./]\d{2}[./]\d{2,4}$', '', name).strip()
    for pat in patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            s = s[:m.start()].strip()
            break
    return s


def _get_rates_for_calc() -> dict:
    rates = db.get_latest_exchange_rates() or {}
    return {
        "usd": rates.get("usd", 1.0),
        "eur": rates.get("eur", 1.0),
        "cny": rates.get("cny", 1.0),
    }


def _calc_cost(price: float | None, face_value: float | None,
               nkd: float | None, currency: str | None,
               qty: int, rates: dict) -> float | None:
    """Рассчитывает стоимость позиции в рублях."""
    if price is None or face_value is None:
        return None
    cur = (currency or "RUB").upper()
    rate = rates.get(cur.lower(), 1.0) if cur != "RUB" else 1.0
    bond_price = price / 100 * face_value * rate
    nkd_rub    = (nkd or 0) * (rate if cur != "RUB" else 1.0)
    return round((bond_price + nkd_rub) * qty, 2)


# ── Получение данных с MOEX ────────────────────────────────────────────────

async def _fetch_bond_data(secid: str) -> dict:
    """Делает 2 запроса к MOEX ISS для облигации."""
    async with _get_client() as client:
        # 1. Description — статические поля
        r1 = await client.get(f"/securities/{secid}.json",
                              params={"iss.meta": "off", "iss.only": "description"})
        r1.raise_for_status()
        desc_data = r1.json()["description"]
        dm = {row[0]: row[2] for row in desc_data["data"]}

        boardid    = dm.get("BOARDID", "TQCB")
        face_value = float(dm.get("FACEVALUE") or 1000)
        face_unit  = dm.get("FACEUNIT", "SUR")
        currency   = "RUB" if face_unit in ("SUR", "RUB") else face_unit

        # 2. Котировки
        market = "bonds"
        r2 = await client.get(
            f"/engines/stock/markets/{market}/boards/{boardid}/securities/{secid}.json",
            params={
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
                "securities.columns": "SECID,SHORTNAME,PREVPRICE,ACCRUEDINT,YIELDATPREVWAPRICE",
                "marketdata.columns": "SECID,LAST,OPEN",
            }
        )
        r2.raise_for_status()
        q = r2.json()

        sec_row = next(iter(q["securities"]["data"]), None)
        md_row  = next(iter(q["marketdata"]["data"]), None)

        shortname = (sec_row[1] if sec_row else None) or dm.get("SHORTNAME") or secid
        price     = (md_row[1] if md_row and md_row[1] else None) or \
                    (sec_row[2] if sec_row else None)
        open_p    = md_row[2] if md_row else None
        nkd       = float(sec_row[3]) if sec_row and sec_row[3] else 0.0
        yld       = float(sec_row[4]) if sec_row and sec_row[4] else None

        is_amort  = 1 if dm.get("BOND_SUBTYPE") == "Амортизационная" else 0

    # Определяем эмитента
    # Если в bonds уже есть запись с emitent_id — сохраняем его, не пересоздаём
    existing_bond = db.get_bond_by_secid(secid)
    if existing_bond and existing_bond.get("emitent_id"):
        emitent_id = existing_bond["emitent_id"]
    else:
        emitent_name = _extract_emitent(dm.get("NAME") or shortname)
        emitent_id   = None
        if emitent_name:
            existing_emitent = db.get_emitent_by_name(emitent_name)
            emitent_id = existing_emitent["id"] if existing_emitent else db.upsert_emitent(emitent_name)

    return {
        "secid":              secid,
        "isin":               dm.get("ISIN", secid),
        "emitent_id":         emitent_id,
        "shortname":          shortname,
        "boardid":            boardid,
        "price":              float(price) if price else None,
        "face_value":         face_value,
        "cost":               None,  # заполнится после
        "yield_value":        yld,
        "days_to_redemption": int(dm.get("DAYSTOREDEMPTION") or 0),
        "is_amort":           is_amort,
        "buyback_date":       dm.get("BUYBACKDATE"),
        "bond_subtype":       dm.get("BOND_SUBTYPE"),
        "coupon_freq":        int(dm.get("COUPONFREQUENCY") or 0),
        "coupon_pct":         float(dm.get("COUPONPERCENT") or 0) or None,
        "coupon_value":       float(dm.get("COUPONVALUE") or 0) or None,
        "nkd":                nkd,
        "currency":           currency,
        "is_qual":            int(dm.get("ISQUALIFIEDINVESTORS") or 0),
        "matdate":            dm.get("MATDATE"),
        "list_level":         int(dm.get("LISTLEVEL") or 0) or None,
        "open_price":         float(open_p) if open_p else None,
    }


async def _fetch_stock_data(secid: str) -> dict:
    """Получает данные акции с MOEX."""
    async with _get_client() as client:
        r1 = await client.get(f"/securities/{secid}.json",
                              params={"iss.meta": "off", "iss.only": "description"})
        r1.raise_for_status()
        desc_data = r1.json()["description"]
        dm = {row[0]: row[2] for row in desc_data["data"]}

        boardid = dm.get("BOARDID", "TQBR")
        r2 = await client.get(
            f"/engines/stock/markets/shares/boards/{boardid}/securities/{secid}.json",
            params={
                "iss.meta": "off",
                "iss.only": "securities,marketdata",
                "securities.columns": "SECID,SHORTNAME,LOTSIZE,CURRENCYID",
                "marketdata.columns": "SECID,LAST,OPEN",
            }
        )
        r2.raise_for_status()
        q = r2.json()

        sec_cols = q["securities"]["columns"]
        sec_row  = next(iter(q["securities"]["data"]), None)
        md_row   = next(iter(q["marketdata"]["data"]), None)

        def sv(col):
            idx = sec_cols.index(col) if col in sec_cols else -1
            return sec_row[idx] if sec_row and idx >= 0 else None

        shortname = sv("SHORTNAME") or dm.get("SHORTNAME") or secid
        lot_size  = int(sv("LOTSIZE") or 1)
        currency  = sv("CURRENCYID") or "RUB"
        if currency == "SUR": currency = "RUB"
        is_qual   = int(dm.get("ISQUALIFIEDINVESTORS") or 0)
        price     = md_row[1] if md_row and md_row[1] else None
        open_p    = md_row[2] if md_row else None

    # Эмитент
    # Если в stocks уже есть запись с emitent_id — сохраняем его, не пересоздаём
    existing_stock = db.get_stock_by_secid(secid)
    if existing_stock and existing_stock.get("emitent_id"):
        emitent_id = existing_stock["emitent_id"]
    else:
        raw_name = dm.get("NAME") or dm.get("SHORTNAME") or shortname
        emitent_name = re.sub(r'\s+(ПАО|ОАО|ЗАО|ООО|АО|НАО)\s+ао$', '', raw_name,
                              flags=re.IGNORECASE).strip()
        emitent_name = re.sub(r'\s+ао$', '', emitent_name, flags=re.IGNORECASE).strip()
        emitent_name = re.sub(r'\s+(ПАО|ОАО|ЗАО|ООО|АО|НАО)$', '', emitent_name,
                              flags=re.IGNORECASE).strip()
        existing_emitent = db.get_emitent_by_name(emitent_name)
        emitent_id = existing_emitent["id"] if existing_emitent else db.upsert_emitent(emitent_name)

    return {
        "secid":      secid,
        "isin":       dm.get("ISIN", secid),
        "emitent_id": emitent_id,
        "shortname":  shortname,
        "boardid":    boardid,
        "price":      float(price) if price else None,
        "open_price": float(open_p) if open_p else None,
        "cost":       None,
        "currency":   currency,
        "is_qual":    is_qual,
        "lot_size":   lot_size,
    }


# ── Курсы валют ────────────────────────────────────────────────────────────

async def fetch_and_store_rates() -> None:
    try:
        async with _get_client() as client:
            r = await client.get("/statistics/engines/currency/markets/selt/rates.json",
                                 params={"iss.meta": "off"})
            r.raise_for_status()
            data = r.json()

        usd = eur = cny = None
        for block_name in ("cbrf", "wap_rates"):
            block = data.get(block_name, {})
            cols  = block.get("columns", [])
            rows  = block.get("data", [])
            for row in rows:
                d = dict(zip(cols, row))
                name = d.get("SHORTNAME", "") or d.get("SECID", "")
                val  = d.get("RATE") or d.get("WAPRICE")
                if not val:
                    continue
                if "USD" in name and not usd: usd = float(val)
                if "EUR" in name and not eur: eur = float(val)
                if "CNY" in name and not cny: cny = float(val)

        if usd or eur or cny:
            today = dt_date.today().isoformat()
            db.upsert_exchange_rates(today, usd or 0, eur or 0, cny or 0)
            logger.info("Курсы обновлены: USD=%.2f EUR=%.2f CNY=%.4f", usd, eur, cny)
    except Exception as e:
        logger.error("Ошибка получения курсов: %s", e)


# ── Pydantic схемы ─────────────────────────────────────────────────────────

class AddItem(BaseModel):
    secid:  str
    broker: str | None = None
    qty:    int = 0


# ── Эндпоинты ──────────────────────────────────────────────────────────────

@router.get("/portfolio")
async def get_portfolio():
    items = db.get_portfolio_with_instruments()
    rates = _get_rates_for_calc()

    for item in items:
        if item["instrument_type"] == "bond":
            item["cost"] = _calc_cost(
                item.get("price"), item.get("face_value"),
                item.get("nkd"), item.get("currency"),
                item.get("qty", 1), rates
            )
        elif item["instrument_type"] in ("stock", "fund"):
            price = item.get("price") or 0
            qty   = item.get("qty", 1)
            cur   = (item.get("currency") or "RUB").lower()
            rate  = rates.get(cur, 1.0) if cur != "rub" else 1.0
            item["cost"] = round(price * qty * rate, 2)

    return JSONResponse(items)


STALE_SECONDS = 30  # данные старше 30 сек считаем устаревшими


def _is_stale(updated_at: str | None) -> bool:
    """Проверяет устарели ли данные (более 30 секунд)."""
    if not updated_at:
        return True
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - dt).total_seconds() > STALE_SECONDS
    except Exception:
        return True


@router.post("/portfolio/add")
async def add_to_portfolio(item: AddItem):
    secid = item.secid.upper().strip()

    # 1. Ищем в bonds
    bond_data = db.get_bond_by_secid(secid)
    if bond_data:
        if _is_stale(bond_data.get("updated_at")):
            try:
                fresh = await _fetch_bond_data(secid)
                db.upsert_bond(fresh)
                bond_data = db.get_bond_by_secid(secid)
            except Exception as e:
                logger.warning("Не удалось обновить %s: %s", secid, e)
        db.upsert_portfolio("bond", bond_data["id"], item.broker, item.qty)
        logger.info("Добавлено в портфель: %s (bond)", secid)
        return JSONResponse({"ok": True, "secid": secid, "type": "bond"})

    # 2. Ищем в stocks
    stock_data = db.get_stock_by_secid(secid)
    if stock_data:
        if _is_stale(stock_data.get("updated_at")):
            try:
                fresh = await _fetch_stock_data(secid)
                db.upsert_stock(fresh)
                stock_data = db.get_stock_by_secid(secid)
            except Exception as e:
                logger.warning("Не удалось обновить %s: %s", secid, e)
        db.upsert_portfolio("stock", stock_data["id"], item.broker, item.qty)
        logger.info("Добавлено в портфель: %s (stock)", secid)
        return JSONResponse({"ok": True, "secid": secid, "type": "stock"})

    # 3. Ищем в funds
    fund_data = db.get_fund_by_secid(secid)
    if fund_data:
        if _is_stale(fund_data.get("updated_at")):
            try:
                fresh = await _fetch_stock_data(secid)  # фонды — та же логика
                db.upsert_fund(fresh)
                fund_data = db.get_fund_by_secid(secid)
            except Exception as e:
                logger.warning("Не удалось обновить %s: %s", secid, e)
        db.upsert_portfolio("fund", fund_data["id"], item.broker, item.qty)
        logger.info("Добавлено в портфель: %s (fund)", secid)
        return JSONResponse({"ok": True, "secid": secid, "type": "fund"})

    # 4. Нигде нет — идём на MOEX, определяем тип, создаём запись
    try:
        async with _get_client() as client:
            r = await client.get(f"/securities/{secid}.json",
                                 params={"iss.meta": "off", "iss.only": "description"})
            r.raise_for_status()
            dm = {row[0]: row[2] for row in r.json()["description"]["data"]}
        boardid = dm.get("BOARDID", "TQBR")
        group   = dm.get("GROUP", "")
    except Exception as e:
        return JSONResponse({"error": f"Ошибка запроса к MOEX: {e}"}, status_code=502)

    if "bond" in group.lower() or boardid in ("TQCB", "TQOB", "TQOD"):
        try:
            data = await _fetch_bond_data(secid)
            instrument_id = db.upsert_bond(data)
            instrument_type = "bond"
        except Exception as e:
            return JSONResponse({"error": f"Ошибка загрузки облигации: {e}"}, status_code=502)
    elif "fund" in group.lower() or boardid in ("TQTF", "TQTE"):
        try:
            data = await _fetch_stock_data(secid)
            instrument_id = db.upsert_fund(data)
            instrument_type = "fund"
        except Exception as e:
            return JSONResponse({"error": f"Ошибка загрузки фонда: {e}"}, status_code=502)
    else:
        try:
            data = await _fetch_stock_data(secid)
            instrument_id = db.upsert_stock(data)
            instrument_type = "stock"
        except Exception as e:
            return JSONResponse({"error": f"Ошибка загрузки акции: {e}"}, status_code=502)

    db.upsert_portfolio(instrument_type, instrument_id, item.broker, item.qty)
    logger.info("Добавлено в портфель: %s (%s)", secid, instrument_type)
    return JSONResponse({"ok": True, "secid": secid, "type": instrument_type})


@router.delete("/portfolio/{instrument_type}/{instrument_id}")
async def remove_from_portfolio(instrument_type: str, instrument_id: int):
    db.delete_portfolio_item(instrument_type, instrument_id)
    return JSONResponse({"ok": True})


@router.post("/portfolio/sync/{secid}")
async def sync_one(secid: str):
    """Обновляет данные одной бумаги с MOEX."""
    secid = secid.upper()
    bond  = db.get_bond_by_secid(secid)
    stock = db.get_stock_by_secid(secid)

    try:
        if bond:
            data = await _fetch_bond_data(secid)
            db.upsert_bond(data)
        elif stock:
            data = await _fetch_stock_data(secid)
            db.upsert_stock(data)
        else:
            return JSONResponse({"error": "Бумага не найдена"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    logger.info("Синхронизирована бумага: %s", secid)
    return JSONResponse({"ok": True, "secid": secid})


@router.post("/portfolio/sync-all")
async def sync_all():
    """Обновляет все бумаги портфеля с MOEX."""
    items = db.get_portfolio_with_instruments()
    results = {"ok": [], "error": []}
    for item in items:
        secid = item["secid"]
        try:
            if item["instrument_type"] == "bond":
                data = await _fetch_bond_data(secid)
                db.upsert_bond(data)
            elif item["instrument_type"] == "stock":
                data = await _fetch_stock_data(secid)
                db.upsert_stock(data)
            results["ok"].append(secid)
        except Exception as e:
            logger.warning("Ошибка синхронизации %s: %s", secid, e)
            results["error"].append(secid)

    return JSONResponse(results)


@router.get("/rates")
async def get_rates():
    rates = db.get_latest_exchange_rates()
    if not rates:
        await fetch_and_store_rates()
        rates = db.get_latest_exchange_rates()
    return JSONResponse(rates or {})