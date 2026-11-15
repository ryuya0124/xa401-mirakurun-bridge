"""One authenticated receive session, shared by same-channel subscribers."""
import asyncio
import logging
import time

import aiohttp
from pydtcpip.models import TimeRange

from .device import lan_url
from .epg import SITParser
from .receiver import Receiver, TSNormalizer

LOG = logging.getLogger('xa401_bridge')


def seek_for_key(key):
    if not isinstance(key, str) or '@' not in key:
        return None
    position = float(key.split('@', 1)[1].split('#', 1)[0])
    return TimeRange(start_ms=round(position * 1000)) if position > 0 else None


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
        self.received_time_range = None
        self.started = time.time()

    async def run(self):
        try:
            device = self.bridge.device
            resource = await device.resource(self.service_id)
            expected_host, expected_port = lan_url(resource.url, device.host)
            normalizer = TSNormalizer()
            # A recording's historical SIT must never replace live EPG entries.
            sit = SITParser(device.items[self.service_id]['service']) if isinstance(self.service_id, int) else None
            authenticated = False

            async def event(value):
                nonlocal authenticated
                if value.type == 'media_info_received':
                    self.received_time_range = value.media_info.time_range
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
                for program in sit.feed(ts) if sit else ():
                    self.bridge.epg.update(program)
                    self.program_observations += 1
                self.ready.set()
                # Bound per-client memory; a stalled reader cannot block other viewers.
                for queue in tuple(self.subscribers):
                    if isinstance(self.service_id, str):
                        # Recorded playback is an independent seekable source. Pace
                        # its HTTP receiver instead of dropping packets when buffered.
                        async with asyncio.timeout(120):
                            await queue.put(ts)
                        continue
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
                    await client.stream(resource, data, time_range=seek_for_key(self.service_id), on_event=event)
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
                'received_time_range': self.received_time_range,
                'started_at': self.started, 'error_type': self.error_type}

    @property
    def has_viewers(self):
        return bool(self.subscribers - self.background_subscribers)

