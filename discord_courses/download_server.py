"""Serveur local sans index, accessible uniquement par liens signés."""
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

from aiohttp import web
from dotenv import load_dotenv
from course_browser import Catalog, Settings, disk_call
from download_links import SignedLinks


def byte_range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('Plage invalide')
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(size - 1, int(last)) if last else size - 1
    else:
        count = int(last)
        if count <= 0:
            raise ValueError('Plage invalide')
        start, end = max(0, size - count), size - 1
    if start > end or start >= size:
        raise ValueError('Plage hors du document')
    return start, end, True


def create_app(catalog, links):
    async def download(request):
        relative = request.query.get('path', '')
        if not links.verify(relative, request.query.get('expires', ''), request.query.get('sig', '')):
            raise web.HTTPForbidden(text='Lien invalide ou expiré. Sélectionne à nouveau le fichier dans Discord.')
        try:
            stream, size = await disk_call(catalog.open_document, Path(relative))
        except FileNotFoundError:
            raise web.HTTPNotFound(text='Document introuvable. Actualise le menu Discord.')
        except (PermissionError, ValueError, OSError):
            raise web.HTTPForbidden(text='Document inaccessible.')
        with stream:
            try:
                start, end, partial = byte_range(request.headers.get('Range'), size)
            except ValueError:
                raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range': 'bytes */{}'.format(size)})
            length = max(0, end - start + 1)
            filename = Path(relative).name
            headers = {'Content-Type': 'application/octet-stream',
                       'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename, safe=''),
                       'Content-Length': str(length), 'Accept-Ranges': 'bytes',
                       'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                       'Referrer-Policy': 'no-referrer'}
            if partial:
                headers['Content-Range'] = 'bytes {}-{}/{}'.format(start, end, size)
            response = web.StreamResponse(status=206 if partial else 200, headers=headers)
            await response.prepare(request)
            if request.method == 'HEAD':
                return response
            await disk_call(stream.seek, start)
            remaining = length
            while remaining:
                chunk = await disk_call(stream.read, min(256 * 1024, remaining))
                if not chunk:
                    raise ConnectionError('Document modifié pendant le téléchargement')
                await response.write(chunk)
                remaining -= len(chunk)
            await response.write_eof()
            return response
    application = web.Application()
    application.router.add_get('/download', download)
    # Aucune route de listing ni de publication du répertoire des cours.
    return application


if __name__ == '__main__':
    load_dotenv(Path(__file__).with_name('.env'))
    logging.basicConfig(level=logging.WARNING)
    settings = Settings.from_env()
    links = SignedLinks(settings.link_key_file, settings.link_url_file, settings.link_ttl)
    application = create_app(Catalog(settings), links)
    if os.environ.get('FILEBROWSER_USER'):
        from filebrowser_proxy import add_filebrowser_proxy
        add_filebrowser_proxy(application, os.environ['FILEBROWSER_USER'], os.environ.get('FILEBROWSER_PASSWORD', ''))
    # Pas de journaux d'accès contenant les signatures des liens.
    web.run_app(application, host='127.0.0.1', port=18880, access_log=None)
