#!/usr/bin/env python3
"""Ervisio AppHost: root-owned control plane; workloads run as ervisio-apphost.
JSON HTTP over a local Unix socket, accessible only through the admin Ervisio plugin.
"""
import json
import os
import re
import shlex
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA = Path(os.environ.get('APPHOST_DATA', '/srv/ervisio/apphost'))
SOCKET = Path(os.environ.get('APPHOST_SOCKET', '/run/ervisio-apphost/control.sock'))
RUN_USER = os.environ.get('APPHOST_USER', 'ervisio-apphost')
SYSTEMCTL = os.environ.get('APPHOST_SYSTEMCTL', 'systemctl')
MAX_BODY = 131072
ID_RE = re.compile(r'^[a-z][a-z0-9-]{2,47}$')
ENV_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
JOBS = {}
JOB_LOCK = threading.Lock()
SAMPLE = {}
SAMPLE_LOCK = threading.Lock()

class Fault(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def run(cmd, timeout=20, cwd=None, check=True, **kwargs):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                stdin=subprocess.DEVNULL, **kwargs)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise Fault(str(e), 503)
    if check and result.returncode:
        raise Fault((result.stderr or result.stdout or 'Command failed').strip()[-1000:], 409)
    return result


def service(appid, verb):
    if verb not in {'start', 'stop', 'restart', 'enable', 'disable', 'is-enabled'}:
        raise Fault('Invalid service operation')
    return run([SYSTEMCTL, verb, f'ervisio-apphost@{appid}.service'], timeout=40, check=verb not in {'is-enabled'})


def app_dir(appid):
    if not ID_RE.fullmatch(appid):
        raise Fault('Invalid app id')
    path = DATA / appid
    if path.is_symlink() or not (path / 'config.json').is_file():
        raise Fault('App not found', 404)
    return path


def json_read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except FileNotFoundError:
        return default


def write_json(path, data, mode=0o640):
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex[:8])
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def fix_owner(path):
    import pwd
    user = pwd.getpwnam(RUN_USER)
    os.chown(path, user.pw_uid, user.pw_gid)


def config(appid):
    return json_read(app_dir(appid) / 'config.json')


def system_props(appid):
    r = run([SYSTEMCTL, 'show', f'ervisio-apphost@{appid}.service', '--no-pager',
             '--property=LoadState,ActiveState,SubState,MainPID,MemoryCurrent,CPUUsageNSec,TasksCurrent,NRestarts,UnitFileState'],
            check=False)
    d = dict(line.split('=', 1) for line in r.stdout.splitlines() if '=' in line)
    cpu_ns = int(d.get('CPUUsageNSec') or 0) if (d.get('CPUUsageNSec') or '').isdigit() else 0
    now = time.monotonic()
    with SAMPLE_LOCK:
        before = SAMPLE.get(appid)
        SAMPLE[appid] = (now, cpu_ns)
    cpu_pct = round(100 * max(0, cpu_ns - before[1]) / (1_000_000_000 * max(now - before[0], .01)), 1) if before and cpu_ns >= before[1] else 0.0
    memory = d.get('MemoryCurrent', '0')
    return {
        'state': d.get('ActiveState', 'unknown'), 'substate': d.get('SubState', ''),
        'pid': int(d.get('MainPID') or 0), 'cpuPercent': cpu_pct,
        'memoryBytes': int(memory) if memory.isdigit() else 0,
        'tasks': int(d.get('TasksCurrent') or 0) if (d.get('TasksCurrent') or '').isdigit() else 0,
        'restarts': int(d.get('NRestarts') or 0),
        'enabled': d.get('UnitFileState', 'disabled') == 'enabled'
    }


def public_app(appid):
    c = config(appid)
    c.pop('sourceUrl', None) if False else None
    return {**c, 'id': appid, 'metrics': system_props(appid)}


def env_validate(data):
    if not isinstance(data, dict) or len(data) > 80:
        raise Fault('Environment must be an object with at most 80 variables')
    clean = {}
    for k, v in data.items():
        if not ENV_RE.fullmatch(k) or not isinstance(v, str) or len(v) > 12000 or '\x00' in v:
            raise Fault(f'Invalid environment entry: {k}')
        if k in ('PATH', 'HOME', 'USER', 'SHELL', 'LD_PRELOAD', 'PYTHONPATH'):
            raise Fault(f'Reserved environment variable: {k}')
        clean[k] = v
    return clean

