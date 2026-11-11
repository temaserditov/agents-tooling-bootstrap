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
            def connect(cfg):
                d=cfg.get('database',{})
                return pg8000.native.Connection(user=d.get('user',getpass.getuser()),database=d.get('name','postgres'),unix_sock=str(sock/f'.s.PGSQL.{port}'))
            psql=lambda db,sql:subprocess.run([str(bindir/'psql'),'-X','-q','-h',str(sock),'-p',str(port),'-d',db,'-v','ON_ERROR_STOP=1','-f',str(ADDON/sql)],capture_output=True,text=True)
            try:
                db=connect({})
                try:
                    db.run('CREATE ROLE agents_student LOGIN');db.run('CREATE DATABASE agents_student_kb')
                finally:db.close()
                # The schema refuses any other database; the exact grants need the dedicated one.
                self.assertNotEqual(psql('postgres','schema.sql').returncode,0)
                for sql in ('schema.sql','grants.sql'):
                    r=psql('agents_student_kb',sql);self.assertEqual(r.returncode,0,r.stderr)
                a=root/'project-a';b=root/'project-b';a.mkdir();b.mkdir()
                (a/'note.md').write_text('alpha lighthouse original')
                (b/'note.md').write_text('alpha separate customer')
                cfg={'database':{'name':'agents_student_kb','user':'agents_student'},'roots':[{'path':str(a),'project':'a','profile':'tech'},{'path':str(b),'project':'b','profile':'tech'}]}
                with patch.object(tools,'connection',side_effect=connect),patch.object(tools,'embed',side_effect=lambda cfg,items:[[1.0]+[0.0]*1023 for _ in items]):
                    health=tools.doctor(cfg)
                    self.assertTrue(health['ready']);self.assertFalse(health['runtimeOwnsDatabase']);self.assertTrue(health['socket'])
                    probe=connect(cfg)
                    try:self.assertEqual(probe.run("SELECT has_table_privilege('team_kb_chunks','DELETE')")[0][0],False)
                    finally:probe.close()
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



def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ADDON/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class FakeHTTPError(Exception):
    def __init__(self,code):self.code=code


