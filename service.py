"""
Установка и управление Windows-службой для MOEX Proxy.

Команды:
    python service.py install    — установить службу
    python service.py start      — запустить
    python service.py stop       — остановить
    python service.py restart    — перезапустить
    python service.py remove     — удалить службу

    moex_proxy_svc.exe install   — то же через собранный exe
"""
import sys
import os
import time
import subprocess

# PyInstaller path fix
if hasattr(sys, "_MEIPASS"):
    sys.path.insert(0, sys._MEIPASS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    import win32api
except ImportError:
    print("ERROR: pywin32 не установлен. Выполните: pip install pywin32")
    sys.exit(1)

from app import config
from app.logger import logger


class MoexProxyService(win32serviceutil.ServiceFramework):
    _svc_name_ = config.SERVICE_NAME
    _svc_display_name_ = config.SERVICE_DISPLAY_NAME
    _svc_description_ = config.SERVICE_DESCRIPTION

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._process: subprocess.Popen | None = None

    def SvcStop(self):
        logger.info("Служба получила команду остановки")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        self._terminate()

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        logger.info("Служба %s запускается", self._svc_name_)
        self._run()

    def _get_exe_path(self) -> str:
        """Путь к run.py или к exe-файлу при сборке через PyInstaller."""
        if hasattr(sys, "_MEIPASS"):
            # Запущено из exe — ищем run.exe рядом
            base = os.path.dirname(sys.executable)
            exe = os.path.join(base, "moex_proxy.exe")
            if os.path.exists(exe):
                return exe
        # Запущено как скрипт
        base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "run.py")

    def _run(self):
        exe_path = self._get_exe_path()

        if exe_path.endswith(".exe"):
            cmd = [exe_path]
        else:
            cmd = [sys.executable, exe_path]

        logger.info("Запуск процесса: %s", " ".join(cmd))

        self._process = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # Ждём сигнала остановки
        while True:
            rc = win32event.WaitForSingleObject(self._stop_event, 5000)
            if rc == win32event.WAIT_OBJECT_0:
                break
            # Проверяем что процесс ещё жив
            if self._process.poll() is not None:
                logger.error("Процесс сервера неожиданно завершился, перезапуск...")
                self._process = subprocess.Popen(
                    cmd,
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

        self._terminate()

    def _terminate(self):
        if self._process and self._process.poll() is None:
            logger.info("Останавливаем процесс сервера (pid=%d)", self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            logger.info("Процесс сервера остановлен")


def main():
    if len(sys.argv) == 1:
        # Запущено как служба Windows (SCM вызвал напрямую)
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MoexProxyService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(MoexProxyService)


if __name__ == "__main__":
    main()
