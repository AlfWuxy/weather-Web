import {esc,select,check,button,note,numberText,units,datetimeText} from './ui.js';
import {rateSourceLabel} from './display.js';
export {rateSourceLabel} from './display.js';

const reasons={
  COMPARABLE_SELF_REPORT:'本人自述的条件与当前农活可比，计入观察范围。',
  EVENT_ID_UNKNOWN:'这条记录缺少可追溯编号。',DUPLICATE_EVENT_ID:'重复保存的同一条记录，只计算一次。',
  DIFFERENT_WORKER:'由另一位人员完成，不混作这位人员的用时。',EVENT_ID_CONFLICT:'同一编号下有不一致的记录，需要先核对。',
  CORRECTION_CHAIN_INVALID:'更正前后的关联不完整，需要先核对。',SUPERSEDED:'已有更新的更正记录，旧记录保留但不重复计入。',
  CONSENT_WITHDRAWN_OR_MISSING:'本人未同意使用，或已撤回同意。',WITHDRAWN:'这条记录已撤回。',
  STATUS_NOT_RATE_ELIGIBLE:'完成情况或实际工作量还不能用于比较用时。',
  ACTUAL_CONTEXT_NOT_CONFIRMED:'尚未确认当时的做法、工具和现场情况与农活记录相同。',
  HISTORICAL_CONTEXT_MISSING:'缺少当时保存的现场情境，不能用今天的资料代替。',
  HISTORICAL_CONTEXT_INVALID:'当时保存的情境资料不完整，需要核对。',
  HISTORICAL_CONTEXT_REFERENCE_MISMATCH:'当时的情境与这条记录对应不上。',
  TARGET_CONTEXT_UNKNOWN:'当前农活还有未明确的现场条件，暂不能比较。',CONTEXT_MISMATCH:'作物阶段、地块、做法、工具或负重等条件不同。',
  SOURCE_UNKNOWN_OR_COPIED:'数量或用时的来源不清楚，或来自安排中的预估。',NOT_NET_WORK:'尚未拆清净干活用时，不能与净工作用时混算。',
  SYNTHETIC_REAL_MODE_MISMATCH:'演示或测试记录不能用作真实个人估时依据。',
  ACTUAL_TIME_INVALID_OR_UNKNOWN:'实际时间缺失或前后不一致。',QUANTITY_OR_UNIT_INCOMPATIBLE:'实际完成量不明确，或单位不能直接换算。',
  RATE_INVALID:'实际完成量与净工作用时无法形成有效的每单位用时。',DUPLICATE_PHYSICAL_EVENT:'可能重复记录了同一次农活。',
  PHYSICAL_EVENT_OVERLAP:'这位人员的实际工作时间互相重叠，需要先核对。',ARCHIVE_INTEGRITY_REVIEW_REQUIRED:'记录档案还有冲突，核对完整后才能比较。',
  INTERRUPTED_SEPARATE_REVIEW:'这次中途停下，需要单独核对原因及未完成部分，不直接混入一般用时范围。',
  CONTEXT_INVALID:'农活情境资料不完整。',PLOT_CONTEXT_UNKNOWN:'地块、地点或露天/棚内情况待确认。',
  PLOT_CONDITIONS_UNKNOWN:'地块位置或现场条件待确认。',WORK_CONTEXT_UNKNOWN:'实际工作条件待确认。',
  ACTIVITY_CONTEXT_UNKNOWN:'实际动作待确认。',LOAD_CONTEXT_UNKNOWN:'一次拿起或搬动的最大重量待确认。',
  AGRONOMY_CONTEXT_UNKNOWN:'现场农事条件待确认。',TRANSPORT_CONTEXT_UNKNOWN:'搬运负重或距离待确认。',
  WAIT_CONTEXT_UNKNOWN:'是否等待、等多久还没有明确。',RESOURCE_CONTEXT_UNKNOWN:'实际工具与所需条件待确认。',
};
const contextFields={WORKER_ID:'人员',TASK_ID:'农活',TASK_CODE:'农活种类',OPERATION:'作业类型',METHOD:'实际做法',CROP_ID:'具体作物',STAGE:'作物阶段',UNIT:'计量单位'};
export function reviewReason(code){return reasons[code]||(code?.startsWith('CONTEXT_UNKNOWN_')?`${contextFields[code.slice(16)]||'现场信息'}待确认。`:'这条记录还有需要核对的信息。');}
const conditionText={
  CONFIRM_CONTEXT_AND_RECORDS:'逐条核对人员、作物阶段、地块、做法、工具负重、实际完成量和净干活分钟。',
  EXPLICIT_RATE_ADOPTION:'由本人明确确认后，才另存一份估时及其依据。',
  REFRESH_BASIS_BEFORE_ADOPTION:'保存前会重新核对依据；记录更正或现场条件变化后，需要重新查看。',
  NO_AUTOMATIC_TIGHTENING:'几次较快的记录不能直接缩短原来估计的最长用时。',
  CURRENT_CONDITIONS_STILL_REQUIRED:'今天的身体、天气、农事条件和工具仍需单独确认。',
  REVIEW_EXCLUSIONS:'请查看没有计入的记录，未知和中断的情况仍然保留。',
  RESOLVE_INTERRUPTED_RECORDS:'还有中断记录需要核对，暂不能把一般用时范围作为新的估时。',
  RESOLVE_ARCHIVE_CONFLICT:'更正关联、编号或实际时间存在冲突，请先核对记录。',
  COMPLETE_TARGET_CONTEXT:'当前农活还有未知条件，不能推断历史记录是否可比。',
  TEST_DATA_NOT_ADOPTABLE:'演示和测试记录只用于查看功能，不能采用为真实个人估时。',
  CURRENT_RATE_REVIEW_REQUIRED:'原估时的来源、单位或包含的用时还没明确，请先核对。',
};
function rateText(rate){
  if(!rate)return '尚未填写';
  return `${numberText(rate.low)}—${numberText(rate.high)} 分钟 / ${esc(units[rate.unit]||rate.unit||'单位待确认')}（${rate.scope==='net_work'?'净干活':rate.scope==='whole_session'?'包含准备、往返或休息':'用时口径待确认'}）`;
}
const back=()=>button('返回地块和农活','navigate','secondary','data-page="farm"');
export function reviewView(state,taskId='',loaded=null){
  const task=state.tasks.find(t=>t.id===taskId)||state.tasks[0];
  if(!task)return `<h1>用自己的记录核对估时</h1>${note('还没有农活。先登记一项农活，并如实记录实际完成情况。')}${back()}`;
  const valid=loaded?.mode===state.mode&&loaded?.revision===state.revision&&loaded?.taskId===task.id;
  const selected=valid?loaded.workerId:state.profile.id;
  const review=valid?loaded.review:null;
  const workers=[state.profile,...state.helpers];
  return `<div class="page-heading"><div><h1>用自己的记录核对估时</h1><p>分别查看每个人的实际记录，核对当时条件与现在是否相同。</p></div>${back()}</div>
  ${state.mode==='demonstration'?note('这是演示资料，可以查看记录如何比较，不能采用为真实个人估时。','warning'):''}
  <form data-form="rate-review" class="panel"><div class="form-grid">${select('task_id','要核对哪项农活',state.tasks.map(t=>({value:t.id,label:t.name})),task.id)}${select('worker_id','核对谁的用时',workers.map(w=>({value:w.id,label:w.name||'本人'})),selected)}</div><div class="form-error" role="alert" tabindex="-1"></div><div class="form-actions"><button class="button primary" type="submit">查看这位人员的记录</button></div></form>
  ${review?reviewResult(state,task,workers.find(w=>w.id===selected),review):note('选择农活与人员后，查看已有记录。没有记录时，可以先保存实际完成情况；这里不会预填示例记录。')}`;
}
function reviewResult(state,task,worker,review){
  const candidate=review.candidate,proposal=review.conservative_proposal;
  const allowed=review.adoption?.blocked===false&&Boolean(proposal)&&Boolean(review.basis_sha256);
  const records=review.records||[];
  const replaced=new Set(state.feedback.map(f=>f.supersedes_event_id).filter(Boolean));
  return `<section class="panel" id="rate-review-result"><h2>${esc(task.name)} · ${esc(worker?.name||'本人')}</h2>
  <dl class="data-pairs"><dt>现在填写的估时</dt><dd>${rateText(review.current_rate)}</dd><dt>现在的估时来源</dt><dd>${esc(rateSourceLabel(review.current_rate?.source))}</dd><dt>可比较的自述记录</dt><dd>${numberText(candidate?.count??0)} 条</dd></dl>
  ${candidate?`<div class="inline-summary"><h3>这些记录中观察到的用时范围</h3><p>${rateText(candidate)}</p><p class="field-hint">这是已记录情况的描述，不表示以后的耗时一定落在其中。只有一条记录时，上下值相同，也不代表每次都相同。</p></div>`:note(records.length?'目前没有条件和记录都明确、可以直接比较的自述记录。请查看下方原因。':'这位人员还没有可查看的完成记录。先按实际情况记录，之后再来核对。','warning')}
  ${review.target_reason_codes?.length?`<h3 class="subheading">当前农活还需要确认</h3><ul class="reason-list">${review.target_reason_codes.map(c=>`<li>${esc(reviewReason(c))}</li>`).join('')}</ul><div class="actions">${button('核对这项农活','navigate','secondary',`data-page="task" data-id="${esc(task.id)}"`)}${button('核对地块条件','navigate','secondary',`data-page="plot" data-id="${esc(task.plot_id)}"`)}</div>`:''}
  <h3 class="subheading">逐条查看记录</h3>${records.length?`<ol class="mini-list">${records.map(row=>{
    const event=state.feedback[row.index]||state.feedback.find(f=>f.event_id===row.event_id);
    const corrected=replaced.has(row.event_id);
    return `<li><strong>${esc(state.tasks.find(t=>t.id===row.task_id)?.name||'已保存的农活')} · ${esc([state.profile,...state.helpers].find(w=>w.id===event?.worker_id)?.name||'人员待核对')}</strong><p class="record-meta">${datetimeText(event?.started_at||event?.created_at)} · ${row.included?'计入观察范围':'此次未计入'}</p><ul class="reason-list">${(row.reason_codes||[]).map(c=>`<li>${esc(reviewReason(c))}</li>`).join('')}</ul>${row.observed_rate_minutes_per_unit!=null?`<p class="field-hint">这次记录为 ${numberText(row.observed_rate_minutes_per_unit)} 分钟 / ${esc(units[task.remaining_quantity.unit]||task.remaining_quantity.unit)}。</p>`:''}${row.unit_conversion?`<p class="field-hint">面积单位已统一：1 亩 = 2000/3 平方米；其他计量不作推测换算。</p>`:''}${event&&!corrected?button('核对或更正这条记录','navigate','secondary',`data-page="correction" data-id="${esc(row.event_id)}"`):''}</li>`;
  }).join('')}</ol>`:button('记录实际完成情况','navigate','secondary',`data-page="feedback" data-id="${esc(task.id)}"`)}
  <h3 class="subheading">采用前还要核对</h3><ul class="reason-list">${(review.adoption?.conditions||[]).map(c=>`<li>${esc(conditionText[c.code]||c.message||'还有情况需要进一步核对。')}</li>`).join('')}</ul>
  ${proposal?`<div class="inline-summary"><h3>核对后可以采用的估时</h3><p>${rateText(proposal)}</p><p class="field-hint">保留原来较长的用时边界，另存此次依据。个人活动与休息限制由原有资料决定。</p></div>`:''}
  ${allowed?`<form data-form="rate-adopt" data-task="${esc(task.id)}" data-worker="${esc(worker?.id)}">${check('adopt_confirmed','我已逐条核对这些记录与实际条件，同意把上方范围作为这位人员的新估时',false,'这是本人的明确选择，不是现场实测校准或健康许可。')}<div class="form-error" role="alert" tabindex="-1"></div><div class="form-actions"><button class="button primary" type="submit" disabled>确认采用上方估时</button></div></form>`:note('目前还不能采用新的估时。原来的记录与估时会保留，请先核对上面的原因。','warning')}
  <p class="field-hint">自述记录、来源文字及保存时间不能认证真实性。这份回顾不提供准确率或健康结论。</p></section>`;
}

export function adoptionPayload(state,loaded,data){
  if(!data.adopt_confirmed)throw new Error('请先逐条核对记录，并明确勾选同意采用。');
  if(!loaded||loaded.mode!==state.mode||loaded.revision!==state.revision)throw new Error('资料已经变化，请重新查看记录后再决定。');
  const review=loaded.review;
  if(review?.adoption?.blocked!==false||!review.conservative_proposal||!review.basis_sha256)throw new Error('目前还不能采用新的估时，请先核对未确认的情况。');
  return {mode:state.mode,revision:state.revision,task_id:loaded.taskId,worker_id:loaded.workerId,basis_sha256:review.basis_sha256,confirmed:true};
}
