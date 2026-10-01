import unittest
from aiohttp import BasicAuth, web
from aiohttp.test_utils import TestClient, TestServer
from filebrowser_proxy import add_filebrowser_proxy


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received = []
        async def backend(request):
            self.received.append((request.method, request.headers.get('X-Filebrowser-User'), request.headers.get('Authorization')))
            return web.Response(body=b'course-document', headers={'Content-Type': 'application/octet-stream'})
        application = web.Application()
        application.router.add_route('*', '/{path:.*}', backend)
        self.backend = TestServer(application)
        await self.backend.start_server()
        proxy = web.Application()
        self.password = 'fixture-password-not-production-123'
        add_filebrowser_proxy(proxy, 'cours', self.password, str(self.backend.make_url('')).rstrip('/'))
        self.client = TestClient(TestServer(proxy))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.backend.close()

    async def test_missing_or_wrong_password_never_reaches_filebrowser(self):
        for auth in (None, BasicAuth('cours', 'incorrect')):
            response = await self.client.get('/', auth=auth)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.received, [])

    async def test_authenticated_get_replaces_spoofed_identity_and_streams(self):
        response = await self.client.get('/api/resources/document.pdf', auth=BasicAuth('cours', self.password),
                                         headers={'X-Filebrowser-User': 'admin'})
        self.assertEqual(await response.read(), b'course-document')
        self.assertEqual(self.received, [('GET', 'cours', None)])

    async def test_mutating_operations_blocked_even_when_authenticated(self):
        for method in ('PUT', 'DELETE', 'PATCH', 'POST'):
            response = await self.client.request(method, '/api/resources/test', auth=BasicAuth('cours', self.password))
            self.assertEqual(response.status, 403)
        self.assertEqual(self.received, [])

    async def test_filebrowser_proxy_login_is_allowed_for_fixed_reader(self):
        response = await self.client.post('/api/login', auth=BasicAuth('cours', self.password), json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.received[0][1], 'cours')


if __name__ == '__main__':
    unittest.main()
