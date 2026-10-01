"""Extension discord.py 2.6+, Python 3.8+ : /cours, sans token dans le code."""
import asyncio
import functools
import logging
import math
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import discord
from discord import app_commands
from discord.ext import commands

LOG = logging.getLogger(__name__)
PAGE_SIZE = 25
MAX_ATTACHMENT = 25 * 1024 * 1024
ICONS = {'.pdf': '📄', '.ppt': '📊', '.pptx': '📊', '.pps': '📊', '.ppsx': '📊',
         '.doc': '📝', '.docx': '📝', '.odt': '📝', '.txt': '📝', '.md': '📝',
         '.xls': '📈', '.xlsx': '📈', '.csv': '📈', '.zip': '📦', '.7z': '📦',
         '.rar': '📦', '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.mp4': '🎬',
         '.py': '💻', '.ipynb': '💻', '.m': '💻'}


def display_name(name):
    return unicodedata.normalize('NFC', name).replace('_', ' ')


def fit(text, limit=100):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def size_text(size):
    return '{:.1f} Mio'.format(size / 1024 ** 2)


async def disk_call(function, *args):
    # asyncio.to_thread n'est pas disponible sur le Python 3.8 de ce serveur.
    return await asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args))


@dataclass(frozen=True)
class Settings:
    root: Path
    timeout: float = 1800
    download_base: str = ''
    roles: tuple = ()
    link_key_file: str = ''
    link_url_file: str = ''
    link_ttl: int = 3600

    @classmethod
    def from_env(cls):
        root = Path(os.environ.get('COURS_DIR', '/srv/cours')).expanduser()
        if not root.is_absolute():
            raise ValueError('COURS_DIR doit être un chemin absolu')
        timeout = float(os.environ.get('COURS_VIEW_TIMEOUT', '1800'))
        if not 60 <= timeout <= 86400:
            raise ValueError('COURS_VIEW_TIMEOUT doit être compris entre 60 et 86400 secondes')
        base = os.environ.get('COURS_DOWNLOAD_BASE_URL', '').rstrip('/')
        if base and (urlsplit(base).scheme not in ('http', 'https') or not urlsplit(base).netloc or urlsplit(base).query or urlsplit(base).fragment):
            raise ValueError('COURS_DOWNLOAD_BASE_URL doit être une URL HTTP(S) sans paramètres')
        roles = tuple(int(x.strip()) for x in os.environ.get('COURS_ALLOWED_ROLE_IDS', '').split(',') if x.strip())
        key_file = os.environ.get('COURS_LINK_KEY_FILE', '')
        url_file = os.environ.get('COURS_LINK_URL_FILE', '')
        ttl = int(os.environ.get('COURS_LINK_TTL_SECONDS', '3600'))
        if not 60 <= ttl <= 86400:
            raise ValueError('COURS_LINK_TTL_SECONDS doit être compris entre 60 et 86400 secondes')
        if bool(key_file) != bool(url_file):
            raise ValueError('Configurer ensemble COURS_LINK_KEY_FILE et COURS_LINK_URL_FILE')
        return cls(root=root, timeout=timeout, download_base=base, roles=roles,
                   link_key_file=key_file, link_url_file=url_file, link_ttl=ttl)


@dataclass(frozen=True)
class Entry:
    path: Path
    folder: bool
    size: int = 0


