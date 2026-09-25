/* 标定谱系页面：所有数据均来自真实接口 */
const $ = (sel) => document.querySelector(sel);

const state = { records: [], branches: [], currentBranchId: null, currentBranch: null };

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

function branchFeedback(msg, cls) {
  const el = $("#branch-feedback");
  el.textContent = msg;
  el.className = "feedback " + (cls || "muted");
}

async function refresh() {
  const records = await api("GET", "/api/records");
  state.records = records;
  renderRecords();
  renderParentOptions();
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

// --------------------------------------------------------------------------- //
// 分支试建
// --------------------------------------------------------------------------- //
async function refreshBranches(selectId) {
  state.branches = await api("GET", "/api/branches");
  const sel = $("#branch-select");
  const keep = selectId || state.currentBranchId
    || (state.branches.find((b) => b.status === "draft") || {}).id;
  sel.innerHTML = state.branches.length
    ? state.branches.map((b) =>
        `<option value="${esc(b.id)}"${b.id === keep ? " selected" : ""}>` +
        `${esc(b.id)} · ${branchStatusText(b.status)}` +
        ` · 快照 ${b.snapshot_count} · 草案 ${b.entry_count}</option>`).join("")
    : '<option value="">（暂无分支）</option>';
  state.currentBranchId = keep || null;
  if (state.currentBranchId) {
    await loadBranch(state.currentBranchId);
  } else {
    state.currentBranch = null;
    $("#branch-detail").innerHTML =
      '<p class="muted">创建分支后，可在不影响正式谱系的情况下试建草案。</p>';
    $("#draft-parents").innerHTML = "";
  }
}

function branchStatusText(s) {
  return s === "draft" ? "草案中" : s === "published" ? "已发布" : s;
}

async function loadBranch(id) {
  state.currentBranch = await api("GET", `/api/branches/${encodeURIComponent(id)}`);
  renderBranchDetail();
  renderDraftParentOptions();
}

function renderBranchDetail() {
  const b = state.currentBranch;
  if (!b) return;
  const snapList = b.snapshot.length
    ? b.snapshot.map((s) =>
        `<span class="pid" title="快照直接依据：${
          esc(s.direct_basis.join("、") || "无（原始记录）")}">${esc(s.record_id)}</span>`)
      .join("、")
    : '<span class="muted">空（创建分支时正式谱系无有效记录）</span>';
  const snapIdSet = new Set(b.snapshot.map((s) => s.record_id));
  const entryList = b.entries.length
    ? b.entries.map((e) => {
        const text = typeof e.payload.value !== "undefined"
          ? e.payload.value : JSON.stringify(e.payload);
        const basis = e.parent_refs.length
          ? `<div class="parents-line">依据：${e.parent_refs.map((p) =>
              `<span class="${snapIdSet.has(p) ? "pid" : "draft-pid"}" title="${
                snapIdSet.has(p) ? "分支快照中的正式记录" : "本分支先前草案"}">${esc(p)}</span>`)
              .join("、")}</div>`
          : "";
        const mapped = e.record_id
          ? ` → 正式编号 <span class="id ok-text">${esc(e.record_id)}</span>`
          : "";
        return `<div class="record draft-record">
          <div class="head">
            <span class="id draft-pid">${esc(e.draft_id)}</span>${mapped}
            <span class="badge ${esc(e.kind)}">${e.kind === "raw" ? "原始草案" : "推导草案"}</span>
          </div>
          <div class="meta">${esc(text)}</div>${basis}
        </div>`;
      }).join("")
    : '<p class="muted">分支内尚无草案。</p>';
  $("#branch-detail").innerHTML = `
    <div class="branch-head">
      <span class="id">${esc(b.id)}</span>
      <span class="badge ${b.status === "draft" ? "valid" : "raw"}">${esc(branchStatusText(b.status))}</span>
      <span class="muted">创建于 ${esc(b.created_at)}</span>
    </div>
    <details class="snapshot-box" ${b.status === "draft" ? "" : "open"}>
      <summary>分支快照（${b.snapshot.length} 条创建分支时冻结的有效正式记录，悬停查看其当时直接依据）</summary>
      <div class="parents">${snapList}</div>
    </details>
    <div class="entry-list">${entryList}</div>`;
  const draftMode = b.status === "draft";
  $("#draft-form").style.display = draftMode ? "" : "none";
  $("#publish-form").style.display = draftMode ? "" : "none";
}

function renderDraftParentOptions() {
  const b = state.currentBranch;
  const box = $("#draft-parents");
  if (!b) { box.innerHTML = ""; return; }
  const opts = [
    ...b.snapshot.map((s) => ({
      ref: s.record_id, cls: "pid", tag: "快照",
      basis: s.direct_basis.join("、") || "原始记录",
    })),
    ...b.entries.map((e) => ({
      ref: e.draft_id, cls: "draft-pid", tag: "本分支草案",
      basis: (e.parent_refs.join("、") || "原始草案"),
    })),
  ];
  box.innerHTML = opts.length ? opts.map((o) => `
    <label><input type="checkbox" value="${esc(o.ref)}">
      <span class="${o.cls}">${esc(o.ref)}</span>
      <span class="muted">（${o.tag}，依据：${esc(o.basis)}）</span>
    </label>`).join("")
    : '<span class="muted">快照为空且分支内尚无草案可引用</span>';
}

$("#branch-create-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#new-branch-id").value.trim();
  try {
    const body = v ? { branch_id: v } : {};
    const b = await api("POST", "/api/branches", body);
    branchFeedback(`已创建分支 ${b.id}，冻结 ${b.snapshot.length} 条有效记录快照`, "ok-text");
    $("#new-branch-id").value = "";
    await refreshBranches(b.id);
  } catch (err) {
    showBranchError(err);
  }
});

$("#branch-select").addEventListener("change", async () => {
  state.currentBranchId = $("#branch-select").value || null;
  try {
    if (state.currentBranchId) await loadBranch(state.currentBranchId);
  } catch (err) {
    showBranchError(err);
  }
});

$("#draft-kind").addEventListener("change", () => {
  $("#draft-parents-row").hidden = $("#draft-kind").value !== "derived";
});

$("#draft-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = state.currentBranch;
  if (!b) return;
  const body = {
    kind: $("#draft-kind").value,
    payload: { value: $("#draft-value").value || "" },
  };
  if (body.kind === "derived") {
    body.parent_refs = [...document.querySelectorAll("#draft-parents input:checked")]
      .map((c) => c.value);
  }
  try {
    const en = await api("POST",
      `/api/branches/${encodeURIComponent(b.id)}/entries`, body);
    branchFeedback(`草案 ${en.draft_id} 已加入分支 ${b.id}（尚未进入正式谱系）；` +
      `依据：${en.parent_refs.length ? en.parent_refs.join("、") : "无"}`, "ok-text");
    $("#draft-value").value = "";
    await loadBranch(b.id);
    renderDraftParentOptions();
    await refreshBranches(b.id);
  } catch (err) {
    showBranchError(err);
  }
});

$("#publish-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const b = state.currentBranch;
  const op = $("#publish-op").value.trim();
  if (!b) return;
  try {
    const res = await api("POST",
      `/api/branches/${encodeURIComponent(b.id)}/publish`,
      { operation_id: op });
    branchFeedback(
      `整组发布${res.replayed ? "（重传，返回首次编号映射）" : ""}成功：\n` +
      res.mapping.map((m) => `${m.draft_id} → ${m.record_id}`).join("\n"),
      "ok-text");
    await Promise.all([refresh(), refreshBranches(b.id)]);
  } catch (err) {
    showBranchError(err);
    await Promise.all([
      refresh().catch(() => {}),
      refreshBranches(b.id).catch(() => {}),
    ]);
  }
});

$("#branch-refresh").addEventListener("click", () =>
  refreshBranches().catch(showBranchError));

function showBranchError(err) {
  branchFeedback(`操作被拒绝（${err.code || err.status}）：${err.message}\n` +
    (err.details ? `定位信息：${JSON.stringify(err.details, null, 2)}` : ""),
    "error-text");
}

refresh().catch((e) => feedback(e.message, "error-text"));
refreshBranches().catch((e) => branchFeedback(e.message, "error-text"));
