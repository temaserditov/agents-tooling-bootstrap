#!/usr/bin/env python3
"""Parse every PowerShell run: block of the Windows workflows the way the runner will.

shell: pwsh       -> PowerShell 7 reads the generated .ps1 as UTF-8.
shell: powershell -> Windows PowerShell 5.1 reads a BOM-less .ps1 in the ANSI code page
                     (cp1252 on GitHub runners): non-ASCII bytes become other characters,
                     some of them quote marks. Such a step must be pure ASCII.
Needs pwsh on PATH (the parser is the same for both editions).
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

ROOT=Path(__file__).resolve().parents[1]
PARSE=r'''$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$t,[ref]$e)
if($e.Count){$e|ForEach-Object{"{0}:{1} {2}" -f $_.Extent.StartLineNumber,$_.Extent.StartColumnNumber,$_.Message};exit 1}'''


def blocks():
    for wf in sorted((ROOT/'.github/workflows').glob('*.yml')):
        data=yaml.safe_load(wf.read_text(encoding='utf-8'))
        for job,spec in (data.get('jobs') or {}).items():
            default=((spec.get('defaults') or {}).get('run') or {}).get('shell')
            for i,step in enumerate(spec.get('steps') or []):
                if 'run' not in step:continue
                shell=step.get('shell',default)
                if shell in ('pwsh','powershell'):yield wf.name,job,step.get('name',f'step {i}'),shell,step['run']


def parse(text):
    with tempfile.TemporaryDirectory() as tmp:
        script=Path(tmp)/'parse.ps1';script.write_text('param([string]$Target)\n'+PARSE.replace('$args[0]','$Target'),encoding='utf-8')
        target=Path(tmp)/'step.ps1';target.write_text(text,encoding='utf-8')
        r=subprocess.run(['pwsh','-NoProfile','-NonInteractive','-File',str(script),str(target)],capture_output=True,text=True)
    return r.returncode==0,(r.stdout+r.stderr).strip()


def main():
    problems=[];count=0
    for wf,job,name,shell,text in blocks():
        count+=1
        ok,why=parse(text)
        if not ok:problems.append([wf,job,name,shell,'parse: '+why])
        if shell=='powershell':
            odd=sorted({c for c in text if ord(c)>127})
            if odd:problems.append([wf,job,name,shell,'non-ASCII in a Windows PowerShell 5.1 step: '+''.join(odd)[:40]])
            seen=text.encode('utf-8').decode('cp1252',errors='replace')
            ok,why=parse(seen)
            if not ok:problems.append([wf,job,name,shell,'parse as 5.1 reads it (cp1252): '+why])
    print(json.dumps({'blocks':count,'problems':problems},ensure_ascii=False,indent=1))
    sys.exit(1 if problems or not count else 0)


if __name__=='__main__':main()
