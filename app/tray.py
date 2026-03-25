"""
Системный трей для MOEX Proxy.

Запускается вместе с сервером когда приложение стартует как .exe (не служба).
Иконка в трее даёт доступ к меню:
  - Статус (адрес сервера)
  - Открыть лог
  - Перезапустить сервер
  - Выход
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False


def _make_icon(size: int = 64) -> "Image.Image":
    """
    Рисуем простую иконку: тёмный квадрат с буквой «M» (MOEX).
    Не требует внешних .ico файлов.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Фон — тёмно-синий скруглённый квадрат
    r = size // 6
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(15, 30, 60, 255))

    # Буква M — белая, простыми линиями
    m = size // 7
    h = size - 2 * m
    pts = [
        (m,         size - m),
        (m,         m),
        (size // 2, size // 2),
        (size - m,  m),
        (size - m,  size - m),
    ]
    draw.line(pts, fill=(0, 212, 255, 255), width=max(2, size // 14))

    return img


class TrayApp:
    def __init__(self, host: str, port: int, log_file: Path, stop_callback):
        self._host = host
        self._port = port
        self._log_file = log_file
        self._stop_callback = stop_callback
        self._icon: pystray.Icon | None = None

    # ------------------------------------------------------------------
    def _on_open_log(self, icon, item):
        if self._log_file.exists():
            os.startfile(str(self._log_file))
        else:
            os.startfile(str(self._log_file.parent))

    def _on_open_browser(self, icon, item):
        import webbrowser
        webbrowser.open(f"http://{self._host}:{self._port}/docs")

    def _on_exit(self, icon, item):
        icon.stop()
        self._stop_callback()

    # ------------------------------------------------------------------
    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                f"MOEX Proxy  ·  :{self._port}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Открыть Swagger Docs", self._on_open_browser),
            pystray.MenuItem("Открыть лог",          self._on_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход",                self._on_exit),
        )

    def run(self):
        """Запускает трей в текущем потоке (блокирующий вызов)."""
        if not HAS_TRAY:
            return
        self._icon = pystray.Icon(
            name="MoexProxy",
            icon=_make_icon(),
            title=f"MOEX Proxy  :{self._port}",
            menu=self._build_menu(),
        )
        self._icon.run()

    def stop(self):
        if self._icon:
            self._icon.stop()


# ------------------------------------------------------------------
# Публичный хелпер — запускает трей в отдельном потоке
# ------------------------------------------------------------------

def start_tray(host: str, port: int, log_file: Path, stop_callback) -> TrayApp | None:
    """
    Создаёт и запускает TrayApp в отдельном daemon-потоке.
    Возвращает TrayApp или None если pystray недоступен.
    """
    if not HAS_TRAY:
        return None

    app = TrayApp(host, port, log_file, stop_callback)
    t = threading.Thread(target=app.run, daemon=True, name="tray")
    t.start()
    return app
