import os,time,json,logging
from pathlib import Path
import redis
from .db import Session
from .models import GerminationTest,Lot,ModelVersion
from .common import lock
from .vitality import predict_with
queue=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)
while True:
    Path('/tmp/seed-heartbeat').touch()
    try:
        item=queue.blpop('seed-vitality',timeout=2)
        if not item:continue
        id=int(item[1])
        with Session() as s:
            test=lock(s,GerminationTest,id)
            if test.state!='queued':continue
            test.state='running';s.commit()
            try:
                lot=s.get(Lot,test.lot_id)
                # 版本在入队时已固定；重试也读取同一个版本，缺少版本则按最终失败处理，绝不改换模型。
                version=s.get(ModelVersion,test.model_version_id) if test.model_version_id else None
                if not version:raise RuntimeError('复测未固定模型版本，无法估计')
                result=predict_with(version,lot,test);test.prediction=result;test.state='ready';test.error=None
                Path('/artifacts').mkdir(exist_ok=True);Path(f'/artifacts/vitality-{id}.json').write_text(json.dumps(result,ensure_ascii=False));s.commit()
            except Exception as ex:test.state='failed';test.error=str(ex)[:450];s.commit();logging.exception('vitality failed')
    except Exception:logging.exception('worker queue error');time.sleep(1)
