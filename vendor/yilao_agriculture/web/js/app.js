import {request,requestBodyLimit,requestBodyLimitText} from './api.js';
import {esc,options,button,showNotice,getForm,makeId,downloadJSON,quantity,units,note,datetimeText} from './ui.js';
import {plotForm,taskForm,personForm,resourceForm,feedbackForm,methodLabel} from './forms.js';
import {todayView,farmView,peopleView,weatherView,recordsView,setupView,forecastTimeRows} from './views.js';
import {editPlot,editTask,editPerson,editResource,editSettings,editPolicy,feedbackPayload,remainingFor} from './model.js';
import {reviewView,adoptionPayload} from './review.js';
import {stageLabel} from './display.js';
import {predictionView,predictionsView,predictionQuery,freezePayload,predictionBindingField} from './predictions.js';
import {storageText,savedLocation,applyStorageFrame} from './context.js';
import {navigationHtml,isAppEvent,parseAppRoute} from './navigation.js';

let state,catalog,estimates,readiness,rateReview=null,route={page:'today',id:''},busy=false,importBundle=null;
let predictionPreview=null,predictionReview=null;
const weatherSnapshots=new Map();
const appRoot=document.getElementById('agri-app');
const appElement=id=>appRoot.querySelector(`[id="${id}"]`);
const main=appElement('agri-main');
applyStorageFrame();