class Catalog:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.root.resolve()

    def check(self, relative=Path('.')):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise PermissionError('Chemin hors du dossier des cours')
        path = self.root
        for part in relative.parts:
            if part.startswith(('.', '_')) or '.part-' in part or '.previous-' in part:
                raise PermissionError('Fichier technique masqué')
            path = path / part
            if path.is_symlink():
                raise PermissionError('Lien symbolique non autorisé')
        path.resolve().relative_to(self.root)
        return path

    def list(self, relative=Path('.')):
        folder = self.check(relative)
        entries = []
        with os.scandir(str(folder)) as children:
            for child in children:
                if child.name.startswith(('.', '_')) or '.part-' in child.name or '.previous-' in child.name or child.is_symlink():
                    continue
                try:
                    info = child.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Synchronisation atomique en cours.
                is_dir = stat.S_ISDIR(info.st_mode)
                if not is_dir and not stat.S_ISREG(info.st_mode):
                    continue
                if relative == Path('.') and not is_dir:
                    continue
                entries.append(Entry(Path(child.path).relative_to(self.root), is_dir, info.st_size))
        return sorted(entries, key=lambda e: (not e.folder, display_name(e.path.name).casefold(), e.path.name))

    def open_document(self, relative):
        path = self.check(relative)
        # Sur Linux, ouvrir chaque répertoire avec O_NOFOLLOW bloque aussi une
        # substitution par un symlink entre l'affichage du menu et le clic.
        if os.name == 'posix':
            directory = os.open(str(self.root), os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in relative.parts[:-1]:
                    nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = nxt
                fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            finally:
                os.close(directory)
            stream = os.fdopen(fd, 'rb')
        else:
            stream = path.open('rb')
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            stream.close()
            raise PermissionError('Ce document ne correspond pas à un fichier ordinaire')
        return stream, info.st_size

    def large_document(self, relative, size):
        name = discord.utils.escape_markdown(display_name(relative.name))
        text = '**{}** — {}. Ce fichier dépasse la limite de pièce jointe.\n'.format(name, size_text(size))
        if self.settings.link_key_file:
            from download_links import SignedLinks
            try:
                link = SignedLinks(self.settings.link_key_file, self.settings.link_url_file, self.settings.link_ttl).url(relative.as_posix())
                text += '[📥 Télécharger le document](<{}>)\nLien valable {} minutes.'.format(link, self.settings.link_ttl // 60)
            except (OSError, ValueError, KeyError):
                text += 'Le service de téléchargement démarre ou est indisponible. Réessaie dans un instant.'
        elif self.settings.download_base:
            text += '[Télécharger le document](<{}>)'.format(self.settings.download_base + '/' + quote(relative.as_posix(), safe='/'))
        else:
            text += 'Emplacement sur Ubuntu :\n```\n{}\n```'.format(str(self.root / relative).replace('```', "'''"))
        return fit(text, 1900)


async def notify(interaction, text, private=True):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=private, allowed_mentions=discord.AllowedMentions.none())
    else:
        await interaction.response.send_message(text, ephemeral=private, allowed_mentions=discord.AllowedMentions.none())


class CourseSelect(discord.ui.Select):
    def __init__(self, explorer, entries):
        self.explorer = explorer
        self.entries = entries  # Valeurs opaques : aucun chemin fourni par Discord.
        options = []
        for number, item in enumerate(entries):
            options.append(discord.SelectOption(
                label=fit(display_name(item.path.name)), value=str(number),
                emoji='📁' if item.folder else ICONS.get(item.path.suffix.lower(), '📎'),
                description='Ouvrir le dossier' if item.folder else size_text(item.size)))
        super().__init__(placeholder=explorer.prompt(), options=options, row=0)

    async def callback(self, interaction):
        explorer = self.explorer
        await interaction.response.defer()
        async with explorer.lock:
            try:
                # Un clic en retard sur une ancienne page ne doit pas choisir
                # accidentellement un autre fichier avec le même index.
                if self not in explorer.children:
                    await notify(interaction, 'Le menu a changé. Choisis dans le menu actuel.')
                    return
                index = int(self.values[0])
                if not 0 <= index < len(self.entries):
                    raise ValueError('Choix invalide')
                selected = self.entries[index]
                if selected.folder:
                    explorer.relative, explorer.page = selected.path, 0
                    await explorer.render(interaction)
                else:
                    await explorer.send_document(interaction, selected.path)
            except (OSError, ValueError) as exc:
                await explorer.problem(interaction, exc)


class CourseView(discord.ui.View):
    def __init__(self, catalog, owner_id):
        super().__init__(timeout=catalog.settings.timeout)
        self.catalog, self.owner_id = catalog, owner_id
        self.relative, self.page = Path('.'), 0
        self.message = None
        self.lock = asyncio.Lock()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.owner_id:
            await notify(interaction, 'Lance /cours pour ouvrir ton propre explorateur.')
            return False
        if not allowed(interaction, self.catalog.settings):
            await notify(interaction, 'Tu ne disposes plus du rôle nécessaire pour accéder aux cours.')
            return False
        return True

    def prompt(self):
        if self.relative == Path('.'):
            return '1 · Choisir une matière'
        if len(self.relative.parts) == 1:
            return '2 · Choisir un chapitre ou un document'
        return '3 · Choisir un fichier'

    async def build(self):
        # Si le dossier a été supprimé, remonter au premier parent existant.
        while True:
            try:
                entries = await disk_call(self.catalog.list, self.relative)
                break
            except FileNotFoundError:
                if self.relative == Path('.'):
                    raise
                self.relative = self.relative.parent
                self.page = 0
        pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        self.clear_items()
        if entries:
            self.add_item(CourseSelect(self, entries[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]))
        controls = [('⬅️ Retour', self.back, self.relative == Path('.')),
                    ('🔄 Actualiser', self.refresh, False)]
        if pages > 1:
            controls += [('◀ Précédent', self.previous, self.page == 0),
                         ('Suivant ▶', self.next_page, self.page == pages - 1)]
        for label, callback, disabled in controls:
            button = discord.ui.Button(label=label, disabled=disabled, row=1)
            button.callback = callback
            self.add_item(button)
        location = 'Matières' if self.relative == Path('.') else ' / '.join(display_name(p) for p in self.relative.parts)
        embed = discord.Embed(title='📚 Explorateur de cours', description=fit(discord.utils.escape_markdown(location), 1500), colour=discord.Colour.blue())
        if not entries:
            embed.add_field(name='Dossier vide', value='Aucun document disponible ici. Utilise Retour ou Actualiser.', inline=False)
        else:
            embed.add_field(name=self.prompt(), value='Sélectionne un dossier ou un document dans le menu.', inline=False)
        embed.set_footer(text='Page {}/{} · {} éléments · expiration après {} minutes sans clic'.format(self.page + 1, pages, len(entries), int(self.timeout / 60)))
        return embed

    async def render(self, interaction):
        embed = await self.build()
        await interaction.message.edit(embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none())
        self.message = interaction.message

    async def navigate(self, interaction, action):
        await interaction.response.defer()
        async with self.lock:
            try:
                action()
                await self.render(interaction)
            except (OSError, ValueError) as exc:
                await self.problem(interaction, exc)

    async def back(self, interaction):
        def move():
            self.relative = self.relative.parent
            self.page = 0
        await self.navigate(interaction, move)

    async def refresh(self, interaction):
        await self.navigate(interaction, lambda: None)

    async def previous(self, interaction):
        await self.navigate(interaction, lambda: setattr(self, 'page', self.page - 1))

    async def next_page(self, interaction):
        await self.navigate(interaction, lambda: setattr(self, 'page', self.page + 1))

    async def send_document(self, interaction, relative):
        stream, size = await disk_call(self.catalog.open_document, relative)
        limit = min(MAX_ATTACHMENT, getattr(interaction, 'filesize_limit', 10 * 1024 * 1024))
        with stream:
            if size >= limit:
                await notify(interaction, self.catalog.large_document(relative, size), private=False)
                return
            document = discord.File(stream, filename=relative.name)
            try:
                await interaction.followup.send(file=document, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:
                if exc.status == 413 or exc.code == 40005:
                    await notify(interaction, self.catalog.large_document(relative, size), private=False)
                else:
                    raise
            finally:
                document.close()

    async def problem(self, interaction, exc):
        if isinstance(exc, FileNotFoundError):
            text = 'Ce document ou dossier a été déplacé ou supprimé. Clique sur Actualiser.'
        elif isinstance(exc, PermissionError):
            text = "Le bot n'a pas accès à ce document. Vérifie les permissions du dossier Ubuntu."
        else:
            LOG.warning('Erreur de navigation : %s', type(exc).__name__)
            text = 'Impossible de lire ce document. Clique sur Actualiser ou réessaie plus tard.'
        await notify(interaction, text)

    async def on_error(self, interaction, error, item):
        LOG.error('Erreur explorateur : %s', type(error).__name__)
        try:
            await notify(interaction, "L'opération a échoué. Vérifie les permissions Discord et réessaie.")
        except discord.HTTPException:
            LOG.warning('Impossible de transmettre le message d’erreur Discord')

    async def on_timeout(self):
        async with self.lock:
            for item in self.children:
                item.disabled = True
            if self.message:
                try:
                    await self.message.edit(content='Explorateur expiré. Lance /cours pour le rouvrir.', view=self)
                except discord.HTTPException:
                    pass


def allowed(interaction, settings):
    if not settings.roles:
        return True
    roles = {role.id for role in getattr(interaction.user, 'roles', ())}
    return bool(roles.intersection(settings.roles))


class CourseBrowser(commands.Cog):
    def __init__(self, bot, settings=None):
        self.bot = bot
        self.catalog = Catalog(settings or Settings.from_env())

    @app_commands.command(name='cours', description='Explorer les matières, chapitres et documents de cours')
    @app_commands.guild_only()
    @app_commands.checks.cooldown(2, 10.0, key=lambda i: i.user.id)
    async def cours(self, interaction: discord.Interaction):
        if not allowed(interaction, self.catalog.settings):
            await notify(interaction, "Tu n'as pas le rôle nécessaire pour accéder aux cours.")
            return
        await interaction.response.defer(thinking=True)
        view = CourseView(self.catalog, interaction.user.id)
        try:
            embed = await view.build()
        except (OSError, ValueError) as exc:
            await view.problem(interaction, exc)
            # Supprimer le message public d'attente après une erreur privée.
            await interaction.delete_original_response()
            return
        message = await interaction.edit_original_response(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
        # Message éditable par le token du BOT après les 15 minutes du token
        # d'interaction initial : le menu public reste utilisable 30 minutes.
        view.message = interaction.channel.get_partial_message(message.id)

    @cours.error
    async def cours_error(self, interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await notify(interaction, 'Réessaie dans quelques secondes.')
        else:
            LOG.error('Erreur /cours : %s', type(error).__name__)
            await notify(interaction, "Impossible d'ouvrir l'explorateur. Vérifie les permissions du bot.")


async def setup(bot):
    await bot.add_cog(CourseBrowser(bot))
