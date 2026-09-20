import {esc, field, select, check, options, note, units, dateToday, quantity} from './ui.js';
import {activityActions,activityCodes} from './activity.js';
import {regionLabel,stageLabel,rateTotal,rateSourceLabel} from './display.js';
import {predictionBindingField} from './predictions.js';
import {storageText,isWeatherSite} from './context.js';

export const methodNames={"walk_observe":"步行观察","sort_prepare":"分装与整理","carry":"手提或肩挑（具体方式另记）","wheelbarrow":"推车","hoe":"用锄","spade":"用铲","broadcast":"撒施","hole":"穴施","furrow":"沟施","tray":"育苗盘","nursery_bed":"苗床","direct":"直播","row":"条播","hill":"点播","hand":"手工移栽","rice_hand":"手工插秧","observe":"观察","thin":"间除","replace":"补苗","hose":"水管","gate":"开闸控水","watering_can":"水壶浇灌","clear_channel":"清理沟渠","fertigation":"水肥配合作业","hand_pull":"手拔","lay_cover":"铺覆盖物","ground_tie":"地面绑扎","hand_tool":"手工具整理","manual":"人工授粉","bag":"套袋","support_fruit":"托果","observe_record":"观察记录","remove_affected":"摘除受害部位","place_trap":"布置物理设施","licensed_service":"有资质人员服务","hand_pick":"手工采摘","hand_cut":"手工割收","pull":"拔收","dig":"挖收","service":"专业或农机服务","sort":"分拣","wash":"清洗","manual_clean":"手工去杂","spread":"摊晒","turn":"翻晒","store":"入库整理","collect_residue":"清理残体","observe_from_accessible_area":"从可安全到达处观察","hand_hoe":"手工锄草","manual_carry":"人工搬运","shoulder_pole":"扁担挑运","hand_cart":"手推车","cart":"推车","hand_sow":"人工播种","machine":"机械作业","shovel":"铁锹作业","pump":"水泵","hand_spread":"手工撒施","backpack_sprayer":"背负式喷雾器"};
export const methodLabel=value=>methodNames[value]||({carry:'搬运',hand_hoe:'手工锄草',wheelbarrow:'独轮车'}[value])||value;
export const cropLabel=(plot,catalog)=>plot?.crop_name || catalog.crops?.find(c=>c.id===plot?.crop_id)?.name || '作物待确认';
const errorBox='<div class="form-error" role="alert" tabindex="-1"></div>';
const finish=(label,back='today')=>`${errorBox}<div class="form-actions"><button class="button primary" type="submit">${esc(label)}</button><button class="button secondary" type="button" data-action="navigate" data-page="${esc(back)}">返回</button></div>`;
const textarea=(name,label,value='',hint='')=>`<div class="field full-width"><label for="field-${name}">${esc(label)}</label><textarea id="field-${name}" name="${name}">${esc(value)}</textarea>${hint?`<p class="field-hint">${esc(hint)}</p>`:''}</div>`;
export const windowValues=(person,date)=>{
  const w=person?.availability?.find(w=>w.start?.startsWith(date))||person?.availability?.[0];
  return {start:w?.start?.slice(11,16)||'',end:w?.end?.slice(11,16)||'',date:w?.start?.slice(0,10)||date,originalStart:w?.start||'',originalEnd:w?.end||''};
};
const originalWindowFields=w=>`<input type="hidden" name="availability_original_start" value="${esc(w.originalStart)}"><input type="hidden" name="availability_original_end" value="${esc(w.originalEnd)}">`;

