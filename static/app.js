/*
 * BubbleSheetTools front end (plain JavaScript, no build step).
 *
 * Talks to the FastAPI server under /api. Login state lives in HttpOnly
 * cookies set by the server, so this script never handles tokens except the
 * one-time password-reset token Supabase puts in the URL.
 *
 * All user-supplied text (names, file names, messages) is inserted with
 * textContent, never innerHTML, so it cannot inject markup.
 */
"use strict";

const LETTERS = "ABCDEFGHIJ";
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Create an element: el("li", {class: "x"}, "text", childEl, ...) */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function show(node, visible = true) { node.hidden = !visible; }

function setAlert(node, message, kind = "") {
  node.className = "alert" + (kind ? " " + kind : "");
  node.replaceChildren();
  if (Array.isArray(message)) {
    node.append(el("ul", {}, message.map((m) => el("li", {}, m))));
  } else {
    node.textContent = message || "";
  }
  show(node, Boolean(message && message.length));
}

/** Call the API. Returns parsed JSON (or a Blob for binary responses). */
async function api(path, { method = "GET", json, form, blob = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (json !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(json);
  } else if (form) {
    opts.body = form;
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new Error("Could not reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    let detail = `Request failed (${res.status}).`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) detail = "Please check the form: some values are invalid.";
    } catch { /* not JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return blob ? res.blob() : res.json();
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/* ================================================================ tabs ==== */

function selectTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  show($("#panel-create"), name === "create");
  show($("#panel-grade"), name === "grade");
  if (name === "grade") refreshGrade();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));

/* ======================================================= create sheets ==== */

let parsedNames = null;   // names after the server has checked them
let csvText = null;       // contents of an uploaded roster CSV

function setupCreateForm() {
  const choices = $("#num-choices");
  for (let n = 2; n <= 10; n++) {
    choices.append(el("option", { value: n, selected: n === 4 }, `${n} (A–${LETTERS[n - 1]})`));
  }
  $("#num-written").addEventListener("input", renderWrittenInputs);
  renderWrittenInputs();

  // Any change to the names invalidates the previous check.
  $("#names-text").addEventListener("input", () => {
    csvText = null;
    $("#names-csv").value = "";
    resetNames();
  });
  $("#names-csv").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    csvText = await file.text();
    $("#names-text").value = "";
    resetNames();
    await checkNames();
  });
  $("#check-names").addEventListener("click", checkNames);
  $("#create-form").addEventListener("submit", createSheets);
}

function renderWrittenInputs() {
  const list = $("#written-list");
  const count = Math.max(0, Math.min(20, parseInt($("#num-written").value, 10) || 0));
  const existing = $$("input", list).map((i) => i.value);
  list.replaceChildren();
  for (let i = 0; i < count; i++) {
    list.append(el("label", {}, `Box ${i + 1} height (inches)`,
      el("input", { type: "number", min: "0.5", max: "8.5", step: "0.25",
                    value: existing[i] || "2", "data-written": i })));
  }
}

function resetNames() {
  parsedNames = null;
  $("#names-count").textContent = "";
  show($("#names-preview"), false);
}

async function checkNames() {
  const errorBox = $("#create-error");
  setAlert(errorBox, "");
  const text = csvText ?? $("#names-text").value;
  try {
    const res = await api("/api/roster/parse", {
      method: "POST", json: { text, source: csvText !== null ? "csv" : "paste" } });
    parsedNames = res.names;
    $("#names-count").textContent = `${parsedNames.length} student${parsedNames.length === 1 ? "" : "s"} found`;
    const preview = $("#names-preview");
    preview.replaceChildren(...parsedNames.map((n) => el("li", {}, n)));
    show(preview, true);
    return true;
  } catch (err) {
    parsedNames = null;
    setAlert(errorBox, err.message, "error");
    return false;
  }
}

