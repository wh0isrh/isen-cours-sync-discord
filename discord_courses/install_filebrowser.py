"""Préparer les binaires officiels et la configuration sur Ubuntu, Python 3.8+."""
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path
from urllib.request import Request, urlopen
from dotenv import dotenv_values, set_key

BASE = Path(__file__).resolve().parent


def fetch(url):
    request = Request(url, headers={'User-Agent': 'ISEN-Courses-Installer/1.0'})
    return urlopen(request, timeout=120)


def release_asset(repository, filename, destination):
    with fetch('https://api.github.com/repos/' + repository + '/releases/latest') as response:
        release = json.load(response)
    asset = next(item for item in release['assets'] if item['name'] == filename)
    expected = asset.get('digest') or ''
    if not expected.startswith('sha256:'):
        checks = next(item for item in release['assets'] if 'checksum' in item['name'].lower() and item['name'].endswith('.txt'))
        with fetch(checks['browser_download_url']) as response:
            lines = response.read().decode().splitlines()
        expected = 'sha256:' + next(line.split()[0] for line in lines if line.split()[-1].lstrip('*') == filename)
    temporary = destination.with_suffix('.part')
    digest = hashlib.sha256()
    try:
        with fetch(asset['browser_download_url']) as response, temporary.open('wb') as output:
            while True:
                data = response.read(1024 * 1024)
                if not data:
                    break
                digest.update(data)
                output.write(data)
        if digest.hexdigest() != expected.split(':', 1)[1]:
            raise ValueError('Checksum incorrect : ' + filename)
        os.replace(str(temporary), str(destination))
    finally:
        temporary.unlink(missing_ok=True)
    print('Binaire officiel vérifié :', repository, release['tag_name'], filename)


def main():
    if platform.system() != 'Linux':
        raise SystemExit('Exécuter cet installateur sur Ubuntu, avec le venv Python 3.8 du bot.')
    machine = platform.machine()
    arch = {'i386': '386', 'i686': '386', 'x86_64': 'amd64', 'aarch64': 'arm64'}.get(machine)
    if not arch:
        raise SystemExit('Architecture non prise en charge : ' + machine)
    env = BASE / '.env'
    values = dotenv_values(env)
    root = Path(values.get('COURS_DIR', '/srv/cours'))
    if not root.is_absolute() or not root.is_dir() or root == Path('/'):
        raise SystemExit('Le dossier COURS_DIR doit exister et être absolu.')
    if not (BASE / 'filebrowser').exists():
        fb_arch = {'amd64': 'amd64', '386': '386', 'arm64': 'arm64'}[arch]
        archive = BASE / 'filebrowser.tar.gz'
        release_asset('filebrowser/filebrowser', 'linux-' + fb_arch + '-filebrowser.tar.gz', archive)
        with tarfile.open(str(archive)) as bundle:
            member = next(m for m in bundle.getmembers() if Path(m.name).name == 'filebrowser' and m.isfile())
            with bundle.extractfile(member) as source, (BASE / 'filebrowser').open('wb') as target:
                shutil.copyfileobj(source, target)
        archive.unlink()
        (BASE / 'filebrowser').chmod(0o700)
    if not (BASE / 'cloudflared').exists():
        release_asset('cloudflare/cloudflared', 'cloudflared-linux-' + arch, BASE / 'cloudflared')
        (BASE / 'cloudflared').chmod(0o700)
    for binary in ('filebrowser', 'cloudflared'):
        subprocess.run([str(BASE / binary), '--version'], check=True)
    database = BASE / 'filebrowser.db'
    marker = BASE / '.filebrowser_setup.json'
    if database.exists() and not marker.exists():
        raise SystemExit('Base FileBrowser existante non créée par cet outil : conservée sans modification.')
    if not database.exists():
        marker.write_text(json.dumps({'root': str(root)}), encoding='utf-8')
        command = [str(BASE / 'filebrowser'), '--database', str(database)]
        subprocess.run(command + ['config', 'init', '--root', str(root), '--auth.method', 'proxy',
            '--auth.header', 'X-Filebrowser-User', '--disableExec=true', '--disableThumbnails=true'], check=True)
        subprocess.run(command + ['users', 'add', 'cours', secrets.token_urlsafe(32), '--scope', '.',
            '--locale', 'fr', '--perm.admin=false', '--perm.execute=false', '--perm.create=false',
            '--perm.rename=false', '--perm.modify=false', '--perm.delete=false', '--perm.share=false',
            '--perm.download=true'], check=True)
    else:
        if json.loads(marker.read_text(encoding='utf-8'))['root'] != str(root):
            raise SystemExit('COURS_DIR a changé : vérifier la base existante avant de relancer.')
        print('Configuration FileBrowser déjà initialisée par cet outil, conservée.')
    database.chmod(0o600)
    key = BASE / '.download_key'
    if not key.exists():
        key.write_bytes(secrets.token_bytes(32))
    key.chmod(0o600)
    updates = {'COURS_LINK_KEY_FILE': str(key), 'COURS_LINK_URL_FILE': str(BASE / 'download_url.json'),
               'COURS_LINK_TTL_SECONDS': '3600', 'FILEBROWSER_USER': 'cours',
               'FILEBROWSER_PASSWORD': values.get('FILEBROWSER_PASSWORD') or secrets.token_urlsafe(24)}
    for name, value in updates.items():
        set_key(str(env), name, value, quote_mode='always')
    env.chmod(0o600)
    print('Configuration prête. Identifiants FileBrowser dans .env, sans affichage du mot de passe.')


if __name__ == '__main__':
    main()
