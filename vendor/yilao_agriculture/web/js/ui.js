// 所有用户输入进入页面前均转义，避免导入文件改变页面结构。
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const textOf = value => typeof value === 'string' ? value : value?.message || value?.reason || value?.label || '';
export const numberText = value => value!==null && value!==undefined && value!=='' && Number.isFinite(Number(value)) ? new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 2}).format(Number(value)) : '待确认';
export const units = {mu:'亩', sqm:'平方米', trip:'趟', kg:'公斤', plant:'株', m:'米', m3:'立方米'};
export const quantity = (value, unit) => `${numberText(value)} ${units[unit] || esc(unit || '')}`;
export function dateToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
export const isoTime = (date, time) => `${date}T${time.length === 5 ? time + ':00' : time}+08:00`;
export function timeText(value) {
  if (!value) return '待确认';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? esc(value) : d.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai',hour12:false});
}
export function datetimeText(value) {
  if (!value) return '尚未更新';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? esc(value) : d.toLocaleString('zh-CN', {month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Shanghai',hour12:false});
}
export const button = (label, action, variant = 'primary', attrs = '') => `<button type="button" class="button ${variant}" data-action="${esc(action)}" ${attrs}>${esc(label)}</button>`;
export function options(items, selected, empty = '') {
  return (empty ? `<option value="">${esc(empty)}</option>` : '') + items.map(x => {
    const value = typeof x === 'string' ? x : x.value ?? x.id;
    const label = typeof x === 'string' ? x : x.label ?? x.name ?? x.name_zh ?? x.id;
    return `<option value="${esc(value)}" ${String(value) === String(selected) ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');
}
export function field(name, label, {value='', type='text', required=false, hint='', min, max, step, placeholder='', wide=false, attrs=''}={}) {
  const id = `field-${name}`;
  return `<div class="field ${wide?'full-width':''}"><label for="${esc(id)}">${esc(label)}${required?'<span class="required-note">（必填）</span>':''}</label><input id="${esc(id)}" name="${esc(name)}" type="${esc(type)}" value="${esc(value)}" ${required?'required':''} ${min!==undefined?`min="${esc(min)}"`:''} ${max!==undefined?`max="${esc(max)}"`:''} ${step!==undefined?`step="${esc(step)}"`:''} placeholder="${esc(placeholder)}" ${hint?`aria-describedby="${esc(id)}-hint"`:''} ${attrs}>${hint?`<p id="${esc(id)}-hint" class="field-hint">${esc(hint)}</p>`:''}</div>`;
}
export function select(name, label, items, value='', {hint='', required=false, wide=false}={}) {
  return `<div class="field ${wide?'full-width':''}"><label for="field-${esc(name)}">${esc(label)}${required?'<span class="required-note">（必填）</span>':''}</label><select id="field-${esc(name)}" name="${esc(name)}" ${required?'required':''} ${hint?`aria-describedby="field-${esc(name)}-hint"`:''}>${options(items,value)}</select>${hint?`<p class="field-hint" id="field-${esc(name)}-hint">${esc(hint)}</p>`:''}</div>`;
}
export function check(name,label,checked=false,hint='') {
  return `<label class="check-field"><input type="checkbox" name="${esc(name)}" ${checked?'checked':''}><span>${esc(label)}${hint?`<small>${esc(hint)}</small>`:''}</span></label>`;
}
export function note(message, type='info', title='') {
  return `<div class="notice ${esc(type)}">${title?`<strong>${esc(title)}</strong>`:''}<p>${esc(message)}</p></div>`;
}
export function showNotice(message, type='success') {
  const target=document.getElementById('agri-app')?.querySelector('#agri-notice');
  if(target)target.innerHTML=note(message,type);
}
export function getForm(form) {return Object.fromEntries(new FormData(form).entries());}
export function num(value) {return value === '' || value === undefined || value === null ? null : Number(value);}
export const clone = value => JSON.parse(JSON.stringify(value));
export const makeId = prefix => `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
export function nonEmptySource(value) {return String(value || '').trim().length > 0;}
export function downloadJSON(value, name) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
  const a=document.createElement('a'); a.href=url;a.download=name;a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
