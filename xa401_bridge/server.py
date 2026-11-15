"""Small Mirakurun API subset for KonomiTV. No dependency on KonomiTV code."""
import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from pydtcpip import DeviceStore

from . import __version__
from .device import Device, bound_socket, lan_url, source_index
from .receiver import Receiver, TSNormalizer, credentials_from_file
from .lan_proxy import start_proxy
from .epg import ProgramCache, SITParser
from .collector import EPGCollector

LOG = logging.getLogger('xa401_bridge')


class ChannelSession:
    def __init__(self, bridge, service_id):
        self.bridge, self.service_id = bridge, service_id
        self.subscribers = set()
        self.background_subscribers = set()
        self.ready = asyncio.Event()
        self.task = None
        self.error_type = None
        self.bytes_sent = 0
        self.authentications = 0
        self.program_observations = 0
        self.started = time.time()

    async def run(self):
        try:
            device = self.bridge.device
            resource = await device.resource(self.service_id)
            expected_host, expected_port = lan_url(resource.url, device.host)
            normalizer = TSNormalizer()
            sit = SITParser(device.items[self.service_id]['service'])
            authenticated = False

            async def event(value):
                nonlocal authenticated
                if value.type == 'media_info_received':
                    if value.media_info.host != expected_host:
                        raise ValueError('Authentication endpoint left the verified XA401')
                if value.type == 'authentication_started':
                    if value.is_move or value.host != expected_host:
                        raise ValueError('Unexpected authentication destination/operation')
                if value.type == 'authentication_completed':
                    authenticated = True
                    self.authentications += 1
                    LOG.info('Authentication completed for service %s', self.service_id)

            async def data(chunk):
                if not authenticated:
                    raise ValueError('Media received before authentication completion')
                ts = normalizer.feed(chunk)
                if not ts:
                    return
                self.bytes_sent += len(ts)
                for program in sit.feed(ts):
                    self.bridge.epg.update(program)
                    self.program_observations += 1
                self.ready.set()
                # Bound per-client memory; a stalled reader cannot block other viewers.
                for queue in tuple(self.subscribers):
                    if queue.full():
                        self.subscribers.discard(queue)
                        self.background_subscribers.discard(queue)
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(None)
                    else:
                        queue.put_nowait(ts)
                if not self.subscribers:
                    raise asyncio.CancelledError

            async def request_start(session, context, params):
                if params.method not in ('HEAD', 'GET') or lan_url(str(params.url), expected_host)[1] != expected_port:
                    raise ValueError('Streaming request left the selected media endpoint')

            trace = aiohttp.TraceConfig()
            trace.on_request_start.append(request_start)
            connector = self.bridge.connector()
            async with aiohttp.ClientSession(trust_env=False, connector=connector,
                                             trace_configs=[trace]) as session:
                store = self.bridge.store
                async with Receiver(self.bridge.credentials, source=self.bridge.source,
                                    interface_index=self.bridge.index, http_session=session, timeout=15,
                                    srm=store.load_srm(), on_srm_update=store.save_srm) as client:
                    await client.stream(resource, data, on_event=event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error_type = type(exc).__name__
            # Never serialize arbitrary DTCP exceptions, Pydantic inputs or credentials.
            LOG.error('Service %s failed (%s)', self.service_id, self.error_type)
        finally:
            self.ready.set()
            for queue in tuple(self.subscribers):
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)
            self.bridge.last_result = self.status()
            LOG.info('Stream stopped: service=%s bytes=%s auth=%s error=%s',
                     self.service_id, self.bytes_sent, self.authentications, self.error_type)

    def status(self):
        return {'service_id': self.service_id, 'authentication_completions': self.authentications,
                'ts_bytes': self.bytes_sent, 'clients': len(self.subscribers),
                'viewers': len(self.subscribers - self.background_subscribers),
                'background_clients': len(self.background_subscribers),
                'started_at': self.started, 'error_type': self.error_type}

    @property
    def has_viewers(self):
        return bool(self.subscribers - self.background_subscribers)


