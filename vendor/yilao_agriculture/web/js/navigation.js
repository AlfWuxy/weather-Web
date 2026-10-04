// 模块只处理自己的节点和路由，宿主页面的表单、链接与锚点保持原有行为。
export const primaryNavigation=[['today','农活安排'],['farm','我的农活'],['records','完成记录']];
const pages=new Set(['today','farm','records','setup','people','weather','plot','task','person','helper','resource','feedback','correction','review','prediction','predictions']);
const parents={plot:'farm',task:'farm',review:'farm',prediction:'farm',predictions:'records',feedback:'records',correction:'records',people:'setup',weather:'setup',person:'setup',helper:'setup',resource:'setup'};
export function navigationHtml(page){
  const active=parents[page]||page;
  return primaryNavigation.map(([key,label])=>`<a class="nav-item" href="#${key}" ${key===active?'aria-current="page"':''}>${label}</a>`).join('');
}
export function isAppEvent(root,event){return Boolean(root&&event?.target&&root.contains(event.target));}
export function parseAppRoute(hash){
  const [path,query='']=String(hash||'').replace(/^#/,'').split('?');
  const [page='today',...parts]=path.split('/');
  if(page&&!pages.has(page))return null;
  try{return {page:page||'today',id:decodeURIComponent(parts.join('/')),focus:new URLSearchParams(query).get('field')||''};}catch{return null;}
}
