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
