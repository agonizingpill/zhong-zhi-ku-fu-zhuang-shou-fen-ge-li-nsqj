import{esc,data}from'../utils/format.js';
import{title,specimen,reserve,lotOptions,empty}from'../components/specimens.js';
import{perform,bindActions,toast}from'../utils/actions.js';
import{api}from'../api.js';

const VERSION_STATES={active:'现行版',candidate:'待发布候选',rejected:'已否决',superseded:'已被接替'};
const fmt=(value,suffix='')=>value===null||value===undefined?'—':Number(value).toFixed(2)+suffix;

function modelPanel(d,user){
  let boss=user.role==='supervisor',versions=d.model_versions||[],active=versions.find(v=>v.status==='active');
  let candidates=versions.filter(v=>v.status==='candidate'),others=versions.filter(v=>['rejected','superseded'].includes(v.status));
  let row=v=>{
    let m=v.metrics||{},evals=(d.model_evaluations||[]).filter(e=>e.candidate_id===v.id).sort((a,b)=>b.id-a.id),last=evals[0];
    let reviewed=!!v.reviewed_evaluation_id&&!!last&&v.reviewed_evaluation_id===last.id;
    let needsCompare=!!last&&!!active&&last.baseline_version_id!==active.id;
    let candidateMae=m.candidate_mae_percentage_points??(v.kind==='simulated'?m.holdout_mae_percentage_points:null);
    let baselineMae=m.baseline_mae_percentage_points??null;
    let verdict=v.kind==='simulated'?'现行模拟数据基线：自身留出集MAE，尚未与历史复测盲评。':(m.decision==='better'?'同一封存样本上候选平均绝对误差更小':(m.decision==='keep_current'?'盲评平均绝对误差未优于对照版本，沿用对照版本':(m.reason||'')));
    return `<article class="paper modelcard"><h2>${esc(v.code)} <small>${VERSION_STATES[v.status]||esc(v.status)} / ${v.kind==='simulated'?'模拟数据基线':'历史复测训练'}</small></h2>
      <p>${esc(v.reason||'')}</p>
      <div class="evidence">
        <div><small>${v.kind==='simulated'?'留出 MAE':'盲评 MAE 候选'}</small><b>${fmt(candidateMae,'')}</b></div>
        <div><small>盲评 MAE 现行版</small><b>${fmt(baselineMae,'')}</b></div>
        <div><small>对照版本</small><b>${esc(m.baseline_version_code||'—')}</b></div>
        <div><small>盲评种批</small><b>${(m.sample_lot_codes||v.training?.holdout_lot_codes||[]).length}</b></div>
      </div>
      <p><small>训练 ${v.training?.train_rows??'—'} 条 / 盲评 ${v.training?.holdout_rows??'—'} 条 · 盲评种批 ${esc((v.training?.holdout_lot_codes||[]).join('、')||'—')}<br>${esc(verdict)}${needsCompare?'<br><b>盲评所用旧版已被接替：须先与现行版重比。</b>':''}</small></p>
      ${(m.comparison||[]).length?`<table class="blind"><thead><tr><th>种批</th><th>实测</th><th>现行版估计</th><th>候选估计</th><th>现行误差</th><th>候选误差</th></tr></thead><tbody>${m.comparison.map(r=>`<tr><td>${esc(r.lot_code)}</td><td>${r.actual_pct.toFixed(1)}%</td><td>${r.baseline_estimate_pct.toFixed(1)}%</td><td>${r.candidate_estimate_pct.toFixed(1)}%</td><td>${r.baseline_abs_error.toFixed(2)}</td><td>${r.candidate_abs_error.toFixed(2)}</td></tr>`).join('')}</tbody></table>`:''}
      ${v.status==='candidate'?`<div class="actions">
        <button data-model-evaluate="${v.id}" ${!boss||!needsCompare?'disabled':''}>与现行版重比</button>
        <button data-model-review="${v.id}" ${!boss||reviewed||needsCompare?'disabled':''}>${reviewed?'已核对盲评':'主管核对盲评'}</button>
        <button class="primary" data-model-publish="${v.id}" ${!boss||!reviewed||needsCompare?'disabled':''}>发布该版本</button>
      </div>`:''}
    </article>`;
  };
  return `<section class="paper"><div class="heading"><h2>活力模型版本与盲评</h2><button data-train-model ${!boss?'disabled':''}>用旧复测训练候选</button></div>
    <p class="note">候选只用刚发布版未曾见过的历史复测训练，盲评种批整体留出封存；新旧两版在同一份封存样本上计算平均绝对误差。留不出两个种批或误差变大时沿用现行版。主管在下方核对盲评后才发布。</p>
    ${active?row(active):empty('现行活力模型版本缺失。')}
    ${candidates.length?`<h2>待发布候选</h2>${candidates.map(row).join('')}`:''}
    ${others.length?`<details><summary>历史候选 ${others.length} 个</summary>${others.map(row).join('')}</details>`:''}
  </section>`;
}

