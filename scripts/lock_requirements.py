"""Lock the tested runtime closure to hashed artifacts for this Python/Linux platform.

Run from the tested API virtualenv after validating dependency updates:
  python scripts/lock_requirements.py --output services/api/requirements.lock \
      --build-output services/api/requirements-build.lock

Only public PyPI metadata and the official CPU PyTorch index are queried. The generated
files use direct HTTPS artifact URLs and --no-index, so an extra index cannot supply a
similarly named dependency. Source-only packages use the separately pinned build tools.
"""
import argparse
from collections import deque
from html.parser import HTMLParser
from importlib.metadata import distribution
import json
from pathlib import Path
import platform
import re
import urllib.parse
import urllib.request

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename


BUILD_TOOLS = {'setuptools': '78.1.0', 'wheel': '0.48.0', 'packaging': '26.3'}


def runtime_versions(source: Path) -> dict[str, str]:
    queue = deque(Requirement(line) for line in source.read_text().splitlines()
                  if line.strip() and not line.startswith('#'))
    selected, extras = {}, {}
    while queue:
        req = queue.popleft()
        if req.marker and not req.marker.evaluate():
            continue
        name = canonicalize_name(req.name)
        dist = distribution(name)
        if dist.version not in req.specifier:
            raise ValueError(f'{name} {dist.version} does not satisfy {req}')
        new = (set(req.extras) | {''}) - extras.get(name, set())
        if not new:
            continue
        extras.setdefault(name, set()).update(new)
        selected[name] = dist.version
        for raw in dist.requires or []:
            child = Requirement(raw)
            if child.marker is None or any(child.marker.evaluate({'extra': extra}) for extra in new):
                child.marker = None
                queue.append(child)
    return selected


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={'User-Agent': 'chunkcanvas-lock/1'})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('Package metadata exceeds its size limit')
    return data


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a' and (url := dict(attrs).get('href')):
            self.urls.append(url)


def artifact(name: str, version: str, tags: dict) -> tuple[str, str]:
    candidates = []
    if name in {'torch', 'torchvision'}:
        index = f'https://download.pytorch.org/whl/cpu/{name}/'
        links = Links()
        links.feed(fetch(index).decode())
        for href in links.urls:
            url, fragment = urllib.parse.urldefrag(urllib.parse.urljoin(index, href))
            filename = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])
            digest = urllib.parse.parse_qs(fragment).get('sha256', [None])[0]
            candidates.append((filename, url, digest, None))
    else:
        data = json.loads(fetch(f'https://pypi.org/pypi/{name}/{version}/json'))
        candidates = [(item['filename'], item['url'], item['digests']['sha256'], item.get('requires_python'))
                      for item in data['urls'] if not item.get('yanked')]
    ranked = []
    for filename, url, digest, python in candidates:
        if python and platform.python_version() not in SpecifierSet(python):
            continue
        parts = urllib.parse.urlsplit(url)
        expected = {'download.pytorch.org', 'download-r2.pytorch.org'} if name in {'torch', 'torchvision'} else {'files.pythonhosted.org'}
        if parts.scheme != 'https' or parts.hostname not in expected or not digest or not re.fullmatch(r'[a-f0-9]{64}', digest):
            continue
        if filename.endswith('.whl'):
            wheel_name, wheel_version, _, wheel_tags = parse_wheel_filename(filename)
            if canonicalize_name(wheel_name) != name or str(wheel_version) != version:
                continue
            rank = min((tags[tag] for tag in wheel_tags if tag in tags), default=None)
            if rank is not None:
                ranked.append((rank, url, digest))
        elif filename.endswith(('.tar.gz', '.zip')) and name not in {'torch', 'torchvision'}:
            ranked.append((100000, url, digest))
    if not ranked:
        raise ValueError(f'No trusted compatible artifact for {name}=={version}')
    _, url, digest = min(ranked)
    return url, digest


def lock(versions: dict[str, str]) -> str:
    tags = {tag: i for i, tag in enumerate(sys_tags())}
    lines = [f'# Generated for CPython {platform.python_version()} / {platform.system()} {platform.machine()}.',
             '# Runtime artifacts are pinned by URL and SHA-256; source builds use requirements-build.lock.',
             '# Install build tools first, then use --no-build-isolation for this runtime lock.',
             '--no-index', '--require-hashes']
    for name, version in sorted(versions.items()):
        url, digest = artifact(name, version, tags)
        lines += [f'# {name}=={version}', f'{name} @ {url} \\', f'    --hash=sha256:{digest}']
    return '\n'.join(lines) + '\n'


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--build-output', type=Path, required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.python_version_tuple()[:2] != ('3', '12'):
        parser.error('The supported runtime lock targets Linux / Python 3.12; validate another target before generating its lock.')
    runtime = lock(runtime_versions(root / 'services/api/requirements.txt'))
    build = lock(BUILD_TOOLS)
    # Publish only after every dependency was resolved successfully.
    args.output.write_text(runtime)
    args.build_output.write_text(build)


if __name__ == '__main__':
    main()
