/* 编排流台：组件化 SPA（Svelte 式状态驱动渲染，无构建步骤便于容器验收） */
const state = {
  user: '', lab: '', items: [], selected: null,
  plantPanel: null, branchErr: '', who: 'u1', busy: false,
};

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || '请求失败');
  return j;
}

async function boot() {
  try {
    const me = await api('/api/me');
    state.user = me.user;
    state.lab = me.lab;
    state.items = (await api('/api/flows')).items;
    await refreshPanel();
    render();
  } catch (e) {
    location.href = '/login';
  }
}

function panelUrl() {
  return '/api/branch/panel' + (state.selected ? '?flow_id=' + state.selected.id : '');
}

async function refreshPanel() {
  try {
    state.plantPanel = await api(panelUrl());
    state.branchErr = '';
  } catch (e) {
    state.plantPanel = { error: String(e.message || e) };
  }
}

async function refreshFlows() {
  state.items = (await api('/api/flows')).items;
  if (state.selected) state.selected = await api('/api/flows/' + state.selected.id);
}

async function select(id) {
  state.selected = await api('/api/flows/' + id);
  await refreshPanel();
  render();
}

async function doExtract() {
  state.selected = await api('/api/flows/' + state.selected.id + '/extract', { method: 'POST' });
  await refreshFlows();
  await refreshPanel();
  render();
}

async function doLand() {
  state.selected = await api('/api/flows/' + state.selected.id + '/land', { method: 'POST' });
  await refreshFlows();
  render();
}

async function logout() {
  await api('/api/logout', { method: 'POST' });
  location.href = '/login';
}

