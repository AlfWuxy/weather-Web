import {readContext,apiPath,storageText,validateAccountContext} from './context.js';

// 由页面配置声明当前服务的请求大小上限，未配置时保持本机默认值。
export function requestBodyLimit(doc=globalThis.document){
  const raw=doc?.querySelector?.('meta[name="yilao-body-limit"]')?.getAttribute('content');
  if(raw==null||raw==='')return 4000000;
  if(!/^[1-9]\d*$/.test(raw)||!Number.isSafeInteger(Number(raw)))throw new Error('文件大小限制配置无效，请刷新页面后重试。');
  return Number(raw);
}
export function requestBodyLimitText(doc=globalThis.document){
  const bytes=requestBodyLimit(doc);
  if(bytes<1000)return '小于 1 KB';
  const divisor=bytes>=1000000?1000000:1000,unit=bytes>=1000000?'MB':'KB';
  return `约 ${Number((bytes/divisor).toFixed(2))} ${unit}`;
}

export class ApiError extends Error {
  constructor(message, details=null) {super(message);this.name='ApiError';this.details=details;}
}
// 根路径与网站子路径共用同源接口；账户身份由同源登录会话决定。
export function createApiClient({getContext=readContext,fetchImpl=(...args)=>globalThis.fetch(...args)}={}){
return async function request(path, {method='GET', body, signal}={}) {
  const context=getContext(),url=apiPath(path,context),verb=method.toUpperCase();
  const headers=body===undefined?{}:{'Content-Type':'application/json'};
  const accountContext=validateAccountContext(context);
  if(context.storageScope==='account')headers['X-Yilao-Account-Context']=accountContext;
  if(!['GET','HEAD','OPTIONS'].includes(verb)&&context.csrfToken)headers['X-CSRF-Token']=context.csrfToken;
  let response;
  try {
    response=await fetchImpl(url,{method:verb,headers,body:body===undefined?undefined:JSON.stringify(body),signal,credentials:'same-origin',mode:'same-origin'});
  } catch (error) {
    if (error.name==='AbortError') throw error;
    throw new ApiError(storageText('networkError',context));
  }
  let data;
  try {data=await response.json();} catch {throw new ApiError(storageText('incompleteResponse',context));}
  if (!response.ok || data.ok===false) {
    if(response.status===409&&(data.error?.code==='ACCOUNT_CONTEXT_CHANGED'||data.code==='ACCOUNT_CONTEXT_CHANGED'))throw new ApiError('登录账户已变化，请刷新页面后继续',data);
    throw new ApiError(typeof data.error==='string'?data.error:data.error?.message || data.message || '这次操作没有完成，请检查填写内容后重试。',data);
  }
  return data;
};
}
export const request=createApiClient();

// 附件直接由同源服务器响应；验证值只放POST表单，不进入下载地址或历史记录。
export function submitAccountDownload(kind,selected,{context=readContext(),doc=globalThis.document,
  onError=()=>{},schedule=globalThis.setTimeout,cancel=globalThis.clearTimeout}={}){
  if(context.storageScope!=='account'||!['backup','field-collection'].includes(kind)||!['real','demonstration'].includes(selected))throw new Error('下载类型或账户模式无效。');
  const binding=validateAccountContext(context),token=context.csrfToken;
  if(!/^[a-f0-9]{64}$/.test(binding)||typeof token!=='string'||!token||token.length>512||/[\r\n]/.test(token))throw new Error('页面验证已失效，请刷新后再下载。');
  const action=apiPath('/api/download',context),frame=doc.createElement('iframe'),form=doc.createElement('form');
  frame.name=`agri-download-${globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  frame.hidden=true;frame.title='账户资料下载';frame.setAttribute('aria-hidden','true');
  form.method='POST';form.action=action;form.target=frame.name;form.hidden=true;form.enctype='application/x-www-form-urlencoded';
  for(const [name,value] of Object.entries({mode:selected,kind,csrf_token:token,account_context:binding})){
    const input=doc.createElement('input');input.type='hidden';input.name=name;input.value=value;form.appendChild(input);
  }
  let timer;
  const cleanup=()=>{if(timer!==undefined)cancel(timer);form.remove();frame.remove();};
  frame.addEventListener('load',()=>{
    try{
      const document=frame.contentDocument;
      if(!document)throw new Error('下载响应不能在当前页面读取');
      const text=document.body?.textContent?.trim();
      if(!text)return; // 初始空白页和附件响应均不能当作保存成功回执。
      let message='下载未能完成，请刷新页面后重试。';
      if(text.length<=8192){try{const data=JSON.parse(text);const error=typeof data.error==='string'?data.error:data.error?.message;if(typeof error==='string'&&error.length<=512)message=error;}catch{}}
      onError(message);cleanup();
    }catch{onError('下载状态未能确认，请刷新页面后重试。');cleanup();}
  });
  try{doc.body.appendChild(frame);doc.body.appendChild(form);form.submit();form.remove();timer=schedule(cleanup,180000);}
  catch(error){cleanup();throw error;}
  return cleanup;
}
