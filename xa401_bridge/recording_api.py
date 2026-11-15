"""Private recording dashboard/API, separate from the Mirakurun/KonomiTV API."""
import time
from pathlib import Path

from aiohttp import web


def install_recording_routes(app, bridge):
    recorder = bridge.recorder

    async def dashboard(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'recordings.html', headers={'Cache-Control': 'no-cache'})

    async def script(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'recordings.js', headers={'Cache-Control': 'no-cache'})

    async def original(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'original.html', headers={'Cache-Control': 'no-cache'})

    async def timeline(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'timeline.mjs', headers={'Cache-Control': 'no-cache'})

    async def playback_session(request):
        return web.FileResponse(Path(__file__).with_name('web') / 'playback-session.mjs', headers={'Cache-Control': 'no-cache'})

    async def overview(request):
        return web.json_response({'enabled': recorder.enabled,
            'jobs': sorted(recorder.jobs.values(), key=lambda j: j['created_at'], reverse=True),
            'channels': [{'id': key, 'name': value['service']['name'],
                          'title': value['current_title']} for key, value in bridge.device.items.items()],
            'programs': bridge.epg.programs(), 'receive_slots': bridge.maximum})

    async def recorded(request):
        if time.time() - bridge.device.recordings_updated > 60:
            await bridge.device.recorded_catalogue()
        return web.json_response(bridge.device.recorded_summaries())

    async def create(request):
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object')
            job = recorder.create(data)
        except (ValueError, KeyError, TypeError):
            raise web.HTTPBadRequest(text='Invalid source, time, duration, or recording configuration') from None
        return web.json_response(job, status=201)

    async def stop(request):
        try:
            return web.json_response(recorder.stop(request.match_info['job']))
        except KeyError:
            raise web.HTTPNotFound() from None

    async def download(request):
        try:
            job = recorder.jobs[request.match_info['job']]
            if job['status'] not in ('completed', 'stopped'):
                raise KeyError
            path = recorder.path(job)
        except (KeyError, ValueError):
            raise web.HTTPNotFound() from None
        return web.FileResponse(path, headers={'Content-Type': 'video/MP2T', 'Cache-Control': 'no-store'})

    app.router.add_get('/xa401/recordings', dashboard)
    # KonomiTV's PWA navigation fallback excludes /api/. Keep this independent
    # page reachable even when its service worker already owns the same origin.
    app.router.add_get('/api/xa401/recordings', dashboard)
    app.router.add_get('/api/xa401/original', original)
    app.router.add_get('/xa401/recordings.js', script)
    app.router.add_get('/xa401/timeline.mjs', timeline)
    app.router.add_get('/xa401/playback-session.mjs', playback_session)
    app.router.add_get('/xa401/api/recording', overview)
    app.router.add_get('/xa401/api/recorded', recorded)
    app.router.add_post('/xa401/api/jobs', create)
    app.router.add_post('/xa401/api/jobs/{job}/stop', stop)
    app.router.add_get('/xa401/api/jobs/{job}/file', download)