async function branchAction(body) {
  if (state.busy) return;
  state.busy = true;
  state.branchErr = '';
  render();
  try {
    await api('/api/branch/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await refreshPanel();
    await refreshFlows();
  } catch (e) {
    // 抢领冲突、未领齐等 409 口径直接展示给操作人
    state.branchErr = e.message;
    await refreshPanel();
  } finally {
    state.busy = false;
    render();
  }
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function renderClaimList(p) {
  const rows = p.items.map((b) => {
    const claimed = !!b.owner;
    return `
      <tr class="${claimed ? 'is-claimed' : ''}">
        <td class="mono">支路 ${esc(b.branch_no)}</td>
        <td>${claimed ? '已领 · <strong>' + esc(b.owner) + '</strong>' : '<span class="tag tag-open">未领</span>'}</td>
        <td>${b.ok ? '<span class="tag tag-ok">已确认</span>' : '<span class="tag tag-wait">待拼段</span>'}</td>
        <td>
          <button type="button" class="mini" data-claim="${esc(b.branch_no)}" ${claimed || state.busy ? 'disabled' : ''}>
            ${claimed ? '已被领取' : '领取'}
          </button>
        </td>
      </tr>`;
  }).join('');
  return `
    <section class="card">
      <h3>${esc(p.list_label || '领段列表')} <span class="progress">已领 ${p.claimed ?? 0}/${p.total ?? p.items.length}</span></h3>
      <table class="branch-table"><tbody>${rows}</tbody></table>
      <label class="who-line">领段工号
        <input id="who-input" value="${esc(state.who)}" maxlength="32" ${state.busy ? 'disabled' : ''} />
      </label>
      <p class="hint">同一支路只发一枚号：并发抢领在后端排队，后提交者会收到“该支路已被领取”。</p>
    </section>`;
}

function renderMergeDetail(p) {
  const lines = p.items.map((b) => `
      <li class="mono">支路 ${esc(b.branch_no)}：${b.owner ? esc(b.owner) + (b.ok ? '（已确认）' : '（已领待拼）') : '<span class="tag tag-open">缺失</span>'}</li>`).join('');
  const missing = (p.missing || []).length
    ? `<p class="warn">未领齐：缺 ${p.missing.map(esc).join('、')}，禁止拼段。</p>`
    : '';
  const mergeLabel = p.merged ? '已拼段落库' : (p.all_claimed ? '拼段落库' : '拼段落库（未领齐）');
  return `
    <section class="card">
      <h3>拼段详情</h3>
      <ul class="branch-lines">${lines}</ul>
      ${missing}
      <button type="button" id="btn-merge" ${(!p.can_merge || state.busy) ? 'disabled' : ''}>${mergeLabel}</button>
      <p class="hint">未领齐时按钮禁用只是体验层，后端同样拒绝拼段请求。</p>
    </section>`;
}

function renderCloseout(p) {
  return `
    <section class="card closeout">
      <h3>收口页</h3>
      <div class="strategy-banner">
        <span class="tag tag-strategy">收口策略</span>
        <strong>${esc(p.strategy_name)}</strong>
        <p class="hint">${esc(p.strategy_desc || '')}</p>
      </div>
      <p class="hint">回滚在单事务内原子清掉该任务全部已抽段（含已确认段），不留残留；领段列表进度同步归零。</p>
      <button type="button" class="ghost" id="btn-rollback" ${state.busy ? 'disabled' : ''}>整回滚</button>
    </section>`;
}

function renderPlantPanel() {
  const p = state.plantPanel;
  if (!p) return '';
  if (p.error) return `<section class="card"><h3>支路抢领</h3><p class="warn">${esc(p.error)}</p></section>`;
  if (!state.selected || p.flow_id == null) {
    return `<section class="card"><h3>支路抢领</h3><p class="hint">从左侧选择一条编排流后展示领段列表。</p>
      <div class="strategy-banner"><span class="tag tag-strategy">收口策略</span><strong>${esc(p.strategy_name)}</strong></div></section>`;
  }
  return `
    <section class="plant-panel">
      <div class="plant-head">
        <h3>支路抢领 · 流 #${esc(p.flow_id)}</h3>
        <span class="tag tag-strategy">收口策略：${esc(p.strategy_name)}</span>
      </div>
      ${state.branchErr ? `<p class="warn plant-err">${esc(state.branchErr)}</p>` : ''}
      <div class="plant-grid">
        ${renderClaimList(p)}
        ${renderMergeDetail(p)}
        ${renderCloseout(p)}
      </div>
    </section>`;
}

function render() {
  const el = document.getElementById('app');
  const rows = state.items.map((it) => `
    <button type="button" class="row ${state.selected && state.selected.id === it.id ? 'on' : ''}" data-id="${it.id}">
      <strong>${esc(it.title)}</strong>
      <span class="mono">${esc(it.code)}</span>
      <span class="chip">${esc(it.status)}</span>
    </button>`).join('');
  const detail = state.selected ? `
    <section class="detail">
      <h2>${esc(state.selected.title)}</h2>
      <p class="mono">${esc(state.selected.source_uri)}</p>
      <p>状态 <span class="chip">${esc(state.selected.status)}</span> · 已抽取 ${state.selected.extracted} · 已落库 ${state.selected.landed}</p>
      <div class="actions">
        <button type="button" id="btn-ex">执行抽取</button>
        <button type="button" id="btn-land" class="ghost">执行落库</button>
      </div>
    </section>` : '<p class="hint">从左侧选择一条编排流</p>';
  el.innerHTML = `
    <header class="bar"><div class="brand">${esc(state.lab)}</div><div>${esc(state.user)} · <button type="button" id="out" class="link">退出</button></div></header>
    <div class="layout"><aside>${rows}</aside><main>${detail}${renderPlantPanel()}</main></div>`;

  el.querySelectorAll('.row').forEach((b) => { b.onclick = () => select(+b.dataset.id); });
  const out = el.querySelector('#out'); if (out) out.onclick = logout;
  const be = el.querySelector('#btn-ex'); if (be) be.onclick = doExtract;
  const bl = el.querySelector('#btn-land'); if (bl) bl.onclick = doLand;
  const whoInput = el.querySelector('#who-input');
  if (whoInput) whoInput.oninput = () => { state.who = whoInput.value; };
  el.querySelectorAll('[data-claim]').forEach((btn) => {
    btn.onclick = () => branchAction({
      action: 'claim',
      flow_id: state.selected.id,
      branch_no: btn.dataset.claim,
      who: (state.who || '').trim(),
    });
  });
  const bm = el.querySelector('#btn-merge');
  if (bm) bm.onclick = () => branchAction({ action: 'merge', flow_id: state.selected.id });
  const br = el.querySelector('#btn-rollback');
  if (br) br.onclick = () => branchAction({ action: 'rollback', flow_id: state.selected.id });
}

boot();
