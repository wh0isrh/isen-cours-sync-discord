import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import discord
from discord.ext import commands
import course_browser as app


def interaction(user=1, limit=10 * 1024 * 1024):
    response = SimpleNamespace(defer=AsyncMock(), is_done=lambda: True, send_message=AsyncMock())
    return SimpleNamespace(user=SimpleNamespace(id=user, roles=[]), response=response,
                           followup=SimpleNamespace(send=AsyncMock()), filesize_limit=limit,
                           message=SimpleNamespace(edit=AsyncMock()))


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = app.Settings(self.root)
        self.catalog = app.Catalog(self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, relative, content=b'document'):
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    async def test_three_clicks_send_real_file_and_preserve_filename(self):
        self.file('Electronique/Chapitre_1/TD_corrige.pdf')
        view = app.CourseView(self.catalog, 1)
        await view.build()
        for expected in ('Electronique', 'Chapitre 1', 'TD corrige.pdf'):
            menu = view.children[0]
            self.assertEqual(menu.options[0].label, expected)
            menu._values = ['0']
            current = interaction()
            await menu.callback(current)
        document = current.followup.send.call_args.kwargs['file']
        self.assertEqual(document.filename, 'TD_corrige.pdf')
        self.assertFalse(current.followup.send.call_args.kwargs['ephemeral'])

    async def test_pagination_folders_and_files_over_25(self):
        for i in range(27):
            self.file('Matiere{:02}/Semaine/Enonce.pdf'.format(i))
        view = app.CourseView(self.catalog, 1)
        embed = await view.build()
        self.assertEqual(len(view.children[0].options), 25)
        self.assertIn('Page 1/2', embed.footer.text)
        await view.next_page(interaction())
        self.assertEqual(len(view.children[0].options), 2)
        section = self.root / 'Matiere00' / 'Semaine'
        for i in range(29):
            (section / ('Fichier{:02}.pdf'.format(i))).write_bytes(b'a')
        view.relative, view.page = Path('Matiere00/Semaine'), 0
        await view.build()
        self.assertEqual(len(view.children[0].options), 25)
        await view.next_page(interaction())
        self.assertEqual(len(view.children[0].options), 5)

    async def test_names_same_label_have_distinct_opaque_values(self):
        self.file('Cours/Chapitre/nom_long_' + 'x' * 105 + '.pdf')
        self.file('Cours/Chapitre/nom_long_' + 'x' * 105 + 'bis.pdf')
        view = app.CourseView(self.catalog, 1)
        view.relative = Path('Cours/Chapitre')
        await view.build()
        opts = view.children[0].options
        self.assertEqual(opts[0].label, opts[1].label)
        self.assertNotEqual(opts[0].value, opts[1].value)
        self.assertTrue(all(len(x.label) <= 100 for x in opts))

    async def test_refresh_reads_new_files_and_back_returns_parent(self):
        self.file('Cours/Section/a.pdf')
        view = app.CourseView(self.catalog, 1)
        view.relative = Path('Cours/Section')
        await view.build()
        self.file('Cours/Section/b.pdf')
        await view.refresh(interaction())
        self.assertEqual(len(view.children[0].options), 2)
        await view.back(interaction())
        self.assertEqual(view.relative, Path('Cours'))

    async def test_empty_folder_and_deleted_section(self):
        (self.root / 'Cours' / 'Vide').mkdir(parents=True)
        view = app.CourseView(self.catalog, 1)
        view.relative = Path('Cours/Vide')
        embed = await view.build()
        self.assertIn('Dossier vide', embed.fields[0].name)
        self.assertFalse(any(isinstance(x, discord.ui.Select) for x in view.children))
        (self.root / 'Cours' / 'Vide').rmdir()
        await view.refresh(interaction())
        self.assertEqual(view.relative, Path('Cours'))

    async def test_stale_menu_never_opens_wrong_document(self):
        self.file('Cours/Section/a.pdf')
        view = app.CourseView(self.catalog, 1)
        view.relative = Path('Cours/Section')
        await view.build()
        old = view.children[0]
        await view.refresh(interaction())
        old._values = ['0']
        current = interaction()
        await old.callback(current)
        self.assertIn('menu a changé', current.followup.send.call_args.args[0])
        self.assertNotIn('file', current.followup.send.call_args.kwargs)

    async def test_attachment_limit_adapts_to_discord_and_encodes_external_link(self):
        self.file('Cours/Semaine 1/énoncé.pdf', b'abcdef')
        catalog = app.Catalog(app.Settings(self.root, download_base='https://example.org/cours'))
        view = app.CourseView(catalog, 1)
        current = interaction(limit=5)
        await view.send_document(current, Path('Cours/Semaine 1/énoncé.pdf'))
        text = current.followup.send.call_args.args[0]
        self.assertIn('Semaine%201/%C3%A9nonc%C3%A9.pdf', text)
        self.assertNotIn('file', current.followup.send.call_args.kwargs)
        self.assertFalse(current.followup.send.call_args.kwargs['ephemeral'])

    async def test_25_mib_always_uses_large_file_fallback(self):
        p = self.file('Cours/Diapo.ppsx')
        with p.open('wb') as f:
            f.truncate(app.MAX_ATTACHMENT)
        view = app.CourseView(self.catalog, 1)
        current = interaction(limit=100 * 1024 * 1024)
        await view.send_document(current, Path('Cours/Diapo.ppsx'))
        text = current.followup.send.call_args.args[0]
        self.assertIn('25.0 Mio', text)
        self.assertIn(str(p), text)

    async def test_hidden_artifacts_and_traversal_are_refused(self):
        self.file('_Inventaire/rapport.json')
        self.file('Cours/.secret')
        self.file('Cours/f.pdf.part-abcd')
        self.file('Cours/f.pdf.previous-abcd')
        self.file('Cours/vrai.pdf')
        self.assertEqual([e.path.name for e in self.catalog.list()], ['Cours'])
        self.assertEqual([e.path.name for e in self.catalog.list(Path('Cours'))], ['vrai.pdf'])
        for relative in ('../secret', '/etc/passwd', 'Cours/.secret'):
            with self.assertRaises((PermissionError, ValueError)):
                self.catalog.check(Path(relative))

    async def test_missing_document_and_permissions_show_useful_errors(self):
        self.file('Cours/Section/a.pdf')
        view = app.CourseView(self.catalog, 1)
        view.relative = Path('Cours/Section')
        await view.build()
        menu = view.children[0]
        menu._values = ['0']
        (self.root / 'Cours/Section/a.pdf').unlink()
        current = interaction()
        await menu.callback(current)
        self.assertIn('supprimé', current.followup.send.call_args.args[0])
        with patch.object(self.catalog, 'list', side_effect=PermissionError('fixture')):
            await view.refresh(current)
        self.assertIn('permissions', current.followup.send.call_args.args[0])

    async def test_owner_and_role_checks(self):
        view = app.CourseView(self.catalog, 1)
        current = interaction(user=2)
        self.assertFalse(await view.interaction_check(current))
        self.assertTrue(await view.interaction_check(interaction(user=1)))
        catalog = app.Catalog(app.Settings(self.root, roles=(42,)))
        restricted = app.CourseView(catalog, 1)
        self.assertFalse(await restricted.interaction_check(interaction(user=1)))
        authorized = interaction(user=1)
        authorized.user.roles = [SimpleNamespace(id=42)]
        self.assertTrue(await restricted.interaction_check(authorized))

    async def test_timeout_disables_components(self):
        (self.root / 'Cours').mkdir()
        view = app.CourseView(self.catalog, 1)
        await view.build()
        view.message = SimpleNamespace(edit=AsyncMock())
        await view.on_timeout()
        self.assertTrue(all(x.disabled for x in view.children))
        view.message.edit.assert_awaited_once()

    async def test_extension_registers_slash_command_without_network(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
        try:
            await bot.add_cog(app.CourseBrowser(bot, self.settings))
            self.assertIsNotNone(bot.tree.get_command('cours'))
        finally:
            await bot.close()

    @unittest.skipUnless(os.name == 'posix', 'Protection dir_fd vérifiée sur Ubuntu')
    async def test_symlink_and_fifo_are_not_downloaded(self):
        self.file('Cours/vrai.pdf')
        (self.root / 'Cours/lien.pdf').symlink_to('/etc/passwd')
        os.mkfifo(str(self.root / 'Cours/tube.pdf'))
        self.assertEqual([e.path.name for e in self.catalog.list(Path('Cours'))], ['vrai.pdf'])
        with self.assertRaises(PermissionError):
            self.catalog.open_document(Path('Cours/lien.pdf'))
        with self.assertRaises(PermissionError):
            self.catalog.open_document(Path('Cours/tube.pdf'))


if __name__ == '__main__':
    unittest.main()
