"""Bounded LAN discovery and read-only ContentDirectory access."""
import asyncio
import hashlib
import ipaddress
import os
import re
import socket
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import aiohttp
from defusedxml.ElementTree import fromstring
from pydtcpip.dlna.arib import ARIB_PUA_TO_UNICODE
from pydtcpip.dlna.description import parse_device_description
from pydtcpip.dlna.didl import parse_didl
from pydtcpip.dlna.profiles import select_profile

SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def lan_url(value, host=None):
    u = urlsplit(value)
    if u.scheme != 'http' or u.username or u.password or u.fragment or not u.hostname:
        raise ValueError('Expected an HTTP LAN URL without credentials')
    address = ipaddress.IPv4Address(u.hostname)
    if not any(address in network for network in PRIVATE) or (host and str(address) != host):
        raise ValueError('Destination is outside the verified device')
    return str(address), u.port or 80


def xml(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('DTD and XML entities are not supported')
    return fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def local(tag):
    return tag.rsplit('}', 1)[-1]


def source_index(source):
    if os.name != 'nt' or not source:
        return None
    # The literal IP is validated before constructing the PowerShell query.
    ipaddress.IPv4Address(source)
    command = f"Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq '{source}' | Select-Object -ExpandProperty InterfaceIndex"
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoProfile', '-Command', command],
                            capture_output=True, text=True, timeout=10, check=True)
    values = result.stdout.split()
    if len(values) != 1:
        raise ValueError('Source must identify one active network interface')
    return int(values[0])


def bound_socket(family, kind, protocol, source, index):
    sock = socket.socket(family, kind, protocol)
    try:
        if source:
            sock.bind((source, 0))
            if os.name == 'nt':
                sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(index))
        return sock
    except BaseException:
        sock.close()
        raise


