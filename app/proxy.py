"""
Логика проксирования запросов к MOEX ISS API.

Маршрут:  GET /iss/{path:path}?{params}
Пример:   GET /iss/securities/SBER.json?iss.meta=off
"""
import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response

from app import config
from app.logger import logger

router = APIRouter()

# Один переиспользуемый httpx-клиент (connection pool)
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=config.MOEX_BASE_URL,
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "MoexProxy/1.0 (local)",
                "Accept": "application/json, text/html, */*",
            },
        )
    return _client


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        logger.info("HTTP client закрыт")


@router.get("/iss/{path:path}")
async def proxy_iss(path: str, request: Request) -> Response:
    """
    Проксирует любой GET-запрос к iss.moex.com/iss/{path}.
    Все query-параметры передаются как есть.
    """
    query = str(request.query_params)
    target = f"/{path}"
    if query:
        target += f"?{query}"

    logger.info("→ MOEX ISS  %s", target)

    try:
        resp = await get_client().get(target)
    except httpx.TimeoutException:
        logger.warning("Таймаут при запросе к %s", target)
        raise HTTPException(status_code=504, detail="MOEX ISS не ответил за отведённое время")
    except httpx.RequestError as exc:
        logger.error("Ошибка запроса к MOEX ISS: %s", exc)
        raise HTTPException(status_code=502, detail=f"Ошибка соединения с MOEX ISS: {exc}")

    logger.info("← %d  %s", resp.status_code, target)

    # Пробрасываем Content-Type из ответа MOEX
    content_type = resp.headers.get("content-type", "application/json")

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=content_type,
    )
