#!/usr/bin/env python3
import base64
import hashlib
import html
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("VPN_PORTAL_DB", os.path.join(ROOT, "data.sqlite3"))
HOST = os.environ.get("VPN_PORTAL_HOST", "127.0.0.1")
PORT = int(os.environ.get("VPN_PORTAL_PORT", "8080"))
ADMIN_PASSWORD = os.environ.get("VPN_PORTAL_ADMIN_PASSWORD", "")
PUBLIC_ORIGIN = os.environ.get("VPN_PORTAL_PUBLIC_ORIGIN", "")
XRAY_BINARY = os.environ.get("VPN_PORTAL_XRAY_BINARY", "/usr/local/x-ui/bin/xray-linux-amd64")
SESSION_COOKIE = "vpn_portal_session"
SESSIONS = {}


def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    with db() as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            access_id TEXT NOT NULL UNIQUE,
            subscription_id TEXT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            source_url TEXT NOT NULL,
            cached_links TEXT NOT NULL DEFAULT '[]',
            server_id INTEGER REFERENCES servers(id),
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS subscriptions_user_idx ON subscriptions(user_id);
        """)
        connection.execute("CREATE TABLE IF NOT EXISTS servers (id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT NOT NULL UNIQUE, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(subscriptions)")]
        if "cached_links" not in columns:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN cached_links TEXT NOT NULL DEFAULT '[]'")
        if "server_id" not in columns:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN server_id INTEGER REFERENCES servers(id)")
        user_columns = [row["name"] for row in connection.execute("PRAGMA table_info(users)")]
        if "subscription_id" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN subscription_id TEXT")
        for row in connection.execute("SELECT id, user_id, source_url FROM subscriptions WHERE server_id IS NULL").fetchall():
            address = subscription_address(row["source_url"])
            if address == "не определен":
                continue
            connection.execute("INSERT OR IGNORE INTO servers(ip) VALUES(?)", (address,))
            server = connection.execute("SELECT id FROM servers WHERE ip=?", (address,)).fetchone()
            connection.execute("UPDATE subscriptions SET server_id=? WHERE id=?", (server["id"], row["id"]))
            source_id = subscription_id_from_url(row["source_url"])
            if source_id:
                connection.execute("UPDATE users SET subscription_id=COALESCE(subscription_id, ?) WHERE id=?", (source_id, row["user_id"]))
        connection.execute("UPDATE users SET subscription_id=access_id WHERE subscription_id IS NULL OR subscription_id='' ")


def esc(value):
    return html.escape(str(value), quote=True)


def password_ok(value):
    return secrets.compare_digest(value, ADMIN_PASSWORD)


def fetch_vless(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Tri-Bukvy-Subscription/1.0"})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read(2_000_000)
    text = raw.decode("utf-8", errors="replace").strip()
    # 3x-ui commonly serves base64, while some panels serve newline-delimited URIs.
    compact = re.sub(r"\s+", "", text)
    try:
        decoded = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8")
        if "vless://" in decoded:
            text = decoded
    except (ValueError, UnicodeDecodeError):
        pass
    return [line.strip() for line in text.splitlines() if line.strip().lower().startswith("vless://")]


def vless_xray_config(link, port):
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    value = lambda key, default="": query.get(key, [default])[0]
    user = {"id": unquote(parsed.username or ""), "encryption": value("encryption", "none")}
    if value("flow"):
        user["flow"] = value("flow")
    stream = {"network": value("type", "tcp"), "security": value("security", "none")}
    if stream["security"] == "reality":
        stream["realitySettings"] = {
            "serverName": value("sni"), "fingerprint": value("fp", "chrome"),
            "publicKey": value("pbk"), "shortId": value("sid"), "spiderX": value("spx", "/"),
        }
    network = stream["network"]
    if network == "xhttp":
        settings = {"path": value("path", "/"), "mode": value("mode", "auto")}
        if value("host"):
            settings["host"] = value("host")
        if value("extra"):
            try:
                settings["extra"] = json.loads(value("extra"))
            except ValueError:
                pass
        stream["xhttpSettings"] = settings
    elif network == "ws":
        stream["wsSettings"] = {"path": value("path", "/"), "headers": {"Host": value("host")}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": value("serviceName")}
    return {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
        "outbounds": [{"protocol": "vless", "settings": {"vnext": [{"address": parsed.hostname, "port": parsed.port, "users": [user]}]}, "streamSettings": stream}],
    }


def vless_endpoint_available(link):
    try:
        parsed = urlparse(link)
        if parsed.scheme.lower() != "vless" or not parsed.hostname or not parsed.port:
            return False
        if os.path.isfile(XRAY_BINARY):
            with socket.socket() as probe_socket:
                probe_socket.bind(("127.0.0.1", 0))
                local_port = probe_socket.getsockname()[1]
            with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as config_file:
                json.dump(vless_xray_config(link, local_port), config_file)
                config_path = config_file.name
            process = subprocess.Popen([XRAY_BINARY, "run", "-config", config_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.35)
                check = subprocess.run(
                    ["curl", "--silent", "--show-error", "--fail", "--socks5-hostname", "127.0.0.1:%d" % local_port, "--connect-timeout", "3", "--max-time", "5", "https://cp.cloudflare.com/generate_204"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6,
                )
                return check.returncode == 0
            finally:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                os.unlink(config_path)
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def replace_host_in_url(url, old_ip, new_ip):
    # Replace only URL host occurrences, preserving ports, paths and query strings.
    return re.sub(r"(?<![A-Za-z0-9.-])" + re.escape(old_ip) + r"(?=[:/?#]|$)", new_ip, url)


def subscription_host(url):
    try:
        return urlparse(url).hostname or "не определен"
    except ValueError:
        return "не определен"


def subscription_address(url):
    try:
        return urlparse(url).netloc or "не определен"
    except ValueError:
        return "не определен"


def subscription_url(ip, subscription_id):
    return "https://%s/sub/%s" % (ip, subscription_id)


def subscription_id_from_url(url):
    match = re.search(r"/sub/([A-Za-z0-9_-]+)(?:$|[/?#])", urlparse(url).path)
    return match.group(1) if match else ""


def user_by_access(access_id):
    with db() as connection:
        return connection.execute("SELECT * FROM users WHERE (access_id=? OR subscription_id=?) AND active=1", (access_id, access_id)).fetchone()


def subscription_rows(user_id):
    with db() as connection:
        rows = connection.execute("SELECT subscriptions.*, servers.ip AS server_ip, users.subscription_id AS user_subscription_id FROM subscriptions JOIN users ON users.id=subscriptions.user_id LEFT JOIN servers ON servers.id=subscriptions.server_id WHERE subscriptions.user_id=? AND subscriptions.active=1 ORDER BY subscriptions.id", (user_id,)).fetchall()
        return [dict(row, source_url=subscription_url(row["server_ip"], row["user_subscription_id"]) if row["server_ip"] and row["user_subscription_id"] else row["source_url"]) for row in rows]


def all_servers():
    with db() as connection:
        return connection.execute("SELECT * FROM servers WHERE active=1 ORDER BY id").fetchall()


def add_server_for_users(ip):
    with db() as connection:
        connection.execute("INSERT INTO servers(ip) VALUES(?)", (ip,))
        server = connection.execute("SELECT id FROM servers WHERE ip=?", (ip,)).fetchone()
        users = connection.execute("SELECT id, subscription_id, access_id FROM users WHERE active=1").fetchall()
        for user in users:
            connection.execute("INSERT INTO subscriptions(user_id, label, source_url, server_id) VALUES(?,?,?,?)", (user["id"], "Сервер 1", subscription_url(ip, user["subscription_id"] or user["access_id"]), server["id"]))
        connection.execute("UPDATE subscriptions SET label='Сервер ' || (SELECT COUNT(*) FROM subscriptions s2 WHERE s2.user_id=subscriptions.user_id AND s2.id<=subscriptions.id) WHERE server_id=?", (server["id"],))
        return len(users)


def collect_links(user_id, use_cache=True):
    subscriptions = subscription_rows(user_id)
    result = {}
    errors = []

    def load(subscription):
        try:
            return subscription, fetch_vless(subscription["source_url"]), None
        except Exception as error:
            cached = []
            if use_cache:
                try:
                    cached = json.loads(subscription["cached_links"] or "[]")
                except (TypeError, ValueError):
                    cached = []
            return subscription, cached, str(error)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(subscriptions)))) as executor:
        futures = [executor.submit(load, subscription) for subscription in subscriptions]
        for future in as_completed(futures):
            subscription, links, error = future.result()
            result[subscription["id"]] = {"id": subscription["id"], "label": subscription["label"], "source_url": subscription["source_url"], "links": links}
            if error:
                message = "Источник недоступен, показаны последние сохраненные ссылки" if use_cache and links else "Источник временно недоступен"
                errors.append({"label": subscription["label"], "message": message})
            else:
                with db() as connection:
                    connection.execute("UPDATE subscriptions SET cached_links=? WHERE id=?", (json.dumps(links), subscription["id"]))
    return [result[subscription["id"]] for subscription in subscriptions], errors


def page(title, body, extra=""):
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>%s | Три буквы от Сани</title><link rel="stylesheet" href="/static/style.css"></head><body><main class="shell"><header class="top"><a class="brand" href="/">ТРИ БУКВЫ <span>ОТ САНИ</span></a><div class="top-actions">%s</div></header>%s</main></body></html>""" % (esc(title), extra, body)


