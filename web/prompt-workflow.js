/* Manual prompt production workspace; uses the dashboard's authenticated API. */
window.PromptWorkflowUI = (() => {
  const titles={t1:'1 \u00b7 \u00dd t\u01b0\u1edfng',t2:'2 \u00b7 C\u1ed1t truy\u1ec7n',t3:'3 \u00b7 Scene Plan',t4:'4 \u00b7 Reference',t5:'5 \u00b7 Video Prompt',info:'Scene Info'};
  const descriptions={
    t1:'Nh\u1eadp t\u01b0\u1edbng v\u00e0 s\u1ed1 ph\u00fat, t\u1ea1o prompt r\u1ed3i copy sang AI.',
    t2:'Paste \u00fd t\u01b0\u1edfng \u0111\u00e3 ch\u1ecdn. Sau khi h\u1ecfi AI, copy PART 1 \u2014 FULL STORY v\u00e0 PART 3 \u2014 CHAPTER DESIGN v\u00e0o hai \u00f4 ri\u00eang b\u00ean d\u01b0\u1edbi.',
    t3:'Ch\u1ecdn nh\u00f3m Chapter v\u00e0 t\u1ef1 nh\u1eadp s\u1ed1 Scene b\u1eaft \u0111\u1ea7u. C\u00e1c nh\u00f3m c\u00f3 th\u1ec3 l\u00e0m song song.',
    t4:'Paste ri\u00eang 3 ph\u1ea7n t\u1eeb output AI: Reference List, Scene \u2192 Reference Map, Missing Visual Decisions.',
    t5:'Ch\u1ecdn nh\u00f3m Scene Plan \u0111\u00e3 duy\u1ec7t trong T3. Sau khi duy\u1ec7t Generation JSON, b\u1ea5m n\u00fat import \u0111\u1ec3 t\u1ea1o Chapter project theo t\u00ean nh\u00f3m (v\u00ed d\u1ee5 Chapter 1+2) v\u00e0 n\u1ea1p to\u00e0n b\u1ed9 JSON \u0111\u00e3 duy\u1ec7t v\u00e0o \u0111\u00f3.',
    info:'T\u1ef1 gom to\u00e0n b\u1ed9 Scene Plan \u0111\u00e3 duy\u1ec7t; kh\u00f4ng ph\u1ee5 thu\u1ed9c T4/T5.'};
  let host,project,data,stage,group,generated,dirty,busy;
  const $=id=>host.querySelector('#'+id);
  const base=()=>'/api/admin/projects/'+encodeURIComponent(project)+'/prompt-workflow';
  const key=()=>stage+(['t3','t5'].includes(stage)?':'+group:'');
  const entry=()=>data.records[key()]||{};
  function field(name,label,value='',small=false){return `<label>${label}${small?`<input data-pw-field="${name}" value="${esc(value)}">`:`<textarea data-pw-field="${name}" spellcheck="false">${esc(value)}</textarea>`}</label>`;}
  function fields(){return Object.fromEntries([...host.querySelectorAll('[data-pw-field]')].map(e=>[e.dataset.pwField,e.value]));}
  function notice(text,error=false){$('pwNotice').textContent=text;$('pwNotice').style.color=error?'#b42318':'#067647';}
  function canonical(value){if(!/^\s*\d+(?:\s*[+,]\s*\d+)*\s*$/.test(value||''))throw Error('Nh\u1eadp Chapter nh\u01b0 1 ho\u1eb7c 1+2.');const n=[...new Set(value.match(/\d+/g).map(Number))].sort((a,b)=>a-b);if(!n.length||n[0]<1)throw Error('S\u1ed1 Chapter kh\u00f4ng h\u1ee3p l\u1ec7.');return n.join('+');}
  function firstSceneNumber(text){const m=(text||'').match(/^\s*(?:#{1,6}\s*)?(?:\*\*)?SCENE\s+(\d+)(?=\s*[\u2014\u2013:\-](?!\s*\d))/im);return m?m[1]:'';}
  async function save(approve=false){
    const f=fields();if(stage==='t2')f.output=[f.story||'',f.outline||''].filter(Boolean).join('\n\n');
    if(['t3','t5'].includes(stage)&&canonical(f.chapters)!==group)throw Error('D\u00f9ng \u00f4 nh\u00f3m Chapter v\u00e0 n\u00fat M\u1edf nh\u00f3m tr\u01b0\u1edbc khi l\u01b0u.');
    data=await api(base()+'/'+stage,{method:'PUT',body:{fields:f,revision:data.revision,approve,source_hash:generated?.source_hash||'',prompt:generated?.prompt||''}});dirty=false;
    $('pwStatus').textContent=data.statuses[key()]?.message||'Ch\u01b0a c\u00f3 k\u1ebft qu\u1ea3';
    notice(approve?'\u0110\u00e3 duy\u1ec7t. C\u00e1c b\u01b0\u1edbc sau s\u1ebd d\u00f9ng b\u1ea3n n\u00e0y.':'\u0110\u00e3 l\u01b0u nh\u00e1p.');
  }
  async function leave(){if(dirty)await save();}
  function render(){
    const rec=entry(),draft=rec.draft||rec.approved||{},f=draft.fields||{};generated=draft.source_hash?{source_hash:draft.source_hash,prompt:draft.prompt||''}:null;dirty=false;
    const plans=Object.keys(data.records).filter(k=>k.startsWith('t3:')&&data.records[k].approved);
    const groups=(stage==='t5'?plans:Object.keys(data.records).filter(k=>k.startsWith(stage+':'))).sort((a,b)=>Number(a.split(':')[1].split('+')[0])-Number(b.split(':')[1].split('+')[0]));
    let inputs='';
    if(stage==='t1')inputs=field('champion','T\u00ean t\u01b0\u1edbng',f.champion||'',true)+field('minutes','Th\u1eddi l\u01b0\u1ee3ng phim (ph\u00fat)',f.minutes||'20',true);
    if(stage==='t2')inputs=field('idea','\u00dd t\u01b0\u1edfng \u0111\u00e3 ch\u1ecdn',f.idea||'')+field('minutes','Th\u1eddi l\u01b0\u1ee3ng phim (ph\u00fat)',f.minutes||data.records.t1?.approved?.fields.minutes||'20',true);
    if(['t3','t5'].includes(stage))inputs=field('chapters','Nh\u00f3m Chapter \u0111ang l\u00e0m',group,true);
    if(stage==='t3')inputs+=field('scene_start','Scene b\u1eaft \u0111\u1ea7u t\u1eeb s\u1ed1 (nh\u1eadp th\u1ee7 c\u00f4ng)',f.scene_start||firstSceneNumber(f.output)||(group.split('+').map(Number).includes(1)?'1':''),true);
    let outputs;
    if(stage==='t2')outputs=field('story','PART 1 \u2014 FULL STORY (copy ri\u00eang ph\u1ea7n n\u00e0y t\u1eeb output AI)',f.story||'')+field('outline','PART 3 \u2014 CHAPTER DESIGN (copy to\u00e0n b\u1ed9 ph\u1ea7n n\u00e0y t\u1eeb output AI)',f.outline||'');
    else if(stage==='t4')outputs=field('reference_list','1. REFERENCE LIST (paste ri\u00eang ph\u1ea7n n\u00e0y)',f.reference_list||f.reference_plan||'')+field('reference_map','2. SCENE \u2192 REFERENCE MAP (paste ri\u00eang ph\u1ea7n n\u00e0y)',f.reference_map||'')+field('missing_decisions','3. MISSING VISUAL DECISIONS (paste ri\u00eang ph\u1ea7n n\u00e0y; n\u1ebfu kh\u00f4ng c\u00f3 th\u00ec ghi None)',f.missing_decisions||'');
    else outputs=field('output','K\u1ebft qu\u1ea3 AI \u2014 paste to\u00e0n b\u1ed9 c\u00e2u tr\u1ea3 l\u1eddi',f.output||'');
    host.innerHTML=`<div class="pw-workspace"><p>Copy prompt \u2192 h\u1ecfi AI b\u00ean ngo\u00e0i \u2192 paste k\u1ebft qu\u1ea3 \u2192 l\u01b0u / duy\u1ec7t. D\u1eef li\u1ec7u thu\u1ed9c Project l\u1edbn, d\u00f9ng chung gi\u1eefa c\u00e1c Chapter.</p>
      <nav class="project-actions">${Object.entries(titles).map(([s,t])=>`<button class="btn ${s===stage?'btn-p':''}" data-pw="tab" data-stage="${s}">${t}</button>`).join('')}</nav>
      <h3>${titles[stage]}</h3><p>${descriptions[stage]}</p><p id="pwStatus">${esc(data.statuses[key()]?.message||'Ch\u01b0a c\u00f3 k\u1ebft qu\u1ea3')}</p>
      ${rec.approved&&JSON.stringify(draft.fields)!==JSON.stringify(rec.approved.fields)?'<p>\u0110ang xem b\u1ea3n nh\u00e1p. C\u00e1c b\u01b0\u1edbc sau v\u1eabn d\u00f9ng b\u1ea3n \u0111\u00e3 duy\u1ec7t.</p>':''}<div id="pwNotice" role="status"></div>
      ${stage==='t3'?`<div class="project-actions">${groups.map(k=>`<button class="btn ${k===key()?'btn-p':''}" data-pw="group" data-group="${esc(k.split(':')[1])}">Chapter ${esc(k.split(':')[1])} \u00b7 ${esc(data.statuses[k]?.state||'draft')}</button>`).join('')}<input id="pwGroup" placeholder="1+2" value="${esc(group)}"><button class="btn" data-pw="open-group">M\u1edf nh\u00f3m / th\u00eam l\u01b0\u1ee3t</button></div>`:''}
      ${stage==='t5'?`<div class="project-actions">${groups.map(k=>{const g=k.split(':')[1];return `<button class="btn ${g===group?'btn-p':''}" data-pw="group" data-group="${esc(g)}">Chapter ${esc(g)} \u00b7 approved</button>`;}).join('')||'Ch\u01b0a c\u00f3 nh\u00f3m Scene Plan T3 \u0111\u00e3 duy\u1ec7t.'}</div>`:''}
      ${['t4','t5','info'].includes(stage)?`<p>Scene Plan \u0111\u00e3 l\u01b0u: ${esc(plans.map(k=>k.slice(3)+' ('+data.statuses[k].state+')').join(', ')||'ch\u01b0a c\u00f3')}.</p>`:''}
      ${['t3','t4','t5','info'].includes(stage)?`<p>Danh s\u00e1ch Chapter trong Outline T2 (kh\u00f4ng ph\u1ea3i nh\u00f3m Scene Plan \u0111ang m\u1edf): ${esc(data.outline_chapters.join(', ')||'ch\u01b0a nh\u1eadn di\u1ec7n \u0111\u01b0\u1ee3c')}.</p>`:''}
      <div class="project-form">${inputs}</div><div class="project-actions"><button class="btn btn-p" data-pw="generate">T\u1ea1o prompt \u0111\u1ea7y \u0111\u1ee7</button><button class="btn" data-pw="copy-prompt">Copy prompt</button></div>
      <details ${generated?'open':''}><summary>Prompt \u0111\u1ea7y \u0111\u1ee7</summary><textarea id="pwPrompt" readonly>${esc(generated?.prompt||'')}</textarea></details><details><summary>D\u1eef li\u1ec7u ngu\u1ed3n</summary><pre id="pwSources">B\u1ea5m T\u1ea1o prompt \u0111\u1ec3 xem d\u1eef li\u1ec7u.</pre></details>
      <div class="project-form">${outputs}</div><div class="project-actions"><button class="btn" data-pw="save">L\u01b0u nh\u00e1p</button><button class="btn btn-p" data-pw="approve">Duy\u1ec7t k\u1ebft qu\u1ea3</button><button class="btn" data-pw="copy-result">Copy k\u1ebft qu\u1ea3</button>${stage==='info'?'<button class="btn" data-pw="copy-json">Copy JSON \u0111\u1ec3 import</button><p>Tr\u1edf v\u1ec1 Chapter \u0111\u00fang r\u1ed3i d\u00f9ng Import Scene Info \u0111\u1ec3 ki\u1ec3m tra v\u00e0 import.</p>':''}${stage==='t5'&&rec.approved?`<button class="btn btn-p" data-pw="import-chapters">T\u1ea1o Chapter ${esc(group)} r\u1ed3i import JSON \u0111\u00e3 duy\u1ec7t</button>`:''}<div id="pwChapterImport" hidden></div><button class="btn" data-pw="download">T\u1ea3i b\u1ea3n l\u01b0u</button></div>
      <details><summary>B\u1ea3n \u0111\u00e3 duy\u1ec7t</summary><pre>${esc(rec.approved?JSON.stringify(rec.approved.fields,null,2):'Ch\u01b0a c\u00f3')}</pre></details><details><summary>L\u1ecbch s\u1eed (${rec.history?.length||0})</summary>${(rec.history||[]).map((h,i)=>`<details><summary>${new Date(h.saved_at*1000).toLocaleString()} <button class="btn" data-pw="restore" data-index="${i}">Kh\u00f4i ph\u1ee5c b\u1ea3n nh\u00e1p</button></summary><pre>${esc(JSON.stringify(h.fields,null,2))}</pre></details>`).join('')}</details></div>`;
    const chapterField=host.querySelector('[data-pw-field="chapters"]');if(chapterField)chapterField.readOnly=true;
    host.oninput=e=>{if(e.target.matches('[data-pw-field]')){dirty=true;if(['champion','minutes','idea','chapters','scene_start'].includes(e.target.dataset.pwField)){generated=null;$('pwPrompt').value='';}}};
    host.onclick=e=>{const b=e.target.closest('[data-pw]');if(!b||busy)return;e.preventDefault();run(b).catch(err=>notice(err.message,true));};
  }
  function parseJSONTolerant(text){try{return JSON.parse(text);}catch{}let out='',quoted=false,escaped=false;for(let i=0;i<text.length;i++){const c=text[i];if(quoted){out+=c;if(escaped)escaped=false;else if(c==='\\')escaped=true;else if(c==='\"')quoted=false;}else if(c==='\"'){quoted=true;out+=c;}else if(c===','){let j=i+1;while(j<text.length&&/\s/.test(text[j]))j++;if(text[j]===']'||text[j]==='}')continue;out+=c;}else out+=c;}return JSON.parse(out);}
  function extractJSON(text){for(const candidate of [text,...[...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)].map(m=>m[1])])try{const v=parseJSONTolerant(candidate);if(v&&typeof v==='object')return JSON.stringify(v,null,2);}catch{}throw Error('Kh\u00f4ng t\u00ecm th\u1ea5y JSON h\u1ee3p l\u1ec7.');}

  async function run(button){busy=true;host.querySelectorAll('button').forEach(b=>b.disabled=true);try{const a=button.dataset.pw;
    if(['tab','group','open-group'].includes(a)){await leave();if(a==='tab'){stage=button.dataset.stage;const firstPlan=Object.keys(data.records).filter(k=>k.startsWith('t3:')&&data.records[k].approved).sort((x,y)=>Number(x.slice(3).split('+')[0])-Number(y.slice(3).split('+')[0]))[0];group=stage==='t5'?(firstPlan?.slice(3)||'1'):'1';}else group=canonical(a==='group'?button.dataset.group:$('pwGroup').value);render();}
    else if(a==='generate'){generated=await api(base()+'/'+stage+'/generate',{method:'POST',body:{fields:fields()}});$('pwPrompt').value=generated.prompt;$('pwSources').textContent=JSON.stringify(generated.sources,null,2);dirty=true;await save();notice('Prompt \u0111\u00e3 t\u1ea1o. Copy sang AI, r\u1ed3i paste k\u1ebft qu\u1ea3 v\u00e0o c\u00e1c \u00f4.');}
    else if(a==='save'||a==='approve'){await save(a==='approve');if(a==='approve'){render();notice('\u0110\u00e3 duy\u1ec7t.');}}
    else if(a==='import-chapters'){const started=performance.now();notice('\u0110ang t\u1ea1o Chapter project v\u00e0 import JSON...');const result=await api(base()+'/t5/import-chapters',{method:'POST',body:{fields:{chapters:group}}});const seconds=((performance.now()-started)/1000).toFixed(1);$('pwChapterImport').innerHTML=`<p>\u0110\u00e3 import ${result.imported_count} Scene v\u00e0o ${esc(result.chapter_group)}.</p>`;notice(`\u0110\u00e3 import JSON v\u00e0o ${result.chapter_group} (${seconds}s).`);window.dispatchEvent(new Event('prompt-workflow-scenes-imported'));}

    else if(a==='split'){const r=await api('/api/admin/prompt-workflow/split',{method:'POST',body:{text:fields().output}});for(const [src,dst] of Object.entries({reference_plan:'reference_plan',reference_map:'reference_map'}))if(r.fields[src])host.querySelector(`[data-pw-field="${dst}"]`).value=r.fields[src];dirty=true;notice('Ki\u1ec3m tra c\u00e1c ph\u1ea7n v\u1eeba t\u00e1ch tr\u01b0\u1edbc khi duy\u1ec7t.');}
    else if(a==='copy-prompt'){if(!generated?.prompt)throw Error('T\u1ea1o prompt tr\u01b0\u1edbc.');const latest=await api(base()+'/'+stage+'/generate',{method:'POST',body:{fields:fields()}});if(latest.source_hash!==generated.source_hash||latest.prompt!==generated.prompt)throw Error('Ngu\u1ed3n \u0111\u00e3 \u0111\u1ed5i. H\u00e3y t\u1ea1o l\u1ea1i prompt.');await copy(generated.prompt);notice('\u0110\u00e3 copy prompt.');}
    else if(a==='copy-result'||a==='copy-json'){const f=fields();const result=stage==='t2'?[f.story,f.outline].filter(Boolean).join('\n\n'):stage==='t4'?[f.reference_list,f.reference_map,f.missing_decisions].filter(Boolean).join('\n\n'):f.output;await copy(a==='copy-json'?extractJSON(f.output):result);notice('\u0110\u00e3 copy.');}
    else if(a==='restore'){const old=entry().history[Number(button.dataset.index)];for(const [n,v] of Object.entries(old.fields)){const c=host.querySelector(`[data-pw-field="${n}"]`);if(c)c.value=v;}generated={source_hash:old.source_hash,prompt:old.prompt};dirty=true;notice('\u0110\u00e3 kh\u00f4i ph\u1ee5c v\u00e0o nh\u00e1p.');}
    else if(a==='download'){await leave();const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const link=document.createElement('a');link.href=u;link.download='prompt-workflow.json';link.click();setTimeout(()=>URL.revokeObjectURL(u),1000);}
  }finally{busy=false;host.querySelectorAll('button').forEach(b=>b.disabled=false);}}
  window.addEventListener('beforeunload',e=>{if(dirty&&host?.isConnected){e.preventDefault();e.returnValue='';}});
  return {async open(id,container){host=container;project=id;stage='t1';group='1';busy=false;dirty=false;data=await api(base());project=data.project_id;render();},async flush(){if(busy)throw Error('\u0110ang l\u01b0u, vui l\u00f2ng ch\u1edd xong.');if(host?.isConnected&&dirty)await save();}};
})();
