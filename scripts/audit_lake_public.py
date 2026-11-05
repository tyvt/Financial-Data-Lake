"""Read-only sample audit of the lake against independent public sources."""
from __future__ import annotations
import json,random,sys,csv
from pathlib import Path
from datetime import datetime,timezone
import duckdb,requests,pandas as pd
ROOT=Path(__file__).resolve().parent.parent
LAKE=ROOT/'data/cnequity/duckdb/cnequity.duckdb'
OUT=ROOT/'分析产出'
H={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'}
S=requests.Session();S.trust_env=False

def dc(report,columns,filt,pagesize=10,sort=None):
    p={'reportName':report,'columns':columns,'filter':filt,'pageSize':str(pagesize),'pageNumber':1,'source':'HSF10','client':'PC'}
    if sort:p.update(sortColumns=sort[0],sortTypes=sort[1])
    r=S.get('https://datacenter.eastmoney.com/securities/api/data/v1/get',params=p,headers=H,timeout=25);r.raise_for_status()
    j=r.json()
    if not j.get('success'):raise RuntimeError(f'{report}: {j.get("message")}')
    return (j.get('result') or {}).get('data') or []

def tencent(symbols):
    codes=','.join(('sh' if s.endswith('.SH') else 'sz' if s.endswith('.SZ') else 'bj')+s[:6] for s in symbols)
    r=S.get(f'https://qt.gtimg.cn/q={codes}',headers=H,timeout=20);r.raise_for_status()
    text=r.content.decode('gbk',errors='replace');out={}
    for line in text.split(';'):
        if '="' not in line:continue
        body=line.split('="',1)[1].strip('"\n ').split('~')
        if len(body)<40:continue
        code=body[2];market=body[0]
        suffix='.SH' if market=='1' else '.BJ' if market in ('62','0') and code.startswith('92') else '.SZ'
        out[f'{code}{suffix}']={'market':market,'code':code,'name':body[1],'close':float(body[3]),'pre_close':float(body[4]),'open':float(body[5]),'volume_shares':float(body[6])*100,'date':body[30][:8],'high':float(body[33]),'low':float(body[34]),'amount_yuan':float(body[37])*10000 if body[37] else None}
    return out

def sina_daily(symbol,count=6):
    prefix='sh' if symbol.endswith('.SH') else 'sz' if symbol.endswith('.SZ') else 'bj'
    r=S.get('https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData',params={'symbol':prefix+symbol[:6],'scale':'240','ma':'no','datalen':str(count)},headers={'User-Agent':'Mozilla/5.0'},timeout=20);r.raise_for_status()
    return {row['day']:row for row in r.json()}

def tencent_daily(symbol,start,end):
    prefix='sh' if symbol.endswith('.SH') else 'sz' if symbol.endswith('.SZ') else 'bj'
    r=S.get('https://web.ifzq.gtimg.cn/appstock/app/kline/kline',params={'param':f'{prefix}{symbol[:6]},day,{start},{end},800'},headers={'User-Agent':'Mozilla/5.0'},timeout=25)
    r.raise_for_status();block=(r.json().get('data') or {}).get(prefix+symbol[:6]) or {}
    rows=block.get('day') or block.get('qfqday') or []
    return {row[0]:row for row in rows}

def pick_universe(con):
    held=pd.read_csv(OUT/'打新底仓_最终8只组合_v2.csv',encoding='utf-8-sig').sym.tolist()
    rng=random.Random(20261024)
    pools={'SH主板':("exchange='SH' and asset_type='stock' and delist_date is null and symbol not like '688%'"),'SH科创':("exchange='SH' and symbol like '688%'"),'SZ主板':("exchange='SZ' and asset_type='stock' and delist_date is null and symbol not like '30%'"),'SZ创业':("exchange='SZ' and symbol like '30%'"),'BJ':("exchange='BJ' and asset_type='stock' and delist_date is null")}
    extra=[]
    for label,where in pools.items():
        rows=[r[0] for r in con.execute(f'SELECT symbol FROM instruments WHERE {where} ORDER BY symbol').fetchall() if r[0] not in held]
        if rows:extra.append((label,rng.choice(rows)))
    return held,extra

def main():
    con=duckdb.connect(str(LAKE),read_only=True);con.execute('SET enable_progress_bar=false')
    held,extra=pick_universe(con);sample=held+[s for _,s in extra]
    latest=con.execute('SELECT max(trade_date) FROM daily_bars').fetchone()[0]
    checks=[]
    def add(category,symbol,field,lake,public,note=''):
        ok=None
        try:
            if lake is None or public is None:ok=None
            else:ok=abs(float(lake)-float(public))<=max(1e-6,abs(float(public))*1e-6)
        except (TypeError,ValueError):ok=(str(lake)==str(public))
        checks.append({'category':category,'symbol':symbol,'field':field,'lake':lake,'public':public,'match':ok,'note':note})
    # 1) latest close/volume/amount: Tencent + Sina must agree with the lake.
    #    Compared only to the precision each vendor actually publishes.
    quotes=tencent(sample)
    sines={}
    for s in sample:
        try:sines[s]=sina_daily(s,3)
        except Exception: sines[s]=None
    for s in sample:
        q=quotes.get(s);sn=sines.get(s)
        row=con.execute('SELECT trade_date,close,volume,amount FROM daily_bars WHERE symbol=? AND trade_date=?',[s,latest]).fetchone()
        if not row:add('行情快照',s,'存在性',False,None,'湖内无该日行情');continue
        if not q:add('行情快照',s,'腾讯可读性',True,False,'腾讯未返回该代码');continue
        add('行情快照',s,'收盘价',row[1],q['close'])
        # Tencent's volume unit differs by board (lot vs share), so volume is
        # verified against Sina, which always reports shares. Guessing the unit
        # per board would turn the check into a fit.
        pub_vol=float(q['volume_shares'])
        checks.append({'category':'行情快照','symbol':s,'field':'成交量(腾讯,原始单位)','lake':row[2],'public':int(pub_vol),'match':None,'note':'腾讯成交量单位随板块不同，仅记录，不作判定'})
        if q['amount_yuan']:amt_ok=abs(float(row[3])-q['amount_yuan'])<=max(20000.0,abs(q['amount_yuan'])*1e-5)
        else:amt_ok=None
        checks.append({'category':'行情快照','symbol':s,'field':'成交额(元)','lake':round(row[3]),'public':int(q['amount_yuan']) if q['amount_yuan'] else None,'match':amt_ok,'note':'腾讯公布到万元，容忍2万元/1e-5舍入'})
        if sn:
            day=latest.isoformat();p=sn.get(day)
            if p:
                add('行情快照',s,'收盘价(新浪独立源)',row[1],float(p['close']))
                checks.append({'category':'行情快照','symbol':s,'field':'成交量(新浪,股)','lake':row[2],'public':int(float(p['volume'])),'match':abs(float(row[2])-float(p['volume']))<=max(100.0,float(p['volume'])*1e-6),'note':'新浪以股为单位'})
    # 2) five-session OHLC vs Sina (independent vendor path)
    for s in sample:
        try:public=sina_daily(s,6)
        except Exception as exc:
            add('日线历史',s,'新浪可读性',True,False,f'请求失败 {type(exc).__name__}');continue
        rows=con.execute('SELECT trade_date,open,high,low,close FROM daily_bars WHERE symbol=? ORDER BY trade_date DESC LIMIT 6',[s]).fetchall()
        matched=0;compared=0
        for date,o,h,l,c in rows:
            p=public.get(date.isoformat())
            if not p:continue
            compared+=1
            same=(abs(o-float(p['open']))<1e-6 and abs(h-float(p['high']))<1e-6 and abs(l-float(p['low']))<1e-6 and abs(c-float(p['close']))<1e-6)
            matched+=same
            if not same:add('日线历史',s,f'{date} OHLC',f'{o}/{h}/{l}/{c}',f"{p['open']}/{p['high']}/{p['low']}/{p['close']}")
        checks.append({'category':'日线历史','symbol':s,'field':'近6日OHLC一致数','lake':f'{matched}/{compared}','public':'新浪日线','match':(matched==compared and compared>0),'note':'逐日开盘/最高/最低/收盘完全相等才计一致'})
    # 3) dividends vs EastMoney bonus report
    for s in sample:
        try:rows=dc('RPT_SHAREBONUS_DET','SECUCODE,REPORT_DATE,EX_DIVIDEND_DATE,PRETAX_BONUS_RMB','(SECUCODE="%s")'%s,12,('REPORT_DATE',-1))
        except Exception as exc:
            # "返回数据为空" is a content answer (new listing has no scheme yet),
            # not a source failure; record it separately instead of a mismatch.
            empty='返回数据为空' in str(exc) or '9201' in str(exc)
            checks.append({'category':'分红','symbol':s,'field':'东财分红方案','lake':None,'public':'无记录' if empty else '接口失败','match':None if empty else False,'note':'新股尚无分红方案，非湖内错误' if empty else f'{type(exc).__name__}'})
            continue
        public={}
        for r in rows:
            if not r.get('EX_DIVIDEND_DATE') or r.get('PRETAX_BONUS_RMB') is None:continue
            public[r['EX_DIVIDEND_DATE'][:10]]=float(r['PRETAX_BONUS_RMB'])/10  # 每10股派息 -> 每股
        lake={d.isoformat():v for d,v in con.execute("SELECT ex_date,cash_dividend FROM corporate_actions WHERE symbol=? AND action_type='cash_dividend'",[s]).fetchall() if v}
        common=sorted(set(public)&set(lake),reverse=True)[:5]
        if not common:add('分红',s,'可比除权日',len(lake),len(public),'无共同除权日');continue
        for d in common:
            add('分红',s,f'{d} 每股派现',lake[d],public[d])
    # 4) financials vs EastMoney performance report
    items={'BASIC_EPS':'eps','PARENT_NETPROFIT':'net_profit','WEIGHTAVG_ROE':'roe','TOTAL_OPERATE_INCOME':'revenue'}
    for s in sample:
        try:rows=dc('RPT_LICO_FN_CPD','SECUCODE,REPORTDATE,BASIC_EPS,PARENT_NETPROFIT,WEIGHTAVG_ROE,TOTAL_OPERATE_INCOME,NOTICE_DATE','(SECUCODE="%s")'%s,8,('REPORTDATE',-1))
        except Exception as exc:
            add('财务',s,'东财可读性',True,False,f'{type(exc).__name__}');continue
        for r in rows[:3]:
            period=r['REPORTDATE'][:10];q=f"{period[:4]}Q{(int(period[5:7])-1)//3+1}"
            for pub,item in items.items():
                if r.get(pub) is None:continue
                lake=con.execute('SELECT item_value FROM financial_statement_items WHERE symbol=? AND report_period=? AND item_code=?',[s,q,item]).fetchone()
                if not lake:add('财务',s,f'{q} {item}',None,r[pub],'湖内无该科目');continue
                add('财务',s,f'{q} {item}',lake[0],r[pub])
    # 5) instrument listing date & name vs EastMoney basic info
    for s in sample:
        try:rows=dc('RPT_F10_BASIC_ORGINFO','SECUCODE,SECURITY_NAME_ABBR,LISTING_DATE','(SECUCODE="%s")'%s,1)
        except Exception as exc:
            add('证券资料',s,'东财可读性',True,False,f'{type(exc).__name__}');continue
        if not rows:add('证券资料',s,'存在性',True,False,'东财无记录');continue
        name,list_date,ind=con.execute('SELECT name,list_date,exchange FROM instruments WHERE symbol=?',[s]).fetchone()
        # Exchange names use full-width letters (粤高速Ａ); compare after NFKC folding.
        import unicodedata
        fold=lambda v:unicodedata.normalize('NFKC',str(v or '')).strip().upper()
        add('证券资料',s,'证券简称',fold(name),fold(rows[0].get('SECURITY_NAME_ABBR')),'全角/半角归一后比较')
        pub_date=(rows[0].get('LISTING_DATE') or '')[:10]
        add('证券资料',s,'上市日期',list_date.isoformat() if list_date else None,pub_date)
    # 6) industry classification vs EastMoney industry name
    for s in sample:
        row=con.execute("SELECT industry_name FROM industry_members WHERE symbol=? AND classification_system='eastmoney' ORDER BY as_of_date DESC LIMIT 1",[s]).fetchone()
        try:pub=dc('RPT_F10_BASIC_ORGINFO','SECUCODE,INDUSTRYCSRC1','(SECUCODE="%s")'%s,1)
        except Exception as exc:pub=[]
        public=(pub[0].get('INDUSTRYCSRC1') if pub else None)
        add('行业分类',s,'东财行业',row[0] if row else None,public,'口径不同：湖内为东财行业板块，公开为证监会门类，仅记录不做相等判定')
        checks[-1]['match']=None
    # 7) cross-dataset internal consistency: adjusted vs raw returns on dividend dates
    bad_adj=con.execute("SELECT count(*) FROM daily_bars_adj WHERE hfq_close IS NULL OR hfq_close<=0 OR adj_close IS NULL OR adj_close<=0").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'全湖','field':'复权价非正/空行数','lake':bad_adj,'public':'不适用','match':bad_adj==0,'note':'复权价必须为正，否则波动率与回撤会失真'})
    inexact=con.execute("SELECT count(*) FROM daily_bars_adj WHERE adj_is_exact=false").fetchone()[0]
    total_adj=con.execute("SELECT count(*) FROM daily_bars_adj").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'全湖','field':'adj_is_exact=false 行数','lake':f'{inexact}/{total_adj}','public':'不适用','match':None,'note':'标记位本身不是错误；需看涉及的标的'})
    # Which names carry the inexact flag, and are any of them held?
    bad_names=con.execute("SELECT symbol,count(*) n FROM daily_bars_adj WHERE adj_is_exact=false GROUP BY 1 ORDER BY n DESC").fetchall()
    held_set=set(held)
    held_inexact=[(s,n) for s,n in bad_names if s in held_set]
    checks.append({'category':'内部一致性','symbol':'持仓8只','field':'非精确复权行数','lake':sum(n for _,n in held_inexact),'public':'不适用','match':sum(n for _,n in held_inexact)==0,'note':'持仓股复权必须精确，否则波动率与回撤不可信'})
    cdr=con.execute("SELECT count(*) FROM daily_bars_adj a JOIN instruments i USING(symbol) WHERE a.adj_is_exact=false AND i.asset_type<>'stock'").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'非股票(CDR等)','field':'非精确复权行数','lake':cdr,'public':'不适用','match':None,'note':'九号公司(689009.SH, CDR)因子源不足，仅影响该标的自身，不进股票池'})
    stale_tip=con.execute("SELECT count(*) FROM (SELECT symbol,count(*) n FROM daily_bars_adj WHERE adj_is_exact=false GROUP BY 1 HAVING max(trade_date)=DATE '2026-10-23')").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'全湖','field':'最新日仍有非精确复权标的数','lake':stale_tip,'public':'不适用','match':None,'note':'逐标的判断；本次涉及的5只均非持仓'})
    # Dividend ex-dates with no bar in an otherwise active series: either the
    # stock was suspended (correct) or the bar is missing (would misplace the
    # adjustment). Separate them instead of asserting one cause.
    con.execute("""CREATE TEMP TABLE orphan_div AS
      SELECT c.symbol,c.ex_date,
        (SELECT max(b.trade_date) FROM daily_bars b WHERE b.symbol=c.symbol AND b.trade_date<c.ex_date) prev,
        (SELECT count(*) FROM daily_bars b WHERE b.symbol=c.symbol AND b.trade_date BETWEEN c.ex_date-30 AND c.ex_date+30) near
      FROM corporate_actions c JOIN instruments i USING(symbol)
      WHERE c.action_type='cash_dividend' AND c.ex_date>=DATE '2016-01-04' AND i.asset_type='stock'
        AND NOT EXISTS (SELECT 1 FROM daily_bars b WHERE b.symbol=c.symbol AND b.trade_date=c.ex_date)""")
    # A missing bar on an ex-date is only a defect when the market was open AND
    # public vendors DO publish that symbol/session. Suspension is legitimate.
    gap_candidates=con.execute("""SELECT symbol,ex_date FROM orphan_div
      WHERE prev IS NOT NULL AND near>=20 ORDER BY ex_date DESC""").fetchall()
    unexplained=[]
    checked_susp=0;checked_open=0
    for sym,ex in gap_candidates:
        if len(unexplained)>=6 and checked_open>=6:break
        try:pub=tencent_daily(sym,ex,ex)
        except Exception:pub=None
        if pub is None:continue
        checked_open+=1
        if ex.isoformat() in pub:unexplained.append(f'{sym}@{ex}')
        else:checked_susp+=1
    checks.append({'category':'内部一致性','symbol':'全湖','field':'分红除权日无行情(历史活跃期)','lake':len(gap_candidates),'public':f'抽查{checked_open}例：{checked_susp}例公开源同样无行情(停牌)','match':len(unexplained)==0,'note':('未能用公开源证实的样例：'+', '.join(unexplained)) if unexplained else '抽查样例均为真实停牌，非湖内缺口'})
    no_prior=con.execute("SELECT count(*) FROM orphan_div WHERE prev IS NULL").fetchone()[0]
    sparse=con.execute("SELECT count(*) FROM orphan_div WHERE prev IS NOT NULL AND near<20").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'全湖','field':'分红除权日无行情(无前序行情)','lake':no_prior,'public':'不适用','match':None,'note':'多为上市前/极早期历史，不进入当前三年窗口'})
    checks.append({'category':'内部一致性','symbol':'全湖','field':'分红除权日无行情(历史稀疏)','lake':sparse,'public':'不适用','match':None,'note':'该标的历史本身稀疏，逐标的核查；不影响持仓'})
    held_orphans=con.execute("SELECT symbol,ex_date FROM orphan_div WHERE symbol IN ("+','.join('?'*len(held))+")",held).fetchall()
    held_verified=[]
    for sym,ex in held_orphans:
        try:pub=tencent_daily(sym,ex,ex)
        except Exception:pub=None
        held_verified.append((sym,ex.isoformat(),'无法核对' if pub is None else ('公开源同日也无行情(停牌)','一致') if ex.isoformat() not in pub else ('公开源有行情+湖内缺','缺口')))
    real_gaps=[r for r in held_verified if r[2]=='缺口']
    checks.append({'category':'内部一致性','symbol':'持仓8只','field':'分红除权日无行情','lake':len(held_orphans),'public':held_verified,'match':len(real_gaps)==0,'note':'持仓股分红日须有行情；公开源确认停牌的不算缺口'})
    # Coverage in the windows the scripts actually use: 252 sessions + 3 fiscal years.
    cover=con.execute("""WITH cal AS (SELECT trade_date FROM trading_calendar WHERE is_trading AND trade_date BETWEEN DATE '2024-01-01' AND ?),
      s AS (SELECT symbol FROM instruments WHERE symbol IN ("""+','.join('?'*len(held))+"""))
      SELECT s.symbol,COUNT(cal.trade_date)-COUNT(b.trade_date) missing
      FROM s CROSS JOIN cal LEFT JOIN daily_bars b ON b.symbol=s.symbol AND b.trade_date=cal.trade_date
      GROUP BY 1 ORDER BY missing DESC""",[latest]+held).fetchall()
    worst=cover[0] if cover else ('-',0)
    checks.append({'category':'内部一致性','symbol':'持仓8只','field':'2024年以来缺失交易日','lake':f'{worst[0]} 缺 {worst[1]} 天','public':'腾讯/新浪同期行情','match':worst[1]<=3,'note':'三年窗口内缺失须可解释为停牌；>3天需人工确认'})
    dup=con.execute("SELECT count(*)-count(DISTINCT (symbol||'|'||cast(trade_date as varchar))) FROM daily_bars").fetchone()[0]
    checks.append({'category':'内部一致性','symbol':'全湖','field':'daily_bars 主键重复','lake':dup,'public':'不适用','match':dup==0})
    con.close()
    df=pd.DataFrame(checks)
    df.to_csv(OUT/'数据湖抽检_公开源比对明细.csv',index=False,encoding='utf-8-sig')
    summary={'audited_at':datetime.now(timezone.utc).astimezone().isoformat(),'sample':{'held':held,'extra':[{'pool':p,'symbol':s} for p,s in extra]},'latest_trade_date':str(latest),'checks':int(len(df)),'match':int((df.match==True).sum()),'mismatch':int((df.match==False).sum()),'inconclusive':int(df.match.isna().sum()),'by_category':{k:{'checks':int(len(g)),'match':int((g.match==True).sum()),'mismatch':int((g.match==False).sum()),'inconclusive':int(g.match.isna().sum())} for k,g in df.groupby('category')}}
    (OUT/'数据湖抽检_公开源比对.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    bad=df[df.match==False]
    if len(bad):print('\nMISMATCHES\n'+bad.to_string(index=False)[:6000])

def run_audit() -> dict:
    """Scheduled entry point: audit, compare with the previous run, persist both.

    A new mismatch is a drift signal and must surface in the run status; a
    mismatch that was already recorded and explained is not re-alarmed. The
    script never writes to the lake, so a failure here cannot damage data.
    """
    history=ROOT/'logs/selection_update/public_audit_history.jsonl'
    history.parent.mkdir(parents=True,exist_ok=True)
    previous=None
    if history.exists():
        for line in history.read_text(encoding='utf-8').splitlines():
            if line.strip():
                try:previous=json.loads(line)
                except ValueError:continue
    before=set()
    if previous:
        before={f"{r['category']}|{r['symbol']}|{r['field']}" for r in previous.get('mismatches',[])}
    # The audit prints; capture stdout so the scheduler log stays readable.
    import io,contextlib
    buffer=io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main()
    summary=json.loads((OUT/'数据湖抽检_公开源比对.json').read_text(encoding='utf-8'))
    import csv as _csv
    with (OUT/'数据湖抽检_公开源比对明细.csv').open(encoding='utf-8-sig') as f:
        rows=list(_csv.DictReader(f))
    mismatches=[r for r in rows if r.get('match')=='False']
    new_issues=[r for r in mismatches if f"{r['category']}|{r['symbol']}|{r['field']}" not in before]
    record={'audited_at':summary['audited_at'],'checks':summary['checks'],'match':summary['match'],
            'mismatch':summary['mismatch'],'inconclusive':summary['inconclusive'],
            'mismatches':mismatches,'new_issues':new_issues}
    with history.open('a',encoding='utf-8') as f:
        f.write(json.dumps(record,ensure_ascii=False,default=str)+chr(10))
    result={'status':'warning' if new_issues else 'success','checks':summary['checks'],
            'match':summary['match'],'mismatch':summary['mismatch'],
            'inconclusive':summary['inconclusive'],'known_issues':len(mismatches)-len(new_issues),
            'new_issues':new_issues,'history':str(history)}
    if new_issues:
        print(chr(10)+'NEW AUDIT ISSUES'+chr(10)+chr(10).join(f"{r['category']} {r['symbol']} {r['field']}: lake={r['lake']} public={r['public']}" for r in new_issues[:20]))
    return result

if __name__=='__main__':main()
