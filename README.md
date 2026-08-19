# Три буквы от Сани

## Установка одной командой на Linux

Поддерживаются Debian 12/13 и Ubuntu с `apt`, `systemd` и nginx. Для HTTPS нужен сервер с открытыми TCP-портами `80` и `443`, а DNS A-запись домена должна указывать на его публичный IP. Домен не обязателен: без него приложение работает по HTTP, а домен можно подключить позже повторным запуском.

## Установка напрямую из GitHub

После публикации проекта в публичном GitHub-репозитории установить его можно без предварительного копирования файлов:

```bash
curl -fsSL https://raw.githubusercontent.com/DE1C1DE/sanya-vpn-sait/main/install-from-github.sh \
  | sudo bash -s -- \
    --repo DE1C1DE/sanya-vpn-sait \
    --domain vpn.example.com \
    --admin-password 'сложный-пароль'
```

Bootstrap-скрипт скачивает архив указанной ветки, находит в нем `install.sh` и запускает его.

Для конкретного тега или ветки:

```bash
curl -fsSL https://raw.githubusercontent.com/DE1C1DE/sanya-vpn-sait/main/install-from-github.sh \
  | sudo bash -s -- \
    --repo DE1C1DE/sanya-vpn-sait \
    --ref v1.0.0 \
    --domain vpn.example.com \
    --admin-password 'сложный-пароль'
```

Одной командой с уже сгенерированным паролем:

```bash
curl -fsSL https://raw.githubusercontent.com/DE1C1DE/sanya-vpn-sait/main/install-from-github.sh \
  | sudo bash -s -- \
    --repo DE1C1DE/sanya-vpn-sait \
    --domain vpn.example.com
```

Пароль в этом случае будет сгенерирован установщиком и выведен в конце.

`--domain` не обязателен: без него установка проходит в HTTP-режиме без сертификата. Повторный запуск той же команды с другим `--domain` переключает установленный сервер на новый домен и выпускает сертификат для него.

Для приватного репозитория не передавайте GitHub-токен в URL. Используйте deploy key, GitHub Actions artifact или предварительную авторизацию GitHub CLI на сервере. Перед запуском через `curl | bash` рекомендуется закреплять конкретный тег, а не `main`.

Скопируйте каталог проекта на Linux-сервер и выполните от root:

```bash
sudo bash install.sh --domain vpn.example.com --admin-password 'сложный-пароль'
```

Без домена (HTTP-режим, сертификат не выпускается):

```bash
sudo bash install.sh --admin-password 'сложный-пароль'
```

Подключить домен и HTTPS на уже установленном сервере:

```bash
sudo bash install.sh --domain vpn.example.com --admin-password 'сложный-пароль'
```

Повторный запуск с другим `--domain` переключает сервер на новый домен.

Установщик:

- установит Python, nginx, SQLite, Certbot и системные сертификаты;
- установит приложение в `/opt/tri-bukvy`;
- создаст systemd-сервис `tri-bukvy`;
- создаст `/etc/tri-bukvy.env` с секретами;
- настроит nginx на HTTP/HTTPS;
- получит доверенный сертификат Let’s Encrypt;
- запустит приложение и выведет URL сайта, админки и пароль.

Если пароль не передать, установщик сгенерирует случайный пароль:

```bash
sudo bash install.sh --domain vpn.example.com
```

Публичный адрес можно задать отдельно:

```bash
sudo bash install.sh --domain vpn.example.com --public-origin https://vpn.example.com
```

Локальная база `data.sqlite3` переносится автоматически только если на сервере ее еще нет. Для осознанной замены существующей базы используйте:

```bash
sudo bash install.sh --domain vpn.example.com --force-db
```

Перед `--force-db` сделайте резервную копию текущей базы.

## Резервные копии

Для SQLite-базы на уже установленном сервере:

```bash
sudo bash backup.sh
```

По умолчанию копии создаются в `/var/backups/tri-bukvy`. Другой каталог можно передать параметром:

```bash
sudo bash backup.sh /mnt/backups/tri-bukvy
```

Кроме этого, администратор может скачать JSON-копию пользователей и серверов из админки. JSON предназначен для переноса логической структуры, а `backup.sh` сохраняет полную SQLite-базу вместе с кэшем подписок.