export function plotForm(state,catalog,id='') {
  const plot=state.plots.find(p=>p.id===id)||{};
  const crop=catalog.crops?.find(c=>c.id===plot.crop_id);
  const categories=(catalog.categories||[]).map(c=>({value:c.id,label:c.name}));
  const category=plot.category_id||crop?.category_ids?.[0]||'';
  const crops=(catalog.crops||[]).filter(c=>!category||c.category_ids?.includes(category)).map(c=>({value:c.id,label:c.name}));
  return `<div class="page-heading"><div><h1>${id?'修改地块':'记下一块地'}</h1><p>总面积用来认识地块；每项农活还剩多少，需要另外记录。</p></div></div><form data-form="plot" data-id="${esc(id)}" class="panel"><div class="form-grid">
    ${field('name','地块名称',{value:plot.name,required:true,placeholder:'如：东边地'})}
    ${select('category_id','作物大类',[{value:'',label:'先不确定'},...categories],category)}
    ${select('crop_id','具体作物',[{value:'unknown',label:'待确认 / 只知道当地叫法'},...crops],plot.crop_id||'unknown',{hint:'收录作物名称，只方便记录；不代表已经验证当地种植方法。'})}
    ${field('crop_name','当地怎么叫',{value:plot.crop_name,placeholder:'如：菜瓜',hint:'原称呼会保留，不会自动认定为另一种作物。'})}
    ${field('area_value','地块总面积',{value:plot.area?.value,type:'number',min:0.0001,step:'any',required:true})}
    ${select('area_unit','面积单位',[{value:'mu',label:'亩'},{value:'sqm',label:'平方米'}],plot.area?.unit||'mu',{hint:'1 亩约为 666.67 平方米。'})}
    ${field('stage','现在长到哪一步',{value:plot.stage&&plot.stage!=='unknown'?stageLabel(plot.stage):'',placeholder:'如：苗期、开花、可以采收；不知道可留空'})}
    ${select('environment','作业环境',[{value:'outdoor',label:'露天地块'},{value:'greenhouse',label:'棚内 / 温室'}],plot.environment||'outdoor')}
    ${field('region_id','所在乡镇或地区',{value:regionLabel(plot.region_id||state.settings.place),placeholder:'如：都昌某乡镇'})}
    ${select('soil','地面情况',[{value:'unknown',label:'还没确认'},{value:'workable',label:'现场确认可作业'},{value:'wet',label:'湿滑 / 过湿'},{value:'dry',label:'偏干'},{value:'flooded',label:'积水'}],plot.conditions?.soil||'unknown')}
  </div><details ${id?'':'open'}><summary>天气地点（坐标不知道可以先留空）</summary><p class="form-intro">${isWeatherSite()?'坐标应对应这块地；不知道可以先留空。':'坐标应对应这块地。浏览器定位需经你同意，且可能只是现在所在的位置。'}</p><div class="form-grid">
    ${field('latitude','纬度',{value:plot.latitude,type:'number',min:-90,max:90,step:'any',placeholder:'如：29.3'})}
    ${field('longitude','经度',{value:plot.longitude,type:'number',min:-180,max:180,step:'any',placeholder:'如：116.2'})}
  </div>${isWeatherSite()?'<p class="field-hint">填写地块坐标，或请家人协助确认。</p>':'<div class="actions actions-spaced"><button type="button" class="button secondary" data-action="locate">使用我现在的位置</button></div>'}<p class="field-hint" id="location-status" aria-live="polite"></p></details>${finish('保存这块地','farm')}</form>`;
}

