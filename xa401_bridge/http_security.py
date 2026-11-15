"""Host and browser-origin checks; neither replaces authentication or the LAN ACL."""
import ipaddress
from urllib.parse import urlsplit

from aiohttp import web


def hostname(authority):
    try:
        url = urlsplit('//' + authority)
        if url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError
        url.port  # Validate the port even though the host comparison omits it.
        if not url.hostname:
            raise ValueError
        return url.hostname.lower().rstrip('.')
    except (ValueError, TypeError):
        raise web.HTTPMisdirectedRequest(text='Invalid request host') from None


def browser_origin(request):
    if request.headers.get('Sec-Fetch-Site') == 'cross-site':
        raise web.HTTPForbidden(text='Cross-site requests are not allowed')
    origin = request.headers.get('Origin')
    if origin is not None and origin != f'{request.scheme}://{request.host}':
        raise web.HTTPForbidden(text='Cross-origin requests are not allowed')


@web.middleware
async def loopback_boundary(request, handler):
    host = hostname(request.host)
    try:
        permitted = host == 'localhost' or ipaddress.ip_address(host).is_loopback
    except ValueError:
        permitted = False
    if not permitted:
        raise web.HTTPMisdirectedRequest(text='The receiver API requires a loopback host')
    browser_origin(request)
    return await handler(request)
