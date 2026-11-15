"""Export public registry names/versions only; never inspect runtime configuration."""
import argparse
import json
import tomllib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path('uv.lock'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    packages = tomllib.loads(args.lock.read_text(encoding='utf-8'))['package']
    public = [p for p in packages if p['source'].get('registry') == 'https://pypi.org/simple']
    args.output.write_text(''.join(f'{p["name"]}=={p["version"]}\n' for p in public), encoding='utf-8')
    print(json.dumps({'registry_packages': len(public), 'not_covered_direct_sources':
        [p['name'] for p in packages if 'registry' not in p['source'] and 'editable' not in p['source']]}))


if __name__ == '__main__':
    main()
