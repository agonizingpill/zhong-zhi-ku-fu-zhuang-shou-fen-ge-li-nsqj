import hashlib,json,random
import numpy as np
from sklearn.linear_model import BayesianRidge
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

SEED=32
THRESHOLD=85.0

def training_rows():
    rng=random.Random(32);rows=[]
    for i in range(180):
        months=rng.uniform(0,60);temp=rng.uniform(-18,22);initial=rng.uniform(80,98)
        actual=max(0,min(100,initial-.29*months-.34*(temp+18)+rng.gauss(0,2.2)))
        rows.append([round(months,4),round(temp,4),round(initial,4),round(actual,4)])
    return rows

def checksum(rows):
    return hashlib.sha256(json.dumps(rows,separators=(',',':'),sort_keys=True).encode()).hexdigest()

def serialize(scaler,model):
    return {'mean':[float(v)for v in scaler.mean_],'scale':[float(v)for v in scaler.scale_],'coef':[float(v)for v in model.coef_],'intercept':float(model.intercept_),'alpha':float(model.alpha_),'lambda':float(model.lambda_),'sigma':[[float(v)for v in row]for row in np.asarray(model.sigma_)]}

def fit(rows):
    """Fit StandardScaler + BayesianRidge on rows[months,temp,initial,actual] and freeze the parameters."""
    data=np.asarray(rows,dtype=float)
    if data.ndim!=2 or data.shape[0]<2:raise ValueError('历史复测训练样本不足')
    scaler=StandardScaler();x=scaler.fit_transform(data[:,:3]);model=BayesianRidge();model.fit(x,data[:,3])
    return serialize(scaler,model)

def predict_params(params,features):
    mean=np.asarray(params['mean'],float);scale=np.asarray(params['scale'],float);coef=np.asarray(params['coef'],float);sigma=np.asarray(params['sigma'],float)
    z=(np.asarray(features,float)-mean)/scale
    value=float(z@coef+params['intercept']);variance=float(z@sigma@z)+1.0/float(params['alpha'])
    return value,float(np.sqrt(max(variance,0)))

def score(params,rows):
    """Mean absolute error and 95% predictive coverage for rows[months,temp,initial,actual]."""
    if not rows:raise ValueError('盲评样本为空')
    errors=[];inside=[]
    for row in rows:
        value,spread=predict_params(params,row[:3]);actual=float(row[3])
        errors.append(abs(value-actual));inside.append(value-1.96*spread<=actual<=value+1.96*spread)
    return {'mae_percentage_points':round(float(np.mean(errors)),4),'interval95_coverage':round(float(np.mean(inside)),4),'rows':len(rows)}

def baseline_snapshot():
    """Freeze the current simulated-data model exactly as the historical predict() computed it."""
    rows=training_rows();data=np.asarray(rows)
    train,hold=train_test_split(np.arange(len(rows)),test_size=.25,random_state=SEED)
    params=fit(data[train].tolist());metrics=score(params,data[hold].tolist())
    training={'dataset':'simulated','train_rows':len(train),'holdout_rows':len(hold),'train_indices':train.tolist(),'holdout_indices':hold.tolist(),'seed':SEED,'source_checksum':checksum(rows)}
    return params,training,{'holdout_mae_percentage_points':metrics['mae_percentage_points'],'holdout_interval95_coverage':metrics['interval95_coverage'],'holdout_rows':metrics['rows'],'dataset':'simulated'}

def predict_with(version,lot,test):
    params=version.params or {};training=version.training or {};metrics=version.metrics or {}
    estimate,spread=predict_params(params,[lot.stored_months,lot.temperature,lot.initial_rate]);observed=100*test.germinated/test.sampled
    # 训练版自身在盲评种批上的误差；模拟基线用固定留出集。
    holdout_mae=metrics.get('holdout_mae_percentage_points')
    if holdout_mae is None:holdout_mae=metrics.get('candidate_mae_percentage_points')
    holdout_coverage=metrics.get('holdout_interval95_coverage')
    if holdout_coverage is None:holdout_coverage=metrics.get('candidate_interval95_coverage')
    return {'model_version_id':version.id,'model_version_code':version.code,'model_version_kind':version.kind,
        'estimate_pct':round(estimate,3),'lower95_pct':round(max(0,estimate-1.96*spread),3),'upper95_pct':round(min(100,estimate+1.96*spread),3),
        'observed_pct':round(observed,3),'renewal_required':observed<THRESHOLD or estimate<THRESHOLD,'threshold_pct':THRESHOLD,
        'holdout_mae_percentage_points':holdout_mae,'holdout_interval95_coverage':holdout_coverage,
        'train_rows':training.get('train_rows'),'holdout_rows':training.get('holdout_rows'),'train_indices':training.get('train_indices'),
        'holdout_indices':training.get('holdout_indices'),'seed':training.get('seed'),'source_checksum':training.get('source_checksum') or training.get('history_checksum'),
        'input_snapshot':{'lot_id':lot.id,'stored_months':lot.stored_months,'temperature':lot.temperature,'initial_rate':lot.initial_rate,'sampled':test.sampled,'germinated':test.germinated},
        'model':'StandardScaler(train only) + BayesianRidge',
        'interval':'Gaussian predictive mean ±1.96 predictive standard deviation, clipped to0–100',
        'dataset':training.get('dataset')}

def predict(lot,test):
    """Compatibility helper: predict with the current simulated-data baseline."""
    params,training,metrics=baseline_snapshot()
    class Version:pass
    version=Version();version.id=None;version.code='MV-SIM-32';version.kind='simulated';version.params=params;version.training=training;version.metrics=metrics
    return predict_with(version,lot,test)
