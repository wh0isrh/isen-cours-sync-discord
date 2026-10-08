import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "moodle_sync"))

import discord
import moodle_sync_bot as bot_module


class TestSyncCancelView(unittest.TestCase):
    def test_view_initialization(self):
        event = threading.Event()
        view = bot_module.SyncCancelView(author_id=12345, cancel_event=event)
        self.assertEqual(view.author_id, 12345)
        self.assertFalse(event.is_set())
        self.assertEqual(len(view.children), 1)
        btn = view.children[0]
        self.assertIn("Annuler", btn.label)

    def test_cancel_button_forbidden_for_other_user(self):
        event = threading.Event()
        view = bot_module.SyncCancelView(author_id=12345, cancel_event=event)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 99999
        interaction.guild = None
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        btn = view.children[0]
        asyncio.run(btn.callback(interaction))

        self.assertFalse(event.is_set())
        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Seule la personne", interaction.response.send_message.call_args[0][0])

    def test_cancel_button_allowed_for_author(self):
        event = threading.Event()
        view = bot_module.SyncCancelView(author_id=12345, cancel_event=event)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 12345
        interaction.guild = None
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()

        btn = view.children[0]
        asyncio.run(btn.callback(interaction))

        self.assertTrue(event.is_set())
        self.assertTrue(btn.disabled)
        self.assertIn("Annulation", btn.label)
        interaction.response.edit_message.assert_awaited_once()

    def test_cancel_button_allowed_for_admin(self):
        event = threading.Event()
        view = bot_module.SyncCancelView(author_id=12345, cancel_event=event)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 99999
        interaction.guild = MagicMock()
        interaction.user.guild_permissions = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()

        btn = view.children[0]
        asyncio.run(btn.callback(interaction))

        self.assertTrue(event.is_set())
        self.assertTrue(btn.disabled)
        interaction.response.edit_message.assert_awaited_once()

    def test_cancel_button_triggers_on_cancel(self):
        event = threading.Event()
        on_cancel_mock = AsyncMock()
        view = bot_module.SyncCancelView(author_id=12345, cancel_event=event, on_cancel=on_cancel_mock)

        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()
        interaction.user.id = 12345
        interaction.guild = None
        interaction.response = MagicMock()
        interaction.response.edit_message = AsyncMock()

        btn = view.children[0]
        asyncio.run(btn.callback(interaction))

        self.assertTrue(event.is_set())
        on_cancel_mock.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
