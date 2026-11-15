"""Apply/revert the small, version-checked optional KonomiTV client hook.

The provider/receiver remains in this independent application. No KonomiTV
database, running server, dependencies or user configuration is modified here.
"""
import argparse
from pathlib import Path
import subprocess

SUPPORTED_HEAD = '2774e39e512b5f3dfd423fdfc8a7602e92e844f5'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('konomitv', type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--revert', action='store_true')
    args = parser.parse_args()
    target = args.konomitv.resolve(strict=True)
    patch = Path(__file__).with_name('konomitv-external-recordings.patch').resolve(strict=True)

    def git(*commands):
        return subprocess.run(['git', '-C', str(target), *commands],
            capture_output=True, text=True, encoding='utf-8', timeout=30)

    result = git('rev-parse', 'HEAD')
    if result.returncode or result.stdout.strip() != SUPPORTED_HEAD:
        raise SystemExit('Unsupported KonomiTV revision. Review/rebase the patch before applying.')
    installed = git('apply', '--reverse', '--check', str(patch)).returncode == 0
    if args.revert:
        if not installed:
            raise SystemExit('Patch is absent or changed; no files were modified.')
        command = git('apply', '--reverse', str(patch))
    elif installed:
        print('Integration hook is already installed. No files modified.')
        return
    else:
        check = git('apply', '--check', str(patch))
        if check.returncode:
            raise SystemExit('Patch conflicts with the current client. No files were modified.')
        if not args.apply:
            print('Compatible client; patch check passed. Use --apply to install.')
            return
        command = git('apply', str(patch))
    if command.returncode:
        raise SystemExit('Git could not apply/revert the client hook.')
    print('Client source updated. Build client/ with the existing frozen lockfile; server restart is unnecessary.')


if __name__ == '__main__':
    main()
