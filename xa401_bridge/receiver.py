"""Source-bound pydtcpip receiver and incremental TTS normalization."""
import asyncio
import socket
from pathlib import Path

from dotenv import dotenv_values
from pydtcpip import DTCPClient, DeviceCredentials
from pydtcpip.dtcp.pcp import read_packets

from .device import bound_socket


def credentials_from_file(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError('An explicit credentials file of at most 4 KiB is required')
    try:
        fields = dotenv_values(path, interpolate=False)
        return DeviceCredentials(certificate=bytes.fromhex(fields['PYDTCPIP_CERTIFICATE']),
                                 private_key=bytes.fromhex(fields['PYDTCPIP_PRIVATE_KEY']))
    except Exception:
        raise ValueError('Credential validation failed (values are suppressed)') from None


class PacketReader:
    """Bound each PCP allocation; continuous reception has no cumulative byte cap."""
    def __init__(self, reader):
        self.reader = reader

    async def readexactly(self, size):
        if not 0 <= size <= 8 * 1024 * 1024:
            raise ValueError('PCP size exceeds 8 MiB')
        return await self.reader.readexactly(size)


class Receiver(DTCPClient):
    def __init__(self, *args, source=None, interface_index=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.source, self.interface_index = source, interface_index

    async def _authenticate(self, media_info, *, is_move, transfer_id, on_event):
        if is_move:
            raise ValueError('Move is not exposed by this application')
        if not self.source:
            return await super()._authenticate(media_info, is_move=False, transfer_id=transfer_id, on_event=on_event)
        sock = bound_socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, self.source, self.interface_index)
        writer = None
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setblocking(False)
            async with asyncio.timeout(self._timeout):
                await asyncio.get_running_loop().sock_connect(sock, (media_info.host, media_info.port))
                reader, writer = await asyncio.open_connection(sock=sock)
            return await self._run_authentication(reader, writer, media_info.host, media_info.port,
                                                 is_move=False, transfer_id=transfer_id, on_event=on_event)
        finally:
            if writer:
                writer.close()
            else:
                sock.close()

    async def _read_packets(self, response):
        async for packet in read_packets(PacketReader(response.content)):
            if packet.cipher_algorithm != 0:
                raise ValueError('Unsupported PCP cipher')
            yield packet


class TSNormalizer:
    """Recognize 188/192 byte packets, keep partial packets across callbacks."""
    def __init__(self):
        self.buffer = bytearray()
        self.stride = self.offset = None

    def feed(self, data):
        self.buffer.extend(data)
        if self.stride is None:
            if len(self.buffer) < 192 * 6:
                return b''
            for stride, offset in ((192, 4), (188, 0)):
                if all(self.buffer[offset + i * stride] == 0x47 for i in range(6)):
                    self.stride, self.offset = stride, offset
                    break
            if self.stride is None:
                raise ValueError('Decrypted payload is not aligned MPEG-TS/TTS')
        count = len(self.buffer) // self.stride
        output = bytearray()
        for i in range(count):
            start = i * self.stride + self.offset
            packet = self.buffer[start:start + 188]
            if packet[0] != 0x47:
                raise ValueError('MPEG-TS synchronization lost')
            output.extend(packet)
        del self.buffer[:count * self.stride]
        return bytes(output)
