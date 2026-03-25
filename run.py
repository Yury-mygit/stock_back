"""
Точка входа для запуска сервера напрямую и через PyInstaller .exe.

Использование:
    python run.py            — запуск сервера (с иконкой в трее если доступен pystray)
    python run.py --no-tray  — запуск без трея (только консоль)
    moex_proxy.exe           — то же после сборки
"""
import sys
import os
import threading
import signal

# PyInstaller: добавляем временную директорию в sys.path
if hasattr(sys, "_MEIPASS"):
    sys.path.insert(0, sys._MEIPASS)

# Директория exe/скрипта
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn
from app import config
from app.logger import logger


def _run_server():
    """Запускает uvicorn сервер (блокирующий вызов)."""
    logger.info(
        "Запуск MOEX Proxy на http://%s:%d",
        config.HOST, config.PORT,
    )
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        reload=False,
    )


def run():
    use_tray = "--no-tray" not in sys.argv

    if not use_tray:
        # Простой режим — только консоль
        _run_server()
        return

    # Пробуем запустить трей
    from app.tray import start_tray, HAS_TRAY

    if not HAS_TRAY:
        logger.warning(
            "pystray/Pillow не установлены — трей недоступен. "
            "Установите: pip install pystray pillow"
        )
        _run_server()
        return

    # Сервер — в отдельном потоке, трей — в главном
    # (pystray требует главный поток на Windows)
    stop_event = threading.Event()

    server_thread = threading.Thread(
        target=_run_server,
        daemon=True,
        name="uvicorn",
    )
    server_thread.start()

    def on_tray_exit():
        """Вызывается из меню трея «Выход»."""
        logger.info("Завершение по команде из трея")
        stop_event.set()
        signal.raise_signal(signal.SIGINT)

    tray = start_tray(
        host=config.HOST,
        port=config.PORT,
        log_file=config.LOG_FILE,
        stop_callback=on_tray_exit,
    )

    stop_event.wait()
    if tray:
        tray.stop()
    logger.info("MOEX Proxy остановлен")


if __name__ == "__main__":
    run()