function validateCreateForm() {
  const problems = [];
  const mark = (input, bad) => input.classList.toggle("invalid", bad);
  const cls = $("#class-name"), exam = $("#exam-name"), nq = $("#num-questions");
  mark(cls, !cls.value.trim()); if (!cls.value.trim()) problems.push("Enter a class name.");
  mark(exam, !exam.value.trim()); if (!exam.value.trim()) problems.push("Enter an exam name.");
  const q = Number(nq.value);
  const qBad = !Number.isInteger(q) || q < 1 || q > 100;
  mark(nq, qBad); if (qBad) problems.push("Number of questions must be a whole number from 1 to 100.");
  $$("[data-written]").forEach((input, i) => {
    const h = Number(input.value);
    const bad = !(h >= 0.5 && h <= 8.5);
    mark(input, bad);
    if (bad) problems.push(`Written box ${i + 1} must be between 0.5 and 8.5 inches tall.`);
  });
  const hasNames = (csvText ?? $("#names-text").value).trim().length > 0;
  mark($("#names-text"), !hasNames);
  if (!hasNames) problems.push("Add student names (paste them or upload a CSV).");
  return problems;
}

async function createSheets(e) {
  e.preventDefault();
  const errorBox = $("#create-error");
  const problems = validateCreateForm();
  if (problems.length) { setAlert(errorBox, problems, "error"); return; }
  if (!parsedNames && !(await checkNames())) return;

  const btn = $("#create-btn");
  btn.disabled = true;
  btn.textContent = "Creating PDF…";
  try {
    const body = {
      class_name: $("#class-name").value.trim(),
      exam_name: $("#exam-name").value.trim(),
      num_questions: Number($("#num-questions").value),
      num_choices: Number($("#num-choices").value),
      written_heights: $$("[data-written]").map((i) => Number(i.value)),
      names: parsedNames,
    };
    const blob = await api("/api/sheets", { method: "POST", json: body, blob: true });
    const safe = (s) => s.replace(/[^A-Za-z0-9._-]+/g, "_");
    downloadBlob(blob, `${safe(body.class_name)}_${safe(body.exam_name)}_sheets.pdf`);
    setAlert(errorBox, `Created ${parsedNames.length} student sheets plus the answer key sheet ` +
      "(page 1). Print them all; fill in the answer key yourself.", "ok");
  } catch (err) {
    setAlert(errorBox, err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Create bubble sheets (PDF)";
  }
}

/* ============================================================ accounts ==== */

let currentUser = null;
let authConfigured = true;
let resetToken = null;

async function loadMe() {
  try {
    const res = await api("/api/auth/me");
    currentUser = res.user;
    authConfigured = res.configured;
  } catch { currentUser = null; }
  renderAccount();
}

function renderAccount() {
  const box = $("#account");
  box.replaceChildren();
  if (currentUser) {
    const name = [currentUser.first_name, currentUser.last_name].filter(Boolean).join(" ");
    box.append(el("span", {}, name || currentUser.email),
      el("button", { class: "link", type: "button", onclick: () => openPasswordDialog("change") },
        "Change password"),
      el("button", { class: "ghost small-btn", type: "button", onclick: signOut }, "Sign out"));
  }
}

function setupAuth() {
  $$("[data-auth]").forEach((b) => b.addEventListener("click", () => showAuthForm(b.dataset.auth)));
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    await authAction(() => api("/api/auth/login", { method: "POST", json: {
      email: $("#login-email").value, password: $("#login-password").value } }), true);
  });
  $("#signup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!$("#su-adult").checked) {
      setAlert($("#auth-message"), "You must confirm that you are 18 or older.", "error");
      return;
    }
    await authAction(() => api("/api/auth/signup", { method: "POST", json: {
      first_name: $("#su-first").value, last_name: $("#su-last").value,
      email: $("#su-email").value, password: $("#su-password").value,
      is_adult: $("#su-adult").checked } }));
  });
  $("#forgot-btn").addEventListener("click", async () => {
    const email = $("#login-email").value;
    if (!email.trim()) {
      setAlert($("#auth-message"), "Enter your email above, then click “Forgot password?”", "warn");
      return;
    }
    await authAction(() => api("/api/auth/forgot", { method: "POST", json: { email } }));
  });
  $("#password-form").addEventListener("submit", savePassword);
  $("#password-cancel").addEventListener("click", () => $("#password-dialog").close());
}

