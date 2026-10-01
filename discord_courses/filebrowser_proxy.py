"""Authentification indépendante de FileBrowser ; navigation en lecture seule."""
import hmac
from aiohttp import BasicAuth, ClientSession, ClientTimeout, web

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host'}


def add_filebrowser_proxy(application, username, password, backend='http://127.0.0.1:18882'):
    if not username or len(password) < 20:
        raise ValueError('Configurer un mot de passe FileBrowser indépendant, de 20 caractères minimum')

    async def start(app):
        app['fb_session'] = ClientSession(auto_decompress=False,
            timeout=ClientTimeout(total=None, connect=5, sock_read=300))

    async def stop(app):
        await app['fb_session'].close()

    async def proxy(request):
        try:
            credentials = BasicAuth.decode(request.headers.get('Authorization', ''))
            valid = hmac.compare_digest(credentials.login.encode(), username.encode()) and hmac.compare_digest(credentials.password.encode(), password.encode())
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise web.HTTPUnauthorized(text='Connexion requise.',
                headers={'WWW-Authenticate': 'Basic realm="Cours ISEN", charset="UTF-8"'})
        if request.method not in ('GET', 'HEAD') and not (request.method == 'POST' and request.path in ('/api/login', '/api/renew', '/api/search')):
            raise web.HTTPForbidden(text='Accès en lecture seule.')
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_HEADERS | {'authorization', 'x-filebrowser-user'}}
        # Ne jamais faire confiance au nom d'utilisateur envoyé par le client.
        headers['X-Filebrowser-User'] = 'cours'
        body = await request.read() if request.method == 'POST' else None
        async with request.app['fb_session'].request(request.method, backend + request.raw_path,
                headers=headers, data=body, allow_redirects=False) as upstream:
            response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_HEADERS}
            response_headers['Cache-Control'] = 'private, no-store'
            response_headers['Referrer-Policy'] = 'no-referrer'
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            if request.method != 'HEAD':
                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await response.write(chunk)
            await response.write_eof()
            return response

    application.on_startup.append(start)
    application.on_cleanup.append(stop)
    application.router.add_route('*', '/{path:.*}', proxy)