export function taskForm(state,catalog,id='') {
  const task=state.tasks.find(t=>t.id===id)||{};
  const taskDef=catalog.tasks?.find(t=>t.code===task.task_code)||catalog.tasks?.[0]||{};
  const hasFeedback=state.feedback.some(f=>f.task_id===id);
  const wait=task.wait_duty;
  const missingOldWait=Boolean(id)&&!Object.hasOwn(task,'wait_duty');
  const waitStatus=missingOldWait?'unchanged':wait?.status==='known'?(wait.minutes===0?'none':'known'):'unknown';
  const unit=task.remaining_quantity?.unit||taskDef.units?.[0]||'mu';
  const taskOptions=(catalog.tasks||[]).map(t=>({value:t.code,label:t.name}));
  if(task.task_code&&!taskOptions.some(o=>o.value===task.task_code))taskOptions.unshift({value:task.task_code,label:`${task.name||'原农活'}（分类待核对）`});
  const methodOptions=(taskDef.methods||[taskDef.method||'manual']).map(m=>({value:m,label:taskDef.method_labels?.[m]||methodLabel(m)}));
  if(task.method&&!methodOptions.some(o=>o.value===task.method))methodOptions.unshift({value:task.method,label:`${methodNames[task.method]||'原做法'}（待核对）`});
  const unitOptions=(taskDef.units||taskDef.valid_units||['mu','sqm']).map(u=>({value:u,label:units[u]||u}));
  if(!unitOptions.some(o=>o.value===unit))unitOptions.unshift({value:unit,label:`${units[unit]||'原单位'}（待核对）`});
  const workers=[state.profile,...state.helpers];
  const plot=state.plots.find(p=>p.id===task.plot_id)||state.plots[0];
  const taskCrop=task.crop_id??plot?.crop_id??'unknown';
  const taskStage=task.stage??plot?.stage??'unknown';
  const taskCrops=[{value:'unknown',label:'具体作物待确认'},...(catalog.crops||[]).map(c=>({value:c.id,label:c.name}))];
  if(!taskCrops.some(c=>c.value===taskCrop))taskCrops.push({value:taskCrop,label:task.crop_name||(plot?.crop_id===taskCrop?plot.crop_name:null)||'原记录作物（名称待核对）'});
  const date=state.settings.planning_date;
  const q=task.remaining_quantity?.value;
  const isTrip=unit==='trip';
  return `<div class="page-heading"><div><h1>${id?'修改农活':'记下一项农活'}</h1><p>只填这次还没做完的部分。截止再急，也要把准备和休息留出来。</p></div></div><form data-form="task" data-id="${esc(id)}" class="panel"><div class="form-grid">
    ${select('plot_id','在哪块地',state.plots.map(p=>({value:p.id,label:`${p.name} · ${cropLabel(p,catalog)}`})),task.plot_id||state.plots[0]?.id,{required:true})}
    ${select('task_code','要做什么',taskOptions,task.task_code||taskDef.code,{required:true})}
    ${field('name','这项农活怎么称呼',{value:task.name,placeholder:'如：东边地挑粪、给菜地浇水'})}
    ${select('method','采用的做法',methodOptions,task.method||taskDef.method)}
    ${field('quantity',hasFeedback?'最初登记的待做量':'这次剩下多少',{value:q,type:'number',min:0,step:isTrip?'1':'any',required:true,attrs:hasFeedback?'readonly':'',hint:hasFeedback?'这是最初计量基数，已锁定；当前剩余量请看农活列表。实际完成请新增或更正完成记录。':'不是整块地的总面积，也不包括已经做完的部分。'})}
    ${select('unit','剩余量的单位',unitOptions,unit)}
    ${field('earliest_date','最早可以开始的日期',{value:task.earliest_start?.slice(0,10)||date,type:'date',required:true})}
    ${field('deadline_date','最晚做到哪天',{value:task.deadline?.slice(0,10)||'',type:'date',hint:'没有明确日期可以留空，之后再确认。'})}
    ${field('deadline_source','这个截止日依据什么',{value:task.deadline_source,placeholder:'如：本人计划、现场农技人员的建议',wide:true})}
    ${select('priority','这项活有多急',[{value:'1',label:'一般，可以商量'},{value:'2',label:'稍急'},{value:'3',label:'比较急'},{value:'4',label:'急，需要尽早商量'},{value:'5',label:'很急，需要优先商量'}],task.priority||1)}
    ${select('divisible','能否分几次完成',[{value:'yes',label:'可以分次'},{value:'no',label:'需要一口气完成这一项'}],task.divisible===false?'no':'yes')}
    ${field('load_per_trip_kg','一次最多需要拿/搬多重（公斤）',{value:task.load_per_trip_kg,type:'number',min:0,step:'any',wide:true,hint:'搬运按每趟最大实际重量填写；其他农活按一次拿起或搬动的最大重量填写。未知留空，只有明确无需负重才填 0。这不是建议负重上限。'})}
  </div><div id="carry-fields" ${isTrip?'':'hidden'}><hr class="section-divider"><h2>每趟搬运的情况</h2><p class="form-intro">挑粪、挑水、搬肥料都按实际趟数记录；不同负重、工具和距离要分别估时。</p><div class="form-grid">
    ${field('distance_m','单程搬运距离（米）',{value:task.distance_m,type:'number',min:0,step:'any'})}
  </div></div><hr class="section-divider"><h2>${hasFeedback?'最初登记的工作量，估计净干多久':'这项剩余农活，净干多久'}</h2><p class="form-intro">${hasFeedback?'为保持每单位估时一致，这里对应上方最初计量基数，不是扣除记录后的余量。':'可以先留空。'}只估真正干活的分钟，不含喝水休息、准备和来回路程；帮手单独填写，不按人数直接加速。</p><div id="worker-rates">
  ${workers.map(w=>{const r=task.rates?.[w.id];return `<fieldset class="field-group" data-worker="${esc(w.id)}"><legend>${esc(w.name||'本人')}</legend><div class="form-grid">${field(`rate_low__${w.id}`,'估计最少用时（分钟）',{value:rateTotal(r,q,unit,'low'),type:'number',min:0,step:'any'})}${field(`rate_high__${w.id}`,'估计最多用时（分钟）',{value:rateTotal(r,q,unit,'high'),type:'number',min:0,step:'any'})}${select(`rate_scope__${w.id}`,'这份用时包括哪些部分',[{value:'net_work',label:'只算净干活，不含准备和休息'},{value:'whole_session',label:'包含准备、往返或休息，尚未拆清'}],r?.scope||'net_work',{wide:true,hint:'没有拆清的整段用时可以保留，但不会冒充净工作用时。'})}${field(`rate_source__${w.id}`,'估时从哪里来',{value:r?.source?rateSourceLabel(r.source):'',placeholder:'如：我以前做过，凭记忆估计；或某天的实际记录',wide:true})}</div></fieldset>`;}).join('')}
  </div><details><summary>准备、往返、工具与先后顺序</summary><div class="form-grid">
    ${field('setup_minutes','每次准备（分钟）',{value:task.session?.setup_minutes??0,type:'number',min:0,step:1})}
    ${field('outbound_minutes','每次去程（分钟）',{value:task.session?.outbound_minutes??0,type:'number',min:0,step:1})}
    ${field('return_minutes','每次返程（分钟）',{value:task.session?.return_minutes??0,type:'number',min:0,step:1})}
    ${field('cleanup_minutes','每次收尾（分钟）',{value:task.session?.cleanup_minutes??0,type:'number',min:0,step:1})}
    ${field('buffer_minutes','每次另外留出的时间（分钟）',{value:task.session?.buffer_minutes??0,type:'number',min:0,step:1})}
    ${field('min_chunk_minutes','每次至少净干多久（分钟）',{value:task.min_chunk_minutes??10,type:'number',min:1,max:120,step:1})}
  </div><fieldset class="field-group"><legend>等水、等设备或等人的时间</legend><p class="form-intro">只记不走动、不操作的等待。走动或操作请另记作业或行程；等待不会自动算成恢复休息，也不代替需要的休息时间。</p><div class="form-grid">
    ${select('wait_status','这项农活是否需要等待',[...(missingOldWait?[{value:'unchanged',label:'原来未填写，本次暂不修改'}]:[]),{value:'unknown',label:missingOldWait?'仍不清楚，记为待确认':'未确认'},{value:'none',label:'确认无需等待'},{value:'known',label:'有非劳动等待'}],waitStatus,{wide:true,hint:'没有记录过等待情况时保持未确认，不会自动当成零分钟。'+(missingOldWait?'选择“本次暂不修改”会保留原记录未填写的状态。':'')})}
    ${field('wait_minutes','有等待时：每次等待多久（分钟）',{value:waitStatus==='known'?wait.minutes:'',type:'number',step:'any',hint:'只在选择「有非劳动等待」时填写正分钟数。'})}
    ${select('wait_reason','有等待时：在等什么',[{value:'',label:'请选择等待原因'},{value:'equipment',label:'等水或设备'},{value:'helper',label:'等帮手或其他人'},{value:'material',label:'等物料'},{value:'weather',label:'等天气条件变化'},{value:'watch',label:'留在原地看守'},{value:'other',label:'其他等待'}],waitStatus==='known'?wait.reason:'')}
    ${field('wait_source','有等待时：时间和原因从哪里得知',{value:waitStatus==='known'?wait.source:'',wide:true,placeholder:'如：本人记录过等水约 20 分钟，或已和帮手确认'})}
  </div></fieldset><div class="field-group"><h3>需要的工具</h3><div class="option-group">${state.resources.filter(r=>r.kind==='tool').map(r=>check(`resource__${r.id}`,r.name,task.required_resources?.includes(r.id))).join('')||'<p class="muted">先在「我与帮手」添加实际能用的工具。</p>'}</div></div><div class="field-group"><h3>必须先完成哪些农活</h3><div class="option-group">${state.tasks.filter(t=>t.id!==id).map(t=>check(`depends__${t.id}`,t.name,task.depends_on?.includes(t.id))).join('')||'<p class="muted">暂无其他农活。</p>'}</div></div></details>
  <details><summary>实际会用到哪些动作</summary><p class="form-intro">请按这块地、这件工具和实际做法确认，不会根据农活名称自动猜测。</p><div class="form-grid">${activityActions.map(a=>check(`tag__${a.value}`,a.label,task.tags?.includes(a.value))).join('')}${field('other_activity_tags','其他已经记录的动作（待核对）',{value:(task.tags||[]).filter(t=>!activityCodes.has(t)).join('，'),wide:true,hint:'原话会保留。这里的其他动作尚不能自动与人员限制逐项比较。'})}${field('activity_source','这些动作由谁、何时确认',{value:task.activity_source,wide:true,placeholder:'如：本人今天查看现场并试过工具后确认'})}${check('activity_tags_confirmed','我已核对这项农活实际涉及的动作',task.activity_tags_confirmed,'如果上面的动作都不涉及，也请核对后勾选；没有勾选时按尚未确认处理。')}</div></details>
  <details><summary>现场农艺依据与备注</summary><p class="form-intro">只记录已有依据。目录里有这项农活，不代表本地现在适合做。</p><div class="form-grid">${select('task_crop_id','本项农活的作物',taskCrops,taskCrop,{wide:true,hint:'新农活先带入地块所记作物，请核对这一项。已有农活保留自己的记录，地块变化后不会无声改写。'})}${field('task_stage','本项农活的作物阶段',{value:stageLabel(taskStage),wide:true,hint:'如苗期、开花期、结果期；不清楚可以写“阶段待确认”。本项农活的阶段需单独核对。'})}${select('agronomy_status','依据是否明确',[{value:'unknown',label:'尚不明确 / 待确认'},{value:'confirmed',label:'我确认已有明确来源'}],task.agronomy?.status==='confirmed'?'confirmed':'unknown')}${field('agronomy_source','依据来源',{value:task.agronomy?.source,placeholder:'如：哪位农技人员、什么资料、何时确认'})}${textarea('notes','其他要留意的情况',task.notes)}</div></details>${finish('保存这项农活','farm')}</form>`;
}