def form_value(values, key):
    return values.get(key, [""])[0].strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send(self, status, content, content_type="text/html; charset=utf-8"):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def body_values(self):
        length = int(self.headers.get("Content-Length", "0"))
        return parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)

    def session(self):
        cookie = self.headers.get("Cookie", "")
        match = re.search(r"(?:^|;\s*)" + SESSION_COOKIE + r"=([^;]+)", cookie)
        return SESSIONS.get(match.group(1)) if match else None

    def public_origin(self):
        if PUBLIC_ORIGIN:
            return PUBLIC_ORIGIN.rstrip("/")
        forwarded = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
        scheme = forwarded or "http"
        return scheme + "://" + self.headers.get("Host", "127.0.0.1:%d" % PORT)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/static/style.css":
            return self.send(200, CSS, "text/css; charset=utf-8")
        if path == "/health":
            return self.send(200, "ok", "text/plain; charset=utf-8")
        match = re.fullmatch(r"/sub/([A-Za-z0-9_-]{4,100})", path)
        if match:
            return self.subscription_response(match.group(1))
        if path == "/admin":
            return self.admin_page()
        if path == "/admin/export" and self.session():
            with db() as connection:
                data = {
                    "version": 1,
                    "users": [dict(row) for row in connection.execute("SELECT access_id,subscription_id,name,active FROM users ORDER BY id")],
                    "servers": [dict(row) for row in connection.execute("SELECT ip,active FROM servers ORDER BY id")],
                }
            content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="tri-bukvy-backup.json"')
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            return self.wfile.write(content)
        if path == "/admin/logout":
            cookie = self.headers.get("Cookie", "")
            match = re.search(r"(?:^|;\s*)" + SESSION_COOKIE + r"=([^;]+)", cookie)
            if match:
                SESSIONS.pop(match.group(1), None)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", SESSION_COOKIE + "=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        match = re.fullmatch(r"/u/([A-Za-z0-9_-]{4,100})", path)
        if match:
            return self.user_page(match.group(1))
        return self.home()

    def do_POST(self):
        parsed = urlparse(self.path)
        values = self.body_values()
        if parsed.path == "/login":
            access = form_value(values, "access_id")
            if user_by_access(access):
                return self.redirect("/u/" + quote(access))
            return self.home("Неверный ID или доступ отключен.")
        if parsed.path == "/admin/login":
            if password_ok(form_value(values, "password")):
                token = secrets.token_urlsafe(32)
                SESSIONS[token] = True
                self.send_response(303); self.send_header("Location", "/admin"); self.send_header("Set-Cookie", SESSION_COOKIE + "=" + token + "; HttpOnly; SameSite=Strict; Path=/"); self.send_header("Content-Length", "0"); self.end_headers(); return
            return self.admin_page("Неверный пароль администратора.")
        if not self.session():
            return self.admin_page("Сессия истекла. Войдите снова.")
        if parsed.path == "/admin/users/add":
            source_id = form_value(values, "subscription_id")
            name = form_value(values, "name")
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", source_id):
                return self.admin_page("Укажите корректный ID пользователя из 3x-ui.")
            if not name or len(name) > 120:
                return self.admin_page("Укажите имя пользователя.")
            access = secrets.token_urlsafe(18)
            with db() as connection:
                cursor = connection.execute("INSERT INTO users(access_id,subscription_id,name) VALUES(?,?,?)", (source_id, source_id, name))
                user_id = cursor.lastrowid
                for index, server in enumerate(connection.execute("SELECT * FROM servers WHERE active=1 ORDER BY id"), 1):
                    connection.execute("INSERT INTO subscriptions(user_id,label,source_url,server_id) VALUES(?,?,?,?)", (user_id, "Сервер %d" % index, subscription_url(server["ip"], source_id), server["id"]))
            return self.admin_page("Пользователь создан. ID можно скопировать из списка.")
        if parsed.path == "/admin/users/delete":
            user_id = form_value(values, "user_id")
            with db() as connection:
                connection.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
                connection.execute("DELETE FROM users WHERE id=?", (user_id,))
            return self.admin_page("Пользователь удален.")
        if parsed.path == "/admin/servers/add":
            address = form_value(values, "server_ip").removeprefix("https://").removeprefix("http://").strip("/")
            if not re.fullmatch(r"[A-Za-z0-9.:-]+", address):
                return self.admin_page("Укажите корректный IP или IP:порт.")
            try:
                count = add_server_for_users(address)
            except sqlite3.IntegrityError:
                return self.admin_page("Такой сервер уже добавлен.")
            return self.admin_page("Сервер добавлен для пользователей: %d." % count)
        if parsed.path == "/admin/servers/delete":
            server_id = form_value(values, "server_id")
            with db() as connection:
                connection.execute("DELETE FROM subscriptions WHERE server_id=?", (server_id,))
                connection.execute("DELETE FROM servers WHERE id=?", (server_id,))
            return self.admin_page("Сервер удален.")
        if parsed.path == "/admin/replace-ip":
            old, new = form_value(values, "old_ip"), form_value(values, "new_ip")
            if not old or not new:
                return self.admin_page("Укажите оба IP-адреса.")
            with db() as connection:
                servers = connection.execute("SELECT * FROM servers").fetchall()
                changed = 0
                for server in servers:
                    updated = replace_host_in_url(server["ip"], old, new)
                    if updated != server["ip"]:
                        connection.execute("UPDATE servers SET ip=? WHERE id=?", (updated, server["id"]))
                        changed += 1
            return self.admin_page("Изменено серверов: %d." % changed)
        if parsed.path == "/admin/import":
            try:
                payload = json.loads(form_value(values, "backup_json"))
                users, servers = payload["users"], payload["servers"]
                with db() as connection:
                    connection.execute("DELETE FROM subscriptions")
                    connection.execute("DELETE FROM servers")
                    connection.execute("DELETE FROM users")
                    for server in servers:
                        connection.execute("INSERT INTO servers(ip,active) VALUES(?,?)", (server["ip"], int(server.get("active", 1))))
                    for item in users:
                        cursor = connection.execute("INSERT INTO users(access_id,subscription_id,name,active) VALUES(?,?,?,?)", (item["access_id"], item["subscription_id"], item.get("name", "Пользователь"), int(item.get("active", 1))))
                        for index, server in enumerate(connection.execute("SELECT * FROM servers WHERE active=1 ORDER BY id"), 1):
                            connection.execute("INSERT INTO subscriptions(user_id,label,source_url,server_id) VALUES(?,?,?,?)", (cursor.lastrowid, "Сервер %d" % index, subscription_url(server["ip"], item["subscription_id"]), server["id"]))
                return self.admin_page("Импорт завершен: %d пользователей, %d серверов." % (len(users), len(servers)))
            except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
                return self.admin_page("Ошибка импорта: %s" % error)
        return self.send(HTTPStatus.NOT_FOUND, "Страница не найдена", "text/plain; charset=utf-8")

    def home(self, error=""):
        message = '<div class="notice danger">%s</div>' % esc(error) if error else ""
        body = '<section class="auth"><div class="service-name">Три буквы от Сани</div><div class="eyebrow">ЛИЧНЫЙ ДОСТУП</div><h1>Ваши VPN-подключения</h1><p>Введите персональный ID, чтобы открыть актуальные подписки и прямые VLESS-ссылки.</p>%s<form method="post" action="/login"><label>Персональный ID<input name="access_id" required autocomplete="off" placeholder="Например, K7m..." /></label><button>Открыть доступ <span>→</span></button></form><a class="admin-link" href="/admin">Вход администратора</a></section>' % message
        return self.send(200, page("Доступ", body))

    def user_page(self, access):
        user = user_by_access(access)
        if not user:
            return self.home("Пользователь не найден или доступ отключен.")
        groups, errors = collect_links(user["id"])
        subscription_url = self.public_origin() + "/sub/" + (user["subscription_id"] or access)
        error_html = ''.join('<div class="notice danger">%s: %s</div>' % (esc(item["label"]), esc(item["message"])) for item in errors)
        groups_html = ""
        for group in groups:
            links = ''.join('<div class="link-row"><div class="link-copy"><strong>Прямая ссылка на сервер</strong><small>Рекомендуется для использования на ПК</small><code>%s</code></div><button class="copy" data-copy="%s" title="Скопировать ссылку">Скопировать ссылку</button></div>' % (esc(link), esc(link)) for link in group["links"]) or '<div class="muted">VLESS-ссылки не найдены в источнике.</div>'
            groups_html += '<section class="source"><div class="source-head"><div class="source-info"><h2>%s</h2><span class="source-label">Ссылка-подписка</span><a class="source-url" href="%s">%s</a></div><span class="count">%d</span></div>%s</section>' % (esc(group["label"]), esc(group["source_url"]), esc(group["source_url"]), len(group["links"]), links)
        body = '<section class="welcome"><div><div class="service-name">Три буквы от Сани</div><div class="eyebrow">ЛИЧНЫЙ КАБИНЕТ</div><h1>Ваши VPN-подключения</h1><p>Все подключения собраны в одном месте. Обновляйте подписку в клиенте, чтобы получить изменения сервера.</p></div><a class="outline" href="/">Выйти</a></section>%s<section class="subscription-main"><div class="section-title"><div><div class="eyebrow">ССЫЛКА ДЛЯ КЛИЕНТА</div><h2>Единая ссылка обновления</h2></div><span class="live">● АКТИВНА</span></div><div class="subscription-box"><code>%s</code><button class="copy" data-copy="%s">Скопировать ссылку</button></div><small>Добавьте эту ссылку в v2rayTun или другой VLESS-клиент. При обновлении клиент получит актуальные конфигурации.</small></section><div class="section-title direct-title"><div><div class="eyebrow">ПРЯМЫЕ ПОДКЛЮЧЕНИЯ</div><h2>Прямые VLESS-ссылки</h2></div></div>%s' % (error_html, esc(subscription_url), esc(subscription_url), groups_html)
        return self.send(200, page("Подключения", body + SCRIPT))

    def subscription_response(self, access):
        user = user_by_access(access)
        if not user:
            return self.send(404, "Подписка не найдена", "text/plain; charset=utf-8")
        # Never send cached links to VPN clients: an unavailable server must be
        # omitted and will automatically return after its source is reachable.
        groups, _ = collect_links(user["id"], use_cache=False)
        candidates = [link for group in groups for link in group["links"]]
        links = []
        if candidates:
            with ThreadPoolExecutor(max_workers=min(16, len(candidates))) as executor:
                checks = {executor.submit(vless_endpoint_available, link): link for link in candidates}
                available = []
                for future in as_completed(checks):
                    if future.result():
                        available.append(checks[future])
            links = [link for link in candidates if link in available]
        user_agent = self.headers.get("User-Agent", "").lower()
        accepts_html = "text/html" in self.headers.get("Accept", "").lower()
        if "mozilla" in user_agent or accepts_html:
            rows = "".join('<div class="link-row"><code>%s</code><button class="copy" data-copy="%s">Скопировать ссылку</button></div>' % (esc(link), esc(link)) for link in links)
            body = '<section class="auth subscription-preview"><div class="eyebrow">ПРЕДПРОСМОТР ПОДПИСКИ</div><h1>Прямые VLESS-ссылки</h1><p>Это браузерный просмотр. VPN-клиенты получают эту же подписку в автоматическом формате.</p><div class="preview-links">%s</div><a class="outline" href="/">На главную</a></section>' % (rows or '<div class="muted">Активные ссылки пока не получены.</div>')
            return self.send(200, page("Предпросмотр подписки", body) + SCRIPT)
        # A client receives only URI lines; browsers can still request this endpoint explicitly.
        payload = "\n".join(links) + ("\n" if links else "")
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        return self.send(200, encoded, "text/plain; charset=ascii")

    def admin_page(self, message=""):
        return self.admin_page_new(message)

    def admin_page_new(self, message=""):
        if not self.session():
            body = '<section class="auth compact"><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h1>Вход администратора</h1><p>Введите пароль администратора.</p><form method="post" action="/admin/login"><label>Пароль<input type="password" name="password" required /></label><button>Войти <span>→</span></button></form><a class="admin-link" href="/">На главную</a></section>'
            return self.send(200, page("Администрирование", body))
        with db() as connection:
            users = connection.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
            servers = connection.execute("SELECT * FROM servers WHERE active=1 ORDER BY id").fetchall()
        notice = '<div class="notice success">%s</div>' % esc(message) if message else ""
        user_rows = ''.join('<tr><td><strong>%s</strong></td><td><code>%s</code></td><td><a href="/u/%s" target="_blank">Открыть кабинет</a></td><td><form method="post" action="/admin/users/delete" onsubmit="return confirm(\'Удалить пользователя и его доступ?\')"><input type="hidden" name="user_id" value="%s"><button class="danger-button">Удалить</button></form></td></tr>' % (esc(u["name"]), esc(u["subscription_id"] or u["access_id"]), quote(u["subscription_id"] or u["access_id"]), u["id"]) for u in users) or '<tr><td colspan="4" class="muted">Пользователей пока нет.</td></tr>'
        server_rows = ''.join('<tr><td><strong>Сервер %d</strong></td><td><code>%s</code></td><td class="url-cell">%s</td><td><form method="post" action="/admin/servers/delete" onsubmit="return confirm(\'Удалить сервер у всех пользователей?\')"><input type="hidden" name="server_id" value="%s"><button class="danger-button">Удалить</button></form></td></tr>' % (index, esc(server["ip"]), esc("https://" + server["ip"] + "/sub/"), server["id"]) for index, server in enumerate(servers, 1)) or '<tr><td colspan="4" class="muted">Серверов пока нет.</td></tr>'
        body = '<section class="admin-hero"><div><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h1>Управление доступом</h1><p>Пользователи, серверы и резервные копии в одном месте.</p></div><a class="outline" href="/admin/logout">Выйти</a></section>%s<section class="backup-actions"><div><div class="eyebrow">РЕЗЕРВНАЯ КОПИЯ</div><h2>Экспорт и импорт</h2></div><a class="outline" href="/admin/export">Скачать JSON</a><form method="post" action="/admin/import"><label>Содержимое JSON-файла<textarea name="backup_json" required placeholder="Вставьте содержимое резервной копии"></textarea></label><button>Импортировать</button></form></section><nav class="tabs"><button data-tab="users">Пользователи</button><button data-tab="servers">Серверы</button></nav><section data-section="users"><div class="panel"><div class="eyebrow">НОВЫЙ ПОЛЬЗОВАТЕЛЬ</div><h2>Добавить пользователя</h2><form method="post" action="/admin/users/add"><label>Имя пользователя<input name="name" required placeholder="Алексей" /></label><label>ID пользователя из 3x-ui<input name="subscription_id" required placeholder="abcdef1234567890" /></label><button>Создать пользователя <span>+</span></button></form></div><section class="panel table-panel"><div class="section-title"><h2>Пользователи</h2><span class="count">%d</span></div><table><tr><th>Имя</th><th>ID</th><th>Кабинет</th><th>Действие</th></tr>%s</table></section></section><section data-section="servers"><div class="panel"><div class="eyebrow">НОВЫЙ СЕРВЕР</div><h2>Добавить сервер</h2><p>Сервер автоматически добавится всем пользователям.</p><form method="post" action="/admin/servers/add"><label>IP или IP:порт<input name="server_ip" required placeholder="203.0.113.10:2096" /></label><button>Добавить сервер <span>+</span></button></form></div><div class="panel replace"><div><div class="eyebrow">МАССОВОЕ ОБНОВЛЕНИЕ</div><h2>Заменить IP сервера</h2><p>Замена применяется к серверу и его шаблонам.</p></div><form method="post" action="/admin/replace-ip" class="inline-form"><input name="old_ip" required placeholder="Старый IP:порт" /><span>→</span><input name="new_ip" required placeholder="Новый IP:порт" /><button>Заменить адрес</button></form></div><section class="panel table-panel"><div class="section-title"><h2>Текущие серверы</h2><span class="count">%d</span></div><p>Шаблон: <code>https://сервер:порт/sub/</code></p><table><tr><th>Сервер</th><th>IP:порт</th><th>Шаблон ссылки</th><th>Действие</th></tr>%s</table></section></section>' % (notice, len(users), user_rows, len(servers), server_rows)
        return self.send(200, page("Администрирование", body, '<span class="user-mark">АДМИН</span>') + ADMIN_SCRIPT)
        if not self.session():
            body = '<section class="auth compact"><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h1>Вход администратора</h1><p>Доступ защищен единым паролем, заданным в переменной окружения.</p><form method="post" action="/admin/login"><label>Пароль<input type="password" name="password" required /></label><button>Войти <span>→</span></button></form><a class="admin-link" href="/">На главную</a></section>'
            return self.send(200, page("Администрирование", body))
        with db() as connection:
            users = connection.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
            subscriptions = connection.execute("SELECT subscriptions.*, users.name AS user_name, users.access_id AS access_id, users.subscription_id AS user_subscription_id, servers.ip AS server_ip FROM subscriptions JOIN users ON users.id=subscriptions.user_id LEFT JOIN servers ON servers.id=subscriptions.server_id ORDER BY subscriptions.id DESC").fetchall()
            servers = connection.execute("SELECT * FROM servers WHERE active=1 ORDER BY id").fetchall()
        notice = '<div class="notice success">%s</div>' % esc(message) if message else ""
        user_options = ''.join('<option value="%s">%s (%s)</option>' % (user["id"], esc(user["name"]), esc(user["access_id"])) for user in users)
        user_rows = ''.join('<tr><td><strong>%s</strong></td><td><code>%s</code></td><td><a href="/u/%s" target="_blank">Открыть кабинет</a></td><td><form method="post" action="/admin/users/delete" onsubmit="return confirm(\'Удалить пользователя и его доступ?\')"><input type="hidden" name="user_id" value="%s"><button class="danger-button">Удалить</button></form></td></tr>' % (esc(u["name"]), esc(u["subscription_id"] or u["access_id"]), quote(u["subscription_id"] or u["access_id"]), u["id"]) for u in users) or '<tr><td colspan="4" class="muted">Пользователей пока нет.</td></tr>'
        server_rows = ''.join('<tr><td><strong>Сервер %d</strong></td><td><code>%s</code></td><td class="url-cell">%s</td><td><form method="post" action="/admin/servers/delete" onsubmit="return confirm(\'Удалить сервер у всех пользователей?\')"><input type="hidden" name="server_id" value="%s"><button class="danger-button">Удалить</button></form></td></tr>' % (index, esc(server["ip"]), esc("https://" + server["ip"] + "/sub/"), server["id"]) for index, server in enumerate(servers, 1)) or '<tr><td colspan="4" class="muted">Серверов пока нет.</td></tr>'
        body = '<section class="admin-hero"><div><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h1>Управление доступом</h1><p>Пользователи, серверы и резервные копии в одном месте.</p></div><a class="outline" href="/admin/logout">Выйти</a></section>%s<section class="backup-actions"><div><div class="eyebrow">РЕЗЕРВНАЯ КОПИЯ</div><h2>Экспорт и импорт</h2></div><a class="outline" href="/admin/export">Скачать JSON</a><form method="post" action="/admin/import"><label>Содержимое JSON-файла<textarea name="backup_json" required placeholder="Вставьте содержимое резервной копии"></textarea></label><button>Импортировать</button></form></section><nav class="tabs"><button data-tab="users">Пользователи</button><button data-tab="servers">Серверы</button></nav><section data-section="users"><div class="panel"><div class="eyebrow">НОВЫЙ ПОЛЬЗОВАТЕЛЬ</div><h2>Добавить пользователя</h2><form method="post" action="/admin/users/add"><label>Имя пользователя<input name="name" required placeholder="Алексей" /></label><label>ID пользователя из 3x-ui<input name="subscription_id" required placeholder="abcdef1234567890" /></label><button>Создать пользователя <span>+</span></button></form></div><section class="panel table-panel"><div class="section-title"><h2>Пользователи</h2><span class="count">%d</span></div><table><tr><th>Имя</th><th>ID</th><th>Кабинет</th><th>Действие</th></tr>%s</table></section></section><section data-section="servers"><div class="panel"><div class="eyebrow">НОВЫЙ СЕРВЕР</div><h2>Добавить сервер</h2><p>Сервер автоматически добавится всем пользователям. Имя подписки назначается по порядку.</p><form method="post" action="/admin/servers/add"><label>IP или IP:порт<input name="server_ip" required placeholder="203.0.113.10:2096" /></label><button>Добавить сервер <span>+</span></button></form></div><div class="panel replace"><div><div class="eyebrow">МАССОВОЕ ОБНОВЛЕНИЕ</div><h2>Заменить IP сервера</h2><p>Замена применяется к серверу и всем автоматически сформированным шаблонам.</p></div><form method="post" action="/admin/replace-ip" class="inline-form"><input name="old_ip" required placeholder="Старый IP:порт" /><span>→</span><input name="new_ip" required placeholder="Новый IP:порт" /><button>Заменить адрес</button></form></div><section class="panel table-panel"><div class="section-title"><h2>Текущие серверы</h2><span class="count">%d</span></div><p>Шаблон: <code>https://сервер:порт/sub/</code></p><table><tr><th>Сервер</th><th>IP:порт</th><th>Шаблон ссылки</th><th>Действие</th></tr>%s</table></section></section>' % (notice, len(users), user_rows, len(servers), server_rows)
        body = '<section class="admin-hero"><div><div class="eyebrow">АДМИНИСТРИРОВАНИЕ</div><h1>Управление доступом</h1><p>Управляйте стабильными ссылками пользователей и меняйте адреса VPN централизованно.</p></div><a class="outline" href="/admin/logout">Выйти</a></section>%s<nav class="tabs"><button data-tab="users">Пользователи</button><button data-tab="subscriptions">Подписки</button><button data-tab="ip">Замена IP</button></nav><div class="admin-grid" data-section="users"><section class="panel"><div class="eyebrow">НОВЫЙ ПОЛЬЗОВАТЕЛЬ</div><h2>Добавить пользователя</h2><form method="post" action="/admin/users/add"><label>Имя или метка<input name="name" required placeholder="Команда поддержки" /></label><button>Создать ID <span>+</span></button></form></section></div><section class="panel table-panel" data-section="users"><div class="section-title"><h2>Пользователи</h2><span class="count">%d</span></div><table><tr><th>Пользователь</th><th>Кабинет</th><th>Статус</th></tr>%s</table></section><div data-section="subscriptions"><section class="panel"><div class="eyebrow">НОВЫЙ ИСТОЧНИК</div><h2>Добавить подписку</h2><p>Одному пользователю можно добавить любое количество подписок, в том числе с разными IP.</p><form method="post" action="/admin/subscriptions/add"><label>Пользователь<select name="user_id" required>%s</select></label><label>Название<input name="label" required placeholder="Сервер 1 / IP 203.0.113..." /></label><label>URL 3x-ui<input name="source_url" type="url" required placeholder="https://.../sub/..." /></label><button>Добавить источник <span>+</span></button></form></section><section class="panel table-panel"><div class="section-title"><h2>Источники подписок</h2><span class="count">%d</span></div><table><tr><th>Пользователь</th><th>Название</th><th>URL</th></tr>%s</table></section></div><section class="panel replace" data-section="ip"><div><div class="eyebrow">МАССОВОЕ ОБНОВЛЕНИЕ</div><h2>Заменить IP сервера</h2><p>Меняется только точное совпадение старого IP в URL источников. Другие адреса останутся без изменений.</p></div><form method="post" action="/admin/replace-ip" class="inline-form"><input name="old_ip" required placeholder="Старый IP" /><span>→</span><input name="new_ip" required placeholder="Новый IP" /><button>Заменить адрес</button></form></section>' % (notice, len(users), user_rows, user_options, len(subscriptions), sub_rows)
        body = body.replace('<div class="eyebrow">НОВЫЙ ИСТОЧНИК</div><h2>Добавить подписку</h2><p>Одному пользователю можно добавить любое количество подписок, в том числе с разными IP.</p><form method="post" action="/admin/subscriptions/add"><label>Пользователь<select name="user_id" required>%s</select></label><label>Название<input name="label" required placeholder="Сервер 1 / IP 203.0.113..." /></label><label>URL 3x-ui<input name="source_url" type="url" required placeholder="https://.../sub/..." /></label><button>Добавить источник <span>+</span></button></form>' % user_options, '<div class="eyebrow">НОВЫЙ СЕРВЕР</div><h2>Добавить сервер</h2><p>Новый сервер автоматически добавится всем пользователям как следующая подписка.</p><form method="post" action="/admin/servers/add"><label>IP или IP:порт<input name="server_ip" required placeholder="203.0.113.10:2096" /></label><button>Добавить сервер <span>+</span></button></form>')
        body = body.replace("Источники подписок", "Текущие ссылки выбранного пользователя")
        body = body.replace('<button data-tab="subscriptions">Подписки</button><button data-tab="ip">Замена IP</button>', '<button data-tab="servers">Серверы</button>')
        body = body.replace('data-section="subscriptions"', 'data-section="servers"').replace('data-section="ip"', 'data-section="servers"')
        body = body.replace('<div class="section-title"><h2>Текущие ссылки выбранного пользователя</h2>', '<div class="section-title"><div><h2>Текущие ссылки выбранного пользователя</h2><label class="filter-label">Пользователь<select id="subscription-user-filter"><option value="all">Все пользователи</option>%s</select></label></div>' % user_options)
        body = body.replace('<table><tr><th>Пользователь</th><th>Название</th><th>URL</th></tr>', '<table><tr><th>Пользователь</th><th>Название</th><th>Текущий IP</th><th>Ссылка-подписка</th></tr>')
        body = body.replace('<nav class="tabs">', '<section class="backup-actions"><div><div class="eyebrow">РЕЗЕРВНАЯ КОПИЯ</div><h2>Экспорт и импорт</h2></div><a class="outline" href="/admin/export">Скачать JSON</a><form method="post" action="/admin/import"><label>Содержимое JSON-файла<textarea name="backup_json" required placeholder="Вставьте сюда содержимое файла резервной копии"></textarea></label><button>Импортировать</button></form></section><nav class="tabs">')
        body = body.replace('<section class="panel table-panel"><div class="section-title"><h2>Текущие ссылки выбранного пользователя</h2>', '<section class="panel table-panel"><div class="section-title"><h2>Текущие ссылки выбранного пользователя</h2>')
        return self.send(200, page("Администрирование", body, '<span class="user-mark">АДМИН</span>') + ADMIN_SCRIPT)


