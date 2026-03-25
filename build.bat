@echo off
setlocal

echo ============================================
echo  MOEX Proxy — сборка exe-файлов
echo ============================================

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller не найден. Выполните: pip install -r requirements.txt
    pause & exit /b 1
)

if not exist dist mkdir dist

set COMMON_OPTS=^
    --onefile ^
    --noconsole ^
    --add-data "app;app" ^
    --hidden-import uvicorn.logging ^
    --hidden-import uvicorn.loops ^
    --hidden-import uvicorn.loops.auto ^
    --hidden-import uvicorn.protocols ^
    --hidden-import uvicorn.protocols.http ^
    --hidden-import uvicorn.protocols.http.auto ^
    --hidden-import uvicorn.protocols.websockets ^
    --hidden-import uvicorn.protocols.websockets.auto ^
    --hidden-import uvicorn.lifespan ^
    --hidden-import uvicorn.lifespan.on ^
    --hidden-import fastapi ^
    --hidden-import httpx ^
    --hidden-import pystray ^
    --hidden-import PIL ^
    --hidden-import PIL.Image ^
    --hidden-import PIL.ImageDraw

echo.
echo [1/2] Сборка moex_proxy.exe  (приложение с треем)...
python -m PyInstaller %COMMON_OPTS% --name moex_proxy run.py

if errorlevel 1 (
    echo [ERROR] Сборка moex_proxy.exe завершилась с ошибкой
    pause & exit /b 1
)

echo.
echo [2/2] Сборка moex_proxy_svc.exe  (Windows-служба)...
python -m PyInstaller %COMMON_OPTS% ^
    --hidden-import win32serviceutil ^
    --hidden-import win32service ^
    --hidden-import win32event ^
    --hidden-import servicemanager ^
    --hidden-import win32api ^
    --name moex_proxy_svc service.py

if errorlevel 1 (
    echo [ERROR] Сборка moex_proxy_svc.exe завершилась с ошибкой
    pause & exit /b 1
)

echo.
echo ============================================
echo  Готово! Файлы в папке dist\:
echo.
echo    moex_proxy.exe      — запуск как приложение (иконка в трее)
echo    moex_proxy.exe --no-tray  — без трея, только консоль
echo    moex_proxy_svc.exe  — управление Windows-службой
echo.
echo  Прокси будет доступен на http://127.0.0.1:9011
echo ============================================
echo.
pause
