#!/usr/bin/env python3
"""Safe student preflight. Reads no env/auth/session/log files; installs nothing."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess

PROFILES=('marketer','copywriter','designer','editor','tech','assistant','sales')
COMMANDS=('hermes','node','npm','uv','ffmpeg','ffprobe','yt-dlp','tesseract',
          'pdftotext','pdfinfo','rg','psql','ollama','whisper-cli','agent-browser','bws','bw')
PACKAGES=('pg8000','mcp','playwright','faster-whisper','pypdf','python-docx')
# Hermes 0.23 removed agent-browser: browser_exec drives Hermes's own Chromium
# (`hermes pm install chromium`) and reads browser.user_data_dir.
HERMES_CHROMIUM_FROM=(0,23)


def hermes_version(root):
    """Installed Hermes version from its source tree (no process, no auth), or None."""
    root=Path(root)
    for source in (root/'hermes-agent',root.parent/'hermes-agent'):
        if source.is_symlink():continue
        stamp=source/'install-stamp.json'
        try:
            value=json.loads(stamp.read_text(encoding='utf-8')).get('baseVersion') if stamp.is_file() else None
        except (OSError,ValueError):value=None
        if not value:
            try:text=(source/'pyproject.toml').read_text(encoding='utf-8')
            except OSError:continue
            project=re.search(r'(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)',text)
            match=re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']',project.group(1) if project else '')
            value=match.group(1) if match else None
        # 0.23 keeps pyproject at the 0.0.0 placeholder; only install-stamp.json is authoritative.
        if isinstance(value,str) and value!='0.0.0' and re.fullmatch(r'\d+\.\d+\.\d+[0-9A-Za-z.+-]*',value):return value
    return None


def hermes_mcp_client(root):
    """True/False: the installed Hermes interpreter can import the MCP client, None if unknown.

    Hermes 0.23 keeps its packages in a PM store (~/.hermes/installs/...); venv/bin/python
    is only a launcher, so ask that interpreter instead of looking for files.
    """
    root=Path(root)
    for source in (root/'hermes-agent',root.parent/'hermes-agent'):
        if source.is_symlink():continue
        for python in (source/'venv/bin/python',source/'venv/Scripts/python.exe'):
            if not python.is_file():continue
            try:
                probe=subprocess.run([str(python),'-I','-c','import importlib.util,sys;sys.exit(0 if importlib.util.find_spec("mcp") else 3)'],
                                     capture_output=True,timeout=60)
            except (OSError,subprocess.SubprocessError):return None
            return True if probe.returncode==0 else False if probe.returncode==3 else None
    return None


def browser_mode(version):
    """'hermes-chromium' (0.23+), 'agent-browser' (older) or 'unknown'."""
    if not version:return 'unknown'
    major,minor=(int(x) for x in version.split('.')[:2])
    return 'hermes-chromium' if (major,minor)>=HERMES_CHROMIUM_FROM else 'agent-browser'


def scan(home=None,hermes_home=None):
    home=Path(home) if home is not None else Path.home()
    roots=[Path(hermes_home)] if hermes_home else [home/'.hermes',home/'AppData/Local/hermes/.hermes']
    profiles=[];version=None;mcp_client=None
    for root in roots:
        if root.is_symlink():continue
        if root.is_dir() and version is None:version=hermes_version(root);mcp_client=hermes_mcp_client(root)
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
            'hermesVersion':version,'browserMode':browser_mode(version),'hermesMcpClient':mcp_client,
            'commands':{name:shutil.which(name) for name in COMMANDS},
            'packagesInThisPython':packages,'webshellPresent':(shell/'server.mjs').is_file(),
            'tariffStatePresent':(home/'InfobizAgents/control/tariffs/last-apply.json').is_file(),
            'notes':['Package absence applies only to this interpreter; inspect dedicated runtimes separately.',
                     'Windows profiles require the actual home discovered by the official Hermes launcher.',
                     'Telegram Reader is intentionally excluded.',
                     'Hermes 0.23+ has no agent-browser: use its own Chromium (hermes pm install chromium).',
                     'hermesMcpClient false: MCP servers cannot connect; on 0.23 run hermes pm install --extra mcp.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--hermes-home',type=Path)
    a=p.parse_args();print(json.dumps(scan(hermes_home=a.hermes_home),ensure_ascii=False,indent=2))
