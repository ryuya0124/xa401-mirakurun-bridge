"""Read PC recording reservations in KonomiTV's schema, without an EDCB server.

STB reservations and completed exports are not PC reservations. Creation/settings
remain on the Bridge dashboard; cancellation stops only this PC's recording job.
"""
import json
import re
from datetime import datetime, timezone

import aiohttp
from aiohttp import web

from .konomi_recordings import UPSTREAM

ACTIVE = {'scheduled', 'waiting', 'recording', 'stopping', 'finalizing', 'disabled'}
DEFAULTS = {'is_enabled': True, 'priority': 2, 'recording_folders': [],
    'recording_start_margin': 0, 'recording_end_margin': 0, 'recording_mode': 'SpecifiedService',
    'caption_recording_mode': 'Default', 'data_broadcasting_recording_mode': 'Default',
    'post_recording_mode': 'Nothing', 'post_recording_bat_file_path': None,
    'is_event_relay_follow_enabled': False, 'is_exact_recording_enabled': False,
    'is_oneseg_separate_output_enabled': False, 'is_sequential_recording_in_single_file_enabled': False,
    'forced_tuner_id': None}


def pending_jobs(recorder):
    result = {}
    for job in recorder.jobs.values():
        if job['source'] != 'live' or job['status'] not in ACTIVE:
            continue
        if not re.fullmatch('[a-f0-9]{32}', job['id']):
            raise ValueError('Invalid stored job ID')
        ident = int(job['id'][:12], 16)  # Stable and exactly representable by JavaScript Number.
        if ident in result:
            raise ValueError('Reservation identity collision')
        result[ident] = job
    return result


def reservation(ident, job, service, channel, jobs, maximum):
    nid, sid = service['networkId'], service['serviceId']
    channel_id = f'NID{nid}-SID{sid:03}'
    if channel is None:
        channel = {'id': channel_id, 'display_channel_id': channel_id,
            'network_id': nid, 'service_id': sid, 'transport_stream_id': service.get('transportStreamId'),
            'remocon_id': service.get('remoteControlKeyId', 0), 'channel_number': '---',
            'type': service['channel']['type'], 'name': service['name'], 'terrestrial_regions': None,
            'jikkyo_force': None, 'is_subchannel': False, 'is_radiochannel': False, 'is_watchable': True}
    else:
        channel = {k: v for k, v in channel.items() if k not in {'program_present', 'program_following'}}
        channel_id = channel['id']
    start, duration = job['start_at'], job['duration_seconds']
    end = start + duration
    iso = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat()
    # These are the requested PC recording times, not invented broadcast EPG timestamps.
    program = {'id': f'{channel_id}-EID65535', 'channel_id': channel_id,
        'network_id': nid, 'service_id': sid, 'event_id': 65535,
        'title': job['title'], 'description': 'XA401 BridgeのPC録画予約。時刻はPCでの録画予定です。',
        'detail': {}, 'start_time': iso(start), 'end_time': iso(end), 'duration': duration,
        'is_free': False, 'genres': [], 'video_type': None, 'video_codec': None, 'video_resolution': None,
        'primary_audio_type': '不明', 'primary_audio_language': '不明', 'primary_audio_sampling_rate': '不明',
        'secondary_audio_type': None, 'secondary_audio_language': None, 'secondary_audio_sampling_rate': None}
    # Future viewers may use these shared receive slots too. This is not a recording guarantee.
    events = []
    for other in jobs:
        if other['status'] == 'disabled':
            continue
        left, right = max(start, other['start_at']), min(end, other['start_at'] + other['duration_seconds'])
        if left < right:
            events.extend(((left, 1, other['source_key']), (right, -1, other['source_key'])))
    counts, peak = {}, 0
    for _, change, key in sorted(events):
        counts[key] = counts.get(key, 0) + change
        if counts[key] == 0:
            del counts[key]
        peak = max(peak, len(counts))
    available = 'Partial' if peak > maximum or job['status'] == 'waiting' else 'Full'
    return {'id': ident, 'channel': channel, 'program': program,
        'is_recording_in_progress': job['status'] in {'recording', 'stopping', 'finalizing'},
        'recording_availability': available, 'comment': 'XA401 Bridge → PC（本体予約ではありません）',
        'scheduled_recording_file_name': job['file_name'], 'estimated_recording_file_size': 0,
        'record_settings': dict(DEFAULTS, is_enabled=job['status'] != 'disabled')}


def install_konomi_reservation_routes(app, bridge):
    async def channels():
        try:
            async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=8, connect=3)) as client:
                async with client.get(UPSTREAM + '/api/channels', allow_redirects=False) as response:
                    if response.status != 200:
                        return {}
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > 8 * 1024 * 1024:
                            return {}
                    groups = json.loads(data)
            return {(c['network_id'], c['service_id']): c for group in groups.values() for c in group}
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    async def listing(request):
        try:
            jobs = pending_jobs(bridge.recorder)
        except ValueError:
            raise web.HTTPServiceUnavailable(text='Reservation identity is invalid') from None
        metadata = await channels() if jobs else {}
        items = []
        for ident, job in jobs.items():
            item = bridge.device.items.get(job['source_key'])
            if item is None:
                raise web.HTTPServiceUnavailable(text='Reserved channel is currently unavailable')
            s = item['service']
            items.append(reservation(ident, job, s, metadata.get((s['networkId'], s['serviceId'])),
                jobs.values(), bridge.maximum))
        items.sort(key=lambda p: (p['program']['start_time'], p['id']))
        tail = request.match_info.get('ident')
        if tail is not None:
            item = next((p for p in items if str(p['id']) == tail), None)
            if item is None:
                return web.json_response({'detail': 'Specified reservation_id was not found'}, status=404)
            return web.json_response(item, headers={'Cache-Control': 'no-store'})
        return web.json_response({'total': len(items), 'reservations': items}, headers={'Cache-Control': 'no-store'})

    async def cancel(request):
        try:
            job = pending_jobs(bridge.recorder)[int(request.match_info['ident'])]
        except (ValueError, KeyError):
            return web.json_response({'detail': 'Specified reservation_id was not found'}, status=404)
        bridge.recorder.stop(job['id'])
        return web.Response(status=204)

    async def presets(request):
        return web.json_response({'global_defaults': {'recording_start_margin': 0, 'recording_end_margin': 0,
            'caption_recording_mode': 'Enable', 'data_broadcasting_recording_mode': 'Enable',
            'post_recording_mode': 'Nothing'},
            'presets': [{'id': 0, 'name': 'XA401 Bridge（PC録画）', 'record_settings': DEFAULTS}]},
            headers={'Cache-Control': 'no-store'})

    async def unsupported(request):
        return web.json_response({'detail': '予約の作成・設定変更は /api/xa401/recordings のPC録画管理画面をご利用ください。'}, status=501)

    prefix = '/api/xa401/konomi/recording'
    for slash in ('', '/'):
        app.router.add_get(prefix + '/reservations' + slash, listing)
        app.router.add_get(prefix + '/reservations/{ident:[0-9]+}' + slash, listing)
        app.router.add_delete(prefix + '/reservations/{ident:[0-9]+}' + slash, cancel)
        app.router.add_get(prefix + '/presets' + slash, presets)
    app.router.add_route('*', prefix + '/{tail:.*}', unsupported)
