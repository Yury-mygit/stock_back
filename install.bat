@echo off
setlocal

:: Проверяем права администратора
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Требуются права администратора.
    echo Запустите файл от имени администратора.
    pause & exit /b 1
)

set SVC_EXE=%~dp0dist\moex_proxy_svc.exe
set ARG=%1

if "%ARG%"=="" (
    echo Использование:
    echo   install.bat install   — установить и запустить службу
    echo   install.bat remove    — остановить и удалить службу
    echo   install.bat start     — запустить службу
    echo   install.bat stop      — остановить службу
    echo   install.bat restart   — перезапустить службу
    echo   install.bat status    — статус службы
    pause & exit /b 0
)

if not exist "%SVC_EXE%" (
    echo [ERROR] Файл не найден: %SVC_EXE%
    echo Сначала выполните сборку: build.bat
    pause & exit /b 1
)

if "%ARG%"=="install" (
    echo Установка службы MoexProxy...
    "%SVC_EXE%" install
    echo Настройка автозапуска...
    sc config MoexProxy start= auto
    echo Запуск службы...
    "%SVC_EXE%" start
    echo.
    echo [OK] Служба установлена и запущена
    echo      Прокси доступен на http://127.0.0.1:8000
    goto end
)

if "%ARG%"=="remove" (
    echo Остановка службы...
    "%SVC_EXE%" stop 2>nul
    timeout /t 2 /nobreak >nul
    echo Удаление службы...
    "%SVC_EXE%" remove
    echo [OK] Служба удалена
    goto end
)

if "%ARG%"=="start"   "%SVC_EXE%" start   & goto end
if "%ARG%"=="stop"    "%SVC_EXE%" stop    & goto end
if "%ARG%"=="restart" "%SVC_EXE%" restart & goto end

if "%ARG%"=="status" (
    sc query MoexProxy
    goto end
)

echo [ERROR] Неизвестная команда: %ARG%

:end
echo.
pause
