"""Windows knowledge base without services: backend "file" (SQLite + in-process ONNX bge-m3).

Unit tests use a stand-in embedder and run on any OS. The real-model check runs only with
AGENTS_TOOLING_ONNX_MODEL=<folder with the pinned onnx/ files>.
"""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
ADDON=Path(os.environ.get('AGENTS_TOOLING_SOURCE',str(ROOT/'installer/tooling-addon'))).resolve()
REAL_MODEL=os.environ.get('AGENTS_TOOLING_ONNX_MODEL')


def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ADDON/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


tools=load('student_tools_file','student_tools.py')


def bag_of_words(cfg,texts):
    """Stand-in for bge-m3: deterministic 1024-dim bag of words."""
    out=[]
    for text in texts:
        v=[0.0]*1024
        for word in text.lower().split():v[int(hashlib.sha256(word.encode()).hexdigest(),16)%1024]+=1.0
        out.append(v)
    return out


def file_cfg(root,roots=()):
    return {'schemaVersion':1,'backend':'file','embeddingModel':'bge-m3','embeddingDimensions':1024,
            'kbFile':str(root/'kb/agents-student-kb.sqlite'),'embeddingModelDir':str(root/'model'),
            'embeddingRevision':tools.BGE_M3_ONNX_REVISION,'roots':list(roots),'secretMap':{}}