CSS = r'''
@import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Manrope:wght@400;600;700;800&display=swap');
.service-name{font-size:clamp(18px,2.5vw,28px);font-weight:800;line-height:1.2;color:var(--ink);margin-bottom:18px}.auth>.service-name{font-size:clamp(24px,4vw,38px);margin-bottom:24px}.brand{white-space:nowrap}.brand span{letter-spacing:.08em}
.backup-actions{display:grid;grid-template-columns:auto auto minmax(280px,1fr);align-items:end;gap:18px;background:white;border:1px solid var(--line);padding:22px;margin-bottom:22px}.backup-actions form{display:flex;align-items:end;gap:10px}.backup-actions label{flex:1;margin:0}.backup-actions textarea{width:100%;min-height:70px;resize:vertical;border:1px solid var(--line);padding:11px;font:12px 'DM Mono';color:var(--ink)}
.danger-button{background:#b64b42;padding:9px 12px;font-size:11px}.danger-button:hover{background:#943b34}.table-panel form{margin:0}.table-panel td:last-child{white-space:nowrap}
.link-copy{display:grid;gap:6px;min-width:0;flex:1}.link-copy strong{display:block;line-height:1.35}.link-copy small{display:block;color:var(--muted);font-size:12px;line-height:1.45}.link-copy code,.source-url{display:block;max-width:100%;overflow-wrap:anywhere;word-break:break-word;white-space:normal;line-height:1.55}.source-info{min-width:0;max-width:calc(100% - 60px)}.source-label{display:block;color:var(--muted);font-size:12px;margin:8px 0 3px}.source-url{font:12px 'DM Mono';color:var(--green);text-decoration:none}.direct-title{margin-top:48px}.subscription-box{padding:24px 22px;gap:22px}.subscription-box code{font-size:16px;line-height:1.55;overflow-wrap:anywhere;word-break:break-word;white-space:normal;min-width:0}.copy{background:var(--green);border:1px solid var(--green);font-size:12px;min-height:44px;padding:12px 18px;justify-content:center;white-space:nowrap}.copy:hover{background:#1d4e32}.link-row{align-items:flex-start;gap:22px}.link-row .copy{flex:0 0 auto}.source-head{gap:18px}.panel,.subscription-main,.source,.table-panel{min-width:0}.url-cell{overflow-wrap:anywhere;word-break:break-word;white-space:normal}
:root{--ink:#17221d;--muted:#708078;--line:#dce5df;--paper:#f7faf8;--mint:#d9f3df;--green:#286341;--lime:#b8efc4;--red:#b64b42}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Manrope,Arial,sans-serif}.shell{max-width:1120px;margin:auto;padding:28px 28px 70px}.top{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:22px}.brand{font-weight:800;letter-spacing:.04em;color:var(--ink);text-decoration:none}.brand span{color:var(--green);font-weight:500;margin-left:5px}.top-actions,.user-mark{font:500 11px 'DM Mono',monospace;letter-spacing:.08em;color:var(--muted)}h1{font-size:clamp(34px,5vw,64px);line-height:1.05;letter-spacing:0;margin:14px 0 18px;max-width:760px}h2{font-size:19px;margin:7px 0 4px}p{color:var(--muted);line-height:1.65}.eyebrow{font:500 11px 'DM Mono',monospace;letter-spacing:.14em;color:var(--green)}.auth{max-width:600px;margin:13vh auto 0}.auth p{max-width:490px}.auth form{margin-top:42px}.compact{max-width:460px}.compact form{margin-top:28px}label{display:grid;gap:9px;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:18px}input,select{border:1px solid var(--line);background:white;border-radius:4px;padding:14px;font:14px Manrope;color:var(--ink);width:100%;outline:none}input:focus,select:focus{border-color:var(--green)}button,.outline{border:0;background:var(--green);color:#fff;padding:13px 17px;border-radius:4px;font:700 12px Manrope;cursor:pointer;text-decoration:none;display:inline-flex;gap:18px;align-items:center}button span{font-size:17px;line-height:10px}.admin-link{display:block;margin-top:30px;color:var(--muted);font-size:12px}.notice{padding:13px 16px;margin:22px 0;border-radius:4px;font-size:13px}.danger{background:#fae8e5;color:var(--red)}.success{background:var(--mint);color:var(--green)}.welcome,.admin-hero{display:flex;justify-content:space-between;align-items:end;padding:72px 0 55px}.welcome p,.admin-hero p{max-width:650px}.outline{background:transparent;color:var(--green);border:1px solid #9ec5a9}.subscription-main{border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:30px 0 35px;margin-bottom:48px}.section-title,.source-head{display:flex;justify-content:space-between;align-items:center}.live{font:11px 'DM Mono';color:var(--green)}.subscription-box{display:flex;align-items:center;justify-content:space-between;background:var(--mint);padding:18px 20px;margin:24px 0 10px;gap:15px}.subscription-box code{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.copy{background:var(--green);white-space:nowrap;font-size:11px;padding:10px 12px}.source{border-top:1px solid var(--line);padding:24px 0}.source-head p{font:11px 'DM Mono';margin:4px 0;overflow:hidden;text-overflow:ellipsis}.source-head a{color:var(--muted)}.count{background:var(--mint);border-radius:20px;color:var(--green);padding:5px 10px;font:11px 'DM Mono'}.link-row{display:flex;align-items:center;justify-content:space-between;gap:15px;border-top:1px solid var(--line);padding:14px 0;margin-top:12px}.link-row code{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.muted{color:var(--muted);font-size:13px}.admin-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.panel{background:white;border:1px solid var(--line);padding:25px;margin-bottom:18px}.panel form{margin-top:22px}.replace{display:flex;align-items:center;justify-content:space-between;gap:30px}.replace p{font-size:13px;max-width:550px}.inline-form{display:flex;align-items:center;gap:9px;min-width:430px}.inline-form input{max-width:160px}.inline-form span{color:var(--green)}.table-panel{overflow:auto}.table-panel .section-title{margin-bottom:17px}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;border-top:1px solid var(--line);padding:13px 9px;vertical-align:top}th{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.08em}td code{font:11px 'DM Mono';color:var(--green)}td a{color:var(--green)}.url-cell{font:11px 'DM Mono';word-break:break-all;max-width:460px}@media(max-width:700px){.shell{padding:20px 17px 50px}.welcome,.admin-hero{display:block;padding:50px 0 35px}.welcome .outline,.admin-hero .outline{margin-top:15px}.admin-grid{grid-template-columns:1fr}.replace{display:block}.inline-form{min-width:0;display:grid;grid-template-columns:1fr auto 1fr}.inline-form button{grid-column:1/-1;justify-content:center}.subscription-box{display:block}.subscription-box button{margin-top:14px}.link-row{display:block}.link-row .copy{margin-top:10px}.source-head{align-items:start}}
@media(max-width:900px){.shell{padding-inline:clamp(16px,4vw,28px)}.admin-grid{grid-template-columns:minmax(0,1fr)}.replace{align-items:stretch;flex-direction:column}.inline-form{min-width:0;width:100%}.section-title{gap:16px}.welcome,.admin-hero{padding:52px 0 40px}}
@media(max-width:900px){.backup-actions{grid-template-columns:1fr}.backup-actions form{display:grid}.backup-actions .outline,.backup-actions button{justify-content:center}}
@media(max-width:700px){body{overflow-x:hidden}.shell{width:100%;padding:16px 14px 42px}.top{padding-bottom:15px}.brand{font-size:14px}.welcome,.admin-hero{display:block;padding:36px 0 28px}.welcome h1,.admin-hero h1{font-size:clamp(32px,10vw,42px);line-height:1.08}.welcome .outline,.admin-hero .outline{margin-top:12px}.section-title{align-items:flex-start}.live{flex:0 0 auto;margin-top:4px}.subscription-main{padding:24px 0 28px;margin-bottom:34px}.subscription-box{display:grid;padding:17px 14px;margin-top:18px}.subscription-box code{font-size:13px}.subscription-box .copy,.link-row .copy{width:100%;margin:0;justify-content:center}.direct-title{margin-top:36px}.source{padding:22px 0}.source-head{display:flex;align-items:flex-start}.source-info{max-width:calc(100% - 48px)}.source-url{font-size:11px}.link-row{display:grid;padding:17px 0;gap:14px}.link-copy code{font-size:11px}.tabs{overflow-x:auto;scrollbar-width:none}.tabs button{flex:0 0 auto;padding-inline:13px}.panel{padding:18px 15px}.table-panel{padding:15px 10px;overflow-x:auto}.table-panel table{min-width:650px}.filter-label{max-width:none!important}.inline-form{display:grid;grid-template-columns:minmax(0,1fr);gap:10px}.inline-form input{max-width:none}.inline-form span{display:none}.inline-form button{justify-content:center}.auth{margin-top:9vh}.auth form{margin-top:28px}}
@media(max-width:380px){.shell{padding-inline:11px}.welcome h1,.admin-hero h1{font-size:30px}.eyebrow{font-size:10px;letter-spacing:.1em}.subscription-box{padding:14px 11px}.panel{padding:15px 12px}.copy{padding-inline:12px}}
.subscription-box{display:flex;align-items:center;padding:24px 22px;gap:22px}.subscription-box code{min-width:0;font-size:16px;line-height:1.55;white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;word-break:break-word}.copy{min-height:44px;padding:12px 18px;font-size:12px;font-weight:800;border:1px solid var(--green);box-shadow:0 2px 0 rgba(23,34,29,.15)}.link-row{display:flex;align-items:flex-start;gap:22px}.link-copy{display:grid;gap:6px;min-width:0;flex:1}.link-copy strong,.link-copy small,.link-copy code{display:block}.link-copy small{color:var(--muted);font-size:12px;line-height:1.45}.link-copy code,.source-url{white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;word-break:break-word;line-height:1.55}.source-info{min-width:0;max-width:calc(100% - 60px)}.source-label{display:block;margin:8px 0 3px;color:var(--muted);font-size:12px}.source-url{display:block;font:12px 'DM Mono';color:var(--green)}
.subscription-preview{max-width:900px;margin:6vh auto 0}.subscription-preview h1{max-width:none}.preview-links{margin:28px 0}.preview-links .link-row{border:1px solid var(--line);background:white;border-radius:6px;padding:18px;margin:10px 0}.preview-links .link-row code{flex:1;min-width:0;overflow-wrap:anywhere;word-break:break-word;white-space:normal}
@media(max-width:700px){.subscription-box{display:grid;padding:17px 14px}.subscription-box code{font-size:13px}.subscription-box .copy,.link-row .copy{width:100%;margin:0;justify-content:center}.link-row{display:grid;gap:14px}.link-copy code{font-size:11px}.source-head{display:flex}.source-info{max-width:calc(100% - 48px)}}
'''
SCRIPT = '''<script>document.querySelectorAll('[data-copy]').forEach(function(button){button.addEventListener('click',function(){navigator.clipboard.writeText(button.dataset.copy).then(function(){var old=button.textContent;button.textContent='Скопировано';setTimeout(function(){button.textContent=old},1400)})})})</script>'''
ADMIN_SCRIPT = '''<style>[data-section]{display:none}.tabs{display:flex;gap:8px;border-bottom:1px solid var(--line);margin-bottom:22px}.tabs button{background:transparent;color:var(--muted);border-radius:0;border-bottom:2px solid transparent}.tabs button.active{color:var(--green);border-color:var(--green)}.tabs button[data-tab="users"].active ~ *{display:block}</style><script>(function(){var tabs=document.querySelectorAll('[data-tab]'),sections=document.querySelectorAll('[data-section]');function show(name){tabs.forEach(function(tab){tab.classList.toggle('active',tab.dataset.tab===name)});sections.forEach(function(section){section.style.display=section.dataset.section===name?'block':'none'})}tabs.forEach(function(tab){tab.addEventListener('click',function(){show(tab.dataset.tab)})});show('users')})()</script>'''
FILTER_SCRIPT = '''<style>.filter-label{max-width:280px;margin:10px 0 0}</style><script>(function(){var filter=document.getElementById('subscription-user-filter');if(!filter)return;filter.addEventListener('change',function(){document.querySelectorAll('[data-user-id]').forEach(function(row){row.style.display=filter.value==='all'||row.dataset.userId===filter.value?'table-row':'none'})})})()</script>'''


if __name__ == "__main__":
    if not ADMIN_PASSWORD:
        raise SystemExit("VPN_PORTAL_ADMIN_PASSWORD не задан. Установите переменную окружения и перезапустите сервис.")
    init_db()
    print("Три буквы от Сани: http://%s:%s" % (HOST, PORT))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
