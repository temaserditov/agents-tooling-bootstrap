#!/usr/bin/env python3
"""An isolated, explicitly headless browser test; no existing profiles or logins.

Hermes 0.23+ (``--hermes-python``): Hermes resolves its own Chromium and builds the
launch command with its own rules (sandbox policy for root/Docker included); the
test only swaps the start page for a synthetic one. Older Hermes: agent-browser.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

PAGE='data:text/html,<title>Agents tooling smoke</title><h1>Fixture</h1>'
# Runs inside the Hermes interpreter; prints only the executable and argv, no config values.
HERMES_ARGV='''import json,sys
from hermes_cli.browser_runtime import chromium_executable,_pm_executable
from tools.browser_launcher import build_chromium_argv
exe=chromium_executable()
if not exe:print(json.dumps({"missing":True}));sys.exit(0)
argv=build_chromium_argv(exe,sys.argv[1],packaged=exe==_pm_executable(),headed=False,sandbox_bypass=True)
print(json.dumps({"argv":argv}))
'''


def isolated_env(root):
    env={'PATH':os.environ.get('PATH',''),'HOME':str(root),'USERPROFILE':str(root),'TMPDIR':str(root)}
    # Required Windows runtime locations are metadata, not credentials.
    for name in ['SYSTEMROOT','WINDIR','COMSPEC','LOCALAPPDATA']:
        if name in os.environ:env[name]=os.environ[name]
    return env


def hermes_smoke(python,hermes_home):
    if not Path(python).is_file():raise ValueError('Hermes Python missing')
    with tempfile.TemporaryDirectory(prefix='hb-',dir='/tmp' if os.name!='nt' else None) as tmp:
        root=Path(tmp).resolve();profile=root/'profile'
        env={**isolated_env(root),'HERMES_HOME':str(Path(hermes_home).resolve())}
        if 'HOME' in os.environ:env['HOME']=os.environ['HOME']  # pm's Chromium store lives under the real home
        plan=subprocess.run([str(python),'-B','-c',HERMES_ARGV,str(profile)],env=env,capture_output=True,timeout=60)
        if plan.returncode:raise ValueError('Hermes browser runtime unavailable; raw output suppressed')
        data=json.loads(plan.stdout.decode().strip().splitlines()[-1])
        if data.get('missing'):raise ValueError('Hermes Chromium is not installed: run hermes pm install chromium')
        argv=data['argv']
        if '--headless=new' not in argv or not any(a=='--user-data-dir='+str(profile) for a in argv):
            raise ValueError('Unexpected Hermes launch plan')
        argv=[a for a in argv[:-1] if not a.startswith('--remote-debugging-port')]+['--dump-dom',PAGE]
        result=subprocess.run(argv,env=env,capture_output=True,timeout=90)
        if result.returncode or b'Agents tooling smoke' not in result.stdout:
            raise ValueError('Isolated headless Chromium check failed; raw output suppressed')
        return {'headless':True,'isolatedProfile':True,'syntheticPageVerified':True,'browser':'hermes-chromium'}


def smoke(executable=None):
    cli=shutil.which('agent-browser')
    if not cli:raise ValueError('agent-browser is not installed in this process PATH')
    if executable and not Path(executable).is_file():raise ValueError('Browser executable missing')
    with tempfile.TemporaryDirectory(prefix='ab-',dir='/tmp' if os.name!='nt' else None) as tmp:
        root=Path(tmp).resolve();cfg=root/'browser.json';cfg.write_text('{"headed":false}')
        session='at-'+uuid.uuid4().hex[:8]
        env={**isolated_env(root),'AGENT_BROWSER_HEADED':'false','AGENT_BROWSER_SOCKET_DIR':str(root)}
        args=[cli,'--config',str(cfg),'--headed','false','--session',session,'--profile',str(root/'profile')]
        if executable:args+=['--executable-path',str(Path(executable).resolve())]
        try:
            result=subprocess.run(args+['open',PAGE],env=env,capture_output=True,timeout=45)
            if result.returncode:raise ValueError('Isolated headless open failed; raw output suppressed')
            title=subprocess.run(args+['get','title'],env=env,capture_output=True,timeout=20)
            if title.returncode or b'Agents tooling smoke' not in title.stdout:raise ValueError('Browser content check failed')
            return {'headless':True,'isolatedProfile':True,'syntheticPageVerified':True,'browser':'agent-browser'}
        finally:
            subprocess.run(args+['close'],env=env,capture_output=True,timeout=20)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--hermes-python',type=Path,help='Hermes 0.23+: the Python of the installed Hermes')
    p.add_argument('--hermes-home',type=Path,help='with --hermes-python: the profile HERMES_HOME')
    p.add_argument('--executable',type=Path,help='agent-browser mode only (Hermes before 0.23)');a=p.parse_args()
    try:
        if a.hermes_python:
            if not a.hermes_home:raise ValueError('--hermes-home is required with --hermes-python')
            print(json.dumps(hermes_smoke(a.hermes_python,a.hermes_home)))
        else:print(json.dumps(smoke(a.executable)))
    except Exception as error:
        print(json.dumps({'status':'failed','errorClass':type(error).__name__,'message':str(error) if isinstance(error,ValueError) else 'Inspect the installed browser version; no existing browser profile was used.'}))
        raise SystemExit(1)
