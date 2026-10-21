/* Workflow UI uses authenticated fetches; all generated/model text is textContent. */
(() => {
  'use strict';
  const base = '/api/admin/film-studio';
  const q = id => document.getElementById(id);
  let current = null, currentId = null, createId = crypto.randomUUID();
  let imageUrls = [];
  const el = (tag, text) => { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; return e; };
  const error = exc => { q('flow-status').textContent = exc.message; };
  async function api(path, method = 'GET', body) {
    const headers = {'X-Admin-Key': localStorage.getItem('dola_admin_key') || ''};
    if (!(body instanceof FormData)) headers['Content-Type'] = 'application/json';
    const response = await fetch(base + path, {method, headers,
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail));
    return value;
  }
  function button(text, action) {
    const b = el('button', text); b.type = 'button'; b.style.margin = '4px';
    b.onclick = async () => { b.disabled = true; try { await action(); } catch (e) { error(e); } finally { b.disabled = false; } };
    return b;
  }
  function jsonBlock(value) {
    const pre = el('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2));
    pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto'; return pre;
  }
  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json'}));
    const a = el('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function list() {
    const runs = await api('/runs'); q('runs').replaceChildren();
    for (const run of runs) q('runs').append(button(`${run.title} — ${run.status}${run.paused ? ' (tạm dừng)' : ''}`, () => open(run.id)));
    const status = await api('/status');
    q('runtime-status').textContent = JSON.stringify(status, null, 2);
    q('browser-status').textContent = status.chatgpt_transport + (status.chatgpt_error ? ': ' + status.chatgpt_error : '');
  }
  q('browser-connect').onclick = async () => {
    q('browser-connect').disabled = true;
    try { await api('/browser/connect', 'POST', {endpoint:q('browser-endpoint').value}); await list(); }
    catch (e) { q('browser-status').textContent = e.message; }
    finally { q('browser-connect').disabled = false; }
  };
  q('browser-profile-connect').onclick = async () => {
    q('browser-profile-connect').disabled = true;
    try { await api('/browser/connect-profile', 'POST'); await list(); }
    catch (e) { q('browser-status').textContent = e.message; }
    finally { q('browser-profile-connect').disabled = false; }
  };
  q('browser-disconnect').onclick = async () => {
    try { await api('/browser/disconnect', 'POST'); await list(); }
    catch (e) { q('browser-status').textContent = e.message; }
  };
  async function open(id) {
    currentId = id; current = await api('/runs/' + id);
    for (const url of imageUrls) URL.revokeObjectURL(url); imageUrls = [];
    const box = q('run-detail'); box.hidden = false; box.replaceChildren();
    box.append(el('h2', current.title), el('p', `${current.status} · phiên bản ${current.revision}`));
    const snap = current;
    box.append(button(current.paused ? 'Tiếp tục' : 'Tạm dừng', async () => {
      await api(`/runs/${id}/control/${snap.paused ? 'resume' : 'pause'}`, 'POST'); await open(id);
    }), button('Hủy workflow', async () => {
      if (!confirm('Hủy workflow này? Lịch sử vẫn giữ lại.')) return;
      await api(`/runs/${id}/control/cancel`, 'POST'); await open(id); await list();
    }), button('Xuất hồ sơ dự án', () => download(id + '.json', snap)));
    const artifact = aid => snap.artifacts.find(a => a.id === aid);
    const script = el('details'); script.append(el('summary', 'Kịch bản hiện tại'), jsonBlock(artifact(snap.screenplay)?.content)); box.append(script);
    const gate = snap.status === 'awaiting_screenplay_approval' ? 'screenplay' : ['awaiting_reference_approval', 'reference_revision_limit'].includes(snap.status) ? 'references' : null;
    if (gate) {
      const approval = el('div');
      approval.append(el('h3', gate === 'screenplay' ? 'Duyệt kịch bản' : 'Duyệt toàn bộ ảnh'));
      const review = gate === 'references' ? artifact(snap.reference_qa) : [...snap.artifacts].reverse().find(a => a.kind === 'script_review');
      approval.append(jsonBlock(review?.content));
      if (snap.status !== 'reference_revision_limit') approval.append(button('Duyệt phiên bản này', async () => {
        await api(`/runs/${id}/approve`, 'POST', {gate, artifact_id: gate === 'screenplay' ? snap.screenplay : snap.reference_qa, revision: snap.revision}); await open(id);
      }));
      const notes = el('textarea'); notes.rows = 3; notes.placeholder = 'Những điểm cần sửa'; notes.setAttribute('aria-label', 'Yêu cầu sửa');
      approval.append(notes, button('Yêu cầu sửa', async () => {
        await api(`/runs/${id}/changes`, 'POST', {gate, notes: notes.value, revision: snap.revision}); await open(id);
      })); box.append(approval);
    }
    if (Object.keys(snap.images).length) {
      box.append(el('h3', 'Bộ ảnh hiện tại'));
      for (const [ref, aid] of Object.entries(snap.images)) {
        const imageId = artifact(aid).content.image_id;
        const figure = el('figure'); figure.append(el('figcaption', ref)); box.append(figure);
        try {
          const response = await fetch(`${base}/runs/${id}/images/${imageId}`, {headers: {'X-Admin-Key': localStorage.getItem('dola_admin_key') || ''}});
          if (!response.ok) throw new Error('Không tải được ảnh');
          const url = URL.createObjectURL(await response.blob()); imageUrls.push(url);
          const img = el('img'); img.src = url; img.alt = ref; img.style.maxWidth = '240px'; figure.append(img);
        } catch (e) { figure.append(el('p', e.message)); }
      }
    }
    if (snap.status === 'reference_inspection_required') box.append(button('Kiểm tra lại bộ ảnh (không tạo lại)', async () => {
      await api(`/runs/${id}/inspect`, 'POST'); await open(id);
    }));
    box.append(el('h3', 'Video / batch'));
    for (const clip of [...snap.clips].sort((a,b) => b.number - a.number)) {
      const row = el('details'); row.append(el('summary', `${clip.batch_name} — ${clip.status} — ${clip.attempts.filter(a => a.generated).length}/10 video`));
      row.append(jsonBlock({batch_id: clip.batch_id, selected_task_id: clip.selected_task_id, contract: clip.contract, attempts: clip.attempts}));
      if (clip.selected_task_id) {
        const selected = clip.attempts.find(a => a.task_id === clip.selected_task_id);
        if (selected?.video_url) {
          const url = new URL(selected.video_url, location.origin);
          if (['http:', 'https:'].includes(url.protocol)) { const video = el('video'); video.controls = true; video.preload = 'none'; video.src = url.href; video.style.maxWidth = '100%'; row.append(video); }
        }
      }
      if (clip.status === 'inspection_required') row.append(button('QA lại take này', async () => {
        await api(`/runs/${id}/inspect?clip_number=${clip.number}`, 'POST'); await open(id);
      }));
      if (clip.status === 'admission_blocked') row.append(button('Thử gửi lại sau khi xử lý lỗi đầu vào', async () => {
        await api(`/runs/${id}/clips/${clip.number}/retry-admission`, 'POST'); await open(id);
      }));
      box.append(row);
    }
    box.append(el('h3', 'Nhiệm vụ agent'), el('p', 'ChatGPT worker chưa kết nối. Có thể xuất đầu vào và nhập kết quả thực tế để kiểm thử workflow; ứng dụng không tự gửi chat ở bước này.'));
    for (const task of snap.tasks) {
      const row = el('details'); row.append(el('summary', `${task.kind}${task.clip ? ' · video '+task.clip : ''}${task.ref ? ' · '+task.ref : ''} — ${task.status}`));
      row.append(button('Xuất input + schema', async () => download(task.id + '.json', await api(`/runs/${id}/tasks/${task.id}/input`))));
      if (task.output) row.append(jsonBlock(artifact(task.output)?.content));
      if (task.status === 'pending') {
        const result = el('textarea'); result.rows = 6; result.style.width = '100%'; result.placeholder = 'JSON output từ agent, đúng schema đã xuất'; result.setAttribute('aria-label', `Kết quả ${task.kind}`);
        if (task.kind === 'image_create') {
          const file = el('input'); file.type = 'file'; file.accept = 'image/png,image/jpeg,image/webp'; file.setAttribute('aria-label', 'Ảnh tạo bằng ChatGPT');
          row.append(file, button('Tải ảnh ChatGPT đã tạo', async () => {
            if (!file.files[0]) throw new Error('Chọn ảnh thực tế.');
            const form = new FormData(); form.append('file', file.files[0]);
            const image = await api(`/runs/${id}/tasks/${task.id}/image`, 'POST', form);
            result.value = JSON.stringify(image, null, 2);
          }));
        }
        row.append(result, button('Kiểm tra và nhận kết quả', async () => {
          await api(`/runs/${id}/tasks/${task.id}/result`, 'POST', {output: JSON.parse(result.value)}); await open(id); await list();
        }));
      }
      box.append(row);
    }
    const events = el('details'); events.append(el('summary', 'Nhật ký'), jsonBlock(snap.events)); box.append(events);
  }
  q('new-film').onsubmit = async event => {
    event.preventDefault(); const submit = event.target.querySelector('button'); submit.disabled = true;
    try {
      const run = await api('/runs', 'POST', {title: q('film-title').value, screenplay: q('screenplay').value, request_id: createId});
      createId = crypto.randomUUID(); await list(); await open(run.id); q('flow-status').textContent = 'Đã lưu kịch bản. Chờ kết quả agent phản biện.';
    } catch (e) { error(e); } finally { submit.disabled = false; }
  };
  q('skill-form').onsubmit = async event => {
    event.preventDefault(); const submit = event.target.querySelector('button'); submit.disabled = true;
    try { await api('/skills/' + q('skill-name').value, 'PUT', {version: q('skill-version').value, source: q('skill-source').value}); await list(); }
    catch (e) { error(e); } finally { submit.disabled = false; }
  };
  q('refresh-runs').onclick = async () => { try { await list(); if (currentId) await open(currentId); } catch (e) { error(e); } };
  list().catch(error);
})();