class FileIndexAndSearch(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        self.a=self.root/'project-a';self.b=self.root/'project-b';self.a.mkdir();self.b.mkdir()
        (self.a/'lighthouse.md').write_text('маяк светит кораблям ночью',encoding='utf-8')
        (self.a/'soup.md').write_text('рецепт борща со свёклой',encoding='utf-8')
        (self.b/'other.md').write_text('маяк соседнего проекта',encoding='utf-8')
        self.cfg=file_cfg(self.root,[{'path':str(self.a),'project':'a','profile':'tech'},
                                     {'path':str(self.b),'project':'b','profile':'tech'}])
        self.embed=patch.object(tools,'embed',side_effect=bag_of_words);self.embed.start()
        tools.REPORTED_SKIPS.clear()

    def tearDown(self):
        self.embed.stop();self.tmp.cleanup()

    def titles(self,query,profile='tech',project='a'):
        return [r['title'] for r in tools.search(self.cfg,query,profile,project)['results']]

    def test_index_search_repeat_update_revert_and_archive(self):
        self.assertTrue(tools.ensure_kb_schema(self.cfg));self.assertFalse(tools.ensure_kb_schema(self.cfg))
        if os.name!='nt':self.assertEqual(os.stat(self.cfg['kbFile']).st_mode&0o777,0o600)
        first=tools.index(self.cfg)
        self.assertEqual(first['documentsWritten'],3);self.assertEqual(first['skippedReasons'],{})
        again=tools.index(self.cfg);self.assertEqual(again['documentsWritten'],0);self.assertEqual(again['unchanged'],3)
        result=tools.search(self.cfg,'маяк кораблям','tech','a')
        # Same response shape as the PostgreSQL backend.
        self.assertEqual(set(result),{'project','profile','results','note'})
        top=result['results'][0]
        self.assertEqual(set(top),{'id','content','contentHash','source','title','metadata','score'})
        self.assertEqual(top['title'],'lighthouse.md');self.assertEqual(top['metadata']['project'],'a')
        self.assertTrue(all(r['metadata']['project']=='a' for r in result['results']))
        (self.a/'lighthouse.md').write_text('маяк светит кораблям ночью amended',encoding='utf-8')
        self.assertEqual(tools.index(self.cfg)['documentsWritten'],1)
        self.assertIn('amended',tools.search(self.cfg,'маяк','tech','a')['results'][0]['content'])
        (self.a/'lighthouse.md').write_text('маяк светит кораблям ночью',encoding='utf-8')
        calls=self.embed.target.embed.call_count
        self.assertEqual(tools.index(self.cfg)['documentsWritten'],1)  # the old record is active again
        self.assertEqual(self.embed.target.embed.call_count,calls)  # ... with its stored vectors, not re-embedded
        self.assertNotIn('amended',json.dumps(tools.search(self.cfg,'маяк','tech','a'),ensure_ascii=False))
        (self.a/'lighthouse.md').unlink()
        tools.index(self.cfg)
        self.assertNotIn('lighthouse.md',self.titles('маяк кораблям'))
        self.assertTrue((self.b/'other.md').exists());self.assertIn('other.md',self.titles('маяк','tech','b'))
        db=tools.kb_connect(self.cfg)
        try:
            states=dict(db.execute("SELECT status,count(*) FROM documents WHERE title='lighthouse.md' GROUP BY status").fetchall())
        finally:db.close()
        self.assertEqual(states,{'archived':2})  # soft archive: history stays, source files untouched

    def test_agents_and_project_filters(self):
        tools.ensure_kb_schema(self.cfg);tools.index(self.cfg)
        self.assertNotIn('other.md',self.titles('маяк'))
        # A record only for the designer is invisible to the tech profile.
        db=tools.kb_connect(self.cfg)
        try:
            db.execute('BEGIN')
            doc=db.execute("""INSERT INTO documents(source_id,source_kind,source_path,title,agents,scopes,source_hash,content,metadata,logical_id,project,root)
                VALUES ('x:1','student-file','/x/design.md','design.md','["ai-designer"]','["a"]','h','маяк дизайнера','{}','x','a','/x')""").lastrowid
            db.execute("INSERT INTO document_agents VALUES (?,'ai-designer')",(doc,))
            db.execute("INSERT INTO chunks(document_id,chunk_index,content,content_hash,embedding_model,embedding) VALUES (?,0,'маяк дизайнера','h','bge-m3',?)",
                       (doc,tools.vector_blob(bag_of_words({},['маяк дизайнера'])[0])))
            db.execute('COMMIT')
        finally:db.close()
        self.assertNotIn('design.md',self.titles('маяк дизайнера','tech'))
        self.assertIn('design.md',self.titles('маяк дизайнера','designer'))

    def test_fts_input_is_never_fts_syntax(self):
        tools.ensure_kb_schema(self.cfg);tools.index(self.cfg)
        self.assertEqual(tools.fts_query('маяк кораблям'),'"маяк" AND "кораблям"')
        self.assertEqual(tools.fts_query('" * - NEAR( ^ :'),'"NEAR"')
        for query in ['"маяк','маяк"кораблям"','маяк*','-маяк','маяк NEAR кораблям','NEAR(маяк кораблям)',
                      'маяк OR борщ','content:маяк','^маяк','{маяк}','***','""','маяк AND','NOT']:
            with self.subTest(query=query):
                result=tools.search(self.cfg,query,'tech','a')  # no sqlite3.OperationalError
                self.assertIsInstance(result['results'],list)
        db=tools.kb_connect(self.cfg)
        try:
            match=lambda q:[r[0] for r in db.execute('SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?',(tools.fts_query(q),))]
            self.assertEqual(len(match('маяк кораблям')),1)
            self.assertEqual(match('маяк OR борщ'),[])  # OR is a word here, not an operator
            self.assertEqual(len(match('МАЯК')),2)      # unicode61 folds case, like 'simple' (both projects here)
        finally:db.close()

    def test_index_runs_once_at_a_time(self):
        tools.ensure_kb_schema(self.cfg)
        held=tools.IndexLock(tools.guard(self.cfg['kbFile']))
        self.assertTrue(held.acquire())
        try:self.assertEqual(tools.index(self.cfg),{'status':'index-already-running'})
        finally:held.release()
        self.assertEqual(tools.index(self.cfg)['documentsWritten'],3)

    def test_index_needs_the_schema_and_refuses_a_foreign_file(self):
        with self.assertRaises(tools.StudentFacingError):tools.index(self.cfg)
        import sqlite3
        kb=Path(self.cfg['kbFile']);kb.parent.mkdir(parents=True)
        db=sqlite3.connect(str(kb));db.execute('CREATE TABLE notes(x)');db.commit();db.close()
        with self.assertRaises(tools.StudentFacingError):tools.ensure_kb_schema(self.cfg)

    def test_status_file_of_the_scheduled_run(self):
        tools.ensure_kb_schema(self.cfg);status=self.root/'tooling/index-status.json';status.parent.mkdir()
        held=tools.IndexLock(tools.guard(self.cfg['kbFile']));self.assertTrue(held.acquire())
        try:tools.index_with_status(self.cfg,status)
        finally:held.release()
        self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['status'],'index-already-running')  # never ok
        tools.index_with_status(self.cfg,status)
        body=json.loads(status.read_text(encoding='utf-8'))
        self.assertEqual(body['status'],'ok');self.assertEqual(body['documentsWritten'],3)
        os.remove(self.cfg['kbFile'])
        with self.assertRaises(tools.StudentFacingError):tools.index_with_status(self.cfg,status)
        self.assertEqual(json.loads(status.read_text(encoding='utf-8'))['status'],'index-failed')


