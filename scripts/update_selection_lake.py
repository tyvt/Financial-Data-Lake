"""Fail-closed scheduled entry point for the selection data lake."""
from __future__ import annotations
import argparse,json,os,sys,logging,traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'分析产出'))
from external_lake import atomic_json,LAKE
POLICY_PATH=ROOT/'configs/selection_lake_policy.json'


def load_and_validate():
 import cnequity.steps
 from cnequity.config import load_config
 from cnequity.config.loader import validate_config
 from cnequity.orchestrator.registry import STEP_REGISTRY
 policy=json.loads(POLICY_PATH.read_text(encoding='utf-8'));cfg=load_config(ROOT/'configs/cnequity.toml')
 errors=validate_config(cfg)
 if errors:raise ValueError(errors)
 allowed=set(policy['allowed_steps'])
 planned={step for g in list(cfg.schedule_groups.values())+list(cfg.events_groups.values()) for step in g.steps}
 planned.update(step for wave in cfg.daily_waves for step in wave.steps)
 if planned-allowed:raise ValueError('Config contains excluded steps: '+str(sorted(planned-allowed)))
 def visit(step,seen):
  if step not in allowed:raise ValueError('Forbidden dependency: '+step)
  if step in seen:return
  seen.add(step)
  for dep in STEP_REGISTRY[step].depends_on:visit(dep,seen)
 for step in allowed:visit(step,set())
 return policy,cfg


def install_guards(policy):
 from cnequity.orchestrator.registry import STEP_REGISTRY
 from cnequity.storage.revisions import RevisionStore
 allowed_steps=set(policy['allowed_steps']);allowed_data=set(policy['allowed_datasets'])
 for name,entry in STEP_REGISTRY.items():
  if name not in allowed_steps:
   def blocked(*args,_name=name,**kwargs):raise RuntimeError('Excluded step blocked before fetching: '+_name)
   entry.fn=blocked
 original=RevisionStore.commit
 def checked_commit(self,dataset,**kwargs):
  if dataset not in allowed_data:raise RuntimeError('Excluded dataset publication blocked: '+dataset)
  return original(self,dataset,**kwargs)
 RevisionStore.commit=checked_commit


def configure_logging():
 directory=ROOT/'logs/selection_update';directory.mkdir(parents=True,exist_ok=True)
 handler=RotatingFileHandler(directory/'update.log',maxBytes=5*1024**2,backupCount=5,encoding='utf-8')
 handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
 logger=logging.getLogger();logger.setLevel(logging.INFO);logger.addHandler(handler)
 # Library prints and warnings are captured as well as logging records.
 class Stream:
  def write(self,text):
   if text.strip():logging.getLogger('console').info(text.rstrip())
  def flush(self):pass
 sys.stdout=Stream();sys.stderr=Stream()
 return directory