export function render(s){
  let d=s.data,write=s.user.role!=='viewer';
  let selected=d.germination_tests.find(t=>t.id===Number(sessionStorage.getItem('test-selected')))||d.germination_tests.at(-1),prediction=selected?.prediction;
  let versions=d.model_versions||[],versionById=Object.fromEntries(versions.map(v=>[v.id,v]));
  let pinned=selected?.model_version_id?versionById[selected.model_version_id]:null;
  return title('01 / SEED ACCESSION','一粒种子，一份来历。','核对种批与代次，实际抽样耗种；将复测送交活力估计。')
  +`<div class="two"><section><div class="lotgrid">${d.lots.map(l=>`<article class="paper">${specimen(l,d)}<p>储期 ${l.stored_months}月 · ${l.temperature}℃ · 初始发芽率 ${l.initial_rate}%</p><small>${l.parent_lot_id?'母本种批 #'+l.parent_lot_id:'原始入藏批'}</small></article>`).join('')}</div>`
  +`${d.accessions.map(a=>reserve(a,d)).join('')}`
  +`<section class="paper"><h2>发芽复测簿</h2>${d.germination_tests.map(t=>`<article class="testrow"><button data-test="${t.id}">${esc(t.code)}</button><span>${t.germinated}/${t.sampled}粒 · ${esc(t.state)}${t.model_version_id?' · 版本 '+esc(versionById[t.model_version_id]?.code||('#'+t.model_version_id)):''}</span><button data-action="/germination-tests/${t.id}/estimate" ${!write||!['recorded','failed'].includes(t.state)?'disabled':''}>${t.state==='failed'?'按原版本重试':'送交活力估计'}</button>${t.error?`<p class="error">${esc(t.error)}</p>`:''}</article>`).join('')}`
  +`${prediction?`<div class="evidence"><div><small>预测发芽率</small><b>${prediction.estimate_pct}%</b></div><div><small>95%预测区间</small><b>${prediction.lower95_pct}–${prediction.upper95_pct}%</b></div><div><small>实测发芽率</small><b>${prediction.observed_pct}%</b></div></div><p class="note">${prediction.renewal_required?'触发复壮：实测或预测低于85%。':'未触发低活力复壮。'} 复测所用版本 ${esc(prediction.model_version_code||pinned?.code||'—')}（入队时固定，重试不改换）。<br>该版本留出MAE ${fmt(prediction.holdout_mae_percentage_points)}百分点 / 95%区间覆盖率 ${prediction.holdout_interval95_coverage==null?'—':(prediction.holdout_interval95_coverage*100).toFixed(1)+'%'}；${prediction.train_rows??'—'}训练 / ${prediction.holdout_rows??'—'}留出。模拟辅助估计，非真实种质性能认证。</p>`:empty('点击一条已完成复测查看独立worker估计。')}</section>`
  +modelPanel(d,s.user)
  +`</section><aside><section class="paper"><h2>录入发芽复测</h2><form id="test-form"><label>抽样种批</label><select name="lot_id">${lotOptions(d)}</select><label>复测编号</label><input name="code" required placeholder="GT-20261118-01"><div class="pair"><div><label>抽样粒数</label><input name="sampled" type="number" min="20" max="1000" value="100" required></div><div><label>发芽粒数</label><input name="germinated" type="number" min="0" value="80" required></div></div><button class="primary" ${!write?'disabled':''}>记录抽样并扣种</button></form></section><section class="paper"><h2>新种质入藏</h2><form id="accession-form">${[['code','种质编号'],['name','种质名称'],['crop','作物'],['origin','采集来源']].map(([k,l])=>`<label>${l}</label><input name="${k}" required>`).join('')}<label>最低保底 / 粒</label><input name="minimum" type="number" min="100" value="300"><button ${!write?'disabled':''}>登记种质身份</button></form></section><section class="paper"><h2>首批种子入库</h2><form id="lot-form"><label>仅新种质首批</label><select name="accession_id">${d.accessions.map(a=>`<option value="${a.id}">${esc(a.code)}</option>`).join('')}</select>${[['code','种批编号','text',''],['generation','代次','number',0],['available','入库粒数','number',800],['stored_months','储期 / 月','number',0],['temperature','储温 / ℃','number',-18],['initial_rate','初始发芽率 / %','number',95]].map(([k,l,t,v])=>`<label>${l}</label><input name="${k}" type="${t}" value="${v}" required>`).join('')}<button ${!write?'disabled':''}>首批入库</button></form></section></aside></div>`;
}

export function bind(s,reload){
  bindActions(reload);
  document.querySelectorAll('[data-test]').forEach(b=>b.onclick=async()=>{sessionStorage.setItem('test-selected',b.dataset.test);await reload()});
  document.querySelector('#test-form').onsubmit=e=>{e.preventDefault();perform('/germination-tests','POST',data(e.target,['lot_id','sampled','germinated']),reload,'抽样已扣种，复测已记录',x=>sessionStorage.setItem('test-selected',x.id))};
  document.querySelector('#accession-form').onsubmit=e=>{e.preventDefault();perform('/accessions','POST',data(e.target,['minimum']),reload,'种质身份已登记')};
  document.querySelector('#lot-form').onsubmit=e=>{e.preventDefault();perform('/lots','POST',data(e.target,['accession_id','generation','available','stored_months','temperature','initial_rate']),reload,'首批种子已入库')};
  document.querySelectorAll('[data-train-model]').forEach(b=>b.onclick=()=>perform('/model-versions','POST',{},reload,'候选已训练并按封存盲评与现行版比对'));
  document.querySelectorAll('[data-model-evaluate]').forEach(b=>b.onclick=()=>perform('/model-versions/'+b.dataset.modelEvaluate+'/evaluate','POST',{},reload,'候选已与现行版在同一份封存样本上重比'));
  document.querySelectorAll('[data-model-review]').forEach(b=>b.onclick=()=>{
    let note=prompt('核对盲评记录后的说明（可留空）')??null;
    if(note===null)return;
    perform('/model-versions/'+b.dataset.modelReview+'/review','POST',{note},reload,'盲评已核对，可以发布');
  });
  document.querySelectorAll('[data-model-publish]').forEach(b=>b.onclick=async()=>{
    if(!confirm('确认将此候选发布为现行活力模型？已入队的复测仍按原版本运行。'))return;
    try{let r=await api('/model-versions/'+b.dataset.modelPublish+'/publish','POST',{});await reload();toast('已发布 '+r.published.code+'，接替 '+r.superseded.code)}catch(e){toast(e.message)}
  });
}
