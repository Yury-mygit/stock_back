from contextlib import asynccontextmanager
from datetime import date as dt_date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import config, db
from app.logger import logger
from app.proxy import router as proxy_router, close_client
from app.portfolio_api import router as portfolio_router, fetch_and_store_rates


scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # Проверяем курсы при старте — если на сегодня нет, запрашиваем
    today = dt_date.today().isoformat()
    if not db.get_exchange_rates(today):
        logger.info("Курсы на сегодня (%s) отсутствуют — запрашиваем...", today)
        await fetch_and_store_rates()
    else:
        logger.info("Курсы на сегодня (%s) уже есть в БД", today)

    # Планировщик: каждый день в 01:00 обновляем курсы
    scheduler.add_job(fetch_and_store_rates, "cron", hour=1, minute=0,
                      id="daily_rates", replace_existing=True)
    scheduler.start()
    logger.info("Планировщик запущен (курсы обновляются в 01:00)")

    logger.info("MOEX Proxy запущен  →  http://%s:%d", config.HOST, config.PORT)
    logger.info("CORS origins: %s", config.CORS_ORIGINS)
    yield

    scheduler.shutdown(wait=False)
    await close_client()
    logger.info("MOEX Proxy остановлен")


app = FastAPI(
    title="MOEX ISS Proxy",
    description="Локальный прокси для API Московской биржи + портфель облигаций",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

app.include_router(proxy_router)
app.include_router(portfolio_router)


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "moex_base": config.MOEX_BASE_URL, "db": str(config.DB_PATH)})


@app.get("/")
async def root():
    return JSONResponse({
        "service": "MOEX ISS Proxy + Portfolio",
        "version": "1.1.0",
        "endpoints": {
            "proxy":     "GET  /iss/{path}",
            "portfolio": "GET/POST/PUT/DELETE  /portfolio",
            "sync":      "POST /portfolio/{secid}/sync",
            "sync_all":  "POST /portfolio/sync-all",
        },
        "docs": f"http://{config.HOST}:{config.PORT}/docs",
    })