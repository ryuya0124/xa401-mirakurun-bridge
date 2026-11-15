import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xa401_bridge import server


async def test_mirakurun_subset_uses_catalogue_without_fabricating_epg(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'credentials_from_file', lambda _: object())
    monkeypatch.setattr(server, 'source_index', lambda _: None)

    async def start(self, app):
        self.device = SimpleNamespace(services=[{'id': 123, 'name': 'Observed channel'}],
            items={123: {'current_title': 'Observed title'}})

    monkeypatch.setattr(server.Bridge, 'start', start)
    app = server.create_app({'credentials': 'unused', 'state_dir': str(tmp_path)})
    async with TestClient(TestServer(app)) as client:
        assert await (await client.get('/api/services')).json() == [{'id': 123, 'name': 'Observed channel'}]
        assert await (await client.get('/api/programs')).json() == []
        assert (await (await client.get('/api/tuners')).json())[0]['isFree'] is True
        assert (await (await client.get('/health')).json())['maximum_distinct_channels'] == 1
        assert (await client.get('/api/services/999/stream')).status == 404
        assert (await client.post('/api/services')).status == 405


async def test_same_channel_shared_other_channel_busy_and_last_client_cancels(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'credentials_from_file', lambda _: object())
    monkeypatch.setattr(server, 'source_index', lambda _: None)
    started = 0

    async def receive(self):
        nonlocal started
        started += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.ready.set()

    monkeypatch.setattr(server.ChannelSession, 'run', receive)
    bridge = server.Bridge({'credentials': 'unused', 'state_dir': str(tmp_path)})
    first, a = await bridge.subscribe(123)
    second, b = await bridge.subscribe(123)
    await asyncio.sleep(0)
    assert first is second and started == 1
    with pytest.raises(web.HTTPServiceUnavailable):
        await bridge.subscribe(456)
    await bridge.unsubscribe(first, a)
    assert not first.task.done()
    await bridge.unsubscribe(first, b)
    assert first.task.cancelled()
    third, c = await bridge.subscribe(456)
    assert third is not first
    await bridge.unsubscribe(third, c)
