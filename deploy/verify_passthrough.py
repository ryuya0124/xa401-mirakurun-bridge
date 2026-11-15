"""Bounded real-stream comparison: bridge input vs KonomiTV Original output.

Uses only loopback APIs. Saves media locally, never uploads it. Comparison of
overlapping elementary-stream blocks is separate from container byte equality.
"""
import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import aiohttp


async def receive(session, url, destination, limit):
    total = 0
    async with session.get(url, allow_redirects=False) as response:
        response.raise_for_status()
        with destination.open('xb') as out:
            async for data in response.content.iter_chunked(65536):
                chunk = data[:limit - total]
                out.write(chunk)
                total += len(chunk)
                if total == limit:
                    break
    return total


def streams(ffprobe, path):
    result = subprocess.run([ffprobe, '-v', 'quiet', '-show_entries',
        'stream=codec_type,codec_name,id,width,height', '-of', 'json', str(path)],
        capture_output=True, timeout=20, check=True)
    return json.loads(result.stdout)['streams']


def elementary(path, pid):
    raw = path.read_bytes()
    output = bytearray()
    for i in range(0, len(raw) - 187, 188):
        packet = raw[i:i + 188]
        if packet[0] != 0x47:
            raise ValueError('Bad TS sync')
        if ((packet[1] & 31) << 8) | packet[2] != pid or not (packet[3] & 0x10):
            continue
        pos = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
        payload = packet[pos:]
        if packet[1] & 0x40 and payload.startswith(b'\x00\x00\x01'):
            payload = payload[9 + payload[8]:]
        output.extend(payload)
    return bytes(output)


def compare(source, result, block):
    # Several distinct large blocks must match verbatim in the time overlap.
    matches = []
    for offset in range(0, len(source) - block, block):
        fragment = source[offset:offset + block]
        position = result.find(fragment)
        if position >= 0 and len(set(fragment)) > 32:
            matches.append({'source_offset': offset, 'result_offset': position,
                            'bytes': block, 'sha256': hashlib.sha256(fragment).hexdigest()})
            if len(matches) == 3:
                break
    return {'three_distinct_blocks_identical': len(matches) == 3, 'matches': matches}


async def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    direct, konomi = args.out / 'bridge.ts', args.out / 'konomitv-original.ts'
    timeout = aiohttp.ClientTimeout(total=50, connect=5, sock_read=20)
    async with aiohttp.ClientSession(trust_env=False, timeout=timeout) as session:
        await asyncio.gather(
            receive(session, f'http://127.0.0.1:40772/api/services/{args.service}/stream', direct, args.max_bytes),
            receive(session, f'http://127.0.0.77:7010/api/streams/live/{args.channel}/original/mpegts', konomi, args.max_bytes))
    a, b = streams(args.ffprobe, direct), streams(args.ffprobe, konomi)
    report = {'source': a, 'output': b, 'media_source': 'real_XA401_live',
              'comparison': {}, 'browser_playback_verified': False}
    for kind, block in (('video', 65536), ('audio', 4096)):
        left = next(s for s in a if s['codec_type'] == kind)
        right = next(s for s in b if s['codec_type'] == kind)
        report['comparison'][kind] = compare(elementary(direct, int(left['id'], 16)),
            elementary(konomi, int(right['id'], 16)), block)
    report['original_encoded_av_preserved_in_observed_overlap'] = all(
        v['three_distinct_blocks_identical'] for v in report['comparison'].values())
    (args.out / 'comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['original_encoded_av_preserved_in_observed_overlap'] else 2


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--service', type=int, required=True)
    parser.add_argument('--channel', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--ffprobe', required=True)
    parser.add_argument('--max-bytes', type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    if not 1024 * 1024 <= args.max_bytes <= 32 * 1024 * 1024:
        parser.error('Byte cap must be 1–32 MiB per stream')
    if not args.channel.isalnum():
        parser.error('Expected an alphanumeric KonomiTV channel ID')
    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
