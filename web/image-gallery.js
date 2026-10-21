const ImageGallery=(()=>{
  const root=document.getElementById('tab-gallery');
  let images=[],total=0,offset=0,loading=false,deleting=false,signature='';
  const selected=new Set(),urls=new Map();
  const key=i=>`${i.job_id}/${i.index}`;
  root.innerHTML=`<div class="is-head"><div><div class="is-eyebrow">Dola · Gallery</div><h1>Kho ảnh của bạn</h1><p>Chỉ những hình ảnh đã tạo. Xem, tải xuống và sắp xếp lại không gian của bạn.</p></div><button class="btn" id="igRefresh">↻ Làm mới</button></div><div class="ig-toolbar"><label><input type="checkbox" id="igAll"> Chọn cả trang</label><span id="igCount" role="status"></span><button class="btn btn-danger" id="igDelete" disabled>Xóa đã chọn</button></div><p class="is-error" id="igError" role="alert" hidden></p><div id="igGrid" class="ig-grid"></div><div id="igEmpty" class="is-empty" hidden><h3>Chưa có ảnh nào</h3><p>Ảnh hoàn tất trong tab Tạo ảnh sẽ tự động xuất hiện ở đây.</p></div><div class="is-pagination"><button class="btn" id="igPrev">← Trước</button><span id="igPage"></span><button class="btn" id="igNext">Sau →</button></div>`;
  const el=id=>document.getElementById(id);
  const viewer=document.createElement('dialog');viewer.className='is-dialog';viewer.setAttribute('aria-label','Xem ảnh trong kho');document.body.append(viewer);
  function error(message=''){el('igError').textContent=message;el('igError').hidden=!message;}
  function selection(){el('igCount').textContent=selected.size?`Đã chọn ${selected.size} ảnh`:`${total} ảnh`;el('igDelete').disabled=deleting||!selected.size;el('igAll').checked=!!images.length&&images.every(i=>selected.has(key(i)));el('igAll').indeterminate=selected.size>0&&!el('igAll').checked;}
  async function imageURL(item){const k=key(item);if(urls.has(k))return urls.get(k);const pending=(async()=>{const response=await fetch(item.url,{headers:{'X-Admin-Key':adminKey}});if(!response.ok){if(response.status===401)showLogin();throw new Error('Không tải được ảnh. Hãy làm mới.');}return URL.createObjectURL(await response.blob());})();urls.set(k,pending);try{return await pending;}catch(e){urls.delete(k);throw e;}}
  function cleanup(){const keep=new Set(images.map(key));for(const [k,p] of urls)if(!keep.has(k)){p.then(URL.revokeObjectURL).catch(()=>{});urls.delete(k);}}
  function render(){
    for(const k of selected)if(!images.some(i=>key(i)===k))selected.delete(k);
    el('igGrid').innerHTML=images.map((i,n)=>`<article class="ig-item"><label class="ig-select"><input type="checkbox" data-select="${n}" aria-label="Chọn ảnh ${offset+n+1}" ${selected.has(key(i))?'checked':''}></label><button class="ig-image" data-view="${n}" aria-label="Xem ảnh ${offset+n+1}"><img alt="${esc(i.prompt.slice(0,160))}" loading="lazy"></button><div class="ig-actions"><span>${i.width} × ${i.height}</span><button class="btn" data-download="${n}" aria-label="Tải ảnh ${offset+n+1}">↓ Tải</button><button class="btn btn-danger" data-delete="${n}" aria-label="Xóa ảnh ${offset+n+1}">Xóa</button></div></article>`).join('');
    el('igEmpty').hidden=images.length>0;el('igPrev').disabled=offset===0;el('igNext').disabled=offset+48>=total;el('igPage').textContent=total?`${offset+1}–${offset+images.length} / ${total} ảnh`:'';
    el('igGrid').querySelectorAll('.ig-actions').forEach((actions,n)=>{
      const close=document.createElement('button');close.className='is-delete-hover';close.textContent='×';close.dataset.delete=String(n);close.title='Xóa ảnh';close.setAttribute('aria-label','Xóa ảnh');actions.parentElement.append(close);
      const button=document.createElement('button');button.className='btn';button.textContent='Open Dola';button.dataset.open=String(n);
      button.title='Mở hội thoại bằng tài khoản tạo ảnh trên máy chạy server';actions.append(button);
    });
    selection();cleanup();
    images.forEach(async(i,n)=>{const img=el('igGrid').children[n]?.querySelector('img');try{const url=await imageURL(i);if(img?.isConnected)img.src=url;}catch(e){if(img?.isConnected)img.alt=e.message;}});
  }
  async function refresh(force=false){if(loading||deleting)return;loading=true;try{let data=await api(`/api/admin/image-studio/gallery?offset=${offset}&limit=48`);if(offset&&offset>=data.total){offset=Math.max(0,Math.ceil(data.total/48)-1)*48;data=await api(`/api/admin/image-studio/gallery?offset=${offset}&limit=48`);}images=data.images;total=data.total;const next=JSON.stringify(data);if(force||next!==signature){signature=next;render();}error();}catch(e){error(e.message);}finally{loading=false;}}
  async function remove(items){
    if(deleting||!items.length)return;
    if(!confirm(`Xóa vĩnh viễn ${items.length} ảnh khỏi Kho ảnh và kết quả tạo ảnh? Bản đã lưu riêng vào thư viện tham chiếu vẫn được giữ.`))return;
    deleting=true;selection();let removed=0,failure='';
    try{for(const item of items){await api(item.url,{method:'DELETE'});selected.delete(key(item));const p=urls.get(key(item));if(p)p.then(URL.revokeObjectURL).catch(()=>{});urls.delete(key(item));removed++;}}catch(e){failure=`Đã xóa ${removed}/${items.length} ảnh. ${e.message}`;}finally{deleting=false;viewer.close();await refresh(true);if(failure)error(failure);}
  }
  root.addEventListener('change',e=>{if(e.target.dataset.select!==undefined){const i=images[Number(e.target.dataset.select)];if(e.target.checked)selected.add(key(i));else selected.delete(key(i));selection();}});
  root.addEventListener('click',async e=>{
    const b=e.target.closest('button');if(!b)return;
    try{
      if(b.dataset.open!==undefined){b.disabled=true;try{await api(`/api/admin/image-studio/jobs/${images[Number(b.dataset.open)].job_id}/open`,{method:'POST'});b.textContent='Đang mở…';}finally{setTimeout(()=>{b.disabled=false;b.textContent='Open Dola';},2500);}}
      if(b.dataset.delete!==undefined)await remove([images[Number(b.dataset.delete)]]);
      if(b.dataset.download!==undefined){const i=images[Number(b.dataset.download)],a=document.createElement('a');a.href=await imageURL(i);a.download=`dola-${i.job_id.slice(0,8)}-${i.filename}`;document.body.append(a);a.click();a.remove();}
      if(b.dataset.view!==undefined){const i=images[Number(b.dataset.view)];viewer.innerHTML=`<div class="is-dialog-head"><strong>${i.width} × ${i.height}</strong><button class="btn" data-close aria-label="Đóng ảnh">✕</button></div><div class="ig-full"><img alt="${esc(i.prompt)}"></div>`;viewer.showModal();const img=viewer.querySelector('img');img.src=await imageURL(i);}
    }catch(err){error(err.message);}
  });
  viewer.addEventListener('click',e=>{if(e.target===viewer||e.target.closest('[data-close]'))viewer.close();});
  el('igAll').onchange=e=>{images.forEach(i=>e.target.checked?selected.add(key(i)):selected.delete(key(i)));render();};
  el('igDelete').onclick=()=>remove(images.filter(i=>selected.has(key(i))));
  el('igRefresh').onclick=()=>refresh(true);
  el('igPrev').onclick=()=>{offset=Math.max(0,offset-48);selected.clear();refresh(true);};
  el('igNext').onclick=()=>{offset+=48;selected.clear();refresh(true);};
  return {refresh};
})();