class FileSettings(unittest.TestCase):
    def write(self,root,**changes):
        cfg={**file_cfg(root),**changes};path=root/'settings.json'
        path.write_text(json.dumps(cfg),encoding='utf-8');return path

    def test_config_accepts_the_file_backend_and_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            self.assertEqual(tools.config(self.write(root))['backend'],'file')
            bom=root/'bom.json';bom.write_bytes(b'\xef\xbb\xbf'+json.dumps(file_cfg(root)).encode())
            self.assertEqual(tools.config(bom)['backend'],'file')  # as Windows PowerShell 5.1 writes it
            for bad in [{'databasePassword':'x'},{'password':'x'},{'database':{'password':'x'}},{'dsn':'x'},
                        {'embeddingRevision':'0'*40},{'kbFile':'relative.sqlite'},{'kbFile':str(root/'kb.db')},
                        {'embeddingModelDir':'model'},{'backend':'cloud'}]:
                with self.subTest(bad=bad),self.assertRaises(tools.StudentFacingError):tools.config(self.write(root,**bad))
            with self.assertRaises(ValueError):tools.config(self.write(root,embeddingDimensions=768))
            with self.assertRaises(ValueError):tools.config(self.write(root,embeddingModel='other'))

    def test_windows_uses_the_file_backend_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();postgres=root/'pg.json'
            postgres.write_text((ADDON/'settings.example.json').read_text(encoding='utf-8'),encoding='utf-8')
            self.assertNotIn('backend',tools.config(postgres))  # Mac/Ubuntu: no field, unchanged
            with patch.object(tools,'IS_WINDOWS',True):
                with self.assertRaises(tools.StudentFacingError) as caught:tools.config(postgres)
                self.assertIn('backend file',str(caught.exception))
                self.assertEqual(tools.config(self.write(root))['backend'],'file')

    def test_the_windows_example_is_a_file_backend(self):
        example=json.loads((ADDON/'settings.windows.example.json').read_text(encoding='utf-8'))
        self.assertEqual(example['backend'],'file');self.assertEqual(example['embeddingRevision'],tools.BGE_M3_ONNX_REVISION)
        self.assertNotIn('database',example);self.assertTrue(example['kbFile'].endswith('.sqlite'))


class PinnedEmbeddingModel(unittest.TestCase):
    def fixture(self):
        files={'model.onnx':b'graph','model.onnx_data':b'weights'*3,'tokenizer.json':b'{}'}
        pins={n:(hashlib.sha256(d).hexdigest(),len(d)) for n,d in files.items()}
        calls=[]
        def download(url,target,size,data=files):
            calls.append(url);name=url.rsplit('/',1)[1];Path(target).write_bytes(data[name])
        return files,pins,download,calls

    def test_fetch_verifies_publishes_atomically_and_is_idempotent(self):
        files,pins,download,calls=self.fixture()
        with tempfile.TemporaryDirectory() as tmp,patch.dict(tools.BGE_M3_ONNX_FILES,pins,clear=True):
            target=Path(tmp).resolve()/'models/bge-m3-onnx'
            first=tools.fetch_embedding_model(target,download=download,sleep=lambda s:None)
            self.assertTrue(first['changed']);self.assertEqual(first['revision'],tools.BGE_M3_ONNX_REVISION)
            self.assertEqual(sorted(p.name for p in target.iterdir()),sorted(files))
            self.assertTrue(all(u.startswith('https://huggingface.co/BAAI/bge-m3/resolve/'+tools.BGE_M3_ONNX_REVISION+'/onnx/') for u in calls))
            again=tools.fetch_embedding_model(target,download=lambda *a:self.fail('no second download'))
            self.assertIs(again['changed'],False)
            self.assertEqual([p.name for p in target.parent.iterdir()],['bge-m3-onnx'])
            cfg={'embeddingModelDir':str(target)}
            self.assertTrue(tools.file_model_check(cfg)['verified'])

    def test_wrong_sha_is_a_hard_stop_and_nothing_is_published(self):
        files,pins,download,_=self.fixture()
        bad={**files,'model.onnx_data':b'WEIGHTS'*3}  # same size, other bytes
        with tempfile.TemporaryDirectory() as tmp,patch.dict(tools.BGE_M3_ONNX_FILES,pins,clear=True):
            target=Path(tmp).resolve()/'models/bge-m3-onnx'
            with self.assertRaises(tools.StudentFacingError) as caught:
                tools.fetch_embedding_model(target,download=lambda u,t,s:download(u,t,s,bad),sleep=lambda s:None)
            self.assertIn('model.onnx_data',str(caught.exception));self.assertIn('не установлена',str(caught.exception))
            self.assertFalse(target.exists());self.assertEqual(list(target.parent.iterdir()),[])
            # A non-empty folder that is not the pinned model is the student's: never touched.
            target.mkdir();(target/'model.onnx').write_bytes(b'mine')
            with self.assertRaises(tools.StudentFacingError):tools.fetch_embedding_model(target,download=download)
            self.assertEqual((target/'model.onnx').read_bytes(),b'mine')
            with self.assertRaises(tools.StudentFacingError):tools.file_model_check({'embeddingModelDir':str(target)})

    def test_transient_download_errors_are_retried(self):
        files,pins,download,_=self.fixture();failures=[]
        def flaky(url,target,size):
            if len(failures)<2:failures.append(url);raise ConnectionResetError()
            download(url,target,size)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(tools.BGE_M3_ONNX_FILES,pins,clear=True):
            pauses=[]
            result=tools.fetch_embedding_model(Path(tmp).resolve()/'m',download=flaky,sleep=pauses.append)
            self.assertTrue(result['changed']);self.assertEqual(len(pauses),2)

    def test_download_resumes_a_dropped_transfer(self):
        import io
        seen=[]
        class Response(io.BytesIO):
            status=206
            def __enter__(self):return self
            def __exit__(self,*a):return False
        def opener(request,timeout=None):
            seen.append(request.get_header('Range'));return Response(b'world')
        with tempfile.TemporaryDirectory() as tmp,patch.object(tools.urllib.request,'urlopen',side_effect=opener):
            target=Path(tmp)/'f';(Path(tmp)/'f.part').write_bytes(b'hello ')
            tools.download_file('https://huggingface.co/x',target,11)
            self.assertEqual(target.read_bytes(),b'hello world');self.assertEqual(seen,['bytes=6-'])
            with self.assertRaises(ConnectionError):tools.download_file('https://huggingface.co/x',Path(tmp)/'g',100)

    def test_file_backend_dispatch_never_reaches_ollama(self):
        with patch.object(tools,'ollama_tags',side_effect=AssertionError('no Ollama')),\
             patch.object(tools,'file_model_check',return_value={'verified':True}) as check:
            self.assertTrue(tools.model_check({'backend':'file'})['verified']);check.assert_called_once()