/** Show either the sign-in or the create-account form (never both). */
function showAuthForm(which) {
  show($("#login-form"), which === "login");
  show($("#signup-form"), which === "signup");
  $("#auth-title").textContent = which === "signup" ? "Create a teacher account" : "Sign in to grade";
  setAlert($("#auth-message"), "");
}

/*
 * New-password dialog. It opens only in two situations:
 *   "reset":  the teacher followed a password-reset email link (resetToken is set)
 *   "change": a signed-in teacher chose "Change password"
 */
let passwordMode = null;

function openPasswordDialog(mode) {
  passwordMode = mode;
  $("#new-password").value = "";
  setAlert($("#password-message"), "");
  $("#password-dialog").showModal();
  $("#new-password").focus();
}

async function savePassword(e) {
  e.preventDefault();
  const msg = $("#password-message");
  const password = $("#new-password").value;
  if (password.length < 8) { setAlert(msg, "Passwords must be at least 8 characters.", "error"); return; }
  try {
    const res = passwordMode === "reset"
      ? await api("/api/auth/reset", { method: "POST", json: { access_token: resetToken, password } })
      : await api("/api/auth/password", { method: "POST", json: { password } });
    $("#password-dialog").close();
    if (passwordMode === "reset") {
      resetToken = null;
      selectTab("grade");
      showAuthForm("login");
      setAlert($("#auth-message"), res.message, "ok");
    } else {
      setAlert($("#global-message"), res.message, "ok");
    }
  } catch (err) {
    setAlert(msg, err.message, "error");
  }
}

/** Run a sign-in/up action and show its result. Returns true on success. */
async function authAction(fn, isLogin = false) {
  const msg = $("#auth-message");
  setAlert(msg, "");
  try {
    const res = await fn();
    if (isLogin) {
      currentUser = res.user;
      renderAccount();
      await refreshGrade();
    } else if (res.message) {
      setAlert(msg, res.message, "ok");
    }
    return true;
  } catch (err) {
    setAlert(msg, err.message, "error");
    return false;
  }
}

async function signOut() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* ignore */ }
  currentUser = null;
  renderAccount();
  refreshGrade();
}

/** Supabase email links come back with details in the URL #fragment. */
function handleAuthRedirect() {
  if (location.hash === "#grade") { selectTab("grade"); return; }  // direct link to grading
  if (!location.hash || location.hash.length < 2) return;
  const params = new URLSearchParams(location.hash.slice(1));
  history.replaceState(null, "", location.pathname);  // don't leave tokens in the URL bar
  const msg = $("#auth-message");
  if (params.get("error_description")) {
    selectTab("grade");
    setAlert(msg, params.get("error_description").replace(/\+/g, " "), "error");
  } else if (params.get("type") === "recovery" && params.get("access_token")) {
    resetToken = params.get("access_token");
    selectTab("grade");
    openPasswordDialog("reset");
  } else if (params.get("type") === "signup" || params.get("type") === "email") {
    selectTab("grade");
    setAlert(msg, "Your email is confirmed. You can sign in now.", "ok");
  }
}

/* ============================================================= grading ==== */

let gradeState = null;

async function refreshGrade() {
  const signedIn = Boolean(currentUser);
  show($("#auth-box"), !signedIn);
  if (!signedIn) {
    show($("#start-box"), false);
    show($("#session-box"), false);
    if (!authConfigured) {
      setAlert($("#auth-message"), "Sign-in is not configured on this server yet " +
        "(the administrator needs to add Supabase settings).", "warn");
    }
    return;
  }
  try {
    const st = await api("/api/grade/state");
    renderGrade(st.active ? st : null);
  } catch (err) {
    if (err.status === 401) { currentUser = null; renderAccount(); refreshGrade(); return; }
    setAlert($("#global-message"), err.message, "error");
  }
}

