import uuid
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy import select
from .models import GerminationTest,Lot,ModelVersion,ModelEvaluation
from .common import pack,audit
from . import vitality

# 盲评样本按种批整体封存，至少留出两个种批。
MIN_HOLDOUT_LOTS=2
# BayesianRidge 至少需要两行训练样本。
MIN_TRAIN_ROWS=2

def lock_versions(s):
    """Serializes training, re-evaluation and publication on the small version table."""
    return s.scalars(select(ModelVersion).order_by(ModelVersion.id).with_for_update().execution_options(populate_existing=True)).all()

def active_version(s):
    return s.scalar(select(ModelVersion).where(ModelVersion.status=='active').order_by(ModelVersion.id.desc()))

def active_for_enqueue(s):
    """Linearize against publication on the version table: enqueue pins exactly one published version.

    锁定整张版本表再读 active，避免在 READ COMMITTED 下发布刚改状态时读到“无现行版”的空窗。
    """
    lock_versions(s)
    return active_version(s)

def _history_rows(s):
    lots={l.id:l for l in s.scalars(select(Lot)).all()}
    rows=[]
    for test in s.scalars(select(GerminationTest).order_by(GerminationTest.id)):
        lot=lots.get(test.lot_id)
        if not lot or test.sampled<=0:continue
        # 优先使用复测当时冻结的输入快照，退化时才取种批现值。
        snapshot=((test.prediction or {}).get('input_snapshot') or {})
        rows.append({'test_id':test.id,'test_code':test.code,'lot_id':lot.id,'lot_code':lot.code,
            'stored_months':float(snapshot.get('stored_months',lot.stored_months)),
            'temperature':float(snapshot.get('temperature',lot.temperature)),
            'initial_rate':float(snapshot.get('initial_rate',lot.initial_rate)),
            'sampled':int(test.sampled),'germinated':int(test.germinated),
            'actual_pct':round(100.0*test.germinated/test.sampled,6)})
    return rows

def _vector(row):return [row['stored_months'],row['temperature'],row['initial_rate'],row['actual_pct']]

def _seal(rows):
    payload=[{k:r[k] for k in ('test_id','test_code','lot_id','lot_code','stored_months','temperature','initial_rate','sampled','germinated','actual_pct')}for r in rows]
    return {'checksum':vitality.checksum(payload),'row_count':len(payload),
        'lot_ids':sorted({r['lot_id'] for r in payload}),'lot_codes':sorted({r['lot_code'] for r in payload}),'rows':payload}

def _verify_sealed(version):
    sealed=version.sealed
    if not sealed:raise HTTPException(409,'版本缺少封存盲评样本，不能比对')
    if sealed.get('checksum')!=vitality.checksum(sealed.get('rows') or []):
        raise HTTPException(409,'封存盲评样本校验失败')
    if len({r['lot_id'] for r in sealed['rows']})<MIN_HOLDOUT_LOTS:
        raise HTTPException(409,'封存盲评不足两个种批，不能比对')
    return sealed

def _evaluation_sample(evaluation):
    """The exact sealed sample used by this evaluation; publication recomputes on it."""
    sample=evaluation.sample
    if not sample or not sample.get('rows'):
        raise HTTPException(409,'盲评记录缺少封存样本，不能发布')
    if sample.get('checksum')!=vitality.checksum(sample['rows']):
        raise HTTPException(409,'封存盲评样本校验失败，不能发布')
    if len({r['lot_id'] for r in sample['rows']})<MIN_HOLDOUT_LOTS:
        raise HTTPException(409,'封存盲评不足两个种批，不能发布')
    return sample

