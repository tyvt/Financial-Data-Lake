import unittest,tempfile,sys,csv,contextlib,io
from pathlib import Path
from unittest.mock import patch
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'分析产出'),str(ROOT/'scripts')]
import external_lake as ext
import update_selection_lake as runner
import ownership as gov

class SelectionLakeTests(unittest.TestCase):
 def test_whitelist_and_dependency_closure(self):
  policy,cfg=runner.load_and_validate()
  self.assertFalse(any(n.startswith(('futures_','option_')) for n in policy['allowed_datasets']))
  self.assertNotIn('valuation_metrics',policy['allowed_datasets'])
  self.assertNotIn('derive_industry_index',policy['allowed_steps'])
 def test_excluded_fetch_and_publication_blocked(self):
  from cnequity.orchestrator.registry import STEP_REGISTRY
  from cnequity.storage.revisions import RevisionStore
  policy,_=runner.load_and_validate();originals={k:v.fn for k,v in STEP_REGISTRY.items()};commit=RevisionStore.commit
  try:
   runner.install_guards(policy)
   with self.assertRaisesRegex(RuntimeError,'Excluded step'):STEP_REGISTRY['valuation_metrics'].fn(None,None,None,None)
   with self.assertRaisesRegex(RuntimeError,'publication blocked'):RevisionStore.commit(object(),'option_bars')
  finally:
   for k,fn in originals.items():STEP_REGISTRY[k].fn=fn
   RevisionStore.commit=commit
 def test_snapshots_deduplicate_and_preserve_reversions(self):
  with tempfile.TemporaryDirectory() as directory:
   lake=Path(directory)
   a={'SECUCODE':'000001.SZ','ACTUAL_HOLDER':'甲','SECURITY_NAME_ABBR':'测试','source':'test','source_published_at':None}
   p=ext.append_snapshot('actual_controller_disclosures',[a],lake=lake)
   self.assertEqual(p,ext.append_snapshot('actual_controller_disclosures',[a],lake=lake))
   ext.append_snapshot('actual_controller_disclosures',[{**a,'ACTUAL_HOLDER':'乙'}],lake=lake)
   ext.append_snapshot('actual_controller_disclosures',[a],lake=lake)
   self.assertEqual(len(list((lake/'derived/actual_controller_disclosures').glob('*.parquet'))),3)
   self.assertEqual(ext.load_latest('actual_controller_disclosures',lake=lake).iloc[0].ACTUAL_HOLDER,'甲')
 def test_failed_refresh_preserves_good_controller(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'cache.csv';errors=Path(directory)/'errors.csv'
   previous=dict(code='000001.SZ',name='测试',actual_holder='无实际控制人',owner_type='无实际控制人',governance_ok=True,reason='披露',source='test',fetched_at='2026-01-01')
   pd.DataFrame([previous]).to_csv(path,index=False,encoding='utf-8-sig')
   with patch.object(gov,'save_to_lake'),patch.object(gov,'load_whitelist',return_value={}),patch.object(gov,'reclassify',side_effect=lambda x:x):
    result=gov.fetch_many(['000001.SZ'],refresh=True,cache=str(path),exc_path=str(errors),retries=0,sleep=(0,0),verbose=False)
   self.assertEqual(result.iloc[0].actual_holder,'无实际控制人')
   self.assertEqual(result.iloc[0].fetched_at,'2026-01-01')
   self.assertTrue(errors.exists())
 def test_bank_missing_values_never_become_zero(self):
  with patch.object(ext,'append_snapshot') as save:
   with self.assertRaises(ValueError):ext.store_bank('000001.SZ',[{'REPORT_DATE':'2026-06-30','HXYJBCZL':None,'NONPERLOAN':None,'BLDKBBL':None}])
   save.assert_not_called()
 def test_cross_day_retry_preserves_date_and_skips_successful_stages(self):
  import os,contextlib
  from types import SimpleNamespace
  policy,cfg=runner.load_and_validate()
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);(root/'分析产出').mkdir();logs=root/'logs/selection_update';logs.mkdir(parents=True)
   (logs/'progress-2026-01-01.json').write_text('{"date":"2026-01-01","stages":{"market":{"status":"failed","run_id":"retry-me"}}}',encoding='utf-8')
   cwd=os.getcwd()
   try:
    with patch.object(runner,'ROOT',root),patch.object(runner,'load_and_validate',return_value=(policy,cfg)),patch.object(runner,'configure_logging',return_value=logs),patch.object(runner,'install_guards'),patch('cnequity.file_lock.exclusive_lock',side_effect=lambda *a,**k:contextlib.nullcontext()),patch('cnequity.orchestrator.engine.JobEngine') as cls,patch('update_external_lake.seed_legacy_cache'),patch('update_external_lake.update_external',return_value={'status':'success'}) as update,patch.object(ext,'register_views'):
     engine=cls.return_value
     engine.run_job.side_effect=[{'status':'failed','run_id':'retry-me'},{'status':'success','run_id':'fundamentals'},{'status':'success','run_id':'events'},{'status':'success','run_id':'retry-me'}]
     args=SimpleNamespace(check=False,external_only=False,force_external=False)
     self.assertEqual(runner.run(args),1)
     self.assertEqual(runner.run(args),0)
     self.assertEqual(engine.run_job.call_count,4)
     self.assertTrue(engine.run_job.call_args.kwargs['retry_failed_only'])
     from datetime import date
     self.assertTrue(all(call.kwargs['trade_date']==date(2026,1,1) for call in engine.run_job.call_args_list))
     update.assert_called_once()
   finally:os.chdir(cwd)
 def test_legacy_action_migration_keeps_cash_and_reference(self):
  from datetime import datetime,timezone
  import polars as pl
  from migrate_legacy_actions import normalize_legacy
  frame=pl.DataFrame({'action_type':['non_cash_event','cash_dividend'],'source':['derived_adj_factor','tdx_protocol'],'reference_price':[5.25,None],'split_factor':[1.5,1.0],'cash_dividend':[0.0,0.2],'fetched_at':[None,datetime.now(timezone.utc)]})
  updated,evidence=normalize_legacy(frame)
  self.assertEqual(evidence.height,1)
  self.assertEqual(updated['action_type'][0],'reorg_transfer')
  self.assertEqual(updated['reference_price'][0],5.25)
  self.assertEqual(updated['split_factor'][0],1)
  self.assertEqual(updated['cash_dividend'].to_list(),frame['cash_dividend'].to_list())
  self.assertIsNotNone(updated['fetched_at'][0])
 def test_external_rotation_prioritizes_holdings(self):
  from datetime import datetime,timezone
  from update_external_lake import due_targets
  symbols={'held','other','fresh'};now=datetime.now(timezone.utc)
  actual=due_targets(symbols,{'held'},{'fresh':now.isoformat()},7,1,now)
  self.assertEqual(actual,['held'])


