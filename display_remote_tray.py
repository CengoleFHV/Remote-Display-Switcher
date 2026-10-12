from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
import logging
from logging.handlers import RotatingFileHandler
import socket
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw
from zeroconf import IPVersion, ServiceInfo, Zeroconf

from display_control import (
    APP_VERSION,
    DisplayControlBusyError,
    DisplayControlError,
    DisplayController,
)


PORT = 5050
HOSTNAME = "switch.local"
TOKEN = "1234"  # Nur Komfort-Hürde; Sicherheitshinweis im README beachten.
ALLOWED_COMMANDS = {"tv", "desk", "extend", "clone"}

DISPLAY_CONTROLLER = DisplayController()


def get_local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


LOCAL_IP = get_local_ip()


HTML = f"""
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PC Display Switch</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #111;
            color: white;
            text-align: center;
            padding: 30px;
        }}
        h1 {{ margin-bottom: 30px; }}
        button {{
            font-size: 22px;
            padding: 18px 35px;
            margin: 12px;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            min-width: 290px;
        }}
        button:hover {{ opacity: 0.85; }}
        button:disabled {{ opacity: 0.5; cursor: wait; }}
        .status {{
            margin-top: 25px;
            color: #aaa;
            font-size: 16px;
        }}
        .hint {{
            max-width: 620px;
            margin: 25px auto 0;
            color: #888;
            font-size: 14px;
            line-height: 1.5;
        }}
    </style>
</head>
<body>
    <h1>PC Display Switch</h1>

    <button onclick="run('tv')">TV aktivieren</button><br>
    <button onclick="run('desk')">PC-Monitore aktivieren</button><br>
    <button onclick="run('extend')">Alle Anzeigen erweitern</button><br>
    <button onclick="run('clone')">Alle Anzeigen duplizieren</button>

    <div class="status" id="status">Bereit</div>
    <div class="hint">
        Vor dem ersten TV-Wechsel die korrekte Anordnung beider PC-Monitore
        einmal über das Tray-Menü als PC-Profil speichern.
    </div>
    <div class="hint">Version {APP_VERSION}</div>

    <script>
        let running = false;

        async function run(cmd) {{
            if (running) return;
            running = true;
            const buttons = document.querySelectorAll('button');
            buttons.forEach(button => button.disabled = true);
            const status = document.getElementById('status');
            status.innerText = 'Anzeigen werden umgeschaltet und geprüft...';

            try {{
                const response = await fetch(
                    '/run?token={TOKEN}&cmd=' + encodeURIComponent(cmd),
                    {{ cache: 'no-store' }}
                );
                const text = await response.text();
                if (!response.ok) {{
                    throw new Error(text || ('HTTP ' + response.status));
                }}
                status.innerText = text;
            }} catch (error) {{
                status.innerText = error.message || String(error);
            }} finally {{
                running = false;
                buttons.forEach(button => button.disabled = false);
            }}
        }}
    </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_text(self, status_code, text):
        content = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            content = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path != "/run":
            self.send_text(404, "Seite nicht gefunden")
            return

        query = parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        command_name = query.get("cmd", [""])[0]

        if token != TOKEN:
            self.send_text(403, "Kein Zugriff")
            return
        if command_name not in ALLOWED_COMMANDS:
            self.send_text(400, "Unbekannter Befehl")
            return

        try:
            result = DISPLAY_CONTROLLER.switch(command_name)
            self.send_text(200, result)
        except DisplayControlBusyError as error:
            self.send_text(409, str(error))
        except DisplayControlError as error:
            self.send_text(500, f"Fehler: {error}")


def start_mdns():
    if LOCAL_IP == "127.0.0.1":
        print("Keine Netzwerk-IP gefunden. mDNS wurde nicht gestartet.")
        return None, None

    service_info = ServiceInfo(
        type_="_http._tcp.local.",
        name="PC Display Switch._http._tcp.local.",
        addresses=[socket.inet_aton(LOCAL_IP)],
        port=PORT,
        properties={
            "path": "/",
            "description": "PC Display Switch",
        },
        server=f"{HOSTNAME}.",
    )
    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    zeroconf.register_service(service_info)
    print(f"mDNS-Hostname registriert: http://{HOSTNAME}:{PORT}")
    return zeroconf, service_info


def stop_mdns(zeroconf, service_info):
    if zeroconf is None:
        return

    try:
        if service_info is not None:
            zeroconf.unregister_service(service_info)
    finally:
        zeroconf.close()


def create_icon_image():
    image = Image.new("RGB", (64, 64), "#111111")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 14, 54, 42), outline="white", width=4)
    draw.rectangle((26, 44, 38, 50), fill="white")
    draw.rectangle((20, 52, 44, 56), fill="white")
    return image


def open_local_page(icon=None, item=None):
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def open_hostname_page(icon=None, item=None):
    webbrowser.open(f"http://{HOSTNAME}:{PORT}")


def open_network_page(icon=None, item=None):
    webbrowser.open(f"http://{LOCAL_IP}:{PORT}")


def notify(icon, title, message):
    print(f"{title}: {message}")
    if icon is not None:
        try:
            icon.notify(message, title)
        except (NotImplementedError, OSError):
            pass

    if "Fehler" in title:
        show_error_dialog(title, message)


def show_error_dialog(title, message):
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except (AttributeError, OSError):
        pass


def switch_from_tray(command_name, icon):
    try:
        result = DISPLAY_CONTROLLER.switch(command_name)
        notify(icon, "PC Display Switch", result)
    except DisplayControlError as error:
        notify(icon, "PC Display Switch – Fehler", str(error))


def start_tray_task(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()


def switch_tv(icon=None, item=None):
    start_tray_task(switch_from_tray, "tv", icon)


def switch_desk(icon=None, item=None):
    start_tray_task(switch_from_tray, "desk", icon)


def switch_extend(icon=None, item=None):
    start_tray_task(switch_from_tray, "extend", icon)


def switch_clone(icon=None, item=None):
    start_tray_task(switch_from_tray, "clone", icon)


def save_profile_from_tray(profile_name, label, icon):
    try:
        profile_path = DISPLAY_CONTROLLER.save_profile(profile_name)
        notify(
            icon,
            "PC Display Switch",
            f"Aktuelle Anordnung als {label}-Profil gespeichert: {profile_path}",
        )
    except DisplayControlError as error:
        notify(icon, "PC Display Switch – Fehler", str(error))


def save_desk_profile(icon=None, item=None):
    start_tray_task(save_profile_from_tray, "desk", "PC", icon)


def save_tv_profile(icon=None, item=None):
    start_tray_task(save_profile_from_tray, "tv", "TV", icon)


def restore_desk_profile_from_tray(icon):
    try:
        profile_path = DISPLAY_CONTROLLER.restore_profile_backup("desk")
        notify(
            icon,
            "PC Display Switch",
            "Vorheriges PC-Profil wiederhergestellt. Wähle jetzt "
            f"'PC-Monitore aktivieren'. Profil: {profile_path}",
        )
    except DisplayControlError as error:
        notify(icon, "PC Display Switch – Fehler", str(error))


def restore_desk_profile(icon=None, item=None):
    start_tray_task(restore_desk_profile_from_tray, icon)


def quit_app(icon, item):
    icon.stop()


def main():
    try:
        DISPLAY_CONTROLLER.profile_directory.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                RotatingFileHandler(
                    DISPLAY_CONTROLLER.profile_directory / "display-switch.log",
                    maxBytes=2_000_000,
                    backupCount=2,
                    encoding="utf-8",
                )
            ],
        )
        logging.info("PC Display Switch %s started", APP_VERSION)
    except OSError:
        pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as error:
        show_error_dialog(
            "PC Display Switch – Startfehler",
            f"Webserver auf Port {PORT} konnte nicht gestartet werden: {error}",
        )
        return

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    zeroconf = None
    service_info = None
    try:
        zeroconf, service_info = start_mdns()
    except Exception as error:
        print(f"mDNS konnte nicht gestartet werden: {error}")

    icon = pystray.Icon(
        "PC DisplaySwitch",
        create_icon_image(),
        "PC Display Switch",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Webseite lokal öffnen",
                open_local_page,
                default=True,
            ),
            pystray.MenuItem(
                f"Über Hostname öffnen ({HOSTNAME})",
                open_hostname_page,
            ),
            pystray.MenuItem(f"Über IP öffnen ({LOCAL_IP})", open_network_page),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("TV aktivieren", switch_tv),
            pystray.MenuItem("PC-Monitore aktivieren", switch_desk),
            pystray.MenuItem("Alle Anzeigen erweitern", switch_extend),
            pystray.MenuItem("Alle Anzeigen duplizieren", switch_clone),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Aktuelles Layout als PC-Profil speichern",
                save_desk_profile,
            ),
            pystray.MenuItem(
                "Aktuelles Layout als TV-Profil speichern",
                save_tv_profile,
            ),
            pystray.MenuItem(
                "Vorheriges PC-Profil wiederherstellen",
                restore_desk_profile,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Beenden", quit_app),
        ),
    )

    print()
    print(f"Lokal:    http://127.0.0.1:{PORT}")
    print(f"Netzwerk: http://{LOCAL_IP}:{PORT}")
    print(f"Hostname: http://{HOSTNAME}:{PORT}")
    print()

    try:
        icon.run()
    finally:
        print("Server wird beendet...")
        stop_mdns(zeroconf, service_info)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
