from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xa401_bridge.recorded_playback import install_playback_routes
from xa401_bridge.session import seek_for_key
from pydtcpip import DTCPClient


def test_seek_model_matches_upstream_dtcp_header_contract():
    selected = seek_for_key('recorded:' + 'a' * 24 + '@600.125#' + 'b' * 32)
    assert selected.start_ms == 600125
    assert DTCPClient._request_headers(None, time_range=selected)['TimeSeekRange.dlna.org'] == 'npt=600.125-'
    assert seek_for_key(123) is None
    assert seek_for_key('recorded:' + 'a' * 24 + '@0#' + 'b' * 32) is None


async def test_direct_recorded_playback_selects_known_source_and_time_without_files(tmp_path):
    selected = []

    async def catalogue():
        return []

    async def subscribe(key):
        selected.append(key)
        return object(), object()

    async def unsubscribe(*args):
        pass

    async def deliver(*args):
        return web.Response(body=b'test fixture')

    bridge = SimpleNamespace(device=SimpleNamespace(recordings={'a' * 24: object()},
        recorded_catalogue=catalogue), subscribe=subscribe, unsubscribe=unsubscribe, deliver=deliver)
    app = web.Application()
    install_playback_routes(app, bridge)
    path = '/xa401/api/recorded/' + 'a' * 24 + '/stream?player=' + 'b' * 32
    async with TestClient(TestServer(app)) as client:
        assert (await client.get(path + '&position=600')).status == 200
        assert selected == ['recorded:' + 'a' * 24 + '@600.000#' + 'b' * 32]
        assert (await client.get(path + '&position=nan')).status == 400
        assert (await client.get(path + '&position=-1')).status == 400
        assert (await client.get(path.replace('a' * 24, 'c' * 24))).status == 404
        assert (await client.get(path.replace('b' * 32, 'bad'))).status == 400
        assert (await client.head(path)).status == 405
        assert (await client.get('/xa401/player-lib/index.js')).status == 200
    assert list(tmp_path.iterdir()) == []
