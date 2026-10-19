/* =====================================================================
   viewer/app.js —— 招聘工作台（岗位中心）

   流程：录入岗位要求 → LLM 结构化 → 上传候选人素材 → 实时辩论 → 结果
   设计底线：页面只展示真实评审结果，不内置任何演示数据。
   ===================================================================== */

/* ===================== 状态 ===================== */
const state = {
  jobs: [],
  job: null,            // 当前岗位
  candidates: [],       // 当前岗位的候选人
  editingJobId: null,   // null = 新建
  jdFiles: [],          // 岗位弹窗里待结构化/上传的文件（base64）
  upFiles: [],
  liveEvents: [],       // 直播间事件
  follow: true,         // 直播间自动滚动
  running: false,
  talentGrade: "",      // 人才库等级筛选
  user: null,           // 当前登录用户
  me: null,             // /api/auth/me 的完整信息（含权限）
  pendingIdentity: [],  // 待人工确认的"疑似同一人"
  identityTarget: null, // 正在确认的那个候选人
  outcomeTarget: null,  // 正在回填结果的 person
  currentRunId: null,
  streamClose: null,
  talentPerson: null,   // 抽屉里当前的人
  users: [],            // 用户管理列表（重置口令时按 id 反查，不拼进 HTML 属性）
  calibSets: [],        // 标定集列表（添加条目时选集合用）
  resetPwTarget: null,  // 正在重置口令的用户
  candEditTarget: null, // 正在编辑姓名的候选人
};

const DIMENSIONS = ["岗位能力匹配度", "风险与软素质", "硬实力门槛"];
const DEFAULT_WEIGHTS = { "岗位能力匹配度": 0.4, "风险与软素质": 0.3, "硬实力门槛": 0.3 };

const ROLE_META = {
  pro:         { name: "正方", cls: "pro",   icon: "◆" },
  con:         { name: "反方", cls: "con",   icon: "◆" },
  adjudicator: { name: "裁定裁判", cls: "adj", icon: "§" },
  judge:       { name: "评委", cls: "judge", icon: "★" },
  system:      { name: "系统", cls: "sys",   icon: "•" },
};

function $(id) { return document.getElementById(id); }

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function can(action) {
  const perms = (state.me && state.me.permissions) || [];
  return perms.indexOf("*") >= 0 || perms.indexOf(action) >= 0;
}

/* ===================== 启动 ===================== */
document.addEventListener("DOMContentLoaded", async () => {
  bootStarfield();
  initLiveFeedFollow();
  initLiveBox();

  // 会话过期时统一回到登录页（api.js 里任何 401 都会触发）
  window.addEventListener("auth:expired", () => showAuthGate(false));

  const ok = await bootAuth();
  if (!ok) return;

  bootIntro();
  loadJobs();
  refreshPendingIdentity();
});

/**
 * 认证闸门。
 * 返回 true = 已登录且可以继续；false = 停在登录/初始化界面。
 */
async function bootAuth() {
  let boot = null;
  try {
    boot = await API.bootstrap();
  } catch (e) {
    showAuthGate(false);
    $("loginMsg").textContent = "无法连接后端：" + e.message;
    return false;
  }

  if (boot.needs_setup) {
    showAuthGate(true);
    return false;
  }
  if (!boot.user) {
    showAuthGate(false);
    return false;
  }
  state.user = boot.user;
  await afterLogin();
  return true;
}

function showAuthGate(isSetup) {
  const gate = $("authGate");
  if (!gate) return;
  gate.classList.remove("hidden");
  gate.classList.add("show");
  $("authSetup").classList.toggle("hidden", !isSetup);
  $("authLogin").classList.toggle("hidden", !!isSetup);
  // 登录态失效时隐藏主界面，避免看到空白/报错页
  const app = $("app"), hd = document.querySelector("header");
  if (app) { app.classList.add("hidden"); app.classList.remove("pre-intro"); }
  if (hd) hd.classList.add("hidden");
  const intro = $("intro");
  if (intro) intro.classList.add("hidden");

  if (isSetup) {
    setTimeout(() => { const e = $("setupPass"); if (e) e.focus(); }, 80);
  } else {
    setTimeout(() => { const e = $("loginUser"); if (e) e.focus(); }, 80);
  }
}

function hideAuthGate() {
  const gate = $("authGate");
  if (gate) { gate.classList.add("hidden"); gate.classList.remove("show"); }
  const app = $("app"), hd = document.querySelector("header");
  if (app) app.classList.remove("hidden");
  if (hd) hd.classList.remove("hidden");
}

function renderPwMeter(inputId, meterId, hintId) {
  const pw = ($(inputId) || {}).value || "";
  const s = API.passwordStrength(pw);
  const m = $(meterId);
  if (m) {
    m.style.width = Math.min(100, s.score * 20) + "%";
    m.className = s.score <= 1 ? "bad" : s.score <= 2 ? "mid" : "good";
  }
  const h = $(hintId);
  if (h) h.textContent = pw ? `强度：${s.label}` + (s.ok ? "" : "（至少 8 位）") : "";
}

function renderSetupMeter() { renderPwMeter("setupPass", "setupMeter", "setupHint"); }
document.addEventListener("input", (e) => {
  if (e.target && e.target.id === "setupPass") renderSetupMeter();
});