class PinsAndRetries(unittest.TestCase):
    """Pinned models are a hard stop; network trouble is retried a bounded number of times."""
    def fixture_pins(self,tmp):
        files={'config.json':b'{"fixture":1}','model.bin':b'weights','tokenizer.json':b'{}','vocabulary.txt':b'a\nb'}
        pins={n:__import__('hashlib').sha256(d).hexdigest() for n,d in files.items()}
        def download(target,data=files):
            Path(target).mkdir(parents=True,exist_ok=True)
            for n,d in data.items():(Path(target)/n).write_bytes(d)
            (Path(target)/'.cache').mkdir(exist_ok=True)
        return files,pins,download

    def test_whisper_model_verified_published_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            files,pins,download=self.fixture_pins(tmp)
            target=Path(tmp).resolve()/'models/small'
            with patch.dict(tools.WHISPER_FILES,pins,clear=True):
                first=tools.fetch_whisper_model(target,download=download,sleep=lambda s:None)
                self.assertTrue(first['changed']);self.assertEqual(sorted(p.name for p in target.iterdir()),sorted(files))
                self.assertEqual(tools.fetch_whisper_model(target,download=lambda t:self.fail('no second download'))['changed'],False)
                self.assertEqual(list(target.parent.glob('.small.download-*')),[])

    def test_whisper_model_hash_mismatch_is_a_hard_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            files,pins,download=self.fixture_pins(tmp)
            bad={**files,'model.bin':b'tampered'}
            target=Path(tmp).resolve()/'models/small'
            with patch.dict(tools.WHISPER_FILES,pins,clear=True):
                with self.assertRaises(tools.StudentFacingError) as caught:
                    tools.fetch_whisper_model(target,download=lambda t:download(t,bad),sleep=lambda s:None)
                self.assertIn('model.bin',str(caught.exception));self.assertIn('не установлена',str(caught.exception))
                self.assertFalse(target.exists());self.assertEqual(list(target.parent.iterdir()),[])
                # An unknown non-empty folder is the student's: never overwritten.
                target.mkdir();(target/'model.bin').write_bytes(b'other')
                with self.assertRaises(tools.StudentFacingError):tools.fetch_whisper_model(target,download=download)
                self.assertEqual((target/'model.bin').read_bytes(),b'other')

    def test_transcribe_refuses_an_unpinned_model_before_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=Path(tmp).resolve()/'m';model.mkdir();(model/'model.bin').write_bytes(b'x')
            audio=Path(tmp).resolve()/'a.wav';audio.write_bytes(b'RIFF')
            with self.assertRaises(tools.StudentFacingError):tools.transcribe(model,audio,Path(tmp).resolve()/'out.json')
            self.assertFalse((Path(tmp).resolve()/'out.json').exists())

    def test_transient_failures_are_retried_then_reported_honestly(self):
        calls=[];pauses=[]
        def flaky():
            calls.append(1)
            if len(calls)<3:raise FakeHTTPError(503)
            return 'ok'
        self.assertEqual(tools.with_retries(flaky,'fixture',sleep=pauses.append),'ok')
        self.assertEqual(len(calls),3);self.assertEqual(len(pauses),2)
        def down():raise TimeoutError()
        with self.assertRaises(tools.StudentFacingError) as caught:tools.with_retries(down,'Hugging Face',attempts=3,sleep=pauses.append)
        self.assertIn('3 попыток',str(caught.exception));self.assertIn('ничего не пропускай',str(caught.exception))
        missing=[]
        def gone():missing.append(1);raise FakeHTTPError(404)
        with self.assertRaises(FakeHTTPError):tools.with_retries(gone,'fixture',sleep=pauses.append)
        self.assertEqual(len(missing),1)

    def test_bge_m3_digest_is_pinned(self):
        class Opener:
            def __init__(self,answers):self.answers=list(answers)
            def open(self,url,timeout=None):
                answer=self.answers.pop(0)
                if isinstance(answer,Exception):raise answer
                import io as _io
                class R(_io.BytesIO):
                    def __enter__(self):return self
                    def __exit__(self,*a):return False
                return R(json.dumps(answer).encode())
        good={'models':[{'name':'bge-m3:latest','digest':tools.BGE_M3_DIGEST}]}
        other={'models':[{'name':'bge-m3:latest','digest':'0'*64}]}
        self.assertTrue(tools.model_check({},Opener([FakeHTTPError(503),good]),sleep=lambda s:None)['verified'])
        with self.assertRaises(tools.StudentFacingError) as caught:tools.model_check({},Opener([other]))
        self.assertIn('Индекс не создаётся',str(caught.exception))
        with self.assertRaises(tools.StudentFacingError):tools.model_check({},Opener([{'models':[]}]))
        with tempfile.TemporaryDirectory() as tmp:
            settings=Path(tmp).resolve()/'settings.json'
            settings.write_text(json.dumps({'schemaVersion':1,'embeddingModel':'bge-m3','embeddingDimensions':1024,'embeddingDigest':'1'*64,'roots':[]}))
            with self.assertRaises(tools.StudentFacingError):tools.config(settings)
            settings.write_text(json.dumps({'schemaVersion':1,'embeddingModel':'bge-m3','embeddingDimensions':1024,'embeddingDigest':tools.BGE_M3_DIGEST,'roots':[]}))
            self.assertEqual(tools.config(settings)['embeddingDigest'],tools.BGE_M3_DIGEST)

    def test_student_facing_errors_are_printed_as_is_and_others_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run=lambda *a:subprocess.run([sys.executable,'-B',str(ADDON/'student_tools.py'),*a],capture_output=True,text=True)
            model=Path(tmp).resolve()/'m';model.mkdir();(model/'model.bin').write_bytes(b'x');audio=Path(tmp).resolve()/'a.wav';audio.write_bytes(b'R')
            r=run('transcribe','--model-dir',str(model),'--audio',str(audio),'--output',str(Path(tmp).resolve()/'o.json'))
            self.assertEqual(r.returncode,2);self.assertIn('не совпадает с закреплённой',r.stderr)
            r=run('--config',str(Path(tmp).resolve()/'missing.json'),'doctor')
            self.assertEqual(r.returncode,1);self.assertIn('raw error suppressed',r.stderr);self.assertNotIn(tmp,r.stderr)