TEMPLATES = {
    'node-api': ('node server.js', '', {'server.js': '''const http = require('node:http');
const port = Number(process.env.PORT || 3000);
http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify({ok: true, service: 'AppHost', route: req.url}));
}).listen(port, process.env.BIND_HOST || '127.0.0.1', () => console.log(`Listening on ${port}`));
'''}),
    'python-api': ('python3 server.py', '', {'server.py': '''import json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.dumps({'ok': True, 'service': 'AppHost', 'route': self.path}).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
HTTPServer((os.getenv('BIND_HOST', '127.0.0.1'), int(os.getenv('PORT', '3000'))), Handler).serve_forever()
'''}),
    'discord': ('.venv/bin/python bot.py', 'python3 -m venv .venv && .venv/bin/pip install -U discord.py', {'bot.py': '''import os
import discord
from discord.ext import commands
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}', flush=True)
@bot.command()
async def ping(ctx):
    await ctx.send('Pong!')
bot.run(os.environ['DISCORD_TOKEN'])
'''}),
    'telegram': ('.venv/bin/python bot.py', 'python3 -m venv .venv && .venv/bin/pip install -U python-telegram-bot', {'bot.py': '''import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Ciao! Sono il tuo bot AppHost.')
app = Application.builder().token(os.environ['TELEGRAM_TOKEN']).build()
app.add_handler(CommandHandler('start', start))
print('Telegram bot ready', flush=True)
app.run_polling()
'''}),
    'custom': ('bash start.sh', '', {'start.sh': '#!/bin/sh\necho "Edit start.sh and configure your command in AppHost"\nsleep infinity\n'})
}


def create_app(data):
    name = str(data.get('name', '')).strip()
    if not 2 <= len(name) <= 70:
        raise Fault('Name must be 2-70 characters')
    template = data.get('template', 'custom')
    if template not in TEMPLATES:
        raise Fault('Unknown template')
    source = str(data.get('sourceUrl', '')).strip()
    if source:
        validate_git_url(source)
    port = int(data.get('port') or 0)
    if port < 0 or port > 65535:
        raise Fault('Port outside valid range')
    if port and DATA.exists():
        for existing in DATA.iterdir():
            if existing.is_dir() and not existing.is_symlink() and (existing / 'config.json').is_file():
                if json_read(existing / 'config.json', {}).get('port') == port:
                    raise Fault('This port is already assigned to another AppHost app', 409)
    branch = str(data.get('branch') or 'main')
    if not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', branch) or '..' in branch:
        raise Fault('Invalid Git branch')
    slug = re.sub('[^a-z0-9]+', '-', name.lower()).strip('-')[:27] or 'app'
    if len(slug) < 3:
        slug = 'app-' + slug
    appid = f'{slug}-{uuid.uuid4().hex[:6]}'
    root = DATA / appid
    root.mkdir(mode=0o750, parents=True)
    work = root / 'work'
    work.mkdir(mode=0o750)
    default_command, default_install, files = TEMPLATES[template]
    for filename, content in files.items():
        (work / filename).write_text(content, encoding='utf8')
    c = {'name': name, 'template': template, 'command': default_command,
         'installCommand': default_install, 'sourceUrl': source, 'branch': branch,
         'port': port, 'createdAt': int(time.time())}
    write_json(root / 'config.json', c)
    write_json(root / 'secrets.json', {}, 0o600)
    # Everything under the app path is readable/writable only to the workload user.
    for directory, dirs, filenames in os.walk(root):
        fix_owner(Path(directory))
        for filename in filenames:
            fix_owner(Path(directory) / filename)
    return public_app(appid)


def validate_git_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or len(url) > 1000:
        raise Fault('Use a public HTTPS Git URL without credentials')
    if p.port not in (None, 443) or p.fragment:
        raise Fault('Unsupported Git URL')


def safe_work_file(appid, raw):
    work = app_dir(appid) / 'work'
    if work.is_symlink() or not work.is_dir():
        raise Fault('Unsafe application work directory')
    root = work.resolve()
    if not root.is_relative_to(app_dir(appid).resolve()):
        raise Fault('Work directory outside application')
    if not isinstance(raw, str) or not raw or len(raw) > 350 or '\x00' in raw or raw.startswith('/'):
        raise Fault('Invalid relative path')
    full = (root / raw).resolve()
    if not full.is_relative_to(root) or full == root:
        raise Fault('Path outside app directory')
    return full