export function personForm(state,id='elder') {
  const self=id===state.profile.id;
  const person=self?state.profile:state.helpers.find(h=>h.id===id)||{};
  const limits=person.limits||{};
  const w=windowValues(person,state.settings.planning_date);
  const sourceKind=person.limits_source_kind||(limits.source?.startsWith('有记录的专业建议')?'documented':limits.source?'self_report':'unknown');
  return `<div class="page-heading"><div><h1>${self?'我今天的情况':person.id?'修改帮手情况':'记下一位帮手'}</h1><p>${self?'本人或家属都可以代为记录，来源会保留。':'只有实际确认能来的时段，才算可用人手。'}</p></div></div><form data-form="person" data-id="${esc(person.id||'')}" data-self="${self}" class="panel"><div class="form-grid">
  ${field('name',self?'怎么称呼本人':'帮手姓名或称呼',{value:person.name,required:true})}
  ${self?select('entered_by','现在由谁填写',[{value:'self',label:'本人填写'},{value:'family',label:'家属代填'}],state.settings.entered_by):select('entered_by','这次情况由谁提供',[{value:'self',label:'帮手本人确认'},{value:'family',label:'家属与帮手确认后记录'},{value:'unknown',label:'还没确认'}],person.entered_by||'unknown')}
  ${select('state','现在身体感觉怎么样',[{value:'unknown',label:'还没有确认'},{value:'clear',label:'本人此刻没有不适'},{value:'stop',label:'今天不舒服，先不做农活'}],person.state||'unknown',{wide:true,hint:'没有不适不等于医学上的许可；感到不舒服时，不会给这位人员安排农活。'})}
  ${check('state_checked_now','本次已经向本人确认，这就是现在的身体情况',false,'仅修改其他资料不会刷新上次身体情况的核对时间。')}
  ${textarea('limitations_note','已有的活动限制或需要家人知道的事',person.limitations_note,'如已有不能弯腰、不能提重物的要求，请照原话记录；程序不会根据病名猜测劳动能力。')}
  </div><hr class="section-divider"><h2>实际可以来的时间</h2>${originalWindowFields(w)}<p class="field-hint">这里修改一个时段，其他已保存的时段会保留。</p><div class="form-grid">
  ${field('available_date','日期',{value:w.date,type:'date',required:true})}
  ${check('availability_confirmed',self?'已确认本人这段时间有空':'已向帮手确认，这段时间能来',Boolean(person.availability?.length))}
  ${field('available_start','开始时间',{value:w.start,type:'time'})}
  ${field('available_end','结束时间',{value:w.end,type:'time'})}
  ${check('initial_rest_confirmed','本人确认开始前已经充分休息',person.initial_rest_confirmed)}
  ${field('used_active_minutes','这一天此前已活动多久（分钟）',{value:person.used_active_minutes_by_date?.[w.date]??'',type:'number',min:0,max:1440,step:1,hint:'含当天已经做过的农活；不知道请留空。'})}
  </div><hr class="section-divider"><h2>已有的活动与休息约束</h2><p class="form-intro">这里没有通用的“安全数值”。不知道时留空，仍可以保存农活与实际记录。</p><div class="form-grid">
  ${select('source_kind','这些约束从哪里来',[{value:'unknown',label:'不清楚，尚待确认'},{value:'self_report',label:'本人已有约束 / 本人或家属自述'},{value:'documented',label:'有记录的专业建议'}],sourceKind,{wide:true})}
  ${field('max_active_minutes_per_day','每天最多活动分钟',{value:limits.max_active_minutes_per_day,type:'number',min:1,max:1440,step:1})}
  ${field('max_continuous_active_minutes','每次最多连续活动分钟',{value:limits.max_continuous_active_minutes,type:'number',min:1,max:1440,step:1})}
  ${field('min_rest_minutes','每次至少休息分钟',{value:limits.min_rest_minutes,type:'number',min:1,max:1440,step:1})}
  ${field('max_load_kg','已有的单次负重上限（公斤）',{value:limits.max_load_kg,type:'number',min:0,step:'any'})}
  ${field('limits_source','来源原文或来源说明',{value:limits.source,placeholder:'谁提出、何时提出、适用什么情况',wide:true})}
  ${field('limits_valid_until','已有建议有效至（如有）',{value:person.limits_valid_until?.slice(0,10)||'',type:'date'})}
  </div><fieldset class="field-group"><legend>已有要求明确禁止哪些动作</legend><p class="form-intro">只勾选本人已有要求，应用不会自行认定某个动作适合你。</p><div class="form-grid">${activityActions.map(a=>check(`forbid__${a.value}`,a.label,limits.forbidden_tags?.includes(a.value))).join('')}
  ${field('forbidden_tags','其他禁止动作原话（待核对）',{value:(limits.forbidden_tags||[]).filter(t=>!activityCodes.has(t)).join('，'),wide:true,placeholder:'用逗号分开；没有可以留空',hint:'这些其他动作会保留，但还不能自动逐项匹配；需先核对落实。'})}
  ${check('limitations_reviewed','已有文字限制已逐项落实到上面的动作和数字约束',person.limitations_reviewed,'这只记录本人或家属的核对，不等于医学验证。')}
  ${check('source_confirmed','我确认以上来源已经核对，且适用于当前情况',limits.review_status==='confirmed','这只记录输入者的确认，应用不会认证来源的专业资质。')}
  </div></fieldset>${finish(self?'保存我的情况':'保存帮手情况','people')}</form>`;
}

