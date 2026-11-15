import gzip
import ipaddress
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from xa401_bridge import lan_proxy


async def test_https_proxy_preserves_compressed_body_post_and_websocket(monkeypatch, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'local test')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'test.crt', tmp_path / 'test.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    payload = gzip.compress(b'unchanged media payload' * 1000)

    async def data(request):
        assert request.query['x'] == '1'
        assert request.headers['X-Forwarded-Proto'] == 'https'
        return web.Response(body=payload, headers={'Content-Encoding': 'gzip'})

    async def echo(request):
        assert 'Forwarded' not in request.headers
        assert request.headers['X-Forwarded-For'] == '127.0.0.1'
        return web.Response(body=await request.read())

    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(message.data)
            elif message.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(message.data)
        return ws

    upstream = web.Application()
    upstream.router.add_get('/data', data)
    upstream.router.add_post('/echo', echo)
    upstream.router.add_get('/ws', websocket)
    captured = []
    original_socket = lan_proxy.listening_socket

    def listen(*args):
        sock = original_socket(*args)
        captured.append(sock)
        return sock

    monkeypatch.setattr(lan_proxy, 'listening_socket', listen)
    async with TestServer(upstream) as remote:
        monkeypatch.setattr(lan_proxy, 'UPSTREAM', str(remote.make_url('')).rstrip('/'))
        close = await lan_proxy.start_proxy({'listen': '127.0.0.1', 'port': 0,
            'max_request_bytes': 128,
            'allowed_subnets': ['127.0.0.0/8'], 'certificate': str(cert_path), 'private_key': str(key_path)}, None)
        import ssl
        context = ssl.create_default_context(cafile=str(cert_path))
        base = 'https://127.0.0.1:' + str(captured[0].getsockname()[1])
        try:
            async with aiohttp.ClientSession(auto_decompress=False) as client:
                async with client.get(base + '/data?x=1', ssl=context) as response:
                    assert response.status == 200
                    assert response.headers['Content-Encoding'] == 'gzip'
                    assert await response.read() == payload
                async with client.post(base + '/echo', data=b'\x00\x47body', ssl=context) as response:
                    assert await response.read() == b'\x00\x47body'
                async with client.get(base + '/data?x=1', headers={'Host': 'attacker.invalid'}, ssl=context) as response:
                    assert response.status == 421
                async with client.post(base + '/echo', data=b'no',
                    headers={'Origin': 'https://attacker.invalid'}, ssl=context) as response:
                    assert response.status == 403
                async with client.post(base + '/echo', data=b'ok', headers={'Origin': base,
                    'Forwarded': 'for=1.2.3.4', 'X-Forwarded-For': '1.2.3.4'}, ssl=context) as response:
                    assert response.status == 200 and await response.read() == b'ok'
                async with client.post(base + '/echo', data=b'A' * 129, ssl=context) as response:
                    assert response.status == 413

                async def chunks():
                    yield b'A' * 80
                    yield b'B' * 80

                async with client.post(base + '/echo', data=chunks(), ssl=context) as response:
                    assert response.status == 413
                async with client.ws_connect(base + '/ws', ssl=context) as ws:
                    await ws.send_str('hello')
                    assert (await ws.receive(timeout=2)).data == 'hello'
                    await ws.send_bytes(b'\x00\x47')
                    assert (await ws.receive(timeout=2)).data == b'\x00\x47'
        finally:
            await close()