def launch_job(appid, action, command=None):
    if action not in ('deploy', 'exec'):
        raise Fault('Unsupported job')
    with JOB_LOCK:
        if any(j['appId'] == appid and j['state'] == 'running' for j in JOBS.values()):
            raise Fault('A job is already running for this app', 409)
        jobid = uuid.uuid4().hex[:12]
        JOBS[jobid] = {'id': jobid, 'appId': appid, 'state': 'running', 'output': '', 'startedAt': int(time.time())}
    def worker():
        chunks = []
        try:
            c = config(appid)
            work = app_dir(appid) / 'work'
            if work.is_symlink() or not work.is_dir():
                raise Fault('Unsafe work directory')
            if action == 'exec':
                chunks.append('$ command\n')
                r = run(['runuser', '-u', RUN_USER, '--', 'env', f'HOME={work}', f'npm_config_cache={work}/.npm-cache', 'bash', '-c', command], cwd=work, timeout=120)
                chunks.append((r.stdout + '\n' + r.stderr)[-22000:])
            if action == 'deploy' and c.get('sourceUrl'):
                url = c['sourceUrl']
                validate_git_url(url)
                if (work / '.git').is_dir():
                    cmd = ['runuser', '-u', RUN_USER, '--', 'git', '-C', str(work), 'pull', '--ff-only']
                    chunks.append('$ git pull --ff-only\n')
                else:
                    if any(work.iterdir()):
                        shutil.rmtree(work)
                    cmd = ['runuser', '-u', RUN_USER, '--', 'git', 'clone', '--depth', '1', '--branch', c['branch'], url, str(work)]
                    chunks.append('$ git clone\n')
                r = run(cmd, timeout=180, check=True)
                chunks.append((r.stdout + '\n' + r.stderr)[-12000:])
            if action == 'deploy' and c['installCommand'].strip():
                chunks.append('$ install command\n')
                r = run(['runuser', '-u', RUN_USER, '--', 'env', f'HOME={work}', f'npm_config_cache={work}/.npm-cache', 'bash', '-c', c['installCommand']], cwd=work, timeout=300)
                chunks.append((r.stdout + '\n' + r.stderr)[-16000:])
            state = 'completed'
        except Exception as e:
            chunks.append('\nERROR: ' + str(e))
            state = 'failed'
        with JOB_LOCK:
            JOBS[jobid].update(state=state, output='\n'.join(chunks)[-25000:], endedAt=int(time.time()))
    threading.Thread(target=worker, daemon=True).start()
    return {'jobId': jobid}


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def address_string(self):
        return 'local'
    def send_json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False).encode('utf8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)
    def read_json(self):
        size = int(self.headers.get('Content-Length', 0))
        if size < 0 or size > MAX_BODY:
            raise Fault('Request body too large', 413)
        raw = self.rfile.read(size) if size else b'{}'
        try:
            data = json.loads(raw)
        except ValueError:
            raise Fault('Invalid JSON')
        if not isinstance(data, dict):
            raise Fault('JSON object required')
        return data
    def do_GET(self):
        self.dispatch('GET')
    def do_POST(self):
        self.dispatch('POST')
    def do_PATCH(self):
        self.dispatch('PATCH')
    def do_PUT(self):
        self.dispatch('PUT')
    def do_DELETE(self):
        self.dispatch('DELETE')
    def dispatch(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            parts = path.strip('/').split('/')
            if path == '/v1/health' and method == 'GET':
                return self.send_json({'ok': True, 'runtime': 'apphost', 'templates': list(TEMPLATES)})
            if path == '/v1/apps':
                if method == 'GET':
                    apps = []
                    if DATA.exists():
                        for p in sorted(DATA.iterdir()):
                            if p.is_dir() and (p / 'config.json').is_file() and ID_RE.fullmatch(p.name):
                                apps.append(public_app(p.name))
                    return self.send_json({'apps': apps})
                if method == 'POST':
                    return self.send_json({'app': create_app(self.read_json())}, 201)
            if len(parts) >= 3 and parts[:2] == ['v1', 'apps']:
                appid = parts[2]
                root = app_dir(appid)
                if len(parts) == 3:
                    if method == 'GET':
                        return self.send_json({'app': public_app(appid)})
                    if method == 'PATCH':
                        data = self.read_json()
                        allowed = {'name', 'command', 'installCommand', 'port', 'branch'}
                        if any(k not in allowed for k in data):
                            raise Fault('Unsupported setting')
                        c = config(appid)
                        for k, v in data.items():
                            if k == 'port':
                                v = int(v or 0)
                                if not 0 <= v <= 65535:
                                    raise Fault('Invalid port')
                            else:
                                v = str(v).strip()
                                if len(v) > 2000 or (k == 'branch' and (not re.fullmatch(r'[A-Za-z0-9._/-]{1,100}', v) or '..' in v)):
                                    raise Fault('Invalid setting value')
                            c[k] = v
                        write_json(root / 'config.json', c)
                        fix_owner(root / 'config.json')
                        return self.send_json({'app': public_app(appid)})
                    if method == 'DELETE':
                        if system_props(appid)['state'] == 'active':
                            raise Fault('Stop the app before deleting it', 409)
                        service(appid, 'disable')
                        shutil.rmtree(root)
                        return self.send_json({'deleted': True})
                if len(parts) == 4:
                    part = parts[3]
                    if part == 'power' and method == 'POST':
                        action = self.read_json().get('action')
                        if action not in {'start', 'stop', 'restart', 'enable', 'disable'}:
                            raise Fault('Invalid action')
                        service(appid, action)
                        return self.send_json({'app': public_app(appid)})
                    if part == 'logs' and method == 'GET':
                        limit = max(10, min(int(query.get('limit', ['120'])[0]), 300))
                        r = run(['journalctl', '-u', f'ervisio-apphost@{appid}.service', '-n', str(limit),
                                 '--no-pager', '-o', 'short-iso'], timeout=15, check=False)
                        return self.send_json({'text': r.stdout[-50000:]})
                    if part == 'deploy' and method == 'POST':
                        return self.send_json(launch_job(appid, 'deploy'), 202)
                    if part == 'exec' and method == 'POST':
                        cmd = self.read_json().get('command', '')
                        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 1000:
                            raise Fault('Invalid command')
                        return self.send_json(launch_job(appid, 'exec', cmd), 202)
                    if part == 'env':
                        if method == 'GET':
                            return self.send_json({'env': json_read(root / 'secrets.json', {})})
                        if method == 'PUT':
                            env = env_validate(self.read_json().get('env'))
                            write_json(root / 'secrets.json', env, 0o600)
                            fix_owner(root / 'secrets.json')
                            return self.send_json({'saved': True})
                    if part == 'files' and method == 'GET':
                        relative = query.get('path', ['.'])[0]
                        if relative == '.':
                            directory = root / 'work'
                        else:
                            directory = safe_work_file(appid, relative)
                        if not directory.is_dir():
                            raise Fault('Not a directory', 404)
                        return self.send_json({'entries': [{'name': x.name, 'isDir': x.is_dir(), 'size': x.stat().st_size}
                                                           for x in sorted(directory.iterdir()) if not x.is_symlink()][:300]})
                    if part == 'file':
                        if method == 'GET':
                            target = safe_work_file(appid, query.get('path', [''])[0])
                            if not target.is_file() or target.stat().st_size > 200000:
                                raise Fault('File missing or larger than 200 KB', 404)
                            return self.send_json({'content': target.read_text(encoding='utf8')})
                        if method == 'PUT':
                            data = self.read_json()
                            target = safe_work_file(appid, data.get('path'))
                            content = data.get('content')
                            if not isinstance(content, str) or len(content.encode('utf8')) > 100000:
                                raise Fault('Content must be text, at most 100 KB')
                            target.parent.mkdir(parents=True, exist_ok=True)
                            # Existing symlink components are rejected by safe_work_file.
                            target.write_text(content, encoding='utf8')
                            fix_owner(target)
                            return self.send_json({'saved': True})
            if len(parts) == 3 and parts[:2] == ['v1', 'jobs'] and method == 'GET':
                with JOB_LOCK:
                    job = JOBS.get(parts[2])
                    if not job:
                        raise Fault('Job not found', 404)
                    return self.send_json({'job': dict(job)})
            raise Fault('Route not found', 404)
        except Fault as e:
            self.send_json({'error': {'message': str(e)}}, e.status)
        except (ValueError, KeyError, OSError) as e:
            self.send_json({'error': {'message': str(e)}}, 400)
        except Exception as e:
            self.send_json({'error': {'message': type(e).__name__ + ': ' + str(e)}}, 500)

class ThreadedUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    try:
        SOCKET.unlink()
    except FileNotFoundError:
        pass
    with ThreadedUnixServer(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        print('AppHost listening on ' + str(SOCKET), flush=True)
        server.serve_forever(poll_interval=.2)

if __name__ == '__main__':
    main()