class WindowsProcesses(unittest.TestCase):
    def test_vault_mcp_runs_a_child_with_inherited_stdio_on_windows(self):
        argv=[sys.executable,'-B','student_tools.py','--config','s.json','mcp'];env={'PATH':'x'}
        with patch.object(tools.subprocess,'run',return_value=subprocess.CompletedProcess(argv,7)) as run,\
             patch.object(tools.os,'execve',side_effect=AssertionError('no execve on Windows')):
            self.assertEqual(tools.launch_mcp(argv,env,windows=True),7)
        run.assert_called_once_with(argv,env=env)  # no capture: the child owns this stdio
        with patch.object(tools.os,'execve') as execve,patch.object(tools.subprocess,'run',side_effect=AssertionError('POSIX execs')):
            tools.launch_mcp(argv,env,windows=False)
        execve.assert_called_once_with(argv[0],argv,env)

    def test_vault_mcp_command_passes_the_child_exit_code_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json'
            settings.write_text(json.dumps({**file_cfg(root),'secretMap':{'X_API_KEY':'11111111-1111-4111-8111-111111111111'}}),encoding='utf-8')
            started=[]
            def child(argv,env=None,**kw):
                started.append((argv,env,kw));return subprocess.CompletedProcess(argv,3)
            with patch.object(tools,'IS_WINDOWS',True),patch.object(tools,'windows_native_arm64',return_value=False),\
                 patch.object(tools,'vault_env',return_value=({'X_API_KEY':'v'},['v'])),\
                 patch.dict(os.environ,{'BWS_ACCESS_TOKEN':'fixture-bootstrap-token'}),\
                 patch.object(tools.subprocess,'run',side_effect=child),patch.object(sys,'argv',['student_tools.py','--config',str(settings),'vault-mcp']):
                with self.assertRaises(SystemExit) as caught:tools.main()
                self.assertNotIn('BWS_ACCESS_TOKEN',os.environ)  # the waiting Windows parent keeps no token
            self.assertEqual(caught.exception.code,3)
            argv,env,kw=started[0]
            self.assertEqual(argv[-3:],['--config',str(settings),'mcp']);self.assertEqual(env,{'X_API_KEY':'v'});self.assertEqual(kw,{})

    def test_watch_is_refused_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json'
            settings.write_text(json.dumps(file_cfg(root)),encoding='utf-8')
            with patch.object(tools,'IS_WINDOWS',True),patch.object(tools,'windows_native_arm64',return_value=False),\
                 patch.object(sys,'argv',['student_tools.py','--config',str(settings),'watch']),\
                 patch.object(tools,'index',side_effect=AssertionError('no watch loop')):
                with self.assertRaises(tools.StudentFacingError) as caught:tools.main()
            self.assertIn('schedule-index',str(caught.exception))

    def test_scheduled_task_is_per_user_bounded_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'Папка ученика';settings=root/'tooling/settings.json';settings.parent.mkdir(parents=True)
            settings.write_text('{}',encoding='utf-8');pythonw=root/'venv/Scripts/pythonw.exe';pythonw.parent.mkdir(parents=True);pythonw.write_bytes(b'')
            registered={};calls=[]
            def schtasks(args,**kw):
                calls.append(args);self.assertIs(kw.get('stdin'),subprocess.DEVNULL)
                if args[1]=='/Query':return subprocess.CompletedProcess(args,0 if registered else 1)
                if args[1]=='/Create':registered['xml']=Path(args[args.index('/XML')+1]).read_bytes().decode('utf-16');return subprocess.CompletedProcess(args,0)
                if args[1]=='/End':return subprocess.CompletedProcess(args,1)  # not running: fine
                if args[1]=='/Delete':registered.clear();return subprocess.CompletedProcess(args,0)
            with patch.dict(os.environ,{'USERNAME':'student','USERDOMAIN':'PC'}):
                first=tools.schedule_index(settings,5,python=pythonw,run=schtasks,windows=True)
                self.assertTrue(first['changed']);self.assertEqual(first['task'],'AgentsTooling-KB-Index-student')
                xml=registered['xml']
                self.assertIn('<Interval>PT5M</Interval>',xml);self.assertIn('<RunLevel>LeastPrivilege</RunLevel>',xml)
                self.assertIn('<LogonType>InteractiveToken</LogonType>',xml);self.assertNotIn('UserId',xml)
                self.assertIn('IgnoreNew',xml);self.assertIn(str(pythonw),xml);self.assertIn(' index --status-file ',xml)
                self.assertNotIn('watch',xml)
                # Paths with a space are quoted the way pythonw parses its command line.
                self.assertIn('"'+str(root/'tooling/settings.json')+'"',xml)
                creates=sum(c[1]=='/Create' for c in calls)
                self.assertIs(tools.schedule_index(settings,5,python=pythonw,run=schtasks,windows=True)['changed'],False)
                self.assertEqual(sum(c[1]=='/Create' for c in calls),creates)
                self.assertTrue(tools.schedule_index(settings,10,python=pythonw,run=schtasks,windows=True)['changed'])
                self.assertIn('PT10M',registered['xml'])
                self.assertEqual(list(settings.parent.glob('.index-task-*.xml')),[])  # the definition file is not left behind
                for bad in (0,-1,2000):
                    with self.assertRaises(tools.StudentFacingError):tools.schedule_index(settings,bad,python=pythonw,run=schtasks,windows=True)
                with self.assertRaises(tools.StudentFacingError):tools.schedule_index(settings,5,python=pythonw,run=schtasks,windows=False)
                calls.clear()
                self.assertTrue(tools.unschedule_index(settings,run=schtasks,windows=True)['changed'])
                self.assertEqual([c[1] for c in calls],['/Query','/End','/Delete'])  # a running index is ended first
                self.assertIs(tools.unschedule_index(settings,run=schtasks,windows=True)['changed'],False)

    def test_task_name_is_per_user_and_ascii(self):
        self.assertEqual(tools.index_task_name('student'),'AgentsTooling-KB-Index-student')
        cyr=tools.index_task_name('Иван Петров');other=tools.index_task_name('Пётр Иванов')
        self.assertTrue(cyr.isascii() and other.isascii());self.assertNotEqual(cyr,other)
        self.assertRegex(tools.index_task_name('a.b c'),r'^AgentsTooling-KB-Index-a_b_c-[0-9a-f]{8}$')

    def test_windows_arm64_is_refused_before_any_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text(json.dumps(file_cfg(root)),encoding='utf-8')
            with patch.object(tools,'IS_WINDOWS',True),patch.object(tools,'windows_native_arm64',return_value=True),\
                 patch.object(tools,'config',side_effect=AssertionError('no work on ARM')),\
                 patch.object(sys,'argv',['student_tools.py','--config',str(settings),'doctor']):
                with self.assertRaises(tools.StudentFacingError) as caught:tools.main()
            self.assertEqual(str(caught.exception),'Windows ARM пока не поддерживается: только диагностика')
        self.assertFalse(tools.windows_native_arm64())  # never ARM-refused outside Windows

    def test_windows_json_output_is_ascii(self):
        value={'message':'маяк 🚢'}
        with patch.object(tools,'IS_WINDOWS',True):
            text=tools.dump_json(value,ensure_ascii=False);self.assertTrue(text.isascii());self.assertEqual(json.loads(text),value)
        with patch.object(tools,'IS_WINDOWS',False):
            self.assertEqual(tools.dump_json(value,ensure_ascii=False),json.dumps(value,ensure_ascii=False))  # Mac/Ubuntu as before


