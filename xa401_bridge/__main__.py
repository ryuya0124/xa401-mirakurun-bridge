import argparse
import asyncio
import ipaddress
import json
import logging
from pathlib import Path

from aiohttp import web

from .device import discover, lan_url
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description='Standalone XA401 / Mirakurun bridge')
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--config', required=True, type=Path)
    scan = commands.add_parser('discover')
    scan.add_argument('--source')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # Keep the underlying library from logging unreviewed protocol details.
    logging.getLogger('pydtcpip').setLevel(logging.CRITICAL)
    if args.command == 'discover':
        print(json.dumps(asyncio.run(discover(args.source)), indent=2))
        return
    if args.config.stat().st_size > 16384:
        parser.error('Configuration exceeds 16 KiB')
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    lan_url(config['location'])
    for field in ('credentials', 'state_dir'):
        path = Path(config[field]).expanduser()
        config[field] = str((args.config.parent / path).resolve())
    host = config.get('listen', '127.0.0.1')
    if not ipaddress.ip_address(host).is_loopback:
        parser.error('The unauthenticated streaming API must listen on loopback; use an authenticated proxy for remote use')
    if config.get('source'):
        lan_url('http://' + config['source'])
    if config.get('lan_https'):
        proxy = config['lan_https']
        if proxy['listen'] != config.get('source'):
            parser.error('LAN HTTPS listen must equal the explicitly selected LAN source')
        for field in ('certificate', 'private_key'):
            path = Path(proxy[field]).expanduser()
            proxy[field] = str((args.config.parent / path).resolve())
    quality = config.get('quality', 'original')
    if quality not in ('original', 'mobile1', 'mobile2'):
        parser.error('quality must be original, mobile1, or mobile2')
    config['quality'] = quality
    web.run_app(create_app(config), host=host, port=int(config.get('port', 40772)),
                handler_cancellation=True, access_log=None, print=None)


if __name__ == '__main__':
    main()
