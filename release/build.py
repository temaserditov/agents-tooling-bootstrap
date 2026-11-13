#!/usr/bin/env python3
"""Deterministic public release: ZIP + manifest + SHA256SUMS + INSTALL-AGENTS-TOOLS.md.

Built from the committed tree (HEAD), never from a dirty working copy, and refused
when the bundle carries anything personal: host paths, e-mail addresses, learner
identifiers, keys or tokens.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
EXCLUDE=re.compile(r'^(\.github/|release/)')
PRIVATE=[
    ('host path',re.compile(r'(?<![\w.])/(?:Users|home)/(?!x/|user/|student/|USER/|<|\$|\{)[A-Za-z0-9._-]+')),
    ('host path',re.compile(r'(?i)C:[\\/]+Users[\\/]+(?!USER\b|<|%|\$|Public\b|Default\b)[A-Za-z0-9._-]+')),
    ('host volume',re.compile(r'/Volumes/[A-Za-z0-9]')),
    ('e-mail',re.compile(r'[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9-]+\.[A-Za-z]{2,}')),
    ('learner id',re.compile(r'DIAG-[A-Z]{2,}-[A-Z0-9]{6,}')),
    ('telegram handle',re.compile(r'(?<![\w/])t\.me/[A-Za-z0-9_]{5,}')),
    ('api key',re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}|0\.[A-Za-z0-9]{20,}\.[A-Za-z0-9]{20,})')),
    ('bot token',re.compile(r'\b\d{7,12}:[A-Za-z0-9_-]{30,}\b')),
    ('private key',re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
]
# Fixtures and docs legitimately mention a few of these shapes; they are listed exactly.
ALLOWED={'git@github.com'}


def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)


def private_findings(files):
    found=[]
    for name,data in files.items():
        if re.fullmatch(r'tests/fixtures/[a-z0-9-]+\.wav',name):continue  # synthetic speech fixture
        try:text=data.decode('utf-8')
        except UnicodeDecodeError:found.append((name,'binary file'));continue
        for label,pattern in PRIVATE:
            for match in pattern.finditer(text):
                if match.group(0) in ALLOWED:continue
                if label=='private key' and 'tests/' in name:continue
                found.append((name,label+': '+match.group(0)[:40]))
    return found


def committed_files():
    names=[n for n in git('ls-files','-z').decode().split('\0') if n and not EXCLUDE.match(n)]
    return {n:git('show','HEAD:'+n) for n in sorted(names)}


def build(version,output,qa=False):
    if not re.fullmatch(r'v\d{4}\.\d{2}\.\d{2}\.\d+',version):raise ValueError('Version looks like v2026.11.10.1')
    if git('status','--porcelain').strip():raise ValueError('Commit everything first: the release is built from HEAD')
    commit=git('rev-parse','HEAD').decode().strip()
    # ZIP entry dates are the release date from the version: same tree, same bytes.
    stamp=(*(int(x) for x in version[1:].split('.')[:3]),0,0,0)
    files=committed_files()
    bad=private_findings(files)
    if bad:raise ValueError('Personal data or secrets in the public bundle: '+json.dumps(bad,ensure_ascii=False))
    manifest={'schemaVersion':1,'version':version,'sourceRepository':'temaserditov/agents-tooling-bootstrap',
              'sourceCommit':commit,'files':{n:hashlib.sha256(d).hexdigest() for n,d in files.items()}}
    files['manifest.json']=(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n').encode()
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        for name in sorted(files):
            info=zipfile.ZipInfo(name,stamp);info.external_attr=0o644<<16;info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,files[name],compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    payload=buffer.getvalue();sha=hashlib.sha256(payload).hexdigest();zip_name=f'agents-tooling-{version}.zip'
    header=(ROOT/'release/INSTALL-HEADER.md').read_text(encoding='utf-8')
    for key,value in {'VERSION':version,'ZIP_SHA256':sha,'COMMIT':commit}.items():header=header.replace('{{'+key+'}}',value)
    if '{{' in header:raise ValueError('Unfilled placeholder in INSTALL header')
    if not qa and any(m.encode() in files['FOR-CODEX.md'] or m in header for m in ('WINDOWS_STATUS','MAC_FILE_STATUS')):
        raise ValueError('Set the Windows x64 status in FOR-CODEX.md and release/INSTALL-HEADER.md from the Windows x64 run first')
    install=(header+files['FOR-CODEX.md'].decode('utf-8')).encode()
    exports={zip_name:payload,'SHA256SUMS':(sha+'  '+zip_name+'\n').encode(),'INSTALL-AGENTS-TOOLS.md':install}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name,data in exports.items():
        target=output/name
        if target.exists() and target.read_bytes()!=data:raise ValueError('Output holds a different release: '+name)
        target.write_bytes(data)
    return {'zip':zip_name,'sha256':sha,'commit':commit,'files':len(files)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--version',required=True);p.add_argument('--output',required=True)
    p.add_argument('--qa',action='store_true',help='acceptance-VM build while the Windows row is still pending; never published')
    a=p.parse_args()
    try:print(json.dumps(build(a.version,a.output,a.qa)))
    except ValueError as error:print(str(error),file=sys.stderr);sys.exit(1)
