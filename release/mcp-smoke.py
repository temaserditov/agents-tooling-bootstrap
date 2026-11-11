#!/usr/bin/env python3
"""CI only: a real stdio MCP session with student-agent-kb (initialize, tools/list, tools/call).

--via-launcher starts the server through student_tools.launch_mcp, the path vault-mcp takes:
on Windows a child process on the inherited stdio handles, on POSIX exec.
Prints one JSON line; exit code 0 only when the expected file is the top result.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
LAUNCHER='''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location("student_tools",sys.argv[1])
tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
sys.exit(tools.launch_mcp([sys.executable,"-B",sys.argv[1],"--config",sys.argv[2],"mcp"],dict(os.environ)))
'''


async def session(settings,query,profile,project,via_launcher):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    script=str(ROOT/'student_tools.py')
    args=['-B','-c',LAUNCHER,script,str(settings)] if via_launcher else ['-B',script,'--config',str(settings),'mcp']
    keep={k:v for k,v in os.environ.items() if k in {'PATH','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','LOCALAPPDATA','APPDATA',
                                                       'USERPROFILE','HOME','PYTHONIOENCODING'}}
    params=StdioServerParameters(command=sys.executable,args=args,env=keep)
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as s:
            init=await s.initialize()
            tools=[t.name for t in (await s.list_tools()).tools]
            response=await s.call_tool('agent_kb_search',{'query':query,'profile':profile,'project':project})
            return init.serverInfo.name,tools,json.loads(response.content[0].text)


def main():
    p=argparse.ArgumentParser();p.add_argument('--settings',type=Path,required=True);p.add_argument('--query',required=True)
    p.add_argument('--expect',required=True,help='file name expected as the top result')
    p.add_argument('--profile',default='tech');p.add_argument('--project',default='__skills__')
    p.add_argument('--via-launcher',action='store_true');a=p.parse_args()
    name,tools,answer=asyncio.run(session(a.settings.resolve(),a.query,a.profile,a.project,a.via_launcher))
    results=answer.get('results') or []
    top=Path(results[0]['source']).name if results else None
    ok=name=='student-agent-kb' and tools==['agent_kb_search'] and top==a.expect
    print(json.dumps({'ok':ok,'server':name,'tools':tools,'top':top,'results':len(results),'error':answer.get('error'),
                      'viaLauncher':a.via_launcher},ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__=='__main__':main()
