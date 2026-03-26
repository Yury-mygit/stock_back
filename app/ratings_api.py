"""
API рейтингов эмитентов.

GET  /ratings/emitents              — список эмитентов с последним рейтингом
POST /ratings/set-url               — сохранить ссылку raexpert
POST /ratings/fetch/{emitent_id}    — спарсить рейтинг по ссылке
GET  /ratings/history/{emitent_id}  — история рейтингов
POST /ratings/fetch-all             — фоновый обход всех эмитентов
GET  /ratings/fetch-all/status      — статус обхода
POST /ratings/fetch-all/stop        — остановить
GET  /ratings/debug-parse?url=...   — диагностика парсинга
"""
from __future__ import annotations

import asyncio
import re

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import db
from app.logger import logger

router = APIRouter(prefix="/ratings", tags=["ratings"])

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Состояние фонового обхода ──────────────────────────────────────────────
_crawl_state = {
    "running": False, "total": 0, "done": 0,
    "errors": 0, "current": None,
}


# ── Утилита: извлечение эмитента из SECNAME ───────────────────────────────

def _extract_emitent_from_secname(secname: str, market: str) -> str:
    if not secname:
        return secname
    if market == "bonds":
        s = re.sub(r'\s+\d{2}[./]\d{2}[./]\d{2,4}$', '', secname).strip()
        patterns = [
            r'\s+БО[\s\-]\S+', r'\s+БО$',
            r'\s+\d{3}[РP][\-\s]\S+', r'\s+\d{3}[РP]$',
            r'\d{3}[РP][\-\s]\S+',
            r'\s+[Сс]ерии?\s+\S+', r'\s+[Пп]\d+$',
        ]
        for pat in patterns:
            m = re.search(pat, s, re.IGNORECASE)
            if m:
                s = s[:m.start()].strip()
                break
        return s
    else:
        s = secname
        s = re.sub(r'\s+(ПАО|ОАО|ЗАО|ООО|АО|НАО)\s+ао$', '', s, flags=re.IGNORECASE).strip()
        s = re.sub(r'\s+ао$', '', s, flags=re.IGNORECASE).strip()
        s = re.sub(r'\s+(ПАО|ОАО|ЗАО|ООО|АО|НАО)$', '', s, flags=re.IGNORECASE).strip()
        return s


# ── Парсер Эксперт РА ──────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://raexpert.ru" + url
    if not url.endswith("/"):
        url += "/"
    return url


def _parse_row_cells(cells: list, has_forecast: bool) -> dict | None:
    if len(cells) < (4 if has_forecast else 2):
        return None
    rating_cell = cells[0]
    inner_span  = rating_cell.select_one("span span")
    if inner_span:
        rating_val = inner_span.get_text(strip=True)
    else:
        spans = rating_cell.find_all("span")
        raw   = spans[0].get_text(strip=True) if spans else rating_cell.get_text(strip=True)
        rating_val = raw.split(",")[0].strip()

    rating_clean = rating_val.strip()
    if not rating_clean or rating_clean in ("-", "отозван"):
        return None

    if has_forecast:
        forecast_val    = cells[1].get_text(strip=True)
        watch_text      = cells[2].get_text(strip=True)
        under_watch_val = 1 if "наблюдени" in watch_text.lower() else 0
        date_cell       = cells[3]
        forecast_out    = forecast_val if forecast_val and forecast_val != "-" else None
    else:
        forecast_out    = None
        under_watch_val = 0
        date_cell       = cells[1]

    date_link  = date_cell.find("a")
    date_text  = date_cell.get_text(strip=True)
    source_url = None
    if date_link:
        href = date_link.get("href", "")
        source_url = "https://raexpert.ru" + href if href.startswith("/") else href

    date_iso = None
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', date_text)
    if m:
        date_iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    return {
        "rating": rating_clean, "forecast": forecast_out,
        "under_watch": under_watch_val, "rating_date": date_iso,
        "source_url": source_url,
    }


