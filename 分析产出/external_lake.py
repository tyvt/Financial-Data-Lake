"""Selection-only external facts. Append changed snapshots; never replace good data on HTTP errors."""
from __future__ import annotations
import hashlib,json,os,uuid
from pathlib import Path
from datetime import datetime,timezone
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parent.parent
LAKE=ROOT/'data/cnequity'
SCHEMAS={
 'bank_risk_metrics':pa.schema([('SECUCODE',pa.string()),('REPORT_DATE',pa.string()),('REPORT_DATE_NAME',pa.string()),('HXYJBCZL',pa.float64()),('NONPERLOAN',pa.float64()),('BLDKBBL',pa.float64()),('source',pa.string()),('source_published_at',pa.string()),('observed_at',pa.string()),('payload_hash',pa.string())]),
 'actual_controller_disclosures':pa.schema([('SECUCODE',pa.string()),('SECURITY_NAME_ABBR',pa.string()),('ACTUAL_HOLDER',pa.string()),('source',pa.string()),('source_published_at',pa.string()),('observed_at',pa.string()),('payload_hash',pa.string())]),
 'governance_history':pa.schema([('code',pa.string()),('name',pa.string()),('actual_holder',pa.string()),('owner_type',pa.string()),('governance_ok',pa.bool_()),('reason',pa.string()),('source',pa.string()),('fetched_at',pa.string()),('rule_hash',pa.string()),('observed_at',pa.string()),('payload_hash',pa.string())])}

def configure_proxy():
 """Use the existing local HTTP proxy for HTTPS CONNECT; never change egress."""
 from urllib.parse import urlsplit
 from urllib.request import getproxies
 proxies=getproxies()
 http=os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or proxies.get('http')
 https=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or proxies.get('https')
 # SYSTEM tasks do not inherit this user's WinINET profile; persist the same local route.
 policy_path=ROOT/'configs/selection_lake_policy.json'
 configured=json.loads(policy_path.read_text(encoding='utf-8')).get('proxy_url') if policy_path.exists() else None
 if configured:
  os.environ['HTTP_PROXY']=configured;os.environ['HTTPS_PROXY']=configured
  os.environ['http_proxy']=configured;os.environ['https_proxy']=configured
  return
 if http and https:
  a,b=urlsplit(http),urlsplit(https)
  if a.scheme=='http' and b.scheme=='https' and a.hostname in ('127.0.0.1','localhost','::1') and (a.hostname,a.port)==(b.hostname,b.port):
   os.environ['HTTP_PROXY']=http;os.environ['http_proxy']=http
   os.environ['HTTPS_PROXY']=http;os.environ['https_proxy']=http


