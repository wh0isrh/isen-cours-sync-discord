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
from collections import deque
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
    (19333, "Automatique"), (19337, "Mécanique Quantique"),
    (19336, "Electronique Numérique"), (19341, "Language interprété"),
    (19340, "Introduction à l'Intelligence artificielle"),
    (19338, "Base de Données"), (19343, "Projet Professionnel"),
    (19342, "Anglais"), (19345, "Enjeu des transitions"),
    (19344, "Décryptage de l'information"),
    (20846, "Valorisation de l'Engagement Sociétal"),
]


class AuthenticationExpired(RuntimeError):
    pass


def clean_name(value: str, filename: bool = False) -> str:
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

    @classmethod
    def load(cls, env_path: Path):
        if not env_path.is_file():
            shutil.copyfile(APP_DIR / ".env.example", env_path)
            raise ValueError(f"Configuration créée : {env_path}. Renseigner Ubuntu puis relancer.")
        values = {**dotenv_values(env_path), **os.environ}
        values = {k: (v or "") for k, v in values.items()}
        root = values.get("UBUNTU_REMOTE_DIR", "").strip()
        if not root.startswith("/") or root == "/" or ".." in PurePosixPath(root).parts:
            raise ValueError("UBUNTU_REMOTE_DIR doit être un dossier absolu Ubuntu, différent de /.")
        host, user = values.get("UBUNTU_HOST", "").strip(), values.get("UBUNTU_USER", "").strip()
        if not host or not user:
            raise ValueError("Renseigner UBUNTU_HOST et UBUNTU_USER dans .env")
        key = values.get("UBUNTU_SSH_KEY_PATH", "").strip()
        if key:
            key = str(Path(os.path.expandvars(key)).expanduser())
            if not Path(key).is_absolute():
                key = str(APP_DIR / key)
            if not Path(key).is_file():
                raise ValueError("UBUNTU_SSH_KEY_PATH : fichier de clé privée introuvable")
        audit = Path(os.path.expandvars(values.get("AUDIT_DIR", "") or str(APP_DIR.parent / "ISEN_Lille_2026-2027"))).expanduser()
        if not audit.is_absolute():
            audit = APP_DIR / audit
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


def microsoft_login(context, cfg):
    page = context.pages[0] if context.pages else context.new_page()
    page.goto(MOODLE + "/my/", wait_until="domcontentloaded", timeout=60000)
    if is_moodle_logged_in(page):
        LOG.info("Session Junia déjà ouverte.")
        return page
    LOG.info("Connexion Junia dans Chromium. Vous pouvez intervenir dans la fenêtre.")
    deadline = time.monotonic() + cfg.login_timeout
    sent_email = sent_password = False
    announced = False
    countdown_until = None
    last_second = None
    clicked_sso = False
    while time.monotonic() < deadline:
        # Certains portails ouvrent la connexion Microsoft dans une seconde page.
        for candidate in context.pages:
            if is_moodle_logged_in(candidate):
                LOG.info("Authentification Junia confirmée.")
                return candidate
        active = next((p for p in reversed(context.pages) if not p.is_closed()), page)
        host = urlsplit(active.url).hostname or ""
        if host in {"login.microsoftonline.com", "login.live.com", "login.windows.net"}:
            tile = active.locator(f"[data-test-id*='{cfg.email}'], [role='button']:has-text('{cfg.email}')").filter(has_text=cfg.email)
            if not sent_email and tile.count() and tile.first.is_visible():
                tile.first.click()
                sent_email = True
            elif cfg.email and not sent_email and fill_visible(active, "input[type=email], input[name=loginfmt]", cfg.email):
                active.locator("input[type=email], input[name=loginfmt]").first.press("Enter")
                sent_email = True
            elif cfg.password and not sent_password and fill_visible(active, "input[name=passwd], input[type=password]", cfg.password):
                active.locator("input[name=passwd], input[type=password]").first.press("Enter")
                sent_password = True
                countdown_until = time.monotonic() + cfg.wait_2fa
            # Valider automatiquement « Rester connecté ? » si présent
            kmsi = active.locator("input#idSIButton9, input[type=submit][value='Oui'], button:has-text('Oui')")
            if kmsi.count() and kmsi.first.is_visible() and active.get_by_text(re.compile("Rester connect|Stay signed", re.I)).count():
                check = active.locator("input#KmsiCheckboxField, input[name='DontShowAgain']")
                if check.count() and check.first.is_visible() and not check.first.is_checked():
                    check.first.check()
                kmsi.first.click()
            # La notification peut arriver après la saisie manuelle du mot de passe.
            if not announced and (sent_password or active.get_by_text(re.compile("Authenticator|approuv|approve|vérifi.*identité", re.I)).count()):
                announced = True
                countdown_until = countdown_until or time.monotonic() + cfg.wait_2fa
                LOG.info("Validez l'A2F Microsoft sur votre smartphone (le numéro est dans le navigateur).")
        elif host == "junia-learning.com" and not clicked_sso:
            # Lien vérifié sur la page publique Junia : « J'ai une adresse mail Junia ».
            sso = active.locator("a[href*='/auth/oidc/']")
            if not sso.count():
                sso = active.locator("a").filter(has_text=re.compile("Microsoft|Office.?365|compte.*JUNIA|Connexion.*JUNIA|adresse.*mail.*JUNIA", re.I))
            if sso.count() and sso.first.is_visible():
                sso.first.click(timeout=10000)
                clicked_sso = True
        if countdown_until:
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
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        # Python 3.10/3.11 convertissent certains HEAD redirigés en GET.
        # Conserver HEAD pour vérifier une ressource sans télécharger son contenu.
        if redirected is not None and request.get_method() == "HEAD":
            redirected.method = "HEAD"
        return redirected


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


