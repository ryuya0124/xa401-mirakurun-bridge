import asyncio

import pytest

from xa401_bridge.device import lan_url, xml
from xa401_bridge.receiver import PacketReader, TSNormalizer, credentials_from_file


@pytest.mark.parametrize('stride,offset', [(188, 0), (192, 4)])
@pytest.mark.parametrize('fragment', [1, 191, 513, 4096])
def test_normalizer_keeps_packets_across_callbacks(stride, offset, fragment):
    packets = [bytes([0x47, 0x1f, 0xff, 0x10 | (i % 16)]) + bytes([i]) * 184 for i in range(30)]
    source = b''.join((bytes(4) if offset else b'') + p for p in packets)
    normalizer = TSNormalizer()
    output = b''.join(normalizer.feed(source[i:i + fragment]) for i in range(0, len(source), fragment))
    assert output == b''.join(packets)
    assert not normalizer.buffer


def test_normalizer_rejects_nonts_and_later_sync_loss():
    with pytest.raises(ValueError, match='not aligned'):
        TSNormalizer().feed(bytes(192 * 6))
    normalizer = TSNormalizer()
    normalizer.feed((b'\x47' + bytes(187)) * 7)
    with pytest.raises(ValueError, match='lost'):
        normalizer.feed(bytes(188))


@pytest.mark.parametrize('url', ['http://8.8.8.8/', 'https://192.168.1.2/',
    'http://localhost/', 'http://user:pass@192.168.1.2/', 'http://192.168.1.2/#fragment'])
def test_device_boundary_rejects_untrusted_urls(url):
    with pytest.raises(ValueError):
        lan_url(url)


def test_verified_host_and_xml_boundary():
    assert lan_url('http://192.168.1.2:57777/path', '192.168.1.2') == ('192.168.1.2', 57777)
    with pytest.raises(ValueError):
        lan_url('http://192.168.1.3/', '192.168.1.2')
    with pytest.raises(ValueError):
        xml(b'<!DOCTYPE a [<!ENTITY e SYSTEM "file:///private">]><a>&e;</a>')


async def test_packet_allocation_is_bounded():
    reader = asyncio.StreamReader()
    reader.feed_data(b'hello')
    assert await PacketReader(reader).readexactly(5) == b'hello'
    with pytest.raises(ValueError):
        await PacketReader(reader).readexactly(8 * 1024 * 1024 + 1)


def test_credentials_error_never_contains_values(tmp_path):
    path = tmp_path / 'credentials.env'
    secret = 'do-not-log-this-invalid-credential'
    path.write_text('PYDTCPIP_PRIVATE_KEY=' + secret)
    with pytest.raises(ValueError) as error:
        credentials_from_file(path)
    assert secret not in str(error.value)
