"""Daily low-frequency external updates, scoped to bank universe and screened controllers."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,time
import pandas as pd
import requests
import duckdb
from external_lake import LAKE,ROOT,atomic_json,load_latest,store_bank,append_snapshot,snapshot_governance,register_views

URL='https://datacenter.eastmoney.com/securities/api/data/v1/get'
HEADERS={'User-Agent':'Mozilla/5.0','Referer':'https://emweb.securities.eastmoney.com/'}

def request_report(report,symbol,columns):
 from external_lake import configure_proxy
 configure_proxy()
 response=requests.get(URL,params={'reportName':report,'columns':columns,'filter':f'(SECUCODE="{symbol}")','pageSize':12,'pageNumber':1,'sortColumns':'REPORT_DATE' if 'REPORT_DATE' in columns else 'SECUCODE','sortTypes':-1,'source':'HSF10','client':'PC'},headers=HEADERS,timeout=(8,20))
 response.raise_for_status();body=response.json()
 if not body.get('success'):raise RuntimeError(f'{report}: {body.get("message")}')
 rows=(body.get('result') or {}).get('data') or []
 if not rows:raise LookupError(f'{report} {symbol}: no rows')
 return rows

def target_symbols():
 out=ROOT/'分析产出';held=set();watched=set()
 for file,col in [('打新底仓_最终8只组合_v2.csv','sym'),('沪市替补席.csv','symbol'),('深市替补席.csv','symbol')]:
  path=out/file
  if not path.exists():continue
  df=pd.read_csv(path,encoding='utf-8-sig',dtype=str)
  if col not in df:
   col=next((k for k in ['symbol','sym','code'] if k in df),None)
  if col:
   values={v for v in df[col].dropna().astype(str) if v.endswith(('.SH','.SZ'))}
   watched.update(values)
   if file.startswith('打新底仓'):held.update(values)
 watched.update(['000001.SZ','002966.SZ'])
 con=duckdb.connect(str(LAKE/'duckdb/cnequity.duckdb'),read_only=True)
 try:banks={r[0] for r in con.execute("""SELECT i.symbol FROM instruments i
 LEFT JOIN (SELECT symbol,industry_name FROM industry_members WHERE classification_system='eastmoney'
 QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY as_of_date DESC)=1) m USING(symbol)
 WHERE i.exchange IN ('SH','SZ') AND i.asset_type='stock' AND i.delist_date IS NULL
 AND (i.name LIKE '%银行%' OR i.name LIKE '%农商行%' OR m.industry_name LIKE '%银行%')""").fetchall()}
 finally:con.close()
 controllers=load_latest('actual_controller_disclosures');cached=set(controllers.SECUCODE) if len(controllers) else set()
 path=out/'实控人核查.csv'
 if path.exists():cached.update(pd.read_csv(path,encoding='utf-8-sig',dtype=str).code.dropna())
 return banks,held,watched|cached

def due_targets(symbols,priority,checked,refresh_days,limit,now):
 def age(s):
  stamp=checked.get(s)
  if not stamp:return float('inf')
  try:return (now-datetime.fromisoformat(stamp)).total_seconds()/86400
  except ValueError:return float('inf')
 due=[s for s in symbols if age(s)>=(1 if s in priority else refresh_days)]
 return sorted(due,key=lambda s:(s not in priority,-age(s),s))[:limit]

def seed_legacy_cache():
 import ownership as gov
 cached=gov.load_cached(str(ROOT/'分析产出'/gov.CACHE))
 if not len(cached):return
 for row in cached.to_dict('records'):
  holder=str(row.get('actual_holder') or '').strip()
  if holder and holder.lower()!='nan':append_snapshot('actual_controller_disclosures',[{'SECUCODE':row['code'],'SECURITY_NAME_ABBR':row['name'],'ACTUAL_HOLDER':holder,'source':'legacy-ownership-cache:first-import','source_published_at':None}])
 snapshot_governance(cached)

def update_external(policy):
 import ownership as gov
 status_file=ROOT/'logs/selection_update/external_status.json'
 status=json.loads(status_file.read_text(encoding='utf-8')) if status_file.exists() else {'bank_checked':{},'controller_checked':{}}
 now=datetime.now(timezone.utc);stamp=now.isoformat();result={'started_at':stamp,'banks_updated':[],'controllers_updated':[],'errors':[]}
 banks,held,controllers=target_symbols();priority=held|{'000001.SZ','002966.SZ'}
 bank_due=due_targets(banks,priority,status['bank_checked'],policy['external_refresh_days'],policy['external_max_bank_requests'],now)
 ctrl_due=due_targets(controllers,held,status['controller_checked'],policy['external_refresh_days'],policy['external_max_controller_requests'],now)
 cache=gov.load_cached(str(ROOT/'分析产出'/gov.CACHE));cache=cache.set_index('code',drop=False) if len(cache) else pd.DataFrame(columns=gov.COLS).set_index('code',drop=False)
 wl=gov.load_whitelist(str(ROOT/'分析产出'/gov.WL));failures=0
 for category,symbols in [('bank',bank_due),('controller',ctrl_due)]:
  for symbol in symbols:
   if failures>=policy['external_consecutive_failure_limit']:
    result['circuit_open']=True;break
   try:
    if category=='bank':
     rows=request_report('RPT_F10_FINANCE_MAINFINADATA',symbol,'SECUCODE,REPORT_DATE,REPORT_DATE_NAME,HXYJBCZL,NONPERLOAN,BLDKBBL');store_bank(symbol,rows)
     result['banks_updated'].append(symbol)
    else:
     rows=request_report('RPT_F10_BASIC_ORGINFO',symbol,'SECUCODE,SECURITY_NAME_ABBR,ACTUAL_HOLDER');row=rows[0];holder=str(row.get('ACTUAL_HOLDER') or '').strip()
     if not holder:raise LookupError('ACTUAL_HOLDER missing; previous data retained')
     append_snapshot('actual_controller_disclosures',[{'SECUCODE':symbol,'SECURITY_NAME_ABBR':str(row.get('SECURITY_NAME_ABBR') or ''),'ACTUAL_HOLDER':holder,'source':'eastmoney:RPT_F10_BASIC_ORGINFO','source_published_at':None}])
     typ,ok,reason=gov.classify_owner(holder,wl)
     cache.loc[symbol]=dict(code=symbol,name=str(row.get('SECURITY_NAME_ABBR') or ''),actual_holder=holder,owner_type=typ,governance_ok=ok,reason=reason,source='eastmoney:F10',fetched_at=now.date().isoformat())
     result['controllers_updated'].append(symbol)
    status[category+'_checked'][symbol]=stamp;failures=0
    print('EXTERNAL_OK',category,symbol,flush=True)
   except Exception as exc:
    failures+=1;result['errors'].append({'category':category,'symbol':symbol,'error':str(exc)});print('EXTERNAL_ERROR',category,symbol,type(exc).__name__,str(exc),flush=True)
   atomic_json(status_file,status)
   time.sleep(policy['external_request_interval_seconds'])
 if len(cache):
  current=gov.reclassify(cache.reset_index(drop=True));gov.save_to_lake(current)
  target=ROOT/'分析产出'/gov.CACHE;tmp=target.with_suffix('.update.tmp');current.to_csv(tmp,index=False,encoding='utf-8-sig');tmp.replace(target)
 register_views();result['status']='warning' if result['errors'] or result.get('circuit_open') else 'success';result['finished_at']=datetime.now(timezone.utc).isoformat();status['last_result']=result;atomic_json(status_file,status)
 return result