class Bridge:
    def __init__(self, config):
        self.config = config
        self.credentials = credentials_from_file(config['credentials'])
        self.source = config.get('source')
        self.index = source_index(self.source)
        self.store = DeviceStore(Path(config['state_dir']) / 'dtcp')
        self.sessions = {}
        self.last_active = None
        self.maximum = int(config.get('max_streams', 1))
        if self.maximum not in (1, 2):
            raise ValueError('max_streams must be 1 or 2; this is a bridge limit, not a tuner count')
        if self.maximum == 2 and config.get('quality', 'original') != 'original':
            raise ValueError('Two-stream operation requires original quality; concurrent transcoding is unverified')
        self.last_result = None
        self.lock = asyncio.Lock()
        self.http = None
        self.refresh_task = None
        self.close_lan_proxy = None
        self.epg = ProgramCache(config['state_dir'], config.get('udn', ''))
        self.epg_task = None
        self.collector = EPGCollector(self, config.get('epg_collection', {}))
        self.collector_task = None

    @property
    def live_sessions(self):
        return [s for s in self.sessions.values() if s.task and not s.task.done()]

    @property
    def active(self):
        # Compatibility for health consumers: prefer a viewing session.
        live = self.live_sessions
        return next((s for s in live if s.has_viewers), live[0] if live else self.last_active)

    def connector(self):
        if self.source:
            return aiohttp.TCPConnector(socket_factory=lambda ai: bound_socket(
                ai[0], ai[1], ai[2], self.source, self.index))
        return aiohttp.TCPConnector()

    async def start(self, app):
        self.http = aiohttp.ClientSession(connector=self.connector(), trust_env=False)
        self.device = Device(self.config, self.http)
        try:
            await self.device.refresh()
        except BaseException:
            await self.http.close()
            raise
        LOG.info('Verified XA401; %s live services', len(self.device.services))
        if self.config.get('lan_https'):
            self.close_lan_proxy = await start_proxy(self.config['lan_https'], self.index)
            LOG.info('Interface-bound LAN HTTPS frontend is ready')
        self.refresh_task = asyncio.create_task(self.refresh_loop())
        self.epg_task = asyncio.create_task(self.persist_epg())
        if self.collector.enabled:
            self.collector_task = asyncio.create_task(self.collector.run())

    async def persist_epg(self):
        while True:
            await asyncio.sleep(10)
            if self.epg.dirty:
                snapshot = self.epg.snapshot()
                self.epg.dirty = False
                try:
                    await asyncio.to_thread(self.epg.save, snapshot)
                except OSError:
                    self.epg.dirty = True
                    LOG.warning('Could not persist the observed program cache; will retry')

    async def refresh_loop(self):
        while True:
            await asyncio.sleep(300)
            # Avoid extra SOAP traffic during an active stream.
            if self.live_sessions:
                continue
            try:
                await self.device.refresh()
            except Exception as exc:
                LOG.warning('Catalogue refresh failed (%s); keeping last catalogue', type(exc).__name__)

    async def close(self, app):
        if self.close_lan_proxy:
            await self.close_lan_proxy()
        for task in (self.collector_task, self.refresh_task, self.epg_task,
                     *(s.task for s in self.live_sessions)):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.http:
            await self.http.close()
        if self.epg.dirty:
            await asyncio.to_thread(self.epg.save, self.epg.snapshot())

    async def subscribe(self, service_id, *, background=False):
        async with self.lock:
            self.sessions = {key: s for key, s in self.sessions.items() if not s.task.done()}
            channel = self.sessions.get(service_id)
            if channel is None and len(self.sessions) >= self.maximum:
                victim = next((s for s in self.sessions.values() if not s.has_viewers), None)
                if background or victim is None:
                    raise web.HTTPServiceUnavailable(text='All configured bridge receive slots are in use')
                # A collector yields its slot to a viewer, never the reverse.
                victim.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await victim.task
                self.sessions.pop(victim.service_id, None)
            if channel is None:
                channel = ChannelSession(self, service_id)
                self.sessions[service_id] = channel
            self.last_active = channel
            queue = asyncio.Queue(maxsize=32)
            channel.subscribers.add(queue)
            if background:
                channel.background_subscribers.add(queue)
            if channel.task is None:
                channel.task = asyncio.create_task(channel.run())
            return channel, queue

    async def unsubscribe(self, channel, queue):
        async with self.lock:
            channel.subscribers.discard(queue)
            channel.background_subscribers.discard(queue)
            if not channel.subscribers and channel.task and not channel.task.done():
                channel.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await channel.task

    async def stream(self, request):
        try:
            service_id = int(request.match_info['service_id'])
        except ValueError:
            raise web.HTTPNotFound()
        if service_id not in self.device.items:
            raise web.HTTPNotFound(text='Unknown service')
        channel, queue = await self.subscribe(service_id)
        try:
            try:
                await asyncio.wait_for(channel.ready.wait(), timeout=35)
            except TimeoutError:
                raise web.HTTPGatewayTimeout(text='XA401 did not produce a stream')
            if channel.error_type or not channel.bytes_sent:
                raise web.HTTPBadGateway(text='XA401 reception failed; inspect the local bridge log')
            response = web.StreamResponse(headers={'Content-Type': 'video/MP2T',
                'Cache-Control': 'no-store', 'X-XA401-Bridge': __version__, 'X-Content-Type-Options': 'nosniff'})
            await response.prepare(request)
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                async with asyncio.timeout(10):
                    await response.write(chunk)
            return response
        except (ConnectionError, TimeoutError):
            return response if 'response' in locals() else web.Response(status=504)
        finally:
            await self.unsubscribe(channel, queue)


