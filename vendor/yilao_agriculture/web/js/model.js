import {clone,makeId,num,isoTime} from './ui.js';
import {regionLabel,stageLabel,preserveDisplayed,rateTotal,rateSourceLabel} from './display.js';
import {feedbackPrediction} from './predictions.js';
import {storageText} from './context.js';

function replace(rows,item){const i=rows.findIndex(x=>x.id===item.id);if(i===-1)rows.push(item);else rows[i]=item;}
function windowFrom(data){
  if(!data.availability_confirmed&&!data.confirmed)return [];
  if(!data.available_start||!data.available_end)throw new Error('已确认可用时，请把开始和结束时间填完整。');
  if(data.available_start>=data.available_end)throw new Error('结束时间需要晚于开始时间。');
  return [{start:isoTime(data.available_date,data.available_start),end:isoTime(data.available_date,data.available_end)}];
}
function mergeWindow(old,data){
  const original=(old.availability||[]).find(w=>w.start===data.availability_original_start&&w.end===data.availability_original_end);
  const remaining=(old.availability||[]).filter(w=>w!==original);
  const unchanged=original&&original.start.slice(0,10)===data.available_date&&original.start.slice(11,16)===data.available_start&&original.end.slice(11,16)===data.available_end;
  const current=unchanged&&(data.availability_confirmed||data.confirmed)?[original]:windowFrom(data);
  return [...remaining,...current].sort((a,b)=>new Date(a.start)-new Date(b.start));
}
function waitDutyFrom(data,old){
  const status=data.wait_status||'unknown';
  if(status==='unchanged')return old===undefined?undefined:clone(old);
  if(status==='unknown')return {status:'unknown'};
  if(status==='none')return old?.status==='known'&&old.minutes===0?{...old}:{status:'known',minutes:0,reason:'other',source:'本人确认无需等待',is_physical_labor:false,recovery_claimed:false};
  if(status!=='known')throw new Error('请选择等待情况。');
  const minutes=num(data.wait_minutes),reason=data.wait_reason,source=String(data.wait_source||'').trim();
  if(!Number.isFinite(minutes)||minutes<=0||minutes>10080)throw new Error('有等待时，请填写大于 0 且不超过 10080 的等待分钟数；无需等待请明确选择「确认无需等待」。');
  if(!['equipment','helper','material','weather','watch','other'].includes(reason))throw new Error('有等待时，请选择等待原因。');
  if(!source)throw new Error('有等待时，请写明等待时间和原因从哪里得知。');
  return {...old,status:'known',minutes,reason,source,is_physical_labor:false,recovery_claimed:false};
}
export function editPlot(state,data,id){
  const next=clone(state),old=next.plots.find(p=>p.id===id)||{};
  const cropId=data.crop_id||'unknown';
  const plot={...old,id:id||makeId('plot'),name:data.name.trim(),crop_id:cropId,crop_name:data.crop_name.trim(),category_id:data.category_id,crop_identity_status:cropId==='unknown'?'unknown':'confirmed',stage:data.stage.trim()||'unknown',area:{value:num(data.area_value),unit:data.area_unit},region_id:data.region_id.trim()||state.settings.place,latitude:num(data.latitude),longitude:num(data.longitude),environment:data.environment,conditions:{...old.conditions,soil:data.soil}};
  if((plot.latitude==null)!==(plot.longitude==null))throw new Error('经度和纬度需要一起填写，或都留空。');
  plot.stage=preserveDisplayed(old.stage,data.stage.trim(),stageLabel)||'unknown';
  plot.region_id=preserveDisplayed(old.region_id,data.region_id.trim(),regionLabel)||state.settings.place;
  replace(next.plots,plot);return next;
}
export function editTask(state,data,id,catalog){
  const next=clone(state),old=next.tasks.find(t=>t.id===id)||{};
  const plot=next.plots.find(p=>p.id===data.plot_id);
  const def=catalog.tasks.find(t=>t.code===data.task_code);
  if(!plot||!def)throw new Error('请选择已有地块和农活种类。');
  const q=num(data.quantity),rates=Object.fromEntries(Object.entries(old.rates||{}).filter(([id])=>![state.profile,...state.helpers].some(p=>p.id===id)));
  for(const person of [state.profile,...state.helpers]){
    const low=num(data[`rate_low__${person.id}`]),high=num(data[`rate_high__${person.id}`]);
    const previous=old.rates?.[person.id],scope=data[`rate_scope__${person.id}`]||previous?.scope||'net_work';
    const source=preserveDisplayed(previous?.source,(data[`rate_source__${person.id}`]||'').trim(),rateSourceLabel);
    if(previous&&q===old.remaining_quantity?.value&&data.unit===old.remaining_quantity?.unit&&data.method===old.method&&source===previous.source&&scope===previous.scope&&low===rateTotal(previous,q,data.unit,'low')&&high===rateTotal(previous,q,data.unit,'high')){rates[person.id]=clone(previous);continue;}
    if(low===null&&high===null&&!source)continue;
    if(low===null||high===null||low<=0||high<low||q<=0)throw new Error(`${person.name}的估时需要正数范围，最多分钟不能小于最少分钟；剩余量为零时不再填写估时。`);
    if(!source)throw new Error(`请写明${person.name}的估时从哪里来。`);
    rates[person.id]={...previous,unit:data.unit,low:low/q,high:high/q,scope,source};
  }
  if(data.deadline_date&&data.deadline_date<data.earliest_date)throw new Error('截止日期不能早于最早开始日期。');
  if(data.agronomy_status==='confirmed'&&!data.agronomy_source.trim())throw new Error('选择已有明确农艺依据时，需要写明来源。');
  if(data.activity_tags_confirmed&&!String(data.activity_source||'').trim())throw new Error('确认实际动作时，请写明由谁、何时核对。');
  const task={...old,id:id||makeId('task'),name:data.name.trim()||`${plot.name} · ${def.name}`,plot_id:plot.id,crop_id:plot.crop_id,stage:plot.stage||'unknown',operation:def.operation||def.code,task_code:def.code,method:data.method,remaining_quantity:{value:q,unit:data.unit},rates,earliest_start:isoTime(data.earliest_date,state.settings.start_time),deadline:data.deadline_date?isoTime(data.deadline_date,state.settings.end_time):null,deadline_source:data.deadline_source.trim(),priority:Number(data.priority),divisible:data.divisible==='yes',min_chunk_minutes:Number(data.min_chunk_minutes),depends_on:Object.keys(data).filter(k=>k.startsWith('depends__')).map(k=>k.slice(9)),required_resources:Object.keys(data).filter(k=>k.startsWith('resource__')).map(k=>k.slice(10)),tags:old.tags||[],load_per_trip_kg:num(data.load_per_trip_kg),distance_m:data.unit==='trip'?num(data.distance_m):null,session:Object.fromEntries(['setup_minutes','outbound_minutes','return_minutes','cleanup_minutes','buffer_minutes'].map(k=>[k,Number(data[k]||0)])),agronomy:{...old.agronomy,status:data.agronomy_status,source:data.agronomy_source.trim(),conditions:old.agronomy?.conditions||{},weather_limits:old.agronomy?.weather_limits||{}},notes:data.notes.trim()};
  const quantityChanged=task.remaining_quantity.value!==old.remaining_quantity?.value||task.remaining_quantity.unit!==old.remaining_quantity?.unit;
  if(state.feedback.some(f=>f.task_id===id)&&(['plot_id','operation'].some(k=>task[k]!==old[k])||quantityChanged))throw new Error('这项农活已有完成记录，不能改最初计量基数、地块或种类；不同农活请新建一项。');
  task.tags=[...new Set([...Object.keys(data).filter(k=>k.startsWith('tag__')).map(k=>k.slice(5)),...String(data.other_activity_tags||'').split(/[,，]/).map(x=>x.trim()).filter(Boolean)])];
  task.activity_tags_confirmed=Boolean(data.activity_tags_confirmed);
  task.activity_source=String(data.activity_source||'').trim();
  task.crop_id=data.task_crop_id||'unknown';
  task.stage=preserveDisplayed(old.stage??(!id?plot.stage:undefined),String(data.task_stage||'').trim(),stageLabel)||'unknown';
  if(task.stage==='阶段待确认')task.stage='unknown';
  const waitDuty=waitDutyFrom(data,old.wait_duty);
  if(waitDuty===undefined)delete task.wait_duty;else task.wait_duty=waitDuty;
  // 表单只展示工具，饮水、休息处及未展示的原要求不能因保存而撤销。
  const shownTools=new Set(state.resources.filter(r=>r.kind==='tool').map(r=>r.id));
  const selectedTools=new Set(Object.keys(data).filter(k=>k.startsWith('resource__')).map(k=>k.slice(10)).filter(id=>shownTools.has(id)));
  task.required_resources=[...new Set([...(old.required_resources||[]).filter(id=>!shownTools.has(id)||selectedTools.has(id)),...selectedTools])];
  if(old.earliest_start?.slice(0,10)===data.earliest_date)task.earliest_start=old.earliest_start;
  if(old.deadline?.slice(0,10)===data.deadline_date)task.deadline=old.deadline;
  replace(next.tasks,task);return next;
}
export function editPerson(state,data,id,self){
  const next=clone(state),old=self?next.profile:next.helpers.find(h=>h.id===id)||{};
  if(data.source_confirmed&&(data.source_kind==='unknown'||!data.limits_source.trim()))throw new Error('确认来源前，请选明来源类型并填写来源说明。');
  const sourcePrefix={self_report:'本人或家属自述',documented:'有记录的专业建议'}[data.source_kind];
  let source=data.limits_source.trim();
  if(source!==old.limits?.source&&source&&sourcePrefix&&!source.startsWith(sourcePrefix))source=`${sourcePrefix}：${source}`;
  const person={...old,id:old.id||makeId('helper'),name:data.name.trim(),role:self?'elder_self':'helper',entered_by:data.entered_by,state:data.state,state_checked_at:new Date().toISOString(),availability:windowFrom(data),initial_rest_confirmed:Boolean(data.initial_rest_confirmed),limitations_note:data.limitations_note.trim(),limits_valid_until:data.limits_valid_until?isoTime(data.limits_valid_until,'23:59'):null,used_active_minutes_by_date:{...old.used_active_minutes_by_date},limits:{review_status:data.source_confirmed?'confirmed':'unknown',source,max_active_minutes_per_day:num(data.max_active_minutes_per_day),max_continuous_active_minutes:num(data.max_continuous_active_minutes),min_rest_minutes:num(data.min_rest_minutes),max_load_kg:num(data.max_load_kg),forbidden_tags:data.forbidden_tags.split(/[,，]/).map(x=>x.trim()).filter(Boolean)}};
  person.limits.forbidden_tags=[...new Set([...Object.keys(data).filter(k=>k.startsWith('forbid__')).map(k=>k.slice(8)),...person.limits.forbidden_tags])];
  person.limits={...old.limits,...person.limits};
  person.limits_source_kind=data.source_kind;
  person.availability=mergeWindow(old,data);
  person.state_checked_at=data.state_checked_now||data.state==='stop'&&old.state!=='stop'?new Date().toISOString():data.state===old.state?old.state_checked_at??null:null;
  if(old.limits_valid_until?.slice(0,10)===data.limits_valid_until)person.limits_valid_until=old.limits_valid_until;
  person.limitations_reviewed=Boolean(data.limitations_reviewed);
  const used=num(data.used_active_minutes);
  if(used!==null)person.used_active_minutes_by_date[data.available_date]=used;else delete person.used_active_minutes_by_date[data.available_date];
  if(self){next.profile=person;next.settings.entered_by=data.entered_by;}else replace(next.helpers,person);
  return next;
}
export function editResource(state,data,id){
  const next=clone(state),old=next.resources.find(r=>r.id===id)||{};
  replace(next.resources,{...old,id:id||makeId('resource'),name:data.name.trim(),kind:data.kind,capacity:Number(data.capacity),confirmed:Boolean(data.confirmed),availability:mergeWindow(old,data)});return next;
}
export function editSettings(state,data){
  if(data.start_time>=data.end_time)throw new Error('考虑的结束时间应晚于开始时间。');
  const next=clone(state);next.settings={...next.settings,...data};return next;
}
export function editPolicy(state,data){
  if(data.confirmed&&!data.source.trim())throw new Error('确认已有依据前，请填写来源与适用范围。');
  const next=clone(state);next.policy={...next.policy,source:data.source.trim(),review_status:data.confirmed?'confirmed':'unknown',weather_limits:{...next.policy.weather_limits}};
  for(const k of ['max_temperature_c','max_wind_m_s','max_precipitation_mm']){const v=num(data[k]);if(v===null)delete next.policy.weather_limits[k];else next.policy.weather_limits[k]=v;}
  return next;
}
export function feedbackPayload(state,data,eventId,supersedes=''){
  const task=state.tasks.find(t=>t.id===data.task_id);
  if(!task)throw new Error('请选择需要记录的农活。');
  if(!data.consent)throw new Error(storageText('consentError'));
  const value=num(data.completed_quantity),unknown=data.status==='unknown';
  const untimedNotDone=data.status==='not_done'&&value===0&&num(data.net_minutes)===0&&num(data.rest_minutes)===0&&!data.started_at&&!data.ended_at;
  if(unknown&&value!==null)throw new Error('情况还不清楚时，请将完成量留空。');
  if(data.status==='not_done'&&!data.started_at&&!data.ended_at&&!untimedNotDone)throw new Error('完全没有开始时，请明确填写完成量、净干活和休息均为 0；若曾等待或往返，请填写实际起止时间。');
  if(!unknown&&!untimedNotDone&&(value===null||!data.started_at||!data.ended_at||num(data.net_minutes)===null||num(data.rest_minutes)===null))throw new Error('请填写实际完成量、开始和结束时间、净工作与休息分钟；确实不清楚时，可以把完成情况改为「情况还不清楚」先保存。');
  const context={};
  const prediction=feedbackPrediction(state,data,supersedes);
  if(!supersedes||data.refresh_context){
    const answer=data.context_matches_task||'unknown';
    if(!['unknown','true','false'].includes(answer))throw new Error('请确认这次实际做法、工具和现场情况是否相同。');
    if(supersedes&&answer==='unknown')throw new Error('重新核对情境时，请明确选择相同或不同；尚不清楚可以取消重新核对，保留原记录。');
    context.context_matches_task=answer==='unknown'?null:answer==='true';
    if(supersedes)context.refresh_context=true;
  }
  return {mode:state.mode,revision:state.revision,event_id:eventId,task_id:task.id,worker_id:data.worker_id,status:data.status,completed_quantity:value===null?null:{value,unit:task.remaining_quantity.unit},started_at:data.started_at?`${data.started_at.length===16?data.started_at+':00':data.started_at}+08:00`:null,ended_at:data.ended_at?`${data.ended_at.length===16?data.ended_at+':00':data.ended_at}+08:00`:null,net_minutes:num(data.net_minutes),rest_minutes:num(data.rest_minutes),quantity_source:data.quantity_source,clock_source:data.clock_source,notes:data.notes.trim(),consent:true,...context,...prediction,...(supersedes?{supersedes_event_id:supersedes}:{})};
}

// 展示余量以服务端计算结果为准，不把计划安排量当作实际完成量。
export function remainingFor(task,estimates){return estimates?.tasks?.find(t=>t.task_id===task.id)?.remaining||task.remaining_quantity;}