def section_of(element, default):
    for parent in element.parents:
        identifier = parent.get("id", "")
        if re.fullmatch(r"section-\d+", identifier):
            if identifier == "section-0":
                return "Général"
            heading = parent.select_one("[id^='coursecontentsection'], .sectionname")
            return heading.get_text(" ", strip=True) if heading else default
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
            result.append(("section", Resource(course, node.get_text(" ", strip=True) or section, link, "")))
    text = main.get_text("\n", strip=True)
    blocked = "Ce cours n’est actuellement pas disponible pour les étudiants" in text or "Ce cours n'est actuellement pas disponible pour les étudiants" in text
    enrolled = bool(soup.select_one("#page-enrol-index, body#page-enrol-index")) or "/enrol/" in urlsplit(url).path
    if enrolled:
        blocked = True
    return result, text, blocked


class RemoteStore:
    def __init__(self, sftp, cfg):
        self.sftp, self.cfg = sftp, cfg
        self.root = cfg.remote_dir
        self.ledger_path = posixpath.join(self.root, ".sync_moodle_manifest.json")
        self.records = {}
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
        current = "/"
        for part in PurePosixPath(directory).parts[1:]:
            current = posixpath.join(current, part)
            attrs = self.attributes(current)
            if attrs is None:
                self.sftp.mkdir(current)
            elif not stat.S_ISDIR(attrs.st_mode):
                raise IOError(f"Le chemin distant n'est pas un répertoire : {current}")

    def digest(self, path):
        with self.sftp.open(path, "rb") as stream:
            return sha_stream(stream)

    def matches(self, relative, size, digest=None):
        attrs = self.attributes(self.remote(relative))
        if not attrs or not stat.S_ISREG(attrs.st_mode) or attrs.st_size != size:
            return False
        return not (self.cfg.verify_hash and digest) or self.digest(self.remote(relative)) == digest

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
        except BaseException:
            try:
                self.sftp.remove(temporary)
            except OSError:
                pass
            raise

    def remember(self, relative, size, digest, url="", meta=None):
        record = self.records.setdefault(relative, {"sources": {}})
        record.update(size=size, sha256=digest)
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


def sync_local(store, cfg, summary):
    """Envoyer exclusivement les fichiers encore présents, sans accès Moodle."""
    root = cfg.audit_dir.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Dossier de cours absent : {root}")
    for local in sorted(root.rglob("*")):
        if local.is_symlink():
            raise ValueError(f"Lien symbolique refusé : {local}")
        if not local.is_file():
            continue
        relative = safe_relative(local.relative_to(root).as_posix())
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
            course = relative.split("/")[0]
            if course != "_Inventaire":
                summary.courses[course] = "fichiers locaux synchronisés"
        except Exception as exc:
            summary.errors.append({"resource": relative, "error": str(exc)})
            LOG.error("%s : %s", relative, exc)


