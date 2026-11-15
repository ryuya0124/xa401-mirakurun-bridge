"""Finalize bounded TS without remuxing/reencoding or changing its descriptors."""
import asyncio
import json
import os


async def run_media(executable, *args, timeout=60):
    process = await asyncio.create_subprocess_exec(str(executable), *map(str, args),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async def bounded(pipe):
        chunks, size = [], 0
        while chunk := await pipe.read(65536):
            size += len(chunk)
            if size > 1024 * 1024:
                raise ValueError('Media verifier output exceeds limit')
            chunks.append(chunk)
        return b''.join(chunks)

    readers = [asyncio.create_task(bounded(process.stdout)), asyncio.create_task(bounded(process.stderr))]
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.gather(*readers), timeout)
        await asyncio.wait_for(process.wait(), 5)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    if process.returncode:
        raise ValueError('Media verification failed')
    return stdout, bool(stderr.strip())


def trim_last_pes(source, destination, pids):
    size = source.stat().st_size
    if not size or size % 188:
        raise ValueError('Unaligned TS file')
    offset = max(0, size - 8 * 1024 * 1024)
    offset -= offset % 188
    starts = {}
    with source.open('rb') as reader:
        reader.seek(offset)
        tail = reader.read()
    for pos in range(0, len(tail), 188):
        packet = tail[pos:pos + 188]
        if len(packet) != 188 or packet[0] != 0x47 or packet[3] & 0xc0:
            raise ValueError('Invalid or scrambled TS')
        pid = ((packet[1] & 31) << 8) | packet[2]
        if pid in pids and packet[1] & 64 and packet[3] & 16:
            payload = 5 + packet[4] if packet[3] & 32 else 4
            if packet[payload:payload + 3] == b'\x00\x00\x01':
                starts[pid] = offset + pos
    with source.open('rb') as reader, destination.open('xb') as writer:
        remaining = offset
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('TS changed during finalization')
            writer.write(chunk)
            remaining -= len(chunk)
        for pos in range(0, len(tail), 188):
            packet = tail[pos:pos + 188]
            pid = ((packet[1] & 31) << 8) | packet[2]
            if pid not in starts or offset + pos < starts[pid]:
                writer.write(packet)
        writer.flush()
        os.fsync(writer.fileno())
    return len(starts)


async def finalize_ts(source, destination, ffprobe, ffmpeg):
    raw, _ = await run_media(ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', source)
    info = json.loads(raw)
    streams = info.get('streams', [])
    if not any(s.get('codec_type') == 'video' for s in streams) or not any(s.get('codec_type') == 'audio' for s in streams):
        raise ValueError('Recorded TS has no recognized video/audio')
    pids = {int(s['id'], 0) for s in streams if s.get('codec_type') in ('video', 'audio') and s.get('id')}
    temporary = destination.with_suffix('.finalizing')
    trimmed = await asyncio.to_thread(trim_last_pes, source, temporary, pids)
    _, warnings = await run_media(ffmpeg, '-nostdin', '-v', 'error', '-i', temporary,
        '-t', '5', '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-', timeout=60)
    # Never replace a previous recording, and publish only after verification.
    os.link(temporary, destination)
    temporary.unlink()
    source.unlink()
    return {'video': next(s.get('codec_name') for s in streams if s.get('codec_type') == 'video'),
            'audio': next(s.get('codec_name') for s in streams if s.get('codec_type') == 'audio'),
            'sample_decode_seconds': 5, 'sample_decode_warnings': warnings,
            'trimmed_final_pes_pids': trimmed, 'reencoded': False}
