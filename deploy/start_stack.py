"""Start this bridge and an independently installed KonomiTV without a visible console."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def available(port, host='127.0.0.1'):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) != 0


def spawn(command, cwd, log):
    options = {'cwd': cwd, 'stdin': subprocess.DEVNULL, 'stdout': log, 'stderr': subprocess.STDOUT}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        options['start_new_session'] = True
    return subprocess.Popen(command, **options)


def wait_ready(process, url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('A launched process exited; see the local log')
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(0.2)
    raise RuntimeError('Startup did not become ready within 40 seconds; see the local log')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--konomitv', required=True, type=Path)
    parser.add_argument('--uv', required=True, type=Path)
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    config = args.config.resolve()
    settings = json.loads(config.read_text(encoding='utf-8-sig'))
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    uv_binary = args.uv.resolve()
    if not available(int(settings.get('port', 40772))) or not available(7010, '127.0.0.77') or not available(7000):
        parser.error('A stack port is already occupied. Existing services were left running.')
    with (runtime / 'bridge.log').open('ab') as log:
        bridge = spawn([sys.executable, '-m', 'xa401_bridge', 'serve', '--config', str(config)], config.parent, log)
    (runtime / 'processes.json').write_text(json.dumps({'bridge_pid': bridge.pid}, indent=2), encoding='utf-8')
    wait_ready(bridge, f'http://127.0.0.1:{int(settings.get("port", 40772))}/health')
    with (runtime / 'konomitv.log').open('ab') as log:
        konomi = spawn([str(uv_binary), 'run', '--frozen', '--no-dev', 'task', 'serve'],
                       args.konomitv.resolve() / 'server', log)
    (runtime / 'processes.json').write_text(json.dumps({'bridge_pid': bridge.pid,
        'konomitv_launcher_pid': konomi.pid, 'url': 'http://127.0.0.77:7010/xa401-original.html'}, indent=2), encoding='utf-8')
    wait_ready(konomi, 'http://127.0.0.77:7010/api/version')
    print('Bridge and KonomiTV HTTP APIs are ready.')
    print('http://127.0.0.77:7010/xa401-original.html')


if __name__ == '__main__':
    main()
