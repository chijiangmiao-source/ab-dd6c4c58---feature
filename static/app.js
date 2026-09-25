/* 标定谱系页面：所有数据均来自真实接口 */
const $ = (sel) => document.querySelector(sel);

const state = { records: [], branches: [], currentBranch: null };

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data?.error?.message || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data?.error?.code;
    err.details = data?.error?.details;
    throw err;
  }
  return data;
}

function feedback(msg, cls) {
  const el = $("#feedback");
  el.textContent = msg;
  el.className = "feedback " + (cls || "muted");
}

async function refresh() {
  const records = await api("GET", "/api/records");
  state.records = records;
  renderRecords();
  renderParentOptions();
  await refreshBranches();
}

function renderRecords() {
  if (!state.records.length) {
    $("#records").innerHTML = '<p class="muted">暂无记录，先建立一条原始记录吧。</p>';
    return;
  }
  $("#records").innerHTML = state.records.map((r) => {
    const text = typeof r.payload.value !== "undefined"
      ? r.payload.value : JSON.stringify(r.payload);
    const basis = r.parent_ids.length
      ? `<div class="parents-line">直接依据：${
          r.parent_ids.map((p) => `<span class="pid">${esc(p)}</span>`).join("、")
        }</div>`
      : "";
    const src = r.invalidated_by
      ? `<div class="meta">失效来源（稳定）：<span class="src">${esc(r.invalidated_by)}</span>${
          r.invalidated_at ? ` · ${esc(r.invalidated_at)}` : ""
        }</div>`
      : "";
    return `
      <div class="record">
        <div class="head">
          <span class="id">${esc(r.id)}</span>
          <span class="badge official">正式</span>
          <span class="badge ${esc(r.kind)}">${r.kind === "raw" ? "原始" : "推导"}</span>
          <span class="badge ${esc(r.status)}">${r.status === "valid" ? "有效" : "已失效"}</span>
        </div>
        <div class="meta">${esc(text)}</div>
        ${basis}
        ${src}
      </div>`;
  }).join("");
}

function renderParentOptions() {
  const valids = state.records.filter((r) => r.status === "valid");
  const box = $("#parents");
  if (!valids.length) {
    box.innerHTML = '<span class="muted">当前没有可引用的有效记录</span>';
    return;
  }
  box.innerHTML = valids.map((r) => `
    <label><input type="checkbox" value="${esc(r.id)}">
      <span class="id">${esc(r.id)}</span>
      <span class="muted">（${r.kind === "raw" ? "原始" : "推导"}）</span>
    </label>`).join("");
}

$("#kind").addEventListener("change", () => {
  $("#parents-row").hidden = $("#kind").value !== "derived";
});

$("#create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    kind: $("#kind").value,
    payload: { value: $("#value").value || "" },
  };
  if ($("#kind").value === "derived") {
    body.parent_ids = [...document.querySelectorAll("#parents input:checked")]
      .map((c) => c.value);
  }
  try {
    const rec = await api("POST", "/api/records", body);
    feedback(`已创建记录 ${rec.id}（${rec.status}），直接依据：${
      rec.parent_ids.length ? rec.parent_ids.join("、") : "无"
    }`, "ok-text");
    $("#value").value = "";
    await refresh();
  } catch (err) {
    feedback(`创建被拒绝（${err.code || err.status}）：${err.message}\n` +
      (err.details ? `定位信息：${JSON.stringify(err.details, null, 2)}` : ""),
      "error-text");
  }
});

$("#invalidate-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const target = $("#inv-target").value.trim();
  const op = $("#inv-op").value.trim();
  try {
    const res = await api("POST",
      `/api/records/${encodeURIComponent(target)}/invalidate`,
      { operation_id: op });
    feedback(
      `裁决完成${res.replayed ? "（重复裁决，返回首次结果）" : ""}\n` +
      `操作标识：${res.operation_id}\n失效来源：${res.target_record_id}\n` +
      `级联失效 ${res.cascade.length} 条：${res.cascade.map((c) => c.id).join("、")}`,
      "ok-text");
    await refresh();
  } catch (err) {
    feedback(`裁决失败（${err.code || err.status}）：${err.message}\n` +
      (err.details ? `定位信息：${JSON.stringify(err.details, null, 2)}` : ""),
      "error-text");
  }
});

$("#refresh").addEventListener("click", () =>
  refresh().catch((e) => feedback(e.message, "error-text")));

/* ---------------- 分支试建与整组发布 ---------------- */

async function refreshBranches() {
  state.branches = await api("GET", "/api/branches");
  renderBranches();
  if (state.currentBranch) {
    const still = state.branches.find((b) => b.id === state.currentBranch.id);
    if (still) {
      await loadBranch(still.id);
    } else {
      state.currentBranch = null;
      $("#branch-detail-card").hidden = true;
    }
  }
}

function renderBranches() {
  const box = $("#branches");
  if (!state.branches.length) {
    box.innerHTML = '<p class="muted">暂无分支。点击“新建分支”开始试建草案。</p>';
    return;
  }
  box.innerHTML = state.branches.map((b) => `
    <div class="branch-item${state.currentBranch && state.currentBranch.id === b.id
        ? " selected" : ""}" data-bid="${esc(b.id)}">
      <span class="id">${esc(b.id)}</span>
      <span class="badge ${b.status === "open" ? "draft" : "published"}">${
        b.status === "open" ? "试建中" : "已发布"}</span>
      <span class="muted">快照 ${b.snapshot_size} 条 · 草案 ${b.entry_count} 条</span>
    </div>`).join("");
  box.querySelectorAll(".branch-item").forEach((el) =>
    el.addEventListener("click", () =>
      loadBranch(el.dataset.bid).catch((e) => feedback(e.message, "error-text"))));
}

