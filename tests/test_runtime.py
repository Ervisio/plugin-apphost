"""AppHost runtime API smoke tests with fake systemctl. No root/systemd required."""
import getpass
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost')
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.path)

class AppHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        fake = root / 'systemctl'
        fake.write_text('#!/bin/sh\ncase "$1" in\nshow) printf "ActiveState=inactive\\nMainPID=0\\nMemoryCurrent=0\\nCPUUsageNSec=0\\nTasksCurrent=0\\nNRestarts=0\\nUnitFileState=disabled\\n" ;;\n*) exit 0 ;;\nesac\n')
        fake.chmod(0o755)
        cls.sock = str(root / 'control.sock')
        env = {**os.environ, 'APPHOST_DATA':str(root / 'data'), 'APPHOST_SOCKET':cls.sock,
               'APPHOST_USER':getpass.getuser(), 'APPHOST_SYSTEMCTL':str(fake)}
        cls.proc = subprocess.Popen([sys.executable, str(ROOT/'plugin/runtime/apphost.py')],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        for _ in range(80):
            if Path(cls.sock).exists():break
            time.sleep(.05)
        assert Path(cls.sock).exists(), 'Unix control socket failed to start'
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:cls.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:cls.proc.kill()
        cls.tmp.cleanup()
    def request(self,method,path,data=None):
        conn=UnixHTTP(self.sock)
        body=json.dumps(data).encode() if data is not None else None
        conn.request(method,path,body=body,headers={'Content-Type':'application/json'})
        r=conn.getresponse()
        result=(r.status,json.loads(r.read()))
        conn.close()
        return result
    def test_api_lifecycle(self):
        status, health=self.request('GET','/v1/health');self.assertEqual(status,200)
        self.assertIn('discord',health['templates'])
        status, c=self.request('POST','/v1/apps',{'name':'Demo Telegram','template':'telegram','port':0})
        self.assertEqual(status,201)
        appid=c['app']['id']
        self.assertEqual(c['app']['metrics']['state'],'inactive')
        status, apps=self.request('GET','/v1/apps');self.assertEqual(len(apps['apps']),1)
        status, saved=self.request('PUT',f'/v1/apps/{appid}/env',{'env':{'TELEGRAM_TOKEN':'this-is-a-test'}})
        self.assertEqual(status,200)
        status, env=self.request('GET',f'/v1/apps/{appid}/env');self.assertEqual(env['env']['TELEGRAM_TOKEN'],'this-is-a-test')
        status, r=self.request('GET',f'/v1/apps/{appid}/file?path=bot.py');self.assertIn('run_polling',r['content'])
        status, r=self.request('PUT',f'/v1/apps/{appid}/file',{'path':'helper.py','content':'print("ok")\n'});self.assertEqual(status,200)
        status, r=self.request('GET',f'/v1/apps/{appid}/files');self.assertIn('helper.py',[x['name'] for x in r['entries']])
        status, r=self.request('PUT',f'/v1/apps/{appid}/file',{'path':'../../outside.py','content':'no'});self.assertEqual(status,400)
        status, r=self.request('POST',f'/v1/apps/{appid}/power',{'action':'start'});self.assertEqual(status,200)
        status, r=self.request('DELETE',f'/v1/apps/{appid}');self.assertEqual(status,200)
        status, r=self.request('GET',f'/v1/apps/{appid}');self.assertEqual(status,404)
    def test_rejects_unsafe_environment(self):
        status, r=self.request('POST','/v1/apps',{'name':'Demo API','template':'node-api'})
        appid=r['app']['id']
        status, r=self.request('PUT',f'/v1/apps/{appid}/env',{'env':{'LD_PRELOAD':'evil'}})
        self.assertEqual(status,400)
        self.request('DELETE',f'/v1/apps/{appid}')

if __name__=='__main__':unittest.main()
