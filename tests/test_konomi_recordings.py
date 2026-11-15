import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from multidict import MultiDict

from xa401_bridge.konomi_recordings import (
    REMOTE_MIN, KonomiRecordings, duration_seconds, program_from_item,
    sorted_page, video_id, install_konomi_recording_routes,
)


def item(title='Observed recording'):
    return SimpleNamespace(original_resource=SimpleNamespace(
        protocol_info=SimpleNamespace(parameters={'DLNA.ORG_PN': 'MPEG_TS_HD_JP_T'}),
        attributes={'resolution': '1440x1080'}, cleartext_size=None),
        duration='00:30:12.309', date='2026-11-14T16:59:51', title=title,
        description='Observed description', long_description=None, channel_name='Observed channel')


def test_metadata_identity_duration_and_unknowns_are_not_fabricated():
    a = program_from_item('uuid:own', 'a' * 24, item())
    assert REMOTE_MIN <= a['id'] < 2**49 < 2**53
    assert a['id'] == video_id('uuid:own', 'a' * 24)
    assert a['id'] != video_id('uuid:other', 'a' * 24)
    assert a['duration'] == 1812.309
    assert a['start_time'].endswith('+09:00')
    assert a['is_free'] is None and a['recorded_video']['file_hash'] == ''
    assert a['recorded_video']['primary_audio_codec'] == '未取得'
    assert a['external_playback']['read_only'] is True
    assert a['external_playback']['original_available'] is True
    assert a['recorded_video']['file_path'].startswith('xa401://')


@pytest.mark.parametrize('duration', [None, '0:60:01', '-1:01:00', '00:00:00', '25:00:00'])
def test_invalid_duration_rejected(duration):
    with pytest.raises(ValueError):
        duration_seconds(duration)


def test_merge_pagination_search_and_ids_keep_native_programmes():
    programmes = [program_from_item('uuid:own', str(i), item(f'Show {i}')) for i in range(34)]
    native = dict(programmes[0], id=1, title='PC recording')
    all_items = [native] + programmes
    first = sorted_page(all_items, MultiDict({'order':'desc', 'page':'1'}))
    second = sorted_page(all_items, MultiDict({'order':'desc', 'page':'2'}))
    assert first['total'] == second['total'] == 35
    assert len(first['recorded_programs']) == 30 and len(second['recorded_programs']) == 5
    assert 1 in {p['id'] for p in first['recorded_programs'] + second['recorded_programs']}
    search = sorted_page(all_items, MultiDict({'query':'PC recording'}))
    assert [p['id'] for p in search['recorded_programs']] == [1]
    ids = sorted_page(all_items, MultiDict([('order','ids'),('ids',str(programmes[4]['id'])),('ids','1')]))
    assert [p['id'] for p in ids['recorded_programs']] == [programmes[4]['id'], 1]


async def test_readonly_provider_detail_and_failures_do_not_mutate_stb(tmp_path):
    device = SimpleNamespace(recordings={'a'*24:item()}, recordings_updated=time.time())
    async def resource(token):
        assert token == 'a'*24
        return SimpleNamespace(url='http://192.168.1.50/known-recording')
    device.recorded_resource = resource
    class Response:
        status = 200
        headers = {'TimeSeekRange.dlna.org': 'npt=0.000-1812.309/1812.309'}
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    def head(url, **kwargs):
        assert url == 'http://192.168.1.50/known-recording'
        assert kwargs['allow_redirects'] is False
        return Response()
    bridge = SimpleNamespace(config={'udn':'uuid:own'}, device=device, http=SimpleNamespace(head=head))
    app = web.Application()
    install_konomi_recording_routes(app, bridge)
    ident = video_id('uuid:own', 'a'*24)
    async with TestClient(TestServer(app)) as client:
        detail = await client.get(f'/api/xa401/konomi/videos/{ident}')
        assert detail.status == 200
        assert (await detail.json())['external_playback']['duration'] == 1812.309
        for method in ('post','put','delete'):
            assert (await getattr(client,method)(f'/api/xa401/konomi/videos/{ident}')).status == 405
        assert (await client.get(f'/api/xa401/konomi/videos/{ident}/download')).status == 501
        assert (await client.get(f'/api/xa401/konomi/videos/{ident+1}')).status == 404
        assert (await client.get('/xa401/native-player.mjs')).status == 200
    provider = KonomiRecordings(bridge)
    device.recordings_updated = 0
    async def failed():
        raise RuntimeError('DMS unavailable')
    device.recorded_catalogue = failed
    with pytest.raises(RuntimeError):
        await provider.remote()