async def _parse_raexpert_page(url: str) -> dict:
    url = _normalize_url(url)
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        r = await client.get(url, timeout=15)
        r.raise_for_status()

    soup   = BeautifulSoup(r.text, "html.parser")
    result = {"rating": None, "agency": "Эксперт РА", "forecast": None,
              "under_watch": 0, "rating_date": None, "history": []}

    # Тип 1: рейтинг компании
    company_table = soup.select_one("table.object-rating-table")
    if company_table:
        history = []
        for row in company_table.select("tr:not(thead tr)"):
            entry = _parse_row_cells(row.find_all("td"), has_forecast=True)
            if entry:
                history.append(entry)
        if history:
            result.update({**history[0], "history": history})
        return result

    # Тип 2: рейтинги облигаций
    all_entries = []
    for table in soup.select(".b-actions__rates table"):
        for row in table.select("tr:not(thead tr)"):
            entry = _parse_row_cells(row.find_all("td"), has_forecast=False)
            if entry:
                all_entries.append(entry)

    if not all_entries:
        return result

    all_entries.sort(key=lambda e: e["rating_date"] or "0000-00-00", reverse=True)
    seen, history = set(), []
    for e in all_entries:
        key = (e["rating"], e["rating_date"])
        if key not in seen:
            seen.add(key)
            history.append(e)

    result.update({**all_entries[0], "history": history})
    return result


def _save_parsed(emitent_id: int, raexpert_url: str, parsed: dict) -> None:
    if not parsed.get("rating"):
        return
    for entry in parsed.get("history", [parsed]):
        db.add_rating(
            emitent_id   = emitent_id,
            rating       = entry["rating"],
            agency       = parsed["agency"],
            raexpert_url = raexpert_url,
            under_watch  = entry.get("under_watch", 0),
            forecast     = entry.get("forecast"),
            rating_date  = entry.get("rating_date"),
        )
    logger.info("Рейтинг сохранён: emitent_id=%d → %s (%s)",
                emitent_id, parsed["rating"], parsed["rating_date"])


# ── Фоновый обход ──────────────────────────────────────────────────────────

async def _crawl_task(emitents: list[dict], delay: float = 4.0) -> None:
    global _crawl_state
    _crawl_state.update({"running": True, "total": len(emitents),
                          "done": 0, "errors": 0})
    try:
        for item in emitents:
            if not _crawl_state["running"]:
                break
            emitent_id = item["emitent_id"]
            url        = item.get("raexpert_url")
            _crawl_state["current"] = item["name"]

            if url:
                try:
                    parsed = await _parse_raexpert_page(url)
                    _save_parsed(emitent_id, url, parsed)
                except Exception as e:
                    logger.warning("Ошибка парсинга %s: %s", item["name"], e)
                    _crawl_state["errors"] += 1

            _crawl_state["done"] += 1
            await asyncio.sleep(delay)
    finally:
        _crawl_state.update({"running": False, "current": None})


# ── Pydantic схемы ─────────────────────────────────────────────────────────

class SetUrlRequest(BaseModel):
    emitent_id:   int
    raexpert_url: str


# ── Эндпоинты ──────────────────────────────────────────────────────────────

