#!/usr/bin/env python3
"""An isolated, explicitly headless browser test; no existing profiles or logins."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid


def smoke(executable=None):
    cli=shutil.which('agent-browser')
    if not cli:raise ValueError('agent-browser is not installed in this process PATH')
    if executable and not Path(executable).is_file():raise ValueError('Browser executable missing')
    with tempfile.TemporaryDirectory(prefix='ab-',dir='/tmp' if os.name!='nt' else None) as tmp:
        root=Path(tmp).resolve();cfg=root/'browser.json';cfg.write_text('{"headed":false}')
        session='at-'+uuid.uuid4().hex[:8]
        env={'PATH':os.environ.get('PATH',''),'HOME':str(root),'USERPROFILE':str(root),
             'TMPDIR':str(root),'AGENT_BROWSER_HEADED':'false','AGENT_BROWSER_SOCKET_DIR':str(root)}
        # Required Windows runtime locations are metadata, not credentials.
        for name in ['SYSTEMROOT','WINDIR','COMSPEC']:
            if name in os.environ:env[name]=os.environ[name]
        args=[cli,'--config',str(cfg),'--headed','false','--session',session,'--profile',str(root/'profile')]
        if executable:args+=['--executable-path',str(Path(executable).resolve())]
        try:
            result=subprocess.run(args+['open','data:text/html,<title>Agents tooling smoke</title><h1>Fixture</h1>'],env=env,capture_output=True,timeout=45)
            if result.returncode:raise ValueError('Isolated headless open failed; raw output suppressed')
            title=subprocess.run(args+['get','title'],env=env,capture_output=True,timeout=20)
            if title.returncode or b'Agents tooling smoke' not in title.stdout:raise ValueError('Browser content check failed')
            return {'headless':True,'isolatedProfile':True,'syntheticPageVerified':True}
        finally:
            subprocess.run(args+['close'],env=env,capture_output=True,timeout=20)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--executable',type=Path);a=p.parse_args()
    try:print(json.dumps(smoke(a.executable)))
    except Exception as error:
        print(json.dumps({'status':'failed','errorClass':type(error).__name__,'message':'Inspect the installed browser version; no existing browser profile was used.'}))
        raise SystemExit(1)
