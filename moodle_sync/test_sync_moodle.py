"""Tests locaux : aucun contact avec Junia ou le serveur Ubuntu de l'utilisateur."""
import errno
import hashlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

import paramiko
from playwright.sync_api import sync_playwright
import sync_moodle as app


def config(root):
    return SimpleNamespace(remote_dir="/home/test/cours", verify_hash=True, strict_source=False,
                           state_dir=root / "state", audit_dir=root / "audit", bootstrap=False,
                           http_timeout=5, http_retries=1)


class DiskSFTP:
    """Double local pour injecter une coupure précise entre deux renommages."""
    def __init__(self, root):
        self.root = root
        self.fail_next_promotion = False

    def path(self, name):
        return self.root / name.lstrip("/")

    def lstat(self, name):
        return self.path(name).lstat()

    def mkdir(self, name):
        self.path(name).mkdir()

    def open(self, name, mode):
        return self.path(name).open(mode)

    def put(self, source, target, confirm=True):
        self.path(target).write_bytes(Path(source).read_bytes())

    def posix_rename(self, source, target):
        raise OSError(errno.EOPNOTSUPP, "Operation unsupported")

    def rename(self, source, target):
        if self.fail_next_promotion and ".part-" in source:
            self.fail_next_promotion = False
            raise OSError(errno.EIO, "Coupure injectée")
        self.path(source).rename(self.path(target))

    def remove(self, name):
        self.path(name).unlink()


class StubHTTP:
    def __init__(self):
        self.body = b"%PDF-original"
        self.version = "v1"
        self.download_count = 0

    def head(self, url):
        return app.Metadata(len(self.body), self.version, "", "application/pdf", "cours.pdf")

    def download(self, url, target):
        self.download_count += 1
        target.write_bytes(self.body)
        return len(self.body), hashlib.sha256(self.body).hexdigest(), self.head(url)


class LogicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = config(self.root)
        self.sftp = DiskSFTP(self.root / "ubuntu")
        self.sftp.root.mkdir()
        self.store = app.RemoteStore(self.sftp, self.cfg)

    def tearDown(self):
        self.temp.cleanup()

    def test_local_sync_respects_retained_files_and_detects_changed_content(self):
        local = self.cfg.audit_dir / 'Automatique' / 'Chapitre 1' / 'cours.pdf'
        local.parent.mkdir(parents=True)
        local.write_bytes(b'AAA')
        first = app.Summary()
        app.sync_local(self.store, self.cfg, first)
        relative = 'Automatique/Chapitre 1/cours.pdf'
        self.assertEqual(first.sent, {relative})
        second = app.Summary()
        app.sync_local(self.store, self.cfg, second)
        self.assertEqual(second.skipped, {relative})
        local.write_bytes(b'BBB')
        changed = app.Summary()
        app.sync_local(self.store, self.cfg, changed)
        self.assertEqual(changed.sent, {relative})
        self.assertEqual(self.sftp.path(self.store.remote(relative)).read_bytes(), b'BBB')
        local.unlink()
        deleted = app.Summary()
        app.sync_local(self.store, self.cfg, deleted)
        self.assertFalse(deleted.sent)
        self.assertFalse(local.exists())
        self.assertFalse(deleted.errors)

    def test_wol_packet_and_wait_until_ssh_available(self):
        cfg = SimpleNamespace(wol_mac='00:24:81:36:96:36', host='192.168.1.18',
                              port=22, wol_broadcast='192.168.1.255', wol_wait=5)
        with patch.object(app.socket, 'create_connection', side_effect=[OSError('off'), unittest.mock.MagicMock()]), patch.object(app.socket, 'socket') as sock:
            app.wake_server(cfg)
            packet = b'\xff' * 6 + bytes.fromhex('002481369636') * 16
            udp = sock.return_value.__enter__.return_value
            self.assertEqual(udp.sendto.call_args_list, [unittest.mock.call(packet, ('192.168.1.255', 7)), unittest.mock.call(packet, ('192.168.1.255', 9))])

    def test_hidden_links_sections_and_external_video(self):
        html = '''<div id="region-main"><li id="section-0"><h3>Bienvenue au cours</h3>
        <a href="/mod/resource/view.php?id=1">Général</a></li><li id="section-2">
        <h3 id="coursecontentsection2">Chapitre 2</h3><div hidden>
        <a href="/pluginfile.php/5/mod_resource/content/1/test.pdf">PDF masqué</a>
        <iframe src="https://www.youtube.com/embed/abc" title="Explication"></iframe>
        </div></li><a href="/course/section.php?id=44">Semaine 4</a></div>'''
        found, _, blocked = app.parse_links(html, app.MOODLE + "/course/view.php?id=3", "Cours", "Général")
        self.assertFalse(blocked)
        self.assertIn(("file", "Chapitre 2"), [(k, r.section) for k, r in found])
        self.assertEqual(next(r for k, r in found if r.title == "Général").section, "Général")
        self.assertEqual(next(r for k, r in found if k == "external").title, "Explication")

    def test_unknown_section_index_has_own_title(self):
        found, _, _ = app.parse_links('<div id="region-main"></div><a href="/course/section.php?id=2">TD 1</a>', app.MOODLE + "/course/view.php?id=1", "Cours", "Général")
        self.assertEqual(found[0][1].section, "TD 1")

    def test_remote_same_size_corruption_detected(self):
        local = self.root / "support.pdf"
        local.write_bytes(b"%PDF-1234")
        digest = app.sha_file(local)
        self.store.upload(local, "Cours/S1/support.pdf", local.stat().st_size, digest)
        self.assertTrue(self.store.matches("Cours/S1/support.pdf", 9, digest))
        self.sftp.path(self.store.remote("Cours/S1/support.pdf")).write_bytes(b"%PDF-5678")
        self.assertFalse(self.store.matches("Cours/S1/support.pdf", 9, digest))

    def test_legacy_rename_rolls_back_on_failure(self):
        local = self.root / "support.pdf"
        local.write_bytes(b"ancienne version")
        relative = "Cours/S1/support.pdf"
        self.store.upload(local, relative, local.stat().st_size, app.sha_file(local))
        local.write_bytes(b"nouvelle version")
        self.sftp.fail_next_promotion = True
        with self.assertRaises(OSError):
            self.store.upload(local, relative, local.stat().st_size, app.sha_file(local))
        self.assertEqual(self.sftp.path(self.store.remote(relative)).read_bytes(), b"ancienne version")
        self.assertFalse(list(self.sftp.root.rglob("*.part-*")))

    def test_fast_check_persists_and_rechecks_changed_remote(self):
        self.cfg.fast_remote_check = True
        local = self.root / "support.pdf"
        local.write_bytes(b"original")
        relative, digest = "Cours/S1/support.pdf", app.sha_file(local)
        self.store.upload(local, relative, 8, digest)
        self.store.remember(relative, 8, digest)
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.store.save(self.cfg.state_dir)
        reloaded = app.RemoteStore(self.sftp, self.cfg)
        with patch.object(reloaded, "digest", wraps=reloaded.digest) as read:
            self.assertTrue(reloaded.matches(relative, 8, digest))
            read.assert_not_called()
            remote = self.sftp.path(reloaded.remote(relative))
            old_mtime = remote.stat().st_mtime
            remote.write_bytes(b"modified")
            os.utime(remote, (old_mtime + 2, old_mtime + 2))
            self.assertFalse(reloaded.matches(relative, 8, digest))
            self.assertEqual(read.call_count, 1)

    def test_strict_check_reads_even_with_saved_fingerprint(self):
        self.cfg.fast_remote_check = False
        local = self.root / "support.pdf"
        local.write_bytes(b"original")
        relative, digest = "Cours/S1/support.pdf", app.sha_file(local)
        self.store.upload(local, relative, 8, digest)
        self.store.remember(relative, 8, digest)
        with patch.object(self.store, "digest", wraps=self.store.digest) as read:
            self.assertTrue(self.store.matches(relative, 8, digest))
            read.assert_called_once()

    def test_first_download_second_skip_then_same_size_update(self):
        http = StubHTTP()
        r = app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/cours.pdf", "Cours")
        first = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        first.file(r)
        self.assertEqual(len(first.summary.sent), 1)
        self.assertFalse(list(first.temp.glob("*.download")))
        second = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        second.file(r)
        self.assertEqual(http.download_count, 1)
        self.assertEqual(len(second.summary.skipped), 1)
        http.body, http.version = b"%PDF-modified", "v2"
        third = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        third.file(r)
        self.assertEqual(http.download_count, 2)
        self.assertEqual(self.sftp.path(self.store.remote("Cours/S1/cours.pdf")).read_bytes(), http.body)

    def test_verified_audit_is_adopted_without_http_download(self):
        http = StubHTTP()
        local = self.root / "cours.pdf"
        local.write_bytes(http.body)
        relative = "Cours/S1/cours.pdf"
        self.store.upload(local, relative, len(http.body), app.sha_file(local))
        self.store.remember(relative, len(http.body), app.sha_file(local))
        sync = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        sync.file(app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A"))
        self.assertEqual(http.download_count, 0)
        self.assertEqual(len(sync.summary.skipped), 1)

    def test_same_filename_two_contents_preserved(self):
        http = StubHTTP()
        sync = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        sync.file(app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A"))
        http.body, http.version = b"%PDF-distinct", "v2"
        sync.file(app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/2/b.pdf", "B"))
        self.assertEqual(len(sync.summary.sent), 2)
        self.assertEqual(len(list(self.sftp.path(self.store.remote("Cours/S1")).glob("*.pdf"))), 2)

    def test_identical_aliases_diverge_without_destroying_original(self):
        http = StubHTTP()
        a = app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A")
        b = app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/2/b.pdf", "B")
        first = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        first.file(a)
        first.file(b)
        self.assertEqual(len(self.store.records), 1)
        http.body, http.version = b"%PDF-diverged", "v2"
        next_run = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        next_run.file(a)
        self.assertEqual(len(self.store.records), 2)
        self.assertEqual(self.sftp.path(self.store.remote("Cours/S1/cours.pdf")).read_bytes(), b"%PDF-original")

    def test_shared_resource_kept_in_both_courses(self):
        http = StubHTTP()
        sync = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        for name in ("Cours A", "Cours B"):
            sync.file(app.Resource(name, "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A"))
        self.assertEqual(len(sync.summary.sent), 2)

    def test_failed_upload_keeps_local_temporary(self):
        http = StubHTTP()
        sync = app.Synchronizer(http, self.store, self.cfg, app.Summary())
        with patch.object(self.store, "upload", side_effect=OSError("serveur hors ligne")):
            with self.assertRaises(OSError):
                sync.file(app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A"))
        self.assertEqual(len(list(sync.temp.glob("*.download"))), 1)

    def test_source_without_validator_can_be_strict(self):
        http = StubHTTP()
        http.version = ""
        r = app.Resource("Cours", "S1", app.MOODLE + "/pluginfile.php/1/a.pdf", "A")
        app.Synchronizer(http, self.store, self.cfg, app.Summary()).file(r)
        self.cfg.strict_source = True
        app.Synchronizer(http, self.store, self.cfg, app.Summary()).file(r)
        self.assertEqual(http.download_count, 2)

    def test_path_traversal_refused(self):
        for bad in ("../secret", "/etc/passwd", "Cours/../secret", "Cours\\secret"):
            with self.assertRaises(ValueError):
                self.store.remote(bad)

    def test_audit_bootstrap_does_not_revert_newer_remote_file(self):
        local = self.cfg.audit_dir / "Cours" / "S1" / "cours.pdf"
        local.parent.mkdir(parents=True)
        local.write_bytes(b"ancienne")
        newer = self.root / "new.pdf"
        newer.write_bytes(b"nouvelle")
        self.store.upload(newer, "Cours/S1/cours.pdf", 8, app.sha_file(newer))
        self.store.remember("Cours/S1/cours.pdf", 8, app.sha_file(newer))
        seed = self.root / "audited_files.json"
        seed.write_text(json.dumps([{"relative_path":"Cours/S1/cours.pdf", "size":8, "sha256":app.sha_file(local)}]))
        self.cfg.bootstrap = True
        with patch.object(app, "APP_DIR", self.root), patch.object(self.store, "digest", wraps=self.store.digest) as read:
            app.Synchronizer(StubHTTP(), self.store, self.cfg, app.Summary()).bootstrap_audit()
            self.assertEqual(read.call_count, 1)
            self.assertIn(".sync_moodle_manifest.json.part-", read.call_args.args[0])
        self.assertEqual(self.sftp.path(self.store.remote("Cours/S1/cours.pdf")).read_bytes(), b"nouvelle")

    def test_env_is_independent_of_current_directory(self):
        env = self.root / "settings.env"
        env.write_text("UBUNTU_HOST=192.168.11.1\nUBUNTU_USER=meowalex\nUBUNTU_REMOTE_DIR=/home/meowalex/cours\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            cfg = app.Config.load(env)
        self.assertEqual(cfg.wait_2fa, 15)
        self.assertEqual(cfg.port, 22)

    def test_report_redacts_password_with_json_special_characters(self):
        self.cfg.password = 'fictitious"password\\with-specials'
        self.cfg.ssh_password = "fixture-ssh-secret"
        self.cfg.key_passphrase = "fixture-key-secret"
        self.cfg.user, self.cfg.host = "fixture", "127.0.0.1"
        self.cfg.state_dir.mkdir(parents=True)
        summary = app.Summary()
        summary.errors.append({"resource":"fixture", "error":"error containing " + self.cfg.password})
        with redirect_stdout(io.StringIO()):
            app.report(summary, self.cfg)
        saved = json.loads((self.cfg.state_dir / "dernier_rapport.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["errors"][0]["error"], "error containing [masqué]")

    def test_resolve_course_selection_by_index_id_and_fuzzy_name(self):
        # Index 1-based (3 -> Automatique)
        self.assertEqual(app.resolve_course_selection(app.COURSES, ["3"]), [(19333, "Automatique")])
        # ID Moodle direct (19333)
        self.assertEqual(app.resolve_course_selection(app.COURSES, ["19333"]), [(19333, "Automatique")])
        # Filtre textuel insensible à la casse et aux accents
        self.assertEqual(app.resolve_course_selection(app.COURSES, ["mecanique"]), [(19337, "Mécanique Quantique")])
        # Sélections multiples dédupliquées
        chosen = app.resolve_course_selection(app.COURSES, ["3", "auto", "5"])
        self.assertEqual(chosen, [(19333, "Automatique"), (19336, "Electronique Numérique")])
        # Cible vide -> retourne l'ensemble
        self.assertEqual(app.resolve_course_selection(app.COURSES, []), app.COURSES)

    def test_mfa_code_banner_and_formatting(self):
        banner = app.format_mfa_banner("42")
        self.assertIn("CODE MICROSOFT AUTHENTICATOR (A2F)", banner)
        self.assertIn("[  4 2  ]", banner)
        banner.encode("ascii")

    def test_local_store_upload_matches_and_save(self):
        store_root = self.root / "local_store"
        store = app.LocalStore(store_root, self.cfg)
        local_file = self.root / "test.pdf"
        local_file.write_bytes(b"%PDF-test-local-store")
        digest = app.sha_file(local_file)
        relative = "Automatique/TD1/test.pdf"
        self.assertFalse(store.matches(relative, len(b"%PDF-test-local-store"), digest))
        store.upload(local_file, relative, len(b"%PDF-test-local-store"), digest)
        self.assertTrue(store.matches(relative, len(b"%PDF-test-local-store"), digest))
        self.assertTrue((store_root / relative).is_file())
        store.save()
        reloaded = app.LocalStore(store_root, self.cfg)
        self.assertIn(relative, reloaded.records)
        self.assertEqual(reloaded.records[relative]["sha256"], digest)

    def test_summary_concurrency(self):
        summary = app.Summary()
        def worker(idx):
            for i in range(50):
                summary.mark(f"course_{idx}/file_{i}.pdf", sent=(i % 2 == 0))
                summary.add_bytes(10)
                summary.set_course_status(f"course_{idx}", "ok")
                summary.add_unavailable(f"unavail_{idx}_{i}")
                summary.add_error(f"err_{idx}", "some error")
                summary.add_external_links(1)
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(summary.bytes_sent, 2000)
        self.assertEqual(summary.external_links, 200)
        self.assertEqual(len(summary.errors), 200)
        self.assertEqual(len(summary.unavailable), 200)
        self.assertEqual(len(summary.sent), 100)

    def test_gzip_response_wrapper(self):
        import gzip
        payload = b"Hello from Junia compressed stream!"
        compressed = gzip.compress(payload)
        
        class FakeResp(io.BytesIO):
            def close(self): pass
        
        wrapper = app.GzipResponseWrapper(FakeResp(compressed))
        self.assertEqual(wrapper.read(), payload)

    def test_local_store_fast_remote_check(self):
        local_dir = self.root / "fast_check_courses"
        local_dir.mkdir()
        store = app.LocalStore(local_dir, self.cfg)
        test_file = local_dir / "test.txt"
        content = b"manifest test content"
        test_file.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        store.remember("test.txt", len(content), digest)
        store.save()
        self.assertTrue(store.matches("test.txt", len(content), digest))

    def test_save_and_load_saved_session(self):
        cookies = [{"name": "MoodleSession", "value": "xyz123", "domain": "junia-learning.com", "path": "/"}]
        app.save_session_cookies(cookies, self.cfg)
        cookie_file = self.cfg.state_dir / "session_cookies.json"
        self.assertTrue(cookie_file.is_file())
        with patch.object(app.MoodleHTTP, "open") as mock_open:
            class MockResp:
                def geturl(self): return app.MOODLE + "/my/"
                def read(self, *a): return b"Mes cours"
                def __enter__(self): return self
                def __exit__(self, *a): pass
            mock_open.return_value = MockResp()
            loaded = app.load_saved_session(self.cfg)
            self.assertEqual(loaded, cookies)




class FixtureHTTP(BaseHTTPRequestHandler):
    gets = 0
    visits = []
    payload = b"%PDF-" + b"test-data-" * 200000

    def log_message(self, *args):
        pass

    def handle_request(self, body):
        type(self).visits.append((self.command, self.path))
        if self.headers.get("Cookie") != "MoodleSession=fixture":
            self.send_response(302)
            self.send_header("Location", "/login/index.php")
            self.end_headers()
            return
        if self.path == "/external":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/external")
            self.end_headers()
            return
        if self.path == "/mod/resource/view.php?id=6001":
            self.send_response(302)
            self.send_header("Location", "/pluginfile.php/1/cours.pdf")
            self.end_headers()
            return
        if self.path == "/mod/url/view.php?id=6004":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/resource")
            self.end_headers()
            return
        html_pages = {
            "/course/view.php?id=501": '<div id="region-main"><a href="/course/section.php?id=5001">TD1</a></div>',
            "/course/view.php?id=502": '<div id="region-main">Ce cours n’est actuellement pas disponible pour les étudiants</div>',
            "/course/section.php?id=5001": '''<div id="region-main">
              <a href="/mod/resource/view.php?id=6001">Support</a>
              <a href="/mod/folder/view.php?id=6002">Dossier</a>
              <a href="/mod/page/view.php?id=6003">Consignes</a>
              <a href="/mod/url/view.php?id=6004">Teams</a>
              <a href="/mod/quiz/view.php?id=6005">Quiz</a>
              <iframe src="https://www.youtube.com/embed/fixture" title="Explication"></iframe>
            </div>''',
            "/mod/folder/view.php?id=6002": '<div id="region-main"><div hidden><a href="/pluginfile.php/2/cours.pdf">PDF du dossier</a></div></div>',
            "/mod/page/view.php?id=6003": '<div id="region-main">Lire le cours puis préparer le TD.</div>',
        }
        if self.path in html_pages:
            payload = html_pages[self.path].encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(self.payload)))
        self.send_header("ETag", '"fixture-v1"')
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote("Cours français.pdf"))
        self.end_headers()
        if body:
            type(self).gets += 1
            self.wfile.write(self.payload)

    def do_HEAD(self):
        self.handle_request(False)

    def do_GET(self):
        self.handle_request(True)


class LocalSFTPServer(paramiko.SFTPServerInterface):
    """SFTP réel local, volontairement sans extension posix-rename."""
    def __init__(self, server, root):
        super().__init__(server)
        self.root = Path(root)

    def path(self, value):
        result = (self.root / value.lstrip("/")).resolve()
        if not result.is_relative_to(self.root.resolve()):
            raise OSError(errno.EACCES, "outside fixture")
        return result

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def mkdir(self, path, attr):
        try:
            self.path(path).mkdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        try:
            fd = os.open(self.path(path), flags | getattr(os, "O_BINARY", 0), 0o600)
            mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
            file = os.fdopen(fd, mode)
            handle = paramiko.SFTPHandle(flags)
            if not flags & os.O_WRONLY:
                handle.readfile = file
            if flags & (os.O_WRONLY | os.O_RDWR):
                handle.writefile = file
            handle.stat = lambda: paramiko.SFTPAttributes.from_stat(os.fstat(file.fileno()))
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def remove(self, path):
        try:
            self.path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def rename(self, old, new):
        try:
            if self.path(new).exists():
                return paramiko.SFTP_FAILURE
            self.path(old).rename(self.path(new))
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class LocalSSHServer(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if username == "fixture" and password == "fixture-password" else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class IntegrationTests(unittest.TestCase):
    def test_junia_oidc_email_password_and_wait_for_authenticated_return(self):
        # Toutes les requêtes sont interceptées : aucun accès réel aux domaines
        # Junia/Microsoft et uniquement des identifiants fictifs.
        posts = {}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()

            def fixture(route):
                url = route.request.url
                if url == app.MOODLE + "/my/":
                    route.fulfill(content_type="text/html", body='''<a href="/auth/oidc/?source=loginpage">J'ai une adresse mail Junia</a>
                    <a href="#local">Je n'ai pas d'adresse mail Junia</a>''')
                elif "/auth/oidc/" in url:
                    # Navigation JS : les redirections HTTP des fixtures ne sont
                    # pas toutes réinterceptées par Playwright dans une même chaîne.
                    route.fulfill(content_type="text/html", body='<script>location.href="https://login.microsoftonline.com/fixture/email"</script>')
                elif url.endswith("/fixture/email"):
                    route.fulfill(content_type="text/html", body='<form method="post" action="/fixture/password"><input name="loginfmt" type="email"><button>Next</button></form>')
                elif url.endswith("/fixture/password"):
                    posts["email"] = route.request.post_data
                    route.fulfill(content_type="text/html", body='<form method="post" action="/fixture/approval"><input name="passwd" type="password"><button>Sign in</button></form>')
                elif url.endswith("/fixture/approval"):
                    posts["password"] = route.request.post_data
                    route.fulfill(content_type="text/html", body='<p>Valider la notification Authenticator</p><script>setTimeout(() => location.href="https://junia-learning.com/my/fixture-authenticated", 1200)</script>')
                elif url.endswith("/my/fixture-authenticated"):
                    route.fulfill(content_type="text/html", body='<a href="/login/logout.php">Déconnexion</a>')
                else:
                    route.fulfill(status=404, body="Not found in fixture")

            context.route("**/*", fixture)
            cfg = SimpleNamespace(email="fixture@example.test", password="fixture-only-password", wait_2fa=1, login_timeout=10)
            try:
                try:
                    page = app.microsoft_login(context, cfg)
                except RuntimeError as exc:
                    self.fail(f"{exc}; dernière URL {context.pages[0].url}; étapes POST {list(posts)}; HTML {context.pages[0].content()[:800]}")
                self.assertEqual(page.url, app.MOODLE + "/my/fixture-authenticated")
                self.assertIn("fixture%40example.test", posts["email"])
                self.assertIn("fixture-only-password", posts["password"])
            finally:
                context.close()
                browser.close()

    def test_browser_cookies_http_stream_and_actual_legacy_sftp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config(root)
            fixture_root = root / "ubuntu"
            fixture_root.mkdir()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            host_key = paramiko.RSAKey.generate(2048)
            stop, transports = threading.Event(), []

            def serve():
                sock, _ = listener.accept()
                transport = paramiko.Transport(sock)
                transports.append(transport)
                transport.add_server_key(host_key)
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, LocalSFTPServer, root=fixture_root)
                transport.start_server(server=LocalSSHServer())
                while transport.is_active() and not stop.wait(0.1):
                    pass
                transport.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            ssh = paramiko.SSHClient()
            ssh.get_host_keys().add(f"[127.0.0.1]:{port}", host_key.get_name(), host_key)
            ssh.connect("127.0.0.1", port=port, username="fixture", password="fixture-password", allow_agent=False, look_for_keys=False, timeout=5)
            sftp = ssh.open_sftp()
            server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHTTP)
            http_thread = threading.Thread(target=server.serve_forever, daemon=True)
            http_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context()
                    page = context.new_page()
                    page.set_content('<input type="email"><input type="password">')
                    self.assertTrue(app.fill_visible(page, "input[type=email]", "fixture@example.test"))
                    self.assertEqual(page.locator("input[type=email]").input_value(), "fixture@example.test")
                    context.add_cookies([{"name":"MoodleSession", "value":"fixture", "url":origin}])
                    http = app.MoodleHTTP(context, cfg, origin)
                    store = app.RemoteStore(sftp, cfg)
                    resource = app.Resource("Cours français", "Semaine 1", origin + "/pluginfile.php/1/cours.pdf", "PDF")
                    sync = app.Synchronizer(http, store, cfg, app.Summary())
                    sync.file(resource)
                    relative = "Cours français/Semaine 1/Cours français.pdf"
                    destination = fixture_root / "home/test/cours" / relative
                    self.assertEqual(destination.read_bytes(), FixtureHTTP.payload)
                    self.assertFalse(list(sync.temp.glob("*.download")))
                    store.save(sync.temp)
                    reloaded = app.RemoteStore(sftp, cfg)
                    start_gets = FixtureHTTP.gets
                    second = app.Synchronizer(http, reloaded, cfg, app.Summary())
                    second.file(resource)
                    self.assertEqual(FixtureHTTP.gets, start_gets)
                    self.assertEqual(len(second.summary.skipped), 1)
                    with self.assertRaises(app.ExternalRedirect) as caught:
                        http.head(origin + "/external")
                    self.assertEqual(caught.exception.url, "https://example.invalid/external")
                    pipeline = app.Synchronizer(http, reloaded, cfg, app.Summary())
                    with patch.object(app, "MOODLE", origin):
                        pipeline.course(501, "Automatique")
                        pipeline.course(502, "Anglais")
                    self.assertFalse(pipeline.summary.errors)
                    self.assertEqual(len(pipeline.summary.sent), 1)
                    self.assertIn("Anglais", pipeline.summary.unavailable)
                    self.assertEqual(pipeline.summary.external_links, 2)
                    remote_root = fixture_root / "home/test/cours"
                    self.assertTrue((remote_root / "Automatique/TD1/Cours français.pdf").is_file())
                    self.assertTrue((remote_root / "Automatique/TD1/Consignes.md").is_file())
                    self.assertIn("https://example.invalid/resource", (remote_root / "Automatique/liens_externes.md").read_text(encoding="utf-8"))
                    self.assertFalse(any("/mod/quiz/" in url for _, url in FixtureHTTP.visits))
                    # Deuxième parcours : aucun GET de PDF, y compris les ressources
                    # qui redirigent immédiatement vers un téléchargement.
                    visits_before = len(FixtureHTTP.visits)
                    replay = app.Synchronizer(http, reloaded, cfg, app.Summary())
                    with patch.object(app, "MOODLE", origin):
                        replay.course(501, "Automatique")
                    self.assertFalse(replay.summary.errors)
                    self.assertFalse(any(method == "GET" and "/pluginfile.php/" in url for method, url in FixtureHTTP.visits[visits_before:]))
                    empty = browser.new_context()
                    with self.assertRaises(app.AuthenticationExpired):
                        app.MoodleHTTP(empty, cfg, origin).head(resource.url)
                    empty.close()
                    context.close()
                    browser.close()
            finally:
                sftp.close()
                ssh.close()
                server.shutdown()
                server.server_close()
                stop.set()
                for transport in transports:
                    transport.close()
                listener.close()
                thread.join(timeout=5)
                http_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