export function resourceForm(state,id='') {
  const r=state.resources.find(x=>x.id===id)||{};
  const w=windowValues(r,state.settings.planning_date);
  return `<div class="page-heading"><div><h1>${id?'修改工具或休息条件':'添加工具或休息条件'}</h1><p>拥有工具、知道联系人，都不等于今天实际可用。</p></div></div><form data-form="resource" data-id="${esc(id)}" class="panel"><div class="form-grid">
  ${field('name','名称',{value:r.name,required:true,placeholder:'如：手推车、树荫下的椅子、饮水点'})}
  ${select('kind','是什么',[{value:'tool',label:'工具'},{value:'water',label:'饮水'},{value:'rest_place',label:'可用的休息处'}],r.kind||'tool')}
  ${originalWindowFields(w)}${field('available_date','日期',{value:w.date,type:'date',required:true})}
  ${field('capacity','同时能供几项农活使用',{value:r.capacity??1,type:'number',min:1,max:20,step:1,required:true})}
  ${field('available_start','从几点能用',{value:w.start,type:'time'})}
  ${field('available_end','到几点能用',{value:w.end,type:'time'})}
  ${check('confirmed','我已经实际确认能用，并核对了上述时段',r.confirmed)}
  </div>${finish('保存可用条件','people')}</form>`;
}

