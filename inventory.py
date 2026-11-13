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


# Mac knowledge base (FOR-CODEX.md, section 11). Homebrew publishes PostgreSQL/pgvector
# bottles for Apple Silicon macOS 15, 26 and 27 only; this package verified the PostgreSQL
# path on macOS 15. Everything else that can run it takes backend file (no services).
MAC_POSTGRES_VERIFIED=(15,)
MAC_FILE_MIN_MAJOR=14
VENV_PYTHON_CHECK=('import sys,sqlite3,venv,ensurepip;'
                   'assert (3,11)<=sys.version_info[:2]<=(3,13);'
                   'sqlite3.connect(":memory:").execute("create virtual table t using fts5(x)")')


def mac_major(text):
    found=re.match(r'\s*(\d+)',text or '')
    return int(found.group(1)) if found else 0


def mac_product_version(run=subprocess.run):
    """macOS version; a Python built with an old SDK reports 10.16 instead, so sw_vers decides."""
    try:
        out=run(['/usr/bin/sw_vers','-productVersion'],capture_output=True,text=True,stdin=subprocess.DEVNULL,
                timeout=10,env={**os.environ,'SYSTEM_VERSION_COMPAT':'0'}).stdout.strip()
        if re.fullmatch(r'\d+(\.\d+)*',out):return out
    except (OSError,subprocess.SubprocessError):pass
    return platform.mac_ver()[0]


def apple_silicon(run=subprocess.run):
    """True on Apple Silicon, also when this Python itself runs under Rosetta."""
    if platform.machine()=='arm64':return True
    try:out=run(['/usr/sbin/sysctl','-n','hw.optional.arm64'],capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=10).stdout
    except (OSError,subprocess.SubprocessError):return False
    return out.strip()=='1'


def mac_kb_backend(major,silicon,homebrew):
    """(backend, reason): which knowledge-base backend this Mac gets; backend None = stop."""
    if not silicon:return None,'intel-mac-unsupported'
    if major<MAC_FILE_MIN_MAJOR:return None,'macos-older-than-14-unsupported'
    if major in MAC_POSTGRES_VERIFIED and homebrew:return 'postgres','macos15-homebrew'
    if major in MAC_POSTGRES_VERIFIED:return 'file','no-homebrew'
    if major<min(MAC_POSTGRES_VERIFIED):return 'file','macos14-no-homebrew-bottles'
    return 'file','macos-newer-than-verified-postgres'


def venv_pythons(home):
    """Interpreters for the package venv (3.11-3.13, venv+pip, SQLite with FTS5), best first.

    uv-managed Python first: it does not move when Hermes updates its own runtime.
    """
    found=[];seen=set()
    patterns=[(home/'.local/share/uv/python','cpython-3.1[23]*-macos-aarch64-none/bin/python3.1[23]'),
              (home/'.local/share/uv/python','cpython-3.11*-macos-aarch64-none/bin/python3.11'),
              (Path('/Library/Frameworks/Python.framework/Versions'),'3.1[123]/bin/python3.1[123]'),
              (Path('/opt/homebrew/bin'),'python3.1[123]')]
    for base,pattern in patterns:
        if not base.is_dir():continue
        for python in sorted(base.glob(pattern),reverse=True):
            key=str(python.resolve()) if python.exists() else None
            if not key or key in seen or not python.is_file():continue
            seen.add(key)
            if probe(python,VENV_PYTHON_CHECK)==0:found.append(str(python))
    return found


def mac_state(home):
    version=mac_product_version();major=mac_major(version);silicon=apple_silicon()
    brew=Path('/opt/homebrew/bin/brew') if silicon else Path('/usr/local/bin/brew')
    homebrew=str(brew) if brew.is_file() else None
    backend,reason=mac_kb_backend(major,silicon,homebrew)
    return {'macosVersion':version,'macosMajor':major,'appleSilicon':silicon,
            'pythonNativeArm64':platform.machine()=='arm64','homebrew':homebrew,
            'kbBackend':backend,'kbBackendReason':reason,'venvPythonCandidates':venv_pythons(home)}


# Another knowledge base an agent (or an earlier Codex run) built on its own: FOR-CODEX.md,
# section 12. Only names, table names and row counts are read, never the contents.
KB_HINT=re.compile(r'(?i)sqlite[-_]?vec|\bvec0\b|sqlite[-_]?vss|\bvss0\b|chromadb|lancedb|\bfaiss\b|qdrant|embedding|'
                   r'vector|knowledge[-_ ]?base|\bkb\b|\brag\b|farvater|фарватер|векторн|база знаний')
SKILL_HINT=re.compile(r'(?i)sqlite[-_]?vec|\bvec0\b|\bvss0\b|chromadb|lancedb|\bfaiss\b|farvater|фарватер')
VECTOR_SQL=re.compile(r'(?i)using\s+(vec0|vss0|vss)|embedding|vector')
FOREIGN_SKIP={'node_modules','.git','.cache','cache','logs','sessions','backups','integration-backups',
              'venv','.venv','site-packages','browser-profile-tooling','models','Library','.Trash'}
DB_SUFFIXES=('.sqlite','.sqlite3','.db')


