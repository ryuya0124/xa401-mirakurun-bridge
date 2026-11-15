import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xa401_bridge.native_playback import install_native_routes


async def test_desktop_catalogue_and_live_source_are_same_origin_without_konomitv():
    async def stream(request):
        assert request.match_info['service_id'] == '123'
        return web.Response(body=b'fixture')
    device = SimpleNamespace(recordings_updated=time.time(),
        items={123: {'service': {'name': 'Test channel', 'channel': {'type': 'GR'}}}},
        recorded_summaries=lambda: [{'id': 'a'*24, 'title': 'Test recording',
            'channel': 'Test station', 'date': None, 'duration': '00:30:12.309'}])
    app = web.Application()
    install_native_routes(app, SimpleNamespace(device=device, stream=stream))
    async with TestClient(TestServer(app)) as client:
        catalogue = await (await client.get('/xa401/api/native/catalogue')).json()
        assert catalogue['protocol'] == 1
        assert catalogue['recordings'][0]['duration_seconds'] == 1812.309
        source = await (await client.get('/xa401/api/native/channels/123')).json()
        assert source['kind'] == 'live' and source['duration'] is None
        assert source['stream_url'] == '/xa401/api/native/channels/123/stream'
        assert await (await client.get(source['stream_url'])).read() == b'fixture'
        assert (await client.get('/xa401/api/native/channels/456')).status == 404
        assert (await client.get('/api/xa401/desktop-player')).status == 200
        assert (await client.get('/xa401/desktop-player.mjs')).status == 200
        assert (await client.post('/xa401/api/native/catalogue')).status == 405