## Обновление приложения

Скопируйте новый `app.py` в проект и выполните:

```bash
sudo install -m 0644 app.py /opt/tri-bukvy/app.py
sudo systemctl restart tri-bukvy
sudo systemctl status tri-bukvy --no-pager
curl -fsS https://vpn.example.com/health
```

## Проверка и диагностика

```bash
sudo systemctl status tri-bukvy --no-pager
sudo journalctl -u tri-bukvy -n 100 --no-pager
sudo nginx -t
sudo systemctl status nginx --no-pager
```

Для автопродления сертификата Certbot обычно создает systemd timer. Проверить его можно так:

```bash
sudo systemctl list-timers | grep certbot
```

## Перенос на новый сервер

1. Выполните `sudo bash backup.sh` на старом сервере.
2. Скопируйте `data-*.sqlite3` на новый сервер как `data.sqlite3` рядом с `install.sh`.
3. Укажите DNS A-запись домена на новый IP.
4. Запустите `install.sh` с тем же доменом.
5. Не используйте `--force-db`, если база уже была скопирована до установки.
6. Проверьте `/health`, вход в админку и одну ссылку `/sub/ID`.

Не переносите `/etc/tri-bukvy.env` в публичный репозиторий: в нем находится пароль администратора.

Небольшой центральный портал для 3x-ui подписок. Пользователь получает персональный ID без пароля, а v2rayTun получает стабильную ссылку вида `https://CENTRAL_IP/sub/TOKEN`. Endpoint подписки отвечает стандартным base64-кодированным списком `vless://` строк, который используется большинством VLESS-клиентов для автоматического обновления.

## Локальный запуск

```bash
VPN_PORTAL_ADMIN_PASSWORD='сложный-пароль' python3 app.py
```

Для текущего развертывания пароль администратора задается переменной окружения `VPN_PORTAL_ADMIN_PASSWORD`, которая не хранится в репозитории.

Откройте `http://127.0.0.1:8080/admin`. В админке создайте пользователя и добавьте URL его исходной подписки 3x-ui. ID пользователя можно передать пользователю. На его странице будет URL центральной подписки.

## Обновление IP

В админке укажите старый и новый IP. Операция заменяет точное совпадение старого IP только в URL сохраненных источников. Подписки с другими IP не меняются. После следующего запроса клиента портал скачает новый источник и отдаст новые `vless://` строки.

## HTTPS по IP через nginx

Для рабочего сервера проксируйте nginx на `127.0.0.1:8080`. Сертификат для IP должен содержать этот IP в SAN. Публичные центры сертификации поддерживают IP-сертификаты не всегда; самоподписанный сертификат будет требовать установки корневого сертификата на каждом устройстве пользователя, а некоторые клиенты не примут его вообще. Для v2rayTun это особенно важно: HTTPS должен быть доверенным на устройстве.

Пример `/etc/nginx/sites-available/vpn-portal`:

```nginx
server {
    listen 443 ssl;
    server_name 203.0.113.10;
    ssl_certificate /etc/ssl/vpn-portal/fullchain.pem;
    ssl_certificate_key /etc/ssl/vpn-portal/privkey.pem;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}
server { listen 80; return 301 https://$host$request_uri; }
```

Сервис можно запускать через systemd. Перед запуском задайте `VPN_PORTAL_ADMIN_PASSWORD` и ограничьте права на каталог с `data.sqlite3`. Для защиты от перебора ID рекомендуется поставить rate limit в nginx и использовать длинные случайные ID.

## Важные ограничения

- Исходные ссылки 3x-ui скачиваются при каждом обновлении, поэтому центральный сервер должен иметь исходящий HTTPS-доступ к панелям.
- Пароль администратора задается переменной окружения и не хранится в SQLite.
- Реализован один администратор, как было запрошено.
- Клиентский endpoint отдает только base64-данные со строками `vless://`, без HTML и без служебных сообщений.
- Если клиент не распознает подписку, проверьте, что URL добавлен именно как подписка, а не как одиночная VLESS-ссылка, и что HTTPS-сертификат центрального сервера доверен устройству.
