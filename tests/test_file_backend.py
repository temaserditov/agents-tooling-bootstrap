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


def launchctl_print(plist_path):
    """What `launchctl print` shows for a loaded job (path, interval, arguments)."""
    import plistlib
    body=plistlib.loads(Path(plist_path).read_bytes())
    lines=['gui/501/'+body['Label']+' = {','\tactive count = 0','\tpath = '+str(plist_path),'\ttype = LaunchAgent',
           '\tstate = not running','\tprogram = '+body['ProgramArguments'][0],'\targuments = {',
           *['\t\t'+a for a in body['ProgramArguments']],'\t}','\tstdout path = /dev/null',
           '\trun interval = '+str(body['StartInterval'])+' seconds','}']
    return ('\n'.join(lines)+'\n').encode()


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
                with self.assertRaises(tools.StudentFacingError):tools.schedule_index(settings,5,python=pythonw,run=schtasks,windows=False,mac=False)
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


class MacFileBackend(unittest.TestCase):
    """Mac: backend file on Apple Silicon (macOS 14+), the index refreshed by a LaunchAgent."""

    def test_mac_file_backend_needs_apple_silicon_and_macos_14(self):
        tools.mac_file_supported('27.0.1','arm64');tools.mac_file_supported('14.0','arm64')
        tools.mac_file_supported('15.7.2','arm64');tools.mac_file_supported('26.1','arm64')
        for version,machine in (('13.7','arm64'),('','arm64'),('15.0','x86_64'),('27.0','x86_64')):
            with self.assertRaises(tools.StudentFacingError):tools.mac_file_supported(version,machine)

    def test_mac_config_refuses_intel_and_old_macos_for_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json'
            settings.write_text(json.dumps(file_cfg(root)),encoding='utf-8')
            with patch.object(tools,'IS_MAC',True),patch.object(tools,'IS_WINDOWS',False):
                with patch.object(tools.platform,'machine',return_value='arm64'),patch.object(tools.platform,'mac_ver',return_value=('27.0.1','','')):
                    self.assertEqual(tools.config(settings)['backend'],'file')
                with patch.object(tools.platform,'machine',return_value='x86_64'),patch.object(tools.platform,'mac_ver',return_value=('15.7','','')):
                    with self.assertRaises(tools.StudentFacingError):tools.config(settings)
                with patch.object(tools.platform,'machine',return_value='arm64'),patch.object(tools.platform,'mac_ver',return_value=('13.6','','')):
                    with self.assertRaises(tools.StudentFacingError):tools.config(settings)

    def test_watch_is_refused_for_the_file_backend_on_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json'
            settings.write_text(json.dumps(file_cfg(root)),encoding='utf-8')
            with patch.object(tools,'IS_WINDOWS',False),patch.object(tools,'IS_MAC',True),\
                 patch.object(tools,'mac_file_supported',return_value=None),\
                 patch.object(sys,'argv',['student_tools.py','--config',str(settings),'watch']),\
                 patch.object(tools,'index',side_effect=AssertionError('no watch loop')):
                with self.assertRaises(tools.StudentFacingError) as caught:tools.main()
            self.assertIn('schedule-index',str(caught.exception))

    def test_launch_agent_is_per_user_bounded_and_idempotent(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'Папка ученика';settings=root/'tooling/settings.json';settings.parent.mkdir(parents=True)
            settings.write_text('{}',encoding='utf-8');python=root/'tooling/venv/bin/python';python.parent.mkdir(parents=True);python.write_bytes(b'')
            agents=root/'Library/LaunchAgents';loaded={};calls=[]
            def launchctl(args,**kw):
                calls.append(args[1:]);self.assertIs(kw.get('stdin'),subprocess.DEVNULL);self.assertEqual(args[0],'/bin/launchctl')
                if args[1]=='print':
                    return subprocess.CompletedProcess(args,0,stdout=loaded['print']) if loaded else subprocess.CompletedProcess(args,113,stdout=b'')
                if args[1]=='bootstrap':
                    self.assertEqual(args[2],'gui/501');loaded['plist']=Path(args[3]).read_bytes();loaded['print']=launchctl_print(args[3])
                    return subprocess.CompletedProcess(args,0)
                if args[1]=='bootout':loaded.clear();return subprocess.CompletedProcess(args,0)
                raise AssertionError(args)
            first=tools.schedule_index_mac(settings,5,python=python,run=launchctl,agents_dir=agents,uid=501)
            self.assertTrue(first['changed']);self.assertEqual(first['task'],'com.infobizagents.tooling.kb-index')
            self.assertEqual(first['runNow'],'launchctl kickstart gui/501/com.infobizagents.tooling.kb-index')
            plist=agents/'com.infobizagents.tooling.kb-index.plist'
            body=plistlib.loads(plist.read_bytes())
            self.assertEqual(oct(plist.stat().st_mode&0o777),'0o644')
            self.assertEqual(body['StartInterval'],300);self.assertFalse(body['RunAtLoad']);self.assertEqual(body['ProcessType'],'Background')
            self.assertEqual(body['ProgramArguments'],[str(python),'-B',str(Path(tools.__file__).resolve()),'--config',str(settings),
                                                       'index','--status-file',str(settings.parent/'index-status.json')])
            self.assertNotIn('watch',body['ProgramArguments']);self.assertNotIn('KeepAlive',body)
            self.assertEqual(body['StandardOutPath'],'/dev/null')
            self.assertFalse(any('EnvironmentVariables' in k for k in body))  # no secret can ride in the plist
            boots=sum(c[0]=='bootstrap' for c in calls)
            self.assertIs(tools.schedule_index_mac(settings,5,python=python,run=launchctl,agents_dir=agents,uid=501)['changed'],False)
            self.assertEqual(sum(c[0]=='bootstrap' for c in calls),boots)
            calls.clear()
            self.assertTrue(tools.schedule_index_mac(settings,10,python=python,run=launchctl,agents_dir=agents,uid=501)['changed'])
            self.assertEqual([c[0] for c in calls],['print','bootout','bootstrap'])
            self.assertEqual(plistlib.loads(loaded['plist'])['StartInterval'],600)
            self.assertEqual(list(agents.glob('.*')),[])
            for bad in (0,-1,2000,True):
                with self.assertRaises(tools.StudentFacingError):tools.schedule_index_mac(settings,bad,python=python,run=launchctl,agents_dir=agents,uid=501)
            calls.clear()
            self.assertTrue(tools.unschedule_index_mac(settings,run=launchctl,agents_dir=agents,uid=501)['changed'])
            self.assertEqual([c[0] for c in calls],['print','bootout']);self.assertFalse(plist.exists())
            self.assertIs(tools.unschedule_index_mac(settings,run=launchctl,agents_dir=agents,uid=501)['changed'],False)

    def launchctl_fake(self,agents,fail=()):
        state={'loaded':None,'calls':[]}
        def run(args,**kw):
            state['calls'].append(args[1]);op=args[1]
            if op in fail:return subprocess.CompletedProcess(args,5,stdout=b'')
            if op=='print':
                return subprocess.CompletedProcess(args,0,stdout=state['print']) if state['loaded'] \
                    else subprocess.CompletedProcess(args,113,stdout=b'')
            if op=='bootstrap':state['loaded']=args[3];state['print']=launchctl_print(args[3]);return subprocess.CompletedProcess(args,0)
            if op=='bootout':state['loaded']=None;return subprocess.CompletedProcess(args,0)
        return state,run

    def test_failed_reload_puts_the_previous_job_back(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            plist=agents/'com.infobizagents.tooling.kb-index.plist';before=plist.read_bytes()
            calls={'n':0}
            def flaky(args,**kw):
                if args[1]=='bootstrap':
                    calls['n']+=1
                    if calls['n']==1:return subprocess.CompletedProcess(args,5)
                return run(args,**kw)
            with self.assertRaises(tools.StudentFacingError):
                tools.schedule_index_mac(settings,10,python=python,run=flaky,agents_dir=agents,uid=501)
            self.assertEqual(plist.read_bytes(),before);self.assertEqual(state['loaded'],str(plist))
            self.assertEqual(plistlib.loads(plist.read_bytes())['StartInterval'],300)
            # a refused bootout changes nothing
            _,stuck=self.launchctl_fake(agents,fail=('bootout',))
            def stuck_loaded(args,**kw):
                if args[1]=='print':return subprocess.CompletedProcess(args,0,stdout=launchctl_print(plist))
                return stuck(args,**kw)
            with self.assertRaises(tools.StudentFacingError):
                tools.schedule_index_mac(settings,15,python=python,run=stuck_loaded,agents_dir=agents,uid=501)
            self.assertEqual(plist.read_bytes(),before)

    def test_first_failed_load_leaves_no_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';_,run=self.launchctl_fake(agents,fail=('bootstrap',))
            with self.assertRaises(tools.StudentFacingError):
                tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            self.assertEqual(list(agents.glob('*')),[])

    def test_unschedule_keeps_the_job_of_another_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();python=root/'python';python.write_bytes(b'')
            new=root/'new/settings.json';new.parent.mkdir();new.write_text('{}')
            old=root/'old/settings.json';old.parent.mkdir();old.write_text('{}')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(new,5,python=python,run=run,agents_dir=agents,uid=501)
            with self.assertRaises(tools.StudentFacingError):tools.unschedule_index_mac(old,run=run,agents_dir=agents,uid=501)
            self.assertTrue((agents/'com.infobizagents.tooling.kb-index.plist').is_file());self.assertIsNotNone(state['loaded'])
            self.assertTrue(tools.unschedule_index_mac(new,run=run,agents_dir=agents,uid=501)['changed'])

    def test_scheduled_run_with_refused_settings_leaves_a_fresh_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';status=root/'index-status.json'
            settings.write_text(json.dumps({**file_cfg(root),'embeddingRevision':'0'*40}),encoding='utf-8')
            status.write_text(json.dumps({'status':'ok','runId':'old'}),encoding='utf-8')
            with patch.object(tools,'IS_WINDOWS',False),patch.object(tools,'IS_MAC',False),\
                 patch.object(sys,'argv',['student_tools.py','--config',str(settings),'index','--status-file',str(status)]):
                with self.assertRaises(tools.StudentFacingError):tools.main()
            body=json.loads(status.read_text(encoding='utf-8'))
            self.assertEqual(body['status'],'index-failed');self.assertNotEqual(body['runId'],'old')

    def test_no_change_only_when_launchd_runs_this_definition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            stale=state['print']
            # The file on disk says 10 minutes, launchd still runs the 5-minute job.
            body,_=tools.launch_agent_plist(python,settings,10);(agents/'com.infobizagents.tooling.kb-index.plist').write_bytes(body)
            state['print']=stale
            self.assertTrue(tools.schedule_index_mac(settings,10,python=python,run=run,agents_dir=agents,uid=501)['changed'])
            self.assertIn(b'run interval = 600 seconds',state['print'])

    def test_failed_restore_is_reported_honestly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            def broken(args,**kw):
                if args[1]=='bootstrap':return subprocess.CompletedProcess(args,5)
                return run(args,**kw)
            with self.assertRaises(tools.StudentFacingError) as caught:
                tools.schedule_index_mac(settings,10,python=python,run=broken,agents_dir=agents,uid=501)
            self.assertIn('НЕ загружена',str(caught.exception))

    def test_unreadable_launchctl_state_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';agents.mkdir()
            weird=lambda args,**kw:subprocess.CompletedProcess(args,0,stdout=b'something else\n')
            with self.assertRaises(tools.StudentFacingError):tools.unschedule_index_mac(settings,run=weird,agents_dir=agents,uid=501)
            with self.assertRaises(tools.StudentFacingError):tools.schedule_index_mac(settings,5,python=python,run=weird,agents_dir=agents,uid=501)
            self.assertEqual(list(agents.iterdir()),[])

    def test_unschedule_checks_the_loaded_job_when_the_plist_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();python=root/'python';python.write_bytes(b'')
            new=root/'new/settings.json';new.parent.mkdir();new.write_text('{}')
            old=root/'old/settings.json';old.parent.mkdir();old.write_text('{}')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(new,5,python=python,run=run,agents_dir=agents,uid=501)
            (agents/'com.infobizagents.tooling.kb-index.plist').unlink()
            with self.assertRaises(tools.StudentFacingError):tools.unschedule_index_mac(old,run=run,agents_dir=agents,uid=501)
            self.assertIsNotNone(state['loaded'])
            self.assertTrue(tools.unschedule_index_mac(new,run=run,agents_dir=agents,uid=501)['changed'])

    def test_a_failed_change_gives_back_the_job_launchd_ran(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            plist=agents/'com.infobizagents.tooling.kb-index.plist'
            body,_=tools.launch_agent_plist(python,settings,10);plist.write_bytes(body)  # disk says 10, launchd runs 5
            calls={'n':0}
            def first_fails(args,**kw):
                if args[1]=='bootstrap':
                    calls['n']+=1
                    if calls['n']==1:return subprocess.CompletedProcess(args,5)
                return run(args,**kw)
            with self.assertRaises(tools.StudentFacingError) as caught:
                tools.schedule_index_mac(settings,15,python=python,run=first_fails,agents_dir=agents,uid=501)
            self.assertIn('прежнее состояние возвращено',str(caught.exception))
            self.assertIn(b'run interval = 300 seconds',state['print'])
            self.assertEqual(plistlib.loads(plist.read_bytes())['StartInterval'],300)

    def test_a_failing_launchctl_print_is_not_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';_,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            io_error=lambda args,**kw:subprocess.CompletedProcess(args,5,stdout=b'')
            with self.assertRaises(tools.StudentFacingError):tools.unschedule_index_mac(settings,run=io_error,agents_dir=agents,uid=501)
            self.assertTrue((agents/'com.infobizagents.tooling.kb-index.plist').is_file())

    def test_unschedule_keeps_a_plist_that_is_not_ours(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            agents=root/'LaunchAgents';state,run=self.launchctl_fake(agents)
            tools.schedule_index_mac(settings,5,python=python,run=run,agents_dir=agents,uid=501)
            plist=agents/'com.infobizagents.tooling.kb-index.plist'
            plist.write_bytes(plistlib.dumps({'Label':'com.infobizagents.tooling.kb-index','ProgramArguments':['/usr/bin/true']}))
            with self.assertRaises(tools.StudentFacingError):tools.unschedule_index_mac(settings,run=run,agents_dir=agents,uid=501)
            self.assertTrue(plist.is_file())

    def test_failed_launchctl_bootstrap_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();settings=root/'settings.json';settings.write_text('{}');python=root/'python';python.write_bytes(b'')
            def launchctl(args,**kw):return subprocess.CompletedProcess(args,113 if args[1]=='print' else 5)
            with self.assertRaises(tools.StudentFacingError) as caught:
                tools.schedule_index_mac(settings,5,python=python,run=launchctl,agents_dir=root/'LaunchAgents',uid=501)
            self.assertIn('launchctl',str(caught.exception))

    def test_schedule_index_dispatches_by_os(self):
        with patch.object(tools,'schedule_index_mac',return_value={'changed':True,'task':'mac'}) as mac:
            self.assertEqual(tools.schedule_index('s.json',5,windows=False,mac=True)['task'],'mac');mac.assert_called_once()
        with patch.object(tools,'unschedule_index_mac',return_value={'changed':False}) as mac:
            tools.unschedule_index('s.json',windows=False,mac=True);mac.assert_called_once()
        with self.assertRaises(tools.StudentFacingError):tools.schedule_index('s.json',5,windows=False,mac=False)
        with self.assertRaises(tools.StudentFacingError):tools.unschedule_index('s.json',windows=False,mac=False)


class MacBackendChoice(unittest.TestCase):
    inventory=load('inventory_mac','inventory.py')

    def test_backend_by_macos_version_architecture_and_homebrew(self):
        choose=self.inventory.mac_kb_backend;brew='/opt/homebrew/bin/brew'
        self.assertEqual(choose(15,True,brew),('postgres','macos15-homebrew'))
        self.assertEqual(choose(15,True,None),('file','no-homebrew'))
        self.assertEqual(choose(14,True,brew),('file','macos14-no-homebrew-bottles'))
        for major in (26,27,28):self.assertEqual(choose(major,True,brew)[0],'file')
        self.assertEqual(choose(13,True,brew),(None,'macos-older-than-14-unsupported'))
        self.assertEqual(choose(27,False,'/usr/local/bin/brew'),(None,'intel-mac-unsupported'))

    def test_macos_version_comes_from_sw_vers_not_the_compat_10_16(self):
        def sw_vers(args,**kw):
            self.assertEqual(kw['env'].get('SYSTEM_VERSION_COMPAT'),'0')
            return subprocess.CompletedProcess(args,0,stdout='27.0.1\n')
        self.assertEqual(self.inventory.mac_product_version(run=sw_vers),'27.0.1')
        with patch.object(self.inventory.platform,'mac_ver',return_value=('15.6','','')):
            self.assertEqual(self.inventory.mac_product_version(run=lambda *a,**k:(_ for _ in ()).throw(OSError())),'15.6')

    def test_hermes_interpreter_of_the_official_mac_installer_is_found(self):
        inv=self.inventory
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'hermes'
            env=root/'installs/670a/environments/2b8d/venv/bin';env.mkdir(parents=True);(env/'python').write_text('')
            bare=root/'installs/670a/pm-runtime/generations/c92f/bin';bare.mkdir(parents=True);(bare/'python').write_text('')
            asked=[]
            def probe(python,code,timeout=60):
                asked.append(Path(python));return 0 if Path(python)==env/'python' else 1
            with patch.object(inv,'probe',side_effect=probe):
                self.assertEqual(inv.hermes_python(root,windows=False),env/'python')
            self.assertNotIn(bare/'python',asked)  # only venv/bin/python is asked

    def test_uv_shipped_with_hermes_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();uv=home/'.hermes/tools/uv-0.13.5-darwin-arm64/uv';uv.parent.mkdir(parents=True)
            uv.write_text('#!/bin/sh\n');uv.chmod(0o755)
            self.assertEqual(self.inventory.hermes_uv(home/'.hermes'),[str(uv)])


class ForeignKnowledgeBases(unittest.TestCase):
    """A base an agent built on its own is found, read only, and never confused with ours."""
    inventory=load('inventory_foreign','inventory.py')

    def test_self_made_base_is_reported_without_contents(self):
        import sqlite3,yaml
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'.hermes/profiles/tech';(folder/'skills/farvater-kb').mkdir(parents=True)
            (folder/'skills/farvater-kb/SKILL.md').write_text('# Farvater\nПоиск через sqlite-vec по базе знаний.',encoding='utf-8')
            (folder/'skills/design/SKILL.md').parent.mkdir(parents=True)
            (folder/'skills/design/SKILL.md').write_text('# Design\nChroma, contrast and lightness of colours.',encoding='utf-8')
            (folder/'config.yaml').write_text(yaml.safe_dump({'mcp_servers':{
                'farvater':{'command':'python3','args':['farvater_mcp.py','--db','farvater.sqlite']},
                'telegram-reader':{'command':'reader'},'student-agent-kb':{'command':'ours'}}}),encoding='utf-8')
            base=home/'InfobizAgents/farvater/farvater.sqlite';base.parent.mkdir(parents=True)
            db=sqlite3.connect(base);db.execute('create table docs(id integer primary key,text text,embedding blob)')
            db.execute("insert into docs(text,embedding) values('СЕКРЕТНЫЙ ТЕКСТ УЧЕНИКА',x'00')");db.commit();db.close()
            plain=home/'InfobizAgents/notes.db';db=sqlite3.connect(plain);db.execute('create table notes(x text)');db.commit();db.close()
            ours=home/'InfobizAgents/tooling/kb/agents-student-kb.sqlite';ours.parent.mkdir(parents=True)
            (home/'InfobizAgents/tooling/student_tools.py').write_text('')
            db=sqlite3.connect(ours);db.execute('create table kb_meta(k)');db.execute('create table documents(embedding blob)');db.execute('create table chunks(x)');db.commit();db.close()
            found=self.inventory.foreign_knowledge_bases(home,[{'id':'tech','path':str(folder)}])
            kinds={(x['kind'],x.get('name') or Path(x.get('path','')).name) for x in found}
            self.assertEqual(kinds,{('mcp','farvater'),('skill','farvater-kb'),('sqlite','farvater.sqlite')})
            sqlite_entry=next(x for x in found if x['kind']=='sqlite')
            self.assertEqual(sqlite_entry['tables'],{'docs':1})
            self.assertNotIn('СЕКРЕТНЫЙ',json.dumps(found,ensure_ascii=False))
            self.assertTrue(next(x for x in found if x['kind']=='mcp')['enabled'])

    def test_wal_rows_are_counted_and_the_original_is_untouched(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/kb-own';folder.mkdir(parents=True);base=folder/'own.db'
            db=sqlite3.connect(base);db.execute('pragma journal_mode=wal');db.execute('pragma wal_autocheckpoint=0')
            db.execute('create table items(id integer primary key,body text,embedding blob)')
            db.execute("insert into items(body,embedding) values('x',x'00')");db.commit()
            before=sorted(p.name for p in folder.iterdir())
            self.assertIn('own.db-wal',before)  # the row lives only in the WAL
            found=self.inventory.foreign_knowledge_bases(home,[])
            self.assertEqual(sorted(p.name for p in folder.iterdir()),before)
            db.close()
            entry=next(x for x in found if x['kind']=='sqlite')
            self.assertTrue(entry['rowsKnown']);self.assertEqual(entry['tables'],{'items':1})

    def test_inventory_never_creates_files_next_to_a_base(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/rag';folder.mkdir(parents=True);base=folder/'rag.sqlite'
            db=sqlite3.connect(base);db.execute('pragma journal_mode=wal')
            db.execute('create table docs(id integer primary key,text text,vector blob)');db.commit();db.close()
            before=sorted(p.name for p in folder.iterdir())
            self.inventory.foreign_knowledge_bases(home,[])
            self.assertEqual(sorted(p.name for p in folder.iterdir()),before)

    def test_an_ordinary_table_with_vector_in_its_name_is_not_a_base(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/design';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'icons.db');db.execute('create table vector_icons(id integer primary key,svg text)');db.commit();db.close()
            self.assertEqual(self.inventory.foreign_knowledge_bases(home,[]),[])

    def test_scan_stops_at_its_budget_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();(home/'InfobizAgents/a/b').mkdir(parents=True)
            for i in range(30):(home/'InfobizAgents/a/b'/f'{i}.txt').write_text('x')
            found=self.inventory.foreign_knowledge_bases(home,[],budget=self.inventory.ScanBudget(files=5))
            self.assertEqual(found[-1]['kind'],'scan-incomplete')

    def test_venv_candidates_must_be_native_arm64(self):
        self.assertIn('platform.machine()=="arm64"',self.inventory.VENV_PYTHON_CHECK)

    def test_uv_python_dir_is_asked(self):
        out=subprocess.CompletedProcess([],0,stdout='/Users/x/custom/uv-pythons\n')
        self.assertEqual(self.inventory.uv_python_dir('/u/uv',run=lambda *a,**k:out),Path('/Users/x/custom/uv-pythons'))
        self.assertIsNone(self.inventory.uv_python_dir(None))

    def test_every_ordinary_table_is_counted_and_quoted_columns_are_found(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/x';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'a.db');db.execute('create table vectors_data(id integer primary key,embedding blob)')
            db.execute("insert into vectors_data(embedding) values(x'00')");db.commit();db.close()
            for i,col in enumerate(('[embedding]','`embedding`','"vector"')):
                db=sqlite3.connect(folder/f'q{i}.db');db.execute(f'create table t(id integer,{col} blob)');db.commit();db.close()
            found={Path(x['path']).name:x for x in self.inventory.foreign_knowledge_bases(home,[])}
            self.assertEqual(found['a.db']['tables'],{'vectors_data':1});self.assertTrue(found['a.db']['rowsKnown'])
            self.assertEqual({'q0.db','q1.db','q2.db'}<=set(found),True)

    def test_a_large_base_is_an_unchecked_candidate_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/rag';folder.mkdir(parents=True)
            (folder/'data.db').write_bytes(b'SQLite format 3\x00'+b'\x00'*100)
            with patch.object(self.inventory,'SQLITE_COPY_BYTES',10):
                found=self.inventory.foreign_knowledge_bases(home,[])
            self.assertEqual([(x['kind'],x['rowsKnown'],x['reason']) for x in found],[('sqlite-unchecked',False,'too-large')])

    def test_budget_is_checked_between_files_of_one_folder(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/many';folder.mkdir(parents=True)
            for i in range(5):
                db=sqlite3.connect(folder/f'{i}.db');db.execute('create table t(embedding blob)');db.commit();db.close()
            now={'t':0.0}
            def clock():now['t']+=10;return now['t']
            found=self.inventory.foreign_knowledge_bases(home,[],budget=self.inventory.ScanBudget(seconds=25,clock=clock))
            self.assertEqual(found[-1]['kind'],'scan-incomplete');self.assertLess(sum(x['kind']=='sqlite' for x in found),5)

    def test_hermes_own_databases_are_not_candidates(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'.hermes/profiles/tech';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'state.db');db.execute('create table m(embedding blob)');db.commit();db.close()
            self.assertEqual(self.inventory.foreign_knowledge_bases(home,[{'id':'tech','path':str(folder)}]),[])

    def test_hermes_uv_follows_the_hermes_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'custom-hermes';uv=root/'tools/uv-0.13.5-darwin-arm64/uv';uv.parent.mkdir(parents=True)
            uv.write_text('#!/bin/sh\n');uv.chmod(0o755)
            self.assertEqual(self.inventory.hermes_uv(root),[str(uv)])

    def test_an_ordinary_table_sharing_a_virtual_prefix_is_counted(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/x';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'kb.db');db.execute('create virtual table docs using fts5(body)')
            db.execute('create table docs_archive(id integer primary key,embedding blob)')
            db.execute("insert into docs_archive(embedding) values(x'00')");db.commit();db.close()
            entry=next(x for x in self.inventory.foreign_knowledge_bases(home,[]) if x['kind']=='sqlite')
            self.assertEqual(entry['tables'],{'docs':0,'docs_archive':1})

    def test_hermes_names_outside_hermes_are_still_checked(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/customer-kb';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'projects.db');db.execute('create table t(embedding blob)');db.commit();db.close()
            self.assertEqual([x['kind'] for x in self.inventory.foreign_knowledge_bases(home,[])],['sqlite'])

    def test_a_slow_copy_stops_at_the_scan_deadline(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp).resolve();folder=home/'InfobizAgents/slow';folder.mkdir(parents=True)
            db=sqlite3.connect(folder/'a.db');db.execute('create table t(embedding blob)');db.commit();db.close()
            now={'t':0.0}
            def clock():now['t']+=30;return now['t']
            budget=self.inventory.ScanBudget(seconds=45,clock=clock)
            found=self.inventory.foreign_knowledge_bases(home,[],budget=budget)
            self.assertEqual(found[-1]['kind'],'scan-incomplete')
            self.assertFalse(any(x.get('rowsKnown') for x in found))

    def test_copy_itself_obeys_the_deadline(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp).resolve()/'a.db';db=sqlite3.connect(base);db.execute('create table t(embedding blob)');db.commit();db.close()
            budget=self.inventory.ScanBudget(seconds=-1)
            self.assertEqual(self.inventory.sqlite_summary(base,budget=budget)['reason'],'time-limit');self.assertTrue(budget.exhausted)


if __name__=='__main__':unittest.main()