export function feedbackForm(state,taskId='',original=null) {
  const task=state.tasks.find(t=>t.id===taskId)||state.tasks[0];
  if(!task)return `<h1>还没有可记录的农活</h1>${note('先记下一项农活，再记录实际完成情况。')}<button class="button primary" data-action="navigate" data-page="farm">去记农活</button>`;
  return `<div class="page-heading"><div><h1>记录实际做了多少</h1><p>请按实际回忆填写，安排里的数量不会自动当作已经完成。</p></div></div><form data-form="feedback" class="panel"><div class="form-grid">
  ${select('task_id','是哪项农活',state.tasks.map(t=>({value:t.id,label:t.name})),task.id,{required:true})}
  ${select('worker_id','是谁做的',[state.profile,...state.helpers].map(p=>({value:p.id,label:p.name})),state.profile.id,{required:true})}
  <div class="full-width" data-prediction-binding>${predictionBindingField(state,task.id,original?.worker_id||state.profile.id,original)}</div>
  ${select('status','这次做到什么程度',[{value:'partial',label:'做了一部分'},{value:'completed',label:'做完这项剩余农活'},{value:'not_done',label:'没有开始'},{value:'interrupted',label:'做了一会儿，中途停下'},{value:'unknown',label:'情况还不清楚'}],'partial')}
  ${field('completed_quantity',`这次实际完成多少（${units[task.remaining_quantity.unit]||task.remaining_quantity.unit}）`,{type:'number',min:0,step:['trip','plant'].includes(task.remaining_quantity.unit)?1:'any',hint:`保存前还剩 ${quantity(task.remaining_quantity.value,task.remaining_quantity.unit)}；不确定就留空，不会当作 0。`})}
  ${field('started_at','实际开始时间',{type:'datetime-local',hint:'完全没有开始、等待或往返时，起止可都留空；完成量、净干活和休息请明确填 0。'})}
  ${field('ended_at','实际结束时间',{type:'datetime-local'})}
  ${field('net_minutes','其中净干活分钟',{type:'number',min:0,step:'any',hint:'不包括休息、准备或来回；不清楚就留空。'})}
  ${field('rest_minutes','其中休息分钟',{type:'number',min:0,step:'any'})}
  ${select('quantity_source','完成量怎么得知',[{value:'self_report',label:'本人回忆 / 自述'},{value:'family_report',label:'家属记录'},{value:'measured',label:'实际清点或测量'},{value:'unknown',label:'来源待确认'}],'self_report')}
  ${select('clock_source','用时怎么得知',[{value:'self_report',label:'本人回忆 / 估计'},{value:'clock_record',label:'钟表或计时记录'},{value:'family_report',label:'家属记录'},{value:'unknown',label:'来源待确认'}],'self_report')}
  ${original?`<div class="full-width">${note(original.context_snapshot?'更正默认保留原记录的历史情境，不会按今天的农活设置重写。':'原记录缺少当时情境快照，默认保留这个缺口，不会自动补成已核实记录。')}${check('refresh_context','重新核对本次情境',false,'仅在你明确重新核对后，才按当前农活填写保存新的情境说明。')}</div>`:''}
  <div class="full-width" data-context-field>${select('context_matches_task','这次实际做法、工具和现场情况与这项农活所填相同吗？',[{value:'unknown',label:'未确认'},{value:'true',label:'相同，已经核对'},{value:'false',label:'不同'}],original?.context_matches_task===true?'true':original?.context_matches_task===false?'false':'unknown',{wide:true,hint:'未确认或不同的记录仍能保存，但不能自动当作同一情境下的估时依据。'})}</div>
  ${textarea('notes','中断、身体感觉或其他情况','','慢一些、没做完、中途停下都请如实记录。')}
  ${check('consent',storageText('consent'),false)}
  </div>${finish('保存完成记录','today')}</form>`;
}

