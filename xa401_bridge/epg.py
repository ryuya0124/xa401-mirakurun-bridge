"""Observed current-program information from CRC-checked ARIB SIT sections.

XA401 partial transport streams omit EIT, but include current event information
in PID 0x1f/table 0x7f. No invented timestamps or automatic background tuning.
"""
import json
import time
import zlib
from datetime import timedelta, timezone
from pathlib import Path

from ariblib.event import Event
from ariblib.packet import BIT_REVERSE_TABLE
from ariblib.sections import SelectionInformationSection

JST = timezone(timedelta(hours=9))


def to_program(section, service, expected):
    event = Event(section, service)
    if (event.original_network_id, event.transport_stream_id, event.service_id) != (
        expected['networkId'], expected['transportStreamId'], expected['serviceId']):
        return None
    if event.start_time is None or event.duration is None or not hasattr(event, 'title'):
        return None
    duration = int(event.duration.total_seconds() * 1000)
    if not 0 < duration <= 7 * 24 * 3600 * 1000 or not 0 <= event.event_id <= 65535:
        return None
    start = int(event.start_time.replace(tzinfo=JST).timestamp() * 1000)
    title = str(event.title).strip()
    if not title or not 946684800000 <= start <= 4102444800000:
        return None
    result = {'id': event.original_network_id * 10**10 + event.service_id * 10**5 + event.event_id,
        'networkId': event.original_network_id, 'serviceId': event.service_id, 'eventId': event.event_id,
        'startAt': start, 'duration': duration, 'name': title,
        'description': str(getattr(event, 'desc', '')),
        'extended': {str(k): str(v) for k, v in getattr(event, 'detail', {}).items()},
        # Broadcast CA classification, not copy-control/DTCP output permission.
        'isFree': event.free_CA_mode == 0,
        'genres': [{'lv1': a, 'lv2': b, 'un1': c >> 4, 'un2': c & 15}
                   for a, b, c in zip(getattr(event, 'nibble1', []), getattr(event, 'nibble2', []),
                                      getattr(event, 'user_nibble', []))],
        'xa401MetadataSource': 'SIT'}
    if getattr(event, 'video_content', None) == 1 and event.video_component & 0xf0 == 0xb0:
        result['video'] = {'streamContent': event.video_content, 'componentType': event.video_component,
                           'type': 'mpeg2', 'resolution': '1080i'}
    return result


class SITParser:
    def __init__(self, expected):
        self.expected = expected
        self.buffer = bytearray()
        self.last_cc = None
        self.last_packet = None
        self.last_section = None
        self.crc_errors = 0

    def _drain(self):
        programs = []
        while len(self.buffer) >= 3:
            if self.buffer[0] == 0xff:
                self.buffer.clear()
                break
            size = 3 + ((self.buffer[1] & 15) << 8) + self.buffer[2]
            if not 12 <= size <= 4096:
                self.buffer.clear()
                break
            if len(self.buffer) < size:
                break
            section = bytes(self.buffer[:size])
            del self.buffer[:size]
            if section[0] != 0x7f or not section[1] & 0x80 or not section[5] & 1:
                continue
            if zlib.crc32(section.translate(BIT_REVERSE_TABLE)) != 0xffffffff:
                self.crc_errors += 1
                continue
            if section == self.last_section:
                continue
            try:
                sit = SelectionInformationSection(section)
                for service in sit.services:
                    program = to_program(sit, service, self.expected)
                    if program:
                        programs.append(program)
                self.last_section = section
            except (ValueError, TypeError, IndexError, AttributeError, KeyError, OverflowError):
                continue
        return programs

    def feed(self, data):
        programs = []
        for offset in range(0, len(data) - 187, 188):
            if data[offset] != 0x47:
                raise ValueError('SIT input is not aligned TS')
            if ((data[offset + 1] & 31) << 8) | data[offset + 2] != 0x1f:
                continue
            packet = data[offset:offset + 188]
            if packet[1] & 0x80 or packet[3] & 0xc0:
                self.buffer.clear()
                self.last_cc = None
                continue
            if not packet[3] & 0x10:
                continue
            pos = 4
            if packet[3] & 0x20:
                if packet[4] and packet[5] & 0x80:
                    self.buffer.clear()
                    self.last_cc = None
                pos += 1 + packet[4]
            if pos >= 188:
                continue
            cc = packet[3] & 15
            if cc == self.last_cc and packet == self.last_packet:
                continue
            if self.last_cc is not None and cc != (self.last_cc + 1) % 16:
                self.buffer.clear()
            self.last_cc, self.last_packet = cc, packet
            payload = packet[pos:]
            if packet[1] & 0x40:
                pointer = payload[0]
                if pointer > len(payload) - 1:
                    self.buffer.clear()
                    continue
                if self.buffer:
                    self.buffer.extend(payload[1:1 + pointer])
                    programs.extend(self._drain())
                self.buffer = bytearray(payload[1 + pointer:])
            elif self.buffer:
                self.buffer.extend(payload)
            programs.extend(self._drain())
        return programs


class ProgramCache:
    def __init__(self, directory, udn):
        self.path = Path(directory) / 'programs.json'
        self.udn, self.entries, self.dirty = udn, {}, False
        try:
            if self.path.stat().st_size <= 4 * 1024 * 1024:
                state = json.loads(self.path.read_text(encoding='utf-8'))
                if state['udn'] == udn and len(state['programs']) <= 2000:
                    for program in state['programs']:
                        self.update(program)
        except (OSError, ValueError, KeyError, TypeError):
            self.entries.clear()

    def update(self, program):
        integers = ('networkId', 'serviceId', 'eventId', 'startAt', 'duration')
        if any(type(program.get(k)) is not int for k in integers) or not isinstance(program.get('name'), str):
            raise ValueError('Invalid cached program fields')
        if any(not 0 <= program[k] <= 65535 for k in ('networkId', 'serviceId', 'eventId')):
            raise ValueError('Invalid cached program identity')
        if not program['name'] or not 0 < program['duration'] <= 7 * 86400000:
            raise ValueError('Invalid cached program duration/title')
        key = program['networkId'] * 100000 + program['serviceId']
        if self.entries.get(key) != program:
            self.entries[key] = program
            self.dirty = True

    def programs(self):
        oldest = int((time.time() - 6 * 3600) * 1000)
        return [p for p in self.entries.values() if p['startAt'] + p['duration'] > oldest]

    def snapshot(self):
        return {'udn': self.udn, 'programs': self.programs()}

    def save(self, snapshot):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
