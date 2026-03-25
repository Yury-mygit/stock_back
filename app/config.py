"""
Настройки MOEX Proxy сервера.
Все параметры можно переопределить через переменные окружения или файл .env
"""
import os
from pathlib import Path

# Директория где лежит исполняемый файл (нужно для PyInstaller)
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent

# Сервер
HOST: str = os.getenv("MOEX_PROXY_HOST", "127.0.0.1")
PORT: int = int(os.getenv("MOEX_PROXY_PORT", "9011"))

# MOEX ISS
MOEX_BASE_URL: str = "https://iss.moex.com/iss"
REQUEST_TIMEOUT: float = float(os.getenv("MOEX_PROXY_TIMEOUT", "15"))

# CORS — через запятую, * = все
CORS_ORIGINS: list[str] = os.getenv("MOEX_PROXY_CORS", "*").split(",")

# Логирование
LOG_LEVEL: str = os.getenv("MOEX_PROXY_LOG_LEVEL", "INFO")
LOG_TO_FILE: bool = os.getenv("MOEX_PROXY_LOG_FILE", "1") == "1"
LOG_FILE: Path = BASE_DIR / "logs" / "moex_proxy.log"
LOG_MAX_BYTES: int = 5 * 1024 * 1024   # 5 MB
LOG_BACKUP_COUNT: int = 3

# Windows-служба
SERVICE_NAME: str = "MoexProxy"
SERVICE_DISPLAY_NAME: str = "MOEX ISS Proxy Server"
SERVICE_DESCRIPTION: str = "Локальный прокси-сервер для доступа к API Московской биржи (MOEX ISS)"

# База данных
DB_PATH: Path = BASE_DIR / "portfolio.db"