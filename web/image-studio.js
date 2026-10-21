/* Dola Image Studio: private, authenticated original-image gallery. */
const ImageStudioUI = (() => {
  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/></svg>';
  const spark = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4Z"/></svg>';
  const phases = {queued:'Đang xếp hàng',waiting_capacity:'Chờ suất chạy song song',waiting_account:'Chờ tài khoản đủ điều kiện',preparing:'Đang chuẩn bị',uploading:'Đang tải ảnh tham chiếu',submitting:'Đang gửi yêu cầu',verifying:'Đang xác minh',generating:'Dola đang tạo ảnh',downloading:'Đang tải ảnh gốc',completed:'Hoàn tất',failed:'Chưa hoàn tất',interrupted:'Cần kiểm tra',cancelled:'Đã hủy'};
  const state = {options:null,jobs:[],offset:0,total:0,filter:'all',model:'Seedream 5.0 Pro',ratio:'16:9',busy:false,loading:false,signature:'',requestId:null,blobs:new Map(),preview:null,references:[],uploading:false};
  const el = id => document.getElementById(id);
  const root = el('tab-images');
  root.innerHTML = `
    <div class="is-head"><div><div class="is-eyebrow">Dola · Image Studio</div><h1>Từ ý tưởng thành hình ảnh.</h1><p>Chọn phong cách của bạn. Tạo ảnh và lưu bản gốc không watermark.</p></div><span class="is-chip">${spark} Bản gốc · Không watermark</span></div>
    <div class="is-layout">
      <form id="isForm" class="is-compose"><div class="is-compose-title"><span>${spark} Tạo một điều mới</span><button class="is-submit" id="isGenerate" type="submit" disabled>${spark}<span>Tạo ảnh</span></button></div><div class="is-form-body">
        <div class="is-field is-prompt-field"><label class="is-label" for="isPrompt">Mô tả ý tưởng <small id="isCount">0 / 12.000</small></label><textarea id="isPrompt" required maxlength="12000" placeholder="Một ngôi nhà nhỏ bên hồ, rừng thông phủ sương, ánh sáng bình minh dịu nhẹ…"></textarea>
        <div class="is-presets"><button type="button" data-preset="landscape">↗ Phong cảnh</button><button type="button" data-preset="product">↗ Sản phẩm</button><button type="button" data-preset="illustration">↗ Minh họa</button></div></div>
        <div class="is-field"><div class="is-label">Ảnh tham chiếu <small id="isReferenceCount">0 / 6</small></div><button type="button" id="isPasteArea" class="is-paste-area">${icon}<strong>Dán ảnh bằng Ctrl+V</strong><span>hoặc nhấn để chọn file · PNG, JPG, WebP · tối đa 15 MB/ảnh</span></button><input id="isReferenceFiles" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><div id="isReferences" class="is-references"></div><p id="isUploadStatus" class="is-note" role="status"></p></div>
        <div class="is-config"><div class="is-field"><div class="is-label" id="isModelLabel">Model</div><div id="isModels" class="is-models" role="group" aria-labelledby="isModelLabel"></div><p class="is-note" id="isModelNote">Đang tải lựa chọn…</p></div>
        <div class="is-field"><div class="is-label" id="isRatioLabel">Khung hình <small id="isRatioValue">16:9</small></div><div id="isRatios" class="is-ratios" role="group" aria-labelledby="isRatioLabel"></div></div>
        <div class="is-settings"><div class="is-field"><label class="is-label" for="isStyle">Phong cách</label><select id="isStyle"><option value="auto">Tự nhiên</option></select></div><div class="is-field"><label class="is-label" for="isAccount">Tài khoản Dola</label><select id="isAccount"><option value="">Tự chọn tài khoản rảnh</option></select><p class="is-note">Dùng tài khoản đang bật Dispatch. Tác vụ chờ khi tài khoản đang bận.</p><p id="isImageLimits" class="is-note" role="status"></p></div></div>
        <div class="is-field"><label class="is-label" for="isBatchCount">Số lượt tạo</label><select id="isBatchCount">${[1,2,3,4,5,6,7,8,9,10].map(n=>`<option value="${n}">${n} lượt${n===1?'':' · cùng prompt & cấu hình'}</option>`).join('')}</select><p class="is-note">Mỗi lượt là một yêu cầu riêng và sử dụng credit riêng. Dola có thể trả nhiều ảnh trong mỗi lượt.</p></div>
        </div><div id="isFormError" class="is-error" role="alert" hidden></div><p class="is-footer-note">Ảnh được lưu tự động khi hoàn tất.</p>
      </div></form>
      <div><div class="is-gallery-head"><div><h2>Không gian sáng tạo</h2><p id="isSummary" aria-live="polite">Những ý tưởng của bạn sẽ xuất hiện ở đây.</p></div><button class="btn" type="button" id="isRefresh" aria-label="Làm mới thư viện ảnh">↻ Làm mới</button></div>
        <div class="is-filters" role="group" aria-label="Lọc kết quả"><button type="button" data-filter="all" aria-pressed="true">Tất cả</button><button type="button" data-filter="completed" aria-pressed="false">Hoàn tất</button><button type="button" data-filter="active" aria-pressed="false">Đang tạo</button><button type="button" data-filter="failed" aria-pressed="false">Cần kiểm tra</button></div>
        <div id="isLoadError" class="is-error" role="alert" hidden></div>
        <div id="isEmpty" class="is-empty"><div class="is-empty-art" aria-hidden="true"><div></div><div>${icon}</div></div><h3>Bắt đầu bằng một ý tưởng</h3><p>Mô tả khung cảnh, ánh sáng và cảm xúc. Dola sẽ giúp bạn tạo nên bức ảnh đầu tiên.</p></div>
        <div id="isGrid" class="is-grid"></div><div class="is-pagination"><button type="button" class="btn" id="isPrev" disabled>← Trước</button><span id="isPage"></span><button type="button" class="btn" id="isNext" disabled>Sau →</button></div>
      </div>
    </div>`;
  const dialog = document.createElement('dialog');
  dialog.className='is-dialog'; dialog.id='isPreviewDialog'; dialog.setAttribute('aria-label','Xem ảnh gốc');
  document.body.append(dialog);
  function error(id,message){el(id).textContent=message||'';el(id).hidden=!message;}
  function toast(message){let node=el('isToast');if(!node){node=document.createElement('div');node.id='isToast';node.className='is-toast';node.setAttribute('role','status');document.body.append(node);}node.textContent=message;clearTimeout(node._timer);node._timer=setTimeout(()=>node.remove(),4500);}
  function saveDraft(){try{sessionStorage.setItem('dola_image_draft',JSON.stringify({prompt:el('isPrompt').value,model:state.model,ratio:state.ratio,style:el('isStyle').value,account:el('isAccount').value,references:state.references,batchCount:el('isBatchCount').value}));}catch{}}
  let draft={};try{draft=JSON.parse(sessionStorage.getItem('dola_image_draft')||'{}');}catch{}
  el('isPrompt').value=draft.prompt||'';
  el('isBatchCount').value=String(Math.max(1,Math.min(10,Number(draft.batchCount)||1)));
  state.references=Array.isArray(draft.references)?draft.references.filter(r=>/^[a-f0-9]{64}$/.test(r.id)).slice(0,6):[];
  function renderReferences(){
    el('isReferenceCount').textContent=`${state.references.length} / 6`;
    el('isReferences').innerHTML=state.references.map((r,index)=>`<div class="is-reference"><img alt="Ảnh tham chiếu ${index+1}"><span>Ảnh ${index+1}</span><button type="button" data-remove-reference="${index}" aria-label="Xóa ảnh tham chiếu ${index+1}" ${state.busy||state.uploading?'disabled':''}>×</button></div>`).join('');
    state.references.forEach(async(r,index)=>{const img=el('isReferences').children[index]?.querySelector('img');try{const src=await blob('/api/admin/image-library/'+r.id);if(img?.isConnected)img.src=src;}catch{if(img?.isConnected)img.alt='Ảnh không còn tồn tại; hãy chọn lại';}});
  }
  async function addReferences(files){
    if(state.busy||state.uploading){toast('Vui lòng chờ thao tác hiện tại hoàn tất.');return;}
    files=[...files];if(!files.length)return;
    if(files.length+state.references.length>6){error('isFormError','Tối đa 6 ảnh tham chiếu. Xóa bớt ảnh trước khi thêm.');return;}
    if(files.some(f=>!['image/png','image/jpeg','image/webp'].includes(f.type)||f.size>15*1024*1024)){error('isFormError','Chỉ nhận PNG, JPG, WebP, tối đa 15 MB mỗi ảnh.');return;}
    state.uploading=true;el('isGenerate').disabled=true;el('isUploadStatus').textContent='Đang tải ảnh tham chiếu…';error('isFormError','');renderReferences();
    try{const body=new FormData();files.forEach((f,index)=>body.append('files',f,f.name||`clipboard-${Date.now()}-${index}.png`));const response=await fetch('/api/admin/image-library',{method:'POST',headers:{'X-Admin-Key':adminKey},body});if(response.status===401)showLogin();const data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'Không tải được ảnh tham chiếu.');for(const r of data.images)if(!state.references.some(x=>x.id===r.id))state.references.push({id:r.id,name:r.name});state.requestId=null;saveDraft();el('isUploadStatus').textContent='Đã sẵn sàng. Mô tả cách bạn muốn sử dụng hoặc chỉnh sửa ảnh.';}catch(e){error('isFormError',e.message);el('isUploadStatus').textContent='Tải ảnh chưa thành công. Hãy thử lại.';}finally{state.uploading=false;renderReferences();el('isGenerate').disabled=state.busy||!state.options?.accounts.length;}
  }
  function count(){el('isCount').textContent=`${el('isPrompt').value.length.toLocaleString('vi-VN')} / 12.000`;}
  count();
  function selections(){
    root.querySelectorAll('[data-model]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.model===state.model)));
    root.querySelectorAll('[data-ratio]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.ratio===state.ratio)));
    el('isRatioValue').textContent=state.ratio;
    el('isModelNote').textContent=state.model.includes('5.0')?'Seedream 5.0 Pro · Dola ghi mức sử dụng credit gấp 4 lần.':'Seedream 4.5 · Phù hợp thử ý tưởng và sáng tạo hằng ngày.';
  }
  async function options(){
    const data=await api('/api/admin/image-studio/options');
    const first=!state.options;state.options=data;
    if(first){
      state.model=data.models.includes(draft.model)?draft.model:state.model;
      state.ratio=data.ratios.includes(draft.ratio)?draft.ratio:state.ratio;
      el('isModels').innerHTML=data.models.map(m=>`<button type="button" class="is-model" data-model="${esc(m)}" aria-pressed="false"><strong>${esc(m)}</strong><span>${m.includes('5.0')?'Chi tiết & chất lượng':'Nhanh & linh hoạt'}</span></button>`).join('');
      el('isRatios').innerHTML=data.ratios.map(r=>`<button type="button" class="is-ratio" data-ratio="${r}" aria-label="Tỷ lệ ${r}" aria-pressed="false"><i style="--ratio:${r.replace(':','/')}" aria-hidden="true"></i>${r}</button>`).join('');
      el('isStyle').innerHTML=Object.entries(data.styles).map(([k,v])=>`<option value="${esc(k)}">${esc(v)}</option>`).join('');
      if(data.styles[draft.style])el('isStyle').value=draft.style;
    }
    const selected=first?(draft.account||''):el('isAccount').value;
    el('isAccount').innerHTML='<option value="">Tự chọn tài khoản rảnh</option>'+data.accounts.map(a=>`<option value="${esc(a.name)}">${esc(a.label)}${a.busy?' · Đang bận':''}</option>`).join('');
    if(data.accounts.some(a=>a.name===selected))el('isAccount').value=selected;
    el('isImageLimits').textContent=(data.image_limited_accounts||[]).map(a=>`${a.label}: hết lượt tạo ảnh đến ${new Date(a.retry_at*1000).toLocaleString('vi-VN')}`).join(' · ');
    el('isGenerate').disabled=state.busy||state.uploading||!data.accounts.length;
    if(!data.accounts.length)error('isFormError','Chưa có tài khoản sẵn sàng. Kiểm tra đăng nhập và bật Dispatch trong tab Accounts.');
    selections();renderReferences();
  }
  async function blob(url){
    if(state.blobs.has(url))return state.blobs.get(url);
    const pending=(async()=>{const response=await fetch(url,{headers:{'X-Admin-Key':adminKey}});if(!response.ok){if(response.status===401)showLogin();throw new Error('Không tải được ảnh. Thử làm mới hoặc kiểm tra đăng nhập.');}return URL.createObjectURL(await response.blob());})();
    state.blobs.set(url,pending);
    try{return await pending;}catch(e){state.blobs.delete(url);throw e;}
  }
  function trimBlobs(){const keep=new Set([...state.jobs.flatMap(j=>j.outputs.map(o=>o.url)),...state.references.map(r=>'/api/admin/image-library/'+r.id)]);if(state.preview)keep.add(state.preview.url);for(const [url,promise] of state.blobs)if(!keep.has(url)){promise.then(URL.revokeObjectURL).catch(()=>{});state.blobs.delete(url);}}
  async function thumbnails(){await Promise.all([...root.querySelectorAll('img[data-src]')].map(async img=>{try{const src=await blob(img.dataset.src);if(img.isConnected)img.src=src;}catch{if(img.isConnected){img.alt='Không tải được ảnh — nhấn Làm mới để thử lại';img.dataset.failed='true';}}}));}
  function render(){
    const rows=state.jobs.filter(j=>state.filter==='all'||(state.filter==='active'?['queued','running'].includes(j.status):state.filter==='failed'?['failed','interrupted'].includes(j.status):j.status===state.filter));
    el('isEmpty').hidden=rows.length>0;
    el('isEmpty').querySelector('h3').textContent=state.total?'Chưa có ảnh trong bộ lọc này':'Bắt đầu bằng một ý tưởng';
    el('isEmpty').querySelector('p').textContent=state.total?'Chọn bộ lọc khác hoặc chuyển trang để xem thêm tác vụ.':'Mô tả khung cảnh, ánh sáng và cảm xúc. Dola sẽ giúp bạn tạo nên bức ảnh đầu tiên.';
    el('isGrid').innerHTML=rows.map(job=>{
      const outputs=job.outputs.length?job.outputs:[null];
      return outputs.map((output,index)=>output?.deleted_at?'':`<article class="is-card" data-job="${job.id}">
        ${output?`<button type="button" class="is-preview" data-action="preview" data-id="${job.id}" data-index="${index}" aria-label="Xem ảnh: ${esc(job.prompt.slice(0,100))}"><img data-src="${esc(output.url)}" alt="${esc(job.prompt.slice(0,180))}" loading="lazy"><span class="is-preview-tag">${output.width} × ${output.height} · Bản gốc</span></button>`:['queued','running'].includes(job.status)?`<div class="is-pending"><div class="is-orbit" aria-hidden="true"></div><span>${esc(phases[job.phase]||job.phase)}</span></div>`:''}
        <div class="is-card-content"><div class="is-card-meta"><span class="is-status ${job.status}">${esc(phases[job.status]||job.status)}</span><time>${new Date(job.created_at*1000).toLocaleString('vi-VN',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></div>
        <p class="is-card-prompt" title="${esc(job.prompt)}">${esc(job.prompt)}</p><div class="is-card-spec"><span>${esc(job.model)}</span><span>${esc(job.ratio)}</span><span>${esc(state.options?.styles[job.style]||job.style)}</span></div>
        ${job.error?`<p class="is-error" style="margin:12px 0 0">${esc(job.error)}</p>`:''}
        <div class="is-card-actions">${output?`<button class="btn" data-action="download" data-id="${job.id}" data-index="${index}">↓ Tải ảnh</button><button class="btn" data-action="library" data-id="${job.id}" data-index="${index}">+ Thư viện</button>`:''}<button class="btn" data-action="reuse" data-id="${job.id}">Dùng lại</button>${job.status==='queued'?`<button class="btn" data-action="cancel" data-id="${job.id}">Hủy chờ</button>`:''}${['failed','interrupted'].includes(job.status)&&job.conversation_id?`<button class="btn" data-action="resume" data-id="${job.id}">Kiểm tra tiếp</button>`:''}</div></div></article>`).join('');
    }).join('');
    root.querySelectorAll('.is-card[data-job]').forEach(card=>{
      const job=state.jobs.find(j=>j.id===card.dataset.job);
      if(job&&!['queued','running'].includes(job.status)){
        for(const [action,label] of [['retry','Retry'],['switch','Đổi acc + Retry']]){
          const b=document.createElement('button');b.className='btn';b.dataset.action=action;b.dataset.id=job.id;b.textContent=label;
          card.querySelector('.is-card-actions').append(b);
        }
      }
      const previewButton=card.querySelector('[data-action="preview"]');
      if(previewButton){const b=document.createElement('button');b.className='is-delete-hover';b.textContent='×';b.title='Xóa ảnh';b.setAttribute('aria-label','Xóa ảnh');b.dataset.action='delete';b.dataset.id=job.id;b.dataset.index=previewButton.dataset.index;card.append(b);}
      else if(job.status!=='running'){const b=document.createElement('button');b.type='button';b.className='is-delete-hover';b.textContent='×';b.title='Xóa tác vụ';b.setAttribute('aria-label','Xóa tác vụ');b.dataset.action='delete-job';b.dataset.id=job.id;card.append(b);}
      if(!job?.account&&!job?.requested_account)return;
      const button=document.createElement('button');button.type='button';button.className='btn';
      button.dataset.action='open';button.dataset.id=job.id;button.textContent='Open Dola';
      button.title='Mở hội thoại bằng tài khoản của tác vụ trên máy chạy server';
      card.querySelector('.is-card-actions').append(button);
    });
    el('isPage').textContent=state.total?`${state.offset+1}–${Math.min(state.offset+30,state.total)} / ${state.total} tác vụ`:'';
    el('isPrev').disabled=state.offset===0;el('isNext').disabled=state.offset+30>=state.total;
    trimBlobs();thumbnails();
  }
  async function refresh(force=false){
    if(state.loading)return;state.loading=true;
    try{
      if(!state.options||force)await options();
      const result=await api(`/api/admin/image-studio/jobs?offset=${state.offset}&limit=30`);
      state.jobs=result.jobs;state.total=result.total;
      const c=result.counts;el('isSummary').textContent=`${c.completed||0} tác vụ hoàn tất · ${(c.running||0)+(c.queued||0)} đang tạo`;
      const signature=JSON.stringify([result,state.filter]);
      if(force||signature!==state.signature){state.signature=signature;render();}
      error('isLoadError','');
    }catch(e){error('isLoadError',e.message);throw e;}finally{state.loading=false;}
  }
  async function submit(event){
    event.preventDefault();if(state.busy||state.uploading)return;
    const prompt=el('isPrompt').value.trim();if(!prompt){el('isPrompt').focus();return;}
    state.busy=true;el('isGenerate').disabled=true;error('isFormError','');saveDraft();
    // Retain the same key on ambiguous network failures to prevent double generation.
    state.requestId ||= (crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`);
    const batchCount=Number(el('isBatchCount').value);
    try{await api('/api/admin/image-studio/'+(batchCount>1?'batches':'jobs'),{method:'POST',body:{...(batchCount>1?{count:batchCount}:{}),prompt,model:state.model,ratio:state.ratio,style:el('isStyle').value,account:el('isAccount').value,references:state.references.map(r=>r.id),request_id:state.requestId}});state.requestId=null;state.offset=0;state.filter='all';root.querySelectorAll('[data-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.filter==='all')));toast(`Đã thêm ${batchCount} lượt tạo ảnh vào hàng đợi.`);await refresh(true);}catch(e){error('isFormError',e.message);}finally{state.busy=false;el('isGenerate').disabled=!state.options?.accounts.length;}
  }
  async function preview(job,index){
    const output=job.outputs[index];state.preview=output;
    dialog.innerHTML=`<div class="is-dialog-head"><strong>Ảnh gốc</strong><button type="button" class="btn" data-close aria-label="Đóng xem trước">✕</button></div><div class="is-dialog-body"><div class="is-dialog-image"><p>Đang tải ảnh…</p></div><div class="is-dialog-info"><h3>MÔ TẢ</h3><p>${esc(job.prompt)}</p><div class="is-card-spec"><span>${esc(job.model)}</span><span>${output.width} × ${output.height}</span></div><div class="is-card-actions"><button class="btn btn-p" data-action="download" data-id="${job.id}" data-index="${index}">↓ Tải ảnh gốc</button><button class="btn" data-action="library" data-id="${job.id}" data-index="${index}">+ Thư viện</button></div></div></div>`;
    dialog.showModal();const src=await blob(output.url);if(dialog.open&&state.preview===output){const img=document.createElement('img');img.src=src;img.alt=job.prompt;dialog.querySelector('.is-dialog-image').replaceChildren(img);}
  }
  async function action(event){
    const button=event.target.closest('[data-action]');if(!button)return;
    const job=state.jobs.find(j=>j.id===button.dataset.id);if(!job)return;
    const index=Number(button.dataset.index||0), kind=button.dataset.action;button.disabled=true;
    try{
      if(kind==='retry'||kind==='switch'){
        const data=await api('/api/admin/image-studio/options');
        const current=job.account||job.requested_account;
        const choices=kind==='switch'?data.accounts.filter(a=>a.name!==current):data.accounts.filter(a=>a.name===current);
        if(!choices.length)throw new Error(kind==='switch'?'Chưa có tài khoản khác sẵn sàng.':'Tài khoản cũ chưa sẵn sàng. Hãy chọn Đổi acc + Retry.');
        dialog.innerHTML=`<div class="is-dialog-head"><strong>${kind==='switch'?'Đổi tài khoản + Retry':'Retry tạo ảnh'}</strong><button class="btn" data-close>×</button></div><form id="isRetryForm" style="padding:24px"><p style="margin-bottom:16px">Chạy lại trên bản ghi này, thay kết quả cũ và giữ prompt, model, tỷ lệ, phong cách cùng ảnh tham chiếu. Lượt tạo mới có thể dùng thêm credit Dola.</p>${job.status==='interrupted'?'<p class="is-error">Lượt gửi cũ chưa rõ kết quả. Kiểm tra Open Dola trước khi tạo lại để tránh trùng ảnh.</p>':''}<label for="isRetryAccount">Tài khoản</label><select id="isRetryAccount" style="display:block;width:100%;padding:12px;margin:12px 0">${choices.map(a=>`<option value="${esc(a.name)}">${esc(a.label)}${a.busy?' · Đang bận (sẽ chờ)':''}</option>`).join('')}</select><p id="isRetryError" class="is-error" hidden></p><button class="btn btn-p" type="submit">Xác nhận Retry</button></form>`;
        dialog.showModal();
        const requestId=crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`;
        el('isRetryForm').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('[type=submit]');b.disabled=true;try{await api(`/api/admin/image-studio/jobs/${job.id}/retry`,{method:'POST',body:{account:el('isRetryAccount').value,request_id:requestId}});dialog.close();state.offset=0;toast('Đã đưa tác vụ này về hàng đợi, giữ nguyên ảnh tham chiếu.');await refresh(true);}catch(err){error('isRetryError',err.message);}finally{b.disabled=false;}};
      }
      if(kind==='delete'&&confirm('Xóa ảnh này? Bản đã lưu riêng trong thư viện tham chiếu vẫn được giữ.')){await api(job.outputs[index].url,{method:'DELETE'});const cached=state.blobs.get(job.outputs[index].url);if(cached)cached.then(URL.revokeObjectURL).catch(()=>{});state.blobs.delete(job.outputs[index].url);await refresh(true);}
      if(kind==='delete-job'&&confirm('Xóa tác vụ này khỏi lịch sử? Nếu đang chờ, tác vụ sẽ bị hủy.')){await api(`/api/admin/image-studio/jobs/${job.id}`,{method:'DELETE'});await refresh(true);}
      if(kind==='preview')await preview(job,index);
      if(kind==='open'){await api(`/api/admin/image-studio/jobs/${job.id}/open`,{method:'POST'});toast('Đã yêu cầu mở hội thoại Dola trên máy chạy server.');}
      if(kind==='download'){const output=job.outputs[index];const link=document.createElement('a');link.href=await blob(output.url);link.download=`dola-${job.id.slice(0,8)}-${index+1}.${output.filename.split('.').pop()}`;document.body.append(link);link.click();link.remove();}
      if(kind==='library'){await api(`/api/admin/image-studio/jobs/${job.id}/library/${index}`,{method:'POST'});toast('Đã lưu vào thư viện ảnh tham chiếu.');}
      if(kind==='reuse'){el('isPrompt').value=job.prompt;state.references=[];renderReferences();if(job.references?.length)toast('Hãy dán lại ảnh tham chiếu để dùng cùng prompt này.');state.model=job.model;state.ratio=job.ratio;el('isStyle').value=job.style;state.requestId=null;count();selections();saveDraft();el('isPrompt').focus();el('isForm').scrollIntoView({behavior:'smooth',block:'center'});}
      if(kind==='cancel'||kind==='resume'){await api(`/api/admin/image-studio/jobs/${job.id}/${kind}`,{method:'POST'});await refresh(true);}
    }catch(e){toast(e.message);}finally{button.disabled=false;}
  }
  root.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.dataset.model){state.model=button.dataset.model;state.requestId=null;selections();saveDraft();}
    if(button.dataset.ratio){state.ratio=button.dataset.ratio;state.requestId=null;selections();saveDraft();}
    if(button.dataset.filter){state.filter=button.dataset.filter;root.querySelectorAll('[data-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));render();}
    if(button.dataset.preset){const prompts={landscape:'Một ngôi nhà gỗ bên hồ yên tĩnh, rừng thông trong sương sớm, ánh sáng bình minh vàng nhạt, góc máy rộng, ảnh chân thực giàu chi tiết.',product:'Một chai nước hoa thủy tinh trên khối đá trắng, ánh sáng studio mềm, bóng đổ tinh tế, nền xanh sage, bố cục tối giản cao cấp.',illustration:'Một tiệm sách nhỏ giữa khu vườn đầy hoa, chú mèo nằm bên cửa sổ, phong cách minh họa ấm áp, màu sắc nhẹ nhàng, nhiều chi tiết thú vị.'};el('isPrompt').value=prompts[button.dataset.preset];state.requestId=null;count();saveDraft();el('isPrompt').focus();}
    action(event);
  });
  dialog.addEventListener('click',event=>{if(event.target===dialog||event.target.closest('[data-close]'))dialog.close();else action(event);});
  dialog.addEventListener('close',()=>{state.preview=null;trimBlobs();});
  el('isForm').addEventListener('submit',submit);
  el('isPasteArea').onclick=()=>el('isReferenceFiles').click();
  el('isReferenceFiles').onchange=event=>{addReferences(event.target.files);event.target.value='';};
  el('isReferences').addEventListener('click',event=>{const button=event.target.closest('[data-remove-reference]');if(!button||state.busy||state.uploading)return;state.references.splice(Number(button.dataset.removeReference),1);state.requestId=null;saveDraft();renderReferences();trimBlobs();});
  document.addEventListener('paste',event=>{
    if(curTab!=='images'||dialog.open||q('#loginMask').style.display!=='none')return;
    const files=[...(event.clipboardData?.items||[])].filter(item=>item.kind==='file'&&item.type.startsWith('image/')).map(item=>item.getAsFile()).filter(Boolean);
    if(!files.length)return; // Preserve normal text paste.
    event.preventDefault();addReferences(files);
  });
  el('isForm').addEventListener('input',()=>{state.requestId=null;count();saveDraft();});
  el('isForm').addEventListener('change',()=>{state.requestId=null;saveDraft();});
  el('isRefresh').onclick=()=>refresh(true).catch(()=>{});
  el('isPrev').onclick=()=>{state.offset=Math.max(0,state.offset-30);refresh(true).catch(()=>{});};
  el('isNext').onclick=()=>{state.offset+=30;refresh(true).catch(()=>{});};
  return {refresh};
})();
