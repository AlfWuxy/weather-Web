// 展示名称与原始代码分开，编辑未改值时保留原始记录。
const regions={duchang:'都昌',duchang_demonstration:'都昌（演示地点）'};
const stages={unknown:'阶段待确认',seedling:'苗期',vegetative:'营养生长期',flowering:'开花期',fruiting:'结果期',mature:'成熟期',maturity:'成熟期',harvest:'采收期',harvesting:'采收期',preplant:'种植前',dormant:'休眠期'};
export const regionLabel=value=>regions[value]||value||'地区待确认';
export const stageLabel=value=>stages[value]||value||'阶段待确认';
export const preserveDisplayed=(original,entered,label)=>original&&entered===label(original)?original:entered;
export function rateSourceLabel(value){
  if(/^本人复核的历史自述估时（非实测校准）；依据 [a-f0-9]{16}$/.test(value||''))return '本人复核的历史自述估时（非实测校准）';
  return {self_report_review:'本人复核的历史自述估时',self_report:'本人回忆或自述',family_report:'家属记录',illustrative:'演示假设',demonstration:'演示假设',unknown:'来源待确认'}[value]||value||'来源待确认';
}

export function personStateLabel(person,mode='real',now=Date.now()){
  if(person?.state==='stop')return '已记录身体不适，暂停农活';
  if(person?.state!=='clear')return '今天情况还没确认';
  if(mode==='demonstration')return '示例设定为无不适（演示）';
  const checked=Date.parse(person.state_checked_at),validUntil=Date.parse(person.limits_valid_until);
  const localDate=t=>new Date(t+8*3600000).toISOString().slice(0,10);
  const fresh=Number.isFinite(checked)&&checked<=now&&localDate(checked)===localDate(now)&&Number.isFinite(validUntil)&&validUntil>=now;
  return fresh?'今天已记录自述无不适，出工前仍需确认':'上次自述无不适，出工前需重新确认';
}

export function rateTotal(rate,quantity,unit,key){
  if(!rate||quantity===null||quantity===undefined)return '';
  const scale=rate.unit===unit?1:rate.unit==='mu'&&unit==='sqm'?3/2000:rate.unit==='sqm'&&unit==='mu'?2000/3:null;
  return scale===null?'':Math.round(rate[key]*quantity*scale*100)/100;
}
