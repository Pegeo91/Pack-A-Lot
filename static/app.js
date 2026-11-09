'use strict';

// ============================================================================ helpers
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));

const api = {
  async req(method, url, body) {
    const opt = { method, headers: {} };
    if (body instanceof FormData) opt.body = body;
    else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(url, opt);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
    return data;
  },
  get: u => api.req('GET', u),
  post: (u, b) => api.req('POST', u, b ?? {}),
  put: (u, b) => api.req('PUT', u, b),
  del: u => api.req('DELETE', u),
};

let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), bad ? 7000 : 3000);
}

function getPath(o, p) { return p.split('.').reduce((a, k) => (a == null ? undefined : a[k]), o); }
function setPath(o, p, v) {
  const ks = p.split('.'); const last = ks.pop();
  const t = ks.reduce((a, k) => (a[k] ??= {}), o);
  t[last] = v;
}

// ============================================================================ state
const STEPS = ['Source', 'App details', 'PSADT script', 'Intune settings', 'Build', 'Test', 'Upload'];
let job = null;
let step = 1;
let saveTimer = null;
let busy = false;
let settings = {};
let lastEditor = null;

// ============================================================================ data binding
function bindAll(root = document) {
  if (!job) return;
  $$('[data-bind]', root).forEach(el => {
    const v = getPath(job, el.dataset.bind);
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.type === 'radio') el.checked = el.value === v;
    else if (el.tagName === 'SELECT') { ensureOption(el, v); el.value = v ?? ''; }
    else el.value = v ?? '';
  });
  updateShows();
}
function ensureOption(sel, v) {
  if (v !== undefined && v !== null && v !== '' && ![...sel.options].some(o => o.value === String(v))) sel.add(new Option(v, v));
}
function readEl(el) {
  if (el.type === 'checkbox') return el.checked;
  if (el.type === 'number') return el.value === '' ? 0 : Number(el.value);
  return el.value;
}
function updateShows() {
  $$('[data-show]').forEach(el => el.classList.toggle('hidden', !getPath(job, el.dataset.show)));
}

