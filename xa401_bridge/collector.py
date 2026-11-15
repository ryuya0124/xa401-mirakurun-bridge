"""Bounded idle-time SIT collection, always preemptible by a viewer."""
import asyncio
import logging
import time

from aiohttp import web

LOG = logging.getLogger('xa401_bridge')


class EPGCollector:
    def __init__(self, bridge, settings):
        self.bridge = bridge
        self.enabled = settings.get('enabled', False) is True
        self.timeout = min(20, max(3, float(settings.get('timeout_seconds', 10))))
        self.interval = min(60, max(2, float(settings.get('interval_seconds', 2))))
        self.refresh = min(3600, max(120, float(settings.get('refresh_seconds', 600))))
        self.types = set(settings.get('types', ['GR', 'BS', 'CS']))
        if not self.types <= {'GR', 'BS', 'CS', 'CATV'}:
            raise ValueError('Unsupported EPG collection channel type')
        self.due, self.failures = {}, {}
        self.attempted = self.completed = self.preempted = 0
        self.current = None

    def current_program(self, service_id, now):
        program = self.bridge.epg.entries.get(service_id)
        if program and program['startAt'] <= now * 1000 < program['startAt'] + program['duration']:
            return program
        return None

    def next_service(self, now):
        candidates = []
        for service in self.bridge.device.services:
            if service['channel']['type'] not in self.types:
                continue
            sid = service['id']
            if sid not in self.due:
                program = self.current_program(sid, now)
                self.due[sid] = min(now + self.refresh,
                    (program['startAt'] + program['duration']) / 1000 + 1) if program else 0
            if self.due[sid] <= now:
                # First populate terrestrial channels, then BS, then cable.
                rank = {'GR': 0, 'BS': 1, 'CS': 2, 'CATV': 2}[service['channel']['type']]
                candidates.append((rank, self.due[sid], service.get('remoteControlKeyId', 0), sid))
        return min(candidates)[-1] if candidates else None

    async def collect(self, service_id):
        channel = queue = None
        self.current = service_id
        self.attempted += 1
        try:
            channel, queue = await self.bridge.subscribe(service_id, background=True)
            async with asyncio.timeout(self.timeout):
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        if not channel.error_type:
                            self.preempted += 1
                            return 'preempted'
                        return 'failed'
                    # Cached data alone does not prove this reception contained SIT.
                    if channel.program_observations and self.current_program(service_id, time.time()):
                        self.completed += 1
                        return 'success'
        except web.HTTPServiceUnavailable:
            return 'busy'
        except TimeoutError:
            return 'timeout'
        except Exception as exc:
            LOG.warning('Idle SIT collection failed (%s)', type(exc).__name__)
            return 'failed'
        finally:
            if channel is not None:
                await self.bridge.unsubscribe(channel, queue)
            self.current = None

    def schedule_result(self, service_id, result, now):
        if result == 'success':
            self.failures.pop(service_id, None)
            program = self.current_program(service_id, now)
            self.due[service_id] = min(now + self.refresh,
                (program['startAt'] + program['duration']) / 1000 + 1) if program else now + 30
        elif result in ('busy', 'preempted'):
            self.due[service_id] = now + 5
        else:
            failures = min(self.failures.get(service_id, 0) + 1, 5)
            self.failures[service_id] = failures
            self.due[service_id] = now + min(3600, 300 * 2 ** (failures - 1))

    async def run(self):
        await asyncio.sleep(3)
        while True:
            if len(self.bridge.live_sessions) < self.bridge.maximum:
                sid = self.next_service(time.time())
                if sid is not None:
                    result = await self.collect(sid)
                    self.schedule_result(sid, result, time.time())
                    LOG.info('Idle SIT collection: service=%s result=%s', sid, result)
            await asyncio.sleep(self.interval)

    def status(self):
        return {'enabled': self.enabled, 'active_service': self.current,
                'attempted': self.attempted, 'completed': self.completed,
                'preempted': self.preempted, 'timeout_seconds': self.timeout,
                'types': sorted(self.types)}
