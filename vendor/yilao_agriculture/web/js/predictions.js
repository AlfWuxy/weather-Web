import {esc,field,select,check,button,note,quantity,numberText,datetimeText,num,units} from './ui.js';
import {rateSourceLabel} from './display.js';
import {reviewReason} from './review.js';
import {storageText} from './context.js';

const pointNames={none:'只保留时间范围，不选单点',slow_bound:'取范围中较长的用时作为单点',midpoint:'取范围中间值作为单点'};
const people=state=>[state.profile,...state.helpers];
const journal=state=>state.prediction_journal||[];
const errorBox='<div class="form-error" role="alert" tabindex="-1"></div>';
const resultButton=()=>button('查看已留存估时与实际对照','predictions-review','secondary');
const currentRemaining=(task,estimates)=>estimates?.tasks?.find(t=>t.task_id===task.id)?.remaining||task.remaining_quantity;
const unitChoices=unit=>(['mu','sqm'].includes(unit)?[unit,...['mu','sqm'].filter(u=>u!==unit)]:[unit]).map(value=>({value,label:units[value]||'原计量单位'}));

export function predictionQuery(state,data){
  const task=state.tasks.find(t=>t.id===data.task_id);
  if(!task||!people(state).some(w=>w.id===data.worker_id))throw new Error('请选择已有农活和实际人员。');
  const value=num(data.quantity_value),unit=data.quantity_unit;
  if(!Number.isFinite(value)||value<=0)throw new Error('请填写这一次打算记录的正工作量。');
  if(!unitChoices(task.remaining_quantity.unit).some(u=>u.value===unit))throw new Error('目标量需要采用这项农活可比较的计量单位。');
  if(['plant','trip'].includes(unit)&&!Number.isInteger(value))throw new Error('株数和趟数请填写整数。');
  return {mode:state.mode,task_id:task.id,worker_id:data.worker_id,quantity_value:String(value),quantity_unit:unit};
}
function previewMatches(state,taskId,loaded){return loaded?.mode===state.mode&&loaded?.revision===state.revision&&loaded?.query.task_id===taskId&&loaded?.preview.source_revision===state.revision;}
export function predictionView(state,taskId='',estimates=null,loaded=null){
  const task=state.tasks.find(t=>t.id===taskId)||state.tasks[0];
  if(!task)return `<h1>出工前留存估时</h1>${note('先登记一项农活，再按当时填写的用时留下事前估计。')}${button('去记农活','navigate','primary','data-page="farm"')}`;
  const valid=previewMatches(state,task.id,loaded),query=valid?loaded.query:null,remaining=currentRemaining(task,estimates);
  return `<div class="page-heading"><div><h1>出工前留存估时</h1><p>实际开始前留下这一次的估计，之后由你明确选择对应的实际记录。</p></div>${resultButton()}</div>
  ${note('这里只留存净干活用时，不包含休息、准备或来回。填写未来开始时间不表示已经安排出工，也不表示条件适合出工。','warning')}
  ${state.mode==='demonstration'?note('当前为独立演示区，留存和对照仅用于查看功能。','warning'):''}
  <form data-form="prediction-preview" class="panel"><div class="form-grid">
  ${select('task_id','哪项农活',state.tasks.map(t=>({value:t.id,label:t.name})),task.id)}
  ${select('worker_id','谁来做',people(state).map(w=>({value:w.id,label:w.name||'本人'})),query?.worker_id||state.profile.id)}
  ${field('quantity_value','这一次的目标量',{type:'number',value:query?.quantity_value??remaining.value,min:0,step:['plant','trip'].includes(query?.quantity_unit||remaining.unit)?1:'any',required:true,hint:`按已有记录还剩 ${quantity(remaining.value,remaining.unit)}。目标量不会当作实际完成量。`})}
  ${select('quantity_unit','目标量单位',unitChoices(task.remaining_quantity.unit),query?.quantity_unit||remaining.unit)}
  </div>${errorBox}<div class="form-actions"><button type="submit" class="button primary">先查看这次估时</button></div><p class="field-hint">修改农活、人员或目标量后，需要重新查看，才可以留存。</p></form>
  ${valid?previewResult(loaded.preview):'<div id="prediction-preview-result"></div>'}`;
}
function previewResult(preview){
  const estimate=preview.estimate,allowed=preview.ready===true&&estimate?.scope==='net_work'&&Boolean(preview.preview_sha256);
  const choices=(preview.point_policy_options||[]).map(o=>typeof o==='string'?o:o.value||o.id||o.code).filter(value=>Object.hasOwn(pointNames,value));
  if(!choices.includes('none'))choices.unshift('none');
  return `<section class="panel" id="prediction-preview-result"><h2>本次事前估时</h2><dl class="data-pairs"><dt>目标工作量</dt><dd>${quantity(preview.target_quantity?.value,preview.target_quantity?.unit)}</dd><dt>净干活时间范围</dt><dd>${estimate?`${numberText(estimate.low_minutes)}—${numberText(estimate.high_minutes)} 分钟`:'暂不能形成范围'}</dd><dt>估时来源</dt><dd>${esc(rateSourceLabel(preview.rate_snapshot?.source))}</dd></dl>
  ${(preview.reasons||[]).length?`<ul class="reason-list">${preview.reasons.map(r=>`<li>${esc(r.message||predictionReason(r.code))}</li>`).join('')}</ul>`:''}
  ${(preview.context_reason_codes||[]).length?`<h3>还需核对当时条件</h3><ul class="reason-list">${preview.context_reason_codes.map(c=>`<li>${esc(predictionReason(c))}</li>`).join('')}</ul>`:''}
  ${allowed?`<form data-form="prediction-freeze"><div class="form-grid">${field('expected_start_at','你预计这次何时开始',{type:'datetime-local',required:true,hint:'请填未来时间。这只是这份估时对应的意向时间，不是出工安排。'})}${select('point_policy','是否另外留一个单点估计',choices.map(value=>({value,label:pointNames[value]})),'none',{wide:true,hint:'默认只留范围。较长用时或中间值都需你主动选择，并不比范围更可靠。'})}${check('confirmed','我确认这是实际开始前留下的本次估时',false,'之后发生的实际情况需要另填，不能把这份估计当成已经完成。')}</div>${errorBox}<div class="form-actions"><button type="submit" class="button primary" disabled>确认留存本次估时</button></div></form>`:note('目前还不能留存这次估时，请先补充上面的信息。已经登记的农活会保留。','warning')}
  <p class="field-hint">估时留存，不是出工安排。它保留当时输入的依据，不认证来源或个人安全。</p></section>`;
}
export function freezePayload(state,loaded,data,currentQuery,now=Date.now()){
  if(!data.confirmed)throw new Error('请明确确认这是实际开始前留存的估时。');
  if(!loaded||!previewMatches(state,loaded.query?.task_id,loaded))throw new Error('资料已经变化，请重新查看这次估时。');
  const query=predictionQuery(state,currentQuery);
  if(['task_id','worker_id','quantity_value','quantity_unit'].some(k=>query[k]!==loaded.query[k]))throw new Error('人员、农活或目标量已经变化，请重新查看这次估时。');
  const preview=loaded.preview;
  if(preview.ready!==true||preview.estimate?.scope!=='net_work'||!preview.preview_sha256)throw new Error('这次估时还有信息待确认，暂不能留存。');
  const local=data.expected_start_at||'';
  if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(local))throw new Error('请填写完整的未来开始日期和时间。');
  const expected=local+(local.length===16?':00':'')+'+08:00';
  if(!Number.isFinite(Date.parse(expected))||Date.parse(expected)<=now)throw new Error('事前估时只能在实际开始前留存，请填写未来的预计开始时间。');
  const policy=data.point_policy||'none';
  const policies=(preview.point_policy_options||[]).map(o=>typeof o==='string'?o:o.value||o.id||o.code);
  if(!Object.hasOwn(pointNames,policy)||policy!=='none'&&!policies.includes(policy))throw new Error('请选择这次允许使用的单点估计方式。');
  return {mode:state.mode,revision:state.revision,task_id:query.task_id,worker_id:query.worker_id,quantity:{value:Number(query.quantity_value),unit:query.quantity_unit},expected_start_at:expected,point_policy:policy,confirmed:true,preview_sha256:preview.preview_sha256};
}

