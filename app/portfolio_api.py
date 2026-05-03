"""
API портфеля.

POST /portfolio/add                        — добавить инструмент в портфель
DELETE /portfolio/{type}/{id}              — удалить из портфеля
PATCH  /portfolio/{type}/{id}/qty          — изменить количество
GET  /portfolio                            — список всех позиций
GET  /portfolio/cashflow                   — cashflow по бумагам для Calendar
POST /portfolio/sync/{secid}              — обновить данные одной бумаги с MOEX
POST /portfolio/sync-all                  — обновить все бумаги портфеля
POST /portfolio/bondization/{secid}       — перезагрузить купоны/амортизации
GET  /rates                               — текущие курсы валют
POST /rates/refresh                       — принудительно обновить курсы
GET  /yield-calendar                      — календарь выплат по месяцам
GET  /debug/bond/{secid}                  — отладка данных бумаги
"""
from __future__ import annotations

import asyncio
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

BOARD_TO_TYPE = {
    "TQCB": "bond", "TQOB": "bond", "TQOD": "bond",
    "TQBR": "stock", "TQBS": "stock",
    "TQTF": "fund",  "TQTE": "fund",
}

class AddItem(BaseModel):
    secid:  str
    broker: str | None = None
    qty:    int = 0

class QtyChange(BaseModel):
    delta: int


# ── Утилиты ────────────────────────────────────────────────────────────────

def _get_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=MOEX_BASE, timeout=15)


def _extract_emitent(name: str) -> str:
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
    """Рассчитывает стоимость позиции в рублях.
    MOEX возвращает НКД в рублях даже для валютных бумаг — не умножаем на курс.
    Цена котируется в % от номинала в валюте — умножаем на курс.
    """
    if price is None or face_value is None:
        return None
    cur        = (currency or "RUB").upper()
    rate       = rates.get(cur.lower(), 1.0) if cur != "RUB" else 1.0
    bond_price = price / 100 * face_value * rate
    nkd_rub    = nkd or 0
    return round((bond_price + nkd_rub) * qty, 2)


# ── MOEX: получение данных ─────────────────────────────────────────────────

async def _fetch_bond_data(secid: str) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-tracker/1.0)"}

    async with _get_client() as client:
        r1 = await client.get(
            f"/securities/{secid}.json",
            params={"iss.meta": "off", "iss.only": "description"},
            headers=headers,
        )
        r1.raise_for_status()
        desc_rows = r1.json().get("description", {}).get("data", [])
        dm = {row[0]: row[2] for row in desc_rows}

        boardid    = dm.get("BOARDID") or dm.get("PRIMARY_BOARDID", "TQCB")
        face_value = float(dm.get("FACEVALUE") or 1000)
        face_unit  = dm.get("FACEUNIT", "SUR")
        currency   = "RUB" if face_unit in ("SUR", "RUB") else face_unit

        sec_row = None
        md_row  = None
        boards_to_try = [boardid] if boardid else []
        for alt in ("TQCB", "TQOB", "TQOD"):
            if alt not in boards_to_try:
                boards_to_try.append(alt)

        for board in boards_to_try:
            r2 = await client.get(
                f"/engines/stock/markets/bonds/boards/{board}/securities/{secid}.json",
                params={
                    "iss.meta": "off",
                    "iss.only": "securities,marketdata",
                    "securities.columns": "SECID,SHORTNAME,PREVPRICE,ACCRUEDINT,YIELDATPREVWAPRICE",
                    "marketdata.columns": "SECID,LAST,OPEN",
                },
                headers=headers,
            )
            if r2.status_code != 200:
                continue
            q = r2.json()
            sec_row = next(iter(q.get("securities", {}).get("data", [])), None)
            md_row  = next(iter(q.get("marketdata", {}).get("data", [])), None)
            if sec_row:
                boardid = board
                break

        shortname = (sec_row[1] if sec_row else None) or dm.get("SHORTNAME") or secid
        price     = (md_row[1] if md_row and md_row[1] else None) or \
                    (sec_row[2] if sec_row else None)
        open_p    = md_row[2] if md_row else None
        nkd       = float(sec_row[3]) if sec_row and sec_row[3] else 0.0
        yld       = float(sec_row[4]) if sec_row and sec_row[4] else None
        is_amort  = 1 if dm.get("BOND_SUBTYPE") == "Амортизационная" else 0

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
        "cost":               None,
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

    existing_stock = db.get_stock_by_secid(secid)
    if existing_stock and existing_stock.get("emitent_id"):
        emitent_id = existing_stock["emitent_id"]
    else:
        raw_name     = dm.get("NAME") or dm.get("SHORTNAME") or shortname
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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; portfolio-tracker/1.0)"}
    try:
        async with _get_client() as client:
            r = await client.get(
                "/statistics/engines/currency/markets/selt/rates.json",
                params={"iss.meta": "off"},
                headers=headers,
            )
            r.raise_for_status()
            data = r.json()

        usd = eur = cny = None

        cbrf      = data.get("cbrf", {})
        cbrf_cols = cbrf.get("columns", [])
        cbrf_rows = cbrf.get("data", [])
        if cbrf_rows:
            row = dict(zip(cbrf_cols, cbrf_rows[0]))
            usd = row.get("CBRF_USD_LAST") or row.get("USDTOM_UTS_CLOSEPRICE")
            eur = row.get("CBRF_EUR_LAST")
            if usd: usd = float(usd)
            if eur: eur = float(eur)

        wap      = data.get("wap_rates", {})
        wap_cols = wap.get("columns", [])
        wap_rows = wap.get("data", [])
        for row in wap_rows:
            d     = dict(zip(wap_cols, row))
            secid = d.get("secid", "")
            price = d.get("price")
            if not price:
                continue
            if "CNY" in secid and not cny: cny = float(price)
            if "USD" in secid and not usd: usd = float(price)
            if "EUR" in secid and not eur: eur = float(price)

        if usd or eur or cny:
            today = dt_date.today().isoformat()
            db.upsert_exchange_rates(today, usd or 0, eur or 0, cny or 0)
            logger.info("Курсы обновлены: USD=%.4f EUR=%.4f CNY=%.4f", usd or 0, eur or 0, cny or 0)
        else:
            logger.warning("Курсы не найдены в ответе MOEX. Колонки cbrf: %s", cbrf_cols)
    except Exception as e:
        logger.error("Ошибка получения курсов: %s", e)