async function loadBranch(branchId) {
  const branch = await api("GET", `/api/branches/${encodeURIComponent(branchId)}`);
  state.currentBranch = branch;
  renderBranches();
  renderBranchDetail();
}

function renderBranchDetail() {
  const b = state.currentBranch;
  if (!b) return;
  $("#branch-detail-card").hidden = false;
  $("#branch-detail-id").textContent = b.id;
  $("#branch-detail-status").innerHTML =
    `<span class="badge ${b.status === "open" ? "draft" : "published"}">${
      b.status === "open" ? "试建中" : "已发布"}</span>`;

  $("#branch-snapshot").innerHTML =
    `<div class="meta">创建时刻有效记录快照（${b.snapshot_record_ids.length} 条）：` +
    (b.snapshot_record_ids.length
      ? b.snapshot_record_ids.map((r) => `<span class="pid">${esc(r)}</span>`).join("、")
      : '<span class="muted">空</span>') + "</div>";

  const entriesBox = $("#branch-entries");
  if (!b.entries.length) {
    entriesBox.innerHTML = '<p class="muted">尚无草案条目。</p>';
  } else {
    entriesBox.innerHTML = b.entries.map((e) => {
      const text = typeof e.payload.value !== "undefined"
        ? e.payload.value : JSON.stringify(e.payload);
      const basis = e.parents.length
        ? `<div class="parents-line">直接依据：${e.parents.map((p) =>
            p.kind === "entry"
              ? `<span class="pid draft">${esc(p.id)}</span>`
              : `<span class="pid">${esc(p.id)}</span>`).join("、")}</div>`
        : "";
      const published = e.published_record_id
        ? `<div class="meta">已发布为正式记录 <span class="pid">${esc(e.published_record_id)}</span></div>`
        : "";
      return `
        <div class="record draft-record">
          <div class="head">
            <span class="id">${esc(e.id)}</span>
            <span class="badge draft">草案</span>
            <span class="badge ${esc(e.kind)}">${e.kind === "raw" ? "原始" : "推导"}</span>
          </div>
          <div class="meta">${esc(text)}</div>
          ${basis}
          ${published}
        </div>`;
    }).join("");
  }

  const open = b.status === "open";
  $("#entry-form").hidden = !open;
  $("#publish-form").hidden = !open;
  renderEntryParentOptions();
}

function renderEntryParentOptions() {
  const b = state.currentBranch;
  const box = $("#entry-parents");
  if (!b) { box.innerHTML = ""; return; }
  const snap = b.snapshot_record_ids.map((id) => ({ id, draft: false }));
  const drafts = b.entries.map((e) => ({ id: e.id, draft: true }));
  const options = [...snap, ...drafts];
  if (!options.length) {
    box.innerHTML = '<span class="muted">快照与本分支暂无可引用条目</span>';
    return;
  }
  box.innerHTML = options.map((o) => `
    <label><input type="checkbox" value="${esc(o.id)}">
      <span class="id ${o.draft ? "draft-id" : ""}">${esc(o.id)}</span>
      <span class="muted">（${o.draft ? "本分支草案" : "快照·正式"}）</span>
    </label>`).join("");
}

$("#branch-create").addEventListener("click", async () => {
  try {
    const b = await api("POST", "/api/branches");
    feedback(`已创建分支 ${b.id}：保存了 ${b.snapshot_record_ids.length} 条` +
      "当时有效记录的快照，可开始试建草案。", "ok-text");
    await refreshBranches();
    await loadBranch(b.id);
  } catch (err) {
    feedback(`创建分支失败（${err.code || err.status}）：${err.message}`, "error-text");
  }
});

$("#entry-kind").addEventListener("change", () => {
  $("#entry-parents-row").hidden = $("#entry-kind").value !== "derived";
});

$("#entry-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = state.currentBranch;
  if (!b) return;
  const body = {
    kind: $("#entry-kind").value,
    payload: { value: $("#entry-value").value || "" },
  };
  if (body.kind === "derived") {
    body.parent_refs = [...document.querySelectorAll("#entry-parents input:checked")]
      .map((c) => c.value);
  }
  try {
    const entry = await api("POST",
      `/api/branches/${encodeURIComponent(b.id)}/entries`, body);
    feedback(`已在分支 ${b.id} 追加草案条目 ${entry.id}（草案编号，未进入正式谱系）`,
      "ok-text");
    $("#entry-value").value = "";
    await loadBranch(b.id);
  } catch (err) {
    feedback(`草案条目被拒绝（${err.code || err.status}）：${err.message}\n` +
      (err.details ? `定位信息：${JSON.stringify(err.details, null, 2)}` : ""),
      "error-text");
  }
});

$("#publish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = state.currentBranch;
  if (!b) return;
  const op = $("#pub-op").value.trim();
  try {
    const res = await api("POST",
      `/api/branches/${encodeURIComponent(b.id)}/publish`,
      { operation_id: op });
    const pairs = Object.entries(res.mapping || {})
      .map(([d, r]) => `${d} → ${r}`).join("、") || "（无条目）";
    feedback(
      `发布完成${res.replayed ? "（重复发布，返回首次编号映射）" : ""}\n` +
      `操作标识：${res.operation_id}\n分支：${res.branch_id}\n编号映射：${pairs}`,
      "ok-text");
    await refresh();
    await loadBranch(res.branch_id);
  } catch (err) {
    feedback(`发布被拒绝（${err.code || err.status}）：${err.message}\n` +
      (err.details ? `定位信息：${JSON.stringify(err.details, null, 2)}` : ""),
      "error-text");
    await refresh().catch(() => {});
  }
});

refresh().catch((e) => feedback(e.message, "error-text"));
