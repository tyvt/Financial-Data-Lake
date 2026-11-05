"""Prune redundant COW copies after publication, retaining current/recent/referenced generations."""
from pathlib import Path
import json,re,hashlib,zipfile
from cnequity.storage.lifecycle import LifecycleStore,reference_files,reference_fingerprint
from cnequity.storage.lifecycle.purge import check_idle,verify_content,unfinished


def prune_history(*,root,allowed_datasets,keep=2,apply=False):
 root=Path(root);meta=root/'data/cnequity/meta';store=LifecycleStore(meta)
 if not store.registry_path.exists():return {'status':'skipped','reason':'No reviewed reference registry'}
 check_idle(meta/'manifest.db')
 if unfinished(store):raise RuntimeError('Unfinished historical deletion must be resumed before automatic pruning')
 registry=store.registry(required=True);objects=store.inventory();ids={o['revision_id']:o['object_id'] for o in objects};holds={key:list(value) for key,value in registry['holds'].items()}
 for p in reference_files(registry['reference_roots']):
  text=p.read_text(encoding='utf-8-sig',errors='replace')
  for token in set(re.findall(r'\b[0-9a-f]{32}\b',text)) & ids.keys():
   reason={'kind':'selection_reference','path':str(p.relative_to(root))}
   values=holds.setdefault(ids[token],[])
   if reason not in values:values.append(reason)
 for archive in (root/'分析产出').glob('*.zip'):
  with zipfile.ZipFile(archive) as z:
   for name in z.namelist():
    if Path(name).suffix not in ('.md','.py','.json','.toml','.csv','.txt'):continue
    tokens=set(re.findall(r'\b[0-9a-f]{32}\b',z.read(name).decode('utf-8',errors='replace'))) & ids.keys()
    for token in tokens:
     reason={'kind':'selection_archive_reference','path':str(archive.relative_to(root))+'!'+name}
     values=holds.setdefault(ids[token],[])
     if reason not in values:values.append(reason)
 store.import_manifest({'schema_version':1,'meta_root':str(store.meta),'holds':holds,'reference_roots':registry['reference_roots'],'reference_fingerprint':reference_fingerprint(registry['reference_roots']),'experiments':registry['experiments'],'cases':registry.get('cases',[])})
 eligible=[];invalid=[]
 for obj in store.inspect(keep=keep)['objects']:
  if obj['dataset'] not in allowed_datasets or obj['current'] or obj['blocked_reasons']:continue
  try:verify_content(store,obj,meta/obj['path'])
  except Exception as exc:
   store.hold(obj['object_id'],'Automatic retention: content validation failed; retain for repair');invalid.append({'object_id':obj['object_id'],'error':str(exc)});continue
  eligible.append(obj['object_id'])
 result={'status':'success','keep':keep,'candidate_ids':eligible,'invalid_retained':invalid,'referenced_or_held':len(holds),'bytes_deleted':0}
 if not eligible:return result
 plan=store.plan(keep=keep,phase='purge',selected_ids=eligible);result.update(plan_id=plan['plan_id'],candidate_bytes=plan['logical_bytes_to_purge'])
 directory=root/'logs/selection_update';directory.mkdir(parents=True,exist_ok=True)
 path=directory/f'retention-{plan["plan_id"]}.json';path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 if apply:
  before={p:str(hashlib.sha256(p.read_bytes()).hexdigest()) for p in (meta/'revisions').glob('*/current.json')}
  receipt=store.purge(plan['plan_id'],maintenance=True,manifest=meta/'manifest.db')
  assert all(p.exists() and hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in before.items()),'Current pointer changed'
  result['bytes_deleted']=receipt['logical_bytes_deleted'];result['deletion_status']=receipt['status']
 path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 return result

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
 root=Path(__file__).resolve().parent.parent;policy=json.loads((root/'configs/selection_lake_policy.json').read_text(encoding='utf-8'))
 result=prune_history(root=root,allowed_datasets=set(policy['allowed_datasets']),keep=policy.get('retained_generations',2),apply=args.apply)
 print(json.dumps(result,ensure_ascii=False))