def run(args):
 from cnequity.file_lock import exclusive_lock
 from cnequity.config import WaveConfig
 from cnequity.orchestrator.engine import JobEngine
 from update_external_lake import update_external,seed_legacy_cache
 from external_lake import register_views
 policy,cfg=load_and_validate()
 from external_lake import configure_proxy
 configure_proxy()
 if args.check:
  print(json.dumps({'status':'valid','allowed_datasets':policy['allowed_datasets'],'allowed_steps':policy['allowed_steps']},ensure_ascii=False,indent=2));return 0
 from datetime import date
 directory=configure_logging();status_file=directory/'last_run.json';today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
 requested=getattr(args,'retry_date',None)
 day=requested or today
 if requested:date.fromisoformat(requested)
 if not requested and not args.external_only:
  pending=[]
  for file in directory.glob('progress-*.json'):
   candidate=json.loads(file.read_text(encoding='utf-8'))
   if candidate.get('date','') < today and any(stage.get('status') in ('failed','blocked') for stage in candidate.get('stages',{}).values()):pending.append(candidate['date'])
  if pending:day=min(pending)
 processing_date=date.fromisoformat(day)
 state_path=directory/f'progress-{day}.json'
 os.chdir(ROOT/'分析产出')
 with exclusive_lock(LAKE/'meta/locks/selection_update.lock',blocking=False):
  state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'date':day,'stages':{}}
  overall={'started_at':datetime.now().astimezone().isoformat(),'processing_date':day,'status':'running','mode':'external-only' if args.external_only else 'retry-previous-date' if day!=today else 'daily','stages':state['stages']}
  atomic_json(status_file,overall)
  install_guards(policy)
  engine=JobEngine(cfg)
  stages=[('market','daily:selection_market',[
   WaveConfig(name='reference',parallel=False,steps=['instruments','trading_calendar','trading_status']),
   WaveConfig(name='actions_bars',parallel=False,steps=['corporate_actions','daily_bars']),
   WaveConfig(name='publish',parallel=False,steps=['compact','derive_adj_factors'])]),
   ('fundamentals','daily:selection_fundamentals',[WaveConfig(name='fundamentals',parallel=False,steps=['financial_statement_items','industry_members','share_structure','earnings_disclosure_schedule']),WaveConfig(name='publish',parallel=False,steps=['compact'])]),
   ('events','events:selection',[WaveConfig(name='disclosures',parallel=False,steps=['announcement_index']),WaveConfig(name='publish_disclosures',parallel=False,steps=['compact']),WaveConfig(name='regulatory_news',parallel=False,steps=['regulatory_events','news_headlines']),WaveConfig(name='publish_events',parallel=False,steps=['compact'])])]
  for name,job,waves in ([] if args.external_only else stages):
   previous=state['stages'].get(name,{})
   if previous.get('status') in ('success','skipped_non_trading_day'):continue
   try:
    if previous.get('run_id'):
     result=engine.run_job(job,trade_date=processing_date,run_id=previous['run_id'],retry_failed_only=True)
    else:result=engine.run_job(job,trade_date=processing_date,waves=waves)
    state['stages'][name]=result
   except Exception as exc:
    logging.exception('Stage failed: %s',name);state['stages'][name]={'status':'failed','error':str(exc)}
   atomic_json(state_path,state);overall['stages']=state['stages'];atomic_json(status_file,overall)
  if args.force_external or state['stages'].get('external',{}).get('status')!='success':
   try:
    seed_marker=directory/'legacy_seeded.json'
    if not seed_marker.exists():seed_legacy_cache();atomic_json(seed_marker,{'seeded_at':datetime.now().astimezone().isoformat()})
    state['stages']['external']=update_external(policy)
   except Exception as exc:logging.exception('External update failed');state['stages']['external']={'status':'failed','error':str(exc)}
   atomic_json(state_path,state)
  register_views()
  maintenance={'status':'skipped'}
  if not args.external_only and not any(v.get('status') in ('failed','blocked','pending') for v in state['stages'].values()):
   try:
    from prune_selection_history import prune_history
    maintenance=prune_history(root=ROOT,allowed_datasets=set(policy['allowed_datasets']),keep=policy.get('retained_generations',2),apply=True)
   except Exception as exc:
    logging.exception('Retention skipped safely');maintenance={'status':'warning','error':str(exc)}
  state['maintenance']=maintenance;atomic_json(state_path,state)
  audit={'status':'skipped'}
  if not args.external_only:
   try:
    from audit_lake_public import run_audit
    audit=run_audit()
   except Exception as exc:
    logging.exception('Public audit skipped safely');audit={'status':'warning','error':str(exc)}
  state['audit']=audit;atomic_json(state_path,state)
  failed={name:value for name,value in state['stages'].items() if value.get('status') not in ('success','skipped_non_trading_day')}
  if maintenance.get('status')=='warning':failed['maintenance']=maintenance
  if audit.get('status')=='warning':failed['audit']=audit
  overall.update(status='warning' if failed else 'success',finished_at=datetime.now().astimezone().isoformat(),stages=state['stages'],maintenance=maintenance,audit=audit)
  atomic_json(status_file,overall)
  print(json.dumps(overall,ensure_ascii=False,default=str))
  return 1 if failed else 0


if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');parser.add_argument('--external-only',action='store_true');parser.add_argument('--force-external',action='store_true');parser.add_argument('--retry-date',help='Resume a saved YYYY-MM-DD plan, including on a later non-trading day');args=parser.parse_args()
 try:raise SystemExit(run(args))
 except Exception as exc:
  from cnequity.file_lock import LockUnavailable
  if isinstance(exc,LockUnavailable):
   logging.getLogger(__name__).info('Another updater owns the lake; skipped without changing its status')
   raise SystemExit(0)
  status_path=ROOT/'logs/selection_update/last_run.json'
  previous=json.loads(status_path.read_text(encoding='utf-8')) if status_path.exists() else {}
  atomic_json(status_path,{**previous,'status':'failed','error':str(exc),'finished_at':datetime.now().astimezone().isoformat()})
  traceback.print_exc();raise SystemExit(1)
