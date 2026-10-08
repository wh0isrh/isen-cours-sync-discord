"""Synchronisation Junia Learning -> Ubuntu, Python 3.10+ / Windows.

Authentification dans Chromium visible ; exploration et téléchargements HTTP
avec ses cookies ; fichiers temporaires supprimés seulement après succès SFTP.
"""
from __future__ import annotations

import argparse
import base64
import errno
import getpass
import hashlib
import http.cookiejar
import json
import logging
import os
import posixpath
import re
import shutil
import socket
import stat
import sys
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

import paramiko
from bs4 import BeautifulSoup
from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

APP_DIR = Path(__file__).resolve().parent
MOODLE = "https://junia-learning.com"
LOG = logging.getLogger("moodle_sync")
BLOCK = 1024 * 1024
COURSES = [
    (19335, "Introduction à la conception des systèmes robotisés"),
    (19334, "Modélisation de systèmes déterministes et aléatoires"),
    (19333, "Automatique"),
    (19337, "Mécanique Quantique"),
    (19336, "Electronique Numérique"),
    (19341, "Language interprété"),
    (19340, "Introduction à l'Intelligence artificielle"),
    (19338, "Base de Données"),
    (19342, "Anglais"),
    (19345, "Enjeu des transitions"),
    (19344, "Décryptage de l'information"),
]


class AuthenticationExpired(RuntimeError):
    pass


def fix_mojibake(value: str) -> str:
    if any(c in value for c in ("\u00c3", "\u00c2", "\u00e2")):
        try:
            return value.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return value


def clean_name(value: str, filename: bool = False) -> str:
    value = fix_mojibake(value)
    value = unicodedata.normalize("NFC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ") or "Général"
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        value = "_" + value
    if len(value) > 120:
        suffix = PurePosixPath(value).suffix if filename else ""
        value = value[:120 - len(suffix)].rstrip() + suffix
    return value


