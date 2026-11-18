"""Integration checks in a disposable localhost PostgreSQL instance, never the hosted project."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PG = Path(os.environ.get('POSTGRES_BIN', 'C:/Program Files/PostgreSQL/18/bin'))


@unittest.skipUnless((PG / 'initdb.exe').exists(), 'Set POSTGRES_BIN to run PostgreSQL integration tests')
class SchemaTest(unittest.TestCase):
    @classmethod
    def command(cls, args, sql=None):
        # Background PostgreSQL children inherit Windows pipes; capture into a file instead.
        with tempfile.TemporaryFile(mode='w+t', encoding='utf-8', dir=cls.work) as output:
            options = {'input': sql} if sql is not None else {'stdin': subprocess.DEVNULL}
            result = subprocess.run([str(x) for x in args], encoding='utf-8', stdout=output,
                                    stderr=subprocess.STDOUT, timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW, **options)
            output.seek(0)
            text = output.read()
            if result.returncode:
                raise RuntimeError(text[-4000:])
            return text.strip()

    @classmethod
    def query(cls, sql):
        return cls.command(cls.psql, sql)

    @classmethod
    def setUpClass(cls):
        cls.temp_root = (ROOT/'supabase'/'.temp').resolve()
        cls.temp_root.mkdir(parents=True,exist_ok=True)
        assert cls.temp_root.is_relative_to(ROOT)
        cls.work = Path(tempfile.mkdtemp(prefix='salesquest-schema-test-',dir=cls.temp_root)).resolve()
        assert cls.work.parent == cls.temp_root
        cls.data = cls.work / 'data'
        cls.addClassCleanup(cls.cleanup_server)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        cls.command([PG/'initdb.exe','-D',cls.data,'-U','postgres','-A','trust','--no-locale','--encoding=UTF8'])
        cls.command([PG/'pg_ctl.exe','-D',cls.data,'-l',cls.work/'server.log','-o',f'-h 127.0.0.1 -p {port}','-w','start'])
        cls.psql = [PG/'psql.exe','-w','-h','127.0.0.1','-p',port,'-U','postgres','-d','postgres','-v','ON_ERROR_STOP=1','-Atq']
        cls.query('''create role anon; create role authenticated; create role service_role bypassrls;
            grant usage on schema public to anon,authenticated,service_role;
            alter default privileges in schema public grant all on tables to anon,authenticated,service_role;
            alter default privileges in schema public grant all on sequences to anon,authenticated,service_role;
            create schema auth; create table auth.users(id uuid primary key);''')
        for migration in sorted((ROOT/'supabase'/'migrations').glob('*.sql')):
            cls.query('begin;\n'+migration.read_text()+'\ncommit;')

    @classmethod
    def cleanup_server(cls):
        if (cls.data/'postmaster.pid').exists():
            cls.command([PG/'pg_ctl.exe','-D',cls.data,'-m','fast','-w','stop'])
        assert cls.work.parent == cls.temp_root and cls.work.name.startswith('salesquest-schema-test-')
        shutil.rmtree(cls.work)

    def setUp(self):
        self.state = {'leads':[{'id':'lead-a','status':'new'},{'id':'lead-b','status':'won'}],
                      'templates':[{'id':'tpl-a','channel':'email'}],
                      'sequences':[{'id':'seq-a','active':True,'steps':[{'day':0,'tplId':'tpl-a'}]}]}
        self.query("truncate public.salesquest_outbox,public.salesquest_enroll,public.salesquest_kv restart identity; insert into public.salesquest_kv values('state_rev','0'),('ev','0');")
        self.query("set role service_role; select public.salesquest_import_state('"+json.dumps(self.state)+"');")

    def test_browser_roles_cannot_read_data_or_execute_privileged_functions(self):
        result = self.query("""select count(*) from pg_class where oid in ('public.salesquest_members'::regclass,'public.salesquest_kv'::regclass,'public.salesquest_outbox'::regclass,'public.salesquest_enroll'::regclass) and relrowsecurity and not has_table_privilege('anon',oid,'select') and not has_table_privilege('authenticated',oid,'select');
            select count(*) from pg_proc where proname like 'salesquest_%' and has_function_privilege('anon',oid,'execute');""")
        self.assertEqual(result.splitlines(), ['4','0'])

    def test_import_retries_are_safe_and_conflicting_saves_do_not_overwrite(self):
        self.query("""do $$ declare r jsonb; begin
            begin perform public.salesquest_import_state('{"leads":[]}'); raise exception 'Import overwrote cloud data'; exception when unique_violation then null; end;
            r:=public.salesquest_save_state(0,'{"leads":[]}'); assert r->>'error'='conflict';
            begin perform public.salesquest_save_state(1,'{}'); raise exception 'Invalid state accepted'; exception when invalid_parameter_value then null; end;
            end $$;""")
        result = json.loads(self.query("select public.salesquest_import_state('"+json.dumps(self.state)+"');"))
        self.assertTrue(result['already_imported'])
        def save(name):
            return json.loads(self.query("set role service_role; select public.salesquest_save_state(1,'{\"leads\":[{\"id\":\""+name+"\"}]}');"))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(save, ('writer-a','writer-b')))
        self.assertEqual(sum('error' not in result for result in results),1)
        self.assertEqual(sum(result.get('error')=='conflict' for result in results),1)

    def test_sequences_skip_closed_leads_do_not_duplicate_and_cancel_queued_messages(self):
        result = json.loads(self.query("select public.salesquest_enroll_leads('seq-a',array['lead-a','lead-b']);"))
        self.assertEqual(result,{'enrolled':1,'skipped':1})
        self.assertEqual(json.loads(self.query("select public.salesquest_enroll_leads('seq-a',array['lead-a']);"))['skipped'],1)
        self.assertEqual(self.query('select public.salesquest_run_sequences();'),'1')
        self.assertEqual(self.query('select public.salesquest_run_sequences();'),'0')
        self.query('select public.salesquest_stop_enrollments(array[1]::bigint[]);')
        self.assertEqual(self.query('select status from public.salesquest_outbox;'),'cancelled')
        self.assertEqual(self.query("select v from public.salesquest_kv where k='ev';"),'1')

    def test_workers_enforce_gap_and_daily_cap_even_when_claimed_concurrently(self):
        self.query("insert into public.salesquest_outbox(created,due,lead_id,channel,tpl_id,status,updated) select extract(epoch from now()),0,'lead-a','email','tpl-a','queued',0 from generate_series(1,2);")
        cfg = json.dumps({'limits':{'days':list(range(7)),'windowStart':'00:00','windowEnd':'24:00','emailPerDay':1,'gapSec':20}})
        sql = "set role service_role; select public.salesquest_claim_message('email','"+cfg+"');"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.query(sql), range(2)))
        self.assertEqual(sum(bool(result) for result in results),1)
        self.query("update public.salesquest_outbox set status='sent',sent_at=extract(epoch from now()),updated=0 where status='sending';")
        self.assertEqual(self.query(sql),'')

    def test_event_pagination_and_queue_counts_do_not_stop_at_rest_row_limits(self):
        self.query("insert into public.salesquest_outbox(created,due,lead_id,channel,tpl_id,status,updated) select 0,0,'lead-a','email','tpl-a','queued',0 from generate_series(1,1201); update public.salesquest_outbox set status='cancelled';")
        cursor = 0
        sizes = []
        for _ in range(3):
            result = json.loads(self.query(f'select public.salesquest_poll({cursor});'))
            sizes.append(len(result['events']))
            cursor = result['ev']
        self.assertEqual(sizes,[500,500,201])
        self.assertEqual(cursor,1201)
        self.query("insert into public.salesquest_outbox(created,due,lead_id,channel,tpl_id,status,updated) select 0,0,'lead-a','email','tpl-a','queued',0 from generate_series(1,1201);")
        self.assertEqual(json.loads(self.query('select public.salesquest_poll(1201);'))['queued']['email'],1201)

    def test_empty_stop_list_preserves_closed_lead_default(self):
        self.state['sequences'][0]['stopOn']=[]
        self.query("select public.salesquest_save_state(1,'"+json.dumps(self.state)+"');")
        result=json.loads(self.query("select public.salesquest_enroll_leads('seq-a',array['lead-b']);"))
        self.assertEqual(result,{'enrolled':0,'skipped':1})

    def test_user_retry_cannot_reset_the_sending_gap(self):
        self.query("insert into public.salesquest_outbox(created,due,lead_id,channel,tpl_id,status,updated) select 0,0,'lead-a','email','tpl-a','queued',0 from generate_series(1,2);")
        cfg=json.dumps({'limits':{'days':list(range(7)),'windowStart':'00:00','windowEnd':'24:00','emailPerDay':10,'gapSec':20}})
        sql="select public.salesquest_claim_message('email','"+cfg+"');"
        self.assertTrue(self.query(sql))
        self.query("update public.salesquest_outbox set status='failed' where status='sending'; update public.salesquest_outbox set status='queued' where status='failed';")
        self.assertEqual(self.query(sql),'')

    def test_reminder_is_claimed_only_once(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.query('select public.salesquest_claim_reminder();'),range(2)))
        self.assertCountEqual(results,['t','f'])


if __name__ == '__main__':
    unittest.main()
