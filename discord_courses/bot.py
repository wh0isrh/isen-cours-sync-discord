"""Lanceur autonome avec auto-montage Rclone OneDrive."""
import logging
import os
import subprocess
import time
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))


def ensure_onedrive_mount():
    cours_dir = Path(os.environ.get('COURS_DIR', '/srv/cours_isen/ISEN_Lille_2026-2027'))
    try:
        res = subprocess.run(['mountpoint', '-q', str(cours_dir)], check=False)
        if res.returncode != 0:
            logging.info('Montage Rclone de OneDrive vers %s...', cours_dir)
            rclone = Path.home() / 'bin' / 'rclone'
            if not rclone.is_file():
                rclone = Path('/opt/isen-cours/bin/rclone')
            cours_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run([
                str(rclone), 'mount',
                'onedrive:Cours_ISEN/ISEN_Lille_2026-2027',
                str(cours_dir),
                '--vfs-cache-mode', 'full',
                '--dir-cache-time', '1m',
                '--daemon'
            ], check=True)
            time.sleep(2)
            logging.info('Montage OneDrive actif.')
    except Exception as exc:
        logging.warning('Auto-montage Rclone : %s', exc)


class CoursesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())

    async def setup_hook(self):
        await self.load_extension('course_browser')
        try:
            await self.load_extension('moodle_sync_bot')
            logging.info('Extension moodle_sync_bot chargée.')
        except Exception as exc:
            logging.warning('Impossible de charger moodle_sync_bot : %s', exc)
        commands_synced = await self.tree.sync()
        logging.info('%s commande(s) globale(s) enregistrée(s)', len(commands_synced))
        guild_id = os.environ.get('DISCORD_GUILD_ID', '').strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            commands_synced = await self.tree.sync(guild=guild)
            logging.info('%s commande(s) du serveur enregistrée(s)', len(commands_synced))

    async def on_ready(self):
        logging.info('Bot connecté : %s (id=%s)', self.user.name, self.user.id)
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logging.info('Synchronisé %s commande(s) pour le serveur %s (%s)', len(synced), guild.name, guild.id)
            except Exception as exc:
                logging.warning('Erreur sync serveur %s: %s', guild.id, exc)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    ensure_onedrive_mount()
    token = os.environ.get('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Renseigner DISCORD_TOKEN dans .env sur Ubuntu avant de lancer le bot.')
    CoursesBot().run(token)