def create_app(config):
    bridge = Bridge(config)
    app = web.Application(client_max_size=4096)
    app[web.AppKey('bridge', Bridge)] = bridge
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.close)

    async def services(request):
        return web.json_response(bridge.device.services)

    async def programs(request):
        return web.json_response(bridge.epg.programs())

    async def tuners(request):
        viewing = [s for s in bridge.live_sessions if s.has_viewers]
        return web.json_response([{'index': i, 'name': f'XA401 bridge receive slot {i + 1}',
            'types': ['GR', 'BS', 'CS', 'CATV'], 'isAvailable': True, 'isFree': i >= len(viewing),
            'isRemote': False, 'users': [] if i >= len(viewing) else
                [{'id': f'xa401-bridge-{viewing[i].service_id}', 'priority': 0}]}
            for i in range(bridge.maximum)])

    async def version(request):
        return web.json_response({'current': __version__, 'name': 'xa401-mirakurun-bridge',
                                  'compatibleApi': 'Mirakurun subset'})

    async def health(request):
        return web.json_response({'status': 'ok', 'services': len(bridge.device.services),
            'active': bridge.active.status() if bridge.active and not bridge.active.task.done() else None,
            'streams': [s.status() for s in bridge.live_sessions],
            'last_stream': bridge.last_result, 'epg': 'observed_SIT',
            'epg_collection': bridge.collector.status(),
            'cached_programs': len(bridge.epg.programs()), 'maximum_distinct_channels': bridge.maximum})

    async def current(request):
        # DMS titles are genuine observations, but have no reliable schedule timestamps.
        return web.json_response([{'serviceId': k, 'title': v['current_title']} for k, v in bridge.device.items.items()])

    app.router.add_get('/api/services', services)
    app.router.add_get('/api/programs', programs)
    app.router.add_get('/api/tuners', tuners)
    app.router.add_get('/api/version', version)
    app.router.add_get('/api/services/{service_id}/stream', bridge.stream, allow_head=False)
    app.router.add_get('/health', health)
    app.router.add_get('/xa401/current-program-titles', current)
    return app