export function settingsForm(state) {
  const s=state.settings;
  return `<form data-form="settings" class="panel"><h2>想安排哪一天</h2><div class="form-grid">${field('planning_date','安排日期',{value:s.planning_date,type:'date',required:true})}${field('place','所在地区',{value:s.place,required:true})}${field('start_time','从几点开始考虑',{value:s.start_time,type:'time',required:true})}${field('end_time','到几点结束',{value:s.end_time,type:'time',required:true})}</div>${errorBox}<div class="form-actions"><button class="button secondary" type="submit">保存日期与时间</button></div></form>`;
}

export function policyForm(state) {
  const p=state.policy;
  return `<details data-focus-section="policy"><summary>已有的天气作业依据（没有时不必填写）</summary><form data-form="policy"><p class="form-intro">只记录已有、适用的现场作业依据。应用不提供适用于所有老人的统一气温或工作时间阈值。</p><div class="form-grid">${field('source','依据与适用范围',{value:p.source,wide:true})}${field('max_temperature_c','已有最高气温限制（℃）',{value:p.weather_limits?.max_temperature_c,type:'number',min:-50,max:60,step:'any'})}${field('max_wind_m_s','已有最大风速限制（米/秒）',{value:p.weather_limits?.max_wind_m_s,type:'number',min:0,max:100,step:'any'})}${field('max_precipitation_mm','已有小时降水限制（毫米）',{value:p.weather_limits?.max_precipitation_mm,type:'number',min:0,max:500,step:'any'})}${check('confirmed','我确认来源已核对且适用于这里',p.review_status==='confirmed')}</div>${errorBox}<div class="form-actions"><button class="button secondary" type="submit">保存已有依据</button></div></form></details>`;
}
