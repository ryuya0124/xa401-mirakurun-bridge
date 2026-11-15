from datetime import datetime, timedelta, timezone
import struct

from xa401_bridge.epg import ProgramCache, SITParser

EXPECTED = {'networkId': 32736, 'transportStreamId': 32736, 'serviceId': 1024}


def crc(data):
    value = 0xffffffff
    for byte in data:
        value ^= byte << 24
        for _ in range(8):
            value = ((value << 1) ^ (0x04c11db7 if value & 0x80000000 else 0)) & 0xffffffff
    return value


def descriptor(tag, data):
    return bytes([tag, len(data)]) + data


def section(unknown_time=False, long=False):
    name = b'\x1b(JREAL TITLE'
    text = b'\x1b(J' + b'A' * (210 if long else 5)
    short = descriptor(0x4d, b'jpn' + bytes([len(name)]) + name + bytes([len(text)]) + text)
    ids = descriptor(0x85, struct.pack('>HHHB', 32736, 32736, 12345, 0))
    date = datetime(2026, 11, 15, 12, 0, 0)
    mjd = (date.date() - datetime(1858, 11, 17).date()).days
    start = b'\xff' * 5 if unknown_time else mjd.to_bytes(2, 'big') + b'\x12\x00\x00'
    partial = descriptor(0xc3, b'\x01' + start + b'\x00\x30\x00' + b'\x00\x00\x00' + b'\xf8')
    desc = short + ids + partial
    body = b'\xff\xff\xc1\x00\x00\xf0\x00' + struct.pack('>HH', 1024, 0xc000 | len(desc)) + desc
    length = len(body) + 4
    header = bytes([0x7f, 0xf0 | length >> 8, length & 255])
    data = header + body
    return data + crc(data).to_bytes(4, 'big')


def packets(data, cc=0):
    output = []
    first = True
    while data:
        count = 183 if first else 184
        payload = (b'\x00' if first else b'') + data[:count]
        data = data[count:]
        output.append(bytes([0x47, 0x40 if first else 0, 0x1f, 0x10 | cc]) + payload.ljust(184, b'\xff'))
        cc = (cc + 1) % 16
        first = False
    return output


def test_sit_event_has_real_ids_times_title_and_duration():
    parser = SITParser(EXPECTED)
    program, = parser.feed(b''.join(packets(section())))
    assert program['name'] == 'REAL TITLE'
    assert program['eventId'] == 12345
    assert program['duration'] == 1800000
    assert datetime.fromtimestamp(program['startAt'] / 1000, timezone(timedelta(hours=9))).hour == 12
    assert program['xa401MetadataSource'] == 'SIT'


def test_multiframe_duplicate_packet_and_crc_rejection():
    raw = section(long=True)
    frames = packets(raw)
    assert len(frames) > 1
    parser = SITParser(EXPECTED)
    assert parser.feed(frames[0]) == []
    assert parser.feed(frames[0]) == []
    assert len(parser.feed(b''.join(frames[1:]))) == 1
    damaged = bytearray(raw)
    damaged[20] ^= 1
    parser = SITParser(EXPECTED)
    assert parser.feed(b''.join(packets(damaged))) == []
    assert parser.crc_errors == 1


def test_unknown_time_and_wrong_channel_are_never_fabricated():
    assert SITParser(EXPECTED).feed(b''.join(packets(section(unknown_time=True)))) == []
    wrong = dict(EXPECTED, serviceId=1025)
    assert SITParser(wrong).feed(b''.join(packets(section()))) == []


def test_persisted_cache_is_device_specific(tmp_path, monkeypatch):
    program, = SITParser(EXPECTED).feed(b''.join(packets(section())))
    monkeypatch.setattr('xa401_bridge.epg.time.time', lambda: program['startAt'] / 1000)
    cache = ProgramCache(tmp_path, 'uuid:own')
    cache.update(program)
    cache.save(cache.snapshot())
    assert ProgramCache(tmp_path, 'uuid:other').programs() == []
    restored = ProgramCache(tmp_path, 'uuid:own')
    assert restored.entries[3273601024]['eventId'] == 12345
    monkeypatch.setattr('xa401_bridge.epg.time.time', lambda: program['startAt'] / 1000 + 86400)
    assert restored.programs() == []