def atomic_json(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 try:
  tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8');os.replace(tmp,path)
 finally:
  tmp.unlink(missing_ok=True)

def rule_hash():
 h=hashlib.sha256()
 for name in ['ownership.py','国资主体白名单.csv','非国资法人名单.csv']:
  p=ROOT/'分析产出'/name
  if p.exists():h.update(p.read_bytes())
 return h.hexdigest()

def append_snapshot(dataset,records,*,lake=LAKE):
 """One immutable content-addressed snapshot; identical facts do not grow daily."""
 if dataset not in SCHEMAS:raise ValueError(dataset)
 if not records:return None
 clean=[]
 for row in records:
  r={k:v for k,v in row.items() if k not in ('observed_at','payload_hash')}
  r={k:(None if pd.isna(v) else v) for k,v in r.items()}
  clean.append(r)
 clean.sort(key=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False))
 digest=hashlib.sha256(json.dumps(clean,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
 directory=Path(lake)/'derived'/dataset;directory.mkdir(parents=True,exist_ok=True)
 file=directory/f'{digest}.parquet'
 if file.exists():
  latest=load_latest(dataset,lake=lake)
  keys=['SECUCODE','REPORT_DATE'] if dataset=='bank_risk_metrics' else ['SECUCODE'] if dataset=='actual_controller_disclosures' else ['code']
  bykey={tuple(str(row.get(k)) for k in keys):row for row in latest.to_dict('records')}
  unchanged=True
  for row in clean:
   previous=bykey.get(tuple(str(row.get(k)) for k in keys),{})
   for key,value in row.items():
    old=previous.get(key)
    if pd.isna(old):old=None
    if old!=value:unchanged=False
  if unchanged:return file
  file=directory/f'{digest}-{uuid.uuid4().hex}.parquet'
 now=datetime.now(timezone.utc).isoformat()
 table=pa.Table.from_pylist([{**r,'observed_at':now,'payload_hash':digest} for r in clean],schema=SCHEMAS[dataset])
 temp=file.with_suffix('.'+uuid.uuid4().hex+'.tmp')
 try:pq.write_table(table,temp,compression='zstd');os.replace(temp,file)
 finally:temp.unlink(missing_ok=True)
 return file

def load_latest(dataset,*,lake=LAKE):
 files=list((Path(lake)/'derived'/dataset).glob('*.parquet'))
 if not files:return pd.DataFrame(columns=SCHEMAS[dataset].names)
 df=pd.concat([pq.ParquetFile(p).read().to_pandas() for p in files],ignore_index=True)
 keys=['SECUCODE','REPORT_DATE'] if dataset=='bank_risk_metrics' else ['SECUCODE'] if dataset=='actual_controller_disclosures' else ['code']
 return df.sort_values('observed_at').drop_duplicates(keys,keep='last').reset_index(drop=True)

def bank_from_lake(symbol):
 df=load_latest('bank_risk_metrics')
 if not len(df):return None
 df=df[df.SECUCODE==symbol].sort_values('REPORT_DATE',ascending=False)
 return df if len(df) else None

def store_bank(symbol,rows):
 records=[]
 for row in rows:
  date=str(row.get('REPORT_DATE') or '')[:10]
  if len(date)!=10:raise ValueError('Bank report has no valid REPORT_DATE')
  record={'SECUCODE':symbol,'REPORT_DATE':date,'REPORT_DATE_NAME':str(row.get('REPORT_DATE_NAME') or ''),'source':'eastmoney:RPT_F10_FINANCE_MAINFINADATA','source_published_at':None}
  for key in ['HXYJBCZL','NONPERLOAN','BLDKBBL']:
   value=row.get(key);record[key]=None if value in (None,'','--','-') else float(value)
  if any(record[k] is None for k in ['HXYJBCZL','NONPERLOAN','BLDKBBL']):continue
  if not (0<=record['HXYJBCZL']<=100 and 0<=record['NONPERLOAN']<=100 and 0<=record['BLDKBBL']<=10000):
   raise ValueError('Bank ratios outside valid range; existing snapshots preserved')
  records.append(record)
 if not records:raise ValueError('No bank risk metrics; existing snapshots preserved')
 return append_snapshot('bank_risk_metrics',records)

def snapshot_governance(df):
 if not len(df):return
 rh=rule_hash();records=[]
 for row in df.to_dict('records'):
  row={k:(None if pd.isna(v) else v) for k,v in row.items()}
  row['rule_hash']=rh
  # fetched_at stays in the record for audit; snapshots with unchanged dates/facts deduplicate.
  records.append(row)
 return append_snapshot('governance_history',records)

def register_views():
 import duckdb
 con=duckdb.connect(str(LAKE/'duckdb/cnequity.duckdb'))
 try:
  for dataset in SCHEMAS:
   directory=LAKE/'derived'/dataset
   if not list(directory.glob('*.parquet')):continue
   glob=(directory/'*.parquet').as_posix().replace("'","''")
   sql=f"SELECT * FROM read_parquet('{glob}',union_by_name=true)"
   con.execute(f'CREATE OR REPLACE VIEW {dataset}_history AS {sql}')
   key='SECUCODE,REPORT_DATE' if dataset=='bank_risk_metrics' else 'SECUCODE' if dataset=='actual_controller_disclosures' else 'code'
   latest=f'{sql} QUALIFY row_number() OVER (PARTITION BY {key} ORDER BY observed_at DESC)=1'
   con.execute(f'CREATE OR REPLACE VIEW {dataset} AS {sql if dataset=="governance_history" else latest}')
   if dataset=='governance_history':con.execute(f'CREATE OR REPLACE VIEW governance_history_latest AS {latest}')
 finally:con.close()