# ── Bondization ────────────────────────────────────────────────────────────

async def _fetch_and_save_bondization(secid: str) -> dict:
    if db.has_bondization(secid):
        return {"skipped": True}

    async with _get_client() as client:
        r = await client.get(
            f"/securities/{secid}/bondization.json",
            params={"iss.meta": "off", "iss.only": "coupons,amortizations"}
        )
        r.raise_for_status()
        data = r.json()

    coup_cols = data["coupons"]["columns"]
    coup_rows = data["coupons"]["data"]
    ci = {col: i for i, col in enumerate(coup_cols)}
    coupons = []
    for row in coup_rows:
        coupons.append({
            "coupon_date": row[ci["coupondate"]],
            "value":       row[ci["value"]],
            "value_rub":   row[ci["value_rub"]],
            "face_value":  row[ci["facevalue"]],
        })

    amort_cols = data["amortizations"]["columns"]
    amort_rows = data["amortizations"]["data"]
    ai = {col: i for i, col in enumerate(amort_cols)}
    amorts = []
    for row in amort_rows:
        amorts.append({
            "pay_date":    row[ai["amortdate"]],
            "value":       row[ai["value"]],
            "value_pct":   row[ai["valueprc"]],
            "face_value":  row[ai["facevalue"]],
            "data_source": row[ai["data_source"]],
        })

    c_inserted = db.save_bond_coupons(secid, coupons)
    a_inserted = db.save_bond_amortizations(secid, amorts)
    logger.info("Bondization %s: купонов=%d, амортизаций=%d", secid, c_inserted, a_inserted)
    return {"coupons": c_inserted, "amortizations": a_inserted}


# ── Вспомогательные ────────────────────────────────────────────────────────

STALE_SECONDS = 30

def _is_stale(updated_at: str | None) -> bool:
    if not updated_at:
        return True
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() > STALE_SECONDS
    except Exception:
        return True


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


@router.get("/portfolio/cashflow")
async def portfolio_cashflow():
    """Все выплаты по облигациям портфеля — купоны + амортизации на весь срок.
    Используется в Calendar/Cashflow. Возвращает данные по бумагам с value_pct
    для расчёта динамического капитала при амортизациях.
    """
    data = db.get_portfolio_cashflow()
    return JSONResponse(data)


class CouponOverride(BaseModel):
    secid:        str
    coupon_date:  str           # плановая дата (ключ)
    is_paid:      int | None    # 1=подтверждён, 0=не получен, None=сброс
    actual_date:  str | None    # фактическая дата если перенесён
    actual_value: float | None  # фактическая сумма если скорректирована


@router.patch("/portfolio/coupon-override")
async def save_coupon_override(override: CouponOverride):
    """Сохраняет пользовательскую корректировку купона.
    Вызывается при закрытии попапа в Calendar/Cashflow.
    """
    result = db.upsert_coupon_override(
        secid        = override.secid,
        coupon_date  = override.coupon_date,
        is_paid      = override.is_paid,
        actual_date  = override.actual_date,
        actual_value = override.actual_value,
    )
    return JSONResponse({"ok": True, **result})


@router.get("/portfolio/coupon-override/{secid}/{coupon_date}")
async def get_coupon_override(secid: str, coupon_date: str):
    """Возвращает корректировку купона если есть."""
    result = db.get_coupon_override(secid, coupon_date)
    if not result:
        return JSONResponse({"found": False})
    return JSONResponse({"found": True, **result})


