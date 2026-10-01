"""Lanceur autonome optionnel. Pour un bot existant, charger course_browser."""
import logging
import os
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))


class CoursesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())

    async def setup_hook(self):
        await self.load_extension('course_browser')
        guild_id = os.environ.get('DISCORD_GUILD_ID', '').strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            commands_synced = await self.tree.sync(guild=guild)
        else:
            commands_synced = await self.tree.sync()
        logging.info('%s commande(s) enregistrée(s)', len(commands_synced))

    async def on_ready(self):
        logging.info('Bot connecté : %s (id=%s)', self.user.name, self.user.id)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    token = os.environ.get('DISCORD_TOKEN', '').strip()
    if not token:
        raise SystemExit('Renseigner DISCORD_TOKEN dans .env sur Ubuntu avant de lancer le bot.')
    CoursesBot().run(token)
