"""Isolated toolkit checks. --postgres uses a private temporary Postgres cluster."""
import asyncio
import getpass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
ADDON=Path(os.environ.get('AGENTS_TOOLING_SOURCE',str(ROOT/'installer/tooling-addon'))).resolve()
spec=importlib.util.spec_from_file_location('student_tools',ADDON/'student_tools.py')
tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
POSTGRES='--postgres' in sys.argv
if POSTGRES:sys.argv.remove('--postgres')


class PureHelpers(unittest.TestCase):
    def test_bootstrap_preserves_profiles_and_registers_all_received_materials(self):
        spec=importlib.util.spec_from_file_location('tooling_bootstrap',ADDON/'bootstrap.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home=root/'hermes';folder=home/'profiles/tech';folder.mkdir(parents=True)
            (folder/'skills/example/references').mkdir(parents=True)
            (folder/'skills/example/SKILL.md').write_text('# Example')
            (folder/'skills/example/references/helper.py').write_text('print("fixture")')
            (folder/'knowledge').mkdir();(folder/'knowledge/note.md').write_text('fixture note')
            (folder/'AGENTS.md').write_text('# My Tech\nKeep this personal instruction.\n')
            (folder/'SOUL.md').write_text('# My Soul\nKeep this too.\n')
            original={'model':{'provider':'fixture'},'memory':{'provider':'existing'},'mcp_servers':{'telegram-reader':{'command':'do-not-change'}}}
            (folder/'config.yaml').write_text(yaml.safe_dump(original))
            (folder/'auth.json').write_text('DO_NOT_READ_OR_COPY')
            settings=root/'tooling/settings.json';settings.parent.mkdir()
            settings.write_text((ADDON/'settings.example.json').read_text())
            result=module.prepare(home,settings)
            cfg=yaml.safe_load((folder/'config.yaml').read_text())
            self.assertEqual(cfg['mcp_servers']['telegram-reader'],original['mcp_servers']['telegram-reader'])
            self.assertEqual(cfg['model'],original['model']);self.assertEqual(cfg['memory'],original['memory'])
            self.assertIn('student-agent-kb',cfg['mcp_servers'])
            self.assertEqual(result['profiles'],['tech']);self.assertFalse(result['telegramReaderChanged'])
            registered=json.loads(settings.read_text())['roots']
            self.assertTrue(any(x['path']==str(folder/'skills') for x in registered))
            self.assertTrue(any(x['path']==str(folder/'knowledge') for x in registered))
            snapshot=(settings.parent/'index-context/tech/profile-context.md').read_text()
            self.assertIn('Keep this personal instruction.',snapshot);self.assertNotIn('DO_NOT_READ_OR_COPY',snapshot)
            self.assertEqual(module.prepare(home,settings)['changedFiles'],0)
            self.assertEqual((folder/'auth.json').read_text(),'DO_NOT_READ_OR_COPY')

    def test_browser_profile_key_follows_hermes_version(self):
        spec=importlib.util.spec_from_file_location('tooling_bootstrap_browser',ADDON/'bootstrap.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import yaml
        cases={'0.23.0':('stamp','hermes-chromium'),'0.21.5':('pyproject','agent-browser'),None:(None,'unknown')}
        for version,(source,mode) in cases.items():
            with self.subTest(version=version),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();home=root/'hermes';folder=home/'profiles/tech';folder.mkdir(parents=True)
                agent=home/'hermes-agent';agent.mkdir()
                if source=='stamp':
                    (agent/'install-stamp.json').write_text(json.dumps({'baseVersion':version}))
                    (agent/'pyproject.toml').write_text('[project]\nname = "hermes-agent"\nversion = "0.0.0"\n')
                elif source=='pyproject':
                    (agent/'pyproject.toml').write_text('[project]\nname = "hermes-agent"\nversion = "%s"\n'%version)
                else:(agent/'pyproject.toml').write_text('[project]\nname = "hermes-agent"\nversion = "0.0.0"\n')
                stale=str(folder/'browser-profile-tooling')
                (folder/'config.yaml').write_text(yaml.safe_dump({'browser':{'headed':True,'persistent_profile_path':stale,'engine':'auto'}}))
                settings=root/'tooling/settings.json';settings.parent.mkdir()
                settings.write_text((ADDON/'settings.example.json').read_text())
                result=module.prepare(home,settings,enable_browser=True)
                self.assertEqual(result['browserMode'],mode);self.assertEqual(result['hermesVersion'],version)
                browser=yaml.safe_load((folder/'config.yaml').read_text())['browser']
                self.assertIs(browser['headed'],False);self.assertEqual(browser['engine'],'auto')
                self.assertNotIn('persistent_profile_path',browser)
                if mode=='hermes-chromium':self.assertEqual(browser['user_data_dir'],stale)
                else:self.assertNotIn('user_data_dir',browser)
                self.assertEqual(module.prepare(home,settings,enable_browser=True)['changedFiles'],0)

    @unittest.skipIf(os.name=='nt','POSIX launcher fixture')
    def test_mcp_client_is_asked_from_the_hermes_interpreter(self):
        spec=importlib.util.spec_from_file_location('tooling_inventory',ADDON/'inventory.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve()/'hermes';launcher=home/'hermes-agent/venv/bin/python';launcher.parent.mkdir(parents=True)
            self.assertIsNone(module.hermes_mcp_client(home))
            # 0.23: no venv/lib at all, packages live in the PM store behind this launcher.
            for code,expected in ((0,True),(3,False),(1,None)):
                launcher.write_text('#!/bin/sh\nexit %d\n'%code);launcher.chmod(0o755)
                self.assertIs(module.hermes_mcp_client(home),expected)

    def test_unreadable_instruction_file_is_skipped_not_fatal(self):
        spec=importlib.util.spec_from_file_location('tooling_bootstrap_bytes',ADDON/'bootstrap.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home=root/'hermes'
            for name in ('tech','assistant'):
                folder=home/'profiles'/name;folder.mkdir(parents=True)
                (folder/'AGENTS.md').write_text('# '+name+'\nCourse text.\n');(folder/'config.yaml').write_text(yaml.safe_dump({}))
            broken=home/'profiles/assistant/SOUL.md';raw=b'\xff\xfe\x00broken'+bytes(range(128,160));broken.write_bytes(raw)
            settings=root/'tooling/settings.json';settings.parent.mkdir()
            settings.write_text((ADDON/'settings.example.json').read_text())
            result=module.prepare(home,settings)
            self.assertEqual(result['unreadableInstructions'],['assistant/SOUL.md'])
            self.assertEqual(broken.read_bytes(),raw)
            for name in ('tech','assistant'):
                self.assertEqual((home/'profiles'/name/'AGENTS.md').read_text().count(module.BEGIN),1)
                self.assertIn('student-agent-kb',yaml.safe_load((home/'profiles'/name/'config.yaml').read_text())['mcp_servers'])

    def test_browser_user_choices_are_preserved(self):
        spec=importlib.util.spec_from_file_location('tooling_bootstrap_keep',ADDON/'bootstrap.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home=root/'hermes';folder=home/'profiles/tech';folder.mkdir(parents=True)
            (home/'hermes-agent').mkdir();(home/'hermes-agent/install-stamp.json').write_text('{"baseVersion":"0.23.0"}')
            mine={'user_data_dir':str(root/'my-profile'),'persistent_profile_path':'/elsewhere'}
            (folder/'config.yaml').write_text(yaml.safe_dump({'browser':dict(mine)}))
            settings=root/'tooling/settings.json';settings.parent.mkdir()
            settings.write_text((ADDON/'settings.example.json').read_text())
            module.prepare(home,settings,enable_browser=True)
            browser=yaml.safe_load((folder/'config.yaml').read_text())['browser']
            self.assertEqual(browser['user_data_dir'],mine['user_data_dir'])
            self.assertEqual(browser['persistent_profile_path'],'/elsewhere')
            # Without --enable-headless-config the browser section is untouched.
            (folder/'config.yaml').write_text(yaml.safe_dump({'browser':{'headed':True}}))
            module.prepare(home,settings)
            self.assertEqual(yaml.safe_load((folder/'config.yaml').read_text())['browser'],{'headed':True})

    def test_hermes_browser_check_reads_the_page_back_over_devtools(self):
        import http.server,threading
        spec=importlib.util.spec_from_file_location('browser_smoke',ADDON/'browser-smoke.py')
        smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
        opened=[]
        class DevTools(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def reply(self,body):
                data=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_PUT(self):
                opened.append(self.path);self.reply({'id':'1','type':'page','title':''})
            def do_GET(self):
                self.reply([{'type':'page','title':'Agents tooling smoke' if opened else 'about:blank'}])
        server=http.server.HTTPServer(('127.0.0.1',0),DevTools);threading.Thread(target=server.serve_forever,daemon=True).start()
        class Alive:
            def poll(self):return None
        class Gone:
            def poll(self):return 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                port=Path(tmp)/'DevToolsActivePort';port.write_text(str(server.server_port)+'\n/devtools/browser/x\n')
                self.assertTrue(smoke.cdp_page_title_ok(port,Alive(),deadline=10))
                self.assertTrue(opened[0].startswith('/json/new?data:text/html,'))
                # A browser that exits before it opens its DevTools port fails the check.
                self.assertFalse(smoke.cdp_page_title_ok(Path(tmp)/'missing',Gone(),deadline=5))
        finally:server.shutdown()

    def test_chunks_no_loss_and_bounded(self):
        text='Привет, мир! '*1000
        parts=tools.chunks(text)
        self.assertTrue(all(0<len(x)<=1800 for x in parts))
        self.assertEqual(parts[0]+''.join(x[220:] for x in parts[1:]),text)
        with self.assertRaises(ValueError):tools.chunks(text,100,100)

    def test_index_exclusions_and_secret_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'safe.md').write_text('Public fixture')
            (root/'auth.json').write_text('PRIVATE_SENTINEL')
            (root/'.env').write_text('PRIVATE_SENTINEL')
            (root/'logs').mkdir();(root/'logs/trace.txt').write_text('PRIVATE_SENTINEL')
            (root/'linked.md').symlink_to(root/'safe.md')
            self.assertEqual([p.name for p in tools.candidates(root)],['safe.md'])
            (root/'key.md').write_text('password = '+('A'*30))
            with self.assertRaises(ValueError):tools.text_of(root/'key.md')

    def test_rrf_keeps_lexical_and_limits_duplicates(self):
        def row(i,src,text):return {'id':i,'source':src,'contentHash':text}
        a,b,c,d=row(1,'a','a'),row(2,'a','b'),row(3,'a','c'),row(4,'b','d')
        result=tools.rrf([[a,b,c],[d,a]],6)
        self.assertIn(4,[r['id'] for r in result]);self.assertEqual(sum(x['source']=='a' for x in result),2)

    def test_no_cloud_embedding_or_unregistered_model(self):
        with self.assertRaises(ValueError):tools.embed({'ollamaUrl':'https://example.invalid'},['a'])
        with self.assertRaises(ValueError):tools.embed({'ollamaUrl':'http://127.0.0.1:11434'},['a'])

    def test_vault_child_gets_only_selected_secret_and_output_is_scrubbed(self):
        import types
        calls=[]
        def fake(args,**kw):
            calls.append((args,kw))
            if args[0]=='bws':return types.SimpleNamespace(returncode=0,stdout=b'{"value":"fixture-secret-value"}')
            self.assertNotIn('BWS_ACCESS_TOKEN',kw['env'])
            self.assertNotIn('UNRELATED_API_KEY',kw['env'])
            self.assertEqual(kw['env']['AGENTS_KB_PASSWORD'],'fixture-secret-value')
            return types.SimpleNamespace(returncode=0,stdout=b'fixture-secret-value',stderr=b'')
        import io
        output=io.StringIO()
        with patch.dict(os.environ,{'BWS_ACCESS_TOKEN':'fixture-bootstrap','UNRELATED_API_KEY':'must-not-propagate'}),patch.object(tools.subprocess,'run',side_effect=fake),patch('sys.stdout',output):
            code=tools.vault_run({'secretMap':{'AGENTS_KB_PASSWORD':'11111111-1111-4111-8111-111111111111'}},['fixture-consumer'])
        self.assertEqual(code,0);self.assertEqual(output.getvalue(),'[REDACTED]')
        self.assertNotIn('fixture-secret-value',json.dumps(calls[0][0]))

    def test_mcp_stdio_tool_registration(self):
        async def check():
            from mcp import ClientSession,StdioServerParameters
            from mcp.client.stdio import stdio_client
            with tempfile.TemporaryDirectory() as tmp:
                cfg=Path(tmp).resolve()/'settings.json';cfg.write_text((ADDON/'settings.example.json').read_text())
                params=StdioServerParameters(command=sys.executable,args=['-B',str(ADDON/'student_tools.py'),'--config',str(cfg),'mcp'],env={'PATH':os.environ['PATH'],'HOME':tmp,'USERPROFILE':tmp})
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        result=await session.list_tools()
                        self.assertEqual([x.name for x in result.tools],['agent_kb_search'])
                        response=await session.call_tool('agent_kb_search',{'query':''})
                        self.assertIn('ValueError',str(response))
        asyncio.run(check())


@unittest.skipUnless(POSTGRES,'Use --postgres for the isolated SQL test')
class LocalPostgres(unittest.TestCase):
    def test_real_schema_index_search_update_revert_and_project_filter(self):
        import pg8000.native
        bindir=Path(subprocess.check_output(['pg_config','--bindir'],text=True).strip())
        with tempfile.TemporaryDirectory(prefix='student-kb-sql-') as tmp:
            root=Path(tmp).resolve();data=root/'db';sock=root/'socket';sock.mkdir()
            subprocess.run([str(bindir/'initdb'),'-D',str(data),'-A','trust','--encoding=UTF8','--no-instructions'],check=True,capture_output=True)
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            import shlex
            options=f'-k {shlex.quote(str(sock))} -h \'\' -p {port}'
            subprocess.run([str(bindir/'pg_ctl'),'-D',str(data),'-l',str(root/'server.log'),'-o',options,'-w','start'],check=True,capture_output=True)
            def connect(cfg):return pg8000.native.Connection(user=cfg.get('database',{}).get('user',getpass.getuser()),database='postgres',unix_sock=str(sock/f'.s.PGSQL.{port}'))
            try:
                db=connect({})
                try:
                    # Known static schema only; no production database is contacted.
                    for statement in (ADDON/'schema.sql').read_text().split(';'):
                        if statement.strip():db.run(statement)
                    db.run('CREATE ROLE agents_fixture LOGIN')
                    db.run('GRANT USAGE ON SCHEMA public TO agents_fixture')
                    db.run('GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA public TO agents_fixture')
                    db.run('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO agents_fixture')
                finally:db.close()
                a=root/'project-a';b=root/'project-b';a.mkdir();b.mkdir()
                (a/'note.md').write_text('alpha lighthouse original')
                (b/'note.md').write_text('alpha separate customer')
                cfg={'database':{'name':'postgres','user':'agents_fixture'},'roots':[{'path':str(a),'project':'a','profile':'tech'},{'path':str(b),'project':'b','profile':'tech'}]}
                with patch.object(tools,'connection',side_effect=connect),patch.object(tools,'embed',side_effect=lambda cfg,items:[[1.0]+[0.0]*1023 for _ in items]):
                    self.assertTrue(tools.doctor(cfg)['ready'])
                    self.assertEqual(tools.index(cfg)['documentsWritten'],2)
                    self.assertEqual(tools.index(cfg)['unchanged'],2)
                    result=tools.search(cfg,'alpha','tech','a')['results']
                    self.assertTrue(result);self.assertTrue(all(x['metadata']['project']=='a' for x in result))
                    (a/'note.md').write_text('alpha lighthouse amended')
                    self.assertEqual(tools.index(cfg)['documentsWritten'],1)
                    self.assertIn('amended',tools.search(cfg,'alpha','tech','a')['results'][0]['content'])
                    (a/'note.md').write_text('alpha lighthouse original')
                    self.assertEqual(tools.index(cfg)['documentsWritten'],1)
                    (a/'note.md').unlink()
                    tools.index(cfg)
                    self.assertEqual(tools.search(cfg,'alpha','tech','a')['results'],[])
                    self.assertTrue((b/'note.md').exists())
                    self.assertTrue(tools.search(cfg,'alpha','tech','b')['results'])
            finally:
                subprocess.run([str(bindir/'pg_ctl'),'-D',str(data),'-m','fast','-w','stop'],check=True,capture_output=True)


if __name__=='__main__':unittest.main()
