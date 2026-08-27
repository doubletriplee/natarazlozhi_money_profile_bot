# Развёртывание и откат

Целевой каталог: `/opt/natarazlozhi_money_profile_bot`. Контейнер работает от UID/GID 10001,
с read-only root filesystem, без Linux capabilities, с лимитом 0,5 CPU и 384 МБ RAM. HTTP опубликован
только на `127.0.0.1:18080`.

## Первый запуск

1. Скопировать `compose.yaml`, создать `runtime/data`, положить GeoNames DB в
   `runtime/data/cities.sqlite3` и дать каталог UID 10001.
2. Создать `.env` с правами `600`, заполнить все обязательные production-поля.
3. Установить Caddy-файл из `deploy/` в существующий `sites-enabled`, выполнить `caddy validate`.
4. Запустить образ по точному commit SHA из GHCR и проверить `/healthz`, `/privacy`, `/terms`, `/source`.
5. Только после TLS-проверки настроить URL в Robokassa и включить платёжный поток.

На сервере `195.19.7.56` системный DNS возвращает недоступный маршрут до Telegram. Поэтому
Compose задаёт `api.telegram.org` через `TELEGRAM_API_IPV4`; перед обновлением адреса сначала
проверьте TLS-запросом, что новый IPv4 действительно обслуживает `api.telegram.org`.

## Обновление

Перед обновлением остановить запись на время snapshot, запустить `scripts/backup.py`, проверить созданный
AES-GCM файл, затем изменить `APP_IMAGE` на новый SHA и выполнить `docker compose up -d`. После запуска
проверить health-check и callback в тестовом режиме.

## Откат

Вернуть предыдущий SHA образа. Если новая версия меняла схему, остановить контейнер, восстановить
предмиграционную копию в новый файл через `restore_backup.py`, проверить `PRAGMA integrity_check`, затем
атомарно заменить рабочую БД. Нельзя восстанавливать БД поверх работающего SQLite-файла.