function readRoute(){return parseAppRoute(location.hash)||{page:'today',id:'',focus:''};}
function navigate(page,id='',focus=''){
  if(busy)return;
  const hash=`#${page}${id?'/'+encodeURIComponent(id):''}${focus?'?field='+encodeURIComponent(focus):''}`;
  if(location.hash===hash){route={page,id,focus};render(true);fillCorrection();focusRoute();}else location.hash=hash;
}
function render(focus=false){
  if(!state||!catalog)return;
  const page=route.page;
  appElement('agri-main-nav').innerHTML=navigationHtml(page);
  const settingsLink=appRoot.querySelector('.settings-link');
  if(['setup','people','weather','person','helper','resource'].includes(page))settingsLink.setAttribute('aria-current','page');else settingsLink.removeAttribute('aria-current');
  appElement('agri-mode-label').textContent=storageText(state.mode==='demonstration'?'demoModeLabel':'modeLabel');
  appElement('agri-mode-banner').innerHTML=state.mode==='demonstration'?`<div><span><strong>正在看演示</strong> · ${esc(storageText('demoDescription'))}</span><button type="button" class="demo-exit" data-action="exit-demo">返回我的记录</button></div>`:'';
  const pages={today:()=>todayView(state,readiness,estimates),farm:()=>farmView(state,catalog,estimates),people:()=>peopleView(state),weather:()=>weatherView(state),records:()=>recordsView(state),setup:()=>setupView(state),plot:()=>plotForm(state,catalog,route.id),task:()=>state.plots.length?taskForm(state,catalog,route.id):plotForm(state,catalog),person:()=>personForm(state,route.id||state.profile.id),helper:()=>personForm(state,'new'),resource:()=>resourceForm(state,route.id),feedback:()=>feedbackForm(state,route.id),correction:()=>correctionView(route.id),review:()=>reviewView(state,route.id,rateReview),prediction:()=>predictionView(state,route.id,estimates,predictionPreview),predictions:()=>predictionsView(state,predictionReview)};
  main.innerHTML=(pages[page]||pages.today)();
  decorateRemaining();
  if(focus){main.focus({preventScroll:true});main.scrollIntoView({block:'start',behavior:'instant'});}
  focusRoute();
}
function focusRoute(){
  if(!route.focus)return;
  const target=[...main.querySelectorAll('[name],[data-focus-section]')].find(el=>el.name===route.focus||el.dataset.focusSection===route.focus);
  if(!target)return;
  for(let parent=target.parentElement;parent&&parent!==main;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
  if(target.tagName==='DETAILS')target.open=true;
  const focusTarget=target.tagName==='DETAILS'?target.querySelector('summary'):target;
  focusTarget?.focus({preventScroll:true});target.scrollIntoView({block:'center',behavior:'instant'});
}
function decorateRemaining(){
  const form=main.querySelector('form[data-form="feedback"]');
  if(form){
    form.dataset.eventId=makeId('event');
    updateFeedbackUnit(form);
  }
}
function correctionView(eventId){
  const original=state.feedback.find(f=>f.event_id===eventId);
  if(!original||state.feedback.some(f=>f.supersedes_event_id===eventId))return `<h1>这条记录已经更新</h1>${note(storageText('findLatestRecord'),'warning')}${button(storageText('backToRecords'),'navigate','secondary','data-page="records"')}`;
  return feedbackForm(state,original.task_id,original).replace('记录实际做了多少','更正这条完成记录').replace('请按实际回忆填写，安排里的数量不会自动当作已经完成。','原记录会保留。这次保存新的更正记录后，剩余量按最新的记录重新计算。').replace('data-form="feedback"',`data-form="feedback" data-supersedes="${esc(eventId)}"`).replace('保存完成记录','保存更正记录');
}
function fillCorrection(){
  if(route.page!=='correction')return;
  const original=state.feedback.find(f=>f.event_id===route.id),form=main.querySelector('form[data-form="feedback"]');
  if(!original||!form)return;
  for(const key of ['task_id','worker_id','status','net_minutes','rest_minutes','quantity_source','clock_source','notes'])if(form.elements[key])form.elements[key].value=original[key]??'';
  for(const key of ['started_at','ended_at'])if(original[key])form.elements[key].value=toLocalInput(original[key]);
  form.elements.completed_quantity.value=original.completed_quantity?.value??'';
  form.elements.task_id.disabled=true;form.elements.worker_id.disabled=true;
  form.elements.context_matches_task.disabled=true;
  form.elements.consent.checked=false;
  updateFeedbackUnit(form);
}
function toLocalInput(value){const d=new Date(value);return new Date(d.getTime()+8*3600000).toISOString().slice(0,16);}
function updateFeedbackUnit(form){
  const task=state.tasks.find(t=>t.id===form.elements.task_id.value);if(!task)return;
  const q=remainingFor(task,estimates),label=form.querySelector('label[for="field-completed_quantity"]');
  if(label)label.textContent=`这次实际完成多少（${units[q.unit]||q.unit}）`;
  const hint=form.querySelector('#field-completed_quantity-hint');
  if(hint)hint.textContent=`按已有记录还剩 ${quantity(q.value,q.unit)}。情况或数量不清楚时，请选择「情况还不清楚」并把完成量留空。`;
  form.elements.completed_quantity.step=['trip','plant'].includes(q.unit)?'1':'any';
}
async function refreshEstimates(){
  [estimates,readiness]=await Promise.all([request(`/api/estimates?mode=${state.mode}`),request(`/api/readiness?mode=${state.mode}`)]);
  for(const row of estimates.tasks||[])for(const w of row.workers||[])w.worker_name=[state.profile,...state.helpers].find(x=>x.id===w.worker_id)?.name||'这位人员';
}
async function initialLoad(){
  try{
    const loaded=await Promise.all([request('/api/state?mode=real'),request('/api/catalog')]);
    [state,catalog]=loaded;await refreshEstimates();route=readRoute();render();fillCorrection();
  }catch(error){main.innerHTML=`<div class="loading-state"><h1>暂时没有打开记录</h1>${note(error.message,'error')}${button('重新打开','reload','primary')}</div>`;}
}
function setBusy(flag,trigger){
  busy=flag;
  if(trigger){trigger.disabled=flag;if(flag){trigger.dataset.previousText=trigger.textContent;trigger.textContent='正在处理…';}else if(trigger.dataset.previousText){trigger.textContent=trigger.dataset.previousText;delete trigger.dataset.previousText;}}
  main.setAttribute('aria-busy',String(flag));
}
async function runAction(trigger,callback){
  if(busy)return;
  setBusy(true,trigger);
  try{await callback();}catch(error){showNotice(error.message,'error');appElement('agri-notice').scrollIntoView({block:'start'});}finally{setBusy(false,trigger);}
}
async function saveForm(form,submitter){
  if(busy)return;
  const type=form.dataset.form,data=getForm(form);const box=form.querySelector('.form-error');
  if(box)box.textContent='';
  setBusy(true,submitter);
  try{
    if(type==='prediction-preview'){
      const query=predictionQuery(state,data);
      const preview=await request('/api/predictions/preview?'+new URLSearchParams(query));
      predictionPreview={mode:state.mode,revision:state.revision,query,preview};
      route={page:'prediction',id:data.task_id};location.hash=`#prediction/${encodeURIComponent(data.task_id)}`;render();
      appElement('prediction-preview-result')?.scrollIntoView({block:'start'});return;
    }
    if(type==='prediction-freeze'){
      const queryForm=main.querySelector('form[data-form="prediction-preview"]');
      if(!queryForm)throw new Error('请先查看这一次的估时。');
      const payload=freezePayload(state,predictionPreview,data,getForm(queryForm));
      state=await request('/api/predictions/freeze',{method:'POST',body:payload});
      predictionPreview=null;predictionReview=null;
      let notice='这份事前估时已留存。实际完成后，请如实记录并明确选择对应的这份估时。';
      try{const review=await request(`/api/predictions/review?mode=${state.mode}`);predictionReview={mode:state.mode,revision:state.revision,review};}
      catch{notice='事前估时已留存，暂未读到最新实际对照；可以点击“查看已留存估时与实际对照”重试。';}
      route={page:'predictions',id:''};location.hash='#predictions';render(true);showNotice(notice);return;
    }
    if(type==='rate-review'){
      const result=await request(`/api/rate-review?${new URLSearchParams({mode:state.mode,task_id:data.task_id,worker_id:data.worker_id})}`);
      rateReview={mode:state.mode,revision:state.revision,taskId:data.task_id,workerId:data.worker_id,review:result.review};
      route={page:'review',id:data.task_id};location.hash=`#review/${encodeURIComponent(data.task_id)}`;render();
      appElement('rate-review-result')?.scrollIntoView({block:'start'});return;
    }
    if(type==='rate-adopt'){
      const payload=adoptionPayload(state,rateReview,data);
      const result=await request('/api/rate-review/adopt',{method:'POST',body:payload});state=result.state;
      rateReview=null;await refreshEstimates();
      showNotice('新的自述估时已保存，原估时和采用依据仍有记录。安排需要按当前条件重新计算。');
      render();return;
    }
    if(type==='feedback'){
      // 更正固定为原农活与原人员，避免误把其他记录覆盖。
      if(form.dataset.supersedes){data.task_id=form.elements.task_id.value;data.worker_id=form.elements.worker_id.value;}
      const payload=feedbackPayload(state,data,form.dataset.eventId,form.dataset.supersedes);
      const serialized=JSON.stringify({...payload,revision:undefined,event_id:undefined});
      if(form.dataset.lastSubmission&&form.dataset.lastSubmission!==serialized){form.dataset.eventId=makeId('event');payload.event_id=form.dataset.eventId;}
      form.dataset.lastSubmission=serialized;
      const result=await request('/api/feedback',{method:'POST',body:payload});state=result.state;
      await refreshEstimates();
      showNotice(`完成记录已保存在${savedLocation(state.mode)}。按记录还剩 ${quantity(result.remaining.value,result.remaining.unit)}；原安排需要重新计算。`);
      route={page:'today',id:''};location.hash='#today';render(true);return;
    }
    let candidate;
    if(type==='plot')candidate=editPlot(state,data,form.dataset.id);
    else if(type==='task')candidate=editTask(state,data,form.dataset.id,catalog);
    else if(type==='person')candidate=editPerson(state,data,form.dataset.id,form.dataset.self==='true');
    else if(type==='resource')candidate=editResource(state,data,form.dataset.id);
    else if(type==='settings')candidate=editSettings(state,data);
    else if(type==='policy')candidate=editPolicy(state,data);
    else throw new Error('没有识别这张表单，请重新打开页面。');
    state=await request('/api/state',{method:'PUT',body:candidate});readiness=null;await refreshEstimates();
    showNotice(`${{plot:'地块',task:'农活',person:'人员情况',resource:'可用条件',settings:'日期与时间',policy:'已有依据'}[type]}已保存在${savedLocation(state.mode)}。`);
    const page={plot:'farm',task:'farm',person:'people',resource:'people',settings:route.page,policy:'weather'}[type];
    route={page,id:''};location.hash=`#${page}`;render(true);
  }catch(error){
    if(box){box.textContent=error.message;box.focus();}else showNotice(error.message,'error');
  }finally{setBusy(false,submitter);}
}

document.addEventListener('submit',event=>{if(!isAppEvent(appRoot,event))return;const form=event.target.closest('form[data-form]');if(form){event.preventDefault();saveForm(form,event.submitter);}});
document.addEventListener('click',event=>{
  if(!isAppEvent(appRoot,event))return;
  const trigger=event.target.closest('[data-action]');if(!trigger)return;
  const action=trigger.dataset.action;
  if(action==='navigate'){navigate(trigger.dataset.page,trigger.dataset.id||'',trigger.dataset.focus||'');return;}
  if(action==='reload'){initialLoad();return;}
  if(action==='locate'){locate(trigger);return;}
  if(!['enter-demo','exit-demo','plan','weather-refresh','weather-import','export','import','predictions-review','field-collection-export'].includes(action))return;
  runAction(trigger,async()=>{
    if(action==='predictions-review'){
      const review=await request(`/api/predictions/review?mode=${state.mode}`);
      predictionReview={mode:state.mode,revision:state.revision,review};route={page:'predictions',id:''};location.hash='#predictions';render(true);
    }else if(action==='enter-demo'){
      state=await request('/api/demo',{method:'POST',body:{}});readiness=null;await refreshEstimates();
      route={page:'today',id:''};location.hash='#today';render(true);showNotice(storageText('demoEnter'));
    }else if(action==='exit-demo'){
      state=await request('/api/state?mode=real');readiness=null;await refreshEstimates();route={page:'today',id:''};location.hash='#today';render(true);showNotice(storageText('demoExit'));
    }else if(action==='plan'){
      const result=await request('/api/plan',{method:'POST',body:{mode:state.mode,revision:state.revision}});state=result.state;readiness=result.readiness;await refreshEstimates();render();
      showNotice(result.plan?'安排草稿已更新。请查看工作、休息和未安排的部分。':'记录已保留。现在还有信息待确认，请先看问题清单。',result.plan?'success':'warning');
      (appElement('plan-result')||main).scrollIntoView({block:'start',behavior:'smooth'});
    }else if(action==='weather-refresh'){
      const result=await request('/api/weather/refresh',{method:'POST',body:{mode:state.mode,revision:state.revision,plot_id:trigger.dataset.id}});state=result.state;await refreshEstimates();render();showNotice('天气已更新。请查看来源、日期和预警情况；旧安排需要重新计算。');
    }else if(action==='weather-import'){
      const snapshot=weatherSnapshots.get(trigger.dataset.id);
      if(!snapshot)throw new Error('请先选择并核对这块地的天气快照文件。');
      const result=await request('/api/weather/import',{method:'POST',body:{mode:state.mode,revision:state.revision,plot_id:trigger.dataset.id,snapshot}});
      state=result.state;weatherSnapshots.delete(trigger.dataset.id);await refreshEstimates();render();showNotice(storageText('weatherSnapshotSaved'));
    }else if(action==='field-collection-export'){
      const bundle=await request(`/api/field-collection?mode=${state.mode}`);
      downloadJSON(bundle,`宜老农业-${state.mode==='demonstration'?'演示-':''}待复核记录-${new Date().toISOString().slice(0,10)}.json`);
      showNotice('待复核记录已交给浏览器下载，请核对后再使用。');
    }else if(action==='export'){
      const bundle=await request(`/api/export?mode=${state.mode}`);downloadJSON(bundle,`宜老农业-${state.mode==='demonstration'?'演示':storageText('exportFileLabel')}-${new Date().toISOString().slice(0,10)}.json`);showNotice('备份已交给浏览器下载，请在下载位置查看。');
    }else if(action==='import'){
      if(!importBundle)throw new Error('请先选择需要恢复的备份文件。');
      const result=await request('/api/import',{method:'POST',body:{mode:state.mode,revision:state.revision,bundle:importBundle}});state=result.state||result;importBundle=null;readiness=null;await refreshEstimates();render();showNotice(storageText('importComplete'));
    }
  });
});

document.addEventListener('input',event=>{
  if(!isAppEvent(appRoot,event))return;
  if(event.target.closest('form')?.dataset.form==='prediction-preview'){
    predictionPreview=null;appElement('prediction-preview-result')?.remove();
  }
});
document.addEventListener('change',event=>{
  if(!isAppEvent(appRoot,event))return;
  const el=event.target,form=el.closest('form');
  if(el.id==='import-file'){previewImport(el);return;}
  if(el.dataset.weatherFile){previewWeatherImport(el);return;}
  if(!form)return;
  if(form.dataset.form==='prediction-preview'){
    predictionPreview=null;appElement('prediction-preview-result')?.remove();
    if(el.name==='task_id'){
      const task=state.tasks.find(t=>t.id===el.value),q=remainingFor(task,estimates);
      const allowed=['mu','sqm'].includes(q.unit)?[q.unit,...['mu','sqm'].filter(u=>u!==q.unit)]:[q.unit];
      form.elements.quantity_unit.innerHTML=options(allowed.map(value=>({value,label:units[value]||'原计量单位'})),q.unit);
      form.elements.quantity_value.value=q.value??'';
      const hint=form.querySelector('#field-quantity_value-hint');if(hint)hint.textContent=`按已有记录还剩 ${quantity(q.value,q.unit)}。目标量不会当作实际完成量。`;
    }
    form.elements.quantity_value.step=['plant','trip'].includes(form.elements.quantity_unit.value)?'1':'any';
  }
  if(form.dataset.form==='prediction-freeze'&&el.name==='confirmed')form.querySelector('button[type="submit"]').disabled=!el.checked;
  if(form.dataset.form==='rate-review'){
    rateReview=null;appElement('rate-review-result')?.remove();
  }
  if(form.dataset.form==='rate-adopt'&&el.name==='adopt_confirmed')form.querySelector('button[type="submit"]').disabled=!el.checked;
  if(form.dataset.form==='plot'&&el.name==='category_id'){
    const rows=catalog.crops.filter(c=>!el.value||c.category_ids?.includes(el.value));
    form.elements.crop_id.innerHTML=options([{value:'unknown',label:'待确认 / 只知道当地叫法'},...rows.map(c=>({value:c.id,label:c.name}))],'unknown');
  }
  if(form.dataset.form==='task'&&el.name==='task_code'){
    const def=catalog.tasks.find(t=>t.code===el.value);if(!def)return;
    form.elements.method.innerHTML=options((def.methods||[]).map(m=>({value:m,label:def.method_labels?.[m]||methodLabel(m)})),def.method);
    form.elements.unit.innerHTML=options((def.units||def.valid_units||[]).map(u=>({value:u,label:catalog.unit_labels?.[u]||units[u]||u})),def.units?.[0]);
  }
  if(form.dataset.form==='task'&&el.name==='plot_id'&&!form.dataset.id){
    const plot=state.plots.find(p=>p.id===el.value);
    form.elements.task_crop_id.value=plot?.crop_id||'unknown';
    form.elements.task_stage.value=stageLabel(plot?.stage);
  }
  if(form.dataset.form==='task'&&['unit','task_code'].includes(el.name)){
    const trip=form.elements.unit.value==='trip';appElement('carry-fields').hidden=!trip;form.elements.quantity.step=['trip','plant'].includes(form.elements.unit.value)?'1':'any';
  }
  if(form.dataset.form==='feedback'&&el.name==='task_id'){form.elements.completed_quantity.value='';updateFeedbackUnit(form);}
  if(form.dataset.form==='feedback'&&['task_id','worker_id'].includes(el.name)&&!form.dataset.supersedes){
    form.querySelector('[data-prediction-binding]').innerHTML=predictionBindingField(state,form.elements.task_id.value,form.elements.worker_id.value);
  }
  if(form.dataset.form==='feedback'&&el.name==='refresh_context')form.elements.context_matches_task.disabled=!el.checked;
});
async function previewImport(input){
  const out=appElement('import-preview');importBundle=null;
  try{
    const file=input.files?.[0];if(!file){out.innerHTML='';return;}
    if(file.size>requestBodyLimit())throw new Error(`这份文件超过当前上限 ${requestBodyLimitText()}，请选择较小的宜老农业备份。`);
    const bundle=JSON.parse(await file.text());
    if(bundle.format!=='yilao-community-export-v1'||!bundle.state)throw new Error('没有识别到宜老农业备份格式，请重新选择导出的 JSON 文件。');
    if(bundle.state.mode!==state.mode)throw new Error('演示与真实资料不能混在一起，请切换到备份对应的资料区再恢复。');
    importBundle=bundle;
    out.innerHTML=`${note(`这份备份有 ${bundle.state.plots?.length||0} 块地、${bundle.state.tasks?.length||0} 项农活和 ${bundle.state.feedback?.length||0} 条完成记录。${storageText('importPreviewEnding')}`,'warning')}${button('确认从这份备份恢复','import','primary')}`;
  }catch(error){out.innerHTML=note(error instanceof SyntaxError?'这份文件不是完整的 JSON 备份，请重新选择。':error.message,'error');}
}
async function previewWeatherImport(input){
  const plotId=input.dataset.weatherFile;
  const out=[...main.querySelectorAll('[data-weather-preview]')].find(el=>el.dataset.weatherPreview===plotId);
  weatherSnapshots.delete(plotId);
  try{
    const file=input.files?.[0];if(!file){out.innerHTML='';return;}
    if(file.size>requestBodyLimit())throw new Error(`这份文件超过当前上限 ${requestBodyLimitText()}，请核对是否选中了天气快照。`);
    const snapshot=JSON.parse(await file.text());
    if(snapshot.format!=='yilao-weather-snapshot-v1'||!snapshot.weather||!snapshot.provenance)throw new Error('没有识别到支持的天气快照格式，或缺少来源核对说明。');
    const p=snapshot.provenance,w=snapshot.weather;
    if(!Array.isArray(w.records)||!w.records.length)throw new Error('快照中没有逐时天气记录，不能把空文件当成已取得天气。');
    weatherSnapshots.set(plotId,snapshot);
    out.innerHTML=`<div class="inline-summary"><h3>先核对这份文件</h3><dl class="data-pairs"><dt>来源网址</dt><dd>${esc(p.source_url||'未填写')}</dd><dt>来源核对人</dt><dd>${esc(p.source_checked_by||'未填写')}</dd><dt>核对时间</dt><dd>${datetimeText(p.checked_at)}</dd><dt>时间依据</dt><dd>${esc(p.issue_time_basis||'未填写')}</dd>${forecastTimeRows(w,{importPreview:true})}<dt>逐时记录</dt><dd>${w.records.length} 个时段</dd></dl></div>${note('导入表示提供了这份记录，不代表应用认证了来源，也不代表个人作业安全。预警或地点信息不完整时仍需补充。'+storageText('weatherImportTransfer'),'warning')}${button('确认将快照用于这块地','weather-import','primary',`data-id="${esc(plotId)}"`)}`;
  }catch(error){out.innerHTML=note(error instanceof SyntaxError?'这份文件不是完整的 JSON 天气快照，请重新选择。':error.message,'error');}
}
function locate(trigger){
  const form=trigger.closest('form'),status=appElement('location-status');
  if(!navigator.geolocation){status.textContent='当前浏览器不支持定位，可以手工填写坐标。';return;}
  trigger.disabled=true;status.textContent='正在等待浏览器定位许可…';
  navigator.geolocation.getCurrentPosition(position=>{
    form.elements.latitude.value=position.coords.latitude.toFixed(6);form.elements.longitude.value=position.coords.longitude.toFixed(6);
    status.textContent=`已填入目前位置，浏览器报告误差约 ${Math.round(position.coords.accuracy)} 米。请确认确实是这块地，再保存。`;trigger.disabled=false;
  },()=>{status.textContent='没有取得定位。原填写内容已保留，可以手工填写坐标。';trigger.disabled=false;},{timeout:12000,maximumAge:0,enableHighAccuracy:false});
}
window.addEventListener('hashchange',()=>{const next=parseAppRoute(location.hash);if(!next)return;route=next;render(true);fillCorrection();});
initialLoad();
