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

    Course installs on Mac/Ubuntu: hermes-agent/venv/bin/python (a launcher into the PM
    store). The official installers (Windows, and Mac/Linux since 0.23) keep it under
    <hermes>/installs/<id>/environments/<id>/venv; there every python in the Hermes tree
    (caches, profiles and node_modules aside) is asked.
    """
    root=Path(root);windows=platform.system()=='Windows' if windows is None else windows
    candidates=[]
    for source in (root/'hermes-agent',root.parent/'hermes-agent'):
        if source.is_symlink():continue
        for venv in ('venv','.venv'):
            candidates+=[source/venv/'bin/python',source/venv/'Scripts/python.exe']
    walk=root if windows else root/'installs'
    if walk.is_dir() and not walk.is_symlink():
        base=len(walk.parts)
        for dirpath,dirs,files in os.walk(walk):
            here=Path(dirpath)
            dirs[:]=sorted(d for d in dirs if d.lower() not in HERMES_PYTHON_SKIP and len(here.parts)-base<8
                           and not (here/d).is_symlink())
            if windows and 'python.exe' in files:candidates.append(here/'python.exe')
            # POSIX: environments/<id>/venv/bin/python; bare runtime generations fail the probe.
            if not windows and here.name=='bin' and here.parent.name=='venv' and 'python' in files:
                candidates.append(here/'python')
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
VENV_PYTHON_CHECK=('import sys,sqlite3,venv,ensurepip,platform;'
                   'assert (3,11)<=sys.version_info[:2]<=(3,13);'
                   'assert platform.machine()=="arm64";'
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


def uv_python_dir(uv,run=subprocess.run):
    """Where this uv installs Pythons (UV_PYTHON_INSTALL_DIR, XDG_DATA_HOME honoured), or None."""
    if not uv:return None
    try:out=run([str(uv),'python','dir'],capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=20).stdout.strip()
    except (OSError,subprocess.SubprocessError):return None
    return Path(out) if out and Path(out).is_absolute() else None


def venv_pythons(home,uv=None):
    """Interpreters for the package venv (3.11-3.13 arm64, venv+pip, SQLite with FTS5), best first.

    uv-managed Python first: it does not move when Hermes updates its own runtime.
    """
    found=[];seen=set()
    stores=[d for d in (uv_python_dir(uv),home/'.local/share/uv/python') if d]
    patterns=[*[(d,'cpython-3.1[23]*-macos-aarch64-none/bin/python3.1[23]') for d in stores],
              *[(d,'cpython-3.11*-macos-aarch64-none/bin/python3.11') for d in stores],
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


def hermes_uv(hermes_root):
    """uv shipped with Hermes (<hermes>/tools/uv-<version>-darwin-arm64/uv), newest first."""
    tools=Path(hermes_root)/'tools'
    found=sorted(tools.glob('uv-*/uv'),reverse=True) if tools.is_dir() and not tools.is_symlink() else []
    return [str(p) for p in found if p.is_file() and not p.is_symlink() and os.access(p,os.X_OK)]


def mac_state(home,hermes_root=None):
    version=mac_product_version();major=mac_major(version);silicon=apple_silicon()
    brew=Path('/opt/homebrew/bin/brew') if silicon else Path('/usr/local/bin/brew')
    homebrew=str(brew) if brew.is_file() else None
    backend,reason=mac_kb_backend(major,silicon,homebrew)
    uv=shutil.which('uv') or next(iter(hermes_uv(hermes_root or home/'.hermes')),None)
    return {'macosVersion':version,'macosMajor':major,'appleSilicon':silicon,
            'pythonNativeArm64':platform.machine()=='arm64','homebrew':homebrew,
            'kbBackend':backend,'kbBackendReason':reason,'venvPythonCandidates':venv_pythons(home,uv),'uv':uv}


# Another knowledge base an agent (or an earlier Codex run) built on its own: FOR-CODEX.md,
# section 12. Only names, table names and row counts are read, never the contents.
KB_HINT=re.compile(r'(?i)sqlite[-_]?vec|\bvec0\b|sqlite[-_]?vss|\bvss0\b|chromadb|lancedb|\bfaiss\b|qdrant|embedding|'
                   r'vector|knowledge[-_ ]?base|\bkb\b|\brag\b|farvater|фарватер|векторн|база знаний')
SKILL_HINT=re.compile(r'(?i)sqlite[-_]?vec|\bvec0\b|\bvss0\b|chromadb|lancedb|\bfaiss\b|farvater|фарватер')
# A real vector store: a vec0/vss virtual table, or a column named embedding(s)/vector(s).
VECTOR_COLUMN=r'[(,]\s*["`\[]?(embedding|embeddings|vector|vectors)["`\]]?'
VECTOR_SQL=re.compile(r'(?i)using\s+(vec0|vss0|vss)\b|'+VECTOR_COLUMN+r'\s+(blob|f32_blob|float|real|text|json)\b|'
                      +VECTOR_COLUMN+r'\s*[,)]')
FOREIGN_SKIP={'node_modules','.git','.cache','cache','logs','sessions','backups','integration-backups',
              'venv','.venv','site-packages','browser-profile-tooling','models','Library','.Trash','state-snapshots'}
# Hermes's own databases (sessions, cron, checks): never a knowledge base someone built.
HERMES_DB_NAMES={'state.db','executions.db','verification_evidence.db','projects.db'}
DB_SUFFIXES=('.sqlite','.sqlite3','.db')
# Bounds of the scan: it runs before anything is installed and must never hang.
SCAN_SECONDS=20
SCAN_FILES=20000
SQLITE_COPY_BYTES=256*1024*1024
SQLITE_SECONDS=3
SKILL_READ_BYTES=20000


class ScanBudget:
    def __init__(self,seconds=SCAN_SECONDS,files=SCAN_FILES,clock=None):
        import time
        self.clock=clock or time.monotonic;self.deadline=self.clock()+seconds;self.files=files;self.exhausted=False
    def spend(self,count=1):
        self.files-=count
        if self.files<0 or self.clock()>self.deadline:self.exhausted=True
        return not self.exhausted


# Internal tables each virtual-table module creates for itself, by module. A table of an
# unknown module is counted: counting too much can never make a full base look empty.
SHADOW_TABLES={'fts5':r'data|idx|content|docsize|config','fts4':r'content|segments|segdir|docsize|stat',
               'fts3':r'content|segments|segdir|docsize|stat','rtree':r'node|parent|rowid',
               'vec0':r'chunks|rowids|info|auxiliary|vector_chunks\d+|metadatachunks\d+|metadatatext\d+',
               'vss0':r'index|data'}


def copy_bounded(source,target,deadline,clock):
    """Copy in pieces; False when the scan's time ran out before the end."""
    with open(source,'rb') as src,open(target,'wb') as dst:
        while True:
            if clock()>deadline:return False
            piece=src.read(4*1024*1024)
            if not piece:return True
            dst.write(piece)


