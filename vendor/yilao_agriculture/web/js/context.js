// 服务器注入的页面配置与用户业务资料分开；这里只选择固定界面文案。
const meta=(doc,name)=>doc?.querySelector?.(`meta[name="${name}"]`)?.getAttribute('content');
export function storageScope(doc=globalThis.document){return meta(doc,'yilao-storage-scope')==='account'?'account':'local';}
export function validateApiBase(value='/api'){
  if(typeof value!=='string'||!/^\/[A-Za-z0-9_-]+(?:\/[A-Za-z0-9_-]+)*\/?$/.test(value))throw new Error('本站接口路径配置无效，请联系服务提供者；填写内容会保留。');
  return value.replace(/\/$/,'');
}
export function validateAccountContext(context){
  if(context.storageScope!=='account')return '';
  const value=context.accountContext;
  if(typeof value!=='string'||!/^[\x21-\x7e]{1,256}$/.test(value))throw new Error('账户页面验证信息缺失或已失效，请刷新页面后继续。');
  return value;
}
export function readContext(doc=globalThis.document){
  const csrfToken=meta(doc,'csrf-token')||'';
  if(/[\r\n]/.test(csrfToken))throw new Error('页面验证信息无效，请重新打开页面。');
  const context={apiBase:validateApiBase(meta(doc,'yilao-api-base')||'/api'),storageScope:storageScope(doc),csrfToken,accountContext:meta(doc,'yilao-account-context')||''};
  context.accountContext=validateAccountContext(context);
  return context;
}
export function apiPath(path,context=readContext()){
  if(typeof path!=='string'||!/^\/api(?:\/[A-Za-z0-9_-]+)*(?:\?[^#\s\\]*)?$/.test(path))throw new Error('请求路径不属于当前应用，操作未发送。');
  return validateApiBase(context.apiBase||'/api')+path.slice(4);
}

const copy={
  documentTitle:['宜老农业 · 农活安排','宜老农业 · 账户工作台'],
  modeLabel:['本机记录','当前登录账户'],
  demoModeLabel:['独立演示区','当前账户 · 独立演示区'],
  loading:['正在打开本机记录…','正在打开当前账户记录…'],
  footer:['记录保存在运行本应用的电脑上。天气更新需要联网。','记录按当前登录账户在服务端保存。天气更新需要联网。'],
  recordsTitle:['完成记录','完成记录'],
  recordsDescription:['按实际情况记下完成量和用时，记录保存在本机。','按实际情况记下完成量和用时，记录按当前账户在服务端保存。'],
  backToRecords:['返回完成记录','返回完成记录'],
  findLatestRecord:['请返回完成记录，找到最新的记录再更正。','请返回完成记录，找到最新的记录再更正。'],
  consent:['本人知情并同意把这次情况保存在本机','本人知情并同意把这次情况保存在当前登录账户，由服务端保存'],
  consentError:['请先确认本人知情并同意把这次情况保存在本机。','请先确认本人知情并同意把这次情况保存在当前登录账户，由服务端保存。'],
  networkError:['连接不到本机服务。请确认应用仍在运行，再重试；刚才填写的内容会保留。','连接不到本站服务。请检查网络后重试；刚才填写的内容会保留。'],
  incompleteResponse:['本机服务没有返回完整结果，请稍后重试。','本站服务没有返回完整结果，请稍后重试。'],
  weatherReceived:['本机获取','服务端获取'],
  weatherSnapshotSaved:['天气快照已保存在本机，原始文件一并留存。来源核对由提供者负责，请查看时效与预警状态；原安排需要重算。','天气快照已上传到本站当前登录账户，由服务端保存。来源核对由提供者负责，请查看时效与预警状态；原安排需要重算。'],
  exportButton:['导出本地记录','导出账户记录'],
  exportFileLabel:['本机记录','账户记录'],
  restoreIntro:['先检查备份内容，再确认恢复。恢复前会自动保留本机原记录的快照。','先导出当前账户资料留底，再检查备份内容。恢复可能替换账户中的其他资料；确认后才会把文件内容上传到本站当前登录账户。'],
  importPreviewEnding:['恢复会替换当前资料；原内容会保留快照。','恢复可能替换当前账户中的其他资料，请先导出留底。确认恢复后，文件内容将上传到本站当前登录账户。'],
  importComplete:['备份已恢复，原记录快照已保留。导入的安排仅留作历史，天气需要重新更新。','已从备份更新当前账户资料。导入的安排仅留作历史，天气需要重新更新。'],
  weatherImportTransfer:['确认导入后，快照保存在运行本应用的电脑上。','确认导入后，所选文件内容将上传到本站当前登录账户，由服务端保存。'],
  demoDescription:['天气、人员和农活都是假设，与自己的记录分开。','天气、人员和农活都是假设，与当前登录账户的真实记录分开。'],
  demoEnter:['已进入独立演示区。这里的天气、人员和农活是假设，你自己的记录保持原样。','已进入当前登录账户的独立演示区。这里的天气、人员和农活是假设，同一账户的真实记录保持原样。'],
  demoExit:['已返回自己的本机记录。','已返回当前登录账户的真实记录。'],
  predictionImported:['导入的事前估时尚未核对，不能直接作为本机事前留存依据。','导入的事前估时尚未核对，不能直接作为当前账户在本站事前留存的依据。'],
  predictionOriginUnknown:['尚不能确认这份估时曾在本机事前留存。','尚不能确认这份估时曾在当前账户于本站事前留存。'],
};
export function storageText(key,context){
  if(!Object.hasOwn(copy,key))throw new Error('未定义的界面文案');
  const scope=context?.storageScope||storageScope();
  return copy[key][scope==='account'?1:0];
}
export function savedLocation(mode,context){
  const account=(context?.storageScope||storageScope())==='account';
  return account?(mode==='demonstration'?'当前登录账户的独立演示区，由服务端保存':'当前登录账户，由服务端保存'):(mode==='demonstration'?'本机的独立演示区':'本机');
}
export function isWeatherSite(doc=globalThis.document){return doc?.getElementById?.('agri-app')?.dataset?.host==='weather-site';}
export function applyStorageFrame(doc=globalThis.document){
  if(!doc)return;
  const context={storageScope:storageScope(doc)};
  const root=doc.getElementById?.('agri-app');
  doc.title=isWeatherSite(doc)?'宜老农业 · 宜老天气通':storageText('documentTitle',context);
  for(const [id,key] of [['agri-mode-label','modeLabel'],['agri-loading-title','loading'],['agri-storage-footer','footer']]){
    const el=root?.querySelector?.(`#${id}`);if(el)el.textContent=storageText(key,context);
  }
}