function onBound(e) {
  const el = e.target;
  if (!job || !el.dataset || !el.dataset.bind) return;
  if (el.type === 'radio' && !el.checked) return;
  // selects / checkboxes fire both input+change; handle text on input, others on change
  if (e.type === 'input' && (el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio')) return;
  setPath(job, el.dataset.bind, readEl(el));
  updateShows();
  scheduleSave();
  fieldChanged(el.dataset.bind, e.type);
}
document.addEventListener('input', onBound);
document.addEventListener('change', onBound);

function fieldChanged(path, evType) {
  if (path === 'source.type') renderSourcePanels();
  if (path === 'source.winget_id' && evType === 'change') loadVersions();
  if (/^source\.(winget_id|version|architecture|scope|installer_type|locale)$/.test(path) && evType === 'change') schedulePreview();
  if (path === 'source.type') schedulePreview();
  if (path.startsWith('commands.install') || path.startsWith('commands.uninstall')) refreshCommandPreview();
  if (/^detection\.rules\.\d+\.(type|detection_type|version_operator)$/.test(path)) renderRules();
  else if (/^detection\.rules\.\d+\.(version|product_code)$/.test(path) && evType === 'change') renderRules();
  if (path.startsWith('app.')) updateTitle();
}

function scheduleSave() {
  $('#saveState').textContent = 'Unsaved changes…';
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveNow().catch(e => toast('Save failed: ' + e.message, true)), 700);
}
async function saveNow() {
  clearTimeout(saveTimer);
  if (!job) return;
  job.step = step;
  const saved = await api.put(`/api/jobs/${job.id}`, job);
  job.status = saved.status;
  $('#saveState').textContent = 'Saved';
  renderStepper();
}

// ============================================================================ navigation
function show(view) {
  $('#home').classList.toggle('hidden', view !== 'home');
  $('#wizard').classList.toggle('hidden', view !== 'wizard');
}

function maxStep() {
  const s = job.status;
  if (!s.fetched) return 1;
  if (!s.packaged) return 5;
  if (job.test.enabled && !s.tested) return 6;
  return 7;
}

function renderStepper() {
  if (!job) return;
  const max = maxStep();
  const s = job.status;
  const done = {
    1: s.fetched, 2: s.fetched && job.app.name && job.app.version, 3: s.fetched, 4: s.fetched && job.detection.rules.length,
    5: s.packaged, 6: s.tested === true || (!job.test.enabled && s.packaged), 7: s.uploaded,
  };
  $('#stepper').innerHTML = STEPS.map((name, i) => {
    const n = i + 1;
    const cls = [n === step ? 'active' : '', done[n] && n !== step ? 'done' : '', n > max ? 'locked' : ''].join(' ');
    return `<li class="${cls}" data-goto="${n}"><span class="num">${done[n] && n !== step ? '✓' : n}</span><span class="lbl">${name}</span></li>`;
  }).join('');
}

async function goStep(n) {
  if (busy) return;
  if (n > maxStep()) return;
  step = n;
  $$('.step').forEach(s => s.classList.toggle('active', Number(s.dataset.step) === n));
  $('#btnBack').disabled = n === 1;
  $('#btnNext').classList.toggle('hidden', n === STEPS.length);
  $('#btnFinish').classList.toggle('hidden', n !== STEPS.length);
  location.hash = `job=${job.id}&step=${n}`;
  renderStepper();
  window.scrollTo({ top: 0 });
  await RENDER[n]?.();
  if (job.step !== n) scheduleSave();
}

async function next() {
  if (busy) return;
  try {
    const ok = await (BEFORE_NEXT[step]?.() ?? true);
    if (ok) await goStep(Math.min(step + 1, STEPS.length));
  } catch (e) {
    toast(e.message, true);
  }
}

const BEFORE_NEXT = {
  1: async () => {
    const src = job.source;
    if (src.type === 'winget' && !src.winget_id) throw new Error('Select a winget package (or type its ID).');
    if (src.type === 'custom' && !src.custom_path) throw new Error('Choose an installer file.');
    const same = job.status.fetched && JSON.stringify(job.status.fetched_source) === JSON.stringify(src);
    if (same) return true;
    if (!(await checkFiltersBeforeFetch())) return false;
    return await runTask('fetch');
  },
  2: async () => {
    const missing = ['vendor', 'name', 'version'].filter(k => !String(job.app[k] || '').trim());
    if (missing.length) throw new Error('Required: ' + missing.join(', '));
    return true;
  },
  4: async () => {
    const errs = ruleErrors();
    if (errs.length) throw new Error(errs.join('\n'));
    return true;
  },
  5: async () => (job.status.packaged ? true : await runTask('build')),
  6: async () => {
    if (!job.test.enabled) return true;
    if (job.status.tested) return true;
    throw new Error("Run the sandbox test first (or switch off 'Test before upload').");
  },
};

// ============================================================================ tasks
function setBusy(b) {
  busy = b;
  $$('#btnNext, #btnBack, #btnBuild, #btnTest, #btnUpload').forEach(x => (x.disabled = b || (x.id === 'btnBack' && step === 1)));
  $('#saveState').textContent = b ? 'Working…' : '';
}

async function runTask(name, body) {
  await saveNow();
  const logEl = $(`#log-${name}`);
  logEl.classList.remove('hidden');
  logEl.innerHTML = '';
  setBusy(true);
  try {
    await api.post(`/api/jobs/${job.id}/tasks/${name}`, body);
  } catch (e) {
    setBusy(false);
    toast(e.message, true);
    return false;
  }
  return followTask(name);
}

async function followTask(name) {
  const logEl = $(`#log-${name}`);
  logEl.classList.remove('hidden');
  setBusy(true);
  let offset = 0;
  const jobId = job.id;
  try {
    for (;;) {
      if (job?.id !== jobId) return false;  // user went Home / opened another package
      const t = await api.get(`/api/jobs/${jobId}/task?offset=${offset}`);
      if (job?.id !== jobId) return false;
      appendLog(logEl, t.log);
      offset = t.offset;
      if (t.state !== 'running') {
        await reloadJob();
        if (t.state === 'failed') toast(t.error || `${name} failed`, true);
        return t.state === 'success';
      }
      await sleep(1000);
    }
  } finally {
    setBusy(false);
  }
}

function appendLog(el, lines) {
  if (!lines.length) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.insertAdjacentHTML('beforeend', lines.map(l => {
    const cls = /ERROR|FAIL|failed/.test(l) ? 'err' : /PASS|SUCCESS|completed|Created /.test(l) ? 'ok' : '';
    return `<div class="${cls}">${esc(l)}</div>`;
  }).join(''));
  if (atBottom) el.scrollTop = el.scrollHeight;
}

async function attachRunningTask() {
  const t = await api.get(`/api/jobs/${job.id}/task?offset=0`);
  if (t.state === 'running') followTask(t.name);
}

async function reloadJob() {
  job = await api.get(`/api/jobs/${job.id}`);
  bindAll();
  renderStepper();
  updateTitle();
  await RENDER[step]?.();
}

// ============================================================================ step renderers
const RENDER = {
  1: () => { renderSourcePanels(); if (job.source.winget_id) loadVersions(); loadInstallerPreview(); },
  2: renderAppDetails,
  3: () => {},
  4: () => { refreshCommandPreview(); renderRules(); renderReturnCodes(); },
  5: renderBuild,
  6: renderTestResult,
  7: renderUpload,
};

// ---------------------------------------------------------------- step 1: source
function renderSourcePanels() {
  $('#srcWinget').classList.toggle('hidden', job.source.type !== 'winget');
  $('#srcCustom').classList.toggle('hidden', job.source.type !== 'custom');
}

async function wingetSearch() {
  const q = $('#wgQuery').value.trim();
  if (!q) return;
  const box = $('#wgResults');
  box.innerHTML = '<p class="muted">Searching…</p>';
  try {
    const rows = await api.get('/api/winget/search?q=' + encodeURIComponent(q));
    if (!rows.length) { box.innerHTML = '<p class="muted">No results.</p>'; return; }
    box.innerHTML = `<table><tr><th>Name</th><th>ID</th><th>Version</th><th>Match</th></tr>${rows.map(r =>
      `<tr data-id="${esc(r.id)}" class="${r.id === job.source.winget_id ? 'selected' : ''}"><td>${esc(r.name)}</td><td>${esc(r.id)}</td><td>${esc(r.version)}</td><td class="muted small">${esc(r.match)}</td></tr>`).join('')}</table>`;
  } catch (e) {
    box.innerHTML = `<p class="banner bad">${esc(e.message)}</p>`;
  }
}

let versionsFor = null;
async function loadVersions() {
  const id = job.source.winget_id;
  if (!id || id === versionsFor) return;
  versionsFor = id;
  const sel = $('#wgVersions');
  sel.innerHTML = '<option value="">Latest</option><option disabled>loading…</option>';
  try {
    const versions = await api.get('/api/winget/versions?id=' + encodeURIComponent(id));
    if (versionsFor !== id) return;
    sel.innerHTML = '<option value="">Latest</option>' + versions.map(v => `<option>${esc(v)}</option>`).join('');
  } catch {
    sel.innerHTML = '<option value="">Latest</option>';
  }
  ensureOption(sel, job.source.version);
  sel.value = job.source.version || '';
}

// ---- installer preview: which file winget would download with the current filters
const FILTER_KEYS = ['architecture', 'scope', 'installer_type', 'locale'];
const previewCache = new Map();
let previewTimer = null;

function previewKey(src = job.source) {
  return JSON.stringify([src.winget_id, src.version || '', ...FILTER_KEYS.map(k => src[k] || '')]);
}
function getInstallerPreview(src = job.source) {
  const key = previewKey(src);
  if (!previewCache.has(key)) {
    const q = new URLSearchParams({ id: src.winget_id, version: src.version || '' });
    FILTER_KEYS.forEach(k => q.set(k, src[k] || ''));
    const p = api.get('/api/winget/installer?' + q);
    p.catch(() => previewCache.delete(key));  // don't cache failures
    previewCache.set(key, p);
  }
  return previewCache.get(key);
}
function activeFilters(src = job.source) {
  const labels = { architecture: 'architecture', scope: 'scope', installer_type: 'installer type', locale: 'locale' };
  return FILTER_KEYS.filter(k => src[k]).map(k => `${labels[k]} ${src[k]}`);
}
function hintHtml(h) {
  if (!h) return '';
  const tag = h.scope ? `<span class="pill ${h.scope === 'user' ? 'warn' : 'ok'}">likely ${h.scope} installer</span> ` : '';
  return `<p class="small muted">${tag}${esc(h.reason)}</p>`;
}
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadInstallerPreview, 400);
}
async function loadInstallerPreview() {
  const box = $('#wgInstaller');
  if (!job || job.source.type !== 'winget' || !(job.source.winget_id || '').trim()) { box.classList.add('hidden'); return; }
  const key = previewKey();
  box.classList.remove('hidden');
  box.innerHTML = '<p class="muted small">Checking which installer winget would download…</p>';
  let r;
  try { r = await getInstallerPreview(); } catch (e) {
    if (previewKey() === key) box.innerHTML = `<p class="muted small">Could not check the installer: ${esc(e.message)}</p>`;
    return;
  }
  if (previewKey() !== key) return;  // filters changed meanwhile
  if (r.matched) {
    box.innerHTML = `<div class="kv-line"><span class="muted">Installer:</span> <b>${esc(r.file_name || '(name set by server)')}</b>
      ${r.installer_type ? `<span class="pill">${esc(r.installer_type)}</span>` : ''}
      ${r.version ? `<span class="muted small">v${esc(r.version)}</span>` : ''}</div>${hintHtml(r.hint)}`;
    return;
  }
  const f = r.fallback;
  box.innerHTML = `<div class="banner warn">No installer of <b>${esc(job.source.winget_id)}</b> matches
      ${esc(activeFilters().join(', ') || 'the selected filters')}.
      ${f?.matched ? `<br>Available without filters: <b>${esc(f.file_name)}</b>
        ${f.installer_type ? `<span class="pill">${esc(f.installer_type)}</span>` : ''}${hintHtml(f.hint)}
        <button class="btn small" id="btnClearFilters">Use this installer instead</button>` : (r.message ? '<br>' + esc(r.message) : '')}
    </div>`;
  $('#btnClearFilters')?.addEventListener('click', clearFilters);
}
function clearFilters() {
  FILTER_KEYS.forEach(k => (job.source[k] = ''));
  bindAll($('#srcWinget'));
  scheduleSave();
  loadInstallerPreview();
}

