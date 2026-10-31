#!/usr/bin/env python3
"""Entrypoint executed by systemd as the unprivileged apphost user."""
import json
import os
import re
import sys
from pathlib import Path

if len(sys.argv) != 2 or not re.fullmatch(r'[a-z][a-z0-9-]{2,47}', sys.argv[1]):
    sys.exit('Invalid app id')
root = Path(os.environ.get('APPHOST_DATA', '/srv/ervisio/apphost')) / sys.argv[1]
conf = json.loads((root / 'config.json').read_text())
secrets = json.loads((root / 'secrets.json').read_text())
env = os.environ.copy()
env['HOME'] = str(root / 'work')
env['XDG_CACHE_HOME'] = str(root / 'work' / '.cache')
env.update({k: str(v) for k, v in secrets.items()})
if conf.get('port'):
    env['PORT'] = str(conf['port'])
command = conf.get('command', '').strip()
if not command:
    sys.exit('Missing start command')
os.chdir(root / 'work')
os.execvpe('/bin/bash', ['bash', '-c', command], env)
