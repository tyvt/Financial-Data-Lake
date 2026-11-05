"""Migrate the 16 documented inferred restructuring markers to the current schema."""
from pathlib import Path
from datetime import datetime,timezone
import json
import polars as pl
from cnequity.storage.revisions import RevisionStore
from cnequity.storage.lifecycle import LifecycleStore
from cnequity.file_lock import lake_mutation_lock
from cnequity.storage.lifecycle.purge import check_idle
from cnequity.domain.schemas import validate_dataframe
ROOT=Path(__file__).resolve().parent.parent

def normalize_legacy(frame):
 mask=(pl.col('action_type')=='non_cash_event') & (pl.col('source')=='derived_adj_factor') & (pl.col('reference_price')>0)
 original=frame.filter(mask)
 if original.is_empty():return frame,original
 updated=frame.with_columns(pl.when(mask).then(pl.lit('reorg_transfer')).otherwise(pl.col('action_type')).alias('action_type'),pl.when(mask).then(pl.lit(1.0)).otherwise(pl.col('split_factor')).alias('split_factor'),pl.when(mask & pl.col('fetched_at').is_null()).then(pl.lit(datetime.now(timezone.utc))).otherwise(pl.col('fetched_at')).alias('fetched_at'))
 return updated,original

def main():
 lake=ROOT/'data/cnequity';meta=lake/'meta';work=ROOT/'logs/legacy_action_schema_migration';work.mkdir(parents=True,exist_ok=True)
 rs=RevisionStore(meta,lake/'curated');prior=rs.latest('corporate_actions');changed=[];original=[];before_cash=0;after_cash=0;before_rows=0;after_rows=0
 with lake_mutation_lock(meta,blocking=False):
  check_idle(meta/'manifest.db')
  current=rs.current_root('corporate_actions')
  for p in current.rglob('*.parquet'):
   frame=pl.read_parquet(p);updated,old=normalize_legacy(frame);validate_dataframe(updated,'corporate_actions')
   before_rows+=frame.height;after_rows+=updated.height;before_cash+=frame['cash_dividend'].sum() or 0;after_cash+=updated['cash_dividend'].sum() or 0
   if len(old):original.extend(old.to_dicts());changed.append((p.relative_to(current),updated))
  assert before_rows==after_rows and before_cash==after_cash
  if not original:print('Already migrated');return
  assert len(original)==16,'Only the 16 previously documented inferred markers may be migrated'
  record={'at':datetime.now(timezone.utc).isoformat(),'old_revision_id':prior.revision_id,'rows':original,'meaning':'Inferred restructuring markers; not issuer disclosures. Original source and reference price preserved; factor normalized because reference_price carries the adjustment. Missing fetched_at records this migration observation time, not an invented historical source time.','cash_dividends_unchanged':True,'row_count_unchanged':True}
  (work/'original_rows.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
  # Failed compact may have left mutable candidates; source fragments remain staged for retry.
  rs.materialize_current('corporate_actions')
  paths=[]
  for rel,frame in changed:
   p=lake/'curated/corporate_actions'/rel;frame.write_parquet(p,compression='zstd');paths.append(p)
  receipt=rs.commit('corporate_actions',run_id='legacy-action-schema-migration',changed_files=paths,schema_version=prior.schema_version,contract_fingerprint=prior.contract_fingerprint,metadata={'operation':'legacy_schema_migration','rows_changed':16,'original_rows':str((work/'original_rows.json').relative_to(ROOT)),'inferred_source_preserved':True},_locked=True)
  record['new_revision_id']=receipt.revision_id
  (work/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
 LifecycleStore(meta).hold('revision/corporate_actions/'+prior.revision_id,'Pre-schema-migration source evidence for the 16 inferred restructuring markers')
 print('MIGRATED',len(original),'rows; cash dividends and reference prices unchanged',receipt.revision_id)

if __name__=='__main__':main()
