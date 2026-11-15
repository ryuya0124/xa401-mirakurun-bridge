"""Prepare/activate a narrowly scoped AdGuard rewrite; never prints its secrets.

Run against the owner's config in a controlled deployment environment. Validate
the candidate with AdGuard --check-config and stop that instance before activate.
In a synchronized installation, modify the authoritative origin, never only a
replica. Online changes should use the origin's official rewrite API instead.
"""
import argparse
import copy
import ipaddress
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['prepare', 'activate', 'restore'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--hostname')
    parser.add_argument('--ipv4')
    parser.add_argument('--ipv6')
    args = parser.parse_args()
    path = args.config.resolve()
    if path.name != 'AdGuardHome.yaml':
        parser.error('Expected the explicitly mounted AdGuardHome.yaml')
    candidate = path.with_name(path.name + '.xa401-candidate')
    plan_path = path.with_name(path.name + '.xa401-plan.json')
    yaml = YAML()
    yaml.preserve_quotes = True
    if args.phase == 'prepare':
        if not args.hostname or not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', args.hostname):
            parser.error('Explicit lowercase DNS hostname is required')
        a, aaaa = ipaddress.IPv4Address(args.ipv4), ipaddress.IPv6Address(args.ipv6)
        if not a.is_private or not aaaa.is_private:
            parser.error('Only private addresses are supported; no public DNS records are made')
        data = yaml.load(path)
        original = copy.deepcopy(data)
        rewrites = data.setdefault('filtering', {}).setdefault('rewrites', [])
        wanted = [{'domain': args.hostname, 'answer': str(value), 'enabled': True} for value in (a, aaaa)]
        existing = [entry for entry in rewrites if entry.get('domain') == args.hostname]
        if existing:
            if existing == wanted:
                print(json.dumps({'changed': False, 'already_configured': True}))
                return
            parser.error('That private hostname already has different rewrites')
        rewrites.extend(wanted)
        # No other setting, including resolver/authentication/TLS settings, may change.
        check = copy.deepcopy(data)
        check['filtering']['rewrites'] = check['filtering']['rewrites'][:-2]
        if check != original:
            raise ValueError('Unrelated DNS settings changed')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = path.with_name(path.name + '.before-xa401-' + stamp)
        shutil.copy2(path, backup)
        shutil.copy2(path, candidate)
        with candidate.open('w', encoding='utf-8') as out:
            yaml.dump(data, out)
        if yaml.load(candidate) != data:
            raise ValueError('Candidate does not round-trip')
        plan = {'backup': backup.name, 'hostname': args.hostname, 'added_rewrites': wanted}
        plan_path.write_text(json.dumps(plan, indent=2), encoding='utf-8')
        os.chmod(plan_path, 0o600)
        print(json.dumps({'changed': False, 'candidate_prepared': True, 'hostname': args.hostname}))
    else:
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        backup = path.with_name(plan['backup'])
        if backup.parent != path.parent or not backup.name.startswith(path.name + '.before-xa401-'):
            raise ValueError('Backup is outside the managed directory')
        if args.phase == 'restore':
            shutil.copy2(backup, path)
            print(json.dumps({'restored': True}))
            return
        if yaml.load(path) != yaml.load(backup):
            raise ValueError('Active config changed after preparation; refusing overwrite')
        candidate.replace(path)
        print(json.dumps({'activated': True, 'hostname': plan['hostname']}))


if __name__ == '__main__':
    main()
