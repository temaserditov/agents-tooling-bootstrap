#!/usr/bin/env python3
"""Safe student preflight. Reads no env/auth/session/log files; installs nothing."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil

PROFILES=('marketer','copywriter','designer','editor','tech','assistant','sales')
COMMANDS=('hermes','node','npm','uv','ffmpeg','ffprobe','yt-dlp','tesseract',
          'pdftotext','pdfinfo','rg','psql','ollama','whisper-cli','agent-browser','bws','bw')
PACKAGES=('pg8000','mcp','playwright','faster-whisper','pypdf','python-docx')


def scan(home=None,hermes_home=None):
    home=Path(home) if home is not None else Path.home()
    roots=[Path(hermes_home)] if hermes_home else [home/'.hermes',home/'AppData/Local/hermes/.hermes']
    profiles=[]
    for root in roots:
        if root.is_symlink():continue
        for name in PROFILES:
            folder=root/'profiles'/name
            if not folder.is_dir() or folder.is_symlink():continue
            skills=folder/'skills'
            profiles.append({'id':name,'path':str(folder),'skillsRoot':str(skills),
                             'brainPresent':(folder/'AGENTS.md').is_file(),
                             'soulPresent':(folder/'SOUL.md').is_file()})
    packages={}
    for name in PACKAGES:
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    shell=home/'InfobizAgents/web-shell'
    return {'schemaVersion':1,'os':platform.system(),'architecture':platform.machine(),
            'freeDiskBytes':shutil.disk_usage(home).free,'profiles':profiles,
            'commands':{name:shutil.which(name) for name in COMMANDS},
            'packagesInThisPython':packages,'webshellPresent':(shell/'server.mjs').is_file(),
            'tariffStatePresent':(home/'InfobizAgents/control/tariffs/last-apply.json').is_file(),
            'notes':['Package absence applies only to this interpreter; inspect dedicated runtimes separately.',
                     'Windows profiles require the actual home discovered by the official Hermes launcher.',
                     'Telegram Reader is intentionally excluded.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--hermes-home',type=Path)
    a=p.parse_args();print(json.dumps(scan(hermes_home=a.hermes_home),ensure_ascii=False,indent=2))