export function predictionOptions(state,taskId,workerId){
  return [{value:'',label:'不绑定事前估时'},...journal(state).filter(p=>p.task_id===taskId&&p.worker_id===workerId).map(p=>({value:p.prediction_id,label:`预计 ${datetimeText(p.expected_start_at)} · ${quantity(p.target_quantity?.value,p.target_quantity?.unit)} · 留于 ${datetimeText(p.frozen_at)}`}))];
}
export function predictionBindingField(state,taskId,workerId,original=null){
  if(original){
    const p=journal(state).find(p=>p.prediction_id===original.prediction_id);
    return note(original.prediction_id?`更正保留原绑定的事前估时${p?`：预计 ${datetimeText(p.expected_start_at)}，目标 ${quantity(p.target_quantity?.value,p.target_quantity?.unit)}`:'（原留存资料待核对）'}。不能更换为另一份估时。`:'原记录未绑定事前估时，更正保留这个状态，不会自动补绑。');
  }
  return select('prediction_id','是否对应一份出工前留存的估时',predictionOptions(state,taskId,workerId),'',{wide:true,hint:'默认不绑定。请核对人员、农活、目标量和开始时间后明确选择；选择不会替你填写实际完成量或用时。'});
}
export function feedbackPrediction(state,data,supersedes=''){
  if(supersedes){
    const original=state.feedback.find(f=>f.event_id===supersedes);
    if(Object.hasOwn(data,'prediction_id')&&(data.prediction_id||null)!==(original?.prediction_id||null))throw new Error('更正必须保留原来的事前估时绑定，不能换成另一份。');
    return {};
  }
  if(!data.prediction_id)return {};
  const entry=journal(state).find(p=>p.prediction_id===data.prediction_id);
  if(!entry||entry.task_id!==data.task_id||entry.worker_id!==data.worker_id)throw new Error('这份事前估时不属于所选人员和农活，请重新核对。');
  return {prediction_id:entry.prediction_id};
}