async function doSetup() {
  const btn = $("setupBtn"), msg = $("setupMsg");
  const u = ($("setupUser").value || "").trim();
  const p = $("setupPass").value || "";
  const p2 = $("setupPass2").value || "";
  if (!u) { msg.textContent = "请填写账号"; return; }
  if (p.length < 8) { msg.textContent = "口令至少 8 位"; return; }
  if (p !== p2) { msg.textContent = "两次输入的口令不一致"; return; }

  btn.disabled = true; msg.textContent = "创建中…";
  try {
    await API.setup({ username: u, password: p, display_name: ($("setupName").value || "").trim() });
    msg.textContent = "";
    await refreshMe();
    hideAuthGate();
    bootIntro();
    loadJobs();
    refreshPendingIdentity();
    toast("管理员已创建，欢迎使用", "ok");
  } catch (e) {
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function doLogin() {
  const btn = $("loginBtn"), msg = $("loginMsg");
  const u = ($("loginUser").value || "").trim();
  const p = $("loginPass").value || "";
  if (!u || !p) { msg.textContent = "请填写账号和口令"; return; }
  btn.disabled = true; msg.textContent = "登录中…";
  try {
    await API.login(u, p);
    msg.textContent = "";
    $("loginPass").value = "";
    await refreshMe();
    hideAuthGate();
    bootIntro();
    loadJobs();
    refreshPendingIdentity();
  } catch (e) {
    msg.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function refreshMe() {
  try {
    const r = await API.me();
    state.me = r.user;
    state.user = r.user;
  } catch (e) {
    state.me = null;
    state.user = null;
    return;
  }
  const isAdmin = (state.me && state.me.role === "admin");
  document.querySelectorAll(".admin-only").forEach((el) => el.classList.toggle("hidden", !isAdmin));
  const ub = $("userBadgeText");
  if (ub) {
    const label = { admin: "管理员", hr: "招聘", viewer: "只读" }[state.me.role] || state.me.role;
    ub.textContent = `${state.me.display_name || state.me.username} · ${label}`;
  }
}

async function afterLogin() {
  await refreshMe();
  hideAuthGate();
  renderModelBadge();
  // 进主界面就先自检一次（不联网，只读本地状态）
  loadSelfcheck(false);
}

async function doLogout() {
  try { await API.logout(); } catch (e) { /* 忽略：本地照样清 */ }
  state.me = null; state.user = null;
  closeUserMenu();
  showAuthGate(false);
}

function openUserMenu() {
  if (!state.me) return;
  const role = { admin: "管理员", hr: "招聘", viewer: "只读" }[state.me.role] || state.me.role;
  $("userInfo").innerHTML = `
    账号：<b>${esc(state.me.username)}</b><br>
    显示名：${esc(state.me.display_name || "—")}<br>
    角色：${esc(role)}<br>
    <span class="muted">权限：${(state.me.permissions || []).map(esc).join("、")}</span>`;
  $("pwOld").value = ""; $("pwNew").value = ""; $("pwMsg").textContent = "";
  $("userModal").classList.remove("hidden");
}

function closeUserMenu() { $("userModal").classList.add("hidden"); }

async function doChangePassword() {
  const msg = $("pwMsg");
  const o = $("pwOld").value || "", n = $("pwNew").value || "";
  if (n.length < 8) { msg.textContent = "新口令至少 8 位"; return; }
  msg.textContent = "提交中…";
  try {
    await API.changePassword(o, n);
    msg.textContent = "口令已更新，请重新登录";
    setTimeout(() => { closeUserMenu(); showAuthGate(false); }, 900);
  } catch (e) {
    msg.textContent = e.message;
  }
}

function openNewUser() {
  $("nuUser").value = ""; $("nuName").value = ""; $("nuPass").value = "";
  $("nuRole").value = "hr"; $("nuMsg").textContent = "";
  $("newUserModal").classList.remove("hidden");
}
function closeNewUser() { $("newUserModal").classList.add("hidden"); }

async function doCreateUser() {
  const msg = $("nuMsg");
  msg.textContent = "创建中…";
  try {
    await API.createUser({
      username: ($("nuUser").value || "").trim(),
      display_name: ($("nuName").value || "").trim(),
      role: $("nuRole").value,
      password: $("nuPass").value || "",
    });
    msg.textContent = "";
    closeNewUser();
    loadUsers();
    toast("用户已创建", "ok");
  } catch (e) {
    msg.textContent = e.message;
  }
}

async function loadJobs() {
  try {
    const r = await API.jobs();
    state.jobs = r.jobs || [];
    renderJobs();
    renderStats();
    renderModelBadge();
  } catch (e) {
    toast("加载岗位失败：" + e.message, "err");
  }
}

/** 首页概览：把「现在招得怎么样」一眼说清楚 */
function renderStats() {
  const js = state.jobs || [];
  const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  const cands = js.reduce((n, j) => n + (j.candidate_count || 0), 0);
  const scored = js.reduce((n, j) => n + (j.scored_count || 0), 0);
  set("statJobs", js.length);
  set("statCands", cands);
  set("statScored", scored);
  set("statGrade", js.length ? `${scored}/${cands}` : "—");
}

const PROVIDER_LABEL = {
  workbuddy: "WorkBuddy", deepseek: "DeepSeek", qwen: "通义",
  ollama: "Ollama", "": "自定义",
};

/**
 * 顶栏徽章：只显示**当前在用哪个模型**。
 * 不显示账号（那属于个人隐私信息，与"我在用什么模型"无关）。
 * 顺序：浏览器本地保存的配置 > 后端内存里的配置 > 该预设的默认模型。
 */
async function renderModelBadge() {
  const el = $("modelBadgeText");
  if (!el) return;
  const local = JSON.parse(localStorage.getItem("talent_screen.model_cfg") || "{}");
  let provider = local.provider || "";
  let model = local.model || "";

  try {
    const r = await API.modelConfig();
    const cfg = (r && r.config) || {};
    if (!provider) provider = cfg.provider || "";
    if (!model) model = cfg.model || "";
  } catch (e) { /* 后端没起来时退回本地配置 */ }

  if (!provider) provider = "workbuddy";
  if (!model && PROVIDER_DEFAULTS[provider]) model = PROVIDER_DEFAULTS[provider].model;

  const prov = PROVIDER_LABEL[provider] || provider;
  el.textContent = model ? `${prov} · ${model}` : `${prov} · 未选模型`;
  const badge = $("modelBadge");
  if (badge) badge.classList.toggle("warn", !model);
}

/* ===================== 一、岗位列表 ===================== */
const JOB_STATUS_TEXT = {
  draft: "草稿", collecting: "收集素材中", screening: "评审中", done: "已完成",
};

function renderJobs() {
  const g = $("jobGrid");
  const cnt = $("jobsCount");
  if (cnt) cnt.textContent = state.jobs.length ? `共 ${state.jobs.length} 个` : "";
  if (!state.jobs.length) {
    g.innerHTML = `<div class="hint jobs-empty">还没有岗位。在上面粘贴招聘要求，或点「手工新建岗位」。</div>`;
    return;
  }
  g.innerHTML = state.jobs.map(j => `
    <div class="job-card" data-job-id="${esc(j.id)}">
      <div class="job-card-top">
        <span class="job-card-title">${esc(j.title)}</span>
        <span class="pill st-${esc(j.status)}">${esc(JOB_STATUS_TEXT[j.status] || j.status)}</span>
      </div>
      <div class="job-card-meta">
        <span>硬性要求 ${esc(j.requirement_count)} 条</span>
        <span>候选人 ${esc(j.candidate_count)}</span>
        <span>已评 ${esc(j.scored_count)}</span>
      </div>
      <div class="job-card-time">${esc((j.created_at || "").replace("T", " "))}</div>
    </div>`).join("");
}

// 岗位卡片用事件委托，而不是把 id 拼进 onclick 属性。
// 拼接 onclick 字符串时，id 里只要出现单引号就能跳出属性值执行任意代码；
// 虽然 id 由服务端生成、"看起来"安全，但这类"看起来安全"正是最容易被
// 后续改动打破的地方（比如某天允许自定义岗位短名）。
document.addEventListener("click", (e) => {
  const card = e.target.closest && e.target.closest(".job-card[data-job-id]");
  if (card) openJob(card.getAttribute("data-job-id"));
});

async function openJob(id) {
  const r = await API.job(id);
  if (!r.ok) { toast(r.error || "岗位不存在", "err"); return; }
  state.job = r.job;
  state.candidates = r.candidates || [];
  $("viewHome").classList.add("hidden");
  $("viewJob").classList.remove("hidden");

  // 有运行中的评审就自动接上（刷新页面 / 换标签页都能续上进度），
  // 否则用户会以为"评审停了"，反复点开始。
  const active = r.active_run;
  if (active) {
    state.currentRunId = active.id;
    if (!state.running) {
      state.running = true;
      $("liveCard").classList.remove("hidden");
      $("liveFeed").innerHTML = "";
      $("liveProgress").textContent = "接续进行中的评审…";
      API.runEvents(active.id, 0).then((past) => {
        (past.events || []).forEach(onLiveEvent);
      }).catch(() => {});
      if (state.streamClose) { try { state.streamClose(); } catch (e) {} }
      state.streamClose = API.streamRun(active.id, onLiveEvent, () => {
        state.running = false;
        state.streamClose = null;
        $("liveProgress").textContent = "已结束";
        state.currentRunId = null;
        openJob(id).then(() => refreshPendingIdentity());
      });
    }
  } else {
    state.running = false;
    state.currentRunId = null;
    if (!state.liveEvents.length) $("liveCard").classList.add("hidden");
  }

  renderJob();
  if (state.candidates.some(c => c.person_decision === "pending")) refreshPendingIdentity();
  window.scrollTo(0, 0);
}

function goHome() {
  if (state.running && !confirm("评审正在进行。离开页面不会中断评审（后台会继续跑），确定返回？")) return;
  state.job = null;
  $("viewJob").classList.add("hidden");
  $("viewHome").classList.remove("hidden");
  loadJobs();
  window.scrollTo(0, 0);
}

/* ===================== 视图切换 ===================== */
function switchView(which) {
  const isTalent = which === "talent";
  const isAnalytics = which === "analytics";
  const isAdmin = which === "admin";

  $("viewTalent").classList.toggle("hidden", !isTalent);
  $("viewAnalytics").classList.toggle("hidden", !isAnalytics);
  $("viewAdmin").classList.toggle("hidden", !isAdmin);
  // 岗位视图的显示与否由「是否打开了某个岗位」决定
  if (isAnalytics || isAdmin) {
    $("viewHome").classList.add("hidden");
    $("viewJob").classList.add("hidden");
  } else {
    $("viewHome").classList.toggle("hidden", isTalent || !!state.job);
    $("viewJob").classList.toggle("hidden", isTalent || !state.job);
  }

  $("navJobs").classList.toggle("on", !isTalent && !isAnalytics && !isAdmin);
  $("navTalent").classList.toggle("on", isTalent);
  const an = $("navAnalytics"); if (an) an.classList.toggle("on", isAnalytics);
  const ad = $("navAdmin"); if (ad) ad.classList.toggle("on", isAdmin);

  if (isTalent) loadTalent();
  if (isAnalytics) { loadHitRate(); loadDrift(); loadCalibration(); loadUsage(); }
  if (isAdmin) { loadUsers(); loadPrivacy(); loadAudit(); }
  window.scrollTo(0, 0);
}

/* ===================== 人才库（跨岗位） ===================== */
const TALENT_STATUSES = [
  ["new", "新入库"], ["screening", "评审中"], ["passed", "通过筛选"],
  ["hold", "待定"], ["rejected", "已淘汰"], ["hired", "已入职"], ["archived", "已归档"],
];
const GRADE_CLS = { A: "gA", B: "gB", C: "gC" };
let talentTimer = null, noteTimer = null;
let talentRows = [];
let talentCurrent = null;

function debouncedTalentSearch() {
  clearTimeout(talentTimer);
  talentTimer = setTimeout(loadTalent, 250);
}

function setTalentGrade(g) {
  state.talentGrade = g;
  document.querySelectorAll("#talentGradeSeg .segbtn").forEach(b =>
    b.classList.toggle("on", (b.dataset.g || "") === g));
  loadTalent();
}

async function loadTalent() {
  const sel = $("talentStatus");
  if (sel && !sel.options.length) {
    sel.innerHTML = `<option value="">全部状态</option>` +
      TALENT_STATUSES.map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  }
  const grid = $("talentGrid");
  grid.innerHTML = `<div class="hint">加载中…</div>`;
  try {
    const r = await API.talent({
      q: ($("talentSearch") || {}).value || "",
      grade: state.talentGrade || "",
      status: (sel || {}).value || "",
    });
    talentRows = r.people || [];
    renderTalentStats(r.stats || {});
    renderTalentGrid();
  } catch (e) {
    grid.innerHTML = `<div class="hint warn">加载失败：${esc(e.message)}</div>`;
  }
}

function renderTalentStats(s) {
  const box = $("talentStats");
  if (!box) return;
  const g = s.grades || {};
  const cards = [
    [s.total || 0, "库里总人数"],
    [s.evaluated || 0, "已出结论"],
    [s.multi_job || 0, "面过多个岗位"],
    [`${g.A || 0}/${g.B || 0}/${g.C || 0}`, "A/B/C 分布"],
  ];
  box.innerHTML = cards.map(([v, l]) =>
    `<div class="stat"><div class="sv">${v}</div><div class="sl">${l}</div></div>`).join("");
}

function renderTalentGrid() {
  const grid = $("talentGrid");
  $("talentCount").textContent = talentRows.length ? `共 ${talentRows.length} 人` : "";
  if (!talentRows.length) {
    grid.innerHTML = `<div class="jobs-empty">人才库还是空的。上传候选人并跑一次评审，这里就会有人了。</div>`;
    return;
  }
  grid.innerHTML = talentRows.map(p => {
    const b = p.best;
    const score = b
      ? `<span class="t-score ${GRADE_CLS[b.grade] || ""}">${b.total}<em>${b.grade}</em></span>`
      : `<span class="t-score none">未评</span>`;
    const tags = (p.tags || []).slice(0, 3).map(t => `<span class="tag">${esc(t)}</span>`).join("");
    const jobs = (p.jobs || []).map(j => `<span class="t-job">${esc(j)}</span>`).join("");
    return `<div class="talent-card" onclick="openTalent('${p.id}')">
      <div class="t-top"><span class="t-name">${esc(p.name)}</span>${score}</div>
      <div class="t-meta">
        <span class="t-status st-${p.status}">${esc(p.status_label)}</span>
        ${p.job_count > 1 ? `<span class="t-multi">面过 ${p.job_count} 个岗位</span>` : ""}
      </div>
      <div class="t-jobs">${jobs}</div>
      ${tags ? `<div class="t-tags">${tags}</div>` : ""}
      ${p.note ? `<div class="t-note">${esc(p.note)}</div>` : ""}
    </div>`;
  }).join("");
}

async function openTalent(tid) {
  try {
    const r = await API.talentPerson(tid);
    if (!r.ok) throw new Error(r.error || "读取失败");
    const p = r.person;
    talentCurrent = p;
    state.talentPerson = p;
    $("tdName").textContent = p.name;
    $("tdSub").textContent = `面过 ${p.job_count} 个岗位 · ${p.eval_count} 次评审`
      + (p.best ? ` · 最高 ${p.best.total}（${p.best.grade}）` : "");

    // 真实进展：这是"评分到底准不准"的唯一数据来源，必须显眼
    const stageLabel = (r.stages || []).reduce((m, s) => (m[s.id] = s.label, m), {});
    const st = $("tdStage");
    if (st) {
      const cur = p.latest_stage;
      st.textContent = cur ? (stageLabel[cur] || cur) : "尚未回填";
      st.className = "badge" + (cur ? "" : " warn");
    }

    $("tdStatus").innerHTML = TALENT_STATUSES.map(([k, v]) =>
      `<option value="${k}"${k === p.status ? " selected" : ""}>${v}</option>`).join("");
    $("tdNote").value = p.note || "";
    $("tdImportMsg").textContent = "";
    const inJobs = new Set((p.evaluations || []).map(e => e.job_id));
    const cands = (state.jobs || []).filter(j => !inJobs.has(j.id));
    $("tdJobSel").innerHTML = cands.length
      ? cands.map(j => `<option value="${esc(j.id)}">${esc(j.title)}</option>`).join("")
      : `<option value="">（没有可加入的岗位）</option>`;

    const hist = (p.evaluations || []).length
      ? p.evaluations.map(e => {
          const dims = DIMENSIONS.map(d =>
            `${esc(d)} <b>${(e.scores || {})[d] ?? "—"}</b>`).join(" · ");
          return `<div class="td-eval">
            <div class="td-eval-top">
              <span class="td-job">${esc(e.job_title)}</span>
              <span class="t-score ${GRADE_CLS[e.grade] || ""}">${e.total}<em>${e.grade}</em></span>
            </div>
            <div class="td-eval-dim">${dims}</div>
            <div class="td-eval-meta">${esc(e.date || "")}
              ${e.rubric_version ? `<code>${esc(e.rubric_version)}</code>` : ""}
              ${(e.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("")}</div>
          </div>`;
        }).join("")
      : `<div class="hint">这个人还没有评审记录。</div>`;

    // 进展时间线
    // 每条都能删：回填是"评分准不准"的唯一数据来源，记错一条不改，
    // 命中率就被污染了。删除权跟记录权一致（outcome.write）。
    const canDelOutcome = can("outcome.write");
    const outs = (p.outcomes || []).length
      ? `<div class="td-outcomes">${p.outcomes.map(o =>
          `<div class="td-outcome"><span class="muted">${esc((o.reached_at || "").slice(0, 10))}</span>
             <b>${esc(stageLabel[o.stage] || o.stage)}</b>
             ${o.note ? `<span class="muted">${esc(o.note)}</span>` : ""}${
               canDelOutcome
                 ? `<button class="btn tiny danger-ghost" title="删掉这条记错的进展"
                      onclick="deleteOutcomeRow('${esc(o.id)}')">✕</button>` : ""}</div>`).join("")}</div>`
      : `<div class="hint">还没有回填真实进展。不回填，就无法验证评分是否有效。</div>`;

    $("tdHistory").innerHTML = hist + `<div class="field" style="margin-top:14px">
      <label>真实进展</label>${outs}</div>`;
    // 数据权利按钮按权限显示：viewer 只读；hr 与 admin 有 talent.write /
    // settings.write。按钮隐藏比点了再吃 403 好——404 的按钮等于没有。
    $("btnDeletePerson").classList.toggle("hidden", !can("talent.write"));
    $("btnExportPerson").classList.toggle("hidden", !can("settings.write"));
    $("btnErasePerson").classList.toggle("hidden", !can("settings.write"));
    $("talentDrawer").classList.remove("hidden");
  } catch (e) {
    toast("打开失败：" + e.message, "err");
  }
}

function closeTalent() { $("talentDrawer").classList.add("hidden"); }

async function saveTalentMeta() {
  if (!talentCurrent) return;
  try {
    await API.updateTalent(talentCurrent.id, {
      status: $("tdStatus").value,
      note: $("tdNote").value,
    });
    toast("已保存", "ok");
    loadTalent();
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}

function debouncedSaveNote() {
  clearTimeout(noteTimer);
  noteTimer = setTimeout(saveTalentMeta, 700);
}

/* ---- 人才档案的数据权利（PIPL 第 15/37/45 条）----
   导出 = 查阅权；擦除 = 删除权；删档案 = 人才库自己的清理。
   三者的破坏力不同，措辞和确认次数也不同：导出可重来，删档案可重建，
   擦除不可逆——所以只有擦除要求二次确认（确认框本身算一次，
   这里再要求输入人名，手滑点不中）。 */

async function exportPersonData() {
  const p = state.talentPerson;
  if (!p) return;
  if (!confirm(`导出「${p.name}」的全部数据？\n\n` +
      "导出物包含明文个人信息（姓名、联系方式、材料全文），" +
      "请按内部数据管理规定保存，勿随意转发。")) return;
  try {
    const r = await API.exportPerson(p.id);
    const blob = new Blob([JSON.stringify(r.export || r, null, 2)],
                          { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "person-" + p.id + "-export.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    toast("已导出（含明文个人信息，注意保存）", "ok");
  } catch (e) { toast("导出失败：" + e.message, "err"); }
}

async function erasePersonData() {
  const p = state.talentPerson;
  if (!p) return;
  const typed = prompt(`擦除「${p.name}」的全部数据？\n\n` +
      "这会删除此人的材料、档案与评审关联，且不可恢复。\n" +
      `如确认，请输入此人姓名「${p.name}」：`);
  if (typed === null) return;
  if (typed.trim() !== p.name) { toast("姓名不匹配，已取消", "info"); return; }
  try {
    await API.erasePerson(p.id);
    closeTalent();
    toast("已擦除", "ok");
    loadTalent();
  } catch (e) { toast("擦除失败：" + e.message, "err"); }
}

/* 删除档案：档案没了，各岗位内的历史评审记录保留——
   评审记录属于岗位的历史，删档案不该篡改已经发生的事。 */
async function deleteTalentPerson() {
  const p = state.talentPerson;
  if (!p) return;
  if (!confirm(`从人才库删除「${p.name}」的档案？\n\n` +
      "各岗位内的历史评审记录会保留，但此人不再出现在人才库搜索里。")) return;
  try {
    await API.deleteTalent(p.id);
    closeTalent();
    toast("已删除档案", "ok");
    loadTalent();
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

/* 删掉一条记错的进展。 */
async function deleteOutcomeRow(oid) {
  if (!oid) return;
  if (!confirm("删除这条进展记录？\n\n它会参与命中率计算，删错会影响统计。")) return;
  try {
    await API.deleteOutcome(oid);
    toast("已删除", "ok");
    if (state.talentPerson) openTalent(state.talentPerson.id);
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

async function importToJob() {
  if (!talentCurrent) return;
  const jobId = $("tdJobSel").value;
  const msg = $("tdImportMsg");
  if (!jobId) { msg.className = "wb-status bad"; msg.textContent = "没有可加入的岗位。"; return; }
  msg.className = "wb-status";
  msg.textContent = "⏳ 正在导入…";
  try {
    const r = await API.importTalent(talentCurrent.id, jobId);
    if (!r.ok) throw new Error(r.message || r.error || "导入失败");
    msg.className = "wb-status good";
    msg.textContent = "✓ " + r.message + "（材料已复用，去岗位里跑评审即可）";
    await loadJobs();
  } catch (e) {
    msg.className = "wb-status bad";
    msg.textContent = "✗ " + e.message;
  }
}

/* ===================== 岗位模板 =====================
   一开始就让人对着空文本框写 JD 是最劝退的一步。
   给几个常见岗位的模板，点一下填进去，改两个字就能用。 */
const JD_TEMPLATES = {
  "高级后端工程师": `高级后端工程师

【硬性要求】
1. 8年以上后端开发经验
2. 精通 Go 或 Java 之一，具备分布式系统经验
3. 有高并发/高可用系统设计与落地经验
4. 本科及以上学历，计算机相关专业

【加分项】
- 有亿级流量系统重构经验
- 持有系统架构设计师证书
- 带过技术小组

【风险红线】
- 近两年频繁跳槽（≥2次）
- 简历有明显断层`,

  "前端工程师": `前端工程师

【硬性要求】
1. 3年以上前端开发经验
2. 精通 JavaScript / TypeScript，熟悉 React 或 Vue
3. 有中大型项目性能优化经验
4. 本科及以上学历

【加分项】
- 有组件库或工程化体系搭建经验
- 熟悉 Node.js 全栈开发
- 有开源项目贡献

【风险红线】
- 项目经历写不清自己具体做了什么
- 频繁更换技术栈且无沉淀`,

  "产品经理": `产品经理

【硬性要求】
1. 5年以上产品经验，独立负责过完整产品线
2. 具备需求分析、原型设计与数据分析能力
3. 有跨部门协作推动落地的经验
4. 本科及以上学历

【加分项】
- 有从0到1的产品经历
- 熟悉 SQL / 数据看板等分析工具
- 有 To B 或 SaaS 领域经验

【风险红线】
- 只会执行、无独立判断的案例
- 经历断层或长期空窗
- 频繁换方向（1年内多次转岗）`,

  "数据分析师": `数据分析师

【硬性要求】
1. 3年以上数据分析经验
2. 精通 SQL，熟悉 Python 或 R 做数据处理
3. 具备指标体系搭建与业务归因能力
4. 本科及以上学历，统计/数学/计算机相关专业

【加分项】
- 有 A/B 实验设计与评估经验
- 熟悉 BI 工具（Tableau / Superset 等）
- 有机器学习建模落地经验

【风险红线】
- 只会取数、不会解释业务含义
- 简历中的数字成果无法对应到具体分析动作`,
};

function renderJdTemplates() {
  const box = $("jdTemplates");
  if (!box) return;
  box.innerHTML = Object.keys(JD_TEMPLATES).map(k =>
    `<button class="tpl" type="button" onclick="useTemplate('${k}')">${esc(k)}</button>`).join("");
}

function useTemplate(name) {
  const tpl = JD_TEMPLATES[name];
  if (!tpl) return;
  const raw = $("jdRaw");
  if (raw.value.trim() && !confirm("当前已填写内容，用模板覆盖？")) return;
  raw.value = tpl;
  if (!$("jdTitle").value.trim()) $("jdTitle").value = name;
  raw.focus();
}

function renderJob() {
  const j = state.job;
  if (!j) return;
  $("jobTitle").textContent = j.title || "未命名岗位";
  const jd = j.jd || {};
  $("jobSub").textContent = jd.summary || "尚未填写岗位概述";
  const sb = $("jobStatusBadge");
  sb.textContent = JOB_STATUS_TEXT[j.status] || j.status;
  sb.className = "badge st-" + j.status;

  renderJdCard(jd);
  renderCandidates();
  renderRunHint();
  renderResults();
  loadAlign();
}

/* ===================== 评审细则编辑 ===================== */
let rbState = { presets: [], config: null, canEdit: true };

async function openRubricEditor() {
  if (!state.job) return;
  $("rbMsg").textContent = "";
  $("rubricModal").classList.remove("hidden");
  $("rbPreset").innerHTML = `<option value="">加载中…</option>`;
  try {
    const r = await API.jobRubric(state.job.id);
    rbState.presets = r.presets || [];
    rbState.config = r.config || {};
    rbState.canEdit = r.can_edit !== false;
    renderRubricEditor(r);
  } catch (e) {
    $("rbMsg").textContent = e.message || "读取细则失败";
  }
}

function renderRubricEditor(r) {
  const cfg = r.config || {};
  $("rbPreset").innerHTML = `<option value="">— 选择岗位类型 —</option>` +
    (r.presets || []).map(p =>
      `<option value="${esc(p.key)}"${p.key === cfg.preset ? " selected" : ""}>
        ${esc(p.label)}</option>`).join("");
  const cur = (r.presets || []).find(p => p.key === cfg.preset);
  $("rbPresetWhy").textContent = cur ? (cur.desc || "") + (cur.why ? " " + cur.why : "") : "";

  $("rbWeights").innerHTML = DIMENSIONS.map(d => `
    <div class="rb-wrow">
      <span class="rb-wlabel">${esc(d)}</span>
      <input type="range" min="1" max="100" step="1" data-rb-w="${esc(d)}"
             value="${Math.round((cfg.weights || {})[d] * 100) || 1}"
             oninput="onWeightInput()">
      <input type="number" min="1" max="100" step="1" class="rb-wnum" data-rb-wn="${esc(d)}"
             value="${Math.round((cfg.weights || {})[d] * 100) || 1}"
             oninput="onWeightNum('${esc(d)}')">
      <span class="rb-pct">%</span>
    </div>`).join("");

  $("rbCriteria").innerHTML = DIMENSIONS.map(d => `
    <div class="rb-crow">
      <div class="rb-clabel">${esc(d)}</div>
      <textarea rows="3" maxlength="800" data-rb-c="${esc(d)}"
                placeholder="例如：只看与主线职责直接相关的深度，框架堆砌不算能力"
      >${esc((cfg.criteria || {})[d] || "")}</textarea>
    </div>`).join("");

  $("rbNotes").value = cfg.notes || "";
  $("rbSaveBtn").disabled = !rbState.canEdit;
  const blk = $("rubricBlocked");
  if (!rbState.canEdit) {
    blk.classList.remove("hidden");
    blk.innerHTML = `<b>现在不能改</b><p>${esc(r.blocked_reason || "")}</p>`;
  } else {
    blk.classList.add("hidden");
  }
  const stale = r.scored_with_other_version || [];
  $("rbImpact").innerHTML =
    `当前口径版本 <code>${esc(r.version || "—")}</code>` +
    (stale.length
      ? `<br><span class="warn-text">该岗位已有 ${stale.reduce((a, x) => a + x.count, 0)} 份结果
         是用别的口径评的（${stale.map(x => esc(x.version)).join("、")}）。
         改细则后新旧结果不可直接比较，需要重跑才能对齐。</span>`
      : "");
  onWeightInput();
}

function readRubricWeights() {
  const out = {};
  DIMENSIONS.forEach(d => {
    const n = document.querySelector(`[data-rb-wn="${cssEsc(d)}"]`);
    out[d] = Math.max(1, Math.min(100, Number(n && n.value) || 1));
  });
  return out;
}

function onWeightInput() {
  // 滑块和数字框双向同步，并把"归一化后实际占比"实时算给用户看——
  // 输 40/30/30 和输 4/3/3 是同一把尺子，界面上不应该让人以为它们不同。
  const w = readRubricWeights();
  DIMENSIONS.forEach(d => {
    const n = document.querySelector(`[data-rb-wn="${cssEsc(d)}"]`);
    const s = document.querySelector(`[data-rb-w="${cssEsc(d)}"]`);
    if (n) n.value = w[d];
    if (s) s.value = w[d];
  });
  const sum = DIMENSIONS.reduce((a, d) => a + w[d], 0) || 1;
  $("rbWeightSum").innerHTML = DIMENSIONS.map(d =>
    `${esc(d)} <b>${(w[d] / sum * 100).toFixed(0)}%</b>`).join(" · ") +
    ` <span class="muted">（原始值合计 ${sum}，系统按比例归一化）</span>`;
}

function onWeightNum() { onWeightInput(); }

function onPresetChange() {
  const key = $("rbPreset").value;
  if (!key) { $("rbPresetWhy").textContent = ""; return; }
  const p = (rbState.presets || []).find(x => x.key === key);
  if (!p) return;
  // 选预设 = 接受它那套权重与细则起点；之后仍可逐项改
  DIMENSIONS.forEach(d => {
    const n = document.querySelector(`[data-rb-wn="${cssEsc(d)}"]`);
    if (n) n.value = Math.round((p.weights || {})[d] * 100);
    const c = document.querySelector(`[data-rb-c="${cssEsc(d)}"]`);
    if (c) c.value = (p.criteria || {})[d] || "";
  });
  $("rbPresetWhy").textContent = (p.desc || "") + (p.why ? " " + p.why : "");
  onWeightInput();
}

async function saveRubric() {
  const msg = $("rbMsg");
  if (!state.job) return;
  msg.textContent = "保存中…";
  const criteria = {};
  DIMENSIONS.forEach(d => {
    const c = document.querySelector(`[data-rb-c="${cssEsc(d)}"]`);
    criteria[d] = c ? c.value : "";
  });
  try {
    const r = await API.jobRubricSave(state.job.id, {
      preset: $("rbPreset").value || (rbState.config || {}).preset,
      weights: readRubricWeights(),
      criteria: criteria,
      notes: $("rbNotes").value,
    });
    msg.textContent = r.changed
      ? `已保存，口径版本变为 ${r.version}`
      : `已保存（口径版本未变：${r.version}）`;
    rbState.config = r.config || {};
    rbState.canEdit = r.can_edit !== false;
    // 卡片上的权重和细则要立刻更新，否则用户会以为没保存成功
    await loadJob(state.job.id);
    renderRubricEditor(r);
  } catch (e) {
    msg.textContent = e.message || "保存失败";
  }
}

function closeRubricEditor() { $("rubricModal").classList.add("hidden"); }

function renderJdCard(jd) {
  const block = (title, items, cls) => items && items.length ? `
    <div class="jd-block">
      <div class="jd-block-title ${cls}">${title} <span class="muted">${items.length}</span></div>
      <ul>${items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>
    </div>` : "";

  const w = jd.weights || DEFAULT_WEIGHTS;
  const wsum = DIMENSIONS.map(d =>
    `<span class="wchip">${esc(d)} <b>${Math.round((w[d] || 0) * 100)}%</b></span>`).join("");

  const rc = jd.rubric || {};
  const presetLabel = rc.preset_label || rc.preset || "默认";
  const critRows = DIMENSIONS.map(d => {
    const t = (rc.criteria || {})[d] || "";
    return t ? `<div class="crit-row">
      <div class="crit-dim">${esc(d)}</div>
      <div class="crit-txt">${esc(t)}</div>
    </div>` : "";
  }).join("");
  const why = rc.preset_why || rc.why || "";
  const rubricBlock = `
    <div class="rubric-sec">
      <div class="rubric-sec-head">
        <div>
          <b>评审细则</b>
          <span class="chip tiny-chip">${esc(presetLabel)}</span>
          ${why ? `<div class="rubric-why">${esc(why)}</div>` : ""}
        </div>
        <button class="btn small" onclick="openRubricEditor()">✎ 调整权重与细则</button>
      </div>
      ${critRows || `<div class="hint">还没有自定义细则，评委将按通用描述打分。</div>`}
      ${rc.notes ? `<div class="crit-notes">补充：${esc(rc.notes)}</div>` : ""}
    </div>`;

  $("jdCard").innerHTML = `
    <div class="card-head">
      <h3>岗位要求</h3>
      <div class="head-actions">${wsum}</div>
    </div>
    <div class="card-body">
      ${(jd.requirements || []).length || (jd.preferred || []).length || (jd.red_flags || []).length
        ? `<div class="jd-grid">
             ${block("硬性要求", jd.requirements, "req")}
             ${block("优先项", jd.preferred, "pref")}
             ${block("风险红线", jd.red_flags, "red")}
           </div>`
        : `<div class="hint">还没有岗位要求。点右上角「编辑岗位要求」录入。</div>`}
      ${rubricBlock}
    </div>`;
}

/* ===================== 候选人列表 ===================== */
const CAND_STATUS = {
  pending: ["待评审", "pill"], running: ["评审中", "pill run"],
  done: ["已评", "pill ok"], failed: ["失败", "pill err"],
};

function renderCandidates() {
  const box = $("candList");
  if (!state.candidates.length) {
    box.innerHTML = `<div class="hint">还没有候选人素材。点右上角「上传素材」，
      支持 Word / PDF / 文本 / 图片（图片走视觉模型 OCR）。</div>`;
    return;
  }
  box.innerHTML = state.candidates.map(c => {
    const st = CAND_STATUS[c.status] || CAND_STATUS.pending;
    const res = c.result || {};
    const score = (res.total != null)
      ? `<span class="score g${res.grade || "C"}">${res.total} <em>${res.grade || ""}</em></span>` : "";
    const files = (c.files || []).map(f =>
      `<span class="filechip" title="${esc((f.warnings || []).join('；'))}">${esc(f.filename)}
        <em>${f.role === "assessment" ? "测评" : f.role === "transcript" ? "转写" : "简历"}·
        ${f.chars || 0}字</em></span>`).join("");

    // 身份待确认：必须在卡片上直接可见，否则 HR 不会点进去，
    // 这条"疑似同一人"就永远悬着，人才库里悄悄多出一份重复档案。
    const pending = c.person_decision === "pending" || c.identity_state === "uncertain";
    const idChip = pending
      ? `<button class="btn tiny warn-chip" onclick="openIdentityConfirm('${c.id}')">
           ⚠ 疑似同一人，待确认</button>`
      : (c.identity_state === "confirmed"
        ? `<span class="chip ok-chip" title="你已确认过这层身份关系">身份已确认</span>` : "");

    return `<div class="cand-row${pending ? " needs-attn" : ""}">
      <div class="cand-main">
        <div class="cand-name">${esc(c.name)} <span class="${st[1]}">${st[0]}</span>${score}${idChip}</div>
        <div class="cand-files">${files || '<span class="muted">无素材</span>'}</div>
        ${c.error ? `<div class="cand-err">${esc(c.error)}</div>` : ""}
        ${res.rubric_version ? `<div class="cand-meta muted">评分口径
          <code>${esc(res.rubric_version)}</code>${
            res.spread && res.spread.max >= 2
              ? ` · 评委分歧 <span class="cmp-spread ${spreadLabel(res.spread.max)[1]}">${esc(spreadLabel(res.spread.max)[0])}</span>` : ""}</div>` : ""}
        ${res.audit && res.audit.injection && res.audit.injection.risk !== "none"
          ? `<div class="cand-err">⚠ 材料中发现疑似指令注入（风险 ${esc(res.audit.injection.risk)}），
             已按数据处理，建议人工复核</div>` : ""}
        ${uncertaintyHtml(res)}
        ${linkHtml(c, res)}
        ${decideHtml(c)}
      </div>
      <div class="cand-actions">
        ${c.result ? `<button class="btn tiny" data-decide-id="${esc(c.id)}"
            data-decide-name="${esc(c.name)}"
            data-decide-cur="${esc(c.decision || "")}">${c.decision ? "改决定" : "记录决定"}</button>` : ""}
        <button class="btn tiny" onclick="openCandEdit('${c.id}')">编辑</button>
        <button class="btn tiny danger-ghost" data-del-cand="${esc(c.id)}">删除</button>
      </div>
    </div>`;
  }).join("");

/* 已记录的决定：显示决定 + 理由 + 谁什么时候定的。
   理由必须直接可见——如果理由被折叠在弹窗里，复核时没人会点开它，
   "留了理由"就退化成"留了个字段"。 */
/* 重复评审的波动：直接放在卡片上，不折叠。
   "同一份简历评 3 次得到 A/C/B" 这件事必须一眼看见——
   如果它藏在详情里，用户只会看到最终那个"中位数 B"，并以为它是稳的。 */
function uncertaintyHtml(res) {
  const u = res.uncertainty;
  const bits = [];
  if (u && u.repeats_planned > 1) {
    const sd = (u.stdev == null) ? "—" : u.stdev.toFixed(2);
    bits.push(`<span class="unc-chip${u.grade_disagreement ? " bad" : ""}">
      重复 ${esc(u.repeats_ok)}/${esc(u.repeats_planned)} 次 ·
      极差 ${esc(u.spread)} 分 · 标准差 ${esc(sd)}</span>`);
    if (u.grade_disagreement) {
      bits.push(`<span class="unc-warn">等级在重复评审间发生过变化
        （${esc((u.grades || []).join("、"))}）—— 该候选人处于档位边界，
        <b>不要用等级做判断</b>。</span>`);
    }
    if (u.repeats_failed) {
      bits.push(`<span class="unc-warn">有 ${esc(u.repeats_failed)} 次评审失败，
        上面的分数只基于成功的 ${esc(u.repeats_ok)} 次。</span>`);
    }
  }
  const cb = res.audit && res.audit.context_budget;
  if (res.audit && res.audit.context_truncated && cb) {
    // 区分两件事，否则用户会以为"整份材料都没读到"：
    //   truncated_sections  = 材料内部被截（头尾保留了，中间省略）
    //   dropped_sections    = 整段没装进去
    const iCut = cb.truncated_sections || [];
    const iDrop = cb.dropped_sections || [];
    const detail = [
      iCut.length ? `${esc(iCut.join("、"))} 保留了头尾、省略中段` : "",
      iDrop.length ? `${esc(iDrop.join("、"))} 完全没有装入` : "",
    ].filter(Boolean).join("；");
    bits.push(`<span class="unc-warn">材料超长已截断（约 ${esc(cb.original_tokens)} →
      ${esc(cb.final_tokens)} token${detail ? "，" + detail : ""}）。
      结论只对<b>被读到的那部分材料</b>成立。</span>`);
  }
  if (!bits.length) return "";
  return `<div class="cand-unc">${bits.join("")}</div>`;
}

/* 历史入口：只有评过多次才显示，避免给只跑过一次的人一个空页面 */
function linkHtml(c, res) {
  const u = res.uncertainty;
  if (!u || !u.repeats_planned) return "";
  return `<button class="btn tiny ghost" onclick="openHistory('${c.id}')">
    评审历史</button>`;
}

function decideHtml(c) {
  if (!c.decision) return "";
  const map = {
    advance: ["推进面试", "ok-chip"],
    reject: ["淘汰", "bad-chip"],
    hold: ["待定", "warn-chip"],
  };
  const m = map[c.decision] || [c.decision, ""];
  return `<div class="cand-decision">
    <span class="chip ${m[1]}">${esc(m[0])}</span>
    <span class="cand-reason">${esc(c.decision_reason || "")}</span>
    <span class="muted">— ${esc(c.decided_by || "未知")}
      ${esc((c.decided_at || "").slice(0, 16).replace("T", " "))}</span>
  </div>`;
}
}

async function deleteCandidate(id) {
  if (!confirm("删除这位候选人及其素材？\n\n这会同时删除已上传的原始文件与该候选人的评审结果，不可恢复。")) return;
  try {
    await API.deleteCandidate(id);
  } catch (e) {
    toast("删除失败：" + e.message, "err");
    return;
  }
  await openJob(state.job.id);
}

/* 编辑候选人：只改姓名。后端 PUT 已收窄为内容字段白名单
   （store.CAND_HTTP_PATCHABLE），状态 / 结果 / 身份在这里改不动——
   它们必须走各自的专用端点，才能保住状态机和留痕。 */
function openCandEdit(cid) {
  const c = state.candidates.find(x => x.id === cid);
  if (!c) { toast("找不到该候选人，请刷新岗位", "err"); return; }
  state.candEditTarget = c;
  $("ceName").value = c.name || "";
  $("ceMsg").textContent = "";
  $("candEditModal").classList.remove("hidden");
}
function closeCandEdit() {
  $("candEditModal").classList.add("hidden");
  state.candEditTarget = null;
}

async function saveCandEdit() {
  const c = state.candEditTarget;
  if (!c) return;
  const name = ($("ceName").value || "").trim();
  if (!name) { $("ceMsg").textContent = "姓名不能为空"; return; }
  const msg = $("ceMsg");
  msg.textContent = "保存中…";
  try {
    await API.updateCandidate(c.id, { name: name });
    msg.textContent = "";
    closeCandEdit();
    c.name = name;
    renderCandidates();
    refreshPendingIdentity();
    toast("已保存", "ok");
  } catch (e) { msg.textContent = e.message; }
}

/* ===================== 评审 ===================== */
function renderRunHint() {
  const n = state.candidates.length;
  const pending = state.candidates.filter(c => c.status !== "done").length;
  const rebuild = $("optRebuild").checked;
  const will = rebuild ? n : pending;
  if (n === 0) {
    $("runHint").textContent = "先上传候选人素材才能开始评审。";
    $("costHint").textContent = "";
  } else {
    $("runHint").textContent =
      `共 ${n} 位候选人，本次将评审 ${will} 位（每位 10 次模型调用：2 立论 + 4 驳斥 + 1 裁判 + 3 评委）。`;
    // 异步取成本预估，拿到后补充到提示里
    fetchCostHint(will);
  }
  $("runBtn").disabled = n === 0 || state.running;

  // 运行中显示「取消」，结束后收回
  const cb = $("cancelRunBtn");
  if (cb) cb.classList.toggle("hidden", !state.running);
}

/** 运行前把"大概要花多少"算出来给 HR 看，并如实标注它有多可信 */
async function fetchCostHint(n) {
  const box = $("costHint");
  if (!box) return;
  box.textContent = "正在估算本次消耗…";
  box.className = "hint cost-hint";
  try {
    const cfg = await API.modelConfig().catch(() => null);
    const c = cfg && cfg.config ? cfg.config : {};
    const r = await API.estimate(state.job.id, {
      provider: c.provider || "",
      model: c.model || null,
    });
    const e = r.estimate || {};
    const bits = [];
    bits.push(`预计 ${e.total_calls || n * 10} 次模型调用`);
    if (e.estimated_tokens) bits.push(`≈ ${(e.estimated_tokens / 1000).toFixed(1)}k tokens`);
    if (e.estimated_cost_usd != null) bits.push(`≈ $${e.estimated_cost_usd}`);
    if (e.estimated_credits != null) bits.push(`≈ ${e.estimated_credits} 积分`);
    if (e.remaining_before != null) {
      bits.push(`当前剩余 ${e.remaining_before}`);
      if (e.enough === false) bits.push("⚠ 预计会不够");
    }
    let msg = bits.join(" · ");
    // 关键：把"这个数字有多可信"直接写在旁边，而不是让人以为它很准
    msg += e.confident ? "（基于历史实测，较可信）"
                       : "（样本不足，仅供量级参考，实际可能偏差较大）";
    box.textContent = msg;
    box.className = "hint cost-hint" + (e.enough === false ? " warn" : "");
    if (e.basis) box.title = e.basis;

    // 有历史实测就顺带把"估得准不准"摆出来
    const act = r.actual || {};
    if (act.available) {
      const d = $("actualHint");
      if (d) {
        d.textContent = `历史实测：${act.actual_calls} 次调用 / ${act.actual_tokens} tokens，` +
          `均 ${act.actual_avg_tokens_per_call} tokens 每次。` + (act.message || "");
        d.classList.remove("hidden");
      }
    }
  } catch (err) {
    box.textContent = "";
  }
}

$("optRebuild") && $("optRebuild").addEventListener("change", renderRunHint);

async function startRun() {
  if (state.running) return;
  const rebuild = $("optRebuild").checked;
  const ids = rebuild ? null
    : state.candidates.filter(c => c.status !== "done").map(c => c.id);
  if (!rebuild && !ids.length) { toast("所有候选人都已评过；勾选「重跑已评过的」可重跑。", "info"); return; }

  state.running = true;
  state.liveEvents = [];
  state.follow = true;              // 新一轮重新跟随
  $("runBtn").disabled = true;
  $("liveCard").classList.remove("hidden");
  $("liveFeed").innerHTML = "";
  $("liveProgress").textContent = "准备中…";
  updateFollowBtn();
  renderRunHint();

  let run;
  try {
    const r = await API.startRun(state.job.id, { candidate_ids: ids });
    run = r.run;
    state.currentRunId = run.id;
  } catch (e) {
    state.running = false;
    $("runBtn").disabled = false;
    // 409 = 已有评审在跑。这不是错误，是把已在跑的那条接上。
    if (e.status === 409) {
      toast("该岗位已有评审在进行，正在接上现有进度…", "info");
      try {
        const st = await API.runState(state.job.id);
        if (st.active) { run = st.active; state.currentRunId = run.id; }
      } catch (x) { /* 忽略 */ }
      if (!run) { renderRunHint(); return; }
    } else {
      toast("启动评审失败：" + e.message, "err");
      renderRunHint();
      return;
    }
  }

  state.running = true;
  $("runBtn").disabled = false;

  // 断点续传：先补一遍已发生的事件（刷新页面也能看到完整过程）
  try {
    const past = await API.runEvents(run.id, 0);
    (past.events || []).forEach(onLiveEvent);
  } catch (e) { /* 补历史失败不影响实时流 */ }

  if (state.streamClose) { try { state.streamClose(); } catch (e) {} }
  state.streamClose = API.streamRun(run.id, onLiveEvent, (info) => {
    state.running = false;
    state.streamClose = null;
    const cancelled = info && info.status === "cancelled";
    $("liveProgress").textContent = cancelled ? "已取消" : "已结束";
    openJob(state.job.id).then(() => {
      if (!cancelled) toast("评审完成", "ok");
    });
    refreshPendingIdentity();
  }, { after: 0 });

  // 让「取消」按钮可用
  renderRunHint();
}

/* ---------- 实时辩论直播间 ---------- */
/* 富文本渲染有正则开销，逐块重排是 O(n²)。打字机效果不需要每块都重排：
   攒到 ~100ms 再渲染一次，肉眼完全看不出差别，CPU 差一个数量级。 */
const BUBBLE_RENDER_MS = 100;

function bubbleKey(ev) {
  // 用 candidate_id，不能用姓名。两个理由：
  // ① 3~5 路并发时，不同候选人的 pro_open 是同一个 key，只用 key 会让
  //    后一个人的发言覆盖前一个人的气泡（曾经就是这样）；
  // ② 即使把姓名拼上也还不够——同一个岗位下出现两个「张伟」是真实存在的
  //    （identity 模块专门处理同名，align 报告层甚至拒绝按姓名回退匹配）。
  //    存储层按 ID，展示层也必须按 ID，否则两块逻辑自相矛盾。
  // 旧事件/旧库里没有 candidate_id 时回退到姓名，保证历史数据仍能显示。
  const who = ev.candidate_id || ev.candidate || "";
  return who + "::" + (ev.key || "");
}

function findBubble(feed, ev) {
  return feed.querySelector(`[data-key="${cssEsc(bubbleKey(ev))}"]`);
}

function makeBubble(feed, ev) {
  const safeKey = esc(bubbleKey(ev));
  appendLive(feed, `<div class="bubble ${ROLE_META[ev.role]?.cls || "sys"} thinking"
      data-key="${safeKey}">
      <div class="bubble-head">${bubbleHead(ev)}</div>
      <div class="bubble-body"><span class="dots"><i></i><i></i><i></i></span></div>
    </div>`);
  return findBubble(feed, ev);
}

function scheduleBubbleRender(el) {
  if (!el || el._rbTimer) return;
  el._rbTimer = setTimeout(() => {
    el._rbTimer = null;
    const body = el.querySelector(".bubble-body");
    if (!body) return;
    el.classList.remove("thinking");
    el.classList.add("streaming");
    // 打字过程中不解析「」高亮：半句话里的引号还没闭合，高亮会闪烁。
    // 直接按纯文本显示 + 一个光标，结束后一次性渲染成富文本。
    body.innerHTML = `<span class="typed"></span><span class="caret"></span>`;
    body.querySelector(".typed").textContent = el._raw || "";
    if (state.follow) {
      const f = $("liveFeed");
      f.scrollTop = f.scrollHeight;
    }
  }, BUBBLE_RENDER_MS);
}

function onLiveEvent(ev) {
  const feed = $("liveFeed");

  // 增量事件量大（每秒几十条）且用完即弃，不进内存里那本人造事件史
  if (ev.type === "agent_delta") {
    let el = findBubble(feed, ev);
    if (!el) el = makeBubble(feed, ev);
    el._raw = (el._raw || "") + (ev.text || "");
    scheduleBubbleRender(el);
    return;
  }

  state.liveEvents.push(ev);

  if (ev.type === "run_start") {
    $("liveProgress").textContent = `0 / ${ev.total}`;
    appendLive(feed, `<div class="live-sys">评审开始 · 共 ${ev.total} 位候选人</div>`);
    if (ev.rubric_version) {
      appendLive(feed, `<div class="live-sys muted">评分口径 ${esc(ev.rubric_version)} ·
        ${esc(ev.note || "与不同口径的分数不可直接比较")}</div>`);
    }
  } else if (ev.type === "candidate_queue") {
    $("liveProgress").textContent = `${ev.index} / ${ev.total} 位候选人`;
    appendLive(feed, `<div class="live-cand">第 ${ev.index}/${ev.total} 位 · ${esc(ev.candidate)}</div>`);
  } else if (ev.type === "agent_start") {
    // 用"思考中"占位，避免几秒钟毫无反馈
    makeBubble(feed, ev);
  } else if (ev.type === "agent_message") {
    const el = findBubble(feed, ev);
    const html = `<div class="bubble-head">${bubbleHead(ev)}</div>
                  <div class="bubble-body">${renderDebateText(ev.text)}</div>`;
    if (el) {
      // 流式结束后用全文替换，而不是接着追加：以服务端返回的最终文本为准，
      // 中途万一有重试/补全，这里能自愈，不会显示重复的半截内容。
      if (el._rbTimer) { clearTimeout(el._rbTimer); el._rbTimer = null; }
      el._raw = ev.text || "";
      el.classList.remove("thinking", "streaming");
      el.innerHTML = html;
    } else {
      appendLive(feed, `<div class="bubble ${ROLE_META[ev.role]?.cls || "sys"}"
          data-key="${esc(bubbleKey(ev))}">${html}</div>`);
    }
  } else if (ev.type === "judge_scored") {
    const s = ev.judge.scores || {};
    appendLive(feed, `<div class="live-score">★ ${esc(ev.judge.name)}：
      ${DIMENSIONS.map(d => `${esc(d)} <b>${s[d]}</b>`).join(" · ")}</div>`);
  } else if (ev.type === "candidate_failed") {
    appendLive(feed, `<div class="live-err">✗ ${esc(ev.candidate)} 评审失败：${esc(ev.error)}</div>`);
  } else if (ev.type === "candidate_done") {
    appendLive(feed, `<div class="live-done">✓ ${esc(ev.candidate)} 综合 <b>${ev.total}</b> · ${ev.grade} 级</div>`);
  } else if (ev.type === "injection_warning") {
    // 提示注入不是"候选人人品问题"，只说明材料里出现了可疑指令文本，
    // 需要人复核。措辞上不能暗示"这人作弊"。
    appendLive(feed, `<div class="live-err">⚠ ${esc(ev.candidate || "")} 材料中发现疑似指令注入文本
      （风险 ${esc(ev.risk || "?")}）：${esc((ev.patterns || []).join("、"))}。
      已按数据而非指令处理，请人工复核该材料。</div>`);
  } else if (ev.type === "run_cancelled" || ev.type === "cancelling") {
    appendLive(feed, `<div class="live-sys">${esc(ev.message || "正在取消…")}</div>`);
  } else if (ev.type === "run_done") {
    $("liveProgress").textContent = ev.cancelled ? "已取消" : "全部完成";
    appendLive(feed, `<div class="live-sys">评审结束：成功 ${ev.succeeded} 位，失败 ${ev.failed} 位${
      ev.cancelled ? "（已取消）" : ""}</div>`);
  } else if (ev.type === "error") {
    appendLive(feed, `<div class="live-err">✗ ${esc(ev.error)}</div>`);
  }
}

function bubbleHead(ev) {
  const m = ROLE_META[ev.role] || ROLE_META.system;
  return `<span class="role-badge ${m.cls}">${m.icon} ${esc(m.name)}</span>
          <span class="bubble-label">${esc(ev.label || "")}</span>`;
}

function appendLive(feed, html) {
  const d = document.createElement("div");
  d.className = "live-item";
  d.innerHTML = html;
  feed.appendChild(d);
  if (state.follow) feed.scrollTop = feed.scrollHeight;
}

function updateFollowBtn() {
  const b = $("liveFollow");
  if (b) b.textContent = state.follow ? "自动滚动：开" : "自动滚动：关（已暂停跟随）";
}

function toggleFollow() {
  state.follow = !state.follow;
  updateFollowBtn();
  if (state.follow) {
    const feed = $("liveFeed");
    feed.scrollTop = feed.scrollHeight;
  }
}

/**
 * 你往上滚看内容时，自动跟随要让位。
 * 否则每来一条新消息就把视图拽回底部，等于"根本看不了"上面的内容。
 * 滚回底部则自动恢复跟随。
 */
function initLiveFeedFollow() {
  const feed = $("liveFeed");
  if (!feed) return;
  feed.addEventListener("scroll", () => {
    const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
    if (atBottom === state.follow) return;
    state.follow = atBottom;
    updateFollowBtn();
  }, { passive: true });
}

function cssEsc(s) { return String(s).replace(/["\\]/g, "\\$&"); }

/* ---------------------------------------------------------------------------
   环境自检
   demo 现场最怕的不是功能不好用，而是"配好了但跑不起来"且不知道卡在哪。
   默认只查不需要联网的四项；点「检测模型连通性」才真的打一次 API
   （服务端缓存 10 分钟，避免每刷一次页面就烧一次额度）。
--------------------------------------------------------------------------- */
async function loadSelfcheck(probe) {
  const box = $("scItems");
  if (!box) return;
  if (probe) {
    const btn = $("scProbeBtn");
    if (btn) { btn.disabled = true; btn.textContent = "检测中…"; }
  }
  try {
    const r = await API.selfcheck(!!probe);
    renderSelfcheck(r);
  } catch (e) {
    $("scSummary").textContent = "自检失败：" + (e.message || "");
  } finally {
    const btn = $("scProbeBtn");
    if (btn) { btn.disabled = false; btn.textContent = "检测模型连通性"; }
  }
}

function probeSelfcheck() { return loadSelfcheck(true); }

function renderSelfcheck(r) {
  const items = [];
  const db = r.db || {};
  items.push({
    ok: !!db.ok,
    label: "数据库",
    text: db.ok
      ? (db.writable ? "可读可写" : "只读！评审结果将无法保存")
      : ("不可用：" + (db.error || "未知")),
    extra: "schema v" + (r.schema_version || "?"),
  });

  const prov = r.provider || {};
  items.push({
    ok: !!prov.configured,
    label: "模型配置",
    text: prov.configured
      ? `${esc(prov.provider || "")} ${esc(prov.model || "")}`.trim()
      : (prov.note || "未配置"),
    extra: prov.needs_key ? "需要 API Key" : "",
  });

  const bk = r.backup || {};
  const age = bk.age_hours;
  items.push({
    ok: bk.count > 0 && !bk.stale,
    label: "备份",
    text: bk.count === 0
      ? "还没有备份"
      : `${bk.count} 份 · 最近 ${age == null ? "?" : age} 小时前`,
    extra: bk.stale ? "已过期，建议现在备份一次" : "",
  });

  if (r.connectivity) {
    const c = r.connectivity;
    items.push({
      ok: !!c.ok,
      label: "模型连通性",
      text: c.ok ? `可用（${c.latency_ms} ms）` : (c.error || "不可用"),
      extra: r.connectivity_cached_for
        ? `结果缓存 ${r.connectivity_cached_for}s` : "",
    });
  } else {
    items.push({ ok: null, label: "模型连通性", text: "未检测", extra: "点上方按钮" });
  }

  if (r.credits) {
    const cr = r.credits;
    items.push({
      ok: cr.ok !== false,
      label: "WorkBuddy 额度",
      text: cr.ok === false ? (cr.error || "读取失败")
        : (cr.text || cr.remaining_text || cr.summary || "已读取"),
      extra: "",
    });
  }

  $("scItems").innerHTML = items.map(it => `
    <div class="sc-item ${it.ok === true ? "ok" : it.ok === false ? "bad" : "warn"}">
      <span class="sc-dot"></span>
      <b>${esc(it.label)}</b>
      <span class="sc-text">${it.text || ""}</span>
      ${it.extra ? `<span class="sc-extra muted">${esc(it.extra)}</span>` : ""}
    </div>`).join("");

  const bad = items.filter(x => x.ok === false).length;
  $("scSummary").textContent = bad
    ? `${bad} 项需要注意`
    : "全部正常";
  $("scNote").textContent = r.note || "";
}


/* ---------------------------------------------------------------------------
   辩论直播间浮窗
   目标：跑评审的几十分钟里，既要能偶尔瞄一眼辩论过程，也要能腾出手
   去做别的事（改岗位、看别的候选人）。所以它得能浮、能拖、能缩放、能收起。
   位置和尺寸记在 localStorage，下次打开还是你摆的那个样子。
--------------------------------------------------------------------------- */
const LIVEBOX_KEY = "talent_screen.livebox";

function loadLiveGeom() {
  try { return JSON.parse(localStorage.getItem(LIVEBOX_KEY) || "{}"); }
  catch (e) { return {}; }
}

function saveLiveGeom() {
  const card = $("liveCard");
  if (!card || !card.classList.contains("float")) return;
  // 隐藏时 getBoundingClientRect() 全是 0。曾经把这组 0 存进 localStorage，
  // 于是下次"浮窗默认出现在右下角"变成了"钉死在屏幕左上角"——
  // 而代码里没有任何地方写着 0。
  if (card.classList.contains("hidden")) return;
  const r = card.getBoundingClientRect();
  if (r.width < 80 || r.height < 20) return;
  localStorage.setItem(LIVEBOX_KEY, JSON.stringify({
    left: Math.round(r.left), top: Math.round(r.top),
    width: Math.round(r.width), height: Math.round(r.height),
    min: card.classList.contains("min"),
    float: true,
  }));
}

/** 把浮窗夹回可视区：至少留 120px 宽、整个标题栏可见，否则会"拖丢了"。 */
function clampLiveBox() {
  const card = $("liveCard");
  if (!card || !card.classList.contains("float")) return;
  const r = card.getBoundingClientRect();
  const W = window.innerWidth, H = window.innerHeight;
  let left = r.left, top = r.top;
  if (left > W - 120) left = W - 120;
  if (left < 8 - r.width + 120) left = 8 - r.width + 120;
  if (top < 8) top = 8;
  if (top > H - 48) top = H - 48;
  if (left !== r.left || top !== r.top) {
    card.style.left = left + "px";
    card.style.top = top + "px";
    card.style.right = "auto";
    card.style.bottom = "auto";
  }
}

function applyLiveGeom() {
  const card = $("liveCard");
  if (!card) return;
  const g = loadLiveGeom();
  if (g.min) card.classList.add("min");
  // 只接受"看起来像真的"的尺寸/坐标；0 或负数说明是被隐藏状态污染过的旧值
  if (g.width > 80) card.style.width = Math.max(320, g.width) + "px";
  if (g.height > 20) card.style.height = Math.max(150, g.height) + "px";
  if (g.left > 0 && g.top > 0) {
    card.style.left = g.left + "px";
    card.style.top = g.top + "px";
    card.style.right = "auto";
    card.style.bottom = "auto";
  }
}

function toggleLiveFloat() {
  const card = $("liveCard");
  if (!card) return;
  const on = card.classList.toggle("float");
  if (on) {
    // 从内嵌切到浮窗时，先把内联的 left/top 清掉再套用记忆值
    card.style.left = card.style.top = "";
    card.style.right = card.style.bottom = "";
    applyLiveGeom();
    clampLiveBox();
  } else {
    card.style.left = card.style.top = "";
    card.style.right = card.style.bottom = "";
    card.style.width = card.style.height = "";
  }
  const b = $("liveDock");
  if (b) b.textContent = on ? "内嵌" : "浮窗";
  saveLiveGeom();
}

function toggleLiveMin() {
  const card = $("liveCard");
  if (!card) return;
  const min = card.classList.toggle("min");
  const b = $("liveMin");
  if (b) b.textContent = min ? "▢" : "—";
  if (!min) clampLiveBox();
  saveLiveGeom();
}

function initLiveBox() {
  const card = $("liveCard");
  const head = $("liveHead");
  if (!card || !head) return;

  let drag = null;
  head.addEventListener("mousedown", (e) => {
    if (!card.classList.contains("float")) return;
    if (e.target.closest("button")) return;      // 别把点按钮变成拖动
    const r = card.getBoundingClientRect();
    drag = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    card.style.left = r.left + "px";
    card.style.top = r.top + "px";
    card.style.right = "auto";
    card.style.bottom = "auto";
    card.style.width = r.width + "px";
    card.style.height = r.height + "px";
    card.classList.add("dragging");
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    card.style.left = (e.clientX - drag.dx) + "px";
    card.style.top = (e.clientY - drag.dy) + "px";
  });
  document.addEventListener("mouseup", () => {
    if (!drag) return;
    drag = null;
    card.classList.remove("dragging");
    clampLiveBox();
    saveLiveGeom();
  });

  // 用户用 CSS 手柄缩放时没有事件，只能观察尺寸变化后落盘
  if (window.ResizeObserver) {
    let t = null;
    new ResizeObserver(() => {
      clearTimeout(t);
      t = setTimeout(saveLiveGeom, 400);
    }).observe(card);
  }
  window.addEventListener("resize", clampLiveBox);

  // 默认就以浮窗出现：内嵌的样子会在正文里推开内容，评审时反而挡路
  const g = loadLiveGeom();
  if (g.float !== false) {
    card.classList.add("float");
    applyLiveGeom();
    const b = $("liveDock");
    if (b) b.textContent = "内嵌";
  }
  const mb = $("liveMin");
  if (mb && card.classList.contains("min")) mb.textContent = "▢";
}

/**
 * 把 agent 的发言渲染得能看清"抓漏洞"的过程：
 * - 「…」引用的原文 → 高亮（这是它拿出来当证据的原句）
 * - **加粗** → strong
 * - Markdown 小标题 ### → 段落标题
 * 只做这几种，不引 Markdown 库。
 */
function renderDebateText(text) {
  let s = esc(text || "");
  s = s.replace(/###\s*(.+)/g, '<div class="dt-h">$1</div>');
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/「([^」]{1,200})」/g, '<mark class="quote">「$1」</mark>');
  s = s.replace(/\n/g, "<br>");
  return s;
}

/* ===================== 候选人横向对比 =====================
   评审的最终目的是「在几个人里选一个」，逐个展开看很累。
   这里做并排对比，并顺带算一个一直没用上的信号：**评委分歧度**。

   分歧度 = 三位评委在同一维度上的极差。
   分歧大说明这个人"有人觉得很好、有人觉得很差"——正是最该复看和面试深挖的对象；
   分歧小则说明三位评委看法一致，结论更稳。 */
const compareSet = new Set();

function toggleCompare(candId, ev) {
  if (ev) ev.stopPropagation();          // 别把 <details> 一起展开了
  if (compareSet.has(candId)) compareSet.delete(candId);
  else compareSet.add(candId);
  syncCompareUI();
}

function clearCompare() {
  compareSet.clear();
  syncCompareUI();
}

function syncCompareUI() {
  const bar = $("compareBar");
  const n = compareSet.size;
  $("compareCount").textContent = `已选 ${n} 人`;
  bar.classList.toggle("hidden", n === 0);
  document.querySelectorAll("[data-cmp]").forEach(el => {
    const on = compareSet.has(el.dataset.cmp);
    el.classList.toggle("on", on);
    el.textContent = on ? "☑" : "☐";
  });
}

function closeCompare() { $("compareModal").classList.add("hidden"); }

/** 从三位评委的分数算每个维度的极差（分歧度） */
function judgeSpread(result) {
  const out = {};
  const judges = result.judges || [];
  DIMENSIONS.forEach(d => {
    const vals = judges.map(j => (j.scores || {})[d]).filter(v => typeof v === "number");
    out[d] = vals.length >= 2 ? Math.round((Math.max(...vals) - Math.min(...vals)) * 10) / 10 : null;
  });
  return out;
}

function spreadLabel(v) {
  if (v == null) return ["—", "dim"];
  if (v < 1.5) return ["一致", "ok"];
  if (v < 3) return ["有分歧", "mid"];
  return ["分歧大", "warn"];
}

function openCompare() {
  const picked = state.candidates.filter(c => compareSet.has(c.id) && c.result);
  if (picked.length < 2) { toast("至少选 2 位候选人才能对比", "info"); return; }
  $("compareBody").innerHTML = renderCompare(picked);
  $("compareModal").classList.remove("hidden");
}

function renderCompare(rows) {
  rows = rows.slice().sort((a, b) => (b.result.total || 0) - (a.result.total || 0));

  // ---- 表头 ----
  const head = `<tr><th class="cmp-h">维度</th>${rows.map(c => `
    <th><div class="cmp-name">${esc(c.name)}</div>
        <div class="cmp-total ${GRADE_CLS[c.result.grade] || ""}">
          ${c.result.total}<em>${c.result.grade}</em></div></th>`).join("")}</tr>`;

  // ---- 每个维度：横向条 + 最高分标记 ----
  const dimRows = DIMENSIONS.map(d => {
    const vals = rows.map(c => (c.result.scores || {})[d] ?? 0);
    const max = Math.max(...vals);
    return `<tr>
      <td class="cmp-h">${esc(d)}</td>
      ${rows.map((c, i) => {
        const v = vals[i];
        const isTop = v === max && vals.filter(x => x === max).length === 1;
        return `<td>
          <div class="cmp-bar-wrap">
            <div class="cmp-bar ${isTop ? "top" : ""}" style="width:${(v / 10) * 100}%"></div>
            <span class="cmp-val">${v}${isTop ? ' <b class="cmp-best">最高</b>' : ""}</span>
          </div>
        </td>`;
      }).join("")}
    </tr>`;
  }).join("");

  // ---- 评委分歧度 ----
  const spreadRows = DIMENSIONS.map(d => `<tr>
    <td class="cmp-h">${esc(d)}</td>
    ${rows.map(c => {
      const v = judgeSpread(c.result)[d];
      const [label, cls] = spreadLabel(v);
      return `<td><span class="cmp-spread ${cls}">${label}${v != null ? ` ±${v}` : ""}</span></td>`;
    }).join("")}
  </tr>`).join("");

  // ---- 标签 ----
  const tagRow = `<tr><td class="cmp-h">标签</td>${rows.map(c =>
    `<td class="cmp-tags">${(c.result.tags || []).map(t =>
      `<span class="tag">${esc(t)}</span>`).join("") || "—"}</td>`).join("")}</tr>`;

  // ---- 自动差异结论（只陈述事实，不替 HR 做决定）----
  const notes = [];
  DIMENSIONS.forEach(d => {
    const vals = rows.map(c => ({ n: c.name, v: (c.result.scores || {})[d] ?? 0 }));
    const max = Math.max(...vals.map(x => x.v));
    const min = Math.min(...vals.map(x => x.v));
    if (max - min >= 1.5) {
      const top = vals.filter(x => x.v === max).map(x => x.n).join("、");
      const bot = vals.filter(x => x.v === min).map(x => x.n).join("、");
      notes.push(`<li><b>${esc(d)}</b>：${esc(top)} 领先 ${max}，${esc(bot)} 落后 ${min}，差 ${Math.round((max - min) * 10) / 10} 分</li>`);
    }
  });
  const polarizing = rows.filter(c => Object.values(judgeSpread(c.result)).some(v => v != null && v >= 3));
  if (polarizing.length) {
    notes.push(`<li><b>评委分歧大</b>：${polarizing.map(c => esc(c.name)).join("、")} 在不同评委手里分差 ≥3 分，属于"有人很看好、有人很看衰"，建议面试重点验证争议维度</li>`);
  }
  const todos = rows.filter(c => (c.result.debate?.adjudication?.excluded || []).length > 0);
  if (todos.length) {
    notes.push(`<li><b>存在无依据论断</b>：${todos.map(c => esc(c.name)).join("、")} 的辩论中出现过简历不支持的论点（已被裁判剔除），面试时值得核实</li>`);
  }

  return `<table class="cmp-table">
      <thead>${head}</thead>
      <tbody>
        ${dimRows}
        <tr class="cmp-sep"><td colspan="${rows.length + 1}">评委分歧度（三位评委的极差）</td></tr>
        ${spreadRows}
        ${tagRow}
      </tbody>
    </table>
    ${notes.length ? `<div class="cmp-notes"><h4>自动差异提示</h4><ul>${notes.join("")}</ul>
      <p class="hint">以上只陈述分数与分歧事实，不替你做录用决定。</p></div>` : ""}`;
}

function exportCompare() {
  const picked = state.candidates.filter(c => compareSet.has(c.id) && c.result);
  if (!picked.length) return;
  let md = `# 候选人对比 · ${state.job ? state.job.title : ""}\n\n生成时间：${new Date().toLocaleString()}\n\n`;
  md += `| 维度 | ${picked.map(c => c.name).join(" | ")} |\n`;
  md += `|---|${picked.map(() => "---").join("|")}|\n`;
  md += `| **综合** | ${picked.map(c => `${c.result.total}（${c.result.grade}）`).join(" | ")} |\n`;
  DIMENSIONS.forEach(d => {
    md += `| ${d} | ${picked.map(c => (c.result.scores || {})[d] ?? "—").join(" | ")} |\n`;
  });
  md += `| 评委分歧度 | ${picked.map(c => {
    const s = judgeSpread(c.result);
    const mx = Math.max(...Object.values(s).filter(v => v != null), 0);
    return mx ? `±${mx}` : "—";
  }).join(" | ")} |\n`;
  md += `| 标签 | ${picked.map(c => (c.result.tags || []).join("、") || "—").join(" | ")} |\n`;
  DIMENSIONS.forEach(d => {
    md += `\n## ${d}\n`;
    picked.forEach(c => {
      const cs = (c.result.judges || []).map(j =>
        `${j.name}：${(j.scores || {})[d] ?? "—"}`).join("；");
      md += `- **${c.name}** — ${cs}\n`;
    });
  });
  const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `candidates_compare_${(state.job?.title || "job").replace(/\s+/g, "_")}.md`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ===================== 结果 ===================== */
function renderResults() {
  const done = state.candidates.filter(c => c.status === "done" && c.result);
  if (!done.length) { $("resultCard").classList.add("hidden"); return; }
  $("resultCard").classList.remove("hidden");
  // 岗位切换后，之前勾选的候选人可能已不在本岗位，清掉避免误对比
  const alive = new Set(done.map(c => c.id));
  [...compareSet].forEach(id => { if (!alive.has(id)) compareSet.delete(id); });
  syncCompareUI();

  const rows = done.slice().sort((a, b) => (b.result.total || 0) - (a.result.total || 0));
  $("resultBody").innerHTML = rows.map(c => {
    const r = c.result;
    const judges = (r.judges || []).map(j => `
      <div class="judge-line">
        <span class="judge-name">${esc(j.name)}</span>
        <span class="judge-scores">${DIMENSIONS.map(d => `${Math.round((j.scores[d] || 0) * 10) / 10}`).join(" / ")}</span>
      </div>
      <div class="judge-comment">${renderDebateText(j.comment || "")}</div>`).join("");
    const tags = (r.tags || []).map(t => `<span class="tag">${esc(t)}</span>`).join("");
    const adj = r.debate?.adjudication || {};
    const excluded = (adj.excluded || []).map(e => `<li>${renderDebateText(e)}</li>`).join("");
    const disputes = (adj.disputes || []).map(d => `<li>${renderDebateText(d)}</li>`).join("");
    return `<details class="result-row" data-cand="${esc(c.id)}">
      <summary>
        <span class="cmp-check" data-cmp="${esc(c.id)}" title="加入对比"
              onclick="toggleCompare('${esc(c.id)}', event)">☐</span>
        <span class="rname">${esc(c.name)}</span>
        <span class="rtotal g${r.grade}">${r.total}<em>${r.grade}</em></span>
        <span class="rtags">${tags}</span>
        <span class="rscores">${DIMENSIONS.map(d => `${esc(d)} ${r.scores[d]}`).join(" · ")}</span>
      </summary>
      <div class="result-detail">
        <div class="rsec">
          <h4>🧾 反幻觉审计（裁判剔除的无原文支撑论断）</h4>
          ${excluded ? `<ul class="audit-list">${excluded}</ul>
             <p class="hint">这些是 Agent 辩论中出现、但简历/测评原文并不支持的论断，已被裁判剔除。它们同时是最好的面试验证题来源。</p>`
            : `<p class="hint">本次评审没有发现明显的无依据论断。</p>`}
        </div>
        <div class="rsec">
          <h4>⚖️ 核心争议点</h4>
          ${disputes ? `<ul class="audit-list disputes">${disputes}</ul>` : `<p class="hint">（无明显争议）</p>`}
        </div>
        <div class="rsec">
          <div class="rsec-head">
            <h4>🎤 面试追问清单</h4>
            <button class="btn small" onclick="genInterview('${esc(c.id)}', this)">生成追问清单</button>
          </div>
          <div class="interview-box" id="iv-${esc(c.id)}">${
            c.interview ? renderInterviewHTML(c.interview) : ""
          }</div>
        </div>
        <div class="rtabs">
          <div class="rsec"><h4>评委盲审</h4>${judges}</div>
          <div class="rsec"><h4>辩论留痕</h4>
            <div class="debate-mini">
              <div class="dm pro"><b>正方</b>${renderDebateText(r.debate?.pro || "")}</div>
              <div class="dm con"><b>反方</b>${renderDebateText(r.debate?.con || "")}</div>
            </div>
          </div>
        </div>
      </div>
    </details>`;
  }).join("");
}

const INTENT_META = {
  "核实": ["材料存疑", "#8ab4ff"],
  "深挖": ["经历真实性", "#ffd97a"],
  "边界": ["能力上限", "#b79aff"],
  "风险": ["红线相关", "#ff7a9a"],
};

function renderInterviewHTML(data) {
  const qs = data.questions || [];
  const src = data.based_on || {};
  return `<div class="iv-meta">基于 ${src.disputes || 0} 条争议点、${src.excluded || 0} 条被剔除论断生成</div>
    ${qs.map(q => {
      const [label, color] = INTENT_META[q.intent] || ["核实", "#8ab4ff"];
      return `<div class="iv-q">
        <span class="iv-intent" style="color:${color};border-color:${color}">${esc(label)}</span>
        <div class="iv-body">
          <div class="iv-question">${esc(q.q)}</div>
          ${q.why ? `<div class="iv-why"><b>为什么问：</b>${esc(q.why)}</div>` : ""}
          ${q.probe ? `<div class="iv-probe"><b>听答要点：</b>${esc(q.probe)}</div>` : ""}
        </div>
      </div>`;
    }).join("")}`;
}

async function genInterview(candId, btn) {
  const box = $("iv-" + candId);
  if (!box || !state.job) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "生成中…";
  box.innerHTML = `<div class="hint">正在基于争议点生成追问（会调用一次模型，稍等）…</div>`;
  try {
    const r = await API.generateInterview(state.job.id, candId);
    if (!r.ok) throw new Error(r.error || "生成失败");
    box.innerHTML = renderInterviewHTML(r.interview);
    btn.textContent = "重新生成";
  } catch (e) {
    box.innerHTML = `<div class="hint warn">生成失败：${esc(e.message)}</div>`;
    btn.textContent = old;
  } finally {
    btn.disabled = false;
  }
}

function exportJSON() {
  const data = {
    job: state.job,
    candidates: state.candidates.filter(c => c.result).map(c => c.result),
  };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `talent_${(state.job.title || "job").replace(/\s+/g, "_")}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ===================== 评审报告（发给用人经理） =====================
   原始 JSON 只有机器看得懂。这里生成一份自包含 HTML：
   双击就能看，Word 也能打开，样式全内联不走外链。

   关键是把**方法论**写进报告：读的人得知道分数是怎么来的、
   哪些论断被判为无依据剔除了、分歧在哪，才可能信任这个结论。 */

function _repEsc(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/** 报告里的数字一律保留一位小数。
 *
 * 引擎的 _aggregate 已经 round(...,2)，但报告是要归档的文件——
 * 一旦哪天聚合逻辑变了、或者读到旧库里未舍入的历史值，
 * 表格里就会出现 `6.60000000000000005` 这种把浮点尾数直接印在纸上的结果。
 * 归档件宁可多一行防护。
 */
function _repNum(v, digits = 1) {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!isFinite(n)) return "—";
  return n.toFixed(digits);
}

/** 报告正文里的换行保留 + 「」引用高亮（与界面一致的阅读感） */
function _repText(s) {
  return _repEsc(s).replace(/「([^」]{1,200})」/g, '<mark class="q">「$1」</mark>')
                   .replace(/\n/g, "<br>");
}

function buildReportHTML() {
  const job = state.job || {};
  const jd = job.jd || {};
  const done = state.candidates.filter(c => c.status === "done" && c.result)
    .sort((a, b) => (b.result.total || 0) - (a.result.total || 0));
  const now = new Date().toLocaleString("zh-CN");

  const gradeCls = g => ({ A: "gA", B: "gB", C: "gC" }[g] || "");
  const list = arr => (arr || []).length
    ? `<ul>${arr.map(x => `<li>${_repText(x)}</li>`).join("")}</ul>`
    : `<p class="muted">（未填写）</p>`;

  // ---- 总览表 ----
  const overview = done.length ? `
    <table class="tbl">
      <thead><tr>
        <th style="width:38px">#</th><th style="text-align:left">候选人</th>
        <th>综合</th><th>等级</th>
        ${DIMENSIONS.map(d => `<th>${_repEsc(d)}</th>`).join("")}
        <th>评委分歧</th>
      </tr></thead>
      <tbody>${done.map((c, i) => {
        const sp = judgeSpread(c.result);
        const mx = Math.max(...Object.values(sp).filter(v => v != null), 0);
        const [lbl] = spreadLabel(mx);
        return `<tr>
          <td class="c">${i + 1}</td>
          <td><b>${_repEsc(c.name)}</b></td>
          <td class="c big">${_repNum(c.result.total)}</td>
          <td class="c"><span class="badge ${gradeCls(c.result.grade)}">${_repEsc(c.result.grade)}</span></td>
          ${DIMENSIONS.map(d => `<td class="c">${_repNum((c.result.scores || {})[d])}</td>`).join("")}
          <td class="c">${lbl}${mx ? ` ±${_repNum(mx)}` : ""}</td>
        </tr>`;
      }).join("")}</tbody>
    </table>` : `<p class="muted">还没有已出结论的候选人。</p>`;

  // ---- 逐人详情 ----
  const details = done.map((c, i) => {
    const r = c.result;
    const adj = r.debate?.adjudication || {};
    const judges = (r.judges || []).map(j => `<tr>
        <td>${_repEsc(j.name)}</td>
        ${DIMENSIONS.map(d => `<td class="c">${(j.scores || {})[d] ?? "—"}</td>`).join("")}
        <td class="cmt">${_repText(j.comment || "")}</td>
      </tr>`).join("");
    const iv = c.interview;
    const ivBlock = iv && (iv.questions || []).length ? `
      <h4>🎤 面试追问清单</h4>
      <ol class="iv">${iv.questions.map(q => `<li>
        <div class="iv-q">${_repEsc(q.q)}</div>
        ${q.why ? `<div class="iv-w"><b>为什么问：</b>${_repEsc(q.why)}</div>` : ""}
        ${q.probe ? `<div class="iv-p"><b>听答要点：</b>${_repEsc(q.probe)}</div>` : ""}
      </li>`).join("")}</ol>` : "";

    return `<section class="cand">
      <h3><span class="rank">#${i + 1}</span> ${_repEsc(c.name)}
        <span class="badge ${gradeCls(r.grade)}">${_repEsc(r.grade)}</span>
        <span class="score">${r.total}</span></h3>

      <h4>评委盲审明细</h4>
      <table class="tbl small">
        <thead><tr><th>评委</th>
          ${DIMENSIONS.map(d => `<th>${_repEsc(d)}</th>`).join("")}
          <th style="text-align:left">评语</th></tr></thead>
        <tbody>${judges}</tbody>
      </table>

      <h4>🧾 反幻觉审计 —— 裁判剔除的无原文支撑论断</h4>
      ${(adj.excluded || []).length
        ? `<ul class="audit">${adj.excluded.map(x => `<li>${_repText(x)}</li>`).join("")}</ul>
           <p class="muted">这些论断在辩论中出现过，但简历/测评原文并不支持，已被裁判剔除。</p>`
        : `<p class="muted">本次评审没有发现明显的无依据论断。</p>`}

      <h4>⚖️ 核心争议点</h4>
      ${(adj.disputes || []).length
        ? `<ul class="disp">${adj.disputes.map(x => `<li>${_repText(x)}</li>`).join("")}</ul>`
        : `<p class="muted">（无明显争议）</p>`}

      ${ivBlock}

      ${(r.tags || []).length
        ? `<p class="tags">标签：${r.tags.map(t => `<span class="tag">${_repEsc(t)}</span>`).join(" ")}</p>`
        : ""}
    </section>`;
  }).join("");

  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>评审报告 · ${_repEsc(job.title || "岗位")}</title>
<style>
  *{box-sizing:border-box}
  body{font-family:"Microsoft YaHei UI","PingFang SC",system-ui,sans-serif;
       max-width:940px;margin:0 auto;padding:38px 30px 60px;color:#1c2340;
       line-height:1.75;background:#fff;font-size:14px}
  h1{font-size:25px;margin:0 0 6px;letter-spacing:.3px}
  h2{font-size:17px;margin:34px 0 12px;padding-bottom:7px;border-bottom:2px solid #e6e9f5}
  h3{font-size:16px;margin:26px 0 10px;display:flex;align-items:center;gap:9px}
  h4{font-size:13.5px;margin:18px 0 7px;color:#4a5578}
  .meta{color:#7b86a8;font-size:12.5px;margin-bottom:22px}
  .rank{color:#aab3d0;font-weight:700;font-size:14px}
  .score{font-size:19px;font-weight:800;color:#1c2340;margin-left:auto}
  .badge{display:inline-block;min-width:24px;text-align:center;padding:2px 8px;
         border-radius:6px;font-size:12px;font-weight:700;color:#fff}
  .gA{background:#2fa06b}.gB{background:#3f7ad6}.gC{background:#c2703f}
  .muted{color:#8b93ad;font-size:12.5px}
  .tbl{width:100%;border-collapse:collapse;margin:9px 0;font-size:13px}
  .tbl th{background:#f4f6fc;color:#4a5578;font-weight:700;padding:8px 10px;
          border:1px solid #e3e7f3;font-size:12.5px;text-align:center}
  .tbl td{padding:8px 10px;border:1px solid #e9ecf6;vertical-align:top}
  .tbl .c{text-align:center}
  .tbl .big{font-weight:800;font-size:15px}
  .tbl .cmt{font-size:12.5px;color:#4a5578;max-width:340px}
  .tbl.small{font-size:12.5px}
  ul,ol{margin:7px 0;padding-left:22px}
  li{margin-bottom:5px}
  ul.audit{background:#fff5f7;border-left:3px solid #e0738f;padding:11px 14px 11px 32px;
           border-radius:0 8px 8px 0;color:#7d4055}
  ul.disp{background:#f4f7ff;border-left:3px solid #6f9be8;padding:11px 14px 11px 32px;
          border-radius:0 8px 8px 0;color:#33456e}
  ol.iv{padding-left:20px}
  ol.iv > li{margin-bottom:14px}
  .iv-q{font-weight:600;color:#1c2340}
  .iv-w,.iv-p{font-size:12.5px;color:#5c668a;margin-top:4px}
  .iv-p b{color:#b8860b}
  .tags{margin-top:14px}
  .tag{display:inline-block;background:#eef1fa;color:#4a5578;padding:2px 9px;
       border-radius:20px;font-size:11.5px;margin-right:5px}
  mark.q{background:#fff3cd;padding:0 3px;border-radius:3px}
  section.cand{border:1px solid #e6e9f5;border-radius:12px;padding:18px 20px;margin:16px 0;
               page-break-inside:avoid}
  .method{background:#f8f9fd;border:1px solid #e6e9f5;border-radius:12px;
          padding:16px 20px;font-size:12.5px;color:#4a5578;margin-top:14px}
  .method ol{margin:8px 0 0;padding-left:20px}
  .method li{margin-bottom:4px}
  .toolbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
           background:#eef1fa;border:1px solid #dfe4f3;border-radius:10px;
           padding:11px 15px;margin-bottom:20px;font-size:12.5px;color:#4a5578}
  .toolbar button{font:inherit;font-weight:700;color:#fff;background:#3f7ad6;
                  border:0;border-radius:8px;padding:8px 16px;cursor:pointer}
  .toolbar button:hover{background:#3468b8}
  @media print{
    /* 归档场景：打印出来要能直接放进候选人档案。
       几条都是踩过的坑：背景色默认不打印（等级徽章会变白框）、
       卡片跨页会被拦腰切断、表头翻页后消失、标题落在页尾成孤儿。 */
    html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
    body{padding:0;max-width:none;font-size:12pt;background:#fff;color:#000}
    .no-print{display:none !important}
    a[href]:after{content:""}
    h1{font-size:18pt}
    h2{font-size:14pt;break-after:avoid;page-break-after:avoid}
    h3,h4{break-after:avoid;page-break-after:avoid}
    section.cand{border-color:#bbb;break-inside:avoid;page-break-inside:avoid;
                 margin:10pt 0;padding:12pt 14pt}
    .tbl{font-size:9.5pt}
    .tbl thead{display:table-header-group}
    .tbl tr{break-inside:avoid;page-break-inside:avoid}
    .tbl th{background:#eef1fa !important}
    ul.audit,ul.disp,.method,-mark.q{background:transparent !important}
    mark.q{background:#ffe9a8 !important}
    /* 每位候选人另起一页，档案按人拆开归档时最省事 */
    section.cand + section.cand{break-before:page;page-break-before:always}
    .method{break-inside:avoid;page-break-inside:avoid}
  }
  @page{margin:16mm 14mm}
</style></head><body>

<div class="toolbar no-print">
  <button type="button" onclick="window.print()">🖨 打印 / 另存为 PDF</button>
  <span>在打印对话框里选「另存为 PDF」即可归档；每位候选人会自动另起一页。</span>
</div>

<h1>候选人评审报告</h1>
<div class="meta">
  岗位：<b>${_repEsc(job.title || "未命名岗位")}</b> ·
  参评 ${done.length} 人 ·
  生成时间 ${_repEsc(now)}
</div>

<h2>一、岗位要求</h2>
<p><b>概述：</b>${_repText(jd.summary || "（未填写）")}</p>
<p><b>硬性要求</b>（不满足即淘汰）</p>${list(jd.requirements)}
<p><b>加分项</b></p>${list(jd.preferred)}
<p><b>风险红线</b></p>${list(jd.red_flags)}

<h2>二、结论总览</h2>
${overview}

<h2>三、逐人详情</h2>
${details || `<p class="muted">暂无已出结论的候选人。</p>`}

<h2>四、评审方法</h2>
<div class="method">
本报告结论由多 Agent 对抗式流程产生，不是单次模型打分：
<ol>
  <li><b>正反立论</b>：一方主张录用、一方主张淘汰，各自引用候选人原始材料作为证据。</li>
  <li><b>两轮互驳</b>：双方针对对方的论据逐条反驳，暴露材料中含糊或站不住的地方。</li>
  <li><b>裁判剔除幻觉</b>：裁定裁判逐条检查双方论断，<b>把简历/测评原文并不支持的论断剔除</b>，
      被剔除的内容如实列在每位候选人的「反幻觉审计」中，不作为评分依据。</li>
  <li><b>三评委独立盲审</b>：三位评委独立打分，互不可见，避免相互影响。</li>
  <li><b>中位数聚合</b>：每个维度取三位评委的<b>中位数</b>（而非平均分），以抵抗单个评委的极端打分。</li>
  <li><b>加权定级</b>：按岗位各维度权重加权得综合分；A ≥ 8.0，B ≥ 6.5，C &lt; 6.5。</li>
</ol>
<p style="margin-top:10px">
<b>「评委分歧」</b>指三位评委在同一维度上的极差。分歧大说明该候选人在不同评委手里评价差异明显，
建议在面试中重点验证相关维度，而不是直接采信综合分。
</p>
<p style="margin-top:10px">
<b>本报告的作用是提供可追溯的判断依据，不替代用人决策。</b>
所有分数均可回溯到具体评委评语、争议点与被剔除论断，请结合面试综合判断。
</p>
</div>

</body></html>`;
}

function exportReport() {
  const done = state.candidates.filter(c => c.status === "done" && c.result);
  if (!done.length) { toast("还没有已出结论的候选人，先跑一次评审", "info"); return; }
  const html = buildReportHTML();
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `评审报告_${(state.job?.title || "job").replace(/\s+/g, "_")}_${new Date().toISOString().slice(0, 10)}.html`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast("报告已导出（双击可用浏览器打开，也可用 Word 打开）", "ok");
}

/** 直接打印 / 另存为 PDF。
 *
 * 为什么不复用导出的 HTML 文件：归档场景里没人愿意"先存一个 html 再双击再打印"。
 * 这里开一个新窗口写同一份 HTML，等它渲染好再调 print()。
 * 打印对话框里的「另存为 PDF」就是 PDF 导出——不引入任何依赖，
 * 也不用为了 docx 破坏零依赖约束。
 */
function printReport() {
  const done = state.candidates.filter(c => c.status === "done" && c.result);
  if (!done.length) { toast("还没有已出结论的候选人，先跑一次评审", "info"); return; }
  const w = window.open("", "_blank");
  if (!w) {
    toast("浏览器拦截了新窗口。允许弹窗后重试，或先「导出评审报告」再打开打印", "err");
    return;
  }
  w.document.open();
  w.document.write(buildReportHTML());
  w.document.close();
  // 等样式/字体就绪再打印：立刻 print() 在部分浏览器会打出空白页。
  // 加个一次性标记，避免和 onload 重复弹两次打印对话框。
  let printed = false;
  const fire = () => {
    if (printed) return;
    printed = true;
    try { w.focus(); w.print(); } catch (e) { /* 用户自己按 Ctrl+P */ }
  };
  w.onload = fire;
  setTimeout(fire, 600);
}

/* ===================== 岗位要求录入 / 结构化 ===================== */
/** 步骤条：1 = 还在录入，2 = 已结构化结果、可以核对 */
function setJdStep(n) {
  const s1 = $("jdStep1"), s2 = $("jdStep2");
  if (s1) s1.className = "step" + (n >= 1 ? " on" : "");
  if (s2) s2.className = "step" + (n >= 2 ? " on" : "");
}

function openNewJob() {
  state.editingJobId = null;
  state.jdFiles = [];
  $("jdModalTitle").textContent = "新建岗位";
  $("jdTitle").value = "";
  $("jdRaw").value = "";
  $("jdFilesList").innerHTML = "";
  $("jdStructMsg").textContent = "";
  $("jdStructMsg").className = "wb-status";
  $("jdEdit").classList.add("hidden");
  $("jdSaveBtn").textContent = "保存岗位";
  renderJdTemplates();
  setJdStep(1);
  $("jdModal").classList.remove("hidden");
  setTimeout(() => $("jdTitle").focus(), 60);
}

function openJdEditor() {
  const j = state.job;
  state.editingJobId = j.id;
  state.jdFiles = [];
  const jd = j.jd || {};
  $("jdModalTitle").textContent = "编辑岗位要求";
  $("jdTitle").value = j.title || "";
  $("jdRaw").value = jd.raw_text || "";
  $("jdFilesList").innerHTML = "";
  $("jdStructMsg").textContent = "";
  $("jdStructMsg").className = "wb-status";
  $("jdSaveBtn").textContent = "保存修改";
  fillJdEditor(jd);
  renderJdTemplates();
  $("jdEdit").classList.remove("hidden");
  setJdStep(2);
  $("jdModal").classList.remove("hidden");
}

function closeJd() { $("jdModal").classList.add("hidden"); }

function fillJdEditor(jd) {
  $("jdSummary").value = jd.summary || "";
  fillList("jdReq", jd.requirements || []);
  fillList("jdPref", jd.preferred || []);
  fillList("jdRed", jd.red_flags || []);
  const w = jd.weights || DEFAULT_WEIGHTS;
  $("jdWeights").innerHTML = DIMENSIONS.map((d, i) =>
    `<label class="wfield">${esc(d)}
      <input type="number" min="0" max="10" step="0.05" data-dim="${esc(d)}" value="${w[d] != null ? w[d] : ""}">
    </label>`).join("");
}

function fillList(id, items) {
  $(id).innerHTML = items.map(v =>
    `<div class="li"><input type="text" value="${esc(v)}">
      <button class="btn tiny danger-ghost" type="button" onclick="this.parentNode.remove()">✕</button></div>`).join("");
}

function addListItem(id) {
  const d = document.createElement("div");
  d.className = "li";
  d.innerHTML = `<input type="text" placeholder="输入一条后可直接编辑">
    <button class="btn tiny danger-ghost" type="button" onclick="this.parentNode.remove()">✕</button>`;
  $(id).appendChild(d);
  d.querySelector("input").focus();
}

function readList(id) {
  return Array.from($(id).querySelectorAll("input"))
    .map(i => i.value.trim()).filter(Boolean);
}

function readWeights() {
  const out = {};
  $("jdWeights").querySelectorAll("input").forEach(i => {
    const v = parseFloat(i.value);
    out[i.dataset.dim] = isNaN(v) ? 0 : v;
  });
  return out;
}

/* ---- 文件选择（JD） ---- */
function onJdFilesPicked(input) {
  const files = Array.from(input.files || []);
  input.value = "";
  if (!files.length) return;
  readFilesToB64(files).then(list => {
    state.jdFiles = state.jdFiles.concat(list);
    renderFileList("jdFilesList", state.jdFiles, "jdFiles");
  }).catch(e => toast("读取文件失败：" + e.message, "err"));
}

async function structureJd() {
  const msg = $("jdStructMsg");
  const btn = $("jdStructBtn");
  const raw = $("jdRaw").value.trim();
  if (!raw && !state.jdFiles.length) {
    msg.className = "wb-status bad";
    msg.textContent = "请先粘贴招聘要求，或上传文档/截图。";
    return;
  }
  btn.disabled = true;
  msg.className = "wb-status";
  msg.textContent = "⏳ 正在让模型把要求拆成硬性要求 / 优先项 / 风险红线…";
  try {
    let r;
    if (state.editingJobId) {
      r = await API.structureJd(state.editingJobId, { raw_text: raw, files: state.jdFiles });
    } else {
      // 还没建岗位：先建一个草稿再结构化，成功后留在编辑态
      const created = await API.createJob({ title: $("jdTitle").value.trim() || "未命名岗位" });
      state.editingJobId = created.job.id;
      r = await API.structureJd(state.editingJobId, { raw_text: raw, files: state.jdFiles });
    }
    if (!r.ok) throw new Error(r.error || "结构化失败");
    fillJdEditor(r.jd);
    $("jdEdit").classList.remove("hidden");
    setJdStep(2);
    msg.className = "wb-status good";
    msg.textContent = "✓ 已结构化。请核对并微调下面的条目，然后保存。";
  } catch (e) {
    msg.className = "wb-status bad";
    msg.textContent = "✗ " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function saveJd() {
  const title = $("jdTitle").value.trim() || "未命名岗位";
  const jd = {
    summary: $("jdSummary").value.trim(),
    requirements: readList("jdReq"),
    preferred: readList("jdPref"),
    red_flags: readList("jdRed"),
    weights: readWeights(),
    raw_text: $("jdRaw").value.trim(),
  };
  try {
    if (state.editingJobId) {
      await API.updateJob(state.editingJobId, { title: title, jd: jd });
      toast("已保存", "ok");
      closeJd();
      if (state.job && state.job.id === state.editingJobId) await openJob(state.job.id);
      else await loadJobs();
    } else {
      const r = await API.createJob({ title: title, jd: jd });
      toast("岗位已创建", "ok");
      closeJd();
      await loadJobs();
      await openJob(r.job.id);
    }
  } catch (e) {
    toast("保存失败：" + e.message, "err");
  }
}

async function confirmDeleteJob() {
  if (!state.job) return;
  if (!confirm(`删除岗位「${state.job.title}」？\n所有候选人素材与评审结果都会一并删除。`)) return;
  await API.deleteJob(state.job.id);
  toast("岗位已删除", "ok");
  goHome();
}

/* ===================== 上传候选人素材 ===================== */
function openUpload() {
  state.upFiles = [];
  $("upName").value = "";
  $("upFilesList").innerHTML = "";
  $("upMsg").textContent = "";
  $("upModal").classList.remove("hidden");
}
function closeUpload() { $("upModal").classList.add("hidden"); }

function onUpFilesPicked(input) {
  const files = Array.from(input.files || []);
  input.value = "";
  if (!files.length) return;
  readFilesToB64(files).then(list => {
    state.upFiles = state.upFiles.concat(list);
    renderFileList("upFilesList", state.upFiles, "upFiles");
  }).catch(e => toast("读取文件失败：" + e.message, "err"));
}

function renderFileList(elId, list, storeKey) {
  $(elId).innerHTML = list.map((f, i) => {
    const kb = Math.round((f.size || 0) / 1024);
    const isAudio = /\.(mp3|wav|m4a|mp4|ogg|webm)$/i.test(f.filename || "");
    const role = f.role === "assessment" ? "测评"
      : isAudio ? "录音"
      : f.role === "transcript" ? "转写" : "简历";
    return `<div class="file-row">
      <span class="fname">${esc(f.filename)}</span>
      <span class="fmeta">${role} · ${kb}KB</span>
      <button class="btn tiny danger-ghost" onclick="removeFile('${storeKey}',${i})">✕</button>
    </div>`;
  }).join("");
}

function removeFile(storeKey, i) {
  state[storeKey].splice(i, 1);
  renderFileList(storeKey === "jdFiles" ? "jdFilesList" : "upFilesList", state[storeKey], storeKey);
}

/** 读成 base64（不引 FormData：后端是 base64 JSON，本地场景足够且简单）。 */
function readFilesToB64(files) {
  return Promise.all(files.map(f => new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result);
      const b64 = s.slice(s.indexOf(",") + 1);
      resolve({ filename: f.name, size: f.size, data_base64: b64, role: guessRole(f.name) });
    };
    r.onerror = () => reject(new Error(f.name + " 读取失败"));
    r.readAsDataURL(f);
  })));
}

function guessRole(name) {
  const low = (name || "").toLowerCase();
  if (/测评|评测|笔试|机试|assessment|test|exam|logic/.test(low)) return "assessment";
  if (/转写|面试|逐字稿|transcript|interview/.test(low)) return "transcript";
  if (/\.(mp3|wav|m4a|mp4|ogg|webm)$/.test(low)) return "transcript";  // 录音的内容就是转写稿
  return "resume";
}

async function doUpload() {
  if (!state.upFiles.length) { toast("请先选择文件", "info"); return; }
  const btn = $("upBtn"), msg = $("upMsg");
  btn.disabled = true;
  msg.className = "wb-status";
  msg.textContent = "⏳ 正在上传并解析（图片走视觉模型 OCR，可能稍慢）…";
  try {
    const name = $("upName").value.trim();
    const r = await API.uploadCandidates(state.job.id, {
      candidates: [{ name: name, files: state.upFiles }],
    });
    if (!r.ok) throw new Error(r.error || "上传失败");
    msg.className = r.errors && r.errors.length ? "wb-status bad" : "wb-status good";
    msg.textContent = `✓ 已加入：${(r.added || []).join("、") || "无"}`
      + (r.errors && r.errors.length ? `　⚠ ${r.errors.join("；")}` : "");
    state.upFiles = [];
    renderFileList("upFilesList", state.upFiles, "upFiles");
    await openJob(state.job.id);
    if (!r.errors || !r.errors.length) setTimeout(closeUpload, 900);
  } catch (e) {
    msg.className = "wb-status bad";
    msg.textContent = "✗ " + e.message;
  } finally {
    btn.disabled = false;
  }
}

/* ===================== 模型设置 ===================== */
const PROVIDER_DEFAULTS = {
  workbuddy: { base: "https://copilot.tencent.com/v2", model: "deepseek-v4-pro" },
  deepseek:  { base: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  qwen:      { base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  ollama:    { base: "http://localhost:11434/v1", model: "qwen2.5:7b" },
  "":        { base: "", model: "" },
};
let WB_MODELS = [];

function openSettings() {
  $("settingsModal").classList.remove("hidden");
  const saved = JSON.parse(localStorage.getItem("talent_screen.model_cfg") || "{}");
  $("cfgProvider").value = saved.provider || "workbuddy";
  onProviderChange({ applyDefaults: false });
  if (saved.base_url) $("cfgBase").value = saved.base_url;
  if (saved.model) $("cfgModel").value = saved.model;
  setModelOptions(WB_MODELS);
  loadRubric();
}

/* 口径由什么组成。只显示一个版本号的话，用户看到版本变了也不知道
   是改了权重、换了模型、还是动了一句提示词——于是只能选择忽略它。 */
async function loadRubric() {
  const box = $("cfgRubric");
  if (!box) { return; }
  try {
    const r = await API.rubric();
    const d = r.rubric || {};
    const w = d.weights || {};
    const th = d.thresholds || {};
    const cc = d.concurrency || {};
    const ccTxt = Object.keys(cc).length
      ? Object.keys(cc).sort().map((k) => `${esc(k)} ${esc(cc[k])}`).join(" · ")
      : "—";
    const dims = (d.dimensions || []).map((k) => `${esc(k)} ${w[k]}`).join(" · ");
    box.innerHTML = `
      <div class="rubric-ver"><code>${esc(d.version || "—")}</code>
        <span class="muted">由下列各项共同哈希得出</span></div>
      <dl class="rubric-dl">
        <dt>维度与权重</dt><dd>${dims || "—"}</dd>
        <dt>档位阈值</dt><dd>A ≥ ${esc(th.A)} · B ≥ ${esc(th.B)}</dd>
        <dt>提示词指纹</dt><dd><code>${esc(d.prompt_fingerprint || "—")}</code></dd>
        <dt>辩论 / 评委</dt><dd>${esc(d.debate_rounds)} 轮 · ${esc(d.judge_count)} 位 · ${esc(d.aggregation)}</dd>
        <dt>模型</dt><dd>${esc(d.provider || "—")} / ${esc(d.model || "—")}</dd>
        <dt>在飞请求上限</dt><dd class="muted">${ccTxt}</dd>
      </dl>
      <p class="hint">改动提示词的任何一个字，版本号都会变——所以"这次和上次是同口径吗"
        这个问题可以被回答，而不是靠人记得。</p>`;
  } catch (e) {
    box.innerHTML = `<span class="muted">读取失败：${esc(e.message || "")}</span>`;
  }
}
function closeSettings() {
  localStorage.setItem("talent_screen.model_cfg", JSON.stringify({
    provider: $("cfgProvider").value,
    base_url: $("cfgBase").value.trim(),
    model: $("cfgModel").value.trim(),
  }));
  API.setModelConfig({
    provider: $("cfgProvider").value,
    base_url: $("cfgBase").value.trim(),
    model: $("cfgModel").value.trim(),
    api_key: $("cfgKey").value.trim(),
  }).catch(() => {});
  $("settingsModal").classList.add("hidden");
  toast("设置已保存（API Key 仅本次会话有效）", "ok");
  renderModelBadge();
}
function cancelSettings() { $("settingsModal").classList.add("hidden"); }

function onProviderChange(opts) {
  const apply = !opts || opts.applyDefaults !== false;
  const p = $("cfgProvider").value;
  if (apply && PROVIDER_DEFAULTS[p]) {
    $("cfgBase").value = PROVIDER_DEFAULTS[p].base;
    $("cfgModel").value = PROVIDER_DEFAULTS[p].model;
    setModelOptions([]);
  }
  const isWb = p === "workbuddy";
  $("cfgKey").disabled = isWb;
  $("cfgKey").placeholder = isWb ? "无需填写：使用 WorkBuddy 本机登录积分" : "sk-...";
  $("cfgKeyRow").classList.toggle("dimmed", isWb);
  $("cfgWorkBuddyHint").classList.toggle("hidden", !isWb);
  if (isWb) refreshWorkBuddyStatus();
}

async function refreshWorkBuddyStatus() {
  const el = $("cfgWorkBuddyStatus"), credEl = $("cfgWorkBuddyCredits");
  el.textContent = "⏳ 检测 WorkBuddy 登录状态…";
  el.className = "wb-status";
  try {
    const r = await API.workbuddy();
    const st = r.status || {};
    if (!st.ok) {
      el.textContent = "✗ " + (st.error || "WorkBuddy 通道不可用");
      el.className = "wb-status bad";
      credEl.classList.add("hidden");
      return;
    }
    el.textContent = "✓ 已登录：" + (st.nickname || "(已登录)")
      + (st.domain ? "（" + st.domain + "）" : "");
    el.className = "wb-status good";
    WB_MODELS = r.models || [];
    setModelOptions(WB_MODELS);

    if (r.credits && !r.credits.error) {
      credEl.classList.remove("hidden");
      credEl.innerHTML = renderCredits(r.credits);
    } else {
      credEl.classList.add("hidden");
    }

    // 关键合规提示：这条通道复用本机桌面端登录态，不能用于对外部署
    if (r.notice) {
      el.textContent += " · " + r.notice;
    }
  } catch (e) {
    el.textContent = "✗ " + e.message;
    el.className = "wb-status bad";
  }
}

function renderCredits(c) {
  const pct = c.total_size > 0 ? Math.round(c.total_remain / c.total_size * 100) : 0;
  const rows = (c.packages || []).map(p =>
    `<li><span>${esc(p.name)}${p.monthly ? "（月度）" : ""}</span><span>${p.remain} / ${p.size}</span></li>`).join("");
  return `<div class="wb-credits-head">剩余积分 <b>${c.total_remain}</b> / ${c.total_size}
      （已用 ${c.total_used}）</div>
    <div class="wb-credits-bar"><i style="width:${pct}%"></i></div>
    ${rows ? `<ul class="wb-credits-pkgs">${rows}</ul>` : ""}`;
}

function setModelOptions(models) {
  const sel = $("cfgModelPick");
  if (!sel) return;
  const cur = ($("cfgModel").value || "").trim();
  const list = (models || []).filter(m => m && (typeof m === "string" ? m : m.id));
  sel.innerHTML = `<option value="">${list.length
    ? "— 选择模型（共 " + list.length + " 个）—" : "— 暂无列表，点「读取可用模型」—"}</option>`;
  list.forEach(m => {
    const id = typeof m === "string" ? m : m.id;
    const note = (typeof m === "object" && m.note) ? m.note
      : (typeof m === "object" && m.multiplier === 0) ? "免费"
      : (typeof m === "object" && m.multiplier != null) ? "x" + m.multiplier + " 积分" : "";
    const nm = (typeof m === "object" && m.name) ? m.name : id;
    const o = document.createElement("option");
    o.value = id;
    o.textContent = (nm && nm !== id ? nm + " · " : "") + id + (note ? "（" + note + "）" : "");
    sel.appendChild(o);
  });
  sel.value = list.some(m => ((typeof m === "string") ? m : m.id) === cur) ? cur : "";
}

function onPickModel() { const v = $("cfgModelPick").value; if (v) $("cfgModel").value = v; }

function probePayload(action) {
  // 返回对象，不是字符串——api.js 会自己 JSON.stringify，
  // 这里再 stringify 一次会变成双重编码，后端拿到的是字符串而不是字段。
  return {
    action: action,
    provider: $("cfgProvider").value || null,
    base_url: $("cfgBase").value.trim(),
    api_key: $("cfgKey").value.trim(),
    model: ($("cfgModel").value || "").trim(),
  };
}

async function probeModels() {
  const btn = $("cfgListBtn"), el = $("cfgTestResult");
  btn.disabled = true; el.className = "wb-status"; el.textContent = "⏳ 读取可用模型…";
  try {
    const r = await API.probe(probePayload("models"));
    const res = r.result || {};
    if (r.ok && res.ok && (res.models || []).length) {
      setModelOptions(res.models);
      el.className = "wb-status good";
      el.textContent = "✓ 读取到 " + res.models.length + " 个模型，可在上方下拉选择";
    } else {
      el.className = "wb-status bad";
      el.textContent = "✗ 读取失败 · " + (res.error || r.error || "未知错误");
    }
  } catch (e) { el.className = "wb-status bad"; el.textContent = "✗ " + e.message; }
  finally { btn.disabled = false; }
}

const PROBE_REASONS = {
  "out-of-credit": "余额/积分不足", "not-signed-in": "未登录或会话已失效",
  "rate-limited": "被限流，请稍后再试", "network": "网络不通",
  "server": "服务端异常（5xx）", "auth": "API Key 无效或已过期",
  "model-unavailable": "模型不存在或不可用",
};

async function probeTest() {
  const btn = $("cfgTestBtn"), el = $("cfgTestResult");
  const model = ($("cfgModel").value || "").trim();
  btn.disabled = true; el.className = "wb-status";
  el.textContent = "⏳ 测试连通性…";
  try {
    const r = await API.probe(probePayload("test"));
    const t = r.result || {};
    if (r.ok && t.ok) {
      el.className = "wb-status good";
      el.textContent = "✓ 该 API 可用 · " + t.model + " · " + t.latency_ms + "ms"
        + (t.reply ? " · 回复：" + t.reply : "");
    } else {
      el.className = "wb-status bad";
      el.textContent = "✗ 该 API 不可用 · " + (PROBE_REASONS[t.kind] || t.error || "未知错误");
    }
  } catch (e) { el.className = "wb-status bad"; el.textContent = "✗ " + e.message; }
  finally { btn.disabled = false; }
}

/* ===================== 星空背景（装饰） =====================
   克制的原则：**绝大部分星星是静止的**，只有约一成会极缓慢地"呼吸"，
   且用正弦而不是线性+反弹——满屏同时闪烁会把人的余光一直勾住，看久了很累。 */
function bootStarfield() {
  const cv = $("starfield");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

  let stars = [], W = 0, H = 0, raf = null;

  function build() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight;
    cv.width = W * dpr; cv.height = H * dpr;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const n = Math.round(W * H / 11000);
    stars = Array.from({ length: n }, () => {
      const twinkles = !REDUCED && Math.random() < 0.10;   // 只有一成会呼吸
      return {
        x: Math.random() * W,
        y: Math.random() * H,
        r: Math.random() * 1.15 + 0.25,
        base: 0.16 + Math.random() * 0.42,                       // 静止亮度
        amp: twinkles ? 0.06 + Math.random() * 0.10 : 0,         // 幅度很小：微微起伏而非忽明忽暗
        phase: Math.random() * Math.PI * 2,
        speed: 0.00035 + Math.random() * 0.00045,                // 周期约 8~18 秒
      };
    });
  }

  function draw(t) {
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#dfe7ff";
    for (const s of stars) {
      const a = s.amp ? s.base + s.amp * Math.sin(t * s.speed + s.phase) : s.base;
      ctx.globalAlpha = a < 0 ? 0 : (a > 1 ? 1 : a);
      ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.2832); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function loop(t) { draw(t); raf = requestAnimationFrame(loop); }
  function start() { if (raf === null) raf = requestAnimationFrame(loop); }
  function stop() { if (raf !== null) { cancelAnimationFrame(raf); raf = null; } }

  addEventListener("resize", () => { build(); if (raf === null) draw(performance.now()); });
  build();

  if (REDUCED) { draw(0); return; }   // 系统要求减少动效：画一张静态星空就收工

  // 切到后台就停：省电，也避免回来时看到一个"憋了很久"的跳变
  document.addEventListener("visibilitychange", () => { document.hidden ? stop() : start(); });
  start();
}

/* ===================== 入场 boot：右上角主星划亮星空 =====================
   一颗亮白主星从右上角旋转着滑向屏幕左中，身后拖着光尾；
   光尾扫过的位置背景星点逐一点亮；走完后主星在终点炸开，光晕扩散，overlay 淡出。
   任何时刻点击 / 按键都能跳过——不能让动画把人挡在门外。 */
function bootIntro() {
  const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const intro = $("intro");
  const cv = $("introCanvas");
  if (!intro || !cv) return;

  const ctx = cv.getContext("2d");
  let W, H, DPR, done = false;

  /**
   * 画布每帧都要重绘背景（不能 clearRect 留透明），
   * 填色必须与 body / #intro 的 CSS 底色**完全一致**——
   * 否则星星划完切到主界面时会看到颜色跳变。
   * 这里把 :root 的 --bg-layers 用 canvas 渐变一一对应画出来。
   */
  function paintBg() {
    const lg = ctx.createLinearGradient(0, 0, 0, H);
    lg.addColorStop(0, "#0b1026");          // var(--bg-1)
    lg.addColorStop(1, "#050816");          // var(--bg-0)
    ctx.fillStyle = lg;
    ctx.fillRect(0, 0, W, H);

    const spread = Math.max(W, H);
    const g1 = ctx.createRadialGradient(W * 0.20, H * 0.10, 0, W * 0.20, H * 0.10, spread * 0.60);
    g1.addColorStop(0, "rgba(90,80,200,.18)");
    g1.addColorStop(1, "rgba(90,80,200,0)");
    ctx.fillStyle = g1;
    ctx.fillRect(0, 0, W, H);

    const g2 = ctx.createRadialGradient(W * 0.85, H * 0.90, 0, W * 0.85, H * 0.90, spread * 0.55);
    g2.addColorStop(0, "rgba(255,150,120,.08)");
    g2.addColorStop(1, "rgba(255,150,120,0)");
    ctx.fillStyle = g2;
    ctx.fillRect(0, 0, W, H);
  }

  function size() {
    DPR = window.devicePixelRatio || 1;
    W = window.innerWidth; H = window.innerHeight;
    cv.width = W * DPR; cv.height = H * DPR;
    cv.style.width = W + "px"; cv.style.height = H + "px";
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    paintBg();
  }
  size();
  window.addEventListener("resize", size);

  function finish() {
    if (done) return;
    done = true;
    intro.classList.add("gone");
    document.querySelectorAll(".pre-intro").forEach(el => el.classList.add("on"));
    setTimeout(() => { if (intro.parentNode) intro.remove(); }, 700);
  }

  if (REDUCED) { finish(); return; }
  addEventListener("keydown", finish, { once: true });
  intro.addEventListener("click", finish);
  // 兜底：无论动画是否正常跑完，4.5s 后一定收场（脚本出错也不会卡在黑色遮罩上）
  setTimeout(finish, 4500);

  // 动画参数
  const START = { x: 0.92, y: 0.08 };
  const END = { x: 0.08, y: 0.62 };
  const COLOR = [255, 250, 230];
  const DURATION = 1800;
  const TRAIL = 0.30;
  const WIDTH = 3;

  const stars = [];
  for (let i = 0; i < 240; i++) {
    stars.push({
      x: Math.random(), y: Math.random(),
      r: 0.4 + Math.random() * 1.0,
      lit: false, litT: 0, tw: Math.random() * 6.28,
    });
  }

  function pathPoint(t) {
    const sx = START.x * W, sy = START.y * H;
    const ex = END.x * W, ey = END.y * H;
    return [sx + (ex - sx) * t, sy + (ey - sy) * t];
  }

  function drawStar(x, y, now) {
    const pulse = 1 + 0.1 * Math.sin(now * 0.007);
    const flash = 0.85 + 0.15 * Math.sin(now * 0.007);
    const r = 13 * pulse;
    const g = ctx.createRadialGradient(x, y, 0, x, y, r * 4);
    g.addColorStop(0, `rgba(255,255,255,${0.7 + 0.3 * flash})`);
    g.addColorStop(0.3, `rgba(${COLOR[0]},${COLOR[1]},${COLOR[2]},${0.4 + 0.3 * flash})`);
    g.addColorStop(1, "rgba(255,250,230,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(x, y, r * 4, 0, 6.28); ctx.fill();

    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(now * 0.004);           // 十字星芒缓慢旋转
    ctx.strokeStyle = `rgba(255,255,245,${0.5 + 0.4 * flash})`;
    ctx.lineWidth = 1.5;
    for (let i = 0; i < 4; i++) {
      ctx.rotate(Math.PI / 2);
      ctx.beginPath(); ctx.moveTo(-r * 1.6, 0); ctx.lineTo(r * 1.6, 0); ctx.stroke();
    }
    ctx.restore();

    ctx.fillStyle = `rgba(255,255,255,${0.7 + 0.3 * flash})`;
    ctx.beginPath(); ctx.arc(x, y, 3 * pulse, 0, 6.28); ctx.fill();
  }

  const t0 = performance.now();
  function frame(now) {
    if (done) return;
    const t = Math.min(1, (now - t0) / DURATION);

    paintBg();          // 与主界面同一套底色
    const [hx, hy] = pathPoint(t);

    // 主星扫过 → 点亮附近星点
    for (const s of stars) {
      if (!s.lit && Math.hypot(s.x * W - hx, s.y * H - hy) < 70) {
        s.lit = true; s.litT = now;
      }
    }
    for (const s of stars) {
      // 入场期间的未点亮星点：极慢极轻的起伏（周期约 10s），
      // 主要演出交给"主星扫过→逐点点亮"，不是靠闪烁抓注意力
      let a = 0.12 + 0.05 * Math.sin(now * 0.0006 + s.tw);
      if (s.lit) a = 0.15 + 0.85 * Math.min(1, (now - s.litT) / 500);
      ctx.fillStyle = `rgba(255,250,235,${a})`;
      ctx.beginPath(); ctx.arc(s.x * W, s.y * H, s.r, 0, 6.28); ctx.fill();
    }

    // 拖尾（36 段 alpha 递减）
    const steps = 36;
    for (let i = 0; i < steps; i++) {
      const t1 = Math.max(0, t - TRAIL * i / steps);
      const t2 = Math.max(0, t - TRAIL * (i + 1) / steps);
      const [x1, y1] = pathPoint(t1), [x2, y2] = pathPoint(t2);
      ctx.strokeStyle = `rgba(${COLOR[0]},${COLOR[1]},${COLOR[2]},${(1 - i / steps) * 0.9})`;
      ctx.lineWidth = WIDTH * (1 - i / steps * 0.5);
      ctx.lineCap = "round";
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }

    drawStar(hx, hy, now);

    if (t >= 1) {
      // === 爆发：主星在终点炸开，光晕扩散，overlay 溶入主界面 ===
      const k = Math.min(1, (now - t0 - DURATION) / 900);
      if (!intro.classList.contains("on-triggered")) {
        intro.classList.add("on-triggered");
        document.querySelectorAll(".pre-intro").forEach(el => el.classList.add("on"));
      }
      const [ex, ey] = pathPoint(1);
      const R = 30 + k * Math.max(W, H) * 0.9;
      const g = ctx.createRadialGradient(ex, ey, 0, ex, ey, R);
      g.addColorStop(0, `rgba(255,250,230,${0.55 * (1 - k)})`);
      g.addColorStop(0.4, `rgba(200,220,255,${0.25 * (1 - k)})`);
      g.addColorStop(1, "rgba(255,250,230,0)");
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(ex, ey, R, 0, 6.28); ctx.fill();
      intro.style.opacity = 1 - k;
      if (k >= 1) { finish(); return; }
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

/* ===================== toast ===================== */
function toast(msg, kind = "info", duration = 3600) {
  const host = $("toastHost");
  const colors = {
    info: "background:rgba(20,28,56,.94);color:#c8d0f0;border:1px solid rgba(140,160,255,.3);",
    err: "background:rgba(60,20,30,.94);color:#ffb8c8;border:1px solid rgba(255,122,154,.5);",
    ok: "background:rgba(20,50,35,.94);color:#9fe8c8;border:1px solid rgba(110,231,180,.4);",
  };
  const el = document.createElement("div");
  el.style.cssText = "padding:10px 18px;border-radius:10px;font-size:13px;margin-top:8px;" +
    "box-shadow:0 4px 20px rgba(0,0,0,.4);" + (colors[kind] || colors.info);
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0";
    setTimeout(() => el.remove(), 300); }, duration);
}

/* =====================================================================
   以下为 P0-4 / P1-10 / P1-11 / P2-14 对应的前端逻辑：
   身份确认、结果回填、校准与命中率、用量、管理面板。
   ===================================================================== */

/* ===================== 取消评审 ===================== */
async function cancelRun() {
  if (!state.currentRunId) { toast("当前没有运行中的评审", "info"); return; }
  if (!confirm("取消本次评审？\n\n已完成的结果会保留，尚未开始的候选人不再调用模型（不消耗额度）。")) return;
  try {
    await API.cancelRun(state.currentRunId);
    toast("已请求取消，当前候选人跑完后停止", "info");
  } catch (e) {
    toast("取消失败：" + e.message, "err");
  }
}

/* ===================== 身份确认（疑似同一人） ===================== */
async function refreshPendingIdentity() {
  try {
    const r = await API.pendingIdentity();
    state.pendingIdentity = r.items || [];
  } catch (e) {
    state.pendingIdentity = [];
  }
  const badge = $("pendingBadge");
  if (badge) {
    const n = state.pendingIdentity.length;
    badge.classList.toggle("hidden", n === 0);
    badge.textContent = n ? `⚠ ${n} 条身份待确认` : "";
  }
  // 只有"多条"时才给出批量入口：一条的时候逐条确认更快也更不容易点错
  const bb = $("pendingBatchBtn");
  if (bb) bb.classList.toggle("hidden", state.pendingIdentity.length < 2);
}

/* ---------------------------------------------------------------------------
   批量确认身份（只处理"不是同一人"）
   合并必须逐条做：confirm_identity 会改写 person_id 并可能连带合并档案，
   一次点错几十条无法逐条核对。这里的 UI 有意只提供一个动作。
--------------------------------------------------------------------------- */
function openBatchIdentity() {
  $("batchIdentityMsg").textContent = "";
  $("batchIdentityModal").classList.remove("hidden");
  renderBatchIdentity();
}

function renderBatchIdentity() {
  const box = $("batchIdentityBox");
  const items = state.pendingIdentity || [];
  if (!items.length) {
    box.innerHTML = `<p class="muted">没有待确认的身份了。</p>`;
    return;
  }
  box.innerHTML = items.map((it, i) => `
    <label class="batch-row">
      <input type="checkbox" data-batch-cid="${esc(it.candidate_id || it.id || "")}" checked>
      <span class="batch-name">${esc(it.name || "")}</span>
      <span class="muted">${esc((it.suggestion && it.suggestion.reasons || [])
        .slice(0, 2).join("；") || "疑似与库中记录重复")}</span>
    </label>`).join("");
  $("batchAll").checked = true;
}

function toggleBatchAll(on) {
  document.querySelectorAll("[data-batch-cid]").forEach(el => { el.checked = on; });
}

async function submitBatchIdentity() {
  const msg = $("batchIdentityMsg");
  const picked = Array.from(document.querySelectorAll("[data-batch-cid]"))
    .filter(el => el.checked)
    .map(el => ({ cid: el.getAttribute("data-batch-cid"), is_same: false }));
  if (!picked.length) { msg.textContent = "没有勾选任何一条"; return; }
  // 批量且不可逆（会改写档案归属判定），所以必须再问一次
  if (!window.confirm(`确认把这 ${picked.length} 条都标记为「不是同一人」？\n此操作会写入审计，不可撤销。`)) {
    return;
  }
  msg.textContent = "提交中…";
  try {
    const r = await API.batchIdentity(picked);
    msg.textContent = `已确认 ${r.succeeded} 条` +
      (r.failed ? `，${r.failed} 条失败` : "");
    await refreshPendingIdentity();
    renderBatchIdentity();
    if (state.job) await loadJob(state.job.id);
  } catch (e) {
    msg.textContent = e.message || "批量确认失败";
  }
}

function closeBatchIdentity() { $("batchIdentityModal").classList.add("hidden"); }

function filterPending() {
  const sel = $("talentStatus");
  if (sel) sel.value = "";
  state.talentPending = true;
  loadTalent();
}

/**
 * 打开身份确认弹窗。
 * 这里必须把"为什么判不确定"摊开给人看——不然 HR 只能盲点。
 */
function openIdentityConfirm(candId) {
  const c = state.candidates.find(x => x.id === candId);
  if (!c) return;
  state.identityTarget = c;
  const sug = (c.identity || {}).suggestion || {};
  const reasons = sug.reasons || (c.identity || {}).reasons || [];

  $("identityBody").innerHTML = `
    <div class="id-compare">
      <div class="id-side">
        <div class="id-title">本次上传</div>
        <div class="id-name">${esc(c.name)}</div>
        <div class="id-meta">岗位：${esc(state.job ? state.job.title : "")}</div>
        <div class="id-sig">${renderSig((c.identity || {}).signals || {})}</div>
      </div>
      <div class="id-vs">?</div>
      <div class="id-side">
        <div class="id-title">库里已有</div>
        <div class="id-name">${esc(sug.name || "(同岗位另一份材料)")}</div>
        <div class="id-meta">${sug.person_id ? "档案 " + esc(sug.person_id) : ""}</div>
        <div class="id-sig">${renderSig(sug.signals || {})}</div>
      </div>
    </div>
    <div class="id-reasons">
      <b>比对依据</b>
      <ul>${reasons.map(r => `<li>${esc(r)}</li>`).join("") || "<li>（无）</li>"}</ul>
      ${sug.score != null ? `<div class="hint">
        综合相似度 ${(sug.score * 100).toFixed(0)}%，落在"不确定"区间
        （自动合并阈值 65%，判定不同人阈值 25%）。
      </div>` : ""}
    </div>
    <div class="notice warn">
      <b>为什么没有自动合并：</b>把两个人的材料混成一份评审结论，
      比多一张待确认的卡片危险得多。所以系统只在信号高度一致时才自动合并。
    </div>
    <div>
      <button class="btn tiny ghost" onclick="toggleManualCompare()">系统的判断准不准？自己验一下</button>
      <div id="manualCompareBox" class="hidden" style="margin-top:10px">
        <p class="hint">
          粘贴两份材料的姓名与全文，用同一套规则再比对一次（PIPL 第 24 条：
          你有权知道系统为什么认为这是同一个人）。结果只解释规则，不替代你的判断。
        </p>
        <div class="field">
          <label>材料 A 姓名</label>
          <input id="mcNameA" type="text" value="${esc(c.name)}">
        </div>
        <div class="field">
          <label>材料 A 全文</label>
          <textarea id="mcTextA" rows="3" placeholder="粘贴简历 / 测评全文"></textarea>
        </div>
        <div class="field">
          <label>材料 B 姓名</label>
          <input id="mcNameB" type="text" value="${esc(sug.name || "")}">
        </div>
        <div class="field">
          <label>材料 B 全文</label>
          <textarea id="mcTextB" rows="3" placeholder="粘贴另一份材料的全文"></textarea>
        </div>
        <div class="modal-actions">
          <button class="btn small primary" onclick="runManualCompare()">比对</button>
        </div>
        <div id="mcResult"></div>
      </div>
    </div>`;
  $("identityModal").classList.remove("hidden");
}

function renderSig(sig) {
  const label = {
    name: "姓名", phone: "手机", email: "邮箱", education: "学历",
    work_history: "工作经历", location: "地点", birth_year: "出生年份",
  };
  const keys = Object.keys(label).filter(k => sig[k]);
  if (!keys.length) return `<span class="muted">（材料里没抽到可辨识信号）</span>`;
  return keys.map((k) => {
    const v = sig[k];
    const txt = Array.isArray(v) ? v.join("；") : v;
    return `<div class="id-line"><span>${label[k]}</span>${esc(txt)}</div>`;
  }).join("");
}

function closeIdentity() {
  $("identityModal").classList.add("hidden");
  state.identityTarget = null;
}

async function resolveIdentity(isSame) {
  const c = state.identityTarget;
  if (!c) return;
  const sug = (c.identity || {}).suggestion || {};
  try {
    await API.confirmIdentity(c.id, isSame, isSame ? sug.person_id : null);
    closeIdentity();
    toast(isSame ? "已合并为同一人" : "已标记为不同的人", "ok");
    await refreshPendingIdentity();
    if (state.job) await openJob(state.job.id);
  } catch (e) {
    toast("操作失败：" + e.message, "err");
  }
}

/* ---- 手工比对：可解释性的人工闸门 ----
   compare_profiles 的裁决逻辑对 HR 是个黑盒。给一个入口把两份材料
   重新喂进同一套规则，输出一致的信号、冲突的信号和判定理由——
   规则说得对不对，人眼能验；验不过，就别采信系统的合并建议。 */
function toggleManualCompare() {
  $("manualCompareBox").classList.toggle("hidden");
}

async function runManualCompare() {
  const box = $("mcResult");
  const nameA = ($("mcNameA").value || "").trim();
  const nameB = ($("mcNameB").value || "").trim();
  const textA = $("mcTextA").value || "";
  const textB = $("mcTextB").value || "";
  if (!textA.trim() || !textB.trim()) {
    box.innerHTML = `<p class="err">两份材料都要贴全文才能比对。</p>`;
    return;
  }
  box.innerHTML = `<p class="muted">比对中…</p>`;
  try {
    const r = await API.compareIdentity({
      name_a: nameA, text_a: textA, name_b: nameB, text_b: textB,
    });
    box.innerHTML = renderCompareResult(r.result || {});
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function renderCompareResult(res) {
  const vmap = {
    same: ["同一人", "ok"], different: ["不同的人", "bad"], uncertain: ["不确定", "warn"],
  };
  const v = vmap[res.verdict] || ["—", ""];
  const scoreTxt = res.score == null
    ? "" : ` · 相似度 ${(res.score * 100).toFixed(0)}%`;
  return `
    <div class="id-line"><span>判定</span>
      <b class="${v[1]}-text">${v[0]}</b>${scoreTxt}</div>
    ${(res.matched || []).length
      ? `<div class="id-line"><span>一致</span>
           <span class="ok-text">${(res.matched || []).map(esc).join("；")}</span></div>` : ""}
    ${(res.conflicts || []).length
      ? `<div class="id-line"><span>冲突</span>
           <span class="bad-text">${(res.conflicts || []).map(esc).join("；")}</span></div>` : ""}
    ${(res.reasons || []).length
      ? `<ul class="id-reasons">${(res.reasons || []).map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    <div class="hint">这只是同一套比对规则的解释视图，最终决定权在你。</div>`;
}

/* ===================== 结果回填 ===================== */
async function openOutcome() {
  const pid = state.talentPerson && state.talentPerson.id;
  if (!pid) { toast("请先从人才库打开一个人", "info"); return; }
  state.outcomeTarget = pid;
  $("ocNote").value = "";
  $("ocMsg").textContent = "";
  try {
    const r = await API.outcomes();
    const sel = $("ocStage");
    sel.innerHTML = (r.stages || []).map(
      s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
    const cur = state.talentPerson.latest_stage;
    if (cur) sel.value = cur;
  } catch (e) { /* 用已有选项 */ }
  $("outcomeModal").classList.remove("hidden");
}
function closeOutcome() {
  $("outcomeModal").classList.add("hidden");
  state.outcomeTarget = null;
}

async function saveOutcome() {
  const pid = state.outcomeTarget;
  if (!pid) return;
  const msg = $("ocMsg");
  msg.textContent = "保存中…";
  try {
    await API.recordOutcome(pid, {
      stage: $("ocStage").value,
      note: $("ocNote").value || "",
      job_id: (state.job && state.job.id) || "",
    });
    msg.textContent = "";
    closeOutcome();
    toast("已记录", "ok");
    if (state.talentPerson && state.talentPerson.id === pid) openTalent(pid);
  } catch (e) {
    msg.textContent = e.message;
  }
}

/* ===================== 分析：命中率 / 漂移 / 校准 / 用量 ===================== */
async function loadHitRate() {
  const box = $("hitRateBody");
  if (!box) return;
  box.innerHTML = `<div class="hint">加载中…</div>`;
  try {
    const r = await API.hitRate({});
    const rep = r.report || {};
    box.innerHTML = renderHitRate(rep);
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

function renderHitRate(rep) {
  if (!rep || rep.sufficient === false || rep.message) {
    return `<div class="notice">
      <b>还不能判断。</b> ${esc(rep.message || rep.error || "样本不足")}
      <div class="hint" style="margin-top:8px">
        这不是"功能没做好"，而是刻意的：用 2–3 个样本算出的命中率是噪音，
        把它显示成 78% 会让人误以为评分有效。门槛是
        <b>至少 ${rep.min_samples || 10} 份有结果的样本、且每个等级至少 ${rep.min_per_grade || 3} 份</b>。
      </div>
      ${rep.samples != null ? `<div class="hint">当前样本：${rep.samples} 份</div>` : ""}
    </div>`;
  }
  const rows = (rep.by_grade || []).map(g => `
    <tr>
      <td><b class="g ${g.grade === "A" ? "gA" : g.grade === "B" ? "gB" : "gC"}">${esc(g.grade)}</b></td>
      <td>${g.total}</td>
      <td>${g.positive}</td>
      <td>${g.rate == null ? "—" : (g.rate * 100).toFixed(0) + "%"}</td>
    </tr>`).join("");
  return `
    ${rep.spearman != null ? `<div class="metric-row">
      <div class="metric"><div class="mv">${rep.spearman.toFixed(2)}</div>
        <div class="ml">等级与结果的秩相关（-1~1）</div></div>
      <div class="metric"><div class="mv">${rep.samples}</div><div class="ml">样本数</div></div>
      <div class="metric"><div class="mv">${(rep.positive_rate * 100).toFixed(0)}%</div>
        <div class="ml">整体进入下一轮的比例</div></div>
    </div>` : ""}
    <table class="data-table">
      <thead><tr><th>等级</th><th>人数</th><th>进入下一轮</th><th>命中率</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="hint" style="margin-top:10px">${esc(rep.interpretation || "")}</div>`;
}

async function loadDrift() {
  const box = $("driftBody");
  if (!box) return;
  try {
    const r = await API.drift();
    const rep = r.report || {};
    const vers = rep.versions || [];
    if (!vers.length) {
      box.innerHTML = `<div class="hint">还没有足够的历史评审记录，无法比较口径漂移。</div>`;
      return;
    }
    box.innerHTML = `
      <table class="data-table">
        <thead><tr><th>评分口径</th><th>人数</th><th>平均分</th><th>A/B/C 分布</th></tr></thead>
        <tbody>${vers.map(v => `<tr>
          <td><code>${esc(v.rubric_version || "未标注")}</code></td>
          <td>${v.count}</td>
          <td>${v.avg_total == null ? "—" : v.avg_total.toFixed(2)}</td>
          <td>${esc(v.distribution || "")}</td>
        </tr>`).join("")}</tbody>
      </table>
      <div class="hint" style="margin-top:10px">
        口径不同的分数<b>不能直接比较</b>。上面每一行是一次"换尺子"，
        换尺子后平均分变了不代表人变强了。${esc(rep.note || "")}
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

async function loadCalibration() {
  const box = $("calibBody");
  if (!box) return;
  try {
    const r = await API.calibration();
    const rep = r.report || {};
    const sets = r.sets || [];
    state.calibSets = sets;
    box.innerHTML = `
      <div class="head-actions" style="margin-bottom:10px">
        <button class="btn small primary" onclick="openCalibItem()"
          ${sets.length ? "" : "disabled title='先在上面新建一个标定集'"}>添加标定条目</button>
      </div>
      ${sets.length ? `<div class="hint">标定集：${sets.map(s =>
        `<b>${esc(s.name)}</b>（${s.item_count || 0} 条）`).join("、")}</div>` : ""}
      ${rep.sufficient === false
        ? `<div class="notice">${esc(rep.message || "还没有标定数据。")}
             <div class="hint" style="margin-top:8px">
             做法：新建标定集 → 把已由资深面试官人工定级的候选人加进去 →
             系统会算出"人给的等级"和"模型给的等级"的一致率。
             ${esc(rep.min_note || "")}
             </div></div>`
        : `<div class="metric-row">
             <div class="metric"><div class="mv">${rep.n}</div><div class="ml">标定样本</div></div>
             <div class="metric"><div class="mv">${(rep.agreement * 100).toFixed(0)}%</div>
               <div class="ml">等级完全一致率</div></div>
             <div class="metric"><div class="mv">${rep.weighted_kappa == null ? "—" : rep.weighted_kappa.toFixed(2)}</div>
               <div class="ml">加权 kappa（扣除巧合）</div></div>
           </div>
           <div class="hint">${esc(rep.interpretation || "")}</div>`}`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

async function loadUsage() {
  const box = $("usageBody");
  if (!box) return;
  try {
    const r = await API.usage({});
    const s = r.summary || {};
    const byModel = (s.by_model || []).map(m => `<tr>
      <td>${esc(m.model || "未记录")}</td>
      <td>${esc(m.provider || "—")}</td>
      <td>${m.calls}</td>
      <td>${m.prompt_tokens}</td>
      <td>${m.completion_tokens}</td></tr>`).join("");
    const byRole = (s.by_role || []).map(m => `<tr>
      <td><code>${esc(m.role)}</code></td><td>${m.calls}</td><td>${m.total_tokens}</td></tr>`).join("");
    box.innerHTML = `
      <div class="metric-row">
        <div class="metric"><div class="mv">${s.calls || 0}</div><div class="ml">模型调用次数</div></div>
        <div class="metric"><div class="mv">${s.total_tokens || 0}</div><div class="ml">总 token</div></div>
        <div class="metric"><div class="mv">${s.avg_tokens_per_call || 0}</div><div class="ml">均 token / 次</div></div>
        <div class="metric"><div class="mv">${s.estimated_rows || 0}</div><div class="ml">无网关 usage 的记录</div></div>
      </div>
      ${byModel ? `<table class="data-table"><thead><tr>
        <th>模型</th><th>供应商</th><th>调用</th><th>输入 token</th><th>输出 token</th>
        </tr></thead><tbody>${byModel}</tbody></table>` : `<div class="hint">还没有调用记录。</div>`}
      ${byRole ? `<h3 style="margin-top:16px">按角色</h3>
        <table class="data-table"><thead><tr><th>角色</th><th>调用</th><th>token</th></tr></thead>
        <tbody>${byRole}</tbody></table>` : ""}
      ${s.estimated_rows ? `<div class="notice warn" style="margin-top:12px">
        有 ${s.estimated_rows} 条记录没拿到网关返回的 usage，这些调用的 token 按 0 计入，
        所以上面的总成本<b>偏低</b>。不要把这里的数字当成账单。
      </div>` : ""}`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

function openNewCalibSet() {
  $("csName").value = ""; $("csDesc").value = ""; $("csMsg").textContent = "";
  $("calibSetModal").classList.remove("hidden");
}
function closeNewCalibSet() { $("calibSetModal").classList.add("hidden"); }

async function doCreateCalibSet() {
  const msg = $("csMsg");
  msg.textContent = "创建中…";
  try {
    await API.createCalibSet({
      name: ($("csName").value || "").trim(),
      description: ($("csDesc").value || "").trim(),
    });
    msg.textContent = "";
    closeNewCalibSet();
    loadCalibration();
    toast("标定集已创建", "ok");
  } catch (e) { msg.textContent = e.message; }
}

/* 添加标定条目：把"人给的等级"登记进标定集。
   只列当前岗位已有评分的候选人——没评分的人没有"模型给的等级"可对比，
   加进去只会稀释一致率。 */
function openCalibItem() {
  const sets = state.calibSets || [];
  if (!sets.length) { toast("先在上面新建一个标定集", "info"); return; }
  const scored = (state.candidates || []).filter(c => c.result && c.result.total != null);
  if (!scored.length) { toast("当前岗位还没有已评分的候选人，先跑一轮评审", "info"); return; }
  $("ciSet").innerHTML = sets.map(s =>
    `<option value="${esc(s.id)}">${esc(s.name)}（${s.item_count || 0} 条）</option>`).join("");
  $("ciCand").innerHTML = scored.map(c =>
    `<option value="${esc(c.id)}">${esc(c.name)} · 模型评 ${esc(c.result.total)} 分（${esc(c.result.grade || "?")}）</option>`).join("");
  $("ciLabel").value = "B";
  $("ciNote").value = "";
  $("ciMsg").textContent = "";
  $("calibItemModal").classList.remove("hidden");
}
function closeCalibItem() { $("calibItemModal").classList.add("hidden"); }

async function saveCalibItem() {
  const msg = $("ciMsg");
  const set_id = $("ciSet").value, cand = $("ciCand").value;
  if (!set_id || !cand) { msg.textContent = "请选择标定集和候选人"; return; }
  msg.textContent = "提交中…";
  try {
    await API.addCalibItem({
      set_id: set_id,
      candidate_id: cand,
      human_label: $("ciLabel").value,
      note: ($("ciNote").value || "").trim(),
    });
    msg.textContent = "";
    closeCalibItem();
    loadCalibration();
    toast("已加入标定集", "ok");
  } catch (e) { msg.textContent = e.message; }
}

/* ===================== 管理面板 ===================== */
async function loadUsers() {
  const box = $("usersBody");
  if (!box) return;
  try {
    const r = await API.users();
    state.users = r.users || [];
    const rows = state.users.map(u => `<tr>
      <td><b>${esc(u.username)}</b></td>
      <td>${esc(u.display_name || "—")}</td>
      <td>${esc({ admin: "管理员", hr: "招聘", viewer: "只读" }[u.role] || u.role)}</td>
      <td>${u.disabled ? '<span class="badge bad">已停用</span>' : '<span class="badge ok">正常</span>'}</td>
      <td class="muted">${esc(u.last_login || "从未登录")}</td>
      <td>
        <button class="btn tiny" onclick="toggleUser('${u.id}', ${u.disabled ? "false" : "true"})">
          ${u.disabled ? "启用" : "停用"}</button>
        <button class="btn tiny" onclick="openResetPw('${u.id}')">重置口令</button>
      </td>
    </tr>`).join("");
    box.innerHTML = `<table class="data-table"><thead><tr>
      <th>账号</th><th>显示名</th><th>角色</th><th>状态</th><th>最后登录</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

/* 重置口令：账号遗忘或怀疑失窃时，管理员直接换一个新口令。
   用户名从 state.users 里按 id 反查后写进 textContent，不拼进
   onclick 属性——用户输入不该进 HTML 属性上下文。 */
function openResetPw(uid) {
  const u = (state.users || []).find(x => x.id === uid);
  if (!u) { toast("找不到该用户，请刷新列表", "err"); return; }
  state.resetPwTarget = u;
  $("rpUser").textContent = u.username + (u.display_name ? `（${u.display_name}）` : "");
  $("rpPass").value = "";
  $("rpHint").textContent = "";
  const m = $("rpMeter");
  if (m) { m.style.width = "0"; m.className = ""; }
  $("rpMsg").textContent = "";
  $("resetPwModal").classList.remove("hidden");
}
function closeResetPw() {
  $("resetPwModal").classList.add("hidden");
  state.resetPwTarget = null;
}

async function doResetPw() {
  const u = state.resetPwTarget;
  if (!u) return;
  const pw = $("rpPass").value || "";
  if (pw.length < 8) { $("rpMsg").textContent = "口令至少 8 位"; return; }
  const msg = $("rpMsg");
  msg.textContent = "提交中…";
  try {
    await API.resetPassword(u.id, pw);
    msg.textContent = "";
    closeResetPw();
    toast(`已重置 ${u.username} 的口令`, "ok");
  } catch (e) { msg.textContent = e.message; }
}

async function toggleUser(id, disabled) {
  try {
    await API.disableUser(id, disabled);
    loadUsers();
    toast(disabled ? "已停用" : "已启用", "ok");
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}

async function loadPrivacy() {
  const box = $("privacyBody");
  if (!box) return;
  try {
    const r = await API.privacy();
    const o = r.overview || {};
    const ret = r.retention || {};
    box.innerHTML = `
      <div class="metric-row">
        <div class="metric"><div class="mv">${o.people || 0}</div><div class="ml">人才档案</div></div>
        <div class="metric"><div class="mv">${o.pii_candidates || 0}</div><div class="ml">含个人信息的候选人</div></div>
        <div class="metric"><div class="mv">${o.expiring_count || 0}</div><div class="ml">即将到期</div></div>
        <div class="metric"><div class="mv">${o.expired_count || 0}</div><div class="ml">已过期未清理</div></div>
      </div>
      <div class="hint">
        加密密钥指纹 <code>${esc(o.key_fingerprint || "—")}</code>。
        材料静态加密存储；密钥单独放在 <code>data/</code> 之外时请自行备份，
        <b>密钥丢失 = 所有材料永久不可读</b>。
      </div>
      <div class="head-actions" style="margin-top:14px">
        <button class="btn" onclick="doPurgePreview()">预览过期清理</button>
        <button class="btn danger-ghost" onclick="doPurge()">执行清理</button>
        <button class="btn danger-ghost" onclick="doRotateKey()">轮换加密密钥</button>
      </div>
      <div class="auth-msg" id="privacyMsg"></div>
      ${(o.ops || []).length ? `<h3 style="margin-top:16px">最近操作</h3>
        <div class="audit-list">${o.ops.slice(0, 10).map(x =>
          `<div class="audit-row"><span class="muted">${esc(x.ts)}</span>
           <b>${esc(x.action)}</b> ${esc(x.detail || "")}</div>`).join("")}</div>` : ""}`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

async function doPurgePreview() {
  const m = $("privacyMsg");
  m.textContent = "检查中…";
  try {
    const r = await API.purge(true);
    // purge_expired(dry_run=True) 返回的是 {dry_run, would_purge, items, message}。
    // 这里原来读的是 res.expired（那是**列表**，来自概览接口的 expired 字段）
    // 和 res.people（根本不存在），于是永远显示 "0 份、0 个"，
    // 或者插值出一个 [object Object] —— 让"预览"这个安全阀看起来永远没事可做。
    const res = r.result || r.preview || {};   // 路由返回 {"ok":true,"result":{...}}
    const n = res.would_purge != null ? res.would_purge : 0;
    const items = res.items || [];
    if (!n) {
      m.textContent = "预览：没有已过期的材料，无需清理。（未删除任何东西）";
      return;
    }
    const names = items.slice(0, 5)
      .map(x => x.name || x.candidate_id || "").filter(Boolean).join("、");
    m.textContent = `预览：将清理 ${n} 份到期材料` +
      (names ? `（${names}${n > 5 ? " 等" : ""}）` : "") +
      `。这是预览，未真正删除。`;
  } catch (e) { m.textContent = e.message; }
}

async function doPurge() {
  if (!confirm("执行清理会永久删除已过期候选人的材料与文件，不可恢复。\n\n建议先点「预览过期清理」看清影响面。继续？")) return;
  const m = $("privacyMsg");
  m.textContent = "清理中…";
  try {
    const r = await API.purge(false);
    m.textContent = "已清理：删除 " + ((r.result || {}).deleted || 0) + " 条记录。";
    loadPrivacy();
  } catch (e) { m.textContent = e.message; }
}

async function doRotateKey() {
  if (!confirm("轮换加密密钥？\n\n新密钥只用于之后写入的数据；已加密的旧数据会被重新加密（本操作会遍历全部候选人）。\n请确保你已备份数据库。继续？")) return;
  const m = $("privacyMsg");
  m.textContent = "轮换中…";
  try {
    const r = await API.rotateKey();
    const res = r.result || {};
    m.textContent = `密钥已轮换，重新加密 ${res.reencrypted || 0} 条记录。新指纹：${res.fingerprint || "—"}`;
    loadPrivacy();
  } catch (e) { m.textContent = e.message; }
}

async function loadAudit() {
  const box = $("auditBody");
  if (!box) return;
  try {
    const r = await API.auditLog({ limit: 200 });
    const v = r.verify || {};
    const rows = (r.rows || []).map(x => `<tr>
      <td class="muted">${esc(x.ts)}</td>
      <td><code>${esc(x.action)}</code></td>
      <td>${esc(x.actor || "—")}</td>
      <td>${esc(x.target_type || "")} ${esc(x.target_id || "")}</td>
      <td class="muted">${esc(JSON.stringify(x.detail || {})).slice(0, 90)}</td>
    </tr>`).join("");
    box.innerHTML = `
      <div class="notice ${v.ok ? "" : "warn"}">
        <b>哈希链校验：${v.ok ? "通过" : "失败"}</b>
        ${v.broken_at ? ` · 断点在第 ${v.broken_at} 条（此后的记录不可信）` : ""}
        ${v.ok ? `<div class="hint" style="margin-top:6px">
          每条记录的哈希都覆盖前一条的哈希，改动任意一条历史记录都会让后续全部校验失败。
          这能证明"日志没被改过"，但<b>不能</b>证明"日志记全了"——那取决于代码有没有漏记。</div>` : ""}
      </div>
      <table class="data-table"><thead><tr>
        <th>时间</th><th>动作</th><th>操作者</th><th>对象</th><th>详情</th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  } catch (e) {
    box.innerHTML = `<div class="err">加载失败：${esc(e.message)}</div>`;
  }
}

/* ============================================================================
   回测对齐：把 HR 的真实决定变成标准答案
   ----------------------------------------------------------------------------
   这是整个系统里唯一能回答"它准不准"的部分。没有它，所有分数都只是
   一个未经验证的排序；有了它，才知道该不该信这个排序。
   ========================================================================== */
async function loadAlign() {
  const j = state.job;
  if (!j) return;
  const box = $("alignBatchList");
  if (!box) return;
  try {
    const r = await API.alignList(j.id);
    const batches = r.batches || [];
    state.alignBatches = batches;
    if (!batches.length) {
      box.innerHTML = `<p class="muted">还没有导入过真实决定。点右上「导入真实决定」开始。</p>`;
      $("alignReport").innerHTML = "";
      return;
    }
    box.innerHTML = `<div class="align-list">${batches.map(b => `
      <div class="align-row${state.alignActive === b.id ? " active" : ""}"
           data-align-id="${esc(b.id)}">
        <div class="align-main">
          <b>${esc(b.label || "未命名批次")}</b>
          <span class="muted">${esc((b.created_at || "").slice(0, 10))}
            · ${esc(b.truth_count)} 条真实决定
            ${b.rubric_version ? ` · 口径 <code>${esc(b.rubric_version)}</code>` : ""}</span>
        </div>
        <button class="btn tiny" data-align-view="${esc(b.id)}">看对照</button>
        <button class="btn tiny danger-ghost" onclick="deleteAlignBatch('${esc(b.id)}')">删除</button>
      </div>`).join("")}</div>`;
    if (!state.alignActive) {
      loadAlignReport(batches[0].id);
    } else {
      loadAlignReport(state.alignActive);
    }
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

/* 删掉一个导入错的批次：整批决定都会被移除，但岗位评审不受影响。
   删错批次的代价是重新导入，删错评审的代价是重烧一次 API。 */
async function deleteAlignBatch(aid) {
  if (!aid) return;
  if (!confirm("删除这个回测批次？\n\n批次里的真实决定会被移除，已算出的对照结果不再可查；岗位内的评审记录不受影响。")) return;
  try {
    await API.alignDelete(aid);
    if (state.alignActive === aid) {
      state.alignActive = null;
      $("alignReport").innerHTML = "";
    }
    toast("已删除批次", "ok");
    loadAlign();
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}

async function loadAlignReport(aid) {
  const box = $("alignReport");
  if (!box || !aid) return;
  state.alignActive = aid;
  box.innerHTML = `<p class="muted">计算中…</p>`;
  try {
    const r = await API.alignReport(aid);
    box.innerHTML = renderAlignReport(r.report || {});
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

function renderAlignReport(rep) {
  if (!rep || !rep.scored_total) {
    return `<p class="muted">这个岗位还没有评审结果，先跑一轮评审再回测。</p>`;
  }
  if (rep.sufficient === false) {
    return `<div class="align-warn">
      <b>样本不足，不给结论</b>
      <p>${esc(rep.message || "")}</p>
      <p class="muted">已配对 ${esc(rep.matched)} 人、已决 ${esc(rep.decided)} 人。
        分母太小的百分比是噪音，宁可不给。</p>
    </div>`;
  }
  const rows = (rep.at || []).map(x => {
    const ci = x.precision_ci;
    // 区间必须和点估计并排显示：只给 "67%" 会让人以为这个数字是稳的。
    // 区间很宽时还要显式提示，否则用户会把下沿当成"最差情况"而不是
    // "这份样本根本撑不起这个结论"。
    const ciTxt = ci
      ? `<span class="ci">${(ci.lo * 100).toFixed(0)}%–${(ci.hi * 100).toFixed(0)}%</span>`
      : `<span class="muted">—</span>`;
    const wide = ci && (ci.hi - ci.lo) >= 0.35;
    return `
    <tr>
      <td>前 ${esc(x.k)} 名</td>
      <td><b>${esc(x.hits)}</b> / ${esc(x.k)}</td>
      <td>${(x.precision * 100).toFixed(0)}%</td>
      <td>${ciTxt}</td>
      <td>${(x.recall * 100).toFixed(0)}%</td>
      <td class="${x.lift >= 1.3 ? "lift-good" : (x.lift < 1 ? "lift-bad" : "")}">
        ${x.lift == null ? "—" : x.lift.toFixed(2) + "×"}</td>
      <td class="muted">${esc(x.random_expected)}</td>
    </tr>${wide ? `<tr class="ci-wide"><td colspan="7">
      区间宽达 ${((ci.hi - ci.lo) * 100).toFixed(0)} 个百分点：这个规模上的结论还不稳，
      样本再多一些才有意义。</td></tr>` : ""}`;
  }).join("");

  const pend = rep.pending_bindings || {};
  const pendN = (pend.ambiguous || 0) + (pend.unmatched || 0);
  const pendHtml = pendN ? `
    <div class="align-warn">
      <b>有 ${pendN} 条真实决定没能参与计算</b>
      <p>${esc(pend.note || "")}</p>
      <p class="muted">同名（${esc(pend.ambiguous || 0)} 条）不会自动猜，
        因为猜错会直接污染命中率；找不到人（${esc(pend.unmatched || 0)} 条）
        可能是这人没进过系统。可在「待绑定」里手工指认。</p>
      <button class="btn small" id="btnBindPending">查看待绑定</button>
    </div>` : "";

  return `
    <div class="align-verdict">
      <div class="align-verdict-main">${esc(rep.verdict || "")}</div>
      <div class="muted" style="margin-top:6px">
        已配对 ${esc(rep.matched)} 人（有分数且能在真实结果里找到）·
        HR 实际推了 <b>${esc(rep.positives)}</b> 人，占
        ${(rep.base_rate * 100).toFixed(0)}%
        ${rep.excluded_rubric_mismatch
          ? ` · <span class="warn-text">${esc(rep.excluded_rubric_mismatch)} 人因口径不同被排除</span>`
          : ""}
      </div>
    </div>
    ${pendHtml}
    <table class="tbl small align-tbl">
      <thead><tr>
        <th style="text-align:left">取前多少人</th>
        <th>命中</th><th>precision</th><th>95% 区间</th><th>recall</th>
        <th>相对随机</th><th>随机期望</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint" style="margin-top:10px">
      <b>precision</b> = 前 N 名里有几个是你真推的；
      <b>区间</b> = 对已决人群重抽样 1000 次得到的 95% 区间，
      宽就说明样本还不足以支撑精细结论（不是"最差情况"的上下限）；
      <b>recall</b> = 你推的人里有几个落进了前 N 名；
      <b>相对随机</b> &gt; 1 说明这个排序有信息量，≈ 1 说明跟随便抽没区别。<br>
      注意：这里只检验<b>排序</b>，不检验分数本身 —— 分数没有绝对刻度，
      拿它当及格线卡人是不成立的。
    </p>`;
}

function openAlignImport() {
  if (!state.job) return;
  $("alignLabel").value = "";
  $("alignText").value = "";
  $("alignMsg").textContent = "";
  $("alignPreviewBox").innerHTML = "";
  $("alignModal").classList.remove("hidden");
}

function closeAlign() { $("alignModal").classList.add("hidden"); }

/* 上传文件解析成文本回填：HR 手里的真实决定常常是 Excel/Word/PDF 导出，
   "只能粘贴"会逼人手动搬运——搬错一列，命中率就算错，而且没人会发现。
   这里用通用解析端点抽文字，回填后仍要走人工预览那一步。 */
async function parseAlignFile(input) {
  const f = input.files && input.files[0];
  const msg = $("alignFileMsg");
  if (!f) return;
  msg.textContent = "解析中…";
  try {
    const [b64] = await readFilesToB64([f]);
    const r = await API.parseDocs({
      files: [{ filename: b64.filename, data_base64: b64.data_base64 }],
    });
    const first = (r.results || [])[0] || {};
    if (first.error) throw new Error(first.error);
    const text = (first.text || "").trim();
    if (!text) throw new Error("没能从这个文件抽出文字（扫描版图片/PDF 暂不支持）");
    $("alignText").value = text;
    msg.textContent =
      `已从「${f.name}」解析出 ${text.length} 字。请检查表头和决定列是否完好，再点「先预览」。`;
  } catch (e) {
    msg.textContent = "解析失败：" + e.message;
  } finally {
    input.value = "";   // 清空，以便同一文件改完后再选还能触发 change
  }
}

async function previewAlign() {
  const text = $("alignText").value;
  const msg = $("alignMsg");
  const box = $("alignPreviewBox");
  if (!text.trim()) { msg.textContent = "请先粘贴内容"; return; }
  msg.textContent = "解析中…";
  try {
    const r = await API.alignPreview(text);
    msg.textContent = "";
    const s = r.summary || {};
    box.innerHTML = `
      <div class="align-preview">
        <b>解析结果：</b>
        推进 <b class="ok-text">${esc(s.advance || 0)}</b> ·
        淘汰 <b class="bad-text">${esc(s.reject || 0)}</b> ·
        待定 ${esc(s.hold || 0)}
        ${(r.errors || []).length
          ? `<div class="err" style="margin-top:8px">${esc(r.errors.length)} 行有问题：<br>
             ${r.errors.slice(0, 5).map(e =>
               `第 ${esc(e.row)} 行：${esc(e.error)}`).join("<br>")}</div>`
          : `<div class="muted" style="margin-top:6px">没有解析错误。</div>`}
        <div class="muted" style="margin-top:8px">
          确认无误后点「导入」。决定列如果被误读会让整份结论反过来，
          所以务必先看一眼这个统计。
        </div>
      </div>`;
  } catch (e) {
    msg.textContent = e.message;
  }
}

async function saveAlign() {
  const text = $("alignText").value;
  const msg = $("alignMsg");
  if (!text.trim()) { msg.textContent = "请先粘贴内容"; return; }
  msg.textContent = "导入中…";
  try {
    const label = $("alignLabel").value.trim();
    const c = await API.alignCreate(state.job.id, { label: label || "未命名批次" });
    const aid = c.batch.id;
    const r = await API.alignImport(aid, text);
    const res = r.result || {};
    msg.textContent = "";
    closeAlign();
    toast(`已导入 ${res.added || 0} 条真实决定`, "ok");
    if ((res.ambiguous || []).length) {
      toast(`${res.ambiguous.length} 个同名候选人未自动关联，请在报告中核对`, "err", 6000);
    }
    state.alignActive = aid;
    loadAlign();
  } catch (e) {
    msg.textContent = e.message;
  }
}

/* ============================================================================
   候选人决策：由人决定，并且必须留下理由
   ========================================================================== */
function openDecide(cid, name, cur) {
  state.decideTarget = cid;
  $("decideName").textContent = name || "—";
  $("decideChoice").value = cur || "advance";
  $("decideReason").value = "";
  $("decideMsg").textContent = "";
  $("decideModal").classList.remove("hidden");
  setTimeout(() => { const el = $("decideReason"); if (el) el.focus(); }, 50);
}

function closeDecide() { $("decideModal").classList.add("hidden"); }

async function saveDecide() {
  const cid = state.decideTarget;
  if (!cid) return;
  const msg = $("decideMsg");
  const reason = $("decideReason").value.trim();
  if (reason.length < 4) {
    msg.textContent = "请填写理由（至少 4 个字）：淘汰和推进都要能说明依据。";
    return;
  }
  msg.textContent = "保存中…";
  try {
    await API.decide(cid, $("decideChoice").value, reason);
    msg.textContent = "";
    closeDecide();
    toast("已记录决定", "ok");
    const r = await API.job(state.job.id);
    state.candidates = r.candidates || [];
    renderCandidates();
    loadAlign();
  } catch (e) {
    msg.textContent = e.message;
  }
}

/* 事件委托：对齐批次行 / 决策按钮 */
document.addEventListener("click", (e) => {
  const t = e.target;
  const row = t.closest && t.closest(".align-row[data-align-id]");
  if (row) {
    loadAlignReport(row.getAttribute("data-align-id"));
    return;
  }
  const db2 = t.closest && t.closest("[data-decide-id]");
  if (db2) {
    openDecide(db2.getAttribute("data-decide-id"),
               db2.getAttribute("data-decide-name") || "",
               db2.getAttribute("data-decide-cur") || "");
    return;
  }
  const del = t.closest && t.closest("[data-del-cand]");
  if (del) {
    deleteCandidate(del.getAttribute("data-del-cand"));
    return;
  }
  if (t.id === "btnBindPending") {
    openBindPending();
    return;
  }
  const bindSel = t.closest && t.closest("[data-bind-do]");
  if (bindSel) {
    doBind(bindSel.getAttribute("data-bind-truth"),
           bindSel.parentElement.querySelector("select").value);
    return;
  }
  if (t.id === "btnBindClose") { closeBind(); }
});

/* ---------------------------------------------------------------------------
   待绑定：同名/对不上人的真实决定，由人手工指认。
   机器不猜——猜错会把 A 的决定算到 B 头上，命中率立刻失真。
--------------------------------------------------------------------------- */
async function openBindPending() {
  if (!state.alignActive) { return; }
  const box = $("bindBox");
  $("bindMsg").textContent = "";
  box.innerHTML = `<p class="muted">加载中…</p>`;
  $("bindModal").classList.remove("hidden");
  try {
    const r = await API.alignPending(state.alignActive);
    box.innerHTML = renderPending(r.pending || {});
  } catch (e) {
    box.innerHTML = `<p class="err">${esc(e.message || "加载失败")}</p>`;
  }
}

function renderPending(p) {
  const amb = p.ambiguous || [];
  const un = p.unmatched || [];
  if (!amb.length && !un.length) {
    return `<p class="muted">没有待绑定项：所有真实决定都已对应到具体候选人。</p>`;
  }
  const decLabel = { advance: "推进面试", reject: "淘汰", hold: "待定" };
  const ambHtml = amb.map((x) => `
    <div class="bind-row">
      <div class="bind-head">
        <b>${esc(x.name)}</b>
        <span class="bad-chip">同名 ${esc((x.options || []).length)} 人</span>
        <span class="muted">真实决定：${esc(decLabel[x.decision] || x.decision)}</span>
      </div>
      <div class="bind-pick">
        <select>
          <option value="">— 请选择是这个人的哪一条 —</option>
          ${(x.options || []).map((o) => `
            <option value="${esc(o.id)}">${esc(o.name)}（${esc(o.id)}，
              状态 ${esc(o.status)}，口径 ${esc(o.rubric_version || "无")}）</option>`).join("")}
        </select>
        <button class="btn small" data-bind-do="${esc(x.truth_id)}">绑定</button>
      </div>
      <p class="hint">同名不自动猜：绑错会把这个人的决定算到另一个人头上，
        直接污染命中率。请对照学历/工作经历等线索自己认。</p>
    </div>`).join("");
  const unHtml = un.length ? `
    <h4 style="margin-top:16px">系统里找不到对应候选人（${un.length} 条）</h4>
    <p class="hint">这些人可能从未进过系统。它们无法参与回测，
      但如果其中有人你其实推了，说明有人绕过了系统——这本身是个值得注意的信号。</p>
    <ul class="bind-un">${un.map((x) => `<li>${esc(x.name)}
      <span class="muted">（${esc(decLabel[x.decision] || x.decision)}）</span></li>`).join("")}</ul>` : "";
  return ambHtml + unHtml;
}

async function doBind(truthId, candidateId) {
  const msg = $("bindMsg");
  if (!candidateId) { msg.textContent = "请先选择要绑定的候选人"; return; }
  msg.textContent = "绑定中…";
  try {
    await API.alignBind(state.alignActive, truthId, candidateId);
    msg.textContent = "已绑定";
    // 绑定会改变回测结果，所以要重算报告而不是只刷新弹窗
    await openBindPending();
    if (state.alignActive) { loadAlignReport(state.alignActive); }
  } catch (e) {
    msg.textContent = e.message || "绑定失败";
  }
}

function closeBind() { $("bindModal").classList.add("hidden"); }

/* ---------------------------------------------------------------------------
   评审历史：同一份材料反复评出来的分数序列
--------------------------------------------------------------------------- */
async function openHistory(cid) {
  $("histBox").innerHTML = `<p class="muted">加载中…</p>`;
  $("histModal").classList.remove("hidden");
  try {
    const r = await API.candidateHistory(cid);
    $("histBox").innerHTML = renderHistory(r);
  } catch (e) {
    $("histBox").innerHTML = `<p class="err">${esc(e.message || "加载失败")}</p>`;
  }
}

function renderHistory(r) {
  const evs = r.evaluations || [];
  const d = r.drift;
  if (!evs.length) {
    return `<p class="muted">还没有历史记录（这条结果可能来自旧版本，或只评过一次）。</p>`;
  }
  const rows = evs.map((e) => `
    <tr>
      <td>${esc((e.created_at || "").slice(0, 16).replace("T", " "))}</td>
      <td>${e.total == null ? "—" : e.total.toFixed(1)}</td>
      <td>${esc(e.grade || "—")}</td>
      <td><code>${esc(e.rubric_version || "无")}</code></td>
      <td class="muted">${esc(e.repeats || 1)} 次${e.stdev == null ? "" : ` · σ ${e.stdev.toFixed(2)}`}</td>
    </tr>`).join("");

  let head = "";
  if (d) {
    head = `<div class="align-warn">
      <b>评了 ${esc(d.n)} 次，分数 ${esc(d.min.toFixed(1))} – ${esc(d.max.toFixed(1))}
        （极差 ${esc(d.spread)} 分）</b>
      ${d.grade_changed
        ? `<p class="bad-text">等级在几次之间发生过变化 —— 这个人处于档位边界，
            等级不能作为判断依据。</p>`
        : `<p class="muted">等级没有变化，但这个结论只覆盖被重复评的那几次。</p>`}
      ${d.versions && d.versions.length > 1
        ? `<p class="warn-text">期间换过口径（${esc(d.versions.join(" → "))}），
            跨版本分数不可直接比较 —— 上面的极差里混着口径变化，不全是随机波动。</p>`
        : ""}
    </div>`;
  } else {
    head = `<p class="muted">只评过一次，无法估计波动（不是"很稳"，是"没测"）。</p>`;
  }
  return `${head}
    <table class="tbl small">
      <thead><tr><th style="text-align:left">时间</th><th>总分</th><th>等级</th>
        <th>口径</th><th>重复</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function closeHistory() { $("histModal").classList.add("hidden"); }

/* ---------------------------------------------------------------------------
   数据备份
   注意：备份含明文姓名、决策理由与审计链，所以界面上要把这句话说出来，
   而不是让人以为它是个"安全副本"。
--------------------------------------------------------------------------- */
async function openBackup() {
  $("backupMsg").textContent = "";
  $("backupBox").innerHTML = `<p class="muted">加载中…</p>`;
  $("backupModal").classList.remove("hidden");
  await loadBackups();
}

async function loadBackups() {
  try {
    const r = await API.backups();
    $("backupBox").innerHTML = renderBackups(r);
  } catch (e) {
    $("backupBox").innerHTML =
      `<p class="err">${esc(e.message || "读取备份列表失败")}</p>`;
  }
}

function renderBackups(r) {
  const files = r.files || [];
  if (!files.length) {
    return `<p class="muted">还没有备份。点上面的「立即备份」生成第一份。</p>`;
  }
  const rows = files.map((f) => `
    <tr>
      <td style="text-align:left">${esc(f.file)}</td>
      <td>${fmtSize(f.bytes)}</td>
      <td class="muted">${esc(f.mtime || "")}</td>
      <td><button class="btn tiny ghost" onclick="confirmRestore('${esc(f.file)}')">
        恢复</button></td>
    </tr>`).join("");
  return `
    <p class="hint">共 ${files.length} 份 · 目录 <code>${esc(r.dir || "")}</code></p>
    <table class="tbl small">
      <thead><tr><th style="text-align:left">文件</th><th>大小</th>
        <th>时间</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint" style="margin-top:8px">${esc(r.note || "")}</p>`;
}

function fmtSize(n) {
  if (n == null) { return "—"; }
  if (n < 1024) { return n + " B"; }
  if (n < 1024 * 1024) { return (n / 1024).toFixed(1) + " KB"; }
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

async function doBackup() {
  const msg = $("backupMsg");
  msg.textContent = "备份中…";
  try {
    const r = await API.backupCreate({ note: "界面手动备份" });
    const b = r.backup || {};
    if (b.ok === false) {
      msg.textContent = "备份失败：" + (b.error || "未知原因");
    } else {
      // create() 返回的是完整路径，界面上只显示文件名
      const nm = String(b.path || "").split(/[\\/]/).pop();
      msg.textContent = (b.verified ? "已备份并校验通过：" : "已生成但校验未通过：") + nm;
    }
    await loadBackups();
  } catch (e) {
    msg.textContent = e.message || "备份失败";
  }
}

async function confirmRestore(name) {
  const msg = $("backupMsg");
  // 破坏性操作必须打一次字确认，不能只靠 confirm() 的一句话——
  // 那个对话框点快了是真的会点错的。
  const typed = window.prompt(
    "恢复会用这份备份覆盖当前全部数据。\n" +
    "如果当前数据还没备份，恢复后就找不回来了。\n\n" +
    "确认请输入 RESTORE：" + name);
  if (typed !== "RESTORE") {
    msg.textContent = "已取消（未输入 RESTORE）";
    return;
  }
  msg.textContent = "恢复中…";
  try {
    await API.backupRestore(name);
    msg.textContent = "已从 " + name + " 恢复。建议刷新页面。";
    await loadBackups();
  } catch (e) {
    msg.textContent = e.message || "恢复失败";
  }
}

function closeBackup() { $("backupModal").classList.add("hidden"); }
