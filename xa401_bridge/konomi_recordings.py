"""Read-only external recording provider for KonomiTV's existing video UI.

No database insertion or placeholder TS files. Virtual IDs include the verified
device identity; unknown technical metadata remains explicitly unknown.
"""
import asyncio
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web

REMOTE_MIN = 1 << 48
UPSTREAM = 'http://127.0.0.77:7010'


def video_id(udn, token):
    return REMOTE_MIN + int(hashlib.sha256(f'{udn}:{token}'.encode()).hexdigest()[:12], 16)


def duration_seconds(value):
    if not value or not re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
        raise ValueError('Recording has no valid duration')
    h, m, s = map(float, value.split(':'))
    if m >= 60 or s >= 60 or not 0 < h * 3600 + m * 60 + s <= 86400:
        raise ValueError('Recording duration is out of bounds')
    return h * 3600 + m * 60 + s


def program_from_item(udn, token, item):
    resource = item.original_resource
    if resource is None:
        raise ValueError('Original resource missing')
    profile = resource.protocol_info.parameters.get('DLNA.ORG_PN', '')
    # Only claim MPEG-2 where the advertised original DLNA profile says so.
    codec = 'MPEG-2' if profile.startswith('MPEG_TS_') else None
    duration = duration_seconds(resource.attributes.get('duration') or item.duration)
    start = datetime.fromisoformat(item.date)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone(timedelta(hours=9)))
    begin, end = start.isoformat(), (start + timedelta(seconds=duration)).isoformat()
    ident = video_id(udn, token)
    size = resource.cleartext_size
    resolution = re.fullmatch(r'(\d+)x(\d+)', resource.attributes.get('resolution', ''))
    width, height = map(int, resolution.groups()) if resolution else (0, 0)
    external = {'module_url': '/xa401/native-player.mjs?v=1',
        'stream_url': f'/xa401/api/recorded/{token}/stream',
        'duration': duration, 'read_only': True, 'provider': 'XA401',
        # DTCP_MPEG_TS_JP_T identifies the TS transport, not an observed codec.
        # The worker validates the actual input and never falls back to HLS.
        'original_available': profile.removeprefix('DTCP_').startswith('MPEG_TS_')}
    return {'id': ident, 'title': item.title, 'series_title': None,
        'episode_number': None, 'subtitle': None,
        'description': item.description or item.long_description or '番組概要を取得できませんでした。',
        'detail': {'保存元': f'XA401（{item.channel_name or "放送局未取得"}）から直接再生',
            '配信': 'PCへの事前保存なし。DLNA時間指定読み出し。',
            '未取得の情報': 'ファイルハッシュ、正確なTS容量、映像プロファイル・走査方式・fps、音声形式、無料放送区分は未測定。',
            '対応範囲': '直接再生・時間シーク。STBの削除・再解析・オフライン保存は非対応。'},
        'start_time': begin, 'end_time': end, 'duration': duration,
        'channel': None, 'network_id': None, 'service_id': None, 'event_id': None,
        'series_id': None, 'series_broadcast_period_id': None, 'is_free': None,
        'genres': [], 'primary_audio_type': '未取得', 'primary_audio_language': '未取得',
        'secondary_audio_type': None, 'secondary_audio_language': None,
        'recording_start_margin': 0, 'recording_end_margin': 0,
        'is_partially_recorded': False, 'created_at': begin, 'updated_at': begin,
        'external_playback': external,
        'recorded_video': {'id': ident, 'status': 'Recorded',
            'file_path': f'xa401://{token}/{item.title}', 'file_hash': '',
            'file_size': size or 0, 'file_created_at': begin, 'file_modified_at': begin,
            'recording_start_time': begin, 'recording_end_time': end, 'duration': duration,
            'container_format': 'MPEG-TS', 'video_codec': codec or '未取得',
            'video_codec_profile': '未取得', 'video_scan_type': '未取得',
            'video_frame_rate': 0, 'video_resolution_width': width,
            'video_resolution_height': height, 'has_video_stream_changes': False,
            'primary_audio_codec': '未取得', 'primary_audio_channel': '未取得',
            'primary_audio_sampling_rate': 0, 'secondary_audio_codec': None,
            'secondary_audio_channel': None, 'secondary_audio_sampling_rate': None,
            'cm_sections': None, 'thumbnail_info': None, 'created_at': begin, 'updated_at': begin}}


def sorted_page(items, query):
    order = query.get('order', 'desc')
    if order not in ('asc', 'desc', 'file_path_asc', 'file_path_desc', 'ids'):
        raise ValueError('Invalid order')
    page = int(query.get('page', '1'))
    if not 1 <= page <= 100000:
        raise ValueError('Invalid page')
    if order == 'ids':
        ids = [int(value) for value in query.getall('ids', [])]
        if len(ids) > 100:
            raise ValueError('Too many IDs')
        lookup = {p['id']: p for p in items}
        items = [lookup[ident] for ident in dict.fromkeys(ids) if ident in lookup]
    else:
        terms = query.get('query', '').casefold().split()
        items = [p for p in items if all(t in (p['title'] + '\n' + p['description']).casefold() for t in terms)]
        def natural(text):
            return [(0, int(s)) if s.isdecimal() else (1, s.casefold()) for s in re.split(r'(\d+)', text)]
        key = (lambda p: natural(p['recorded_video']['file_path'])) if order.startswith('file_path') else (lambda p: (datetime.fromisoformat(p['start_time']).timestamp(), p['id']))
        items = sorted(items, key=key, reverse=order.endswith('desc'))
    return {'total': len(items), 'recorded_programs': items[(page - 1) * 30:page * 30]}


