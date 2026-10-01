"""Arrêt du PC Ubuntu, réservé à un unique ID Discord configuré localement."""
import asyncio
import logging
import os
import discord
from discord import app_commands
from discord.ext import commands

LOG = logging.getLogger(__name__)
SHUTDOWN_COMMAND = ('/usr/bin/sudo', '-n', '/sbin/shutdown', '-h', '+1')


async def reply(interaction, text):
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def schedule_shutdown():
    process = await asyncio.create_subprocess_exec(
        *SHUTDOWN_COMMAND, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        await asyncio.wait_for(process.communicate(), timeout=10)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError('La demande d’arrêt a expiré')
    if process.returncode:
        raise RuntimeError('Ubuntu a refusé la demande d’arrêt')


class StopServerView(discord.ui.View):
    def __init__(self, controller):
        super().__init__(timeout=60)
        self.controller = controller
        self.closed = False
        self.lock = asyncio.Lock()
        self.message = None

    async def interaction_check(self, interaction):
        if not self.controller.is_owner(interaction):
            await reply(interaction, "Cette commande est réservée au propriétaire du serveur.")
            return False
        return True

    def finish(self):
        self.closed = True
        for item in self.children:
            item.disabled = True
        self.stop()

    @discord.ui.button(label='Éteindre le PC', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed:
                await reply(interaction, 'Cette confirmation est terminée. Relance /arreter si nécessaire.')
                return
            await interaction.response.defer()
            try:
                await self.controller.request_shutdown()
            except (OSError, RuntimeError):
                LOG.exception('Impossible de programmer l’arrêt Ubuntu')
                await reply(interaction, "L’arrêt a échoué. Le PC reste allumé ; vérifie la permission d’arrêt sur Ubuntu.")
                return
            self.finish()
            await interaction.edit_original_response(
                content='Arrêt du PC programmé dans une minute. Le bot et les téléchargements seront coupés.', view=self)

    @discord.ui.button(label='Annuler', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if not await self.interaction_check(interaction):
            return
        async with self.lock:
            if self.closed:
                await reply(interaction, 'Cette confirmation est terminée.')
                return
            self.finish()
            await interaction.response.edit_message(content='Arrêt annulé : le PC reste allumé.', view=self)

    async def on_timeout(self):
        async with self.lock:
            if self.closed:
                return
            self.finish()
            if self.message:
                try:
                    await self.message.edit(content='Confirmation expirée : le PC reste allumé.', view=self)
                except discord.HTTPException:
                    pass


class ServerControl(commands.Cog):
    def __init__(self, bot, owner_id=None):
        self.bot = bot
        self.owner_id = int(os.environ.get('BOT_OWNER_ID') or 0) if owner_id is None else owner_id
        self.lock = asyncio.Lock()
        self.scheduled = False

    def is_owner(self, interaction):
        return bool(self.owner_id) and interaction.user.id == self.owner_id

    async def request_shutdown(self):
        async with self.lock:
            if not self.scheduled:
                await schedule_shutdown()
                self.scheduled = True

    @app_commands.command(name='arreter', description='Éteindre le PC des cours (propriétaire uniquement)')
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def arreter(self, interaction: discord.Interaction):
        if not self.is_owner(interaction):
            await reply(interaction, "Cette commande est réservée au propriétaire du serveur.")
            return
        if self.scheduled:
            await reply(interaction, 'L’arrêt du PC est déjà programmé.')
            return
        view = StopServerView(self)
        await interaction.response.send_message(
            'Éteindre complètement le petit PC ? Le bot et les téléchargements seront indisponibles. Confirme sous 60 secondes.',
            view=view, ephemeral=True)
        view.message = await interaction.original_response()

    @arreter.error
    async def arreter_error(self, interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            await reply(interaction, 'Attends quelques secondes avant de relancer /arreter.')
        else:
            original = getattr(error, 'original', error)
            LOG.error('Erreur /arreter : %s (HTTP %s, code %s)',
                      type(original).__name__, getattr(original, 'status', None), getattr(original, 'code', None),
                      exc_info=(type(original), original, original.__traceback__))
            await reply(interaction, "Impossible de préparer l’arrêt. Le PC reste allumé.")


async def setup(bot):
    await bot.add_cog(ServerControl(bot))
