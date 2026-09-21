import {esc,button,note,quantity,datetimeText,timeText,numberText,textOf,units} from './ui.js';
import {cropLabel,settingsForm,policyForm} from './forms.js';
import {remainingFor} from './model.js';
import {readinessActions} from './readiness.js';
import {regionLabel,stageLabel,personStateLabel} from './display.js';
import {rateSourceLabel} from './review.js';
import {storageText} from './context.js';
import {requestBodyLimitText} from './api.js';

const navButton=(label,page,variant='secondary')=>button(label,'navigate',variant,`data-page="${esc(page)}"`);
const editButton=(label,page,id)=>button(label,'navigate','secondary',`data-page="${esc(page)}" data-id="${esc(id)}"`);
const allPeople=s=>[s.profile,...s.helpers];
const phaseNames={setup:'准备',outbound:'去程',return:'返程',cleanup:'收尾',buffer:'预留时间',rest:'休息',work:'净工作',active:'工作',initial_rest:'开始前休息'};
const noticeMessages=(xs=[])=>xs.map(textOf).filter(Boolean);
export function header(title,description,action=''){return `<div class="page-heading"><div><h1>${esc(title)}</h1>${description?`<p>${esc(description)}</p>`:''}</div>${action}</div>`;}
export function checklist(state) {
  const items=[['地块与剩余农活',state.tasks.length?`已记 ${state.plots.length} 块地、${state.tasks.length} 项农活`:'有哪些地块？还剩下什么农活？','farm'],['今天身体情况',personStateLabel(state.profile,state.mode),'people'],['实际可用的人和工具',`${state.helpers.length} 位帮手，${state.resources.filter(r=>r.confirmed&&r.availability?.length).length} 项已确认可用条件`,'people'],['当地天气与预警',Object.keys(state.weather).length?'查看更新时刻，也要另外核对预警':'尚未更新天气，预警也未核对','weather']];
  return `<aside class="panel"><h2>安排前，一起确认</h2><ul class="checklist">${items.map(([name,desc,page])=>`<li><div><a href="#${page}">${esc(name)}</a><p>${esc(desc)}</p></div></li>`).join('')}</ul>${note('不确定的信息可以先留空，确认后再安排。','warning')}</aside>`;
}
export function todayView(state,readiness,estimates) {
  const empty=!state.tasks.length,plan=state.plans?.at(-1),items=readiness?.items||[];
  const blocked=readiness?.ready===false;
  const weatherBlocked=items.some(i=>i.severity!=='notice'&&(i.field?.startsWith('weather')||/^(WEATHER|ALERT)_/.test(i.code||'')));
  const stopped=allPeople(state).filter(p=>p.state==='stop');
  const action=empty?navButton('开始记录农活','farm','primary'):blocked&&items.length?button('补充待确认事项','navigate','primary','data-page="today" data-focus="readiness"'):button('查看安排草稿','plan','primary');
  return `${header('农活安排',empty?'先记下农活，再一起确认条件。':`${state.settings.place} · ${state.settings.planning_date} · ${state.settings.start_time}—${state.settings.end_time}`)}
  ${stopped.length?note(`${stopped.map(p=>p.id===state.profile.id?'本人':p.name||'帮手').join('、')}已记录不舒服，先停下农活，不安排其出工。仍可保存记录。`,'stop'):''}
  ${weatherBlocked?note('天气或预警条件尚未通过核对，目前不能据此安排出工。','warning'):''}
  <section class="panel today-card">
    ${empty?'<h2>还没有农活记录</h2><p class="muted">填好地块和剩余量，就可以开始。</p>':`<h2>这次的农活${state.tasks.length>3?`（共 ${state.tasks.length} 项）`:''}</h2><ul class="mini-list">${state.tasks.slice(0,3).map(t=>{const q=remainingFor(t,estimates);return `<li><strong>${esc(t.name)}</strong><p class="record-meta">按记录还剩 ${quantity(q.value,q.unit)}${t.deadline?` · 最晚 ${esc(t.deadline.slice(0,10))}`:''}</p>${q.unknown_records?'<p class="field-hint">还有不清楚的完成记录，余量待核对。</p>':''}${estimateText(t,estimates)}</li>`;}).join('')}</ul>${state.tasks.length>3?'<a class="inline-link" href="#farm">查看全部农活</a>':''}`}
    ${blocked?`<p class="readiness-status">${items.length?`还有 ${items.length} 项待确认 · `:''}暂不能生成安排</p>`:''}
    <div class="actions today-actions">${action}${empty?button('先看演示','enter-demo','tertiary'):''}</div>
    <p class="field-hint">${empty?'不确定的资料可以先留空。':'感觉不舒服时先停下；安排草稿需结合现场再次确认。'}</p>
    ${items.length?`<details class="readiness-details" data-focus-section="readiness"><summary>查看待确认事项与填写入口（${items.length} 项）</summary><ul class="reason-list">${items.map(i=>`<li><p>${esc(i.message)}</p><div class="actions">${readinessActions(i,state).map(a=>button(a.label,'navigate','secondary',`data-page="${esc(a.page)}" data-id="${esc(a.id||'')}" data-focus="${esc(a.focus||'')}"`)).join('')}</div></li>`).join('')}</ul></details>`:''}
  </section>${plan?planView(plan,state):''}`;
}
export function setupView(state){
  return `${header('资料与天气','出工前，核对人、工具和天气。')}<div class="two-sections"><section class="panel"><h2>本人、帮手与工具</h2><p>${esc(personStateLabel(state.profile,state.mode))}</p><p class="field-hint">已记 ${state.helpers.length} 位帮手、${state.resources.length} 项工具或条件。</p><div class="actions actions-spaced">${navButton('查看与修改资料','people')}</div></section><section class="panel"><h2>天气与出工依据</h2><p>${esc(state.settings.place)} · ${esc(state.settings.planning_date)}</p><p class="field-hint">查看地块天气，核对预警、来源与适用范围。</p><div class="actions actions-spaced">${navButton('查看天气与来源','weather')}</div></section></div><details class="panel"><summary>调整安排日期与时间</summary>${settingsForm(state)}</details>`;
}
export function estimateText(task,estimates){
  const estimate=estimates?.tasks?.find(e=>e.task_id===task.id);
  if(!estimate?.workers?.length)return '<p class="field-hint">净工作用时待估计。</p>';
  return `<details class="compact-details"><summary>估时与来源</summary>${estimate.workers.map(w=>`<p class="field-hint">按已填估计，${esc(w.worker_name||w.name||'这位人员')}净干约 ${numberText(w.net_minutes_low)}—${numberText(w.net_minutes_high)} 分钟${w.overhead_minutes?`，每次另有准备和往返约 ${numberText(w.overhead_minutes)} 分钟`:''}。${w.source?`来源：${esc(rateSourceLabel(w.source))}`:''}</p>`).join('')}</details>`;
}
function planView(plan,state) {
  const invalid=plan.stale || !plan.displayable;
  if(invalid)return `<section class="panel"><h2>上一次的安排需要重算</h2>${note('资料、天气或实际完成量已经变化。请重新确认后再查看安排，旧时间轴不作为当前建议。','warning')}${button('根据现在的记录重新安排','plan','secondary')}</section>`;
  const sessions=plan.sessions||[];
  const incomplete=(plan.task_results||[]).filter(t=>Number(t.remaining_quantity)>0);
  const warnings=noticeMessages(plan.display_warnings||[]);
  const actions=(plan.alternatives||[]).flatMap(a=>a.actions?.length?a.actions:[textOf(a)||a.description||a.title]).filter(Boolean);
  return `<section class="panel" id="plan-result"><h2>${state.mode==='demonstration'?'演示安排草稿':'安排草稿'}</h2><p class="muted">${datetimeText(plan.created_at)} 生成 · ${state.mode==='demonstration'?'以下使用假设天气与人员条件':'仍需结合身体感觉、现场和最新预警一起判断'}</p>
  ${note(state.mode==='demonstration'?'这是虚构演示，不可照着去做农活。实际安排要重新填写并确认自己的情况。':'安排只考虑了已填写的条件，不能保证个人健康安全。觉得不舒服时，请先停下农活。','warning')}
  ${warnings.slice(0,2).map(w=>note(w,'warning')).join('')}
  ${sessions.length?`<details class="plan-details" open><summary>查看时间安排（${sessions.length} 个时段）</summary>${sessions.map((s,index)=>{
    const task=state.tasks.find(t=>t.id===s.task_id),worker=allPeople(state).find(w=>w.id===s.worker_id);
    const phases=s.phases||s.segments||[];
    const duration=kind=>phases.filter(p=>p.kind===kind).reduce((sum,p)=>sum+(new Date(p.end)-new Date(p.start))/60000,0);
    return `<article class="session-summary"><div class="session-head"><p class="session-time">${timeText(s.start)}—${timeText(s.end)}</p><h3>${esc(s.task_name||task?.name||'农活')} · ${esc(s.worker_name||worker?.name||'本人')}</h3><p>本段拟完成 ${quantity(s.quantity,s.unit)}</p><p class="field-hint">净工作 ${numberText(duration('work'))} 分钟 · 休息 ${numberText(duration('rest')+duration('initial_rest'))} 分钟 · 另含准备、往返与收尾</p></div><details class="session-phases"><summary>查看工作与休息的每一段（${phases.length} 段）</summary><ol class="timeline">${phases.map(p=>`<li class="${p.kind==='rest'?'rest':''}"><time datetime="${esc(p.start)}">${timeText(p.start)}</time><div class="phase-body"><h3>${esc(p.label||phaseNames[p.kind]||'活动阶段')}</h3><p>到 ${timeText(p.end)}${p.kind==='work'?` · ${esc(s.task_name||task?.name||'农活')}`:''}</p></div></li>`).join('')}</ol></details></article>`;
  }).join('')}</details>`:note('在目前填写的条件下，这次没有排出工作时段。先看下面尚未安排的部分，一起调整。','warning')}
  <h3 class="subheading">${incomplete.length?'未安排的部分':'这次登记的剩余农活均已放入草稿'}</h3>${incomplete.map(t=>`<div class="remaining-row"><strong>${esc(t.name||state.tasks.find(x=>x.id===t.task_id)?.name||'农活')}</strong><p><span class="remaining-number">${quantity(t.remaining_quantity,t.unit)}</span> 尚未安排</p><p class="field-hint">本次拟安排 ${quantity(t.scheduled_quantity,t.unit)}；拟安排不等于已经做完。</p>${(t.display_reasons||[]).length?`<ul class="reason-list">${t.display_reasons.map(r=>`<li>${esc(textOf(r))}</li>`).join('')}</ul>`:''}</div>`).join('')}
  ${actions.length?`<h3 class="subheading">可以一起商量的办法</h3><ul>${[...new Set(actions)].map(a=>`<li>${esc(a)}</li>`).join('')}</ul>`:''}
  <div class="actions actions-spaced">${navButton('记录实际完成','feedback','secondary')}${navButton('调整农活','farm')}${navButton('确认帮手与工具','people')}</div><p class="field-hint">没有安排出来的部分不等于一定做不完；可以核实帮手、工具、任务范围或截止日后再算。</p>
  <details><summary>计算详情与原始限制说明</summary><ul class="reason-list">${noticeMessages(plan.warnings).map(w=>`<li>${esc(w)}</li>`).join('')}</ul>${incomplete.map(t=>t.reasons?.length?`<h3>${esc(t.name||state.tasks.find(x=>x.id===t.task_id)?.name||'农活')}</h3><ul class="reason-list">${t.reasons.map(r=>`<li>${esc(textOf(r)||'这一部分需要进一步确认')}</li>`).join('')}</ul>`:'').join('')}</details></section>`;
}
export function farmView(state,catalog,estimates) {
  return `${header('我的农活','先认识地块，再单独记录每项还没做完的农活。',navButton('添加地块','plot','primary'))}
  <div class="subheading"><h2>我的地块</h2></div>${state.plots.length?`<div class="record-list">${state.plots.map(p=>`<article class="record-row"><div><h3 class="record-title">${esc(p.name)}</h3><p class="record-meta">${esc(cropLabel(p,catalog))}${p.crop_id==='unknown'?'<span class="pending-tag">具体作物待确认</span>':''} · ${quantity(p.area.value,p.area.unit)} · ${p.environment==='greenhouse'?'棚内':'露天'}</p><p class="field-hint">${esc(regionLabel(p.region_id||state.settings.place))} · ${esc(stageLabel(p.stage))}${p.latitude==null||p.longitude==null?' · 天气地点待补充':''}</p></div><div class="actions">${editButton('修改地块','plot',p.id)}</div></article>`).join('')}</div>`:'<div class="empty-small">还没有地块。先添加一块地，名称和面积记清楚就可以开始。</div>'}
  <div class="subheading"><h2>剩余农活</h2>${state.plots.length?navButton('添加农活','task','primary'):''}</div>${state.tasks.length?`<div class="record-list">${state.tasks.map(t=>`<article class="record-row"><div><h3 class="record-title">${esc(t.name)}</h3><p class="record-meta">${esc(state.plots.find(p=>p.id===t.plot_id)?.name||'地块')} · 还剩 ${quantity(remainingFor(t,estimates).value,remainingFor(t,estimates).unit)}</p><p class="field-hint">${t.deadline?`最晚 ${esc(t.deadline.slice(0,10))}`:'截止日期待确认'}${t.remaining_quantity.unit==='trip'?` · 每趟 ${t.load_per_trip_kg==null?'重量待确认':quantity(t.load_per_trip_kg,'kg')} · 单程 ${t.distance_m==null?'距离待确认':quantity(t.distance_m,'m')}`:''}</p>${estimateText(t,estimates)}${t.notes?`<p class="record-note">${esc(t.notes)}</p>`:''}</div><div class="actions">${editButton('修改','task',t.id)}${editButton('记完成量','feedback',t.id)}${editButton('用记录核对估时','review',t.id)}${editButton('出工前留存估时','prediction',t.id)}</div></article>`).join('')}</div>`:'<div class="empty-small">还没有农活。地块总面积不会自动当成待完成量。</div>'}`;
}
export function peopleView(state) {
  return `${header('我与帮手','身体感觉、可用时间和实际工具，分别确认。')}
  <div class="two-sections"><section class="panel"><h2>${esc(state.profile.name||'本人')}</h2><p><strong>${esc(personStateLabel(state.profile,state.mode))}</strong></p><p class="field-hint">${state.profile.state_checked_at?`记录于 ${datetimeText(state.profile.state_checked_at)}`:'今天还没记录身体情况'}</p>${state.profile.state==='stop'?note('今天先不做农活。安排不会给本人派活。','stop'):''}<p class="record-note">${esc(state.profile.limitations_note||'尚未填写已有活动限制。')}</p><p class="field-hint">约束来源：${esc(state.profile.limits?.source||'待确认')}</p><p class="field-hint">${availabilityText(state.profile)}</p><div class="actions actions-spaced">${editButton('记录我的情况','person',state.profile.id)}</div></section>
  <section class="panel"><h2>帮手</h2>${state.helpers.length?`<ul class="mini-list">${state.helpers.map(h=>`<li><strong>${esc(h.name)}</strong><p class="field-hint">${esc(personStateLabel(h,state.mode))}</p><p class="field-hint">${h.state_checked_at?`记录于 ${datetimeText(h.state_checked_at)}`:'还未记录身体情况'}</p><p class="field-hint">${availabilityText(h)}</p>${editButton('修改帮手','person',h.id)}</li>`).join('')}</ul>`:'<p class="muted">还没有记下帮手。先联系确认，再填写能来的时段。</p>'}<div class="actions actions-spaced">${navButton('添加帮手','helper')}</div></section></div>
  <div class="subheading" data-focus-section="resources" tabindex="-1"><h2>工具、饮水与休息处</h2>${navButton('添加可用条件','resource','primary')}</div>${state.resources.length?`<div class="record-list">${state.resources.map(r=>`<article class="record-row"><div><h3>${esc(r.name)}</h3><p class="record-meta">${{tool:'工具',water:'饮水',rest_place:'休息处'}[r.kind]||'可用条件'} · ${r.confirmed&&r.availability?.length?'已经实际确认时段':'还未确认可用'}</p><p class="field-hint">${availabilityText(r)}</p></div>${editButton('修改','resource',r.id)}</article>`).join('')}</div>`:'<div class="empty-small">把能喝水、能休息的地方也记下来；空白不会自动当成可用。</div>'}`;
}
function availabilityText(person){return person.availability?.length?person.availability.map(w=>`${esc(w.start.slice(0,10))} ${timeText(w.start)}—${timeText(w.end)}`).join('；'):'可用时段待确认';}
export function weatherView(state,now=Date.now()) {
  return `${header('天气与预警','看清地点、日期和更新时间；天气预报与官方预警分开核对。')}${state.mode==='demonstration'?note('演示区使用合成天气，不代表任何地点的真实天气。','warning'):note('更新天气时只发送地块坐标，不发送个人身体资料。')}
  ${state.plots.length?state.plots.map(p=>{
    const w=state.weather[p.id];
    const coordinatesValid=Number.isFinite(p.latitude)&&Number.isFinite(p.longitude)&&Math.abs(p.latitude)<=90&&Math.abs(p.longitude)<=180;
    const records=w?.records||[];
    const shown=records.filter(r=>r.start?.startsWith(state.settings.planning_date));
    const temperatures=shown.map(r=>r.temperature_c).filter(t=>t!==null&&t!==undefined&&Number.isFinite(Number(t)));
    const reference=w?.forecast_run_snapshot?.initialised_at||w?.issued_at;
    const age=reference?(now-new Date(reference).getTime())/60000:null;
    const invalidTime=state.mode!=='demonstration'&&w&&((age!==null&&!Number.isFinite(age))||(age!==null&&age<0)||('forecast_run_snapshot' in w&&!w.forecast_run_snapshot?.initialised_at));
    const old=w && (w.stale || (state.mode!=='demonstration'&&Number.isFinite(age)&&age>state.policy.max_forecast_age_minutes));
    return `<section class="panel" data-focus-section="weather:${esc(p.id)}" tabindex="-1"><div class="page-heading"><div><h2>${esc(p.name)}</h2><p>${esc(state.settings.place)} · ${coordinatesValid?`${esc(p.latitude)}, ${esc(p.longitude)}`:'坐标待补充'} · ${p.environment==='greenhouse'?'棚内':'露天'}</p></div>${!coordinatesValid?editButton('先补充地块坐标','plot',p.id):state.mode==='real'?button('更新这块地天气','weather-refresh','primary',`data-id="${esc(p.id)}"`):''}</div>
    ${!w?note('这块地尚未更新天气。没有天气数据时，不会凭空安排工作时段。','warning'):''}
    ${p.environment==='greenhouse'?note('露天预报不能代表棚内条件。棚内作业仍需适用的现场记录。','warning'):''}
    ${old?note('这份天气已过期，请更新后再使用。','warning'):''}${invalidTime?note('这份天气的时间待核对，请重新取得资料。','warning'):''}
    ${w?`<dl class="data-pairs"><dt>数据来源</dt><dd class="weather-source">${esc(typeof w.source==='string'?w.source:w.source?.name||'来源待确认')}</dd><dt>查看日期</dt><dd>${esc(state.settings.planning_date)}</dd><dt>预警情况</dt><dd>${esc(alertStatusText(w,now,state.mode))}</dd></dl><details data-forecast-times><summary>预报时间与来源</summary><dl class="data-pairs">${forecastTimeRows(w,{demonstration:state.mode==='demonstration'})}</dl>${noticeMessages(w.warnings).map(m=>note(m,'warning')).join('')}</details>${note('这里的气象模式预报不等于官方预警。预警未知时，请先核对当地气象部门或乡镇通知。','warning')}${alertQueryView(w,now,state.mode)}
    ${shown.length?`<div class="weather-strip"><span class="temperature">${temperatures.length?`${numberText(Math.min(...temperatures))}—${numberText(Math.max(...temperatures))}℃`:'气温待确认'}</span><span>${esc(state.settings.planning_date)} 的逐时预报</span></div><details><summary>展开逐小时天气（${shown.length} 个时段）</summary><div class="table-scroll"><table><caption class="visually-hidden">${esc(p.name)} ${esc(state.settings.planning_date)} 逐时天气</caption><thead><tr><th scope="col">时段</th><th scope="col">气温</th><th scope="col">湿度</th><th scope="col">风速</th><th scope="col">小时降水</th><th scope="col">昼夜</th></tr></thead><tbody>${shown.map(r=>`<tr><td>${timeText(r.start)}—${timeText(r.end)}</td><td>${numberText(r.temperature_c)}℃</td><td>${numberText(r.relative_humidity_pct)}%</td><td>${numberText(r.wind_m_s)} 米/秒</td><td>${numberText(r.precipitation_mm)} 毫米</td><td>${r.daylight?'白天':'夜间'}</td></tr>`).join('')}</tbody></table></div></details>`:note('这份天气没有覆盖当前安排日期，请调整日期或更新天气。','warning')}`:''}
    ${coordinatesValid?`<div class="actions actions-spaced">${editButton('修改地块与坐标','plot',p.id)}</div>`:''}${state.mode==='real'?weatherImportPanel(p):''}</section>`;
  }).join(''):'<div class="empty-small">先添加地块，填好地点后才能更新对应天气。</div>'}
  ${settingsForm(state)}<section class="panel">${policyForm(state)}</section>`;
}
export function forecastTimeRows(weather,{importPreview=false,demonstration=false}={}) {
  const shown=value=>value?datetimeText(value):'尚未提供';
  const run=weather.forecast_run_snapshot;
  if(demonstration)return `<dt>示例设定时间</dt><dd>${shown(weather.issued_at||run?.initialised_at)} · 仅用于合成演示</dd>`;
  if('forecast_run_snapshot' in weather){
    return `<dt>模型起报</dt><dd>${shown(run?.initialised_at)}</dd><dt>本次资料取得</dt><dd>${shown(run?.retrieved_at)}</dd><dt>预报发布</dt><dd>${weather.issued_at===null?'预报发布时间未提供':'预报时间字段待核对'}</dd>${!importPreview&&weather.imported_at?`<dt>本次导入收到</dt><dd>${shown(weather.imported_at)}</dd>`:''}`;
  }
  const originalUnknown=weather.retrieved_time_basis==='import_received_original_unknown'||(importPreview&&!weather.retrieved_at);
  return `<dt>${esc(originalUnknown?(importPreview?'文件中记录的导入收到':'本次导入收到'):weather.retrieved_time_basis==='input_declaration_original_retrieval'?'原资料取得（导入声明）':importPreview?'原资料取得':storageText('weatherReceived'))}</dt><dd>${shown(weather.retrieved_at)}</dd>${originalUnknown?'<dt>原资料取得时间</dt><dd>尚未提供；导入时间不代表原取得时间</dd>':''}<dt>预报发布</dt><dd>${weather.issued_at?datetimeText(weather.issued_at):'来源未给出发布时间，不能用下载时间代替'}</dd>${!importPreview&&!originalUnknown&&weather.imported_at?`<dt>本次导入收到</dt><dd>${shown(weather.imported_at)}</dd>`:''}`;
}
const alertText=status=>({unknown:'尚未核对当地官方预警',not_checked:'尚未核对当地官方预警',unavailable:'未取得官方预警',known:'已有预警资料，请核对来源',clear:'该次查询未见生效预警，出工前仍需更新',queried_clear:'该次查询未见生效预警，出工前仍需更新',active_alerts:'该次查询有生效预警，请查看当地最新预警与行动要求',feed_unavailable:'暂未取得预警查询结果，请重新查询',query_incomplete:'预警查询不完整，不能判断当前预警情况',not_queried:'尚未查询当地官方预警',synthetic_declared:'演示预警设定，不代表当地真实预警'}[status]||'尚未取得完整的当地官方预警');
function alertStatusText(weather,now,mode){
  if(mode==='demonstration')return alertText('synthetic_declared');
  const feed=weather.alert_feed;
  const status=feed?.coverage_status||(typeof weather.alert_status==='object'?weather.alert_status?.status:weather.alert_status);
  if(status==='synthetic_declared')return alertText(status);
  const queried=typeof feed?.queried_at==='string'?Date.parse(feed.queried_at):NaN;
  if(Number.isFinite(queried)&&now-queried>60*60000)return `预警查询已过期，需更新${status==='active_alerts'?'；上次查询有生效预警':''}`;
  if(['queried_clear','clear','active_alerts'].includes(status)){
    const unknownTime=!Number.isFinite(queried),futureTime=queried>now;
    if(unknownTime||futureTime)return `${status==='active_alerts'?'该次查询有生效预警；':''}${futureTime?'预警查询时间异常，需重新查询':'预警查询时间待确认，不能判断时效'}`;
  }
  return alertText(status);
}
function alertQueryView(weather,now,mode){
  const feed=weather.alert_feed;
  if(!feed||typeof feed!=='object')return '';
  const snapshot=feed.query_snapshot;
  const declaredImport=weather.provenance?.verification==='input_declaration_not_independent_authentication';
  const sourceNote=declaredImport?'此快照的来源属于导入声明，应用未独立认证。':snapshot?.schema==='qweather-alert-query-1'?'查询原文用于核对内容；导入文件本身不能证明来源。':'';
  const sources=Array.isArray(feed.attributions)?feed.attributions.filter(item=>typeof item==='string'):[];
  const shownTime=value=>typeof value==='string'&&Number.isFinite(Date.parse(value))?`<time datetime="${esc(value)}">${datetimeText(value)}</time>`:'来源未提供';
  return `<details data-alert-query><summary>预警查询与来源</summary>${mode==='demonstration'?'<p class="field-hint">下列时间均为示例设定，不表示真实查询。</p>':''}<dl class="data-pairs"><dt>当前状态</dt><dd>${esc(alertStatusText(weather,now,mode))}</dd><dt>查询时间</dt><dd>${shownTime(feed.queried_at)}</dd><dt>响应收到时间</dt><dd>${shownTime(snapshot?.retrieved_at)}</dd><dt>预警列表发布时间</dt><dd>${shownTime(feed.issued_at)}</dd></dl>${sources.length?`<ul class="weather-source">${sources.map(source=>`<li>${esc(source)}</li>`).join('')}</ul>`:'<p class="field-hint">来源归因尚未提供。</p>'}${sourceNote?`<p class="field-hint">${esc(sourceNote)}</p>`:''}</details>`;
}
function weatherImportPanel(plot){
  return `<details><summary>导入一份已核对来源的天气快照</summary><p class="form-intro">已有带来源的天气原始记录时，可以导入到这块地。请先核对地点、预报发布时间或固定模型起报、实际取得时间、预警来源及适用范围。应用只检查文件内容与格式，不认证来源。${esc(storageText('weatherImportTransfer'))}</p><label for="weather-file-${esc(plot.id)}">选择这块地的天气快照文件</label><input id="weather-file-${esc(plot.id)}" type="file" accept="application/json,.json" data-weather-file="${esc(plot.id)}"><p class="field-hint">文件上限 ${esc(requestBodyLimitText())}；提交后以服务端最终检查为准。文件需含原始逐时记录、来源网址、核对人、核对时间和发布时间或模型起报依据。空的预警字段不会当作没有预警。</p><div data-weather-preview="${esc(plot.id)}"></div></details>`;
}
export function recordsView(state) {
  return `${header(storageText('recordsTitle'),storageText('recordsDescription'),state.tasks.length?navButton('新增完成记录','feedback','primary'):'')}
  ${state.feedback.length?`<div class="record-list">${[...state.feedback].reverse().map(f=>`<article class="record-row"><div><h3>${esc(f.task_name||state.tasks.find(t=>t.id===f.task_id)?.name||'农活')}</h3><p class="record-meta">${esc(allPeople(state).find(w=>w.id===f.worker_id)?.name||'人员')} · ${feedbackStatus(f.status)} · ${f.completed_quantity?.value==null?'实际完成量尚不清楚':`本次完成 ${quantity(f.completed_quantity.value,f.completed_quantity.unit)}`}</p><p class="field-hint">${f.started_at?datetimeText(f.started_at):f.status==='not_done'&&!f.ended_at?'未开始，无作业时段':'实际开始时间待确认'}${f.net_minutes==null?'':` · 净干活 ${numberText(f.net_minutes)} 分钟`}</p><p class="field-hint">完成量来源：${sourceName(f.quantity_source)}；用时来源：${sourceName(f.clock_source)}</p>${f.notes?`<p class="record-note">${esc(f.notes)}</p>`:''}${state.feedback.some(e=>e.supersedes_event_id===f.event_id)?'<p class="field-hint">这条记录已被后续记录更正，保留作核对，不重复计入完成量。</p>':`<div class="actions actions-spaced">${editButton('更正这条记录','correction',f.event_id)}</div>`}</div></article>`).join('')}</div>`:'<div class="empty-small">还没有实际完成记录。演示或安排里写“完成”，都不会自动变成真实记录。</div>'}
  <details class="panel"><summary>事前估时与实际对照</summary><p>按自己明确选择的对应关系，查看出工前留存的估时和事后实际记录。</p><div class="actions actions-spaced">${button('查看已留存估时与实际对照','predictions-review','secondary')}</div></details>
  <details class="panel"><summary>备份与恢复</summary><div class="two-sections"><section class="panel"><h2>导出一份备份</h2><p>包含${state.mode==='demonstration'?'演示':'当前真实'}资料、天气、安排历史和实际完成记录。</p><p class="field-hint">上次保存：${datetimeText(state.updated_at)}。导出文件含个人记录，请保存在自己信任的位置。</p><div class="actions actions-spaced">${button(storageText('exportButton'),'export','secondary')}</div></section><section class="panel"><h2>从备份恢复</h2><p>${esc(storageText('restoreIntro'))}</p><div class="field-group"><label for="import-file">选择宜老农业备份文件</label><input type="file" id="import-file" accept="application/json,.json"></div><p class="field-hint">文件上限 ${esc(requestBodyLimitText())}；提交后以服务端最终检查为准。</p><div id="import-preview"></div></section></div></details>
  <details class="panel"><summary>整理实测材料</summary><p>去除姓名、坐标和自由文字；保留农活时间与数量。仍需核对，不能直接当作验证样本。</p><p class="field-hint">同一人在不同账号的材料需人工核对；更换版本后编号可能变化，不可自动合并。</p>${state.mode==='demonstration'?note('下载的是演示材料，不能当作真实实测记录。','warning'):''}<div class="actions actions-spaced">${button('下载待复核记录','field-collection-export','secondary')}</div></details>
  <div class="actions actions-spaced">${button(state.mode==='demonstration'?'返回我的记录':'先看独立演示',state.mode==='demonstration'?'exit-demo':'enter-demo','tertiary')}<p class="field-hint">演示与自己的资料分开保存。</p></div>`;
}

const feedbackStatus=s=>({completed:'记录为做完',partial:'做了一部分',not_done:'没有开始',interrupted:'中途停下',unknown:'情况待确认'}[s]||'情况待确认');
const sourceName=s=>esc(({self_report:'本人自述',family_report:'家属记录',measured:'实际清点或测量',clock_record:'钟表或计时记录',unknown:'待确认'})[s]||s||'待确认');