@router.post("/portfolio/add")
async def add_to_portfolio(item: AddItem):
    secid = item.secid.upper().strip()

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
        try:
            await _fetch_and_save_bondization(secid)
        except Exception as e:
            logger.warning("Не удалось загрузить bondization для %s: %s", secid, e)
        return JSONResponse({"ok": True, "secid": secid, "type": "bond"})

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

    fund_data = db.get_fund_by_secid(secid)
    if fund_data:
        if _is_stale(fund_data.get("updated_at")):
            try:
                fresh = await _fetch_stock_data(secid)
                db.upsert_fund(fresh)
                fund_data = db.get_fund_by_secid(secid)
            except Exception as e:
                logger.warning("Не удалось обновить %s: %s", secid, e)
        db.upsert_portfolio("fund", fund_data["id"], item.broker, item.qty)
        logger.info("Добавлено в портфель: %s (fund)", secid)
        return JSONResponse({"ok": True, "secid": secid, "type": "fund"})

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

    if instrument_type == "bond":
        try:
            await _fetch_and_save_bondization(secid)
        except Exception as e:
            logger.warning("Не удалось загрузить bondization для %s: %s", secid, e)

    return JSONResponse({"ok": True, "secid": secid, "type": instrument_type})


@router.delete("/portfolio/{instrument_type}/{instrument_id}")
async def remove_from_portfolio(instrument_type: str, instrument_id: int):
    db.delete_portfolio_item(instrument_type, instrument_id)
    return JSONResponse({"ok": True})


@router.patch("/portfolio/{instrument_type}/{instrument_id}/qty")
async def change_qty(instrument_type: str, instrument_id: int, change: QtyChange):
    """Изменяет количество бумаг. delta > 0 = купить, delta < 0 = продать."""
    result = db.change_portfolio_qty(instrument_type, instrument_id, change.delta)
    if result is None:
        return JSONResponse({"error": "Позиция не найдена"}, status_code=404)
    if result["qty"] < 0:
        return JSONResponse({"error": "Нельзя продать больше чем есть"}, status_code=400)
    return JSONResponse({"ok": True, "qty": result["qty"]})


@router.post("/portfolio/sync/{secid}")
async def sync_one(secid: str):
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
    items = db.get_portfolio_with_instruments()

    async def fetch_one(item):
        secid = item["secid"]
        try:
            if item["instrument_type"] == "bond":
                data = await _fetch_bond_data(secid)
                db.upsert_bond(data)
            elif item["instrument_type"] == "stock":
                data = await _fetch_stock_data(secid)
                db.upsert_stock(data)
            return ("ok", secid)
        except Exception as e:
            logger.warning("Ошибка синхронизации %s: %s", secid, e)
            return ("error", secid)

    outcomes = await asyncio.gather(*[fetch_one(i) for i in items])
    results = {"ok": [], "error": []}
    for status, secid in outcomes:
        results[status].append(secid)
    return JSONResponse(results)


@router.post("/portfolio/bondization/{secid}")
async def refresh_bondization(secid: str):
    secid = secid.upper()
    bond  = db.get_bond_by_secid(secid)
    if not bond:
        return JSONResponse({"error": "Облигация не найдена"}, status_code=404)
    try:
        with db._write_lock, db.get_conn() as conn:
            conn.execute("DELETE FROM bond_coupons WHERE secid = ?", (secid,))
            conn.execute("DELETE FROM bond_amortizations WHERE secid = ?", (secid,))
            conn.commit()
        result = await _fetch_and_save_bondization(secid)
        return JSONResponse({"ok": True, **result})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@router.get("/yield-calendar")
async def yield_calendar(months: int = 12):
    if months < 1 or months > 120:
        months = 12
    data          = db.get_yield_calendar(months=months)
    total_coupons = sum(
        item["total_rub"] or 0
        for month in data for item in month["items"]
        if item["pay_type"] == "coupon"
    )
    total_amort = sum(
        item["total_rub"] or 0
        for month in data for item in month["items"]
        if item["pay_type"] in ("amortization", "maturity")
    )
    return JSONResponse({
        "months":        data,
        "total_coupons": round(total_coupons, 2),
        "total_amort":   round(total_amort, 2),
        "total":         round(total_coupons + total_amort, 2),
        "period_months": months,
    })


@router.get("/rates")
async def get_rates():
    rates = db.get_latest_exchange_rates()
    if not rates:
        await fetch_and_store_rates()
        rates = db.get_latest_exchange_rates()
    return JSONResponse(rates or {})


@router.post("/rates/refresh")
async def refresh_rates():
    await fetch_and_store_rates()
    rates = db.get_latest_exchange_rates()
    if not rates:
        return JSONResponse({"error": "Не удалось получить курсы"}, status_code=502)
    return JSONResponse({"ok": True, "rates": rates})


@router.get("/debug/bond/{secid}")
async def debug_bond(secid: str):
    try:
        data = await _fetch_bond_data(secid)
        return JSONResponse(data)
    except Exception as e:
        import traceback
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)