def file_state(path):
    try:st=os.stat(path);return (st.st_size,st.st_mtime_ns)
    except OSError:return None


def sqlite_summary(path,attempts=3,budget=None):
    """Tables and row counts of a database that stores vectors, else None. Never touches the original.

    SQLite opens even read-only WAL bases with side effects (-shm/-wal files), so the main file
    and its -wal are copied into a private temporary folder and only the copy is opened; the
    copy includes the WAL, so rows not yet checkpointed are counted too. A copy is trusted only
    when neither file changed while it was taken. Whatever cannot be read safely is reported
    with rowsKnown false (contents unknown), never as empty.
    """
    import sqlite3,tempfile,time
    path=Path(path);wal=Path(str(path)+'-wal')
    unknown=lambda reason:{'rowsKnown':False,'tables':{},'reason':reason}
    budget=budget or ScanBudget()
    before=(file_state(path),file_state(wal))
    if before[0] is None:return unknown('unreadable')
    with tempfile.TemporaryDirectory(prefix='agents-inventory-') as tmp:
        copy=Path(tmp)/'copy.sqlite'
        for _ in range(attempts):
            if before[0] is None:return unknown('unreadable')
            if before[0][0]+(before[1][0] if before[1] else 0)>SQLITE_COPY_BYTES:return unknown('too-large')
            try:
                done=copy_bounded(path,copy,budget.deadline,budget.clock)
                if done and before[1]:done=copy_bounded(wal,Path(str(copy)+'-wal'),budget.deadline,budget.clock)
            except OSError:return unknown('unreadable')
            if not done:budget.exhausted=True;return unknown('time-limit')
            after=(file_state(path),file_state(wal))
            if after==before:break
            before=after
            for extra in (Path(str(copy)+'-wal'),):
                if extra.exists():extra.unlink()
        else:return unknown('changing')
        try:db=sqlite3.connect(str(copy),timeout=1)
        except sqlite3.Error:return unknown('unreadable')
        deadline=time.monotonic()+SQLITE_SECONDS
        db.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,10000)
        try:
            rows=db.execute("select name,sql from sqlite_master where type='table'").fetchall()
            names={name for name,_ in rows}
            if {'kb_meta','chunks','documents'}<=names:return None  # this package's own base
            vector=[n for n,sql in rows if VECTOR_SQL.search(sql or '')]
            if not vector:return None
            virtual={}
            for n,sql in rows:
                module=re.match(r'(?is)\s*create\s+virtual\s+table\s+.*?\s+using\s+(\w+)',sql or '')
                if module and module.group(1).lower() in SHADOW_TABLES:virtual[n]=SHADOW_TABLES[module.group(1).lower()]
            tables={};known=True
            for name,sql in rows:
                # Only the known internal tables of a virtual table (docs_data, vec_chunks...) are
                # left out; every other table, docs_archive included, is counted.
                if name.startswith('sqlite_') or any(name.startswith(v+'_') and re.fullmatch('(?:'+suffixes+')',name[len(v)+1:])
                                                     for v,suffixes in virtual.items()):continue
                try:tables[name]=db.execute('select count(*) from "'+name.replace('"','""')+'"').fetchone()[0]
                except sqlite3.Error:tables[name]=None;known=False
            return {'rowsKnown':known,'tables':tables,'vectorTables':sorted(vector)}
        except sqlite3.Error:return unknown('unreadable')
        finally:db.close()