function renderGrade(state) {
  gradeState = state;
  show($("#start-box"), !state);
  show($("#session-box"), Boolean(state));
  if (!state) return;

  $$(".ttl").forEach((n) => { n.textContent = state.expires_minutes; });
  $("#exam-title").textContent = state.exam
    ? `${state.exam.class_name} — ${state.exam.exam_name}` : "New exam";

  // Bubbles next to each step fill in as the step is completed.
  const graded = state.students.filter((s) => s.bubble_page_read).length;
  $("#step-key").classList.toggle("done", Boolean(state.key));
  $("#step-upload").classList.toggle("done", graded > 0 && state.done);
  $("#step-review").classList.toggle("done", state.done && state.flags.every((f) => f.resolved));
  $("#step-results").classList.toggle("done", state.done);

  // Step 1: key
  $("#key-status").textContent = !state.key ? "No key loaded yet"
    : state.imported_files.length
      ? `From results: ${state.imported_files.join(", ")} · ${state.key.num_questions} questions`
      : `Loaded from ${state.key.source} · ${state.key.num_questions} questions`;
  // The key can't change once students are graded against it.
  $("#key-file").disabled = state.students.length > 0;
  $("#key-file").closest("label").title = state.students.length
    ? "Start a new grading session to use a different answer key." : "";

  // Step 2: uploads, locked until there is a key
  show($("#upload-locked"), !state.key);
  show($("#upload-area"), Boolean(state.key));
  renderPageMessages(state.messages);

  // Steps 3-4
  show($("#results-area"), state.done);
  if (state.done) {
    renderFlags(state);
    renderResults(state);
  }
}

function renderPageMessages(messages) {
  const box = $("#page-messages");
  box.replaceChildren();
  const errors = messages.filter((m) => m.level === "error");
  if (errors.length) {
    box.append(el("div", { class: "alert error" },
      el("strong", {}, `${errors.length} page${errors.length === 1 ? "" : "s"} could not be read:`),
      el("ul", {}, errors.map((m) => el("li", {}, `${m.source}: ${m.message}`)))));
  }
}

async function handleApiError(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err.status === 409) { renderGrade(null); }
    setAlert($("#global-message"), err.message, "error");
    window.scrollTo({ top: 0, behavior: "smooth" });
    return null;
  }
}

function setupGrading() {
  $("#start-btn").addEventListener("click", () => handleApiError(async () => {
    setAlert($("#global-message"), "");
    renderGrade(await api("/api/grade/start", { method: "POST" }));
  }));

  // Continue from results CSV(s): start a fresh session, then load the files.
  $("#start-import").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files);
    e.target.value = "";
    if (!files.length) return;
    const ok = await handleApiError(() => api("/api/grade/start", { method: "POST" }));
    if (!ok) return;
    const loaded = await importResults(files, $("#start-message"));
    if (!loaded) { await api("/api/grade/clear", { method: "POST" }).catch(() => {}); return; }
    $("#step-results").scrollIntoView({ behavior: "smooth" });
  });
  $("#import-file").addEventListener("change", async (e) => {
    const files = Array.from(e.target.files);
    e.target.value = "";
    if (files.length) await importResults(files, $("#key-error"));
  });

  $("#clear-btn").addEventListener("click", () => {
    if (!confirm("Delete all grading data for this session? Download your results first.")) return;
    handleApiError(async () => {
      await api("/api/grade/clear", { method: "POST" });
      $("#upload-log").replaceChildren();
      renderGrade(null);
    });
  });

  $("#key-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const errBox = $("#key-error");
    setAlert(errBox, "");
    $("#key-status").textContent = "Reading answer key…";
    const form = new FormData();
    form.append("file", file);
    try {
      renderGrade(await api("/api/grade/key", { method: "POST", form }));
    } catch (err) {
      $("#key-status").textContent = gradeState?.key ? "" : "No key loaded yet";
      setAlert(errBox, err.message, "error");
    }
  });

  const input = $("#sheet-files");
  input.addEventListener("change", () => { uploadFiles(Array.from(input.files)); input.value = ""; });
  const zone = $("#drop-zone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("over");
    uploadFiles(Array.from(e.dataTransfer.files));
  });

  $("#done-btn").addEventListener("click", () => handleApiError(async () => {
    renderGrade(await api("/api/grade/done", { method: "POST" }));
    $("#step-review").scrollIntoView({ behavior: "smooth" });
  }));

  $("#multi-mode").addEventListener("change", (e) => handleApiError(async () => {
    renderGrade(await api("/api/grade/settings", { method: "POST", json: { multi_mode: e.target.value } }));
  }));

  $$("[data-export]").forEach((a) => {
    a.href = `/api/grade/export/${a.dataset.export}`;
  });
}

