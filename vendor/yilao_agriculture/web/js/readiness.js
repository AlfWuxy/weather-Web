// 只决定缺项的填写入口，不修改或放宽后端的安排条件。
const matches=(field,prefix)=>field===prefix||field.startsWith(prefix+'.');
export function readinessActions(item,state){
  const field=item.field||'',code=item.code||'';
  const person=[state.profile,...state.helpers].find(p=>matches(field,`workers.${p.id}`));
  const task=state.tasks.find(t=>matches(field,`tasks.${t.id}`));
  const plot=state.plots.find(p=>matches(field,`plots.${p.id}`)||matches(field,`weather.${p.id}`));
  if(code==='NO_PLOTS')return [{label:'添加地块',page:'plot'}];
  if(code==='NO_TASKS')return [{label:state.plots.length?'填写农活和剩余量':'先添加地块',page:state.plots.length?'task':'plot'}];
  if(field.startsWith('policy'))return [{label:'核对出工条件与来源',page:'weather',focus:'policy'}];
  if(person)return [{label:`核对${person.id===state.profile.id?'本人':person.name||'这位帮手'}的情况`,page:'person',id:person.id,focus:code==='PAST_LOAD_UNKNOWN'?'used_active_minutes':code==='LIMITATION_MAPPING_UNKNOWN'?'limitations_note':'state'}];
  if(field==='profile'||code==='NO_CONFIRMED_WORKER')return [{label:'核对本人资料',page:'person',id:state.profile.id,focus:'state'},{label:'填写实际帮手',page:state.helpers.length?'people':'helper'}];
  if(field.startsWith('weather'))return [{label:`核对${plot?.name||'地块'}的天气与预警`,page:'weather',focus:plot?`weather:${plot.id}`:'weather'}];
  if(field.startsWith('plots'))return [{label:`补充${plot?.name||'地块'}的位置`,page:plot?'plot':'farm',id:plot?.id,focus:'latitude'}];
  if(code==='QUANTITY_UNKNOWN'&&task){
    const replaced=new Set(state.feedback.map(e=>e.supersedes_event_id).filter(Boolean));
    const event=[...state.feedback].reverse().find(e=>e.task_id===task.id&&!replaced.has(e.event_id)&&(e.status==='unknown'||e.completed_quantity===null));
    return [{label:'核对并更正完成记录',page:event?'correction':'records',id:event?.event_id,focus:event?'status':''}];
  }
  if(task){
    const target={WAIT_UNKNOWN:['确认是否等待','wait_status'],RATE_UNKNOWN:['填写本人和帮手的估时',`rate_low__${state.profile.id}`],AGRONOMY_UNCONFIRMED:['核对农事依据','agronomy_status'],ACTIVITY_UNKNOWN:['确认实际动作','activity_tags_confirmed'],ACTIVITY_MAPPING_UNKNOWN:['核对动作原话','other_activity_tags'],METHOD_OR_UNIT_UNSUPPORTED:['查看做法与计量方式','method'],TASK_MAPPING_UNKNOWN:['核对农活种类','task_code']}[code]||['补充这项农活','name'];
    return [{label:target[0],page:'task',id:task.id,focus:target[1]}];
  }
  if(field.startsWith('resources'))return [{label:'核对工具、饮水与休息处',page:'people',focus:'resources'}];
  if(field.startsWith('settings'))return [{label:'核对日期与时间',page:'weather',focus:'planning_date'}];
  if(field.startsWith('tasks'))return [{label:'核对农活资料',page:'farm'}];
  return [{label:'查看相关资料入口',page:'people'}];
}