async def discover(source=None, seconds=2):
    """One bounded multicast query; no port scan or network settings changes."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    found = set()
    try:
        sock.bind((source or '0.0.0.0', 0))
        if source:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.setblocking(False)
        query = ('M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n'
                 'MAN: "ssdp:discover"\r\nMX: 1\r\nST: urn:schemas-upnp-org:device:MediaServer:1\r\n\r\n').encode()
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(sock, query, ('239.255.255.250', 1900))
        async with asyncio.timeout(seconds):
            while len(found) < 32:
                data, _ = await loop.sock_recvfrom(sock, 8192)
                for line in data.decode('latin1').splitlines():
                    if line.lower().startswith('location:'):
                        candidate = line.split(':', 1)[1].strip()
                        try:
                            lan_url(candidate)
                            found.add(candidate)
                        except ValueError:
                            pass
    except TimeoutError:
        pass
    finally:
        sock.close()
    return sorted(found)


class Device:
    def __init__(self, config, session):
        self.config, self.session = config, session
        self.location = config['location']
        self.host = None
        self.control = None
        self.profile = None
        self.services = []
        self.items = {}
        self.recordings = {}
        self.recordings_updated = 0
        self.lock = asyncio.Lock()

    async def request(self, url, method='GET', **kwargs):
        lan_url(url, self.host)
        async with self.session.request(method, url, allow_redirects=False,
                                       timeout=aiohttp.ClientTimeout(total=8), **kwargs) as response:
            if response.status != 200:
                raise RuntimeError(f'Device HTTP {response.status}')
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 4 * 1024 * 1024:
                    raise ValueError('Device XML exceeds 4 MiB')
                chunks.append(chunk)
            return b''.join(chunks)

    async def identify(self):
        # A cached location is a seed only; model and UDN are verified every refresh.
        self.host = None
        candidates = [self.location]
        for attempt in range(2):
            for candidate in candidates:
                try:
                    data = await self.request(candidate)
                    root = xml(data)
                    description = parse_device_description(candidate, data.decode('utf-8'))
                    if description.model_name != 'XA401' or description.udn.lower() != self.config['udn'].lower():
                        continue
                    host, _ = lan_url(candidate)
                    base = next((e.text for e in root.iter() if local(e.tag) == 'URLBase'), None) or candidate
                    service = next(e for e in root.iter() if local(e.tag) == 'service' and
                                   any(local(c.tag) == 'serviceType' and 'ContentDirectory:' in (c.text or '') for c in e))
                    fields = {local(e.tag): e.text for e in service}
                    control = urljoin(base, fields['controlURL'])
                    lan_url(control, host)
                    self.host, self.location = host, candidate
                    self.control, self.service_type = control, fields['serviceType']
                    self.profile = select_profile(description)
                    return
                except (ValueError, RuntimeError, aiohttp.ClientError, TimeoutError, ET.ParseError):
                    continue
            if attempt == 0:
                candidates = await discover(self.config.get('source'))
        raise RuntimeError('XA401 identity unavailable; run discover and update the location seed')

    async def browse(self, object_id, metadata=False, start=0):
        envelope = ET.Element(f'{{{SOAP}}}Envelope')
        body = ET.SubElement(envelope, f'{{{SOAP}}}Body')
        action = ET.SubElement(body, f'{{{self.service_type}}}Browse')
        for k, v in {'ObjectID': object_id, 'BrowseFlag': 'BrowseMetadata' if metadata else 'BrowseDirectChildren',
                     'Filter': '*', 'StartingIndex': str(start), 'RequestedCount': '1' if metadata else '100',
                     'SortCriteria': ''}.items():
            ET.SubElement(action, k).text = v
        data = await self.request(self.control, 'POST', data=ET.tostring(envelope, encoding='utf-8'),
                                  headers={'Content-Type': 'text/xml; charset="utf-8"',
                                           'SOAPAction': f'"{self.service_type}#Browse"'})
        root = xml(data)
        fields = {local(e.tag): e.text for e in root.iter()}
        if 'Fault' in fields or not fields.get('Result'):
            raise RuntimeError('ContentDirectory Browse failed')
        didl = fields['Result']
        xml(didl.encode())
        return didl, int(fields.get('NumberReturned') or 0), int(fields.get('TotalMatches') or 0)

    async def refresh(self):
        async with self.lock:
            await self.identify()
            items = {}
            for folder in self.config.get('folders', ['0/tb', '0/bs', '0/remux']):
                start = 0
                for _ in range(5):
                    didl, count, total = await self.browse(folder, start=start)
                    for element in xml(didl.encode()):
                        if local(element.tag) != 'item':
                            continue
                        fields = {local(e.tag): e.text or '' for e in element}
                        item_id = element.get('id', '')
                        if not item_id.startswith(folder + '/ch?'):
                            continue
                        match = re.search(r'nid=([0-9a-f]+)_tsid=([0-9a-f]+)_svid=([0-9a-f]+)', item_id)
                        if not match:
                            continue
                        nid, tsid, sid = [int(v, 16) for v in match.groups()]
                        number = fields.get('channelNr', '0')
                        # XA401's 10110 -> main service 011; 120 -> subservice 012.
                        remocon = int(number[1:3] if len(number) == 5 else number[:-2] or '0')
                        entry = {'id': nid * 100000 + sid, 'networkId': nid, 'serviceId': sid,
                                 'transportStreamId': tsid, 'type': 1,
                                 'name': fields.get('channelName', '').translate(ARIB_PUA_TO_UNICODE),
                                 'remoteControlKeyId': remocon,
                                 'channel': {'type': {'0/tb': 'GR', '0/bs': 'BS'}.get(folder, 'CS'),
                                             'channel': str(tsid)},
                                 'hasLogoData': False}
                        items[entry['id']] = {'service': entry, 'item_id': item_id,
                                              'current_title': fields.get('title', '').translate(ARIB_PUA_TO_UNICODE)}
                    start += count
                    if not count or start >= total:
                        break
            if not items:
                raise RuntimeError('No live channel items in the selected DMS folders')
            self.items = items
            self.services = [v['service'] for v in items.values()]
            return self.services

    async def resource(self, service_id):
        if isinstance(service_id, str) and service_id.startswith('recorded:'):
            return await self.recorded_resource(service_id[9:].split('@', 1)[0])
        async with self.lock:
            await self.identify()
            item = self.items[service_id]
            didl, _, _ = await self.browse(item['item_id'], metadata=True)
            objects = parse_didl(didl, self.profile)
            if len(objects) != 1 or objects[0].id != item['item_id']:
                raise ValueError('Unexpected media item')
            resources = objects[0].resources
            quality = self.config.get('quality', 'original')
            matches = [r for r in resources if ('quality=' + quality in r.url if quality != 'original' else 'quality=' not in r.url)]
            if not matches:
                raise ValueError('Requested quality is unavailable')
            resource = matches[0]
            lan_url(resource.url, self.host)
            if not resource.protocol_info.is_dtcp:
                raise ValueError('Expected a DTCP resource')
            return resource

    async def recorded_catalogue(self):
        import time
        async with self.lock:
            await self.identify()
            entries = {}
            start = 0
            for _ in range(20):
                didl, count, total = await self.browse('0/rec/all', start=start)
                for item in parse_didl(didl, self.profile):
                    if not re.fullmatch(r'0/rec/all/pvr\?id=[0-9]+', item.id):
                        continue
                    token = hashlib.sha256(item.id.encode()).hexdigest()[:24]
                    entries[token] = item
                start += count
                if not count or start >= total:
                    break
            self.recordings = entries
            self.recordings_updated = time.time()
            return self.recorded_summaries()

    def recorded_summaries(self):
        return [{'id': key, 'title': item.title, 'channel': item.channel_name,
                 'date': item.date, 'duration': item.duration}
                for key, item in self.recordings.items()]

    async def recorded_resource(self, token):
        if not re.fullmatch(r'[0-9a-f]{24}', token):
            raise ValueError('Invalid recorded-program identifier')
        if token not in self.recordings:
            await self.recorded_catalogue()
        async with self.lock:
            await self.identify()
            item = self.recordings[token]
            didl, _, _ = await self.browse(item.id, metadata=True)
            objects = parse_didl(didl, self.profile)
            if len(objects) != 1 or objects[0].id != item.id:
                raise ValueError('Recorded metadata identity changed')
            resource = next((r for r in objects[0].resources if
                'quality=' not in r.url and r.protocol_info.is_dtcp), None)
            if resource is None:
                raise ValueError('Original recorded resource unavailable')
            lan_url(resource.url, self.host)
            return resource
