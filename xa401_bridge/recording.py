"""Persistent PC recording jobs. Never invokes STB recording/Move/delete APIs."""
import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from .ts_file import finalize_ts

LOG = logging.getLogger('xa401_bridge')
TERMINAL = {'completed', 'stopped', 'cancelled', 'failed', 'interrupted'}


def safe_title(value):
    return ''.join(c for c in str(value) if c not in '<>:"/\\|?*' and
                   not unicodedata.category(c).startswith('C')).strip(' .')[:80] or 'recording'


class Recorder:
    def __init__(self, bridge, settings):
        self.bridge = bridge
        self.enabled = settings.get('enabled', False) is True
        self.directory = Path(settings.get('directory', Path(bridge.config['state_dir']) / 'recordings')).resolve()
        self.state = Path(bridge.config['state_dir']) / 'recording-jobs.json'
        self.ffprobe, self.ffmpeg = settings.get('ffprobe'), settings.get('ffmpeg')
        self.max_bytes = int(settings.get('max_file_bytes', 20 * 1024**3))
        self.min_free = int(settings.get('min_free_bytes', 2 * 1024**3))
        self.jobs, self.tasks = {}, {}
        self.loop_task = None
        self.closing = False
        if self.enabled:
            if self.max_bytes < 188 * 100 or self.min_free < 0:
                raise ValueError('Invalid recording storage limits')
            if not all(p and Path(p).is_file() for p in (self.ffprobe, self.ffmpeg)):
                raise ValueError('Recording requires explicit ffprobe and ffmpeg paths')
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                if self.state.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError('Recording state exceeds limit')
                state = json.loads(self.state.read_text(encoding='utf-8'))
                if state['udn'] != bridge.config['udn'] or len(state['jobs']) > 512:
                    raise ValueError('Recording state identity/size mismatch')
                for job in state['jobs']:
                    if not re.fullmatch('[0-9a-f]{32}', job['id']):
                        raise ValueError('Invalid stored recording identifier')
                    if job['status'] in ('recording', 'finalizing', 'stopping'):
                        job['status'], job['error_type'] = 'interrupted', 'ProcessRestarted'
                    self.jobs[job['id']] = job
            except FileNotFoundError:
                pass

    def persist(self):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state.with_suffix('.tmp')
        temporary.write_text(json.dumps({'udn': self.bridge.config['udn'],
            'jobs': list(self.jobs.values())}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.state)

    def create(self, request):
        if not self.enabled:
            raise ValueError('PC recording is not enabled')
        if len(self.jobs) >= 512 or sum(j['status'] not in TERMINAL for j in self.jobs.values()) >= 64:
            raise ValueError('Recording job limit reached')
        source = request.get('source', 'live')
        if source == 'live':
            service_id = request.get('service_id')
            if type(service_id) is not int or service_id not in self.bridge.device.items:
                raise ValueError('Unknown live service')
            key, observed = service_id, self.bridge.device.items[service_id]['current_title']
        elif source == 'recorded':
            token = request.get('recording_id', '')
            if token not in self.bridge.device.recordings:
                raise ValueError('Refresh the recorded catalogue and select a known program')
            key, observed = 'recorded:' + token, self.bridge.device.recordings[token].title
        else:
            raise ValueError('source must be live or recorded')
        now = time.time()
        start = request.get('start_at')
        if start is not None and not isinstance(start, str):
            raise ValueError('start_at must be an ISO timestamp')
        when = datetime.fromisoformat(start.replace('Z', '+00:00')) if start else datetime.now(timezone.utc)
        if when.tzinfo is None:
            raise ValueError('start_at requires an explicit timezone')
        start_at = max(now, when.timestamp())
        if start_at > now + 365 * 86400:
            raise ValueError('Reservation is too far in the future')
        duration = request.get('duration_seconds', 1800)
        if type(duration) is not int or not 5 <= duration <= 24 * 3600:
            raise ValueError('duration_seconds must be between 5 and 86400')
        ident = uuid.uuid4().hex
        if source == 'recorded':
            key += '@0#' + ident
        title = safe_title(request.get('title') or observed)
        filename = datetime.fromtimestamp(start_at).strftime('%Y%m%d-%H%M%S-') + ident[:8] + '-' + title + '.ts'
        job = {'id': ident, 'source': source, 'source_key': key, 'title': title,
               'file_name': filename, 'start_at': start_at, 'duration_seconds': duration,
               'status': 'scheduled', 'bytes': 0, 'created_at': now, 'error_type': None}
        self.jobs[ident] = job
        self.persist()
        return job

    def path(self, job):
        name = job['file_name']
        path = (self.directory / name).resolve()
        if path.parent != self.directory or path.suffix != '.ts':
            raise ValueError('Recording destination left its configured directory')
        return path

    def stop(self, ident):
        job = self.jobs[ident]
        if job['status'] in TERMINAL:
            return job
        if ident not in self.tasks or self.tasks[ident].done():
            job['status'] = 'cancelled'
        else:
            job['stop_requested'] = True
            job['status'] = 'stopping'
        self.persist()
        return job

    async def start(self):
        if self.enabled:
            self.loop_task = asyncio.create_task(self.scheduler())

    async def scheduler(self):
        while True:
            for ident, job in tuple(self.jobs.items()):
                if job['status'] in ('scheduled', 'waiting') and job['start_at'] <= time.time() and (
                    ident not in self.tasks or self.tasks[ident].done()):
                    self.tasks[ident] = asyncio.create_task(self.capture(job))
            await asyncio.sleep(1)

    async def capture(self, job):
        channel = queue = None
        destination = self.path(job)
        partial = destination.with_suffix('.part')
        reason = 'duration'
        try:
            deadline = job['start_at'] + job['duration_seconds']
            while time.time() < deadline and not job.get('stop_requested'):
                try:
                    channel, queue = await self.bridge.subscribe(job['source_key'])
                    break
                except web.HTTPServiceUnavailable:
                    job['status'] = 'waiting'
                    await asyncio.sleep(2)
            if channel is None:
                job['status'] = 'cancelled' if job.get('stop_requested') else 'failed'
                job['error_type'] = None if job.get('stop_requested') else 'ReceiveSlotsUnavailable'
                return
            if shutil.disk_usage(self.directory).free < self.min_free:
                raise ValueError('Insufficient recording space')
            job['status'], job['started_at'] = 'recording', time.time()
            self.persist()
            checked = 0
            with partial.open('xb') as writer:
                while time.time() < deadline and not job.get('stop_requested'):
                    try:
                        chunk = await asyncio.wait_for(queue.get(), 2)
                    except TimeoutError:
                        continue
                    if chunk is None:
                        if channel.error_type:
                            raise ValueError('Receiver failed during recording')
                        reason = 'source_eof'
                        break
                    if job['bytes'] + len(chunk) > self.max_bytes:
                        raise ValueError('Recording size limit reached')
                    if any(b & 0xc0 for b in chunk[3::188]):
                        raise ValueError('TS still has scrambling flags')
                    if time.time() - checked >= 2:
                        if shutil.disk_usage(self.directory).free < self.min_free:
                            raise ValueError('Insufficient recording space')
                        checked = time.time()
                    await asyncio.to_thread(writer.write, chunk)
                    job['bytes'] += len(chunk)
            await self.bridge.unsubscribe(channel, queue)
            channel = None
            if not job['bytes']:
                raise ValueError('No TS received')
            if shutil.disk_usage(self.directory).free < partial.stat().st_size + self.min_free:
                raise ValueError('Insufficient space for TS finalization')
            job['status'] = 'finalizing'
            self.persist()
            job['verification'] = await finalize_ts(partial, destination, self.ffprobe, self.ffmpeg)
            job['bytes'] = destination.stat().st_size
            job['status'] = 'stopped' if job.get('stop_requested') else 'completed'
            job['finish_reason'] = 'user_stop' if job.get('stop_requested') else reason
            job['source_complete'] = job['source'] == 'recorded' and reason == 'source_eof'
        except asyncio.CancelledError:
            job['status'], job['error_type'] = 'interrupted', 'ProcessStopped'
            raise
        except Exception as exc:
            job['status'], job['error_type'] = 'failed', type(exc).__name__
            LOG.warning('PC recording job %s failed (%s)', job['id'], job['error_type'])
        finally:
            if channel is not None:
                await self.bridge.unsubscribe(channel, queue)
            job['updated_at'] = time.time()
            self.persist()

    async def close(self):
        self.closing = True
        tasks = [self.loop_task, *self.tasks.values()]
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
