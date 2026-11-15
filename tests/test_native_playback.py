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


async def test_native_guide_preserves_observed_times_and_unknown_channels_without_streaming():
    async def forbidden_stream(request):
        raise AssertionError('Guide must not start reception')
    program = {'networkId': 1, 'serviceId': 23, 'eventId': 9,
        'name': 'Observed <title>', 'description': 'Description', 'extended': {'Detail': 'Text'},
        'startAt': 1800000000000, 'duration': 1800000}
    device = SimpleNamespace(items={100023: {'service': {'name': 'Station', 'channel': {'type': 'GR'}, 'remoteControlKeyId': 1}},
        200023: {'service': {'name': 'Other network', 'channel': {'type': 'BS'}}}})
    bridge = SimpleNamespace(device=device, stream=forbidden_stream,
        epg=SimpleNamespace(programs=lambda: [program]))
    app = web.Application()
    install_native_routes(app, bridge)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/xa401/api/native/guide')
        result = await response.json()
        assert response.headers['Cache-Control'] == 'no-store'
        assert result['source'] == 'observed_SIT' and result['future_schedule_available'] is False
        assert result['channels'][0]['program']['start_at'] == program['startAt']
        assert result['channels'][0]['program']['title'] == 'Observed <title>'
        assert result['channels'][1]['program'] is None
        assert 'stream_url' not in str(result)
        assert (await client.post('/xa401/api/native/guide')).status == 405