@router.get("/debug-parse")
async def debug_parse(url: str):
    url = _normalize_url(url)
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
            r = await client.get(url, timeout=15)
        soup        = BeautifulSoup(r.text, "html.parser")
        header      = soup.select_one(".b-title-rating__rating")
        table       = soup.select_one("table.object-rating-table")
        rows        = table.select("tr:not(thead tr)") if table else []
        bond_tables = soup.select(".b-actions__rates table")
        bond_preview = []
        for t in bond_tables[:3]:
            for row in t.select("tr:not(thead tr)")[:2]:
                bond_preview.append([c.get_text(strip=True)[:40]
                                     for c in row.find_all("td")])
        return JSONResponse({
            "status_code":       r.status_code,
            "html_length":       len(r.text),
            "header_rating":     header.get_text(strip=True) if header else None,
            "company_table":     table is not None,
            "company_rows":      len(rows),
            "bond_tables_count": len(bond_tables),
            "bond_rows_preview": bond_preview,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/emitents")
async def list_emitents(in_portfolio: bool = False):
    emitents = db.get_all_emitents_for_ratings(in_portfolio=in_portfolio)
    return JSONResponse(emitents)


@router.post("/set-url")
async def set_raexpert_url(req: SetUrlRequest):
    url = _normalize_url(req.raexpert_url)
    db.set_raexpert_url(req.emitent_id, url)
    return JSONResponse({"ok": True, "emitent_id": req.emitent_id, "url": url})


@router.post("/fetch/{emitent_id}")
async def fetch_rating(emitent_id: int):
    rating_row = db.get_latest_rating(emitent_id)
    if not rating_row or not rating_row.get("raexpert_url"):
        return JSONResponse({"error": "Ссылка не задана"}, status_code=404)
    try:
        parsed = await _parse_raexpert_page(rating_row["raexpert_url"])
        _save_parsed(emitent_id, rating_row["raexpert_url"], parsed)
        latest = db.get_latest_rating(emitent_id)
        return JSONResponse({
            "ok": True, "emitent_id": emitent_id,
            "rating":      latest.get("rating"),
            "forecast":    latest.get("forecast"),
            "under_watch": latest.get("under_watch"),
            "rating_date": latest.get("rating_date"),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/history/{emitent_id}")
async def rating_history(emitent_id: int):
    return JSONResponse(db.get_rating_history(emitent_id))


@router.post("/fetch-all")
async def start_fetch_all(delay: float = 4.0):
    if _crawl_state["running"]:
        return JSONResponse({"error": "Уже запущен"}, status_code=409)
    emitents = [e for e in db.get_all_emitents_for_ratings()
                if e.get("raexpert_url")]
    if not emitents:
        return JSONResponse({"error": "Нет эмитентов со ссылкой"}, status_code=400)
    asyncio.create_task(_crawl_task(emitents, delay=delay))
    return JSONResponse({"ok": True, "total": len(emitents), "delay": delay})


@router.get("/fetch-all/status")
async def fetch_all_status():
    return JSONResponse(_crawl_state)


@router.post("/fetch-all/stop")
async def stop_fetch_all():
    _crawl_state["running"] = False
    return JSONResponse({"ok": True})


# ── Управление эмитентами ──────────────────────────────────────────────────

@router.get("/emitents/{emitent_id}/instruments")
async def get_emitent_instruments(emitent_id: int):
    """Возвращает все бумаги эмитента."""
    return JSONResponse(db.get_emitent_instruments(emitent_id))


@router.put("/emitents/{emitent_id}")
async def apply_emitent_rename(emitent_id: int, body: dict):
    """Применяет новое имя — проверяет конфликт и возвращает превью.
    Если конфликт — возвращает данные существующего эмитента и его бумаги.
    """
    new_name = (body.get("name") or "").strip()
    if not new_name:
        return JSONResponse({"error": "Имя не может быть пустым"}, status_code=400)

    result = db.rename_emitent(emitent_id, new_name)

    if result["action"] == "renamed":
        emitent = db.get_emitent_by_id(emitent_id)
        instruments = db.get_emitent_instruments(emitent_id)
        return JSONResponse({
            "action":      "renamed",
            "emitent":     emitent,
            "instruments": instruments,
        })

    # Конфликт — возвращаем данные обоих для подтверждения
    target     = result["target"]
    target_ins = db.get_emitent_instruments(target["id"])
    dup_ins    = db.get_emitent_instruments(emitent_id)
    return JSONResponse({
        "action":             "conflict",
        "duplicate_id":       emitent_id,
        "target":             target,
        "target_instruments": target_ins,
        "duplicate_instruments": dup_ins,
    })


class MergeRequest(BaseModel):
    duplicate_id: int
    target_id:    int


@router.post("/emitents/merge")
async def merge_emitents(req: MergeRequest):
    """Объединяет дубль в легитимного эмитента."""
    if req.duplicate_id == req.target_id:
        return JSONResponse({"error": "duplicate_id и target_id совпадают"}, status_code=400)

    # Проверяем что оба существуют
    dup    = db.get_emitent_by_id(req.duplicate_id)
    target = db.get_emitent_by_id(req.target_id)
    if not dup:
        return JSONResponse({"error": f"Эмитент {req.duplicate_id} не найден"}, status_code=404)
    if not target:
        return JSONResponse({"error": f"Эмитент {req.target_id} не найден"}, status_code=404)

    result = db.merge_emitents(req.duplicate_id, req.target_id)
    logger.info("Объединены эмитенты: дубль id=%d → легитимный id=%d (%s)",
                req.duplicate_id, req.target_id, target["name"])
    return JSONResponse({"ok": True, "target": target, **result})