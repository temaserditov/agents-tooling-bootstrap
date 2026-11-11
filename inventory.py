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


HERMES_PYTHON_SKIP={'cache','profiles','node_modules','.git','skills','logs','sessions','memories','backups'}


def probe(python,code,timeout=60):
    """Exit code of a tiny isolated check in that interpreter, or None when it cannot run."""
    try:
        return subprocess.run([str(python),'-I','-c',code],capture_output=True,timeout=timeout,
                              stdin=subprocess.DEVNULL).returncode
    except (OSError,subprocess.SubprocessError):return None


def hermes_python(root,windows=None):
    """The interpreter Hermes itself runs on (it imports hermes_cli), or None.

    Mac/Ubuntu: hermes-agent/venv/bin/python (a launcher into the PM store). The official
    Windows installer keeps it elsewhere under %LOCALAPPDATA%\\hermes, so there every
    python.exe in the Hermes tree (caches, profiles and node_modules aside) is asked.
    """
    root=Path(root);windows=platform.system()=='Windows' if windows is None else windows
    candidates=[]
    for source in (root/'hermes-agent',root.parent/'hermes-agent'):
        if source.is_symlink():continue
        for venv in ('venv','.venv'):
            candidates+=[source/venv/'bin/python',source/venv/'Scripts/python.exe']
    if windows and root.is_dir() and not root.is_symlink():
        base=len(root.parts)
        for dirpath,dirs,files in os.walk(root):
            here=Path(dirpath)
            dirs[:]=sorted(d for d in dirs if d.lower() not in HERMES_PYTHON_SKIP and len(here.parts)-base<8
                           and not (here/d).is_symlink())
            if 'python.exe' in files:candidates.append(here/'python.exe')
    seen=set()
    for python in candidates:
        key=os.path.normcase(str(python))
        if key in seen or not python.is_file():continue
        seen.add(key)
        if probe(python,'import hermes_cli')==0:return python
    return None


def hermes_mcp_client(root,python=None):
    """True/False: the installed Hermes interpreter can import the MCP client, None if unknown.

    Hermes 0.23 keeps its packages in a PM store (~/.hermes/installs/...); venv/bin/python
    is only a launcher, so ask that interpreter instead of looking for files.
    """
    python=python or hermes_python(root)
    if python is None:return None
    code=probe(python,'import importlib.util,sys;sys.exit(0 if importlib.util.find_spec("mcp") else 3)')
    return True if code==0 else False if code==3 else None


def browser_mode(version):
    """'hermes-chromium' (0.23+), 'agent-browser' (older) or 'unknown'."""
    if not version:return 'unknown'
    major,minor=(int(x) for x in version.split('.')[:2])
    return 'hermes-chromium' if (major,minor)>=HERMES_CHROMIUM_FROM else 'agent-browser'


def windows_arm64():
    """Windows only: True on ARM64, also for an x64 Python under emulation (IsWow64Process2)."""
    if platform.machine().upper() in ('ARM64','AARCH64'):return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32=ctypes.WinDLL('kernel32')
        check=kernel32.IsWow64Process2
    except (OSError,AttributeError):return False
    check.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.USHORT),ctypes.POINTER(wintypes.USHORT)];check.restype=wintypes.BOOL
    kernel32.GetCurrentProcess.restype=wintypes.HANDLE
    process=wintypes.USHORT();native=wintypes.USHORT()
    if not check(kernel32.GetCurrentProcess(),ctypes.byref(process),ctypes.byref(native)):return False
    return native.value==0xAA64


def scan(home=None,hermes_home=None):
    home=Path(home) if home is not None else Path.home()
    # Windows: the official Hermes lives in %LOCALAPPDATA%\\hermes (Windows v3); the
    # nested .hermes is the layout of older Windows helpers.
    local=os.environ.get('LOCALAPPDATA')
    windows=[Path(local)/'hermes'] if local and platform.system()=='Windows' else []
    roots=[Path(hermes_home)] if hermes_home else [home/'.hermes',*windows,home/'AppData/Local/hermes',home/'AppData/Local/hermes/.hermes']
    profiles=[];version=None;mcp_client=None;hermes_py=None
    seen=set()
    for root in roots:
        key=os.path.normcase(str(root.absolute()))
        if key in seen or root.is_symlink():continue
        seen.add(key)
        if root.is_dir() and version is None:
            version=hermes_version(root);hermes_py=hermes_python(root);mcp_client=hermes_mcp_client(root,hermes_py)
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
    extra={'windowsArm64':windows_arm64()} if platform.system()=='Windows' else {}
    return {**extra,'schemaVersion':1,'os':platform.system(),'architecture':platform.machine(),
            'freeDiskBytes':shutil.disk_usage(home).free,'profiles':profiles,
            'hermesVersion':version,'browserMode':browser_mode(version),'hermesMcpClient':mcp_client,'hermesPython':str(hermes_py) if hermes_py else None,
            'commands':{name:shutil.which(name) for name in COMMANDS},
            'packagesInThisPython':packages,'webshellPresent':(shell/'server.mjs').is_file(),
            'tariffStatePresent':(home/'InfobizAgents/control/tariffs/last-apply.json').is_file(),
            'notes':['Package absence applies only to this interpreter; inspect dedicated runtimes separately.',
                     'Windows profiles require the actual home discovered by the official Hermes launcher.',
                     'Telegram Reader is intentionally excluded.',
                     'Hermes 0.23+ has no agent-browser: use its own Chromium (hermes pm install chromium).',
                     'hermesMcpClient false: MCP servers cannot connect; on 0.23 run hermes pm install --extra mcp.',
                     'hermesPython: the interpreter Hermes runs on (browser-smoke --hermes-python); null if not found.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--hermes-home',type=Path)
    # Windows: ASCII JSON, so no console code page can break it.
    a=p.parse_args();print(json.dumps(scan(hermes_home=a.hermes_home),ensure_ascii=platform.system()=='Windows',indent=2))
