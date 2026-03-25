# MOEX ISS Proxy

Локальный прокси-сервер для доступа к API Московской биржи ([MOEX ISS](https://iss.moex.com)).  
Решает проблему CORS при обращении к `iss.moex.com` из браузерных приложений.

---

## Быстрый старт

### Запуск без сборки (через Python)

#### Go to folder
```bash
cd D:\stock\back
```

### Создать и активировать виртуальное окружение
```bash
python -m venv .venv
```
```bash
.venv\Scripts\activate
```
### Установить зависимости
```bash
pip install -r requirements.txt
```
### Запустить
```bash
python run.py
```



Сервер запустится на 
```bash 
http://127.0.0.1:9011
```

---

## Использование

После запуска замените `https://iss.moex.com/iss/` на `http://127.0.0.1:9011/iss/`:

```
# Было (прямой запрос — CORS-ошибка в браузере)
https://iss.moex.com/iss/securities/SBER.json?iss.meta=off

# Стало (через прокси — работает)
http://127.0.0.1:9011/iss/securities/SBER.json?iss.meta=off
```

Проверка работоспособности:
```
GET http://127.0.0.1:9011/health
```

Swagger-документация API:
```
http://127.0.0.1:9011/docs
```

---

## Настройка

Параметры задаются через переменные окружения или `.env`-файл:

| Переменная             | По умолчанию  | Описание                          |
|------------------------|---------------|-----------------------------------|
| `MOEX_PROXY_HOST`      | `127.0.0.1`   | Адрес для прослушивания           |
| `MOEX_PROXY_PORT`      | `9011`        | Порт                              |
| `MOEX_PROXY_TIMEOUT`   | `15`          | Таймаут запроса к MOEX ISS (сек)  |
| `MOEX_PROXY_CORS`      | `*`           | CORS origins через запятую        |
| `MOEX_PROXY_LOG_LEVEL` | `INFO`        | Уровень логов (DEBUG/INFO/WARNING)|
| `MOEX_PROXY_LOG_FILE`  | `1`           | Запись логов в файл (0/1)         |

Логи пишутся в папку `logs/moex_proxy.log` рядом с exe-файлом.

---

## Сборка в .exe

Требования: Python 3.11+, Windows.

```bat
pip install -r requirements.txt
build.bat
```

В папке `dist\` появятся два файла:
- `moex_proxy.exe` — запуск как обычное приложение (консоль)
- `moex_proxy_svc.exe` — установка и управление Windows-службой

---

## Windows-служба

Все команды выполняются **от имени администратора**.

### Через install.bat

```bat
install.bat install   # установить и запустить (автостарт при загрузке Windows)
install.bat stop      # остановить
install.bat start     # запустить
install.bat restart   # перезапустить
install.bat status    # проверить статус
install.bat remove    # удалить службу
```

### Напрямую через exe

```bat
dist\moex_proxy_svc.exe install
dist\moex_proxy_svc.exe start
dist\moex_proxy_svc.exe stop
dist\moex_proxy_svc.exe remove
```

---


## Системный трей

При запуске как `.exe` (или `python run.py`) в области уведомлений Windows появляется иконка **«M»** с меню:

| Пункт меню | Действие |
|---|---|
| `MOEX Proxy · :9011` | Статус (не кликабельно) |
| `Открыть Swagger Docs` | Открывает браузер на `/docs` |
| `Открыть лог` | Открывает файл `logs/moex_proxy.log` |
| `Выход` | Останавливает сервер и закрывает приложение |

Запуск без трея:
```bat
moex_proxy.exe --no-tray
python run.py --no-tray
```

> Трей требует `pystray` и `Pillow`. При их отсутствии приложение запустится в режиме консоли автоматически.

## Структура проекта

```
moex-proxy/
├── app/
│   ├── __init__.py
│   ├── config.py      # настройки
│   ├── logger.py      # логирование
│   ├── main.py        # FastAPI приложение
│   └── proxy.py       # логика проксирования
├── run.py             # запуск сервера
├── service.py         # Windows-служба
├── build.bat          # сборка exe через PyInstaller
├── install.bat        # управление службой
├── requirements.txt
└── README.md
```

---

## Лицензия

MIT. Данные MOEX ISS предоставляются только для ознакомления.  
Коммерческое использование требует договора с ПАО Московская Биржа.