class WindowsMcpServer(unittest.TestCase):
    """The deadlock fix: on Windows (backend file) native libraries load before stdin is read,
    and the server reads a private duplicate of the pipe while the std handle is NUL."""
    def run_serve(self,windows,backend):
        order=[]
        with patch.object(tools,'IS_WINDOWS',windows),\
             patch.object(tools,'preload_native_libraries',side_effect=lambda:order.append('preload')),\
             patch.object(tools,'detach_std_input',side_effect=lambda:order.append('detach') or 'private-stdin'),\
             patch.object(tools,'run_stdio',side_effect=lambda server,stdin:order.append(('run_stdio',stdin))),\
             patch('mcp.server.fastmcp.FastMCP.run',side_effect=lambda transport:order.append(('fastmcp',transport))):
            tools.serve({'backend':backend})
        return order

    def test_windows_file_backend_preloads_and_swaps_stdin(self):
        self.assertEqual(self.run_serve(True,'file'),['preload','detach',('run_stdio','private-stdin')])

    def test_posix_and_postgres_keep_the_plain_stdio_server(self):
        self.assertEqual(self.run_serve(False,'file'),[('fastmcp','stdio')])
        self.assertEqual(self.run_serve(False,'postgres'),[('fastmcp','stdio')])

    def test_qa_keep_alive_needs_the_other_qa_knobs(self):
        with patch.dict(os.environ,{'AGENTS_TOOLING_QA_KEEPALIVE_SECONDS':'3'}):
            os.environ.pop('AGENTS_TOOLING_QA_MAX_FILES',None);self.assertIsNone(tools.qa_keep_alive())
            os.environ['AGENTS_TOOLING_QA_MAX_FILES']='3';self.assertEqual(tools.qa_keep_alive(),3)
            self.run_serve(False,'file');self.assertEqual(tools._ENGINE['keepAlive'],3)
        tools._ENGINE['keepAlive']=None

    def test_the_low_level_stdio_path_matches_the_installed_mcp(self):
        import inspect
        from mcp.server.fastmcp import FastMCP
        from mcp.server.stdio import stdio_server
        self.assertEqual(list(inspect.signature(stdio_server).parameters),['stdin','stdout'])
        server=FastMCP('x');self.assertTrue(callable(server._mcp_server.run));self.assertTrue(server._mcp_server.create_initialization_options())