/** Load results CSVs one at a time. Returns true if at least one loaded. */
async function importResults(files, messageBox) {
  const lines = [];
  let kind = "ok";
  let loaded = false;
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await api("/api/grade/import", { method: "POST", form });
      const s = res.summary;
      loaded = true;
      lines.push(`${s.file}: ${s.added} student${s.added === 1 ? "" : "s"} loaded` +
        (s.replaced ? `, ${s.replaced} replaced` : "") + ".");
      if (s.warnings.length) { lines.push(...s.warnings); kind = "warn"; }
      renderGrade(res.state);
    } catch (err) {
      lines.push(`${file.name}: ${err.message}`);
      kind = "error";
      if (err.status === 409) { renderGrade(null); break; }
    }
  }
  setAlert(messageBox, lines, kind);
  if (loaded && messageBox.id !== "key-error") setAlert($("#key-error"), lines, kind);
  return loaded;
}

/** Upload files one at a time so each gets its own progress line. */
async function uploadFiles(files) {
  if (!files.length) return;
  const log = $("#upload-log");
  const doneBtn = $("#done-btn");
  doneBtn.disabled = true;
  for (const file of files) {
    const status = el("span", { class: "status" }, "reading…");
    log.prepend(el("li", {}, el("span", { class: "fname" }, file.name), status));
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await api("/api/grade/upload", { method: "POST", form });
      const s = res.summary;
      const bad = s.messages.filter((m) => m.level === "error").length;
      status.textContent = `${s.pages} page${s.pages === 1 ? "" : "s"}, ` +
        `${s.graded.length} student sheet${s.graded.length === 1 ? "" : "s"} read` +
        (bad ? `, ${bad} problem${bad === 1 ? "" : "s"} (see below)` : "");
      status.className = "status " + (bad ? "err" : "ok");
      renderGrade(res.state);
    } catch (err) {
      status.textContent = err.message;
      status.className = "status err";
      if (err.status === 409) { renderGrade(null); break; }
    }
  }
  doneBtn.disabled = false;
}

/* ---- step 3: flagged answers ---- */

