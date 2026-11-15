import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from xa401_bridge import recording
from xa401_bridge.recording import Recorder
from xa401_bridge.ts_file import trim_last_pes


def setup_recorder(tmp_path):
    queue = asyncio.Queue()
    channel = SimpleNamespace(error_type=None)
    released = []

    async def subscribe(key):
        return channel, queue

    async def unsubscribe(ch, q):
        released.append((ch, q))

    bridge = SimpleNamespace(config={'state_dir': str(tmp_path / 'state'), 'udn': 'uuid:own'},
        device=SimpleNamespace(items={1: {'current_title': 'Observed title'}},
            recordings={'a' * 24: SimpleNamespace(title='Recorded title')}),
        subscribe=subscribe, unsubscribe=unsubscribe)
    settings = {'enabled': True, 'directory': str(tmp_path / 'video'),
                'ffmpeg': sys.executable, 'ffprobe': sys.executable, 'min_free_bytes': 0}
    return Recorder(bridge, settings), queue, channel, released, settings


@pytest.mark.parametrize('change', [{'duration_seconds': True}, {'duration_seconds': 86401},
    {'service_id': 2}, {'start_at': '2026-01-01T00:00:00'}, {'start_at': 123},
    {'source': 'recorded', 'recording_id': 'unknown'}])
def test_invalid_jobs_rejected_without_files(tmp_path, change):
    recorder, *_ = setup_recorder(tmp_path)
    with pytest.raises(ValueError):
        recorder.create(dict({'service_id': 1, 'duration_seconds': 10}, **change))
    assert recorder.jobs == {} and list(recorder.directory.iterdir()) == []


def test_safe_destination_persistence_device_identity_and_cancel(tmp_path):
    recorder, *_, settings = setup_recorder(tmp_path)
    future = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()
    job = recorder.create({'service_id': 1, 'title': '../../bad\\name\n', 'start_at': future})
    assert recorder.path(job).parent == recorder.directory
    assert '/' not in job['file_name'] and '\\' not in job['file_name']
    restored = Recorder(recorder.bridge, settings)
    assert restored.jobs[job['id']]['status'] == 'scheduled'
    assert restored.stop(job['id'])['status'] == 'cancelled'
    job['file_name'] = '../escape.ts'
    with pytest.raises(ValueError):
        recorder.path(job)
    recorder.bridge.config['udn'] = 'uuid:other'
    with pytest.raises(ValueError):
        Recorder(recorder.bridge, settings)


async def fake_finalize(source, destination, *args):
    destination.write_bytes(source.read_bytes())
    source.unlink()
    return {'reencoded': False, 'test_fixture': True}


async def test_capture_finishes_releases_slot_and_preserves_saved_job(tmp_path, monkeypatch):
    recorder, queue, channel, released, settings = setup_recorder(tmp_path)
    monkeypatch.setattr(recording, 'finalize_ts', fake_finalize)
    packet = b'\x47\x00\x00\x10' + bytes(184)
    queue.put_nowait(packet * 10)
    queue.put_nowait(None)
    job = recorder.create({'source': 'recorded', 'recording_id': 'a' * 24})
    await recorder.capture(job)
    assert job['status'] == 'completed' and job['source_complete']
    assert recorder.path(job).read_bytes() == packet * 10
    assert len(released) == 1
    assert Recorder(recorder.bridge, settings).jobs[job['id']]['status'] == 'completed'


async def test_failure_keeps_partial_and_does_not_publish_ts(tmp_path, monkeypatch):
    recorder, queue, channel, released, _ = setup_recorder(tmp_path)
    channel.error_type = 'ReceiverError'
    queue.put_nowait(b'\x47\x00\x00\x10' + bytes(184))
    queue.put_nowait(None)
    job = recorder.create({'service_id': 1})
    await recorder.capture(job)
    assert job['status'] == 'failed' and not recorder.path(job).exists()
    assert recorder.path(job).with_suffix('.part').stat().st_size == 188
    assert len(released) == 1


async def test_scheduler_starts_due_job_but_not_future_job(tmp_path, monkeypatch):
    recorder, queue, _, _, _ = setup_recorder(tmp_path)
    monkeypatch.setattr(recording, 'finalize_ts', fake_finalize)
    now = time.time()
    job = recorder.create({'service_id': 1,
        'start_at': datetime.fromtimestamp(now + 60, timezone.utc).isoformat()})
    queue.put_nowait(b'\x47\x00\x00\x10' + bytes(184))
    queue.put_nowait(None)
    await recorder.start()
    await asyncio.sleep(0)
    assert not recorder.tasks
    monkeypatch.setattr(recording.time, 'time', lambda: now + 61)
    await asyncio.sleep(1.1)
    assert job['status'] == 'completed'
    await recorder.close()


def test_restarted_active_jobs_are_interrupted_not_implicitly_resumed(tmp_path):
    recorder, *_, settings = setup_recorder(tmp_path)
    job = recorder.create({'service_id': 1})
    job['status'] = 'recording'
    recorder.persist()
    assert Recorder(recorder.bridge, settings).jobs[job['id']]['status'] == 'interrupted'


def test_tail_trim_preserves_unrelated_packets_and_drops_only_final_pes(tmp_path):
    def packet(pid, cc):
        return bytes([0x47, 0x40 | pid >> 8, pid & 255, 0x10 | cc]) + b'\0\0\1' + bytes(181)
    data = packet(100, 0) + packet(200, 0) + packet(100, 1) + packet(200, 1) + packet(0, 0)
    source, target = tmp_path / 'input.part', tmp_path / 'output.ts'
    source.write_bytes(data)
    assert trim_last_pes(source, target, {100, 200}) == 2
    assert target.read_bytes() == packet(100, 0) + packet(200, 0) + packet(0, 0)
