#!/usr/bin/env python3
"""CI only: a real stdio MCP session with student-agent-kb (initialize, tools/list, tools/call).

--via-launcher starts the server through student_tools.launch_mcp, the path vault-mcp takes:
on Windows a child process on the inherited stdio handles, on POSIX exec.
--again N calls the tool a second time after N seconds: with AGENTS_TOOLING_QA_KEEPALIVE_SECONDS
shorter than N the server has freed the model and loads it anew while its stdin read is pending.
A session longer than --timeout seconds is a failure (exit 3), never a hung job.
Prints one ASCII JSON line; exit code 0 only when the expected file is the top result each time.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
LAUNCHER='''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location("student_tools",sys.argv[1])
tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
sys.exit(tools.launch_mcp([sys.executable,"-B",sys.argv[1],"--config",sys.argv[2],"mcp"],dict(os.environ)))
'''
PASS={'PATH','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','LOCALAPPDATA','APPDATA','USERPROFILE','HOME','USERNAME',
      'AGENTS_TOOLING_QA_MAX_FILES','AGENTS_TOOLING_QA_KEEPALIVE_SECONDS','AGENTS_TOOLING_QA_STACKS'}


def report(body,code):
    print(json.dumps(body,ensure_ascii=True),flush=True)
    os._exit(code)  # no interpreter shutdown that could wait on a stuck child or thread


async def session(a):
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    script=str(a.tools)
    args=['-B','-c',LAUNCHER,script,str(a.settings)] if a.via_launcher else ['-B',script,'--config',str(a.settings),'mcp']
    params=StdioServerParameters(command=sys.executable,args=args,env={k:v for k,v in os.environ.items() if k in PASS})
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as s:
            init=await s.initialize()
            tools=[t.name for t in (await s.list_tools()).tools]
            answers=[]
            for attempt in range(2 if a.again else 1):
                if attempt:await asyncio.sleep(a.again)
                response=await s.call_tool('agent_kb_search',{'query':a.query,'profile':a.profile,'project':a.project})
                answers.append(json.loads(response.content[0].text))
            return init.serverInfo.name,tools,answers


def main():
    p=argparse.ArgumentParser();p.add_argument('--settings',type=Path,required=True);p.add_argument('--query',required=True)
    p.add_argument('--expect',required=True,help='file name expected as the top result')
    p.add_argument('--tools',type=Path,default=ROOT/'student_tools.py',help='student_tools.py of the unpacked package')
    p.add_argument('--profile',default='tech');p.add_argument('--project',default='__skills__')
    p.add_argument('--via-launcher',action='store_true');p.add_argument('--again',type=float,default=0)
    p.add_argument('--timeout',type=float,default=300);a=p.parse_args()
    a.settings=a.settings.resolve();a.tools=a.tools.resolve()
    # A second line of defence: asyncio cannot cancel a thread stuck in a pipe read.
    watchdog=threading.Timer(a.timeout+30,report,args=({'ok':False,'error':'timeout','viaLauncher':a.via_launcher},3))
    watchdog.daemon=True;watchdog.start()
    started=time.monotonic()
    try:name,tools,answers=asyncio.run(asyncio.wait_for(session(a),a.timeout))
    except BaseException as error:
        # A cancelled MCP session surfaces as an ExceptionGroup, not only as TimeoutError.
        if time.monotonic()-started>=a.timeout-0.5:report({'ok':False,'error':'timeout','viaLauncher':a.via_launcher},3)
        report({'ok':False,'error':type(error).__name__,'viaLauncher':a.via_launcher},1)
    tops=[];errors=[]
    for answer in answers:
        results=answer.get('results') or []
        tops.append(Path(results[0]['source']).name if results else None);errors.append(answer.get('error'))
    ok=name=='student-agent-kb' and tools==['agent_kb_search'] and all(t==a.expect for t in tops)
    report({'ok':ok,'server':name,'tools':tools,'top':tops,'calls':len(answers),'error':errors,'viaLauncher':a.via_launcher},0 if ok else 1)


if __name__=='__main__':main()
