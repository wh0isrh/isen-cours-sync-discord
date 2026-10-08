"""Extension discord.py pour lancer la synchronisation Moodle directement sur le VPS."""
import asyncio
import functools
import logging
import os
import sys
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

# Assurer l'accès au module moodle_sync
PROJECT_DIR = Path(__file__).resolve().parent.parent
MOODLE_SYNC_DIR = PROJECT_DIR / "moodle_sync"
if str(MOODLE_SYNC_DIR) not in sys.path:
    sys.path.insert(0, str(MOODLE_SYNC_DIR))

import sync_moodle

LOG = logging.getLogger("moodle_sync_bot")


def get_allowed_role_ids() -> set[int]:
    raw = os.environ.get("COURS_ALLOWED_ROLE_IDS", "").strip()
    if not raw:
        return set()
    result = set()
    for token in raw.split(","):
        token = token.strip()
        if token.isdigit():
            result.add(int(token))
    return result


def is_user_authorized(interaction: discord.Interaction) -> bool:
    allowed_roles = get_allowed_role_ids()
    if not allowed_roles:
        return True
    user_roles = getattr(interaction.user, "roles", [])
    user_role_ids = {r.id for r in user_roles}
    return bool(allowed_roles & user_role_ids) or interaction.user.guild_permissions.administrator


class MoodleSyncCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sync_lock = asyncio.Lock()
        self.last_sync_time = 0
        self.last_sync_summary = None
        self.env_path = MOODLE_SYNC_DIR / ".env"
        if not self.env_path.is_file():
            # Repli sur le .env de discord_courses si moodle_sync/.env n'existe pas
            fallback = Path(__file__).resolve().parent / ".env"
            if fallback.is_file():
                self.env_path = fallback

        auto_sync = os.environ.get("AUTO_SYNC_ENABLED", "false").strip().lower() in {"true", "1", "yes"}
        if auto_sync:
            self.auto_sync_task.start()

    def cog_unload(self):
        if self.auto_sync_task.is_running():
            self.auto_sync_task.cancel()

    @tasks.loop(hours=6)
    async def auto_sync_task(self):
        """Tâche planifiée périodique en tâche de fond sur le VPS."""
        if self.sync_lock.locked():
            return
        LOG.info("Exécution planifiée de la synchronisation Moodle...")
        await self._execute_sync(channel=None, user=None, target_courses=None)

    @auto_sync_task.before_loop
    async def before_auto_sync(self):
        await self.bot.wait_until_ready()

    async def course_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        choices = []
        clean_curr = sync_moodle.clean_name(current).lower()
        for idx, (cid, name) in enumerate(sync_moodle.COURSES, 1):
            if not current or clean_curr in name.lower() or current in str(idx) or current in str(cid):
                display = f"[{idx}] {name}"
                choices.append(app_commands.Choice(name=display[:100], value=name))
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="sync", description="Lancer la synchronisation Moodle Junia Learning sur le VPS")
    @app_commands.describe(matiere="Matière spécifique à synchroniser (laisser vide pour toutes)")
    @app_commands.autocomplete(matiere=course_autocomplete)
    async def sync_cmd(self, interaction: discord.Interaction, matiere: str | None = None):
        if not is_user_authorized(interaction):
            await interaction.response.send_message("❌ Vous n'avez pas l'autorisation d'exécuter cette commande.", ephemeral=True)
            return

        if self.sync_lock.locked():
            await interaction.response.send_message("⚠️ Une synchronisation est déjà en cours d'exécution sur le VPS. Merci de patienter.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        target_courses = None
        if matiere:
            target_courses = sync_moodle.resolve_course_selection(sync_moodle.COURSES, [matiere])

        await self._execute_sync(channel=interaction.channel, user=interaction.user,
                                 target_courses=target_courses, interaction=interaction)

    async def _execute_sync(self, channel: discord.abc.Messageable | None, user: discord.User | None,
                            target_courses: list | None, interaction: discord.Interaction | None = None):
        async with self.sync_lock:
            start_time = time.monotonic()
            selected = target_courses or sync_moodle.COURSES
            course_label = ", ".join(c[1] for c in selected) if len(selected) <= 2 else f"{len(selected)} matières"

            embed = discord.Embed(
                title="🔄 Synchronisation Junia Moodle",
                description=f"**Matières :** {course_label}\n\n⏳ *Démarrage de Chromium headless sur le VPS...*",
                color=discord.Color.blue()
            )
            embed.set_footer(text="Exécution autonome sur VPS Ubuntu")

            if interaction:
                status_msg = await interaction.followup.send(embed=embed)
            elif channel:
                status_msg = await channel.send(embed=embed)
            else:
                status_msg = None

            loop = asyncio.get_running_loop()
            mfa_notified = False

            def on_mfa_code(code: str):
                nonlocal mfa_notified
                mfa_notified = True

                async def notify_discord():
                    mfa_embed = discord.Embed(
                        title="🔐 Validation requise (Microsoft Authenticator)",
                        description=(
                            f"Une demande de double authentification est affichée par Microsoft :\n\n"
                            f"# 👉 [  **{code}**  ] 👈\n\n"
                            f"📱 **Ouvrez Microsoft Authenticator sur votre smartphone et entrez le code ci-dessus.**"
                        ),
                        color=discord.Color.gold()
                    )
                    mfa_embed.set_footer(text="Délai : 60 secondes pour valider sur votre téléphone")

                    if status_msg:
                        await status_msg.edit(embed=mfa_embed)
                    if channel and user:
                        await channel.send(
                            content=f"🔔 {user.mention} **Code de validation Microsoft : `{code}`**",
                            embed=mfa_embed,
                            allowed_mentions=discord.AllowedMentions(users=True)
                        )

                asyncio.run_coroutine_threadsafe(notify_discord(), loop)

            def on_status(status_text: str):
                async def update_status():
                    if status_msg and not mfa_notified:
                        embed.description = f"**Matières :** {course_label}\n\nℹ️ *{status_text}*"
                        try:
                            await status_msg.edit(embed=embed)
                        except Exception:
                            pass
                asyncio.run_coroutine_threadsafe(update_status(), loop)

            def on_progress(current: int, total: int, course_name: str):
                async def update_progress():
                    if status_msg:
                        pct = int((current / total) * 10)
                        bar = "▓" * pct + "░" * (10 - pct)
                        embed.description = (
                            f"**Progression :** `{bar}` **[{current}/{total}]**\n\n"
                            f"📚 *Analyse de :* **{course_name}**"
                        )
                        embed.color = discord.Color.blue()
                        try:
                            await status_msg.edit(embed=embed)
                        except Exception:
                            pass
                asyncio.run_coroutine_threadsafe(update_progress(), loop)

            def worker():
                try:
                    cfg = sync_moodle.Config.load(self.env_path)
                except Exception as exc:
                    return None, f"Erreur de configuration (.env) : {exc}"
                try:
                    summary, code = sync_moodle.run_sync(
                        cfg,
                        target_courses=selected,
                        on_mfa_code=on_mfa_code,
                        on_progress=on_progress,
                        on_status=on_status
                    )
                    return summary, code
                except Exception as exc:
                    return None, str(exc)

            summary, result = await loop.run_in_executor(None, worker)
            duration = int(time.monotonic() - start_time)

            if summary is None:
                err_embed = discord.Embed(
                    title="❌ Échec de la synchronisation",
                    description=f"Une erreur est survenue lors de l'exécution :\n```{result}```",
                    color=discord.Color.red()
                )
                if status_msg:
                    await status_msg.edit(embed=err_embed)
                return

            # Construction du récapitulatif final
            new_files = sorted(list(summary.sent))
            nb_new = len(new_files)
            nb_skipped = len(summary.skipped)

            if nb_new > 0:
                final_embed = discord.Embed(
                    title="✨ Synchronisation terminée avec succès !",
                    description=f"**{nb_new} nouveau(x) fichier(s) téléchargé(s)** et synchronisé(s) vers OneDrive.",
                    color=discord.Color.green()
                )
                preview = "\n".join(f"• `{f}`" for f in new_files[:10])
                if len(new_files) > 10:
                    preview += f"\n*... et {len(new_files) - 10} autre(s)*"
                final_embed.add_field(name="📄 Nouveaux cours détectés", value=preview, inline=False)
            else:
                final_embed = discord.Embed(
                    title="✅ Synchronisation terminée : Tout est à jour !",
                    description="Aucun nouveau document n'a été publié sur Junia Learning.",
                    color=discord.Color.dark_green()
                )

            final_embed.add_field(name="📁 Matières scannées", value=f"{len(selected)}", inline=True)
            final_embed.add_field(name="⚡ Fichiers vérifiés", value=f"{nb_skipped}", inline=True)
            final_embed.add_field(name="⏱️ Durée", value=f"{duration}s", inline=True)
            final_embed.set_footer(text="Montage OneDrive actif sur le VPS | Fichiers disponibles immédiatement")

            if status_msg:
                await status_msg.edit(embed=final_embed)
            elif channel:
                await channel.send(embed=final_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(MoodleSyncCog(bot))