class SourceSafety(unittest.TestCase):
    def test_secret_like_names_are_skipped_in_any_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'note.md').write_text('ok',encoding='utf-8')
            for name in ('AUTH.JSON','Secrets.yaml','secrets.json','server.PEM','id_rsa','.env.local'):(root/name).write_text('x',encoding='utf-8')
            for folder in ('Secrets','CREDENTIALS','cookies'):(root/folder).mkdir();(root/folder/'leak.md').write_text('x',encoding='utf-8')
            self.assertEqual([p.name for p in tools.candidates(root)],['note.md'])

    def test_windows_ignores_case_for_every_excluded_name(self):
        with patch.object(tools,'IS_WINDOWS',True),patch.object(tools.os.path,'normcase',side_effect=str.lower):
            self.assertTrue(tools.excluded_name('Config.YAML'));self.assertTrue(tools.excluded_name('Logs'))
        self.assertFalse(tools.excluded_name('Logs'))  # POSIX names keep their case, as before

    def test_only_name_surrogate_reparse_points_are_links_on_windows(self):
        cases={'symlink':(0x400,0xA000000C,True),'mount point/junction':(0x400,0xA0000003,True),
               'OneDrive placeholder':(0x400,0x9000001A,False),'dedup':(0x400,0x80000013,False),'plain':(0x20,0,False)}
        for label,(attributes,tag,link) in cases.items():
            with self.subTest(label),patch.object(tools,'IS_WINDOWS',True),patch.object(tools,'win_attributes',return_value=(attributes,tag)):
                self.assertIs(tools.reparse_point('/x/item'),link)
                if link:
                    with self.assertRaises(ValueError):tools.guard('/x/item/file')
                else:self.assertEqual(str(tools.guard('/x/item/file')),str(Path('/x/item/file').absolute()))
        bootstrap=load('tooling_bootstrap_reparse','bootstrap.py')
        class St:
            def __init__(self,a,t):self.st_file_attributes=a;self.st_reparse_tag=t
        for (a,t),link in {(0x400,0xA0000003):True,(0x400,0x9000001A):False,(0x400,0x80000013):False}.items():
            with patch.object(bootstrap.os,'lstat',return_value=St(a,t)):self.assertIs(bootstrap.reparse_point('/x'),link)

    def test_cloud_placeholders_are_indexed_only_when_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            for name in ('local.md','placeholder.md','offline.md','dedup.md'):(root/name).write_text('x',encoding='utf-8')
            attributes={'placeholder.md':(0x400|0x400000,0x9000001A),'offline.md':(0x400|0x1000,0x9000001A),
                        'local.md':(0x400,0x9000001A),'dedup.md':(0x400,0x80000013)}
            def attrs(path):return attributes.get(Path(path).name,(0x10,0))
            problems={}
            with patch.object(tools,'IS_WINDOWS',True),patch.object(tools,'win_attributes',side_effect=attrs):
                found=[p.name for p in tools.candidates(root,problems)]
            self.assertEqual(found,['dedup.md','local.md'])  # never a mass download from OneDrive
            self.assertEqual(problems,{'cloudOnly':2})

    def test_an_unreadable_file_is_counted_and_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'good.md').write_text('a',encoding='utf-8');(root/'locked.md').write_text('b',encoding='utf-8')
            real=Path.stat
            def stat(self,*a,**kw):
                if self.name=='locked.md' and not kw.get('follow_symlinks',True) is False:raise PermissionError(13,'denied')
                return real(self,*a,**kw)
            problems={}
            with patch.object(Path,'stat',stat):
                self.assertEqual([p.name for p in tools.candidates(root,problems)],['good.md'])
                with self.assertRaises(PermissionError):list(tools.candidates(root))  # Mac/Ubuntu path unchanged
            self.assertEqual(problems,{'unreadable':1})