def _score(params,sealed):
    if not params:raise HTTPException(409,'模型缺少冻结参数，无法盲评')
    detail=[];errors=[];inside=[]
    for row in sealed['rows']:
        estimate,spread=vitality.predict_params(params,[row['stored_months'],row['temperature'],row['initial_rate']])
        error=abs(estimate-row['actual_pct']);errors.append(error)
        covered=estimate-1.96*spread<=row['actual_pct']<=estimate+1.96*spread;inside.append(covered)
        detail.append({'test_code':row['test_code'],'lot_code':row['lot_code'],'actual_pct':row['actual_pct'],
            'estimate_pct':round(estimate,4),'abs_error':round(error,4),'inside95':covered})
    if not errors:raise HTTPException(409,'封存盲评样本为空，沿用现行版')
    return {'mae_percentage_points':round(sum(errors)/len(errors),4),
        'interval95_coverage':round(sum(inside)/len(inside),4),'rows':detail}

def _compare(candidate,active,sealed):
    new=_score(candidate.params,sealed);old=_score(active.params,sealed)
    improved=new['mae_percentage_points']<old['mae_percentage_points']
    metrics={'decision':'better' if improved else 'keep_current',
        'baseline_version_id':active.id,'baseline_version_code':active.code,'sample_checksum':sealed['checksum'],
        'sample_lot_codes':sealed['lot_codes'],'sample_rows':sealed['row_count'],
        'candidate_mae_percentage_points':new['mae_percentage_points'],'baseline_mae_percentage_points':old['mae_percentage_points'],
        'improvement_percentage_points':round(old['mae_percentage_points']-new['mae_percentage_points'],4),
        'candidate_interval95_coverage':new['interval95_coverage'],'baseline_interval95_coverage':old['interval95_coverage'],
        'comparison':[{'test_code':n['test_code'],'lot_code':n['lot_code'],'actual_pct':n['actual_pct'],
            'baseline_estimate_pct':o['estimate_pct'],'candidate_estimate_pct':n['estimate_pct'],
            'baseline_abs_error':o['abs_error'],'candidate_abs_error':n['abs_error']}for o,n in zip(old['rows'],new['rows'])]}
    return metrics,improved

def _record(s,candidate,active,sealed,actor):
    metrics,improved=_compare(candidate,active,sealed)
    s.add(ModelEvaluation(candidate_id=candidate.id,baseline_version_id=active.id,sample_checksum=sealed['checksum'],
        sample=sealed,decision=metrics['decision'],metrics=metrics,created_by=actor,created_at=datetime.utcnow()))
    candidate.metrics=metrics
    candidate.status='candidate' if improved else 'rejected'
    candidate.reason='' if improved else '盲评平均绝对误差未优于现行版，沿用 '+active.code
    # 新版盲评记录需要主管重新核对，旧核对不作数。
    candidate.reviewed_by=None;candidate.reviewed_at=None;candidate.reviewed_evaluation_id=None
    s.flush()
    return improved

def _blind_rows(rows,active):
    """封存样本：现行版训练没见过的种批里，按种批整体留出两个。

    模拟基线没训练过任何真实复测，因此任何真实种批都对它盲；
    现行版本身是训练版时，只有它训练时未见过的种批才谈得上盲评。
    """
    unseen=sorted({r['lot_id'] for r in rows if r['lot_id'] not in _trained_lots(active)})
    if len(unseen)<MIN_HOLDOUT_LOTS:return []
    holdout=set(unseen[-MIN_HOLDOUT_LOTS:])
    return [r for r in rows if r['lot_id'] in holdout]

def _trained_lots(version):
    return set((version.training or {}).get('train_lot_ids')or [])

def archive_pack(version):
    """Archive view: keeps audit evidence but not the full sealed rows or fitted parameters."""
    item=pack(version)
    item['params']=None
    training=dict(version.training or {})
    for key in ('train_indices','holdout_indices'):training.pop(key,None)
    item['training']=training
    sealed=version.sealed or None
    item['sealed']=({'checksum':sealed['checksum'],'row_count':sealed['row_count'],
        'lot_ids':sealed['lot_ids'],'lot_codes':sealed['lot_codes']}if sealed else None)
    return item

def archive_evaluation(evaluation):
    item=pack(evaluation)
    item['sample']=None
    return item

