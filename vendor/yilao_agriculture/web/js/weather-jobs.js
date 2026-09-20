// 天气更新可由后台完成；停止查看只取消查询，不取消或重新提交后台任务。
const pendingStatuses=new Set(['queued','running']);
const terminalStatuses=new Set(['succeeded','failed']);
const abortError=()=>Object.assign(new Error('已停止本次查看'),{name:'AbortError'});
function delay(ms,signal){
  return new Promise((resolve,reject)=>{
    if(signal.aborted){reject(abortError());return;}
    const stop=()=>{clearTimeout(timer);reject(abortError());};
    const timer=setTimeout(()=>{signal.removeEventListener('abort',stop);resolve();},ms);
    signal.addEventListener('abort',stop,{once:true});
  });
}
function abortable(promise,signal){
  return new Promise((resolve,reject)=>{
    const stop=()=>reject(abortError());
    if(signal.aborted){reject(abortError());return;}
    signal.addEventListener('abort',stop,{once:true});
    Promise.resolve(promise).then(resolve,reject).finally(()=>signal.removeEventListener('abort',stop));
  });
}
const accountChanged=error=>error?.details?.error?.code==='ACCOUNT_CONTEXT_CHANGED'||error?.details?.code==='ACCOUNT_CONTEXT_CHANGED';

