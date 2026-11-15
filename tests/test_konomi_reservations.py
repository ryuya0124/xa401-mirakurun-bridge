import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from xa401_bridge import konomi_reservations as module


def fixture():
    service = {'networkId': 4, 'serviceId': 101, 'transportStreamId': 16625,
        'remoteControlKeyId': 1, 'name': 'Fixture BS', 'channel': {'type': 'BS'}}
    now = time.time()
    def job(ident, state, source='live'):
        return {'id': ident, 'source': source, 'source_key': 400101,
            'title': 'Fixture reservation', 'file_name': 'fixture.ts', 'status': state,
            'start_at': now + 3600, 'duration_seconds': 1800}
    jobs = {'a'*32: job('a'*32, 'scheduled'), 'b'*32: job('b'*32, 'completed'),
        'c'*32: job('c'*32, 'scheduled', 'recorded')}
    stopped = []
    def stop(ident):
        stopped.append(ident)
        jobs[ident]['status'] = 'cancelled'
    recorder = SimpleNamespace(jobs=jobs, stop=stop)
    bridge = SimpleNamespace(recorder=recorder, maximum=2,
        device=SimpleNamespace(items={400101: {'service': service}}))
    return bridge, stopped


async def test_list_detail_cancel_presets_and_unsupported_mutations(monkeypatch):
    async def upstream(request):
        return web.json_response({'BS': [{'id': 'NID4-SID101', 'display_channel_id': 'bs101',
            'network_id': 4, 'service_id': 101, 'name': 'Fixture BS', 'channel_number': '101',
            'program_present': {'should_not_be_copied': True}}]})
    backend = web.Application()
    backend.router.add_get('/api/channels', upstream)
    bridge, stopped = fixture()
    app = web.Application()
    module.install_konomi_reservation_routes(app, bridge)
    async with TestServer(backend) as remote, TestClient(TestServer(app)) as client:
        monkeypatch.setattr(module, 'UPSTREAM', str(remote.make_url('')).rstrip('/'))
        prefix = '/api/xa401/konomi/recording'
        response = await client.get(prefix + '/reservations')
        assert response.status == 200 and response.headers['Cache-Control'] == 'no-store'
        result = await response.json()
        assert result['total'] == 1
        item = result['reservations'][0]
        ident = item['id']
        assert 0 <= ident < 2**53
        assert item['channel']['display_channel_id'] == 'bs101'
        assert 'program_present' not in item['channel']
        assert item['program']['channel_id'] == item['channel']['id']
        assert item['record_settings']['is_event_relay_follow_enabled'] is False
        assert item['comment'].startswith('XA401 Bridge')
        assert (await (await client.get(prefix + f'/reservations/{ident}')).json()) == item
        preset = await (await client.get(prefix + '/presets')).json()
        assert preset['presets'][0]['name'].startswith('XA401 Bridge')
        assert (await client.put(prefix + f'/reservations/{ident}', json={})).status == 501
        assert not stopped
        assert (await client.delete(prefix + f'/reservations/{ident}')).status == 204
        assert stopped == ['a'*32]
        assert (await (await client.get(prefix + '/reservations')).json()) == {'total': 0, 'reservations': []}
        assert (await client.delete(prefix + f'/reservations/{ident}')).status == 404
        assert bridge.recorder.jobs['b'*32]['status'] == 'completed'


async def test_channel_backend_failure_uses_observed_service_identity(monkeypatch):
    monkeypatch.setattr(module, 'UPSTREAM', 'http://127.0.0.1:1')
    bridge, _ = fixture()
    app = web.Application()
    module.install_konomi_reservation_routes(app, bridge)
    async with TestClient(TestServer(app)) as client:
        result = await (await client.get('/api/xa401/konomi/recording/reservations')).json()
        assert result['reservations'][0]['channel']['id'] == 'NID4-SID101'
        assert result['reservations'][0]['channel']['channel_number'] == '---'


async def test_colliding_reservation_ids_are_not_silently_merged():
    bridge, _ = fixture()
    other = dict(bridge.recorder.jobs['a'*32], id='a'*12+'d'*20)
    bridge.recorder.jobs[other['id']] = other
    app = web.Application()
    module.install_konomi_reservation_routes(app, bridge)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get('/api/xa401/konomi/recording/reservations')).status == 503


def test_receive_slots_count_simultaneous_distinct_channels_only():
    bridge, _ = fixture()
    job = bridge.recorder.jobs['a'*32]
    other = dict(job, source_key=400102, duration_seconds=600)
    later = dict(job, source_key=400103, start_at=job['start_at'] + 1200, duration_seconds=600)
    service = bridge.device.items[400101]['service']
    def availability(items):
        return module.reservation(1, job, service, None, items, 2)['recording_availability']
    assert availability([job, other, later]) == 'Full'
    assert availability([job, other, dict(later, start_at=job['start_at'])]) == 'Partial'
    assert availability([job, dict(other, source_key=job['source_key']), later]) == 'Full'