class SelectionAuditDriftTests(unittest.TestCase):
    """A new public-source mismatch must surface; a known one must not re-alarm."""
    def test_run_audit_flags_new_issue_only(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('alp',ROOT/'scripts/audit_lake_public.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        class _Suf(unittest.TestCase):
            def assertEqual(self,a,b,msg=None):pass
            def assertTrue(self,x,msg=None):pass
            def assertFalse(self,x,msg=None):pass
            def assertRaises(self,*a,**k):return contextlib.nullcontext()
            def assertRaisesRegex(self,*a,**k):return contextlib.nullcontext()
            def assertIsNone(self,*a,**k):pass
            def assertIsNotNone(self,*a,**k):pass
            def fail(self,msg=''):raise AssertionError(msg)
        import contextlib,tempfile,json,io
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'分析产出';logs=Path(directory)/'logs/selection_update';out.mkdir();logs.mkdir(parents=True)
            summary={'audited_at':'2026-10-24T00:00:00','checks':3,'match':1,'mismatch':1,'inconclusive':1}
            (out/'数据湖抽检_公开源比对.json').write_text(json.dumps(summary),encoding='utf-8')
            rows=[{'category':'财务','symbol':'600000.SH','field':'roe','lake':'1','public':'1','match':'True'},
                  {'category':'行情快照','symbol':'600001.SH','field':'收盘价','lake':'9','public':'8','match':'False'}]
            with (out/'数据湖抽检_公开源比对明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            real_out,real_root,real_main=mod.OUT,mod.ROOT,mod.main
            try:
                mod.OUT=out;mod.ROOT=Path(directory)
                mod.main=lambda: None   # fixtures replace the collection step
                first=mod.run_audit()
                self.assertEqual(first['status'],'warning','新差异必须报warning')
                self.assertEqual(len(first['new_issues']),1)
                second=mod.run_audit()
                self.assertEqual(second['status'],'success','已记录的已知差异不重复报警')
                self.assertEqual(second['known_issues'],1)
                self.assertFalse(second['new_issues'])
            finally:
                mod.OUT,mod.ROOT,mod.main=real_out,real_root,real_main

    def test_sufficiency_rejects_ffilled_constant_series(self):
        sys.path.insert(0,str(ROOT/'分析产出'))
        from data_sufficiency import Sufficiency,MIN_RETURN_OBS,MIN_BAR_DAYS
        class _Con:
            def execute(self,sql,params=None):
                # window has far fewer bars than MIN_BAR_DAYS though ffill fills them
                class _R:
                    def df(self_inner):
                        import pandas as pd
                        return pd.DataFrame({'symbol':['X.SH'],'n':[MIN_BAR_DAYS-5]})
                return _R()
        suf=Sufficiency(_Con(),'2026-10-23')
        import pandas as pd,numpy as np
        idx=pd.date_range('2026-01-01',periods=260,freq='D')
        returns=pd.DataFrame(index=idx,data={'X.SH':np.linspace(0.0001,0.0001,260)})
        ok=suf.check_returns(['X.SH'],returns)
        self.assertFalse(bool(ok.iloc[0]),'行情天数不足必须判不充足，即使收益被ffill填满')
        self.assertIn('需≥',suf.reasons_for(['X.SH'])['X.SH'])

    def test_sufficiency_accepts_full_history(self):
        sys.path.insert(0,str(ROOT/'分析产出'))
        from data_sufficiency import Sufficiency,MIN_BAR_DAYS,MIN_AMT_DAYS
        class _Con:
            def __init__(self,bars,amt):self.bars,self.amt=bars,amt
            def execute(self,sql,params=None):
                bars,amt=self.bars,self.amt
                class _R:
                    def df(self_inner):
                        import pandas as pd
                        n=bars if 'count(*) n' in sql and 'rk' not in sql else amt
                        return pd.DataFrame({'symbol':['X.SH'],'n':[n]})
                return _R()
        con=_Con(MIN_BAR_DAYS,MIN_AMT_DAYS)
        suf=Sufficiency(con,'2026-10-23')
        import pandas as pd
        # check_amount primes the amount cache; seed the bar cache through the
        # same connection so reasons_for sees both.
        suf.check_amount(['X.SH'])
        suf._bar_counts=pd.Series({'X.SH':MIN_BAR_DAYS})
        ok=suf.check_returns(['X.SH'],pd.DataFrame({'X.SH':[0.01]*260}))
        self.assertTrue(bool(ok.iloc[0]))
        self.assertEqual(suf.reasons_for(['X.SH']),{})

    def test_legacy_action_migration_keeps_cash_and_reference(self):
        from datetime import datetime,timezone
        import polars as pl
        from migrate_legacy_actions import normalize_legacy
        frame=pl.DataFrame({'action_type':['non_cash_event','cash_dividend'],'source':['derived_adj_factor','tdx_protocol'],'reference_price':[5.25,None],'split_factor':[1.5,1.0],'cash_dividend':[0.0,0.2],'fetched_at':[None,datetime.now(timezone.utc)]})
        updated,evidence=normalize_legacy(frame)
        self.assertEqual(evidence.height,1)
        self.assertEqual(updated['action_type'][0],'reorg_transfer')
        self.assertEqual(updated['reference_price'][0],5.25)
        self.assertEqual(updated['split_factor'][0],1)
        self.assertEqual(updated['cash_dividend'].to_list(),frame['cash_dividend'].to_list())
        self.assertIsNotNone(updated['fetched_at'][0])


if __name__=='__main__':unittest.main()
