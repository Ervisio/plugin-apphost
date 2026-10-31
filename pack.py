#!/usr/bin/env python3
"""Create an unsigned Ervisio release archive. Marketplace signature is applied by the registry."""
import hashlib
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
plug = root / 'plugin'
manifest = json.loads((plug/'manifest.json').read_text())
name, version = manifest['id'], manifest['version']
dist = root / 'dist'
dist.mkdir(exist_ok=True)
archive = dist / f'{name}-{version}.tar.gz'
with tarfile.open(archive,'w:gz') as tar:
    for file in sorted(plug.rglob('*')):
        if file.is_file() and '__pycache__' not in file.parts and file.suffix!='.pyc':
            tar.add(file,arcname=str(Path(name)/file.relative_to(plug)),recursive=False)
hash_value = hashlib.sha256(archive.read_bytes()).hexdigest()
(archive.with_suffix(archive.suffix+'.sha256')).write_text(f'{hash_value}  {archive.name}\n')
print(archive,hash_value)
