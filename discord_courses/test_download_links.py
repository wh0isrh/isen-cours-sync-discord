import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from course_browser import Catalog, Settings
from download_links import SignedLinks
from download_server import create_app


class DownloadsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.key = self.root / 'key'
        self.key.write_bytes(b'fixture-key-only-for-unit-tests!!')
        self.url_file = self.root / 'url.json'
        self.url_file.write_text('{"url":"https://fixture.example"}', encoding='utf-8')
        self.links = SignedLinks(self.key, self.url_file)
        self.course = self.root / 'courses'
        (self.course / 'Matière' / 'Semaine 1').mkdir(parents=True)
        self.relative = 'Matière/Semaine 1/énoncé.pdf'
        self.body = b'0123456789' * 100000
        (self.course / self.relative).write_bytes(self.body)
        self.client = TestClient(TestServer(create_app(Catalog(Settings(self.course)), self.links)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    def query(self, relative=None):
        return urlsplit(self.links.url(relative or self.relative)).query

    async def test_signed_link_full_download_preserves_bytes_and_filename(self):
        response = await self.client.get('/download?' + self.query())
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), self.body)
        self.assertIn('%C3%A9nonc%C3%A9.pdf', response.headers['Content-Disposition'])
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    async def test_unsigned_expired_and_modified_paths_refused(self):
        response = await self.client.get('/download?path=' + self.relative)
        self.assertEqual(response.status, 403)
        query = self.query()
        with patch('download_links.time.time', return_value=time.time() + 4000):
            response = await self.client.get('/download?' + query)
            self.assertEqual(response.status, 403)
        response = await self.client.get('/download?' + query.replace('%C3%A9nonc%C3%A9.pdf', 'other.pdf'))
        self.assertEqual(response.status, 403)

    async def test_ranges_head_and_out_of_bounds(self):
        query = '/download?' + self.query()
        response = await self.client.get(query, headers={'Range': 'bytes=100-199'})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), self.body[100:200])
        response = await self.client.get(query, headers={'Range': 'bytes=-25'})
        self.assertEqual(await response.read(), self.body[-25:])
        response = await self.client.head(query)
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers['Content-Length']), len(self.body))
        self.assertEqual(await response.read(), b'')
        response = await self.client.get(query, headers={'Range': 'bytes=999999999-'})
        self.assertEqual(response.status, 416)

    async def test_no_directory_listing_and_traversal_even_with_valid_signature(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 404)
        response = await self.client.get('/download?' + self.query('../key'))
        self.assertEqual(response.status, 403)

    async def test_url_rotation_is_read_when_creating_each_link(self):
        old = self.links.url(self.relative)
        self.url_file.write_text('{"url":"https://new.fixture.example"}', encoding='utf-8')
        new = self.links.url(self.relative)
        self.assertTrue(old.startswith('https://fixture.example'))
        self.assertTrue(new.startswith('https://new.fixture.example'))


if __name__ == '__main__':
    unittest.main()