def canonical_url(url: str) -> str:
    """Les paramètres Moodle de présentation ne changent pas l'identité du fichier."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "" if "/pluginfile.php/" in p.path else p.query, ""))


def safe_relative(path: str) -> str:
    p = PurePosixPath(path)
    if p.is_absolute() or not p.parts or any(x in {"", ".", ".."} for x in p.parts) or "\\" in path:
        raise ValueError("Chemin de ressource non valide")
    return str(p)


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return sha_stream(stream)


def sha_stream(stream) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(BLOCK):
        digest.update(chunk)
    return digest.hexdigest()


def positive_int(values, key, default, minimum=1, maximum=86400):
    value = int(values.get(key, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} doit être compris entre {minimum} et {maximum}")
    return value


def boolean(values, key, default):
    value = str(values.get(key, str(default))).strip().lower()
    if value not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{key} : utiliser true ou false")
    return value in {"true", "1", "yes"}


@dataclass
class Config:
    email: str
    password: str
    wait_2fa: int
    login_timeout: int
    host: str
    port: int
    user: str
    ssh_password: str
    key_path: str
    key_passphrase: str
    remote_dir: str
    ssh_timeout: int
    http_timeout: int
    http_retries: int
    verify_hash: bool
    strict_source: bool
    bootstrap: bool
    audit_dir: Path
    state_dir: Path = APP_DIR / ".state"
    local_only: bool = False
    wol_mac: str = ""
    wol_broadcast: str = "255.255.255.255"
    wol_wait: int = 120
    fast_remote_check: bool = True
    save_notes: bool = False
    local_storage: bool = False
    headless: bool = False

    @classmethod
    def load(cls, env_path: Path):
        if not env_path.is_file():
            shutil.copyfile(APP_DIR / ".env.example", env_path)
            raise ValueError(f"Configuration créée : {env_path}. Renseigner Ubuntu puis relancer.")
        values = {**dotenv_values(env_path), **os.environ}
        values = {k: (v or "") for k, v in values.items()}
        root = values.get("UBUNTU_REMOTE_DIR", "").strip() or values.get("COURS_DIR", "").strip()
        local_storage_default = (sys.platform != "win32" or not values.get("UBUNTU_HOST") or values.get("UBUNTU_HOST") in {"127.0.0.1", "localhost", "local"})
        local_storage = boolean(values, "LOCAL_STORAGE", local_storage_default)
        if not local_storage:
            if not root.startswith("/") or root == "/" or ".." in PurePosixPath(root).parts:
                raise ValueError("UBUNTU_REMOTE_DIR doit être un dossier absolu Ubuntu, différent de /.")
            host, user = values.get("UBUNTU_HOST", "").strip(), values.get("UBUNTU_USER", "").strip()
            if not host or not user:
                raise ValueError("Renseigner UBUNTU_HOST et UBUNTU_USER dans .env")
        else:
            host, user = "localhost", "local"
            if not root:
                root = "/srv/cours_isen/ISEN_Lille_2026-2027"

        key = values.get("UBUNTU_SSH_KEY_PATH", "").strip()
        if key and not local_storage:
            key = str(Path(os.path.expandvars(key)).expanduser())
            if not Path(key).is_absolute():
                key = str(APP_DIR / key)
            if not Path(key).is_file():
                raise ValueError("UBUNTU_SSH_KEY_PATH : fichier de clé privée introuvable")

        audit = Path(os.path.expandvars(values.get("AUDIT_DIR", "") or str(APP_DIR.parent / "ISEN_Lille_2026-2027"))).expanduser()
        if not audit.is_absolute():
            audit = APP_DIR / audit

        headless_default = (not bool(os.environ.get("DISPLAY"))) if sys.platform != "win32" else False
        headless = boolean(values, "HEADLESS", headless_default)
        return cls(
            values.get("JUNIA_EMAIL", ""), values.get("JUNIA_PASSWORD", ""),
            positive_int(values, "WAIT_2FA_SECONDS", 15, minimum=0, maximum=600),
            positive_int(values, "LOGIN_TIMEOUT_SECONDS", 240, maximum=1800),
            host, positive_int(values, "UBUNTU_PORT", 22, maximum=65535), user,
            values.get("UBUNTU_PASSWORD", ""), key, values.get("UBUNTU_SSH_KEY_PASSPHRASE", ""),
            posixpath.normpath(root), positive_int(values, "SSH_TIMEOUT_SECONDS", 30),
            positive_int(values, "HTTP_TIMEOUT_SECONDS", 120),
            positive_int(values, "HTTP_RETRIES", 3, maximum=10),
            boolean(values, "VERIFY_REMOTE_HASH", True), boolean(values, "STRICT_SOURCE_VALIDATION", False),
            boolean(values, "BOOTSTRAP_AUDIT", True), audit,
            local_only=boolean(values, "LOCAL_ONLY", False),
            fast_remote_check=boolean(values, "FAST_REMOTE_CHECK", True),
            save_notes=boolean(values, "SAVE_NOTES", False),
            local_storage=local_storage,
            headless=headless,
            wol_mac=values.get("WOL_MAC", "").strip(),
            wol_broadcast=values.get("WOL_BROADCAST", "255.255.255.255").strip(),
            wol_wait=positive_int(values, "WOL_WAIT_SECONDS", 120, maximum=600),
        )


class SecretFilter(logging.Filter):
    def __init__(self, cfg):
        super().__init__()
        self.secrets = [x for x in (cfg.password, cfg.ssh_password, cfg.key_passphrase) if x]

    def filter(self, record):
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "[masqué]")
        record.msg, record.args = message, ()
        return True


def configure_logging(cfg):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    LOG.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(cfg.state_dir / "sync.log", encoding="utf-8")):
        handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
        handler.addFilter(SecretFilter(cfg))
        LOG.addHandler(handler)


class RunLock:
    """Un verrou système se libère même après un arrêt brutal du processus."""
    def __init__(self, path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError("Une synchronisation est déjà en cours.") from exc
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, known_hosts):
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        fingerprint = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        print(f"\nPremière connexion SSH à {hostname}\nClé {key.get_name()} : SHA256:{fingerprint}")
        answer = input("Après vérification de cette empreinte sur Ubuntu, taper oui pour la mémoriser : ").strip().lower()
        if answer != "oui":
            raise paramiko.SSHException("Clé SSH non approuvée")
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(self.known_hosts))


def wake_server(cfg):
    if not cfg.wol_mac:
        return
    def reachable():
        try:
            with socket.create_connection((cfg.host, cfg.port), timeout=2):
                return True
        except OSError:
            return False
    if reachable():
        return
    raw = re.sub(r"[:-]", "", cfg.wol_mac)
    if not re.fullmatch(r"[a-fA-F0-9]{12}", raw):
        raise ValueError("WOL_MAC : adresse MAC invalide")
    packet = b"\xff" * 6 + bytes.fromhex(raw) * 16
    LOG.info("Réveil Wake-on-LAN de %s ; attente SSH jusqu'à %s secondes.", cfg.host, cfg.wol_wait)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for port in (7, 9):
            udp.sendto(packet, (cfg.wol_broadcast, port))
    deadline = time.monotonic() + cfg.wol_wait
    while time.monotonic() < deadline:
        if reachable():
            LOG.info("Serveur SSH disponible.")
            return
        time.sleep(2)
    raise TimeoutError("Le serveur ne répond pas après le réveil Wake-on-LAN")


def ssh_connect(cfg):
    wake_server(cfg)
    client = paramiko.SSHClient()
    known = cfg.state_dir / "known_hosts"
    user_known = Path.home() / ".ssh" / "known_hosts"
    if user_known.is_file():
        client.load_system_host_keys(str(user_known))
    if known.is_file():
        client.load_host_keys(str(known))
    client.set_missing_host_key_policy(ConfirmHostKey(known))
    password = cfg.ssh_password
    if not password and not cfg.key_path:
        password = getpass.getpass(f"Mot de passe SSH de {cfg.user}@{cfg.host} : ")
    try:
        client.connect(
            cfg.host, port=cfg.port, username=cfg.user, password=password or None,
            key_filename=cfg.key_path or None, passphrase=cfg.key_passphrase or None,
            allow_agent=False, look_for_keys=False, timeout=cfg.ssh_timeout,
            banner_timeout=cfg.ssh_timeout, auth_timeout=cfg.ssh_timeout,
        )
        client.get_transport().set_keepalive(20)
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(cfg.http_timeout)
        return client, sftp
    except BaseException:
        client.close()
        raise


def is_moodle_logged_in(page):
    return urlsplit(page.url).hostname == "junia-learning.com" and page.locator("a[href*='/login/logout.php']").count() > 0


def fill_visible(page, selector, value):
    target = page.locator(selector).first
    if target.count() and target.is_visible():
        target.fill(value)
        return True
    return False


DIGIT_ART = {
    "0": [" ### ", "#   #", "#   #", "#   #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "   # ", "  #  ", "#####"],
    "3": ["#####", "   # ", " ### ", "   # ", "#####"],
    "4": ["#   #", "#   #", "#####", "    #", "    #"],
    "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "],
    "7": ["#####", "   # ", "  #  ", " #   ", " #   "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "],
    "9": [" ### ", "#   #", " ####", "    #", " ### "],
}


def format_mfa_banner(code: str) -> str:
    lines = ["", "=" * 66, ""]
    lines.append("   [!] CODE MICROSOFT AUTHENTICATOR (A2F) :")
    lines.append("")
    if all(c in DIGIT_ART for c in code):
        for row in range(5):
            art_row = "    ".join(DIGIT_ART[c][row] for c in code)
            lines.append(f"            {art_row}")
    lines.append("")
    spaced = " ".join(code)
    lines.append(f"                 >>>  [  {spaced}  ]  <<<")
    lines.append("")
    lines.append("   >> Entrez ce numero dans Microsoft Authenticator sur votre smartphone <<")
    lines.append("=" * 66)
    lines.append("")
    return "\n".join(lines)


def extract_mfa_code(page) -> str | None:
    """Extraire le code à 2 chiffres affiché sur la page Microsoft Authenticator."""
    try:
        code = page.evaluate("""() => {
            const ids = [
                'idRichChallange_DisplaySign',
                'richChallangeText',
                'displaySign',
                'idRemoteNGC_DisplaySign',
                'idRichChallenge_DisplaySign'
            ];
            for (const id of ids) {
                const el = document.getElementById(id);
                if (el) {
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (/^\\d{1,3}$/.test(txt)) return txt;
                }
            }
            const querySelectors = [
                '[data-test-id="richChallangeText"]',
                '[data-test-id="displaySign"]',
                '.displaySign',
                '.rich-challenge',
                '[data-bind*="displaySign"]'
            ];
            for (const sel of querySelectors) {
                const el = document.querySelector(sel);
                if (el) {
                    const txt = (el.innerText || el.textContent || '').trim();
                    if (/^\\d{1,3}$/.test(txt)) return txt;
                }
            }
            const container = document.querySelector('#idDiv_SAOTCAS_Title, #idDiv_SAOTCC_Description, #idRichChallange, .inner, .prompts, form') || document.body;
            if (container) {
                const candidates = container.querySelectorAll('div, span, strong, b');
                for (const c of candidates) {
                    if (c.children.length === 0) {
                        const txt = (c.innerText || c.textContent || '').trim();
                        if (/^\\d{2}$/.test(txt)) {
                            const rect = c.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) return txt;
                        }
                    }
                }
            }
            return null;
        }""")
        if code and re.fullmatch(r"\d{1,3}", str(code)):
            return str(code)
    except Exception:
        pass

    for sel in ("#idRichChallange_DisplaySign", "#richChallangeText", ".displaySign", "#displaySign"):
        try:
            loc = page.locator(sel)
            if loc.count():
                txt = loc.first.inner_text().strip()
                if re.fullmatch(r"\d{1,3}", txt):
                    return txt
        except Exception:
            pass
    return None


def microsoft_login(context, cfg, on_mfa_code=None, on_status=None):
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(MOODLE + "/my/", wait_until="domcontentloaded", timeout=60000)
    if is_moodle_logged_in(page):
        LOG.info("Session Junia déjà ouverte.")
        if on_status:
            try:
                on_status("Session Junia déjà ouverte.")
            except Exception:
                pass
        return page
    LOG.info("Connexion Junia dans Chromium. Vous pouvez intervenir dans la fenêtre.")
    if on_status:
        try:
            on_status("Connexion Junia dans Chromium...")
        except Exception:
            pass
    deadline = time.monotonic() + cfg.login_timeout
    sent_email = sent_password = False
    announced = False
    announced_mfa_code = None
    countdown_until = None
    last_second = None
    last_logged_url = ""
    while time.monotonic() < deadline:
        # Certains portails ouvrent la connexion Microsoft dans une seconde page.
        for candidate in context.pages:
            if is_moodle_logged_in(candidate):
                LOG.info("Authentification Junia confirmée.")
                if on_status:
                    try:
                        on_status("Authentification Junia confirmée.")
                    except Exception:
                        pass
                return candidate
        active = next((p for p in reversed(context.pages) if not p.is_closed()), page)
        current_url = active.url
        if current_url != last_logged_url:
            last_logged_url = current_url
            LOG.info("Étape courante : %s [%s]", current_url, active.title())
            if on_status:
                try:
                    on_status(f"Page : {active.title() or urlsplit(current_url).path}")
                except Exception:
                    pass

        host = urlsplit(active.url).hostname or ""
        if host in {"login.microsoftonline.com", "login.live.com", "login.windows.net"}:
            tile = active.locator(f"[data-test-id*='{cfg.email}'], [role='button']:has-text('{cfg.email}')").filter(has_text=cfg.email)
            if fill_visible(active, "input[type=email], input[name=loginfmt]", cfg.email):
                try:
                    active.locator("input[type=email], input[name=loginfmt]").first.press("Enter", timeout=4000)
                    sent_email = True
                except Exception:
                    pass
            elif not sent_email and tile.count() and tile.first.is_visible():
                try:
                    active.wait_for_timeout(500)
                    tile.first.click(timeout=4000)
                except Exception:
                    pass
            elif cfg.password and not sent_password and fill_visible(active, "input[name=passwd], input[type=password]", cfg.password):
                try:
                    active.locator("input[name=passwd], input[type=password]").first.press("Enter", timeout=4000)
                    sent_password = True
                    LOG.info("Mot de passe soumis, attente de l'A2F...")
                except Exception:
                    pass
            # Valider automatiquement « Rester connecté ? » si présent
            kmsi = active.locator("input#idSIButton9, input[type=submit][value='Oui'], button:has-text('Oui')")
            if kmsi.count() and kmsi.first.is_visible() and active.get_by_text(re.compile("Rester connect|Stay signed", re.I)).count():
                try:
                    check = active.locator("input#KmsiCheckboxField, input[name='DontShowAgain']")
                    if check.count() and check.first.is_visible() and not check.first.is_checked():
                        check.first.check(timeout=2000)
                    kmsi.first.click(timeout=4000)
                except Exception:
                    pass

            # Détection et affichage en grand du code A2F
            if not announced_mfa_code:
                code = extract_mfa_code(active)
                if code:
                    announced_mfa_code = code
                    countdown_until = time.monotonic() + max(cfg.wait_2fa, 60)
                    print("\n" + format_mfa_banner(code), flush=True)
                    LOG.info(">>> CODE MICROSOFT AUTHENTICATOR (A2F) : [ %s ] <<<", code)
                    if on_mfa_code:
                        try:
                            on_mfa_code(code)
                        except Exception as exc:
                            LOG.warning("Erreur callback on_mfa_code : %s", exc)

            # Notification seulement après invite A2F détectée
            if not announced and (announced_mfa_code or (sent_password and active.get_by_text(re.compile("Authenticator|Outlook mobile|approuv|approve|vérifi.*identité|enter the number", re.I)).count())):
                announced = True
                countdown_until = countdown_until or time.monotonic() + max(cfg.wait_2fa, 60)
                if not announced_mfa_code:
                    LOG.info("Validez l'A2F Microsoft sur votre smartphone.")
                    if on_status:
                        try:
                            on_status("📱 Validez la demande sur votre application mobile Microsoft...")
                        except Exception:
                            pass
        elif host == "junia-learning.com":
            sso = active.locator("a[href*='/auth/oidc/']")
            if not sso.count():
                sso = active.locator("a").filter(has_text=re.compile("Microsoft|Office.?365|compte.*JUNIA|Connexion.*JUNIA|adresse.*mail.*JUNIA", re.I))
            if sso.count() and sso.first.is_visible():
                href = sso.first.get_attribute("href")
                if href and href.startswith("http"):
                    try:
                        active.goto(href, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        try:
                            sso.first.click(timeout=5000)
                        except Exception:
                            pass
                else:
                    try:
                        sso.first.click(timeout=5000)
                    except Exception:
                        pass
            else:
                try:
                    active.goto("https://junia-learning.com/auth/oidc/?source=loginpage", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
            active.wait_for_timeout(1000)
        if countdown_until:
            if not announced_mfa_code:
                code = extract_mfa_code(active)
                if code:
                    announced_mfa_code = code
                    countdown_until = time.monotonic() + max(cfg.wait_2fa, 60)
                    print("\n" + format_mfa_banner(code), flush=True)
                    LOG.info(">>> CODE MICROSOFT AUTHENTICATOR (A2F) : [ %s ] <<<", code)
                    if on_mfa_code:
                        try:
                            on_mfa_code(code)
                        except Exception as exc:
                            LOG.warning("Erreur callback on_mfa_code : %s", exc)
            remaining = max(0, int(countdown_until - time.monotonic() + 0.999))
            if remaining != last_second:
                print(f"\rAttente A2F : {remaining:2d} seconde(s)   ", end="", flush=True)
                last_second = remaining
                if remaining == 0:
                    print("\nAttente du retour effectif vers Junia (la connexion n'est pas encore confirmée).")
                    countdown_until = None
        # Aucun clic sur une validation MFA, un CAPTCHA ou une demande de consentement.
        active.wait_for_timeout(250)
    raise RuntimeError(f"Connexion non confirmée après {cfg.login_timeout} secondes. Relancer puis terminer la connexion visible.")


class SameOriginRedirect(HTTPRedirectHandler):
    def __init__(self, base_url):
        self.origin = urlsplit(base_url)

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        p = urlsplit(newurl)
        if (p.scheme, p.netloc) != (self.origin.scheme, self.origin.netloc):
            fp.close()
            raise ExternalRedirect(newurl)
        if "/login/" in p.path:
            fp.close()
            raise AuthenticationExpired("Session Junia expirée. Relancer pour se reconnecter.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


@dataclass
class Metadata:
    size: int | None = None
    etag: str = ""
    modified: str = ""
    content_type: str = ""
    name: str = ""

    @classmethod
    def from_headers(cls, headers, url):
        length = headers.get("Content-Length", "")
        message = Message()
        message["Content-Disposition"] = headers.get("Content-Disposition", "")
        name = message.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        name = name.replace("\\", "/").split("/")[-1]
        return cls(int(length) if length.isdigit() else None, headers.get("ETag", ""), headers.get("Last-Modified", ""), headers.get("Content-Type", "").lower(), clean_name(name, True))


class MoodleHTTP:
    def __init__(self, context, cfg, base_url=MOODLE):
        self.cfg, self.base_url = cfg, base_url
        jar = http.cookiejar.CookieJar()
        for cookie in context.cookies():
            jar.set_cookie(http.cookiejar.Cookie(
                0, cookie["name"], cookie["value"], None, False, cookie["domain"],
                True, cookie["domain"].startswith("."), cookie["path"], True,
                cookie["secure"], int(cookie["expires"]) if cookie["expires"] > 0 else None,
                cookie["expires"] <= 0, None, None, {}, False,
            ))
        self.opener = build_opener(SameOriginRedirect(base_url), HTTPCookieProcessor(jar))

    def open(self, url, method="GET"):
        a, b = urlsplit(url), urlsplit(self.base_url)
        if (a.scheme, a.netloc) != (b.scheme, b.netloc):
            raise ValueError("Les cookies Junia ne peuvent pas être envoyés à une autre origine")
        request = Request(url, method=method, headers={"User-Agent": "JuniaCourseSync/1.0", "Accept-Encoding": "identity"})
        for attempt in range(self.cfg.http_retries):
            try:
                response = self.opener.open(request, timeout=self.cfg.http_timeout)
                if "/login/" in urlsplit(response.geturl()).path:
                    response.close()
                    raise AuthenticationExpired("Session Junia expirée. Relancer pour se reconnecter.")
                return response
            except HTTPError as exc:
                retryable = exc.code in {429, 500, 502, 503, 504}
                exc.close()
                if not retryable or attempt + 1 == self.cfg.http_retries:
                    raise
            except (URLError, TimeoutError):
                if attempt + 1 == self.cfg.http_retries:
                    raise
            time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("Requête HTTP interrompue")

    def head(self, url):
        try:
            with self.open(url, "HEAD") as response:
                result = Metadata.from_headers(response.headers, response.geturl())
                if "text/html" in result.content_type and not response.headers.get("Content-Disposition"):
                    raise AuthenticationExpired("Un fichier Moodle renvoie une page HTML au lieu du support.")
                return result
        except HTTPError as exc:
            if exc.code in {405, 501}:
                return Metadata(name=clean_name(unquote(urlsplit(url).path.rsplit("/", 1)[-1]), True))
            raise

    def download(self, url, target):
        with self.open(url) as response, target.open("wb") as output:
            meta = Metadata.from_headers(response.headers, response.geturl())
            if "text/html" in meta.content_type and not response.headers.get("Content-Disposition"):
                raise AuthenticationExpired("Le téléchargement renvoie une page HTML ; fichier non envoyé.")
            digest, size = hashlib.sha256(), 0
            while chunk := response.read(BLOCK):
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size == 0 or (meta.size is not None and size != meta.size):
            raise IOError("Téléchargement incomplet : taille reçue incorrecte")
        return size, digest.hexdigest(), meta


@dataclass
class Resource:
    course: str
    section: str
    url: str
    title: str

    @property
    def prefix(self):
        return f"{clean_name(self.course)}/{clean_name(self.section)}/"


def extract_section_title(element, default="Général") -> str:
    if not element:
        return default
    for sel in (".courseindex-name", ".sectionname-text", "[data-for='section_title']"):
        target = element.select_one(sel)
        if target:
            txt = target.get_text(" ", strip=True)
            if txt:
                return clean_name(txt)
    clone = BeautifulSoup(str(element), "html.parser")
    for badge in clone.select(".section-number, .courseindex-section-number, .sr-only, .accesshide, .number, .badge"):
        badge.decompose()
    txt = clone.get_text(" ", strip=True)
    if txt:
        m = re.match(r"^(\d+)\s+([A-Za-zÀ-ÖØ-öø-ÿ].*)$", txt)
        if m:
            txt = m.group(2)
        return clean_name(txt)
    return default


def section_of(element, default):
    for parent in element.parents:
        identifier = parent.get("id", "")
        if re.fullmatch(r"section-\d+", identifier):
            if identifier == "section-0":
                return "Général"
            heading = parent.select_one("[id^='coursecontentsection'], .sectionname")
            return extract_section_title(heading, default)
    return default


def parse_links(html, url, course, section):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("#region-main") or soup
    result, seen = [], set()
    for node in main.select("a[href], iframe[src], object[data], embed[src]"):
        link = urljoin(url, node.get("href") or node.get("src") or node.get("data") or "")
        p = urlsplit(link)
        if p.scheme not in {"https", "http"} or link in seen:
            continue
        seen.add(link)
        title = node.get_text(" ", strip=True) or node.get("title") or node.get("aria-label") or "Ressource intégrée"
        name = section_of(node, section)
        if p.netloc != urlsplit(MOODLE).netloc:
            kind = "external"
        elif "/pluginfile.php/" in p.path:
            kind = "file"
        elif p.path.endswith("/course/section.php"):
            kind = "section"
        elif re.search(r"/mod/(resource|folder|page|book|url|assign)/view\.php$", p.path):
            kind = "module"
        elif re.search(r"/mod/(quiz|lesson|scorm|h5pactivity|lti)/view\.php$", p.path):
            kind = "interactive"
        else:
            continue
        result.append((kind, Resource(course, name, link, title)))
    # L'index peut contenir des liens de sections absents de region-main.
    for node in soup.select("a[href*='/course/section.php']"):
        link = urljoin(url, node["href"])
        if link not in seen and urlsplit(link).netloc == urlsplit(MOODLE).netloc:
            seen.add(link)
            result.append(("section", Resource(course, extract_section_title(node, section), link, "")))
    text = main.get_text("\n", strip=True)
    blocked = "Ce cours n’est actuellement pas disponible pour les étudiants" in text or "Ce cours n'est actuellement pas disponible pour les étudiants" in text
    enrolled = bool(soup.select_one("#page-enrol-index, body#page-enrol-index")) or "/enrol/" in urlsplit(url).path
    if enrolled:
        blocked = True
    return result, text, blocked


class LocalStore:
    """Stockage direct sur disque (ex: sur VPS avec OneDrive monté via Rclone)."""
    def __init__(self, root: Path | str, cfg=None):
        self.root = Path(root).resolve()
        self.cfg = cfg
        self.ledger_path = self.root / ".sync_moodle_manifest.json"
        self.records = {}
        self.verified = {}
        self.known_dirs = set()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.ledger_path.is_file():
            try:
                with self.ledger_path.open("r", encoding="utf-8") as stream:
                    data = json.load(stream)
                if data.get("version") == 1 and isinstance(data.get("files"), dict):
                    for relative, record in data["files"].items():
                        safe_relative(relative)
                        self.records[relative] = record
            except Exception as exc:
                LOG.warning("Lecture manifest local : %s", exc)

    def attributes(self, path):
        p = Path(path) if Path(path).is_absolute() else self.root / safe_relative(str(path).replace("\\", "/"))
        try:
            return p.stat() if p.exists() else None
        except OSError:
            return None

    def remote(self, relative):
        return str((self.root / safe_relative(relative)).as_posix())

    def mkdirs(self, directory):
        p = Path(directory) if Path(directory).is_absolute() else self.root / safe_relative(directory)
        p.mkdir(parents=True, exist_ok=True)

    def digest(self, path):
        p = Path(path) if Path(path).is_absolute() else self.root / safe_relative(path)
        return sha_file(p)

    def matches(self, relative, size, digest=None):
        p = self.root / safe_relative(relative)
        if not p.is_file():
            return False
        stat_val = p.stat()
        if stat_val.st_size != size:
            return False
        if not ((self.cfg.verify_hash if self.cfg else True) and digest):
            return True
        fingerprint = {"size": size, "mtime": getattr(stat_val, "st_mtime_ns", stat_val.st_mtime), "sha256": digest}
        cached = self.verified.get(relative) or self.records.get(relative, {}).get("verified_remote")
        if (getattr(self.cfg, "fast_remote_check", True) if self.cfg else True) and cached == fingerprint:
            return True
        if sha_file(p) != digest:
            return False
        self.verified[relative] = fingerprint
        if relative in self.records:
            self.records[relative]["verified_remote"] = fingerprint
        return True

    def upload(self, source, relative, size, digest):
        target = self.root / safe_relative(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.parent / (f".{target.name}.part-{uuid.uuid4().hex}")
        shutil.copy2(source, part)
        stat_val = part.stat()
        if stat_val.st_size != size:
            part.unlink(missing_ok=True)
            raise IOError("Taille incorrecte après copie locale")
        if (self.cfg.verify_hash if self.cfg else True) and sha_file(part) != digest:
            part.unlink(missing_ok=True)
            raise IOError("Empreinte SHA-256 incorrecte après copie locale")
        part.replace(target)
        self.verified[relative] = {"size": size, "mtime": getattr(stat_val, "st_mtime_ns", stat_val.st_mtime), "sha256": digest}
        self.remember(relative, size, digest)

    def remember(self, relative, size, digest, url="", meta=None):
        record = self.records.setdefault(relative, {"sources": {}})
        record.update(size=size, sha256=digest)
        if relative in self.verified and self.verified[relative]["sha256"] == digest:
            record["verified_remote"] = self.verified[relative]
        if url and ("/pluginfile.php/" in urlsplit(url).path or "/mod/resource/" in urlsplit(url).path):
            record.setdefault("sources", {})[canonical_url(url)] = {
                "size": size, "etag": meta.etag if meta else "",
                "modified": meta.modified if meta else "",
            }

    def save(self, directory=None):
        part = self.root / (f".manifest-{uuid.uuid4().hex}.tmp")
        data = {"version": 1, "files": self.records}
        part.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        part.replace(self.ledger_path)


class RemoteStore:
    def __init__(self, sftp, cfg):
        self.sftp, self.cfg = sftp, cfg
        self.root = cfg.remote_dir
        self.ledger_path = posixpath.join(self.root, ".sync_moodle_manifest.json")
        self.records = {}
        self.verified = {}
        self.known_dirs = set()
        self.mkdirs(self.root)
        if self.attributes(self.ledger_path):
            with self.sftp.open(self.ledger_path, "rb") as stream:
                data = json.load(stream)
            if data.get("version") != 1 or not isinstance(data.get("files"), dict):
                raise ValueError("Manifest distant non reconnu ; conservé sans modification")
            for relative, record in data["files"].items():
                safe_relative(relative)
                self.records[relative] = record

    def attributes(self, path):
        try:
            return self.sftp.lstat(path)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None
            raise

    def remote(self, relative):
        return posixpath.join(self.root, safe_relative(relative))

    def mkdirs(self, directory):
        if directory in self.known_dirs:
            return
        current = "/"
        for part in PurePosixPath(directory).parts[1:]:
            current = posixpath.join(current, part)
            if current in self.known_dirs:
                continue
            attrs = self.attributes(current)
            if attrs is None:
                self.sftp.mkdir(current)
            elif not stat.S_ISDIR(attrs.st_mode):
                raise IOError(f"Le chemin distant n'est pas un répertoire : {current}")
            self.known_dirs.add(current)
        self.known_dirs.add(directory)

    def digest(self, path):
        with self.sftp.open(path, "rb") as stream:
            if isinstance(stream, paramiko.SFTPFile):
                # Plusieurs lectures en vol évitent un aller-retour SSH par bloc.
                stream.prefetch(max_concurrent_requests=8)
            return sha_stream(stream)

    def matches(self, relative, size, digest=None):
        attrs = self.attributes(self.remote(relative))
        if not attrs or not stat.S_ISREG(attrs.st_mode) or attrs.st_size != size:
            return False
        if not (self.cfg.verify_hash and digest):
            return True
        fingerprint = {"size": size, "mtime": getattr(attrs, "st_mtime_ns", attrs.st_mtime), "sha256": digest}
        cached = self.verified.get(relative) or self.records.get(relative, {}).get("verified_remote")
        if getattr(self.cfg, "fast_remote_check", False) and attrs.st_mtime is not None and cached == fingerprint:
            return True
        if self.digest(self.remote(relative)) != digest:
            return False
        self.verified[relative] = fingerprint
        if relative in self.records:
            self.records[relative]["verified_remote"] = fingerprint
        return True

    def promote(self, temporary, destination):
        existing = self.attributes(destination)
        if existing and not stat.S_ISREG(existing.st_mode):
            raise IOError("La destination existante n'est pas un fichier ordinaire")
        try:
            self.sftp.posix_rename(temporary, destination)
            return
        except OSError as exc:
            if exc.errno not in {None, errno.ENOSYS, errno.EOPNOTSUPP}:
                raise
            # SFTP ancien sans extension OpenSSH : conserver puis restaurer l'ancien
            # fichier si la seconde opération échoue. Les erreurs de permission
            # ne sont pas une raison pour supprimer un fichier existant.
            if "unsupported" not in str(exc).lower() and exc.errno is None:
                raise
        backup = destination + ".previous-" + uuid.uuid4().hex
        if existing:
            self.sftp.rename(destination, backup)
        try:
            self.sftp.rename(temporary, destination)
        except BaseException:
            if existing:
                self.sftp.rename(backup, destination)
            raise
        if existing:
            self.sftp.remove(backup)

    def upload(self, local, relative, size, digest):
        destination = self.remote(relative)
        self.mkdirs(posixpath.dirname(destination))
        temporary = destination + ".part-" + uuid.uuid4().hex
        try:
            self.sftp.put(str(local), temporary, confirm=True)
            if self.sftp.lstat(temporary).st_size != size:
                raise IOError("Taille incorrecte après transfert SFTP")
            if self.cfg.verify_hash and self.digest(temporary) != digest:
                raise IOError("Empreinte SHA-256 incorrecte après transfert SFTP")
            self.promote(temporary, destination)
            self.verified.pop(relative, None)
            if self.cfg.verify_hash:
                attrs = self.attributes(destination)
                self.verified[relative] = {"size": size, "mtime": getattr(attrs, "st_mtime_ns", attrs.st_mtime), "sha256": digest}
        except BaseException:
            try:
                self.sftp.remove(temporary)
            except OSError:
                pass
            raise

    def remember(self, relative, size, digest, url="", meta=None):
        record = self.records.setdefault(relative, {"sources": {}})
        record.update(size=size, sha256=digest)
        if relative in self.verified and self.verified[relative]["sha256"] == digest:
            record["verified_remote"] = self.verified[relative]
        if url and ("/pluginfile.php/" in urlsplit(url).path or "/mod/resource/" in urlsplit(url).path):
            record.setdefault("sources", {})[canonical_url(url)] = {
                "size": size, "etag": meta.etag if meta else "",
                "modified": meta.modified if meta else "",
            }

    def save(self, directory):
        path = directory / ("manifest-" + uuid.uuid4().hex + ".json")
        path.write_text(json.dumps({"version": 1, "files": self.records}, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            self.upload(path, ".sync_moodle_manifest.json", path.stat().st_size, sha_file(path))
        finally:
            path.unlink(missing_ok=True)


@dataclass
class Summary:
    sent: set = field(default_factory=set)
    skipped: set = field(default_factory=set)
    metadata_sent: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    unavailable: list = field(default_factory=list)
    courses: dict = field(default_factory=dict)
    bytes_sent: int = 0
    external_links: int = 0

    def mark(self, relative, sent=False, metadata=False):
        if metadata:
            if sent:
                self.metadata_sent.add(relative)
        elif sent:
            self.sent.add(relative)
            self.skipped.discard(relative)
        elif relative not in self.sent:
            self.skipped.add(relative)


def sync_local(store, cfg, summary, allowed_courses=None):
    """Envoyer exclusivement les fichiers encore présents, sans accès Moodle."""
    root = cfg.audit_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Dossier de cours absent : {root}")
    allowed_names = {clean_name(name) for _, name in allowed_courses} if allowed_courses else None
    for local in sorted(root.rglob("*")):
        if local.is_symlink():
            raise ValueError(f"Lien symbolique refusé : {local}")
        if not local.is_file():
            continue
        relative = safe_relative(local.relative_to(root).as_posix())
        course = relative.split("/")[0]
        if course in {"_Inventaire", "Valorisation de l'Engagement Sociétal", "Projet Professionnel"}:
            continue
        if allowed_names and course not in allowed_names:
            continue
        if not getattr(cfg, "save_notes", True) and local.suffix.lower() == ".md":
            continue
        try:
            size, digest = local.stat().st_size, sha_file(local)
            metadata = local.suffix.lower() in {".md", ".json"}
            if store.matches(relative, size, digest):
                summary.mark(relative, metadata=metadata)
                LOG.info("Ignoré (vérifié) : %s", relative)
            else:
                store.upload(local, relative, size, digest)
                summary.mark(relative, sent=True, metadata=metadata)
                summary.bytes_sent += size
                LOG.info("Envoyé : %s", relative)
            store.remember(relative, size, digest)
            summary.courses[course] = "fichiers locaux synchronisés"
        except Exception as exc:
            summary.errors.append({"resource": relative, "error": str(exc)})
            LOG.error("%s : %s", relative, exc)


class Synchronizer:
    def __init__(self, http, store, cfg, summary):
        self.http, self.store, self.cfg, self.summary = http, store, cfg, summary
        self.temp = cfg.state_dir / "temp"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.links = defaultdict(list)
        self.interactive = defaultdict(list)
        self.seen_files = set()

    def error(self, label, exc):
        LOG.error("%s : %s", label, exc)
        self.summary.errors.append({"resource": label, "error": str(exc)})

    def bootstrap_audit(self):
        seed = json.loads((APP_DIR / "audited_files.json").read_text(encoding="utf-8"))
        if not self.cfg.bootstrap or not self.cfg.audit_dir.is_dir():
            return
        LOG.info("Amorçage depuis l'audit local : seuls les fichiers distants absents sont envoyés.")
        for number, entry in enumerate(seed, 1):
            relative = safe_relative(fix_mojibake(entry["relative_path"]))
            if relative in self.store.records:
                continue
            local = self.cfg.audit_dir / Path(*PurePosixPath(relative).parts)
            if not local.is_file():
                raw_local = self.cfg.audit_dir / Path(*PurePosixPath(safe_relative(entry["relative_path"])).parts)
                if raw_local.is_file():
                    local = raw_local
                else:
                    continue
            try:
                LOG.info("Audit %d/%d : vérification de %s", number, len(seed), relative)
                attrs = self.store.attributes(self.store.remote(relative))
                if attrs:
                    if relative not in self.store.records and attrs.st_size == entry["size"]:
                        self.store.remember(relative, entry["size"], entry["sha256"])
                    continue
                if local.stat().st_size != entry["size"] or sha_file(local) != entry["sha256"]:
                    raise IOError("Le fichier de l'audit local a changé")
                self.store.upload(local, relative, entry["size"], entry["sha256"])
                self.store.remember(relative, entry["size"], entry["sha256"])
                self.summary.mark(relative, sent=True)
                self.summary.bytes_sent += entry["size"]
                LOG.info("Audit envoyé : %s", relative)
            except Exception as exc:
                self.error(relative, exc)
        LOG.info("Vérification de l'audit terminée ; sauvegarde du manifest.")
        self.store.save(self.temp)

    def file(self, resource, meta=None, origin_url=None):
        identity = canonical_url(resource.url)
        course_clean = clean_name(resource.course)
        seen_key = (course_clean, identity)
        if seen_key in self.seen_files:
            return
        self.seen_files.add(seen_key)
        if origin_url:
            self.seen_files.add((course_clean, canonical_url(origin_url)))

        if meta is None:
            meta = self.http.head(resource.url)
        relative = resource.prefix + meta.name
        record = None
        course_prefix = f"{course_clean}/"
        for candidate, saved in self.store.records.items():
            if candidate.startswith(course_prefix) and identity in saved.get("sources", {}):
                relative, record = candidate, saved
                break

        audited = self.store.records.get(relative)
        if not audited and not record:
            target_filename = meta.name
            for cand, saved in self.store.records.items():
                if cand.startswith(course_prefix) and PurePosixPath(cand).name == target_filename:
                    relative, audited = cand, saved
                    break

        expected_size = meta.size if meta.size is not None else (record.get("size") if record else (audited.get("size") if audited else None))
        if not record and audited and not audited.get("sources") and expected_size is not None and self.store.matches(relative, expected_size, audited.get("sha256")):
            self.store.remember(relative, expected_size, audited.get("sha256"), resource.url, meta)
            if origin_url:
                self.store.remember(relative, expected_size, audited.get("sha256"), origin_url, meta)
            self.summary.mark(relative)
            LOG.info("Ignoré (déjà présent sur le serveur) : %s", relative)
            return
        if record:
            before = record.get("sources", {}).get(identity)
            if before:
                validator_changed = any(current and current != previous for current, previous in ((meta.etag, before.get("etag")), (meta.modified, before.get("modified"))))
                has_validator = bool(meta.etag or meta.modified)
                check_size = meta.size if meta.size is not None else record.get("size")
                if not validator_changed and (has_validator or not self.cfg.strict_source) and check_size is not None and self.store.matches(relative, check_size, record.get("sha256")):
                    self.summary.mark(relative)
                    if origin_url and origin_url != resource.url:
                        self.store.remember(relative, check_size, record.get("sha256"), origin_url, meta)
                    LOG.info("Ignoré (déjà présent et vérifié) : %s", relative)
                    return
        # Téléchargement en flux, sans conserver tout le PDF/PowerPoint en mémoire.
        temporary = self.temp / (uuid.uuid4().hex + ".download")
        size, digest, actual_meta = self.http.download(resource.url, temporary)
        if record and record.get("sha256") != digest and len(record.get("sources", {})) > 1:
            # Deux liens autrefois identiques peuvent ensuite publier des versions différentes.
            # Le lien modifié reçoit sa propre variante, sans écraser l'autre ressource.
            record["sources"].pop(identity, None)
            record = None
        if not record:
            # Les liens de résumé et les dossiers peuvent exposer le même contenu.
            target_name = PurePosixPath(relative).name
            identical = next((p for p, row in self.store.records.items() if p.startswith(resource.prefix) and row.get("sha256") == digest), None)
            if not identical:
                identical = next((p for p, row in self.store.records.items() if p.startswith(course_prefix) and PurePosixPath(p).name == target_name and row.get("sha256") == digest), None)
            if identical:
                relative = identical
            elif self.store.attributes(self.store.remote(relative)):
                # Même nom, autre source : conserver les deux versions.
                if not self.store.matches(relative, size, digest):
                    path = PurePosixPath(relative)
                    relative = str(path.with_name(path.stem + " - variante " + digest[:8] + path.suffix))
        if self.store.matches(relative, size, digest):
            self.summary.mark(relative)
            LOG.info("Ignoré (contenu identique) : %s", relative)
        else:
            self.store.upload(temporary, relative, size, digest)
            self.summary.mark(relative, sent=True)
            self.summary.bytes_sent += size
            LOG.info("Envoyé : %s", relative)
        self.store.remember(relative, size, digest, resource.url, actual_meta)
        if origin_url:
            self.store.remember(relative, size, digest, origin_url, actual_meta)
        temporary.unlink()  # Un échec garde le fichier local pour diagnostic/reprise.

    def note(self, relative, text):
        if not getattr(self.cfg, "save_notes", True):
            return
        path = self.temp / (uuid.uuid4().hex + ".md")
        path.write_text(text, encoding="utf-8")
        size, digest = path.stat().st_size, sha_file(path)
        if not self.store.matches(relative, size, digest):
            self.store.upload(path, relative, size, digest)
            self.summary.mark(relative, sent=True, metadata=True)
        self.store.remember(relative, size, digest)
        path.unlink()

    def course(self, course_id, name):
        LOG.info("\nCours : %s", name)
        course_clean = clean_name(name)
        course_prefix = f"{course_clean}/"
        pending = deque([(MOODLE + f"/course/view.php?id={course_id}", "Général", "course", "")])
        visited = set()
        self.summary.courses[name] = "en cours"
        while pending:
            url, section, kind, title = pending.popleft()
            if url in visited:
                continue
            visited.add(url)
            if kind == "module" and (course_clean, canonical_url(url)) in self.seen_files:
                continue
            try:
                if kind == "module" and "/mod/resource/" in url:
                    canonical_mod = canonical_url(url)
                    known_file = next(
                        (cand for cand, row in self.store.records.items()
                         if cand.startswith(course_prefix) and canonical_mod in row.get("sources", {})),
                        None
                    )
                    if known_file and (known_file in self.summary.skipped or known_file in self.summary.sent):
                        continue
                    try:
                        with self.http.open(url, "HEAD") as probe:
                            probe_meta = Metadata.from_headers(probe.headers, probe.geturl())
                            if "text/html" not in probe_meta.content_type or probe.headers.get("Content-Disposition"):
                                self.file(Resource(name, section, probe.geturl(), title), meta=probe_meta, origin_url=url)
                                continue
                    except HTTPError as exc:
                        if exc.code not in {405, 501}:
                            raise
                with self.http.open(url) as response:
                    final_url = response.geturl()
                    meta = Metadata.from_headers(response.headers, final_url)
                    if "text/html" not in meta.content_type or response.headers.get("Content-Disposition"):
                        self.file(Resource(name, section, final_url, title), meta=meta, origin_url=url)
                        continue
                    html = response.read(16 * BLOCK + 1)
                    if len(html) > 16 * BLOCK:
                        raise ValueError("Page HTML trop volumineuse")
                    html = html.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                found, text, blocked = parse_links(html, final_url, name, section)
                if blocked:
                    self.summary.unavailable.append(name)
                    self.summary.courses[name] = "fermé ou inscription requise"
                    self.note(clean_name(name) + "/acces_indisponible.md", f"# {name}\n\nSource : {url}\n\n{text}\n")
                    LOG.info("Cours fermé aux étudiants ou inscription requise : %s", name)
                    return
                if kind == "course":
                    self.summary.courses[name] = "accessible"
                if not found and "login" in html.lower() and BeautifulSoup(html, "html.parser").select_one("input[type=password]"):
                    raise AuthenticationExpired("Page de connexion reçue pendant l'exploration")
                if kind == "module" and re.search(r"/mod/(page|assign)/", url):
                    self.note(f"{clean_name(name)}/{clean_name(section)}/{clean_name(title, True)}.md", f"# {title}\n\nSource : {url}\n\n{text}\n")
                for target_kind, resource in found:
                    if target_kind == "file":
                        try:
                            self.file(resource)
                        except AuthenticationExpired:
                            raise
                        except Exception as exc:
                            self.error(resource.title, exc)
                    elif target_kind == "section":
                        # La section du lien est son intitulé, pas celle qui contient le lien.
                        pending.append((resource.url, resource.title if resource.title and resource.title != "Ressource intégrée" else resource.section, "section", ""))
                    elif target_kind == "module":
                        pending.append((resource.url, resource.section, "module", resource.title))
                    elif target_kind == "external":
                        self.links[name].append(resource)
                    elif target_kind == "interactive":
                        self.interactive[name].append(resource)
            except AuthenticationExpired as exc:
                # Un module URL peut rediriger vers une destination externe :
                # extraire sa destination de l'en-tête sans lui envoyer les cookies.
                if "/mod/url/" in url and isinstance(exc, ExternalRedirect):
                    self.links[name].append(Resource(name, section, exc.url, title))
                else:
                    raise
            except Exception as exc:
                self.summary.courses[name] = "partiellement traité : erreur"
                self.error(f"{name} / {title or section}", exc)
        self.write_links(name)

    def write_links(self, name):
        if not getattr(self.cfg, "save_notes", True):
            return
        for collection, filename, heading in ((self.links[name], "liens_externes.md", "Liens externes"), (self.interactive[name], "activites_en_ligne.md", "Activités interactives en ligne")):
            unique = {r.url: r for r in collection}
            if not unique:
                continue
            rows = [f"# {heading} — {name}", ""]
            for r in sorted(unique.values(), key=lambda x: (x.section, x.title, x.url)):
                rows.append(f"- **{r.section}** — {r.title} : {r.url}")
            self.note(clean_name(name) + "/" + filename, "\n".join(rows) + "\n")
            if filename == "liens_externes.md":
                self.summary.external_links += len(unique)


class ExternalRedirect(AuthenticationExpired):
    def __init__(self, url):
        super().__init__("Ressource externe ou connexion Microsoft requise")
        self.url = url


def report(summary, cfg):
    def redact(value):
        for secret in (cfg.password, cfg.ssh_password, cfg.key_passphrase):
            if secret:
                value = value.replace(secret, "[masqué]")
        return value
    errors = [{key: redact(str(value)) for key, value in row.items()} for row in summary.errors]
    data = {"date": datetime.now(timezone.utc).isoformat(), "sent": sorted(summary.sent), "skipped": sorted(summary.skipped), "metadata_sent": sorted(summary.metadata_sent), "errors": errors, "unavailable": summary.unavailable, "courses": summary.courses, "bytes_sent": summary.bytes_sent, "external_links": summary.external_links}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    (cfg.state_dir / "dernier_rapport.json").write_text(text, encoding="utf-8")

    print("\n" + "=" * 65)
    if summary.sent:
        print(f" ✨ RÉCAPITULATIF DES NOUVEAUX COURS DÉTECTÉS ({len(summary.sent)}) :")
        print("=" * 65)
        by_course = {}
        for item in sorted(summary.sent):
            parts = item.split("/", 1)
            course_name = parts[0]
            file_name = parts[1] if len(parts) > 1 else item
            by_course.setdefault(course_name, []).append(file_name)
        for course_name, files in sorted(by_course.items()):
            print(f"\n 📁 [{course_name}] ({len(files)} nouveau(x)) :")
            for f in files:
                print(f"    └── 📄 {f}")
    else:
        print(" ℹ️ Aucun nouveau fichier : tous les cours sont déjà à jour.")
    print("=" * 65)

    print("\nRésumé de la synchronisation")
    print(f"Fichiers nouveaux/mis à jour : {len(summary.sent)}")
    print(f"Fichiers ignorés : {len(summary.skipped)}")
    print(f"Notes et listes de liens mises à jour : {len(summary.metadata_sent)}")
    print(f"Liens externes recensés : {summary.external_links}")
    print(f"Cours fermés / inscription requise : {len(summary.unavailable)}")
    print(f"Erreurs : {len(summary.errors)}")
    print(f"Destination : {cfg.user}@{cfg.host}:{cfg.remote_dir}")
    print(f"Rapport : {cfg.state_dir / 'dernier_rapport.json'}")


def resolve_course_selection(available, targets):
    """Filtre les cours selon les indices (1-based), ID Moodle ou portions de nom."""
    if not targets:
        return available
    chosen = []
    available_map = {str(i): c for i, c in enumerate(available, 1)}
    id_map = {str(c[0]): c for c in available}
    for target in targets:
        target_str = str(target).strip()
        if not target_str:
            continue
        # Indice numérique 1-based (ex: '3')
        if target_str in available_map:
            c = available_map[target_str]
            if c not in chosen:
                chosen.append(c)
            continue
        # ID numérique Moodle (ex: '19333')
        if target_str in id_map:
            c = id_map[target_str]
            if c not in chosen:
                chosen.append(c)
            continue
        # Correspondance textuelle insensible à la casse et aux accents (ex: 'auto', 'meca')
        normalized_target = unicodedata.normalize("NFD", target_str.lower()).encode("ascii", "ignore").decode()
        matched = False
        for c in available:
            norm_cname = unicodedata.normalize("NFD", c[1].lower()).encode("ascii", "ignore").decode()
            if normalized_target in norm_cname:
                if c not in chosen:
                    chosen.append(c)
                matched = True
        if not matched:
            LOG.warning("Aucune matière trouvée correspondant à '%s'", target_str)
    return chosen or available


def prompt_course_selection(available):
    """Affiche le menu interactif de sélection des matières."""
    print("\n" + "=" * 65)
    print(" 📚 SÉLECTION DES MATIÈRES À SYNCHRONISER")
    print("=" * 65)
    for i, (cid, name) in enumerate(available, 1):
        print(f"  [{i:2d}] {name}")
    print("  [ A] Toutes les matières (par défaut)")
    print("=" * 65)
    try:
        raw = input("\n👉 Choisir les numéros (ex: 3,4) ou 'A' / Entrée pour tout : ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAnnulation.")
        sys.exit(0)
    if not raw or raw.upper() in {"A", "ALL", "TOUT"}:
        return available
    tokens = [t.strip() for t in raw.replace(";", ",").split(",") if t.strip()]
    return resolve_course_selection(available, tokens)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=APP_DIR / ".env")
    parser.add_argument("--check-config", action="store_true", help="Valider le fichier .env sans connexion réseau")
    parser.add_argument("--local-only", action="store_true", help="Synchroniser uniquement les cours conservés sur le PC, sans Moodle")
    parser.add_argument("--moodle", action="store_true", help="Relancer la découverte Moodle même si LOCAL_ONLY=true")
    parser.add_argument("--course", "-c", action="append", dest="courses", default=[],
                        help="Matière(s) à synchroniser (nom, numéro ou ID Moodle). Répétable.")
    parser.add_argument("--all", "-a", action="store_true", help="Synchroniser toutes les matières sans confirmation")
    parser.add_argument("selected_courses", nargs="*",
                        help="Nom(s) ou numéro(s) de matière(s) à synchroniser (ex: Automatique ou 3)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = Config.load(args.env)
    except (ValueError, OSError) as exc:
        print(f"Configuration : {exc}")
        return 2
    if args.check_config:
        print(f"Configuration valide : {cfg.user}@{cfg.host}:{cfg.port}, destination {cfg.remote_dir}")
        print(f"Connexion Junia : {'email configuré' if cfg.email else 'manuelle dans le navigateur'}")
        return 0

    targets = (args.courses or []) + (args.selected_courses or [])
    if targets:
        target_courses = resolve_course_selection(COURSES, targets)
    elif not args.all and sys.stdin.isatty():
        target_courses = prompt_course_selection(COURSES)
    else:
        target_courses = COURSES

def run_sync(cfg: Config, target_courses: list | None = None, local_only: bool = False, force_moodle: bool = False,
             on_mfa_code=None, on_progress=None, on_status=None) -> tuple[Summary, int]:
    if target_courses is None:
        target_courses = COURSES
    summary, client, sftp, store, context, lock = Summary(), None, None, None, None, None
    manifest_saved = False
    try:
        candidate = RunLock(cfg.state_dir / "run.lock")
        candidate.__enter__()
        lock = candidate

        if cfg.local_storage:
            LOG.info("Stockage direct sur disque : %s", cfg.remote_dir)
            if on_status:
                try:
                    on_status(f"Stockage local : {cfg.remote_dir}")
                except Exception:
                    pass
            store = LocalStore(Path(cfg.remote_dir), cfg)
        else:
            LOG.info("Connexion SFTP au démarrage : %s@%s:%s", cfg.user, cfg.host, cfg.port)
            if on_status:
                try:
                    on_status(f"Connexion SFTP : {cfg.user}@{cfg.host}:{cfg.port}")
                except Exception:
                    pass
            client, sftp = ssh_connect(cfg)
            store = RemoteStore(sftp, cfg)

        if local_only or (cfg.local_only and not force_moodle):
            LOG.info("Mode local : aucune ressource supprimée ne sera téléchargée à nouveau.")
            if on_status:
                try:
                    on_status("Mode local...")
                except Exception:
                    pass
            sync_local(store, cfg, summary, target_courses)
        else:
            if on_status:
                try:
                    on_status("Lancement du navigateur Chromium...")
                except Exception:
                    pass
            with sync_playwright() as playwright:
                launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
                context = playwright.chromium.launch_persistent_context(
                    str(cfg.state_dir / "browser-profile"), headless=cfg.headless,
                    args=launch_args, accept_downloads=True, viewport={"width": 1280, "height": 850},
                )
                try:
                    microsoft_login(context, cfg, on_mfa_code=on_mfa_code, on_status=on_status)
                    synchronizer = Synchronizer(MoodleHTTP(context, cfg), store, cfg, summary)
                    if on_status:
                        try:
                            on_status("Amorçage de l'audit...")
                        except Exception:
                            pass
                    synchronizer.bootstrap_audit()
                    for number, (course_id, name) in enumerate(target_courses, 1):
                        LOG.info("Progression : cours %d/%d (%s)", number, len(target_courses), name)
                        if on_progress:
                            try:
                                on_progress(number, len(target_courses), name)
                            except Exception:
                                pass
                        synchronizer.course(course_id, name)
                    store.save(synchronizer.temp)
                    manifest_saved = True
                finally:
                    context.close()
                    context = None
    except KeyboardInterrupt:
        summary.errors.append({"resource": "Exécution", "error": "Interruption par l'utilisateur"})
        LOG.info("Interruption : les transferts terminés restent disponibles.")
    except Exception as exc:
        LOG.error("Synchronisation arrêtée : %s", exc)
        summary.errors.append({"resource": "Exécution", "error": str(exc)})
    finally:
        if store and not manifest_saved:
            try:
                directory = cfg.state_dir / "temp"
                directory.mkdir(parents=True, exist_ok=True)
                store.save(directory)
            except Exception as exc:
                summary.errors.append({"resource": "Manifest", "error": str(exc)})
        if sftp:
            sftp.close()
        if client:
            client.close()
        report(summary, cfg)
        if lock:
            lock.__exit__(None, None, None)
    return summary, (1 if summary.errors else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=APP_DIR / ".env")
    parser.add_argument("--check-config", action="store_true", help="Valider le fichier .env sans connexion réseau")
    parser.add_argument("--local-only", action="store_true", help="Synchroniser uniquement les cours conservés sur le PC, sans Moodle")
    parser.add_argument("--moodle", action="store_true", help="Relancer la découverte Moodle même si LOCAL_ONLY=true")
    parser.add_argument("--course", "-c", action="append", dest="courses", default=[],
                        help="Matière(s) à synchroniser (nom, numéro ou ID Moodle). Répétable.")
    parser.add_argument("--all", "-a", action="store_true", help="Synchroniser toutes les matières sans confirmation")
    parser.add_argument("selected_courses", nargs="*",
                        help="Nom(s) ou numéro(s) de matière(s) à synchroniser (ex: Automatique ou 3)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = Config.load(args.env)
    except (ValueError, OSError) as exc:
        print(f"Configuration : {exc}")
        return 2
    if args.check_config:
        dest = cfg.remote_dir if cfg.local_storage else f"{cfg.user}@{cfg.host}:{cfg.port}:{cfg.remote_dir}"
        print(f"Configuration valide : destination {dest}")
        print(f"Connexion Junia : {'email configuré' if cfg.email else 'manuelle dans le navigateur'}")
        return 0

    targets = (args.courses or []) + (args.selected_courses or [])
    if targets:
        target_courses = resolve_course_selection(COURSES, targets)
    elif not args.all and sys.stdin.isatty():
        target_courses = prompt_course_selection(COURSES)
    else:
        target_courses = COURSES

    configure_logging(cfg)
    LOG.info("Matière(s) ciblée(s) (%d/%d) : %s", len(target_courses), len(COURSES), ", ".join(c[1] for c in target_courses))
    summary, exit_code = run_sync(cfg, target_courses=target_courses, local_only=args.local_only, force_moodle=args.moodle)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