class Idempotency(unittest.TestCase):
    """Repeated bootstrap writes nothing; secrets and sessions are never opened or copied."""
    CANARY='CANARY-7f3c9e1a-SECRET'

    def install(self,root):
        import yaml
        home=root/'hermes'
        for name in ['tech','copywriter']:
            folder=home/'profiles'/name;(folder/'skills/s').mkdir(parents=True)
            (folder/'skills/s/SKILL.md').write_text('# Skill '+name)
            (folder/'AGENTS.md').write_text('# '+name+'\nMy own rule.\n')
            (folder/'SOUL.md').write_text('# Soul '+name+'\n')
            (folder/'config.yaml').write_text(yaml.safe_dump({'model':{'provider':'x'}}))
            (folder/'auth.json').write_text(self.CANARY);(folder/'.env').write_text('KEY='+self.CANARY)
            (folder/'sessions').mkdir();(folder/'sessions/chat.json').write_text(self.CANARY)
        (home/'.env').write_text('TELEGRAM_BOT_TOKEN='+self.CANARY);(home/'auth.json').write_text(self.CANARY)
        (home/'telegram.session').write_text(self.CANARY)
        bw=root/'bitwarden';bw.mkdir();(bw/'data.json').write_text(self.CANARY)
        settings=root/'tooling/settings.json';settings.parent.mkdir()
        settings.write_text((ADDON/'settings.example.json').read_text())
        return home,settings

    def test_second_bootstrap_changes_nothing(self):
        bootstrap=load('tooling_bootstrap_idem','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            home,settings=self.install(Path(tmp).resolve())
            first=bootstrap.prepare(home,settings)
            self.assertTrue(first['changed']);self.assertGreater(first['changedFiles'],0)
            second=bootstrap.prepare(home,settings)
            self.assertIs(second['changed'],False);self.assertEqual(second['changedFiles'],0);self.assertIsNone(second['backup'])

    def test_secrets_and_sessions_are_never_opened_or_copied(self):
        bootstrap=load('tooling_bootstrap_secret','bootstrap.py');inventory=load('tooling_inventory_secret','inventory.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home,settings=self.install(root)
            protected={str(p) for p in root.rglob('*') if p.is_file() and self.CANARY in p.read_text()}
            opened=[]
            def hook(event,args):
                if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
                    path=os.fsdecode(args[0]);path=str(Path(path).resolve()) if os.path.isabs(path) else path
                    if path in protected:opened.append(path)
            sys.addaudithook(hook)
            with patch.dict(os.environ,{'BWS_ACCESS_TOKEN':self.CANARY,'BW_SESSION':self.CANARY}):
                out=json.dumps(inventory.scan(home=root,hermes_home=home))+json.dumps(bootstrap.prepare(home,settings))
            hook_active=list(opened);opened.clear()
            self.assertEqual(hook_active,[],'protected files were opened')
            self.assertNotIn(self.CANARY,out)
            for path in (root/'tooling').rglob('*'):
                if path.is_file():self.assertNotIn(self.CANARY,path.read_text(errors='replace'),str(path))
            for path in home.rglob('*'):
                if path.is_file() and str(path) not in protected:self.assertNotIn(self.CANARY,path.read_text(errors='replace'),str(path))

    def test_tooling_block_survives_repeated_runs_and_an_update_rewrite(self):
        bootstrap=load('tooling_bootstrap_block','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            home,settings=self.install(Path(tmp).resolve())
            bootstrap.prepare(home,settings);bootstrap.prepare(home,settings)
            agents=home/'profiles/tech/AGENTS.md'
            text=agents.read_text()
            self.assertEqual(text.count(bootstrap.BEGIN),1);self.assertEqual(text.count(bootstrap.END),1);self.assertIn('My own rule.',text)
            # A tariff update rewrites the file around the block (FIX_0495 merge keeps it).
            agents.write_text(text.replace('My own rule.','My own rule.\nNew course rule.'))
            third=bootstrap.prepare(home,settings)
            text=agents.read_text()
            self.assertEqual(text.count(bootstrap.BEGIN),1);self.assertIn('New course rule.',text);self.assertIn('My own rule.',text)
            self.assertIs(bootstrap.prepare(home,settings)['changed'],False)
            self.assertTrue(third['changed'])  # only the index snapshot of the instructions moved


class ReviewFixes(unittest.TestCase):
    """Upgrade from an older release, damaged sources, an empty model folder."""
    def profile(self,root,entry=None):
        import yaml
        home=root/'hermes';folder=home/'profiles/tech';(folder/'skills/s').mkdir(parents=True)
        (folder/'skills/s/SKILL.md').write_text('# s');(folder/'AGENTS.md').write_text('# t\n');(folder/'SOUL.md').write_text('# soul\n')
        cfg={'model':{'provider':'x'},'mcp_servers':{}}
        if entry is not None:cfg['mcp_servers']['student-agent-kb']=entry
        (folder/'config.yaml').write_text(yaml.safe_dump(cfg))
        settings=root/'new-tooling/settings.json';settings.parent.mkdir()
        settings.write_text((ADDON/'settings.example.json').read_text())
        return home,folder,settings

    def test_upgrade_from_an_older_release_or_another_folder_replaces_our_entry(self):
        import yaml
        bootstrap=load('tooling_bootstrap_upgrade','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old={'command':str(root/'old-tooling/venv/bin/python'),'args':['-B',str(root/'old-tooling/student_tools.py'),'--config',str(root/'old-tooling/settings.json'),'vault-mcp']}
            home,folder,settings=self.profile(root,old)
            first=bootstrap.prepare(home,settings)
            entry=yaml.safe_load((folder/'config.yaml').read_text())['mcp_servers']['student-agent-kb']
            self.assertEqual(entry['args'][1],str(ADDON/'student_tools.py'));self.assertEqual(entry['args'][4],'mcp')
            self.assertTrue(first['changed']);self.assertTrue(Path(first['backup']).is_dir())
            self.assertIs(bootstrap.prepare(home,settings)['changed'],False)

    def test_our_entry_keeps_the_students_own_keys_and_disabled_state(self):
        import yaml
        bootstrap=load('tooling_bootstrap_keys','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            old={'command':'/old/venv/bin/python','args':['-B','/old/student_tools.py','--config','/old/settings.json','mcp'],
                 'enabled':False,'timeout':120,'env':{'LANG':'C.UTF-8'}}
            home,folder,settings=self.profile(root,old)
            bootstrap.prepare(home,settings)
            entry=yaml.safe_load((folder/'config.yaml').read_text())['mcp_servers']['student-agent-kb']
            self.assertIs(entry['enabled'],False);self.assertEqual(entry['timeout'],120);self.assertEqual(entry['env'],{'LANG':'C.UTF-8'})
            self.assertEqual(entry['args'][1],str(ADDON/'student_tools.py'));self.assertEqual(entry['command'],sys.executable)
            self.assertIs(bootstrap.prepare(home,settings)['changed'],False)

    def test_database_settings_are_socket_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings=Path(tmp).resolve()/'settings.json'
            base={'schemaVersion':1,'embeddingModel':'bge-m3','embeddingDimensions':1024,'roots':[]}
            settings.write_text(json.dumps({**base,'database':{'host':'127.0.0.1','name':'agents_student_kb','user':'agents_student'}}))
            with self.assertRaises(tools.StudentFacingError):tools.config(settings)
            with self.assertRaises(tools.StudentFacingError):tools.socket_path({'port':5432})
            self.assertEqual(tools.socket_path({'socket':'/var/run/postgresql','port':5432}),'/var/run/postgresql/.s.PGSQL.5432')

    def test_watch_names_a_damaged_file_once_until_it_changes(self):
        import io,contextlib
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp).resolve()/'notes';src.mkdir();bad=src/'broken.pdf';bad.write_bytes(b'%PDF broken')
            class FakeDB:
                def run(self,sql,**kw):return [[True]] if 'pg_try_advisory_lock' in sql else []
                def close(self):pass
            cfg={'roots':[{'path':str(src),'project':'p','profile':'tech'}]}
            tools.REPORTED_SKIPS.clear()
            def run_once():
                err=io.StringIO()
                with patch.object(tools,'connection',return_value=FakeDB()),contextlib.redirect_stderr(err):tools.index(cfg)
                return err.getvalue().count('source-skipped')
            self.assertEqual(run_once(),1);self.assertEqual(run_once(),0)
            bad.write_bytes(b'%PDF broken again, longer');os.utime(bad,None)
            self.assertEqual(run_once(),1)

    def test_a_foreign_student_agent_kb_is_refused_with_a_clear_message(self):
        bootstrap=load('tooling_bootstrap_foreign','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home,folder,settings=self.profile(root,{'command':'node','args':['my-server.js']})
            before=(folder/'config.yaml').read_text()
            with self.assertRaises(bootstrap.BootstrapError) as caught:bootstrap.prepare(home,settings)
            self.assertIn('не из этого пакета',str(caught.exception));self.assertEqual((folder/'config.yaml').read_text(),before)
            r=subprocess.run([sys.executable,'-B',str(ADDON/'bootstrap.py'),'--hermes-home',str(home),'--settings',str(settings)],capture_output=True,text=True)
            self.assertEqual(r.returncode,1);self.assertIn('не из этого пакета',r.stdout)

    def test_damaged_sources_are_skipped_and_office_lock_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();src=root/'notes';src.mkdir()
            (src/'good.md').write_text('alpha lighthouse')
            (src/'broken.pdf').write_bytes(b'%PDF-1.4 this is not a pdf')
            (src/'broken.docx').write_bytes(b'PK\x03\x04 not really a zip')
            (src/'~$draft.docx').write_bytes(b'owner lock')
            self.assertNotIn('~$draft.docx',[p.name for p in tools.candidates(src)])
            class FakeDB:
                def run(self,sql,**kw):
                    if 'pg_try_advisory_lock' in sql:return [[True]]
                    if 'RETURNING id' in sql:return [[1]]
                    return []
                def close(self):pass
            cfg={'roots':[{'path':str(src),'project':'p','profile':'tech'}]}
            with patch.object(tools,'connection',return_value=FakeDB()),patch.object(tools,'embed',side_effect=lambda c,items:[[0.0]*1024 for _ in items]):
                result=tools.index(cfg)
            self.assertEqual(result['documentsWritten'],1);self.assertEqual(result['skipped'],2)
            self.assertEqual(sorted(Path(x['file']).name for x in result['skippedFiles']),['broken.docx','broken.pdf'])
            self.assertNotIn('not a pdf',json.dumps(result))

    def test_an_empty_model_folder_made_in_advance_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            files={'model.bin':b'w'};pins={'model.bin':__import__('hashlib').sha256(b'w').hexdigest()}
            target=Path(tmp).resolve()/'model';target.mkdir()
            def download(t):Path(t).mkdir(parents=True,exist_ok=True);(Path(t)/'model.bin').write_bytes(b'w')
            with patch.dict(tools.WHISPER_FILES,pins,clear=True):
                self.assertTrue(tools.fetch_whisper_model(target,download=download,sleep=lambda s:None)['changed'])

    def test_browser_smoke_has_no_python_310_only_arguments(self):
        text=(ADDON/'browser-smoke.py').read_text()
        self.assertNotIn('ignore_cleanup_errors=True)',text);self.assertIn("sys.version_info>=(3,10)",text)


class LockedLibraries(unittest.TestCase):
    """Pinned versions must work together, not only install."""
    def test_pg8000_calls_only_what_the_pinned_scramp_has(self):
        # pg8000 1.31.5 called scramp.make_channel_binding, removed in scramp 1.5.0:
        # every SSL connection (Ubuntu's default PostgreSQL) failed with AttributeError.
        import inspect,re,scramp,pg8000.core
        used=set(re.findall(r'\bscramp\.([A-Za-z_]+)',inspect.getsource(pg8000.core)))
        self.assertTrue(used);self.assertEqual(sorted(n for n in used if not hasattr(scramp,n)),[])


class PublicBundle(unittest.TestCase):
    """The release builder refuses personal data; the committed tree carries none."""
    def builder(self):
        path=ADDON/'release/build.py'
        if not path.is_file():self.skipTest('release builder not shipped in the bundle')
        spec=importlib.util.spec_from_file_location('tooling_release',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

    def test_scanner_catches_personal_data(self):
        b=self.builder()
        # Built at run time so this file itself stays clean for the same scanner.
        u,v,d='/Us'+'ers/','/Vol'+'umes/','DI'+'AG-'
        sample={'a.md':(u+'somebody/x mail someone'+'@gm'+'ail.com '+d+'MAC-ABCDEF12 C:\\'+'Users\\Someone\\x '+v+'Disk/x 123456789:'+'A'*35).encode()}
        labels={x[1].split(':')[0] for x in b.private_findings(sample)}
        self.assertEqual(labels,{'host path','e-mail','learner id','host volume','bot token'})
        self.assertEqual(b.private_findings({'b.md':b'/home/x/fixture %LOCALAPPDATA%\\hermes C:\\Users\\USER\\x'}),[])

    def test_windows_workflow_powershell_parses_as_the_runner_reads_it(self):
        checker=ADDON/'release/check-workflow-powershell.py'
        if not checker.is_file() or not shutil.which('pwsh'):self.skipTest('needs the repository checkout and pwsh')
        r=subprocess.run([sys.executable,str(checker)],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_every_shipped_file_is_clean(self):
        b=self.builder()
        files={n:(ADDON/n).read_bytes() for n in subprocess.check_output(['git','ls-files','-z'],cwd=ADDON).decode().split('\0') if n and not b.EXCLUDE.match(n)}
        self.assertEqual(b.private_findings(files),[])

if __name__=='__main__':unittest.main()
