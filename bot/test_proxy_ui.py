"""Wizard/access-control tests. Uses aiogram but no Telegram/network calls."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Dispatcher
import rdnet_proxy_ui as ui


class State:
    def __init__(self):
        self.value, self.data = None, {}

    async def clear(self):
        self.value, self.data = None, {}

    async def set_state(self, value):
        self.value = value.state if value else None

    async def get_state(self):
        return self.value

    async def update_data(self, **data):
        self.data.update(data)

    async def get_data(self):
        return dict(self.data)


class Event:
    def __init__(self, text='', data='', user=1, chat='private', callback=False):
        self.from_user = SimpleNamespace(id=user)
        self.chat = SimpleNamespace(type=chat)
        self.text, self.data = text, data
        self.answer = AsyncMock()
        if callback:
            self.message = Event(user=user, chat=chat)


class WizardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dp = Dispatcher()
        self.audit = AsyncMock()
        ui.register_proxy_ui(self.dp, lambda user: user == 1, self.audit)
        self.messages = {h.callback.__name__: h.callback for h in self.dp.message.handlers}
        self.callbacks = {h.callback.__name__: h.callback for h in self.dp.callback_query.handlers}
        self.state = State()

    async def test_private_admin_only(self):
        for event in (Event(user=2), Event(chat='group')):
            await self.messages['command'](event, self.state)
            self.assertIn('فقط مدیر', event.answer.call_args.args[0])
        with patch.object(ui, 'helper', new_callable=AsyncMock) as helper:
            q = Event(data='px:create', user=2, callback=True)
            await self.callbacks['create'](q, self.state)
            helper.assert_not_called()

    async def test_wizard_custom_values_and_idempotent_create(self):
        await self.callbacks['new'](Event(data='px:new', callback=True), self.state)
        await self.messages['name'](Event(text='دفتر تهران'), self.state)
        await self.messages['volume'](Event(text='12.5'), self.state)
        await self.messages['ips'](Event(text='2'), self.state)
        await self.messages['acl'](Event(text='203.0.113.5'), self.state)
        self.assertEqual(await self.state.get_state(), ui.ProxyStates.confirm.state)
        result = {'ok': True, 'username': 'p_123456abcdef', 'password': 'test_secret', 'endpoint': {'host': 'proxy.example.com', 'port': 8443}}
        with patch.object(ui, 'helper', new_callable=AsyncMock, return_value=result) as helper:
            q = Event(data='px:create', callback=True)
            await asyncio.gather(self.callbacks['create'](q, self.state), self.callbacks['create'](q, self.state))
            helper.assert_awaited_once_with({'action': 'create', 'name': 'دفتر تهران', 'gb': '12.5', 'max_ips': 2, 'acl': '203.0.113.5'})
            self.audit.assert_awaited_once_with(1, 'proxy_create', 'p_123456abcdef')
            self.assertNotIn('test_secret', str(self.audit.call_args))
            protected = [call for call in q.message.answer.call_args_list if call.kwargs.get('protect_content')]
            self.assertEqual(len(protected), 1)
            self.assertIn('https://p_123456abcdef:test_secret@proxy.example.com:8443', protected[0].args[0])

    async def test_invalid_volume_and_ips_do_not_advance(self):
        await self.state.set_state(ui.ProxyStates.volume)
        await self.messages['volume'](Event(text='NaN'), self.state)
        self.assertEqual(await self.state.get_state(), ui.ProxyStates.volume.state)
        await self.state.set_state(ui.ProxyStates.ips)
        await self.messages['ips'](Event(text='0'), self.state)
        self.assertEqual(await self.state.get_state(), ui.ProxyStates.ips.state)

    async def test_cancel_discards_creation(self):
        await self.state.set_state(ui.ProxyStates.confirm)
        await self.callbacks['home'](Event(data='px:home', callback=True), self.state)
        with patch.object(ui, 'helper', new_callable=AsyncMock) as helper:
            await self.callbacks['create'](Event(data='px:create', callback=True), self.state)
            helper.assert_not_called()


if __name__ == '__main__':
    unittest.main()
