#!/usr/bin/env python3
"""Ervisio PTY for one app workspace. Invoked as an admin command, drops root before Bash."""
import os
import pwd
import re
import sys
from pathlib import Path

if len(sys.argv) != 2 or not re.fullmatch(r'[a-z][a-z0-9-]{2,47}', sys.argv[1]):
    sys.exit('Invalid AppHost application ID')
root = Path('/srv/ervisio/apphost') / sys.argv[1]
if not (root / 'config.json').is_file() or not (root / 'work').is_dir():
    sys.exit('Application not found')
account = pwd.getpwnam('ervisio-apphost')
os.initgroups(account.pw_name, account.pw_gid)
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
os.chdir(root / 'work')
env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(root / 'work'), 'USER': account.pw_name,
       'SHELL': '/bin/bash', 'TERM': os.environ.get('TERM', 'xterm-256color'), 'LANG': 'C.UTF-8',
       'PS1': f'[{sys.argv[1]}] $ '}
os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], env)