class KonomiRecordings:
    def __init__(self, bridge):
        self.bridge = bridge
        self.lock = asyncio.Lock()
        self.native_cache = None
        self.native_at = 0
        self.precise_durations = {}

    async def precise_detail(self, program):
        """Read the fractional total from a fresh, identity-checked HEAD (no AKE)."""
        token = program['external_playback']['stream_url'].split('/')[-2]
        duration = self.precise_durations.get(token)
        if duration is None:
            resource = await self.bridge.device.recorded_resource(token)
            async with self.bridge.http.head(resource.url, allow_redirects=False,
                    headers={'TimeSeekRange.dlna.org': 'npt=0.000-',
                        'getcontentFeatures.dlna.org': '1', 'transferMode.dlna.org': 'Streaming',
                        'Accept-Encoding': 'identity'}, timeout=aiohttp.ClientTimeout(total=8)) as response:
                if response.status != 200:
                    raise web.HTTPBadGateway(text='XA401 recording metadata probe failed')
                value = response.headers.get('TimeSeekRange.dlna.org', '')
            match = re.fullmatch(r'npt=[0-9.]+-[0-9.]+/([0-9]+(?:\.[0-9]+)?)(?:\s.*)?', value)
            if not match or not 0 < float(match[1]) <= 86400:
                raise web.HTTPBadGateway(text='XA401 did not return the complete recording duration')
            duration = float(match[1])
            # Bound cache to the current read-only catalogue.
            self.precise_durations = {key: value for key, value in self.precise_durations.items()
                if key in self.bridge.device.recordings}
            self.precise_durations[token] = duration
        program['duration'] = program['recorded_video']['duration'] = duration
        program['external_playback']['duration'] = duration
        end = (datetime.fromisoformat(program['start_time']) + timedelta(seconds=duration)).isoformat()
        program['end_time'] = program['recorded_video']['recording_end_time'] = end
        return program

    async def remote(self):
        if time.time() - self.bridge.device.recordings_updated > 60:
            await self.bridge.device.recorded_catalogue()
        programs = {}
        for token, item in self.bridge.device.recordings.items():
            try:
                program = program_from_item(self.bridge.config['udn'], token, item)
            except (ValueError, TypeError):
                # Do not fabricate a duration/date/codec for an unsupported item.
                continue
            if program['id'] in programs:
                raise RuntimeError('External recording identity collision')
            programs[program['id']] = program
        return programs

    async def native(self, client):
        async with self.lock:
            if self.native_cache is not None and time.monotonic() - self.native_at < 10:
                return self.native_cache
            items = []
            for page in range(1, 335):
                async with client.get(UPSTREAM + '/api/videos', params={'page': page}) as response:
                    if response.status != 200:
                        raise web.HTTPBadGateway(text='KonomiTV recording catalogue is unavailable')
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 8 * 1024 * 1024:
                            raise web.HTTPBadGateway(text='KonomiTV catalogue response is too large')
                    import json
                    result = json.loads(body)
                batch = result['recorded_programs']
                if any(p['id'] >= REMOTE_MIN for p in batch):
                    raise RuntimeError('Native recording ID collides with external namespace')
                items.extend(batch)
                if len(items) >= result['total']:
                    break
                if not batch:
                    raise web.HTTPBadGateway(text='KonomiTV catalogue pagination changed')
            else:
                raise web.HTTPBadGateway(text='Native recording catalogue exceeds the bounded adapter limit')
            self.native_cache, self.native_at = items, time.monotonic()
            return items


def install_konomi_recording_routes(app, bridge):
    provider = KonomiRecordings(bridge)

    async def handler(request):
        tail = request.match_info.get('tail', '').strip('/')
        try:
            remote = await provider.remote()
            if not tail or tail == 'search':
                async with aiohttp.ClientSession(trust_env=False,
                        timeout=aiohttp.ClientTimeout(total=30, connect=3)) as client:
                    native = await provider.native(client)
                return web.json_response(sorted_page(native + list(remote.values()), request.query),
                    headers={'Cache-Control': 'no-store'})
            parts = tail.split('/')
            ident = int(parts[0])
            if ident not in remote:
                raise web.HTTPNotFound(text='External recording is unavailable')
            if len(parts) == 1:
                return web.json_response(await provider.precise_detail(remote[ident]), headers={'Cache-Control': 'no-store'})
            if parts[1] in ('thumbnail', 'thumbnail-tile'):
                # Honest placeholder, not an invented programme image.
                return web.Response(text='<svg xmlns="http://www.w3.org/2000/svg" width="480" height="270"><rect width="480" height="270" fill="#30221f"/><text x="240" y="135" text-anchor="middle" fill="#dbb" font-size="28">XA401</text></svg>',
                    content_type='image/svg+xml', headers={'Cache-Control': 'no-cache'})
            if parts[1] == 'jikkyo':
                return web.json_response({'is_success': False, 'comments': [], 'detail': 'XA401録画の実況ログは未対応です。'})
            raise web.HTTPNotImplemented(text='External recordings support direct playback/time seek only')
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest(text='Invalid recording metadata or query') from None

    async def readonly(request):
        raise web.HTTPMethodNotAllowed(request.method, ['GET', 'HEAD'], text='XA401 recordings are read-only')

    app.router.add_get('/api/xa401/konomi/videos', handler)
    app.router.add_get('/api/xa401/konomi/videos/{tail:.*}', handler)
    app.router.add_route('*', '/api/xa401/konomi/videos/{tail:.*}', readonly)

    async def module(request):
        from pathlib import Path
        return web.FileResponse(Path(__file__).with_name('web') / 'native-player.mjs', headers={'Cache-Control': 'no-cache'})
    app.router.add_get('/xa401/native-player.mjs', module)