class SettingsEncodings(unittest.TestCase):
    def test_utf16_and_bom_settings_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();cfg=file_cfg(root)
            for name,data in [('utf16le.json',json.dumps(cfg,ensure_ascii=False).encode('utf-16')),
                              ('utf16be.json',b'\xfe\xff'+json.dumps(cfg).encode('utf-16-be')),
                              ('utf8bom.json',b'\xef\xbb\xbf'+json.dumps(cfg).encode()),('utf8.json',json.dumps(cfg).encode())]:
                with self.subTest(name=name):
                    (root/name).write_bytes(data);self.assertEqual(tools.config(root/name)['backend'],'file')
            bootstrap=load('tooling_bootstrap_enc','bootstrap.py')
            self.assertEqual(json.loads(bootstrap.settings_text(root/'utf16le.json'))['kbFile'],cfg['kbFile'])


class FileBootstrap(unittest.TestCase):
    def install(self,root):
        import yaml
        home=root/'hermes';folder=home/'profiles/tech';(folder/'skills/s').mkdir(parents=True)
        (folder/'skills/s/SKILL.md').write_text('# Маяк\nНавык про маяк.',encoding='utf-8')
        (folder/'AGENTS.md').write_text('# tech\nЛичное правило.\n',encoding='utf-8');(folder/'SOUL.md').write_text('# Душа\n',encoding='utf-8')
        (folder/'config.yaml').write_text(yaml.safe_dump({'model':{'provider':'x'},'display':{'name':'Технарь'}},allow_unicode=True),encoding='utf-8')
        settings=root/'tooling/settings.json';settings.parent.mkdir()
        settings.write_text(json.dumps(file_cfg(root/'tooling'),ensure_ascii=False),encoding='utf-8')
        return home,folder,settings

    def test_bootstrap_creates_the_sqlite_schema_once(self):
        import yaml
        bootstrap=load('tooling_bootstrap_file','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            home,folder,settings=self.install(Path(tmp).resolve())
            first=bootstrap.prepare(home,settings)
            self.assertTrue(first['changed']);self.assertTrue(first['kbSchemaCreated']);self.assertEqual(first['backend'],'file')
            self.assertTrue(Path(json.loads(settings.read_text(encoding='utf-8'))['kbFile']).is_file())
            entry=yaml.safe_load((folder/'config.yaml').read_text(encoding='utf-8'))['mcp_servers']['student-agent-kb']
            self.assertEqual(entry['args'][1:],[str(ADDON/'student_tools.py'),'--config',str(settings),'mcp'])
            self.assertEqual(yaml.safe_load((folder/'config.yaml').read_text(encoding='utf-8'))['display']['name'],'Технарь')
            second=bootstrap.prepare(home,settings)
            self.assertIs(second['changed'],False);self.assertEqual(second['changedFiles'],0);self.assertIs(second['kbSchemaCreated'],False)
            self.assertEqual((folder/'AGENTS.md').read_text(encoding='utf-8').count(bootstrap.BEGIN),1)

    def test_bootstrap_with_index_on_the_file_backend(self):
        bootstrap=load('tooling_bootstrap_file_index','bootstrap.py')
        real_module_from_spec=importlib.util.module_from_spec
        def module_from_spec(spec):
            # bootstrap loads its own copy of student_tools: give that copy the stand-in model.
            module=real_module_from_spec(spec)
            if spec.name=='student_tools':
                original=spec.loader.exec_module
                def exec_module(m):original(m);m.embed=bag_of_words;m.doctor=lambda cfg:{'ready':True}
                spec.loader.exec_module=exec_module
            return module
        with tempfile.TemporaryDirectory() as tmp:
            home,folder,settings=self.install(Path(tmp).resolve())
            with patch.object(bootstrap.importlib.util,'module_from_spec',side_effect=module_from_spec),\
                 patch.dict(os.environ,{'AGENTS_TOOLING_QA_MAX_FILES':''}):
                first=bootstrap.prepare(home,settings,initial_index=True)
                second=bootstrap.prepare(home,settings,initial_index=True)
            self.assertGreaterEqual(first['index']['documentsWritten'],2);self.assertTrue(first['changed'])
            self.assertIs(second['changed'],False);self.assertEqual(second['changedFiles'],0)
            self.assertEqual(second['index']['documentsWritten'],0)

    def test_windows_bootstrap_refuses_the_postgres_backend(self):
        bootstrap=load('tooling_bootstrap_win','bootstrap.py')
        with tempfile.TemporaryDirectory() as tmp:
            home,folder,settings=self.install(Path(tmp).resolve())
            settings.write_text((ADDON/'settings.example.json').read_text(encoding='utf-8'),encoding='utf-8')
            before=(folder/'config.yaml').read_bytes()
            with patch.object(bootstrap,'WINDOWS',True),self.assertRaises(bootstrap.BootstrapError) as caught:bootstrap.prepare(home,settings)
            self.assertIn('backend file',str(caught.exception));self.assertEqual((folder/'config.yaml').read_bytes(),before)


@unittest.skipUnless(REAL_MODEL,'Set AGENTS_TOOLING_ONNX_MODEL to the pinned onnx/ folder for the real-model check')
class RealModel(unittest.TestCase):
    """The pinned ONNX bge-m3 end to end: model-check, doctor, index, Russian search, MCP stdio."""
    def test_russian_query_finds_the_right_file(self):
        real=load('student_tools_real','student_tools.py')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();notes=root/'notes';notes.mkdir()
            (notes/'mayak.md').write_text('Маяк на скалистом мысе каждую ночь подаёт световой сигнал проходящим судам.',encoding='utf-8')
            (notes/'borsch.md').write_text('Для борща нужны свёкла, капуста, морковь и говяжий бульон.',encoding='utf-8')
            (notes/'taxes.md').write_text('Самозанятый платит налог на профессиональный доход через приложение «Мой налог».',encoding='utf-8')
            cfg=file_cfg(root/'tooling',[{'path':str(notes),'project':'p','profile':'tech'}]);cfg['embeddingModelDir']=str(Path(REAL_MODEL).resolve())
            settings=root/'tooling/settings.json';settings.parent.mkdir();settings.write_text(json.dumps(cfg),encoding='utf-8')
            cfg=real.config(settings)
            self.assertTrue(real.model_check(cfg)['verified'])
            health=real.doctor(cfg);self.assertEqual(health['embeddingDimensions'],1024);self.assertTrue(health['local'])
            self.assertTrue(real.ensure_kb_schema(cfg))
            self.assertEqual(real.index(cfg)['documentsWritten'],3)
            # No shared words with the file: only the meaning connects them.
            for query,expected in [('где ночью светит огонь для кораблей','mayak.md'),('как сварить суп со свеклой','borsch.md'),
                                   ('какие налоги у самозанятых','taxes.md')]:
                with self.subTest(query=query):
                    self.assertEqual(real.search(cfg,query,'tech','p')['results'][0]['title'],expected)
            vectors=real.embed(cfg,['Маяк','Маяк на скалистом мысе'*40]);single=real.embed(cfg,['Маяк'])[0]
            import numpy as np
            self.assertGreater(float(np.dot(vectors[0],single)),0.9999)  # padding in a batch changes nothing
            with self.assertRaises(real.StudentFacingError):real.embed(cfg,['слово '*9000])
            self.assertEqual(real.index(cfg)['unchanged'],3)

            async def mcp_call():
                from mcp import ClientSession,StdioServerParameters
                from mcp.client.stdio import stdio_client
                params=StdioServerParameters(command=sys.executable,args=['-B',str(ADDON/'student_tools.py'),'--config',str(settings),'mcp'],
                                             env={'PATH':os.environ['PATH'],'HOME':tmp,'USERPROFILE':tmp})
                async with stdio_client(params) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        self.assertEqual([t.name for t in (await session.list_tools()).tools],['agent_kb_search'])
                        response=await session.call_tool('agent_kb_search',{'query':'где ночью светит огонь для кораблей','project':'p'})
                        return json.loads(response.content[0].text)
            if importlib.util.find_spec('mcp') is None:self.skipTest('model checks passed; no mcp client in this Python for the stdio part')
            answer=asyncio.run(mcp_call())
            self.assertEqual(answer['results'][0]['title'],'mayak.md')


if __name__=='__main__':unittest.main()