const predictionReasons={
  AWAITING_ACTUAL:'还没有明确绑定实际记录。',NO_LINKED_ACTUAL:'还没有明确绑定实际记录。',
  MULTIPLE_CURRENT_ACTUALS:'同一份事前估时关联了多条当前记录，需要先核对对应关系。',
  ASSOCIATION_CONFLICT:'事前估时与实际记录的对应关系有冲突。',PREDICTION_NOT_FOUND:'实际记录指向的事前估时当前没有找到。',
  QUANTITY_MISMATCH:'实际工作量与事前目标量不同，不能直接对照这次用时。',
  ACTUAL_BEFORE_FREEZE:'实际工作在估时留存前已经开始，不是事前对照。',
  FROZEN_AFTER_START:'估时没有在实际开始前留存，不能作为事前对照。',
  EXPECTED_START_MISMATCH:'实际开始与本次留存对应的时间不一致，需要核对。',
  PREDICTION_INTEGRITY_INVALID:'这份事前记录的完整性需要核对。',UNTRUSTED_PREDICTION:'这份留存的来源或完整性尚未核对。',
  IMPORTED_PREDICTION_UNTRUSTED:'导入的事前记录尚未核对，不能直接用于比较。',
  SUPERSEDED:'这条记录已有更正，只保留作历史，不重复比较。',
  FEEDBACK_ID_INVALID:'实际记录缺少有效编号，需要核对原记录。',FEEDBACK_ID_CONFLICT:'实际记录编号重复或冲突，需要先核对。',
  FEEDBACK_CORRECTION_INVALID:'实际记录的更正关联不完整，需要先核对。',REMAINING_QUANTITY_UNKNOWN:'已有完成情况还不清楚，请先核对剩余量。',
  REMAINING_PHYSICAL_EVENT_CONFLICT:'实际工作时段互相重叠，需要核对是否重复记录。',COMPLETED_EXCEEDS_REMAINING_BASE:'记录的完成量已超过最初登记量，需要先核对。',
  TARGET_EXCEEDS_REMAINING:'本次目标量超过按记录计算的剩余量。',TARGET_QUANTITY_INVALID:'目标量或单位需要核对。',
  RATE_RANGE_UNKNOWN:'还没有明确的净工作用时范围。',RATE_NOT_NET_WORK:'已有估时尚未拆清净工作用时。',RATE_SOURCE_UNKNOWN:'原估时来源待确认。',RATE_UNIT_INCOMPATIBLE:'估时与目标量的计量单位不能直接比较。',
  ACTUAL_IMPORTED_UNVERIFIED:'导入的实际记录尚未核对，不能直接作为这一次对照。',ACTUAL_WITHDRAWN_OR_NO_CONSENT:'本人未同意使用这条实际记录，或已经撤回。',
  ACTUAL_INTERRUPTED:'这次中途停下，需单独核对原因和未完成部分。',ACTUAL_STATUS_UNKNOWN_OR_NOT_DONE:'实际完成情况还不清楚，或本次没有开始。',
  ACTUAL_TASK_OR_WORKER_MISMATCH:'实际记录与留存估时的农活或人员不一致。',ACTUAL_SOURCE_UNKNOWN_OR_COPIED:'实际数量或用时来源不明，或照抄了事前估计。',
  ACTUAL_NOT_NET_WORK:'实际用时没有明确区分净干活、准备和休息。',CONTEXT_REFERENCE_MISMATCH:'现场情境与对应的农活或人员不一致。',
  ACTUAL_CONTEXT_MISMATCH:'实际做法、地块、作物阶段、工具或负重等与事前记录不同。',ACTUAL_QUANTITY_MISMATCH_OR_UNKNOWN:'实际完成量未知，或与事前目标量不一致。',
  ACTUAL_NOT_AFTER_FREEZE:'实际开始时间不在估时留存之后，不能作为事前对照。',ACTUAL_DATE_MISMATCH:'实际作业日期与事前所填日期不同，需要另行核对。',
  ACTUAL_SNAPSHOT_AFTER_RECORDING:'实际情境的保存时刻晚于记录时刻，需要核对记录来源。',
  PREDICTION_HASH_MISMATCH:'这份事前留存的内容与原完整性记录不一致。',PREDICTION_SCHEMA_INVALID:'这份事前留存的格式或编号不完整。',PREDICTION_ID_CONFLICT:'同一事前留存编号有多份内容，需要先核对。',
  PREDICTION_MODE_MISMATCH:'这份事前估时不属于当前的真实或演示资料区。',FROZEN_ESTIMATE_OR_TIME_INVALID:'事前用时范围、单点选择或留存时间存在问题，需要核对。',
  MULTIPLE_CURRENT_ASSOCIATIONS:'同一份事前估时绑定了多条当前实际记录，不能挑一条计算。',
};
export function predictionReason(code){
  if(code==='IMPORTED_UNVERIFIED')return storageText('predictionImported');
  if(code==='LOCAL_FREEZE_NOT_ESTABLISHED')return storageText('predictionOriginUnknown');
  return predictionReasons[code]||reviewReason(code);
}
const statusNames={awaiting_actual:'等待实际记录',comparable_self_report:'可以查看这一条自述对照',not_comparable:'当前不能比较',association_conflict:'对应关系需核对'};
export function predictionsView(state,loaded=null){
  const valid=loaded?.mode===state.mode&&loaded?.revision===state.revision,review=valid?loaded.review:null;
  const entries=journal(state);
  return `<div class="page-heading"><div><h1>事前估时与实际对照</h1><p>保留当时的估计，再看明确绑定的实际情况。单条对照不代表总体准确率。</p></div>${resultButton()}</div>
  ${state.mode==='demonstration'?note('当前是演示资料，仅用于查看留存和对照功能。','warning'):''}
  ${!entries.length&&!review?.predictions?.length?`${note('还没有事前留存的估时。可以从一项农活开始，在实际出工前核对目标量并留存。')}${button('到农活页选择','navigate','primary','data-page="farm"')}`:''}
  ${review?(review.predictions||[]).map(p=>predictionRecord(state,p)).join(''):entries.map(p=>`<section class="panel">${frozenSummary(state,p)}<p class="field-hint">已保存这份事前估时。点击上方按钮读取当前实际记录与对照状态。</p></section>`).join('')}
  ${review?.unlinked_record_ids?.length?note(`还有 ${review.unlinked_record_ids.length} 条实际记录没有绑定事前估时，保持未绑定，不自动配对。`):''}
  ${review?.orphan_prediction_links?.length?note(`有 ${review.orphan_prediction_links.length} 处绑定的事前估时当前找不到，请核对记录与导入资料。`,'warning'):''}
  <p class="field-hint">这里比较的是当时输入的估时与事后自述，不证明现场测量可靠、估时已校准或个人作业安全。</p>`;
}
function frozenSummary(state,p){
  const task=state.tasks.find(t=>t.id===p.task_id),worker=people(state).find(w=>w.id===p.worker_id),estimate=p.estimate;
  return `<h2>${esc(task?.name||'原农活')} · ${esc(worker?.name||'原人员')}</h2><dl class="data-pairs"><dt>留存时间</dt><dd>${datetimeText(p.frozen_at)}</dd><dt>当时预计开始</dt><dd>${datetimeText(p.expected_start_at)}</dd><dt>当时目标量</dt><dd>${quantity(p.target_quantity?.value,p.target_quantity?.unit)}</dd><dt>净工作估时</dt><dd>${numberText(estimate?.low_minutes)}—${numberText(estimate?.high_minutes)} 分钟</dd><dt>单点选择</dt><dd>${esc(pointNames[p.point_policy]||'待核对')}${p.point_policy!=='none'&&estimate?.point_minutes!=null?` · ${numberText(estimate.point_minutes)} 分钟`:''}</dd><dt>当时来源</dt><dd>${esc(rateSourceLabel(p.rate_snapshot?.source))}</dd></dl>`;
}
function predictionRecord(state,row){
  const p=row.frozen||{},comparison=row.status==='comparable_self_report'&&row.comparison?.evidence_basis==='self_report'?row.comparison:null;
  const delta=comparison?.actual_minus_point_minutes;
  return `<section class="panel">${frozenSummary(state,p)}<p><strong>${esc(statusNames[row.status]||'对照状态待核对')}</strong></p>
  ${(row.reason_codes||[]).length?`<ul class="reason-list">${row.reason_codes.map(c=>`<li>${esc(predictionReason(c))}</li>`).join('')}</ul>`:''}
  ${comparison?`<div class="inline-summary"><h3>这一条自述记录的对照</h3><dl class="data-pairs"><dt>实际自述净工作</dt><dd>${numberText(comparison.actual_net_minutes)} 分钟</dd><dt>实际自述工作量</dt><dd>${quantity(comparison.actual_quantity?.value,comparison.actual_quantity?.unit)}</dd><dt>事前时间范围</dt><dd>${numberText(comparison.predicted_low_minutes)}—${numberText(comparison.predicted_high_minutes)} 分钟</dd>${comparison.point_policy!=='none'&&comparison.predicted_point_minutes!=null?`<dt>事前主动选的单点</dt><dd>${numberText(comparison.predicted_point_minutes)} 分钟</dd><dt>本次与单点的差别</dt><dd>${delta==null?'尚不能比较':delta===0?'这次自述与单点值相同':`这次自述比单点${delta>0?'多':'少'} ${numberText(Math.abs(delta))} 分钟`}</dd>`:''}</dl>${typeof comparison.within_input_range==='boolean'?`<p>${comparison.within_input_range?'这次自述用时在事前输入范围内。':'这次自述用时在事前输入范围外。'}</p>`:''}<p class="field-hint">单条输入对照，不是总体准确率；范围内也不表示作业适合或安全。</p></div>`:''}
  ${(row.linked_records||[]).length?`<details><summary>查看绑定的实际记录与更正情况</summary><ul class="mini-list">${row.linked_records.map(link=>{const event=state.feedback.find(e=>e.event_id===link.event_id);return `<li><strong>${link.is_current?'当前记录':'历史记录'}</strong><p>${datetimeText(event?.started_at||event?.created_at)}</p>${(link.reason_codes||[]).map(c=>`<p class="field-hint">${esc(predictionReason(c))}</p>`).join('')}${link.is_current&&event?button('核对或更正实际记录','navigate','secondary',`data-page="correction" data-id="${esc(link.event_id)}"`):''}</li>`;}).join('')}</ul></details>`:''}
  </section>`;
}