// Generic yes/no dialog -> Promise<boolean>
function confirmDialog(title, html, yesLabel = 'OK', noLabel = 'Cancel') {
  const dlg = $('#dlgConfirm');
  $('#confirmTitle').textContent = title;
  $('#confirmBody').innerHTML = html;
  $('#confirmYes').textContent = yesLabel;
  $('#confirmNo').textContent = noLabel;
  return new Promise(resolve => {
    const done = v => { dlg.onclose = null; dlg.close(); resolve(v); };
    $('#confirmYes').onclick = () => done(true);
    $('#confirmNo').onclick = () => done(false);
    dlg.onclose = () => resolve(false);  // Esc
    dlg.showModal();
  });
}

// Before downloading: if the filters match nothing, offer to download whatever is available
async function checkFiltersBeforeFetch() {
  const src = job.source;
  if (src.type !== 'winget' || !activeFilters().length) return true;
  let r;
  try { r = await getInstallerPreview(); } catch { return true; }  // can't check - let the download report it
  if (r.matched) return true;
  const appName = $$('#wgResults tr.selected td')[0]?.textContent || src.winget_id;
  const f = r.fallback;
  if (!f?.matched) {
    throw new Error(`No installer of ${appName} is available${r.message ? ': ' + r.message : '.'}`);
  }
  const ok = await confirmDialog('No matching installer',
    `<p>We could not download <b>${esc(appName)}</b> with the selected ${esc(activeFilters().join(', '))}.</p>
     <p>Would you like to download whatever is available?</p>
     <div class="panel small"><span class="muted">Available installer:</span> <b>${esc(f.file_name)}</b>
       ${f.installer_type ? `<span class="pill">${esc(f.installer_type)}</span>` : ''}${hintHtml(f.hint)}
       <p class="small muted">Tip: the installer's file name often tells you its scope – names containing
       <code>User</code>/<code>UserSetup</code>/<code>PerUser</code> usually install into the user profile only, while
       <code>System</code>/<code>Machine</code>/<code>AllUsers</code>/<code>Enterprise</code> or an <code>.msi</code>
       usually install for all users. Intune runs installs as SYSTEM, so a per-user installer may not behave as expected.</p>
     </div>`,
    'Download available installer', 'Cancel');
  if (!ok) return false;
  clearFilters();
  await saveNow();
  return true;
}

