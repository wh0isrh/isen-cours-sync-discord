import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import server_control as app


def interaction(owner=123):
    return SimpleNamespace(user=SimpleNamespace(id=owner),
        response=SimpleNamespace(is_done=lambda:False, send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_owner_cannot_open_or_confirm_stop(self):
        controller=app.ServerControl(None,owner_id=123)
        current=interaction(456)
        with patch.object(app,'schedule_shutdown',new_callable=AsyncMock) as stop:
            await controller.arreter.callback(controller,current)
            self.assertNotIn('view',current.response.send_message.call_args.kwargs)
            view=app.StopServerView(controller)
            await view.children[0].callback(current)
            self.assertFalse(controller.scheduled)
            stop.assert_not_awaited()
            view.stop()

    async def test_empty_owner_disables_stop_for_everyone(self):
        controller=app.ServerControl(None,owner_id=0)
        self.assertFalse(controller.is_owner(interaction(0)))
        self.assertFalse(controller.is_owner(interaction(123)))

    async def test_confirmation_required_and_repeated_clicks_schedule_once(self):
        controller=app.ServerControl(None,owner_id=123)
        current=interaction()
        with patch.object(app,'schedule_shutdown',new_callable=AsyncMock) as stop:
            await controller.arreter.callback(controller,current)
            stop.assert_not_awaited()
            view=current.response.send_message.call_args.kwargs['view']
            await view.children[0].callback(interaction())
            await view.children[0].callback(interaction())
            await controller.request_shutdown()
            stop.assert_awaited_once()
            self.assertTrue(controller.scheduled)
            self.assertTrue(all(item.disabled for item in view.children))

    async def test_cancel_and_expiry_never_shutdown(self):
        controller=app.ServerControl(None,owner_id=123)
        with patch.object(app,'schedule_shutdown',new_callable=AsyncMock) as stop:
            view=app.StopServerView(controller)
            await view.children[1].callback(interaction())
            await view.children[0].callback(interaction())
            expired=app.StopServerView(controller)
            expired.message=SimpleNamespace(edit=AsyncMock())
            await expired.on_timeout()
            await expired.children[0].callback(interaction())
            stop.assert_not_awaited()
            self.assertFalse(controller.scheduled)

    async def test_failure_does_not_mark_server_as_scheduled(self):
        controller=app.ServerControl(None,owner_id=123)
        view=app.StopServerView(controller)
        with patch.object(app,'schedule_shutdown',new_callable=AsyncMock,side_effect=RuntimeError('denied')), patch.object(app.LOG,'exception'):
            await view.children[0].callback(interaction())
        self.assertFalse(controller.scheduled)
        self.assertFalse(view.closed)
        view.stop()

    async def test_fixed_command_and_no_shell(self):
        process=SimpleNamespace(communicate=AsyncMock(return_value=(b'',b'')),returncode=0)
        with patch.object(app.asyncio,'create_subprocess_exec',new_callable=AsyncMock,return_value=process) as spawn:
            await app.schedule_shutdown()
            self.assertEqual(spawn.call_args.args,('/usr/bin/sudo','-n','/sbin/shutdown','-h','+1'))
            self.assertNotIn('shell',spawn.call_args.kwargs)
            process.returncode=1
            with self.assertRaises(RuntimeError):await app.schedule_shutdown()


if __name__=='__main__':
    unittest.main()
