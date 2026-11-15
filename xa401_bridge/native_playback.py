"""Android-free desktop player endpoints, independent of KonomiTV."""
import re
import time
from pathlib import Path

from aiohttp import web

from .konomi_recordings import KonomiRecordings, duration_seconds, video_id


def install_native_routes(app, bridge):
    provider = KonomiRecordings(bridge)
    directory = Path(__file__).with_name('web')

    async def catalogue(request):
        if time.time() - bridge.device.recordings_updated > 60:
            await bridge.device.recorded_catalogue()
        recordings = []
        for item in bridge.device.recorded_summaries():
            try:
                seconds = duration_seconds(item['duration'])
            except ValueError:
                seconds = None
            recordings.append(dict(item, duration_seconds=seconds))
        return web.json_response({'protocol': 1, 'recordings': recordings,
            'channels': [{'id': str(key), 'title': value['service']['name'],
                'channel': value['service']['channel']['type']}
                for key, value in bridge.device.items.items()]}, headers={'Cache-Control': 'no-store'})

    async def recording(request):
        token = request.match_info['token']
        if not re.fullmatch('[0-9a-f]{24}', token):
            raise web.HTTPBadRequest()
        programs = await provider.remote()
        program = programs.get(video_id(bridge.config['udn'], token))
        if program is None:
            raise web.HTTPNotFound(text='Recording unavailable')
        program = await provider.precise_detail(program)
        return web.json_response({'kind': 'recorded', 'id': token, 'title': program['title'],
            'duration': program['duration'], 'stream_url': program['external_playback']['stream_url']},
            headers={'Cache-Control': 'no-store'})

    async def channel(request):
        try:
            key = int(request.match_info['channel'])
            item = bridge.device.items[key]
        except (ValueError, KeyError):
            raise web.HTTPNotFound() from None
        return web.json_response({'kind': 'live', 'id': str(key), 'title': item['service']['name'],
            'duration': None, 'stream_url': f'/xa401/api/native/channels/{key}/stream'},
            headers={'Cache-Control': 'no-store'})

    async def guide(request):
        # Read the existing observed cache only; opening this page must not acquire a tuner.
        observed = {p['networkId'] * 100000 + p['serviceId']: p for p in bridge.epg.programs()}
        channels = []
        for key, value in bridge.device.items.items():
            service = value['service']
            program = observed.get(key)
            channels.append({'id': str(key), 'name': service['name'],
                'type': service['channel']['type'], 'number': service.get('remoteControlKeyId', 0),
                'program': None if program is None else {
                    'event_id': program['eventId'], 'title': program['name'],
                    'description': program.get('description', ''),
                    'extended': program.get('extended', {}),
                    'start_at': program['startAt'], 'duration': program['duration']}})
        return web.json_response({'protocol': 1, 'generated_at': int(time.time() * 1000),
            'source': 'observed_SIT', 'future_schedule_available': False, 'channels': channels},
            headers={'Cache-Control': 'no-store'})

    async def player(request):
        return web.FileResponse(directory / 'desktop-player.html', headers={'Cache-Control': 'no-cache'})

    async def script(request):
        return web.FileResponse(directory / 'desktop-player.mjs', headers={'Cache-Control': 'no-cache'})

    app.router.add_get('/api/xa401/desktop-player', player)
    app.router.add_get('/xa401/desktop-player.mjs', script)
    app.router.add_get('/xa401/api/native/catalogue', catalogue)
    app.router.add_get('/xa401/api/native/guide', guide)
    app.router.add_get('/xa401/api/native/recordings/{token}', recording)
    app.router.add_get('/xa401/api/native/channels/{channel}', channel)
    app.router.add_get('/xa401/api/native/channels/{service_id}/stream', bridge.stream, allow_head=False)