def train_candidate(s,u):
    rows=_history_rows(s)
    lock_versions(s)
    active=active_version(s)
    if not active:raise HTTPException(409,'现行活力模型版本缺失，无法训练候选')
    sealed_rows=_blind_rows(rows,active)
    if len({r['lot_id'] for r in sealed_rows})<MIN_HOLDOUT_LOTS:
        unseen=len({r['lot_id'] for r in rows})-len(_trained_lots(active)&{r['lot_id'] for r in rows})
        why='现行版未训练过的复测仅覆盖 %d 个种批，留不出两个盲评种批，沿用现行版'%max(unseen,0)
        return _rejected(s,u,active,None,None,why)
    sealed_lots={r['lot_id'] for r in sealed_rows}
    train=[r for r in rows if r['lot_id'] not in sealed_lots]
    if len(train)<MIN_TRAIN_ROWS:
        return _rejected(s,u,active,None,None,'留出盲评种批后训练复测不足，沿用现行版')
    params=vitality.fit([_vector(r)for r in train])
    sealed=_seal(sealed_rows)
    training={'dataset':'historical_retests','train_rows':len(train),'holdout_rows':len(sealed_rows),
        'train_lot_ids':sorted({r['lot_id'] for r in train}),'holdout_lot_ids':sealed['lot_ids'],
        'train_lot_codes':sorted({r['lot_code'] for r in train}),'holdout_lot_codes':sealed['lot_codes'],
        'history_checksum':vitality.checksum([_vector(r)for r in rows]),
        'note':'按种批整体封存盲评样本；候选训练样本不含盲评种批的任何复测。'}
    candidate=ModelVersion(code='MV-TRN-'+uuid.uuid4().hex[:10].upper(),kind='trained',status='candidate',
        params=params,training=training,sealed=sealed,created_by=u.email,created_at=datetime.utcnow())
    s.add(candidate);s.flush()
    improved=_record(s,candidate,active,sealed,u.email)
    audit(s,u,('候选活力模型盲评优于现行版，待主管核对发布 ' if improved else '候选活力模型盲评未优于现行版，沿用现行版 ')+candidate.code)
    s.flush()
    return pack(candidate)

def _rejected(s,u,active,params,sealed,why):
    candidate=ModelVersion(code='MV-TRN-'+uuid.uuid4().hex[:10].upper(),kind='trained',status='rejected',
        params=params,sealed=sealed,created_by=u.email,created_at=datetime.utcnow(),reason=why,
        training={'dataset':'historical_retests','train_rows':0,'holdout_rows':0,'reason':why})
    s.add(candidate);s.flush()
    metrics={'decision':'keep_current','reason':why,'baseline_version_id':active.id,'baseline_version_code':active.code,
        'candidate_mae_percentage_points':None,'baseline_mae_percentage_points':None,'sample_checksum':None}
    candidate.metrics=metrics
    s.add(ModelEvaluation(candidate_id=candidate.id,baseline_version_id=active.id,sample_checksum='',
        decision='keep_current',metrics=metrics,created_by=u.email,created_at=datetime.utcnow()))
    candidate.reviewed_by=None;candidate.reviewed_at=None;candidate.reviewed_evaluation_id=None
    audit(s,u,'候选活力模型无法完成盲评，沿用 '+active.code+' '+candidate.code)
    s.flush()
    return pack(candidate)

def _shared_sample(sealed,active):
    """两个版本都未训练过的同一份样本：只取候选封存批里现行版也没训练过的部分。

    候选只训练了封存批以外的种批，所以不能拿“现行版未见过的其他批”充数，
    那会让候选在它训练过的数据上考试。凑不出两个种批就不比，沿用现行版。
    """
    trained=_trained_lots(active)
    rows=[r for r in sealed['rows'] if r['lot_id'] not in trained]
    if len({r['lot_id'] for r in rows})<MIN_HOLDOUT_LOTS:
        raise HTTPException(409,'候选与现行版没有共同的未训练种批，无法用同一份样本比对，应沿用现行版')
    return _seal(rows)

