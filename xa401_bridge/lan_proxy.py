"""Optional source-interface-bound HTTPS frontend for the local KonomiTV.

Keeps the Mirakurun receiver API on loopback and leaves system routes/VPN alone.
"""
import asyncio
import ipaddress
import os
import socket
import ssl

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from .http_security import browser_origin, hostname

HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}
UPSTREAM = 'http://127.0.0.77:7010'


def headers_without_hop(headers):
    connection_tokens = {p.strip().lower() for p in headers.get('Connection', '').split(',')}
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in HOP | connection_tokens)


def listening_socket(address, port, index):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        if os.name == 'nt' and index is not None:
            sock.setsockopt(socket.IPPROTO_IP, 31, socket.htonl(index))
        sock.bind((address, port))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


async def start_proxy(config, index):
    address = str(ipaddress.IPv4Address(config['listen']))
    if not ipaddress.ip_address(address).is_private:
        raise ValueError('LAN HTTPS must bind a private IPv4 address')
    networks = tuple(ipaddress.ip_network(n) for n in config['allowed_subnets'])
    hosts = {hostname(h) for h in config.get('allowed_hosts', [address])}
    max_body = int(config.get('max_request_bytes', 40 * 1024 * 1024))
    if not hosts or not 1 <= max_body <= 40 * 1024 * 1024:
        raise ValueError('Explicit hosts and a request limit of at most 40 MiB are required')
    if not networks or any(not n.is_private or n.version != 4 for n in networks):
        raise ValueError('LAN HTTPS requires explicit private IPv4 client subnets')
    # Deliberately fixed upstream: this is not a user-selectable open proxy.
    upstream = UPSTREAM
    client = aiohttp.ClientSession(trust_env=False, auto_decompress=False,
        timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=40))

    @web.middleware
    async def boundary(request, handler):
        try:
            peer = ipaddress.ip_address(request.remote)
        except (ValueError, TypeError):
            raise web.HTTPForbidden()
        if not any(peer in network for network in networks):
            raise web.HTTPForbidden()
        if hostname(request.host) not in hosts:
            raise web.HTTPMisdirectedRequest(text='This host is not configured')
        browser_origin(request)
        return await handler(request)

    async def proxy(request):
        target = upstream + request.raw_path
        headers = headers_without_hop(request.headers)
        headers.popall('Host', None)
        for key in list(headers):
            if key.lower() == 'forwarded' or key.lower().startswith('x-forwarded-'):
                headers.popall(key, None)
        headers['X-Forwarded-Proto'] = 'https'
        headers['X-Forwarded-Host'] = request.host
        headers['X-Forwarded-For'] = request.remote
        if request.headers.get('Upgrade', '').lower() == 'websocket':
            protocols = [p.strip() for p in request.headers.get('Sec-WebSocket-Protocol', '').split(',') if p.strip()]
            for key in list(headers):
                if key.lower().startswith('sec-websocket-'):
                    headers.popall(key, None)
            async with client.ws_connect(target, headers=headers, protocols=protocols) as remote:
                local = web.WebSocketResponse(protocols=[remote.protocol] if remote.protocol else [])
                await local.prepare(request)

                async def copy(source, destination):
                    async for message in source:
                        if message.type == aiohttp.WSMsgType.TEXT:
                            await destination.send_str(message.data)
                        elif message.type == aiohttp.WSMsgType.BINARY:
                            await destination.send_bytes(message.data)
                        else:
                            break
                tasks = [asyncio.create_task(copy(local, remote)), asyncio.create_task(copy(remote, local))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await local.close()
                return local
        body = None
        if request.can_read_body:
            try:
                async with asyncio.timeout(30):
                    # request.read enforces client_max_size for streamed/chunked uploads too.
                    body = await request.read()
            except TimeoutError:
                raise web.HTTPRequestTimeout(text='Request body timed out') from None
        try:
            async with client.request(request.method, target, headers=headers, data=body,
                                      allow_redirects=False) as result:
                response = web.StreamResponse(status=result.status, headers=headers_without_hop(result.headers))
                await response.prepare(request)
                async for chunk in result.content.iter_chunked(65536):
                    async with asyncio.timeout(10):
                        await response.write(chunk)
                return response
        except (aiohttp.ClientError, TimeoutError, ConnectionError):
            if 'response' in locals() and response.prepared:
                return response
            raise web.HTTPBadGateway(text='Local KonomiTV is unavailable') from None

    app = web.Application(middlewares=[boundary], client_max_size=max_body)
    app.router.add_route('*', '/{path:.*}', proxy)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
    sock = None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config['certificate'], config['private_key'])
        await runner.setup()
        sock = listening_socket(address, int(config.get('port', 7443)), index)
        site = web.SockSite(runner, sock, ssl_context=context)
        await site.start()
    except BaseException:
        if sock:
            sock.close()
        await runner.cleanup()
        await client.close()
        raise

    async def close():
        await runner.cleanup()
        await client.close()

    return close
