import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from xa401_bridge import server
from xa401_bridge.collector import EPGCollector


def make_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr(server, 'credentials_from_file', lambda _: object())
    monkeypatch.setattr(server, 'source_index', lambda _: None)

    async def receive(self):
        try:
            await asyncio.Event().wait()
        finally:
            for queue in tuple(self.subscribers):
                queue.put_nowait(None)

    monkeypatch.setattr(server.ChannelSession, 'run', receive)
    return server.Bridge({'credentials': 'unused', 'state_dir': str(tmp_path)})


async def test_viewer_preempts_other_channel_collector(monkeypatch, tmp_path):
    bridge = make_bridge(monkeypatch, tmp_path)
    background, bg = await bridge.subscribe(123, background=True)
    await asyncio.sleep(0)
    viewer, queue = await bridge.subscribe(456)
    assert background.task.cancelled()
    assert viewer is not background and viewer.has_viewers
    assert await bg.get() is None
    await bridge.unsubscribe(background, bg)
    assert bridge.active is viewer
    await bridge.unsubscribe(viewer, queue)


async def test_background_never_preempts_viewer_and_same_channel_shares(monkeypatch, tmp_path):
    bridge = make_bridge(monkeypatch, tmp_path)
    channel, viewer = await bridge.subscribe(123)
    with pytest.raises(web.HTTPServiceUnavailable):
        await bridge.subscribe(456, background=True)
    shared, bg = await bridge.subscribe(123, background=True)
    assert shared is channel and channel.has_viewers
    await bridge.unsubscribe(channel, bg)
    assert not channel.task.done()
    await bridge.unsubscribe(channel, viewer)


def service(sid, kind):
    return {'id': sid, 'channel': {'type': kind}}


def test_schedule_refresh_expiry_priorities_and_failure_backoff():
    bridge = SimpleNamespace(epg=SimpleNamespace(entries={}),
        device=SimpleNamespace(services=[service(3, 'CS'), service(2, 'BS'), service(1, 'GR')]))
    c = EPGCollector(bridge, {'enabled': True})
    assert c.next_service(1000) == 1
    bridge.epg.entries[1] = {'startAt': 900000, 'duration': 300000}
    c.schedule_result(1, 'success', 1000)
    assert c.due[1] == 1201
    assert c.next_service(1001) == 2
    c.schedule_result(2, 'timeout', 1001)
    assert c.due[2] == 1301
    c.schedule_result(2, 'timeout', 1302)
    assert c.due[2] == 1902
    c.schedule_result(3, 'busy', 1001)
    assert c.next_service(1002) is None
    # A terrestrial refresh that is now due should precede uncollected cable.
    assert c.next_service(1201) == 1
    assert c.current_program(1, 1200) is None


async def test_collect_requires_observed_sit_and_releases_subscription(monkeypatch):
    program = {'startAt': 900000, 'duration': 300000}
    channel = SimpleNamespace(program_observations=0, error_type=None)
    queue = asyncio.Queue()
    closed = []
    ready = asyncio.Event()

    async def subscribe(sid, *, background):
        assert background
        ready.set()
        return channel, queue

    async def unsubscribe(ch, q):
        closed.append((ch, q))

    bridge = SimpleNamespace(epg=SimpleNamespace(entries={1: program}),
                             subscribe=subscribe, unsubscribe=unsubscribe)
    c = EPGCollector(bridge, {'enabled': True})
    monkeypatch.setattr('xa401_bridge.collector.time.time', lambda: 1000)
    task = asyncio.create_task(c.collect(1))
    await ready.wait()
    queue.put_nowait(b'ts without SIT')
    await asyncio.sleep(0)
    assert not task.done()
    channel.program_observations = 1
    queue.put_nowait(b'ts with SIT')
    assert await task == 'success'
    assert len(closed) == 1 and c.completed == 1 and c.current is None


async def test_preempted_collection_and_cancel_release_receiver(monkeypatch, tmp_path):
    bridge = make_bridge(monkeypatch, tmp_path)
    bridge.epg.entries.clear()
    c = bridge.collector
    task = asyncio.create_task(c.collect(123))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    channel, queue = await bridge.subscribe(456)
    assert await task == 'preempted'
    assert c.preempted == 1 and channel.has_viewers
    await bridge.unsubscribe(channel, queue)
    task = asyncio.create_task(c.collect(123))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.active.task.done()
    assert not bridge.active.subscribers


def test_valid_current_cache_delays_collection_until_expiry():
    bridge = SimpleNamespace(epg=SimpleNamespace(entries={
        1: {'startAt': 900000, 'duration': 300000}}),
        device=SimpleNamespace(services=[service(1, 'GR')]))
    c = EPGCollector(bridge, {'enabled': True})
    assert c.next_service(1000) is None
    assert c.next_service(1201) == 1