class Synchronizer:
    def __init__(self, http, store, cfg, summary):
        self.http, self.store, self.cfg, self.summary = http, store, cfg, summary
        self.temp = cfg.state_dir / "temp"
        self.temp.mkdir(parents=True, exist_ok=True)
        self.links = {name: [] for _, name in COURSES}
        self.interactive = {name: [] for _, name in COURSES}
        self.seen_files = set()

    def error(self, label, exc):
        LOG.error("%s : %s", label, exc)
        self.summary.errors.append({"resource": label, "error": str(exc)})

    def bootstrap_audit(self):
        seed = json.loads((APP_DIR / "audited_files.json").read_text(encoding="utf-8"))
        if not self.cfg.bootstrap or not self.cfg.audit_dir.is_dir():
            return
        LOG.info("Amorçage depuis l'audit local : seuls les fichiers distants absents sont envoyés.")
        for entry in seed:
            relative = safe_relative(entry["relative_path"])
            local = self.cfg.audit_dir / Path(*PurePosixPath(relative).parts)
            if not local.is_file():
                continue
            try:
                attrs = self.store.attributes(self.store.remote(relative))
                if attrs:
                    # Un audit ancien ne doit jamais remplacer une version plus récente.
                    if relative in self.store.records and self.store.matches(relative, self.store.records[relative]["size"], self.store.records[relative].get("sha256")):
                        self.summary.mark(relative)
                    elif attrs.st_size == entry["size"] and self.store.matches(relative, entry["size"], entry["sha256"]):
                        self.store.remember(relative, entry["size"], entry["sha256"])
                        self.summary.mark(relative)
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
        self.store.save(self.temp)

    def file(self, resource):
        identity = canonical_url(resource.url)
        seen_key = (resource.course, resource.section, identity)
        if seen_key in self.seen_files:
            return
        self.seen_files.add(seen_key)
        meta = self.http.head(resource.url)
        relative = resource.prefix + meta.name
        if relative in self.store.records or self.store.attributes(self.store.remote(relative)) is not None:
            self.summary.mark(relative)
            LOG.info("Ignoré (déjà présent) : %s", relative)
            return
        record = None
        for candidate, saved in self.store.records.items():
            if candidate.startswith(resource.prefix) and identity in saved.get("sources", {}):
                relative, record = candidate, saved
                break
        audited = self.store.records.get(relative)
        if not record and audited and not audited.get("sources") and meta.size is not None and self.store.matches(relative, meta.size, audited.get("sha256")):
            # L'audit fournit déjà une empreinte connue : adoption du lien courant
            # après vérification distante, sans nouveau téléchargement HTTP.
            self.store.remember(relative, meta.size, audited.get("sha256"), resource.url, meta)
            self.summary.mark(relative)
            LOG.info("Ignoré (audit déjà présent et vérifié) : %s", relative)
            return
        if record and meta.size is not None:
            before = record["sources"][identity]
            validator_changed = any(current and current != previous for current, previous in ((meta.etag, before.get("etag")), (meta.modified, before.get("modified"))))
            has_validator = bool(meta.etag or meta.modified)
            if not validator_changed and (has_validator or not self.cfg.strict_source) and self.store.matches(relative, meta.size, record.get("sha256")):
                self.summary.mark(relative)
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
            identical = next((p for p, row in self.store.records.items() if p.startswith(resource.prefix) and row.get("sha256") == digest), None)
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
        temporary.unlink()  # Un échec garde le fichier local pour diagnostic/reprise.

    def note(self, relative, text):
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
        pending = deque([(MOODLE + f"/course/view.php?id={course_id}", "Général", "course", "")])
        visited = set()
        self.summary.courses[name] = "en cours"
        while pending:
            url, section, kind, title = pending.popleft()
            if url in visited:
                continue
            visited.add(url)
            try:
                if kind == "module" and "/mod/resource/" in url:
                    try:
                        with self.http.open(url, "HEAD") as probe:
                            probe_meta = Metadata.from_headers(probe.headers, probe.geturl())
                            if "text/html" not in probe_meta.content_type or probe.headers.get("Content-Disposition"):
                                self.file(Resource(name, section, probe.geturl(), title))
                                continue
                    except HTTPError as exc:
                        if exc.code not in {405, 501}:
                            raise
                with self.http.open(url) as response:
                    final_url = response.geturl()
                    meta = Metadata.from_headers(response.headers, final_url)
                    if "text/html" not in meta.content_type or response.headers.get("Content-Disposition"):
                        self.file(Resource(name, section, final_url, title))
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
        self.store.save(self.temp)

    def write_links(self, name):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=APP_DIR / ".env")
    parser.add_argument("--check-config", action="store_true", help="Valider le fichier .env sans connexion réseau")
    parser.add_argument("--local-only", action="store_true", help="Synchroniser uniquement les cours conservés sur le PC, sans Moodle")
    parser.add_argument("--moodle", action="store_true", help="Relancer la découverte Moodle même si LOCAL_ONLY=true")
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
    configure_logging(cfg)
    summary, client, sftp, store, context, lock = Summary(), None, None, None, None, None
    try:
        candidate = RunLock(cfg.state_dir / "run.lock")
        candidate.__enter__()
        lock = candidate
        LOG.info("Connexion SFTP au démarrage : %s@%s:%s", cfg.user, cfg.host, cfg.port)
        client, sftp = ssh_connect(cfg)
        store = RemoteStore(sftp, cfg)
        if args.local_only or (cfg.local_only and not args.moodle):
            LOG.info("Mode local : aucune ressource supprimée ne sera téléchargée à nouveau.")
            sync_local(store, cfg, summary)
        else:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(cfg.state_dir / "browser-profile"), headless=False,
                    accept_downloads=True, viewport={"width": 1280, "height": 850},
                )
                try:
                    microsoft_login(context, cfg)
                    synchronizer = Synchronizer(MoodleHTTP(context, cfg), store, cfg, summary)
                    synchronizer.bootstrap_audit()
                    for number, (course_id, name) in enumerate(COURSES, 1):
                        LOG.info("Progression : cours %d/%d", number, len(COURSES))
                        synchronizer.course(course_id, name)
                    store.save(synchronizer.temp)
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
        if store:
            try:
                directory = cfg.state_dir / "temp"
                directory.mkdir(parents=True, exist_ok=True)
                store.save(directory)
            except Exception as exc:
                summary.errors.append({"resource": "Manifest distant", "error": str(exc)})
        if sftp:
            sftp.close()
        if client:
            client.close()
        report(summary, cfg)
        if lock:
            lock.__exit__(None, None, None)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