def re_evaluate(s,id,u):
    lock_versions(s)
    candidate=s.get(ModelVersion,id)
    if not candidate:raise HTTPException(404,'模型版本不存在')
    if candidate.status!='candidate':raise HTTPException(409,'仅待发布候选可与现行版重比')
    if not candidate.params:raise HTTPException(409,'候选缺少冻结参数')
    active=active_version(s)
    if not active:raise HTTPException(409,'现行活力模型版本缺失')
    sealed=_shared_sample(_verify_sealed(candidate),active)
    improved=_record(s,candidate,active,sealed,u.email)
    audit(s,u,'候选与现行版 '+active.code+' 重比：'+('优于现行版，待发布 ' if improved else '未优于现行版，沿用现行版 ')+candidate.code)
    s.flush()
    return pack(candidate)

def review(s,id,u,note=''):
    """主管在复测页面核对盲评记录后才可发布；核对绑定具体一条盲评。"""
    lock_versions(s)
    candidate=s.get(ModelVersion,id)
    if not candidate:raise HTTPException(404,'模型版本不存在')
    if candidate.status!='candidate':raise HTTPException(409,'只有待发布候选可以核对盲评')
    active=active_version(s)
    if not active:raise HTTPException(409,'现行活力模型版本缺失')
    evaluation=s.scalar(select(ModelEvaluation).where(ModelEvaluation.candidate_id==id).order_by(ModelEvaluation.id.desc()))
    if not evaluation:raise HTTPException(409,'候选缺少盲评记录，不能核对')
    if evaluation.baseline_version_id!=active.id:
        raise HTTPException(409,'候选盲评所用旧版已被 '+active.code+' 接替，请先与现行版重比再核对')
    if evaluation.decision!='better':raise HTTPException(409,'候选盲评误差未优于现行版，应沿用现行版')
    candidate.reviewed_by=u.email;candidate.reviewed_at=datetime.utcnow();candidate.reviewed_evaluation_id=evaluation.id
    audit(s,u,'主管在复测页核对候选盲评 '+candidate.code+(('：'+note[:200])if note else ''))
    s.flush()
    return pack(candidate)

def publish(s,id,u):
    lock_versions(s)
    candidate=s.get(ModelVersion,id)
    if not candidate:raise HTTPException(404,'模型版本不存在')
    if candidate.status!='candidate':raise HTTPException(409,'只有待发布候选可以发布；已拒绝或已发布版本不可覆盖')
    active=active_version(s)
    if not active:raise HTTPException(409,'现行活力模型版本缺失')
    evaluation=s.scalar(select(ModelEvaluation).where(ModelEvaluation.candidate_id==id).order_by(ModelEvaluation.id.desc()))
    if not evaluation:raise HTTPException(409,'候选缺少盲评记录，不能发布')
    # 两个候选都比过同一旧版时，先发布的接班；另一个必须与现行版重比。
    if evaluation.baseline_version_id!=active.id:
        raise HTTPException(409,'候选盲评所用旧版已被 '+active.code+' 接替，请先与现行版重比再发布')
    if evaluation.decision!='better':raise HTTPException(409,'候选盲评误差未优于现行版，应沿用现行版')
    if not candidate.reviewed_by or candidate.reviewed_evaluation_id!=evaluation.id:
        raise HTTPException(409,'请先由主管在复测页核对本条盲评再发布')
    sealed=_evaluation_sample(evaluation)
    # 发布前用与盲评完全相同的封存样本复算，防止参数或样本被绕过接口改动。
    if sealed['checksum']!=evaluation.sample_checksum:
        raise HTTPException(409,'封存盲评样本与发布时不一致，不能发布')
    metrics,improved=_compare(candidate,active,sealed)
    if not improved:raise HTTPException(409,'候选盲评误差未优于现行版，应沿用现行版')
    candidate.status='active';candidate.published_by=u.email;candidate.published_at=datetime.utcnow()
    active.status='superseded'
    audit(s,u,'主管核对盲评并发布活力模型 '+candidate.code+'，接替 '+active.code)
    s.flush()
    return {'published':pack(candidate),'superseded':pack(active),'evaluation':metrics}