export function createWeatherJobs({request,getContext,onChange=()=>{},applyResult,wait=delay,now=Date.now,interval=2000,observeMs=120000,setTimer=setTimeout,clearTimer=clearTimeout}){
  const entries=new Map();let generation=0;
  const scope=context=>JSON.stringify([context.accountKey,context.mode]);
  const key=(plotId,context=getContext())=>JSON.stringify([scope(context),plotId]);
  const ticket=()=>({generation,context:{...getContext()}});
  const current=t=>!t.signal?.aborted&&t.generation===generation&&scope(getContext())===scope(t.context)&&getContext().routeKey===t.context.routeKey;
  const emit=(entry,t)=>{if(current(t))onChange(snapshot(entry));};
  const snapshot=entry=>entry?{plotId:entry.plotId,job:entry.job?{...entry.job}:null,phase:entry.phase,
    message:entry.message,observing:!!entry.observer,canContinue:!!entry.job&&!entry.observer&&['paused','timeout','request_error'].includes(entry.phase)}:null;
  function validateJob(result,entry){
    const job=result?.job;
    if(!job||typeof job.id!=='string'||!/^[-A-Za-z0-9_]{1,128}$/.test(job.id)||job.plot_id!==entry.plotId||
      (!pendingStatuses.has(job.status)&&!terminalStatuses.has(job.status))||(entry.job&&entry.job.id!==job.id))throw new Error('天气更新结果不完整，请稍后继续查看。');
    return {...job};
  }
  function failure(entry,error,t){
    if(accountChanged(error)){
      entry.phase='account_changed';entry.message='登录账户已变化，请刷新页面后继续';
    }else{
      entry.phase=entry.job?'request_error':'uncertain';
      entry.message=entry.job?`${error.message||'暂时没有读到更新结果。'} 稍后可继续查看，已有更新不会重复提交。`:
        `${error.message||'暂时没有读到更新结果。'} 尚未确认这次更新是否已开始，请稍后重新打开页面核对。`;
    }
    emit(entry,t);
  }
  async function accept(result,entry,t){
    if(!current(t))return false;
    if(result?.job){
      entry.job=validateJob(result,entry);
      entry.phase=entry.job.status==='succeeded'?'running':entry.job.status;
      entry.message=entry.job.message||(entry.phase==='running'?'正在更新天气，请稍候。':'已请求更新天气，请稍候。');
      if(entry.phase==='failed'){
        entry.message=entry.job.message||'这次天气更新未完成，原有资料已保留。';
        emit(entry,t);return true;
      }
      if(pendingStatuses.has(entry.job.status)){emit(entry,t);return false;}
    }
    const incoming=result?.state;
    if(!incoming||incoming.mode!==entry.context.mode||!Number.isSafeInteger(incoming.revision))throw new Error('暂时没有读到完整资料，请稍后继续查看。');
    if(incoming.revision<getContext().revision){
      entry.phase='paused';entry.message='资料已有更新，请继续查看最新天气结果。';emit(entry,t);return true;
    }
    // 取得天气后可能还需读估时；回调在真正写入页面前再次核对当前视图。
    if(await applyResult(result,()=>current(t))===false){
      if(current(t)){entry.phase='paused';entry.message='资料已有更新，请继续查看最新天气结果。';emit(entry,t);}
      return true;
    }
    if(!current(t))return true;
    entry.phase='succeeded';entry.message='天气已更新，请核对来源、日期和预警情况。';emit(entry,t);return true;
  }
  function observe(entry,t,{immediate=false}={}){
    if(entry.observer)return entry.completion;
    const controller=new AbortController();entry.observer=controller;
    const observation={...t,signal:controller.signal};
    const started=now();
    const timeoutMessage='暂未看到完成结果，后台更新不会因此停止，可以稍后继续查看。';
    const deadline=setTimer(()=>{
      if(entry.observer!==controller)return;
      entry.observer=null;entry.phase='timeout';entry.message=timeoutMessage;
      controller.abort();emit(entry,t);
    },observeMs);
    entry.completion=(async()=>{
      try{
        let first=true;
        while(current(observation)){
          if(!(first&&immediate))await wait(Math.min(interval,Math.max(0,observeMs-(now()-started))),controller.signal);
          if(!current(observation))return;
          if(now()-started>=observeMs){
            entry.phase='timeout';entry.message=timeoutMessage;return;
          }
          first=false;
          const result=await abortable(request(`/api/weather/jobs/${entry.job.id}`,{signal:controller.signal}),controller.signal);
          if(!current(observation))return;
          if(await abortable(accept(result,entry,observation),controller.signal))return;
        }
      }catch(error){if(error.name!=='AbortError')failure(entry,error,observation);}
      finally{
        clearTimer(deadline);
        if(entry.observer===controller){entry.observer=null;emit(entry,t);}
      }
    })();
    emit(entry,t);return entry.completion;
  }
  function pause(){
    generation++;
    for(const entry of entries.values()){
      entry.observer?.abort();entry.observer=null;
      if(['queued','running'].includes(entry.phase)){entry.phase='paused';entry.message='更新已在后台开始，可以继续查看。';}
    }
  }
  function resume(plotId){
    const entry=entries.get(key(plotId));
    if(!entry?.job||['failed','succeeded','account_changed'].includes(entry.phase))return Promise.resolve();
    return observe(entry,ticket(),{immediate:true});
  }
  async function start(plotId,body){
    const t=ticket(),entryKey=key(plotId,t.context),existing=entries.get(entryKey);
    if(t.context.mode!=='real')return null;
    if(existing&&!['failed','succeeded'].includes(existing.phase)){
      if(existing.submission)return existing.submission;
      return {completion:existing.job?resume(plotId):Promise.resolve()};
    }
    const entry={plotId,context:t.context,phase:'submitting',message:'正在请求更新天气…',job:null,observer:null};
    entries.set(entryKey,entry);emit(entry,t);
    entry.submission=(async()=>{
      try{
        const result=await request('/api/weather/refresh',{method:'POST',body});
        // 提交返回后即使已离开天气页，也要留住编号，回来只查询原任务。
        if(result?.job){entry.job=validateJob(result,entry);entry.phase='paused';entry.message='已请求更新天气，可以继续查看。';}
        if(!current(t)){
          if(!entry.job){entry.phase='succeeded';entry.message='已收到更新结果，请重新打开记录查看。';}
          return {completion:Promise.resolve()};
        }
        const done=await accept(result,entry,t);
        return {completion:!done&&entry.job?observe(entry,t):Promise.resolve()};
      }catch(error){failure(entry,error,t);return {completion:Promise.resolve()};}
      finally{entry.submission=null;}
    })();
    return entry.submission;
  }
  return {start,resume,pause,get:plotId=>snapshot(entries.get(key(plotId)))};
}
