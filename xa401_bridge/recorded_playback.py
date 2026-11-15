"""Direct XA401 recorded-program playback. No PC file/cache is created."""
import asyncio
import math
import re
from pathlib import Path

from aiohttp import web


def install_playback_routes(app, bridge):
    players = {}
    lock = asyncio.Lock()

    async def stream(request):
        token = request.match_info['recording']
        player = request.query.get('player', '')
        try:
            position = float(request.query.get('position', '0'))
            if not re.fullmatch('[0-9a-f]{24}', token) or not re.fullmatch('[0-9a-f]{32}', player):
                raise ValueError
            if not math.isfinite(position) or not 0 <= position <= 86400:
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text='Invalid playback identifier or position') from None
        if token not in bridge.device.recordings:
            await bridge.device.recorded_catalogue()
        if token not in bridge.device.recordings:
            raise web.HTTPNotFound()
        key = f'recorded:{token}@{position:.3f}#{player}'
        async with lock:
            previous = players.pop(player, None)
            if previous:
                await bridge.unsubscribe(*previous)
            channel, queue = await bridge.subscribe(key)
            players[player] = (channel, queue)
        try:
            return await bridge.deliver(request, channel, queue)
        finally:
            if players.get(player) == (channel, queue):
                players.pop(player, None)

    app.router.add_get('/xa401/api/recorded/{recording}/stream', stream, allow_head=False)
    app.router.add_static('/xa401/player-lib/', Path(__file__).with_name('web') / 'player-lib',
                          show_index=False, follow_symlinks=False)