def sqlite_summary(path):
    """Read-only: tables and row counts of a database that stores vectors, else None."""
    import sqlite3
    from urllib.parse import quote
    uri='file:'+quote(str(path))+'?mode=ro';extra={}
    try:
        db=sqlite3.connect(uri,uri=True,timeout=2);rows=db.execute("select name,sql from sqlite_master where type='table'").fetchall()
    except sqlite3.Error:
        # A WAL base without a -shm file opens read-only only as immutable (the -wal part unread).
        try:
            db=sqlite3.connect(uri+'&immutable=1',uri=True,timeout=2);extra={'walNotRead':True}
            rows=db.execute("select name,sql from sqlite_master where type='table'").fetchall()
        except sqlite3.Error:return {'tables':{},'unreadable':True} if KB_HINT.search(path.name) else None
    try:
        names={name for name,_ in rows}
        if {'kb_meta','chunks','documents'}<=names:return None  # this package's own base
        if not any(VECTOR_SQL.search(sql or '') for _,sql in rows):return None
        tables={}
        for name,sql in rows:
            if name.startswith('sqlite_') or re.search(r'_(data|idx|content|docsize|config|chunks|rowids|info|vector_chunks\d*)$',name):continue
            try:tables[name]=db.execute('select count(*) from "'+name.replace('"','""')+'"').fetchone()[0]
            except sqlite3.Error:tables[name]=None
        return {'tables':tables,'vectorTables':sorted(n for n,sql in rows if re.search(r'(?i)using\s+(vec0|vss0|vss)',sql or '')),**extra}
    except sqlite3.Error:return {'tables':{},'unreadable':True}
    finally:db.close()


def walk_limited(base,depth):
    base=Path(base)
    if not base.is_dir() or base.is_symlink():return
    start=len(base.parts)
    for dirpath,dirs,files in os.walk(base):
        here=Path(dirpath)
        dirs[:]=sorted(d for d in dirs if d not in FOREIGN_SKIP and not (here/d).is_symlink() and len(here.parts)-start<depth)
        yield here,dirs,files


def foreign_knowledge_bases(home,profiles,yaml=None):
    """MCP servers, skills and SQLite files that look like a knowledge base not from this package."""
    found=[]
    if yaml is None:
        try:import yaml
        except ImportError:yaml=False
    for profile in profiles:
        folder=Path(profile['path']);config=folder/'config.yaml'
        if yaml and config.is_file() and not config.is_symlink():
            try:current=yaml.safe_load(config.read_text(encoding='utf-8')) or {}
            except Exception:current={}
            servers=current.get('mcp_servers') if isinstance(current,dict) else None
            for name,entry in (servers.items() if isinstance(servers,dict) else []):
                if name=='student-agent-kb':continue
                text=name+' '+json.dumps(entry,ensure_ascii=False) if isinstance(entry,(dict,list)) else name
                if KB_HINT.search(text):
                    found.append({'kind':'mcp','profile':profile['id'],'name':name,
                                  'enabled':not (isinstance(entry,dict) and entry.get('enabled') is False)})
        for here,dirs,files in walk_limited(folder/'skills',3):
            if 'SKILL.md' in files:
                try:text=(here/'SKILL.md').read_text(encoding='utf-8',errors='replace')[:20000]
                except OSError:continue
                head=here.name+' '+text
                if SKILL_HINT.search(head) or \
                   (KB_HINT.search(here.name) and re.search(r'(?i)embedding|вектор',text)):
                    found.append({'kind':'skill','profile':profile['id'],'name':here.name,'path':str(here)})
    seen=set()
    bases=[home/'InfobizAgents',home/'.hermes/profiles',*[p for p in sorted(home.glob('*')) if p.is_dir() and KB_HINT.search(p.name)]]
    for base in bases:
        for here,dirs,files in walk_limited(base,4):
            if here.name=='tooling' and (here/'student_tools.py').is_file():
                dirs[:]=[d for d in dirs if d!='kb']
            for name in files:
                if not name.lower().endswith(DB_SUFFIXES):continue
                path=here/name;key=str(path)
                if key in seen or path.is_symlink():continue
                seen.add(key);summary=sqlite_summary(path)
                if summary:found.append({'kind':'sqlite','path':key,'bytes':path.stat().st_size,**summary})
    return found


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
    if platform.system()=='Darwin':extra={'mac':mac_state(home)}
    try:foreign=foreign_knowledge_bases(home,profiles)
    except Exception as error:foreign=[{'kind':'scan-failed','errorClass':type(error).__name__}]
    return {**extra,'schemaVersion':1,'otherKnowledgeBases':foreign,'os':platform.system(),'architecture':platform.machine(),
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
                     'hermesPython: the interpreter Hermes runs on (browser-smoke --hermes-python); null if not found.',
                     'otherKnowledgeBases: a knowledge base not from this package (section 12); never delete it.',
                     'mac.kbBackend: the knowledge-base backend for this Mac (FOR-CODEX.md section 11); null = stop.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--hermes-home',type=Path)
    # Windows: ASCII JSON, so no console code page can break it.
    a=p.parse_args();print(json.dumps(scan(hermes_home=a.hermes_home),ensure_ascii=platform.system()=='Windows',indent=2))