async function uploadInstaller(file) {
  const fd = new FormData();
  fd.append('file', file);
  $('#uploadProgress').textContent = `Uploading ${file.name} (${(file.size / 1048576).toFixed(1)} MB)…`;
  try {
    await saveNow();
    const res = await api.req('POST', `/api/jobs/${job.id}/installer-file`, fd);
    job.source.custom_path = res.path;
    bindAll($('#srcCustom'));
    $('#uploadProgress').textContent = 'Uploaded to ' + res.path;
    await saveNow();
  } catch (e) {
    $('#uploadProgress').textContent = '';
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------- step 2: app details
function renderAppDetails() {
  const i = job.installer || {};
  const meta = i.metadata || {};
  const rows = [
    ['Installer', i.file], ['Technology', i.winget_type || i.kind], ['Product code', i.product_code],
    ['Display name (ARP)', i.display_name], ['Silent switch', i.silent_switch], ['File version', meta.FileVersion],
  ].filter(r => r[1]);
  $('#installerSummary').innerHTML = rows.length
    ? `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : '';
  $('#iconPreview').src = `/api/jobs/${job.id}/icon?t=${Date.now()}`;
  updateTitle();
}

// Same rule as uploader.display_name(): don't repeat the vendor when the name already starts with it
function defaultDisplayName() {
  const a = job.app;
  const vendor = (a.vendor || '').trim(); const name = (a.name || '').trim();
  const v = vendor && name.toLowerCase().startsWith(vendor.toLowerCase()) ? '' : vendor;
  return [v, name, a.version].filter(Boolean).join(' ');
}

function updateTitle() {
  if (!job) { $('#jobTitle').textContent = ''; return; }
  const name = defaultDisplayName();
  $('#jobTitle').textContent = name || job.source.winget_id || 'New package';
  const dn = $('#displayName');
  if (dn) dn.placeholder = name;
}

// ---------------------------------------------------------------- step 3: script
const SNIPPETS = {
  'Registry – set value': "Set-ADTRegistryKey -LiteralPath 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Vendor\\App' -Name 'Setting' -Value 1 -Type DWord",
  'Registry – remove key': "Remove-ADTRegistryKey -LiteralPath 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Vendor\\App' -Recurse",
  'Copy file from Files\\': 'Copy-ADTFile -Path "$($adtSession.DirFiles)\\config.xml" -Destination "$env:ProgramFiles\\Vendor\\App"',
  'Remove file': 'Remove-ADTFile -LiteralPath "$env:PUBLIC\\Desktop\\App.lnk"',
  'Remove folder': 'Remove-ADTFolder -LiteralPath "$env:ProgramFiles\\Vendor\\App"',
  'Run EXE': "Start-ADTProcess -FilePath 'setup.exe' -ArgumentList '/S'",
  'Install MSI': "Start-ADTMsiProcess -Action Install -FilePath 'app.msi' -ArgumentList 'ALLUSERS=1'",
  'Install MSI + transform': "Start-ADTMsiProcess -Action Install -FilePath 'app.msi' -Transforms 'app.mst'",
  'Uninstall by name': "Uninstall-ADTApplication -Name 'App Name'",
  'Uninstall EXE by name': "Uninstall-ADTApplication -Name 'App Name' -ApplicationType EXE -ArgumentList '/S'",
  'Create shortcut': 'New-ADTShortcut -LiteralPath "$env:ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\App.lnk" -TargetPath "$env:ProgramFiles\\Vendor\\App\\app.exe"',
  'Stop service': "Stop-ADTServiceAndDependencies -Name 'ServiceName'",
  'Active Setup (per user)': "Set-ADTActiveSetup -StubExePath \"$env:ProgramFiles\\Vendor\\App\\userconfig.cmd\" -Description 'App user config' -Key 'App_UserConfig'",
  'Write log entry': "Write-ADTLogEntry -Message 'Custom step completed'",
};

function insertSnippet(name) {
  const code = SNIPPETS[name];
  const ta = lastEditor || $('textarea.code[data-bind="script.sections.post_install"]');
  if (!code || !ta) return;
  const { selectionStart: s, selectionEnd: e, value } = ta;
  const prefix = s > 0 && value[s - 1] !== '\n' ? '\n' : '';
  ta.value = value.slice(0, s) + prefix + code + '\n' + value.slice(e);
  ta.focus();
  ta.selectionStart = ta.selectionEnd = s + prefix.length + code.length + 1;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

async function previewScript() {
  try {
    await saveNow();
    const res = await api.post(`/api/jobs/${job.id}/preview`, job);
    $('#previewText').textContent = res.script;
    $('#dlgPreview').showModal();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- step 4: commands / detection
function renderCommandSwitches() {
  $$('[data-cmd]').forEach(box => {
    const k = box.dataset.cmd;
    const p = `commands.${k}`;
    box.innerHTML = `
      <label class="inline">-DeployMode
        <select data-bind="${p}.deploy_mode"><option>Silent</option><option>Auto</option><option>Interactive</option><option>NonInteractive</option><option value="Default">(not specified)</option></select>
      </label>
      <label class="switch"><input type="checkbox" data-bind="${p}.suppress_reboot"><span></span> -SuppressRebootPassThru</label>
      <label class="switch"><input type="checkbox" data-bind="${p}.terminal_server"><span></span> -TerminalServerMode</label>
      <label class="switch"><input type="checkbox" data-bind="${p}.disable_logging"><span></span> -DisableLogging</label>
      <label class="switch"><input type="checkbox" data-bind="${p}.use_custom"><span></span> Use custom command line</label>
      <input data-bind="${p}.custom" data-show="${p}.use_custom" placeholder="Invoke-AppDeployToolkit.exe -DeploymentType ${k === 'install' ? 'Install' : 'Uninstall'} …">
      <p class="small">Result: <code class="cmd-preview" data-preview="${k}"></code></p>`;
  });
}

let cmdTimer;
function refreshCommandPreview() {
  clearTimeout(cmdTimer);
  cmdTimer = setTimeout(async () => {
    try {
      const r = await api.post(`/api/jobs/${job.id}/commands`, job);
      $('[data-preview="install"]').textContent = r.install_command;
      $('[data-preview="uninstall"]').textContent = r.uninstall_command;
    } catch { /* ignore */ }
  }, 200);
}

const OPS = [['equal', 'Equals'], ['notEqual', 'Not equal to'], ['greaterThan', 'Greater than'],
  ['greaterThanOrEqual', 'Greater than or equal to'], ['lessThan', 'Less than'], ['lessThanOrEqual', 'Less than or equal to']];
const FILE_TYPES = [['exists', 'File or folder exists'], ['doesNotExist', 'File or folder does not exist'], ['version', 'File version'],
  ['sizeInMB', 'Size in MB'], ['modifiedDate', 'Date modified'], ['createdDate', 'Date created']];
const REG_TYPES = [['exists', 'Key / value exists'], ['doesNotExist', 'Key / value does not exist'], ['string', 'String comparison'],
  ['integer', 'Integer comparison'], ['version', 'Version comparison']];
const RULE_NAMES = { msi: 'MSI product code', file: 'File / folder', registry: 'Registry', script: 'Custom detection script' };

const selectHtml = (path, opts) => `<select data-bind="${path}">${opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>`;
const sw = (path, label) => `<label class="switch"><input type="checkbox" data-bind="${path}"><span></span> ${label}</label>`;

function renderRules() {
  const box = $('#rules');
  const rules = job.detection.rules;
  if (!rules.length) {
    box.innerHTML = '<p class="banner warn">No detection rules yet – add one below.</p>';
    return;
  }
  box.innerHTML = rules.map((r, i) => {
    const p = `detection.rules.${i}`;
    let body = '';
    if (r.type === 'msi') {
      body = `<div class="grid g3">
        <label>Product code <input data-bind="${p}.product_code" placeholder="{GUID}"></label>
        <label>Version check ${selectHtml(`${p}.version_operator`, [['notConfigured', 'Not configured'], ...OPS])}</label>
        ${r.version_operator && r.version_operator !== 'notConfigured' ? `<label>Version <input data-bind="${p}.version"></label>` : ''}
      </div>`;
    } else if (r.type === 'file') {
      const cmp = !['exists', 'doesNotExist'].includes(r.detection_type || 'exists');
      body = `<div class="grid g2">
        <label>Path <input data-bind="${p}.path" placeholder="%ProgramFiles%\\Vendor\\App"></label>
        <label>File or folder <input data-bind="${p}.name" placeholder="app.exe"></label>
        <label>Detection method ${selectHtml(`${p}.detection_type`, FILE_TYPES)}</label>
        ${cmp ? `<div class="grid g2"><label>Operator ${selectHtml(`${p}.operator`, OPS)}</label>
          <label>Value <input data-bind="${p}.value" placeholder="${r.detection_type === 'version' ? '1.2.3.4' : r.detection_type === 'sizeInMB' ? '10' : '2026-01-01T00:00:00Z'}"></label></div>` : ''}
      </div>${sw(`${p}.check32`, 'Associated with a 32-bit app on 64-bit clients')}`;
    } else if (r.type === 'registry') {
      const cmp = !['exists', 'doesNotExist'].includes(r.detection_type || 'exists');
      body = `<div class="grid g2">
        <label>Key path <input data-bind="${p}.key" placeholder="HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{key}"></label>
        <label>Value name <input data-bind="${p}.value_name" placeholder="(empty = key only)"></label>
        <label>Detection method ${selectHtml(`${p}.detection_type`, REG_TYPES)}</label>
        ${cmp ? `<div class="grid g2"><label>Operator ${selectHtml(`${p}.operator`, OPS)}</label>
          <label>Value <input data-bind="${p}.value"></label></div>` : ''}
      </div>${sw(`${p}.check32`, 'Associated with a 32-bit app on 64-bit clients')}`;
    } else if (r.type === 'script') {
      body = `<label><span class="row between"><span>Script (detected = exit code 0 <em>and</em> output on STDOUT)</span>
          <a href="#" class="small" data-gen-script="${i}">Generate from app name/version</a></span>
        <textarea class="code" rows="12" data-bind="${p}.content"></textarea></label>
        <div class="row wrap">${sw(`${p}.run32`, 'Run as 32-bit process')}${sw(`${p}.enforce_signature`, 'Enforce signature check')}</div>`;
    }
    return `<div class="rule"><div class="rule-head"><span>${i + 1}. ${RULE_NAMES[r.type] || r.type}</span>
      <button class="btn ghost danger small" data-remove-rule="${i}">Remove</button></div>${body}</div>`;
  }).join('');
  bindAll(box);
  const msgs = [...ruleErrors(), ...ruleWarnings()];
  if (msgs.length) box.insertAdjacentHTML('beforeend', `<p class="banner warn">${msgs.map(esc).join('<br>')}</p>`);
}

// Compare dotted versions numerically: returns -1 / 0 / 1 (missing parts count as 0)
function compareVersions(a, b) {
  const pa = String(a).split('.').map(n => parseInt(n, 10) || 0);
  const pb = String(b).split('.').map(n => parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d > 0 ? 1 : -1;
  }
  return 0;
}

// Non-blocking checks: an MSI version condition that the packaged installer itself would not satisfy
function ruleWarnings() {
  const inst = job.installer || {};
  const actual = inst.metadata?.ProductVersion || inst.display_version;
  const test = { equal: c => c === 0, notEqual: c => c !== 0, greaterThan: c => c > 0, greaterThanOrEqual: c => c >= 0,
    lessThan: c => c < 0, lessThanOrEqual: c => c <= 0 };
  const warns = [];
  job.detection.rules.forEach((r, i) => {
    if (r.type !== 'msi' || !actual || !test[r.version_operator]) return;
    if (!String(r.version || '').trim()) {
      warns.push(`Rule ${i + 1}: version check is set but no version is entered.`);
    } else if (r.product_code === inst.product_code && !test[r.version_operator](compareVersions(actual, r.version))) {
      const label = OPS.find(o => o[0] === r.version_operator)?.[1].toLowerCase();
      warns.push(`Rule ${i + 1}: this installer is version ${actual}, which is not "${label} ${r.version}" – ` +
        'the app would never be detected. Use "Not configured" or "Greater than or equal to" ' + actual + '.');
    }
  });
  return warns;
}

function newRule(type) {
  const i = job.installer || {};
  return {
    msi: { type, product_code: i.product_code || '', version_operator: 'notConfigured', version: '' },
    file: { type, path: '%ProgramFiles%\\' + (job.app.name || ''), name: '', detection_type: 'exists', operator: 'greaterThanOrEqual', value: '', check32: false },
    registry: { type, key: 'HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\', value_name: '', detection_type: 'exists', operator: 'equal', value: '', check32: false },
    script: { type, content: '', run32: false, enforce_signature: false },
  }[type];
}

function ruleErrors() {
  const rules = job.detection.rules;
  const errs = [];
  if (!rules.length) errs.push('At least one detection rule is required.');
  if (rules.some(r => r.type === 'script') && rules.length > 1) errs.push('A detection script cannot be combined with other rules.');
  rules.forEach((r, i) => {
    if (r.type === 'msi' && !r.product_code) errs.push(`Rule ${i + 1}: product code is required.`);
    if (r.type === 'file' && (!r.path || !r.name)) errs.push(`Rule ${i + 1}: path and file/folder are required.`);
    if (r.type === 'registry' && !r.key) errs.push(`Rule ${i + 1}: key path is required.`);
    if (r.type === 'script' && !String(r.content || '').trim()) errs.push(`Rule ${i + 1}: script is empty.`);
  });
  return errs;
}

async function genScript(i) {
  const name = job.installer?.display_name || job.app.name;
  const r = await api.post(`/api/jobs/${job.id}/detection-script`, { name, version: job.app.version });
  job.detection.rules[i].content = r.script;
  renderRules();
  scheduleSave();
}

const RC_TYPES = [['success', 'Success'], ['softReboot', 'Soft reboot'], ['hardReboot', 'Hard reboot'], ['retry', 'Retry'], ['failed', 'Failed']];
function renderReturnCodes() {
  $('#returnCodes').innerHTML = job.return_codes.map((rc, i) => `<div class="list-row rc">
      <input type="number" data-bind="return_codes.${i}.code">
      ${selectHtml(`return_codes.${i}.type`, RC_TYPES)}
      <button class="btn ghost danger small" data-remove-rc="${i}">✕</button></div>`).join('');
  bindAll($('#returnCodes'));
}

// ---------------------------------------------------------------- step 5: build
function renderBuild() {
  const s = job.status;
  const rows = [
    ['Installer', job.installer?.file],
    ['Install code', (job.script.sections.install || '').split('\n')[0]],
    ['Package folder', s.package_dir], ['.intunewin', s.intunewin],
  ].filter(r => r[1]);
  $('#buildSummary').innerHTML = (s.packaged ? '<p class="banner ok">Package is built and up to date.</p>'
    : s.package_dir ? '<p class="banner warn">Settings changed since the last build – rebuild the package.</p>' : '')
    + `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}</dl>`;
  $('#btnBuild').textContent = s.packaged ? 'Rebuild package' : 'Build package';
}

// ---------------------------------------------------------------- step 6: test
function renderTestResult() {
  const r = job.status.test_result;
  const box = $('#testResult');
  if (!r) {
    box.innerHTML = job.status.packaged ? '' : '<p class="banner warn">Build the package first.</p>';
    return;
  }
  const stale = job.status.tested === null;
  let html = stale ? '<p class="banner warn">Settings changed after this test – run it again.</p>'
    : `<p class="banner ${r.success ? 'ok' : 'bad'}">${r.success ? '✓ All checks passed' : '✗ Test failed'}${r.error ? ' – ' + esc(r.error) : ''}</p>`;
  const steps = r.steps || [];
  if (steps.length) {
    html += `<table class="result-table"><tr><th>Step</th><th>Result</th><th>Details</th></tr>${steps.map(s => `<tr>
      <td>${esc(s.name)}</td><td><span class="pill ${s.ok ? 'ok' : 'bad'}">${s.ok ? 'PASS' : 'FAIL'}</span></td>
      <td class="small">${s.kind === 'deployment'
        ? `Exit code <b>${s.exit_code}</b> (${esc(s.code_type || 'unknown')}) in ${s.seconds}s<br><code>${esc(s.command)}</code>` +
          (s.note ? `<br><span class="pill ${s.ok ? 'warn' : 'bad'}">${s.ok ? 'Note' : 'Problem'}</span> ${esc(s.note)}` : '')
        : `Detected: <b>${s.detected}</b> (expected ${s.expected})${(s.rules || []).map(x => `<br>[${esc(x.type)}] ${esc(x.detail)}`).join('')}`}</td></tr>`).join('')}</table>`;
  }
  const entries = asArray(r.new_entries);
  if (entries.length) {
    html += `<h3>Apps registered by the installer</h3><table class="result-table"><tr><th>Name</th><th>Version</th><th>Key</th><th>Use as detection</th><th>Uninstall</th></tr>
      ${entries.map((e, i) => `<tr><td>${esc(e.DisplayName)}</td><td>${esc(e.DisplayVersion)}</td><td class="small">${esc(e.KeyPath)}</td>
      <td><div class="row wrap">
        ${e.WindowsInstaller && /^\{[0-9A-F-]{36}\}$/i.test(e.KeyName) ? `<button class="btn small" data-use-entry="${i}" data-kind="msi">MSI</button>` : ''}
        <button class="btn small" data-use-entry="${i}" data-kind="registry">Registry</button>
        <button class="btn small" data-use-entry="${i}" data-kind="script">Script</button></div></td>
      <td><button class="btn small" data-use-entry="${i}" data-kind="uninstall" title="Generate uninstall code from this registered name">Use for uninstall</button></td></tr>`).join('')}</table>`;
  }
  const leftovers = asArray(r.leftover_entries);
  if (leftovers.length) html += `<p class="banner warn">Still registered after uninstall: ${leftovers.map(e => esc(e.DisplayName)).join(', ')}</p>`;
  if (asArray(r.logs).length) html += `<p class="small muted">Logs collected: ${asArray(r.logs).map(esc).join(', ')}</p>`;
  box.innerHTML = html;
}
const asArray = v => (Array.isArray(v) ? v : v ? [v] : []);

async function useEntry(i, kind) {
  const e = asArray(job.status.test_result?.new_entries)[i];
  if (!e) return;
  if (kind === 'uninstall') {
    const r = await api.post(`/api/jobs/${job.id}/uninstall-code`,
      { name: e.DisplayName, key_name: e.KeyName, windows_installer: e.WindowsInstaller });
    const cur = (job.script.sections.uninstall || '').trim();
    // Replace the auto-generated default (or nothing); otherwise append, so multiple components can be added
    const replace = !cur || cur === (job.auto?.uninstall || '').trim();
    if (!replace && cur.includes(r.code)) { toast('That code is already in the Uninstall section.'); return; }
    job.script.sections.uninstall = replace ? r.code : `${cur}\n${r.code}`;
    await saveNow();
    await reloadJob();
    toast(`Uninstall section ${replace ? 'updated' : 'extended'} - rebuild the package and re-run the test.`);
    return;
  }
  let rule;
  if (kind === 'msi') rule = { type: 'msi', product_code: e.KeyName, version_operator: 'notConfigured', version: '' };
  else if (kind === 'registry') {
    const is32 = /\\WOW6432Node\\/i.test(e.KeyPath);
    rule = { type: 'registry', key: e.KeyPath.replace(/\\WOW6432Node/i, ''), value_name: 'DisplayVersion',
      detection_type: e.DisplayVersion ? 'version' : 'exists', operator: 'greaterThanOrEqual', value: e.DisplayVersion || '', check32: is32 };
  } else {
    const r = await api.post(`/api/jobs/${job.id}/detection-script`, { name: e.DisplayName, version: e.DisplayVersion });
    rule = { type: 'script', content: r.script, run32: false, enforce_signature: false };
  }
  if (!confirm(`Replace the current detection rules with this ${kind} rule? You'll need to re-run the test.`)) return;
  job.detection.rules = [rule];
  await saveNow();
  await reloadJob();
  toast('Detection rule updated. Re-run the test to validate it.');
}

// ---------------------------------------------------------------- step 7: upload
async function loadAuthConfig() {
  const a = await api.get('/api/auth-config');
  $('#authMode').value = a.auth_mode || 'interactive';
  $('#tenantId').value = a.tenant_id || '';
  $('#clientId').value = a.client_id || '';
  $('#certThumb').value = a.cert_thumbprint || '';
  $('#authSource').innerHTML = a.error ? `<span class="pill bad">${esc(a.error)}</span>`
    : `Loaded from <code>${esc(a.path)}</code> – changes made here are saved back to that file (the secret never is).`;
  updateAuthFields();
}

function renderUpload() {
  loadAuthConfig().catch(e => toast(e.message, true));
  const s = job.status;
  const name = job.app.display_name || defaultDisplayName();
  const testPill = !job.test.enabled ? '<span class="pill warn">Skipped</span>'
    : s.tested ? '<span class="pill ok">Passed</span>' : '<span class="pill bad">Not passed</span>';
  api.post(`/api/jobs/${job.id}/commands`, job).then(c => {
    $('#uploadSummary').innerHTML = `<dl class="kv">
      <dt>Display name</dt><dd>${esc(name)}</dd>
      <dt>Package</dt><dd>${esc(s.intunewin)}</dd>
      <dt>Install</dt><dd><code>${esc(c.install_command)}</code></dd>
      <dt>Uninstall</dt><dd><code>${esc(c.uninstall_command)}</code></dd>
      <dt>Detection</dt><dd>${job.detection.rules.map(r => RULE_NAMES[r.type]).join(', ')}</dd>
      <dt>Install as</dt><dd>${esc(job.commands.run_as)}</dd>
      <dt>Sandbox test</dt><dd>${testPill}</dd></dl>`;
  });
  renderAssignments();
  $('#uploadResult').innerHTML = s.uploaded
    ? `<p class="banner ok">✓ Uploaded. App ID <code>${esc(s.intune_app_id)}</code> – <a href="${esc(s.intune_url)}" target="_blank" rel="noopener">open in Intune</a></p>` : '';
}

function updateAuthFields() {
  const m = $('#authMode').value;
  $$('.auth-secret').forEach(e => e.classList.toggle('hidden', m !== 'secret'));
  $$('.auth-cert').forEach(e => e.classList.toggle('hidden', m !== 'certificate'));
}

// Theme: System (follows Windows) -> Light -> Dark; remembered per browser
const THEMES = { system: 'System', light: 'Light', dark: 'Dark' };
function applyTheme(mode) {
  if (mode === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  const b = $('#btnTheme');
  b.dataset.mode = mode;
  $('#themeLabel').textContent = THEMES[mode];
  b.title = `Theme: ${THEMES[mode]} (click to change)`;
}
function initTheme() {
  let mode = 'system';
  try { mode = THEMES[localStorage.getItem('packalot-theme')] ? localStorage.getItem('packalot-theme') : 'system'; } catch (e) {}
  applyTheme(mode);
  $('#btnTheme').onclick = () => {
    const keys = Object.keys(THEMES);
    const next = keys[(keys.indexOf($('#btnTheme').dataset.mode) + 1) % keys.length];
    applyTheme(next);
    try { localStorage.setItem('packalot-theme', next); } catch (e) {}
  };
}

const TARGETS = [['allUsers', 'All users'], ['allDevices', 'All devices'], ['group', 'Group (include)'], ['exclude', 'Group (exclude)']];
const INTENTS = [['required', 'Required'], ['available', 'Available'], ['uninstall', 'Uninstall']];
const NOTIFS = [['showAll', 'Show all toasts'], ['showReboot', 'Show only restart toasts'], ['hideAll', 'Hide all toasts']];
function renderAssignments() {
  const box = $('#assignments');
  box.innerHTML = job.assignments.length ? job.assignments.map((a, i) => `<div class="list-row">
      ${selectHtml(`assignments.${i}.target`, TARGETS)}
      ${selectHtml(`assignments.${i}.intent`, INTENTS)}
      <input data-bind="assignments.${i}.group_id" placeholder="Group name (or object ID)" title="Exact Entra group display name, or its object ID. Names are looked up at upload time (needs Group.Read.All)." ${['group', 'exclude'].includes(a.target) ? '' : 'disabled'}>
      ${selectHtml(`assignments.${i}.notifications`, NOTIFS)}
      <button class="btn ghost danger small" data-remove-assign="${i}">✕</button></div>`).join('')
    : '<p class="muted small">No assignments – the app will be uploaded unassigned.</p>';
  bindAll(box);
}

async function upload() {
  const auth = {
    auth_mode: $('#authMode').value, tenant_id: $('#tenantId').value.trim(),
    client_id: $('#clientId').value.trim(), cert_thumbprint: $('#certThumb').value.trim(),
  };
  if (job.assignments.some(a => a.intent === 'available' && a.target === 'allDevices')) {
    toast("'Available' can't be assigned to All devices.", true);
    return;
  }
  try { await api.put('/api/auth-config', auth); } catch (e) { toast(e.message, true); return; }
  const ok = await runTask('upload', { ...auth, client_secret: $('#clientSecret').value });
  if (ok) toast('Uploaded to Intune!');
}

// ============================================================================ home
// Save pending edits first; while a task runs, ask (the task keeps running on the server either way)
async function goHome() {
  if (busy && !confirm('A task is still running. It will keep running in the background - go to Home anyway?')) return;
  try { if (job && saveTimer) await saveNow(); } catch { /* ignore - nothing to lose beyond the last keystrokes */ }
  busy = false;
  setBusy(false);
  showHome();
}

async function showHome() {
  job = null;
  location.hash = '';
  updateTitle();
  show('home');
  const jobs = await api.get('/api/jobs');
  $('#jobList').innerHTML = jobs.length ? jobs.map(j => {
    const s = j.status || {};
    const pill = (on, l) => `<span class="pill ${on ? 'ok' : 'idle'}">${l}</span>`;
    return `<div class="job"><div class="meta"><div class="name">${esc(j.name)}</div>
      <div class="muted small">${esc(j.created)} · step ${j.step}/7</div></div>
      ${pill(s.fetched, 'Source')} ${pill(s.packaged, 'Built')} ${s.tested === false ? '<span class="pill bad">Test failed</span>' : pill(s.tested, 'Tested')} ${pill(s.uploaded, 'Uploaded')}
      <button class="btn" data-open-job="${esc(j.id)}">Open</button>
      <button class="btn ghost danger" data-delete-job="${esc(j.id)}">Delete</button></div>`;
  }).join('') : '<div class="empty">No packages yet. Click <b>+ New package</b> to start.</div>';
  loadPrereqs();
}

async function loadPrereqs() {
  try {
    const p = await api.get('/api/prereqs');
    const item = (ok, label, hint) => `<span class="pill ${ok ? 'ok' : 'warn'}" title="${esc(hint)}">${ok ? '✓' : '!'} ${label}</span>`;
    $('#prereqs').innerHTML = [
      item(p.template, 'PSADT template', p.template_dir),
      item(p.winget, 'winget', 'Required for winget sources'),
      item(p.sandbox, 'Windows Sandbox', 'Enable the Windows Sandbox optional feature'),
      item(p.intunewin_util, 'IntuneWinAppUtil', 'Downloaded automatically on first build'),
      item(p.graph_module, 'Microsoft.Graph.Authentication', 'Installed automatically on first upload'),
    ].join('');
  } catch { /* ignore */ }
}

async function openJob(id, n) {
  job = await api.get(`/api/jobs/${id}`);
  show('wizard');
  bindAll();
  updateTitle();
  versionsFor = null;
  $('#wgResults').innerHTML = '';
  $$('.log').forEach(l => { l.innerHTML = ''; });
  await goStep(Math.min(n || job.step || 1, maxStep()));
  attachRunningTask();
}

// ============================================================================ wiring
function init() {
  renderCommandSwitches();
  $('#snippetSelect').insertAdjacentHTML('beforeend', Object.keys(SNIPPETS).map(k => `<option>${esc(k)}</option>`).join(''));

  $('#btnHome').onclick = goHome;
  initTheme();
  $('#btnFinish').onclick = goHome;
  $('#brand').onclick = goHome;
  $('#btnNew').onclick = async () => { const j = await api.post('/api/jobs'); openJob(j.id, 1); };
  $('#btnNext').onclick = next;
  $('#btnBack').onclick = () => goStep(Math.max(1, step - 1));
  $('#btnWgSearch').onclick = wingetSearch;
  $('#wgQuery').addEventListener('keydown', e => { if (e.key === 'Enter') wingetSearch(); });
  $('#customFile').onchange = e => e.target.files[0] && uploadInstaller(e.target.files[0]);
  $('#iconFile').onchange = async e => {
    const f = e.target.files[0]; if (!f) return;
    const fd = new FormData(); fd.append('file', f);
    try {
      await saveNow();
      const r = await api.req('POST', `/api/jobs/${job.id}/icon`, fd);
      job.app.icon_path = r.path;
      $('#iconPreview').src = `/api/jobs/${job.id}/icon?t=${Date.now()}`;
      await saveNow();
    } catch (err) { toast(err.message, true); }
  };
  $('#btnPreview').onclick = previewScript;
  $('#snippetSelect').onchange = e => { insertSnippet(e.target.value); e.target.value = ''; };
  $('#btnBuild').onclick = () => runTask('build');
  $('#btnTest').onclick = () => runTask('test');
  $('#btnUpload').onclick = upload;
  $('#authMode').onchange = updateAuthFields;
  $('#btnAddRc').onclick = () => { job.return_codes.push({ code: 0, type: 'success' }); renderReturnCodes(); scheduleSave(); };
  $('#btnAddAssign').onclick = () => {
    job.assignments.push({ target: 'group', intent: 'required', group_id: '', notifications: 'showAll' });
    renderAssignments(); scheduleSave();
  };

  $$('#phaseTabs .tab').forEach(t => t.onclick = () => {
    $$('#phaseTabs .tab').forEach(x => x.classList.toggle('active', x === t));
    $$('[data-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.panel !== t.dataset.tab));
  });

  document.addEventListener('focusin', e => { if (e.target.matches('textarea.code[data-bind^="script.sections"]')) lastEditor = e.target; });
  document.addEventListener('keydown', e => {
    if (e.key === 'Tab' && e.target.matches('textarea.code') && !e.shiftKey) {
      e.preventDefault();
      const ta = e.target; const s = ta.selectionStart;
      ta.value = ta.value.slice(0, s) + '    ' + ta.value.slice(ta.selectionEnd);
      ta.selectionStart = ta.selectionEnd = s + 4;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
  });

  // delegated clicks
  document.addEventListener('click', async e => {
    const t = e.target.closest('[data-goto],[data-open-job],[data-delete-job],[data-add-rule],[data-remove-rule],[data-remove-rc],[data-remove-assign],[data-reset],[data-gen-script],[data-use-entry],[data-open],[data-close],tr[data-id]');
    if (!t) return;
    const d = t.dataset;
    if (d.goto) goStep(Number(d.goto));
    else if (d.openJob) openJob(d.openJob);
    else if (d.deleteJob) {
      if (confirm('Delete this package and all its files?')) { await api.del(`/api/jobs/${d.deleteJob}`); showHome(); }
    } else if (d.addRule) {
      job.detection.rules.push(newRule(d.addRule)); renderRules(); scheduleSave();
    } else if (d.removeRule !== undefined) {
      job.detection.rules.splice(Number(d.removeRule), 1); renderRules(); scheduleSave();
    } else if (d.removeRc !== undefined) {
      job.return_codes.splice(Number(d.removeRc), 1); renderReturnCodes(); scheduleSave();
    } else if (d.removeAssign !== undefined) {
      job.assignments.splice(Number(d.removeAssign), 1); renderAssignments(); scheduleSave();
    } else if (d.reset) {
      e.preventDefault();
      const def = job.auto?.[d.reset];
      if (def && confirm(`Reset the ${d.reset} section to the generated default?`)) {
        job.script.sections[d.reset] = def; bindAll(); scheduleSave();
      }
    } else if (d.genScript !== undefined) {
      e.preventDefault(); genScript(Number(d.genScript));
    } else if (d.useEntry !== undefined) {
      useEntry(Number(d.useEntry), d.kind);
    } else if (d.open) {
      api.post(`/api/jobs/${job.id}/open`, { what: d.open }).catch(err => toast(err.message, true));
    } else if (d.close !== undefined) {
      t.closest('dialog').close();
    } else if (d.id) {
      job.source.winget_id = d.id; job.source.version = '';
      $$('#wgResults tr').forEach(r => r.classList.toggle('selected', r === t));
      bindAll($('#srcWinget')); loadVersions(); scheduleSave(); loadInstallerPreview();
    }
  });
  // assignment target change enables/disables the group field
  document.addEventListener('change', e => { if (/^assignments\.\d+\.target$/.test(e.target.dataset?.bind || '')) renderAssignments(); });

  // settings dialog
  $('#btnSettings').onclick = async () => {
    settings = await api.get('/api/settings');
    $$('[data-setting]').forEach(el => {
      const v = settings[el.dataset.setting];
      if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? '';
    });
    $('#dlgSettings').showModal();
  };
  $('#btnSaveSettings').onclick = async () => {
    const vals = {};
    $$('[data-setting]').forEach(el => { vals[el.dataset.setting] = readEl(el); });
    settings = await api.put('/api/settings', vals);
    $('#dlgSettings').close();
    toast('Settings saved');
    if (!job) showHome();
  };

  window.addEventListener('beforeunload', () => { if (job && saveTimer) saveNow(); });
}

(async function start() {
  init();
  settings = await api.get('/api/settings').catch(() => ({}));
  const m = /job=([^&]+)(?:&step=(\d+))?/.exec(location.hash);
  if (m) {
    try { await openJob(m[1], Number(m[2]) || 0); return; } catch { /* fall through */ }
  }
  showHome();
})();