def walk_limited(base,depth,budget=None):
    base=Path(base)
    if not base.is_dir() or base.is_symlink():return
    start=len(base.parts)
    for dirpath,dirs,files in os.walk(base):
        here=Path(dirpath)
        if budget is not None and not budget.spend(len(files)+1):return
        dirs[:]=sorted(d for d in dirs if d not in FOREIGN_SKIP and not (here/d).is_symlink() and len(here.parts)-start<depth)
        yield here,dirs,files


def skill_head(path):
    try:
        with open(path,'rb') as handle:return handle.read(SKILL_READ_BYTES).decode('utf-8',errors='replace')
    except OSError:return None


def foreign_knowledge_bases(home,profiles,yaml=None,budget=None):
    """MCP servers, skills and SQLite files that look like a knowledge base not from this package."""
    found=[];budget=budget or ScanBudget()
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
        for here,dirs,files in walk_limited(folder/'skills',3,budget):
            if 'SKILL.md' in files:
                text=skill_head(here/'SKILL.md')
                if text is None:continue
                if SKILL_HINT.search(here.name+' '+text) or \
                   (KB_HINT.search(here.name) and re.search(r'(?i)embedding|вектор',text)):
                    found.append({'kind':'skill','profile':profile['id'],'name':here.name,'path':str(here)})
    seen=set()
    # The profiles inventory actually found (also %LOCALAPPDATA%\\hermes or a custom --hermes-home).
    bases=[home/'InfobizAgents',*[Path(p['path']) for p in profiles],
           *[p for p in sorted(home.glob('*')) if p.is_dir() and not p.is_symlink() and KB_HINT.search(p.name)]]
    hermes_areas=[Path(os.path.normcase(str(Path(p['path']).absolute()))) for p in profiles]
    for base in bases:
        for here,dirs,files in walk_limited(base,4,budget):
            # Path components, not text: profiles/tech-kb is not inside profiles/tech.
            spot=Path(os.path.normcase(str(here.absolute())))
            in_hermes=any(spot==a or a in spot.parents for a in hermes_areas)
            if here.name=='tooling' and (here/'student_tools.py').is_file():
                dirs[:]=[d for d in dirs if d!='kb']
            for name in files:
                # Hermes's own databases are skipped only inside a Hermes profile.
                if not name.lower().endswith(DB_SUFFIXES) or (in_hermes and name.lower() in HERMES_DB_NAMES):continue
                path=here/name;key=os.path.normcase(str(path))
                if key in seen or path.is_symlink():continue
                if not budget.spend(0):break
                seen.add(key);summary=sqlite_summary(path,budget=budget)
                if summary is None:continue
                # Unread contents: a candidate to check by hand, never a confirmed or empty base.
                kind='sqlite' if summary.get('rowsKnown') else 'sqlite-unchecked'
                found.append({'kind':kind,'path':str(path),'bytes':path.stat().st_size,**summary})
            if budget.exhausted:break
    if budget.exhausted:found.append({'kind':'scan-incomplete','reason':'time-or-file-limit'})
    return found


def scan(home=None,hermes_home=None,foreign=True):
    home=Path(home) if home is not None else Path.home()
    # Windows: the official Hermes lives in %LOCALAPPDATA%\\hermes (Windows v3); the
    # nested .hermes is the layout of older Windows helpers.
    local=os.environ.get('LOCALAPPDATA')
    windows=[Path(local)/'hermes'] if local and platform.system()=='Windows' else []
    roots=[Path(hermes_home)] if hermes_home else [home/'.hermes',*windows,home/'AppData/Local/hermes',home/'AppData/Local/hermes/.hermes']
    profiles=[];version=None;mcp_client=None;hermes_py=None;hermes_root=None
    seen=set()
    for root in roots:
        key=os.path.normcase(str(root.absolute()))
        if key in seen or root.is_symlink():continue
        seen.add(key)
        if root.is_dir() and version is None:
            hermes_root=root
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
    if platform.system()=='Darwin':extra={'mac':mac_state(home,hermes_root)}
    if foreign:
        try:extra['otherKnowledgeBases']=foreign_knowledge_bases(home,profiles)
        except Exception as error:extra['otherKnowledgeBases']=[{'kind':'scan-failed','errorClass':type(error).__name__}]
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
                     'hermesPython: the interpreter Hermes runs on (browser-smoke --hermes-python); null if not found.',
                     'otherKnowledgeBases: a knowledge base not from this package (section 12); never delete it.',
                     'mac.kbBackend: the knowledge-base backend for this Mac (FOR-CODEX.md section 11); null = stop.']}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--hermes-home',type=Path)
    # Windows: ASCII JSON, so no console code page can break it.
    a=p.parse_args();print(json.dumps(scan(hermes_home=a.hermes_home),ensure_ascii=platform.system()=='Windows',indent=2))