function renderFlags(state) {
  const list = $("#flag-list");
  list.replaceChildren();
  const open = state.flags.filter((f) => !f.resolved).length;
  const missing = state.students.filter((s) => !s.bubble_page_read);
  let summary = state.flags.length === 0
    ? "Every bubble was read clearly. Nothing to check."
    : `${open} of ${state.flags.length} flagged answer${state.flags.length === 1 ? "" : "s"} ` +
      `still need${open === 1 ? "s" : ""} checking. Look at the image (or the paper sheet), ` +
      "select what the student marked, and click Confirm. " +
      "Unchecked answers are scored as read by the scanner and marked “?” in the CSV.";
  if (missing.length) {
    summary += ` Missing bubble page for: ${missing.map((s) => s.name).join(", ")}.`;
  }
  $("#review-summary").textContent = summary;

  const k = state.exam.num_choices;
  for (const f of state.flags) {
    let selected = new Set(f.answer.split(""));
    const buttons = [];
    for (let c = 0; c < k; c++) {
      const letter = LETTERS[c];
      const b = el("button", { type: "button", class: selected.has(letter) ? "on" : "",
        "aria-pressed": selected.has(letter) ? "true" : "false" }, letter);
      b.addEventListener("click", () => {
        if (selected.has(letter)) selected.delete(letter); else selected.add(letter);
        b.classList.toggle("on");
        b.setAttribute("aria-pressed", String(selected.has(letter)));
      });
      buttons.push(b);
    }
    const confirmBtn = el("button", { type: "button", class: "primary" },
      f.resolved ? "Update" : "Confirm");
    confirmBtn.addEventListener("click", () => handleApiError(async () => {
      const answer = LETTERS.slice(0, k).split("").filter((l) => selected.has(l)).join("");
      renderGrade(await api("/api/grade/resolve", { method: "POST",
        json: { student_index: f.student_index, question: f.question, answer } }));
    }));
    list.append(el("div", { class: "flag" + (f.resolved ? " resolved" : "") },
      el("div", {},
        el("div", {}, el("span", { class: "flag-title" }, f.student),
          el("span", { class: "flag-q" }, `Q${f.question}`), " ",
          f.resolved ? el("span", { class: "tag checked" }, "checked")
                     : el("span", { class: "tag" }, "check")),
        el("div", { class: "flag-note" }, f.description +
          (f.detected ? ` Scanner read: ${f.detected}.` : " Scanner read: blank.") +
          (f.multi_correct ? " (This question has more than one correct answer.)" : "")),
        f.has_image ? el("img", { src: `/api/grade/snippet/${f.student_index}/${f.question}.png`,
                    alt: `Scanned row for question ${f.question}`, loading: "lazy" }) : null),
      el("div", {},
        el("div", { class: "choices", role: "group", "aria-label": "Student's answer" }, buttons),
        el("div", { class: "row" }, confirmBtn,
          el("span", { class: "hint" }, "Leave all empty for a blank answer"))),
    ));
  }
}

/* ---- step 4: results table, downloads, charts ---- */

function renderResults(state) {
  show($("#multi-mode-box"), state.key.multi_answer_questions.length > 0);
  $("#multi-mode").value = state.multi_mode;

  const nWritten = state.exam.num_written;
  const table = $("#results-table");
  // Show a Class column only when results from several classes are combined.
  const showClass = new Set(state.students.map((s) => s.class_name)).size > 1;
  const head = el("tr", {}, el("th", {}, "Student"), showClass ? el("th", {}, "Class") : null,
    el("th", {}, "Score"), el("th", {}, "%"),
    Array.from({ length: nWritten }, (_, i) => el("th", {}, `Written ${i + 1}`)),
    el("th", {}, "Notes"));
  const rows = state.students.map((s) => {
    const notes = [];
    if (!s.bubble_page_read) notes.push("bubble page not scanned");
    if (s.open_flags) notes.push(`${s.open_flags} to check`);
    notes.push(...s.notes);
    return el("tr", {},
      el("td", {}, s.name),
      showClass ? el("td", {}, s.class_name) : null,
      el("td", {}, s.score === null ? "—" : `${s.score} / ${s.possible}`),
      el("td", {}, s.percent === null ? "—" : `${s.percent}%`),
      s.written.map((w) => el("td", {}, writtenInput(s, w))),
      el("td", {}, notes.length ? el("span", { class: "tag" }, notes.join(", ")) : ""));
  });
  table.replaceChildren(el("thead", {}, head), el("tbody", {}, rows));

  // Charts: add a changing query so the browser fetches fresh images.
  const bust = Date.now();
  $$("[data-chart]").forEach((img) => {
    img.src = `/api/grade/export/${img.dataset.chart}?inline=true&t=${bust}`;
  });
}

function writtenInput(student, w) {
  const input = el("input", { type: "number", min: "0", step: "0.5", value: w.score ?? "",
    "aria-label": `${student.name} written response ${w.number} score` });
  input.addEventListener("change", () => handleApiError(async () => {
    const v = input.value.trim();
    await api("/api/grade/written", { method: "POST", json: {
      student_index: student.index, number: w.number, score: v === "" ? null : Number(v) } });
    w.score = v === "" ? null : Number(v);
  }));
  return input;
}

/* =============================================================== start ==== */

setupCreateForm();
setupAuth();
setupGrading();
loadMe().then(handleAuthRedirect);  // switching to the Grade tab loads its state
