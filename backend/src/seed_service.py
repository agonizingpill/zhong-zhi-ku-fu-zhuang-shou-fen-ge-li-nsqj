from sqlalchemy import select,func,text
from .db import Base,engine,Session
from .models import *
from .auth import hashed
from . import vitality

def _upgrade_schema():
    # 初始快照之后新增的列；create_all 不会改已有表。
    if engine.dialect.name!='mysql':return
    with engine.begin() as conn:
        column=conn.execute(text("SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='germination_tests' AND column_name='model_version_id'")).scalar()
        if not column:conn.execute(text('ALTER TABLE germination_tests ADD COLUMN model_version_id INT NULL'))

def _baseline(s):
    if s.scalar(select(ModelVersion).where(ModelVersion.status=='active')):return
    params,training,metrics=vitality.baseline_snapshot()
    s.add(ModelVersion(code='MV-SIM-32',kind='simulated',status='active',reason='现行模型：固定seed32的模拟储藏观测',
        params=params,training=training,metrics=metrics,created_by='system'))

# 归档复测：旧纸签转录，训练候选用；不计扣种，也不覆盖任何人工记录。
ARCHIVE=[
    ('QH-042','青豆','旧圃归档青豆','2019–2021归档纸签',300,[
        ('QH-042-G2',2,96,2,-18),('QH-042-G3',3,94,12,8),('QH-042-G4',4,90,24,2),
        ('QH-042-G5',5,93,40,-6),('QH-042-G6',6,91,16,-12),('QH-042-G7',7,89,30,10)]),
]
ARCHIVE_TESTS=[('GT-ARC-QH042-01','QH-042-G2',100,96),('GT-ARC-QH042-02','QH-042-G3',100,73),
    ('GT-ARC-QH042-03','QH-042-G4',100,67),('GT-ARC-QH042-04','QH-042-G5',100,69),
    ('GT-ARC-QH042-05','QH-042-G6',100,80),('GT-ARC-QH042-06','QH-042-G7',100,60)]

def _archive(s):
    for code,crop,name,origin,minimum,lots in ARCHIVE:
        accession=s.scalar(select(Accession).where(Accession.code==code))
        if not accession:
            accession=Accession(code=code,crop=crop,name=name,origin=origin,minimum=minimum);s.add(accession);s.flush()
        for lot_code,generation,initial,months,temp in lots:
            if not s.scalar(select(Lot).where(Lot.code==lot_code)):
                s.add(Lot(accession_id=accession.id,code=lot_code,generation=generation,available=260,
                    stored_months=months,temperature=temp,initial_rate=initial))
    s.flush()
    for test_code,lot_code,sampled,germinated in ARCHIVE_TESTS:
        if s.scalar(select(GerminationTest).where(GerminationTest.code==test_code)):continue
        lot=s.scalar(select(Lot).where(Lot.code==lot_code))
        s.add(GerminationTest(lot_id=lot.id,code=test_code,sampled=sampled,germinated=germinated,state='recorded'))

def seed():
    Base.metadata.create_all(engine)
    _upgrade_schema()
    with Session() as s:
        for role in ('operator','supervisor','viewer'):
            email=role+'@qinghe.demo'
            if not s.scalar(select(User).where(User.email==email)):s.add(User(email=email,role=role,password_hash=hashed('Seeds2026!')))
        a=s.scalar(select(Accession).where(Accession.code=='QH-021'))
        if not a:a=Accession(code='QH-021',crop='青豆',name='青禾早熟青豆',origin='青禾北圃2023采集',minimum=300);s.add(a);s.flush()
        for code,generation,available in [('QH-021-G3',3,960),('QH-021-POLLEN',3,400)]:
            if not s.scalar(select(Lot).where(Lot.code==code)):s.add(Lot(accession_id=a.id,code=code,generation=generation,available=available,stored_months=36,temperature=4,initial_rate=94))
        other=s.scalar(select(Accession).where(Accession.code=='QH-087'))
        if not other:other=Accession(code='QH-087',crop='青豆',name='异源晚熟青豆',origin='南坡异源对照',minimum=300);s.add(other);s.flush()
        if not s.scalar(select(Lot).where(Lot.code=='QH-087-G2')):s.add(Lot(accession_id=other.id,code='QH-087-G2',generation=2,available=800,stored_months=18,temperature=-8,initial_rate=96))
        for i in range(18):
            code=chr(65+i//6)+str(i%6+1)
            if not s.scalar(select(Plot).where(Plot.code==code)):s.add(Plot(code=code,row=i//6,position=i%6,capacity=250))
        s.flush();lot=s.scalar(select(Lot).where(Lot.code=='QH-021-G3'))
        if not s.scalar(select(GerminationTest).where(GerminationTest.code=='GT-SEED-80')):s.add(GerminationTest(lot_id=lot.id,code='GT-SEED-80',sampled=100,germinated=80,state='recorded'))
        s.flush();_archive(s);_baseline(s)
        s.commit();return {cls.__tablename__:s.scalar(select(func.count()).select_from(cls))for cls in (User,Accession,Lot,GerminationTest,Regeneration,Plot,Flowering,Pollination,Harvest,Distribution,ModelVersion,ModelEvaluation)}
