'use strict';
const statusNode = document.getElementById('status');
const inputs = {};
async function request(method = 'GET', body) {
  const key = localStorage.getItem('dola_admin_key') || '';
  const response = await fetch('/api/admin/film-studio', {method,
    headers: {'X-Admin-Key': key, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 401 || response.status === 403
    ? 'Hãy đăng nhập Dashboard bằng admin key rồi mở lại Film Studio.'
    : typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}
function updateLink(input, link) {
  try {
    const url = new URL(input.value);
    if (url.protocol !== 'https:' || url.host !== 'chatgpt.com' || url.username || url.password) throw new Error();
    link.href = url.href; link.hidden = false;
  } catch { link.hidden = true; link.removeAttribute('href'); }
}
async function load() {
  try {
    const data = await request();
    const profile = document.getElementById('profile');
    const browser = document.getElementById('browser');
    const account = document.getElementById('account');
    for (const item of (data.accounts || [])) {
      const option = document.createElement('option'); option.value = item.id;
      option.textContent = `${item.email} — ${item.auth_state}`; account.append(option);
    }
    account.value = data.settings.account_id || '';
    browser.value = data.settings.browser || 'chrome';
    const profileOptions = () => browser.value === 'edge' ? (data.edge_profiles || []) : data.profiles;
    const renderProfiles = () => {
      profile.replaceChildren();
      const empty = document.createElement('option'); empty.value = ''; empty.textContent = 'Chưa chọn'; profile.append(empty);
      for (const item of profileOptions()) {
      const option = document.createElement('option'); option.value = item.id;
      option.textContent = `${item.name} — ${item.email || item.id}`; profile.append(option);
      }
    };
    renderProfiles();
    browser.addEventListener('change', () => { renderProfiles(); profile.value = ''; });
    const selected = data.settings.profile_id;
    if (selected && !profileOptions().some(p => p.id === selected)) {
      const option = document.createElement('option'); option.value = selected;
      option.textContent = `${selected} — không còn tìm thấy`; profile.append(option);
    }
    profile.value = selected;
    document.getElementById('project').value = data.settings.project_url;
    document.getElementById('confirmed').checked = data.settings.instructions_confirmed;
    for (const [role, name] of Object.entries(data.roles)) {
      const row = document.createElement('div'); row.className = 'agent';
      const label = document.createElement('label'); label.textContent = name; label.htmlFor = `agent-${role}`;
      const input = document.createElement('input'); input.type = 'url'; input.id = label.htmlFor;
      input.placeholder = 'https://chatgpt.com/c/…'; input.value = data.settings.sessions[role] || '';
      const link = document.createElement('a'); link.textContent = 'Mở chat'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      input.addEventListener('input', () => updateLink(input, link)); updateLink(input, link);
      row.append(label, input, link); document.getElementById('agents').append(row); inputs[role] = input;
    }
    const project = document.getElementById('project'), link = document.getElementById('open-project');
    project.addEventListener('input', () => updateLink(project, link)); updateLink(project, link);
    document.getElementById('fields').disabled = false;
    statusNode.textContent = data.profiles.length ? 'Đã tải thiết lập.' : 'Chưa tìm thấy Chrome profile trên máy chạy server.';
  } catch (error) { statusNode.textContent = error.message; }
}
document.getElementById('setup').addEventListener('submit', async event => {
  event.preventDefault(); const fields = document.getElementById('fields'); fields.disabled = true;
  try {
    await request('PUT', {browser: document.getElementById('browser').value,
      account_id: document.getElementById('account').value,
      profile_id: document.getElementById('profile').value,
      project_url: document.getElementById('project').value,
      instructions_confirmed: document.getElementById('confirmed').checked,
      sessions: Object.fromEntries(Object.entries(inputs).map(([role, input]) => [role, input.value]))});
    statusNode.textContent = 'Đã lưu. Worker tự động chưa được kết nối.';
  } catch (error) { statusNode.textContent = error.message; }
  finally { fields.disabled = false; }
});
load();
