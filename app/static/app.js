"use strict";

(() => {
  const MAX_BYTES = 5 * 1024 * 1024;
  const ALLOWED = ["image/jpeg", "image/png"];
  const STORE_KEY = "civitstop.receipt";
  const POLL_MS = 4000;
  const POLL_MAX_MS = 15000;
  const POLL_LIMIT_MS = 5 * 60 * 1000;

  const MSG = {
    image: "Choose a JPEG or PNG photo.",
    imageSize: "This photo is larger than 5 MB. Choose a smaller one.",
    address: "Enter the bus stop address or nearest intersection.",
    gps: "Enter a valid latitude and longitude, or use an address.",
    email: "Enter a valid email address, like name@example.com.",
    observations: "Choose at least one observation.",
    server503: "We couldn't save your report right now. Nothing was submitted. Please try again.",
    network: "We couldn't reach the server. Your report may not have been saved. Try again; it won't create a duplicate.",
  };

  const LABELS = {
    trees_vegetation: "Trees / vegetation",
    missing_shelter: "Missing bus shelter",
    missing_seating: "Missing seating",
    trash_cans: "Trash cans",
    bus_info_posters: "Bus information posters",
    signage: "Signage",
    other: "Other",
  };
  const STATUS_TEXT = {
    pending: "Pending: waiting for analysis",
    processing: "Processing: analyzing your photo",
    completed: "Completed: report ready",
    failed: "Failed",
  };
  const VISIBILITY = {
    visible: "Seen in photo",
    not_visible: "Not seen in this photo",
    unclear: "Unclear",
    not_assessable: "Can't be assessed from this photo",
  };
  const EVIDENCE = {
    visual: ["From photo", "badge-photo"],
    user_report: ["Reported by you", "badge-user"],
    both: ["Photo + your report", "badge-photo"],
  };

  const $ = (id) => document.getElementById(id);
  const form = $("report-form");
  const imageInput = $("image");
  const preview = $("image-preview");
  const addressInput = $("address");
  const latInput = $("latitude");
  const lngInput = $("longitude");
  const emailInput = $("email");
  const submitBtn = $("submit-btn");
  const summary = $("error-summary");
  const receipt = $("receipt");
  const reportView = $("report-view");
  const lookupForm = $("lookup-form");

  let idemKey = null;
  let inFlight = false;
  let previewUrl = null;
  let poll = null;

  // ---------- helpers ----------
  const el = (tag, opts = {}, ...children) => {
    const node = document.createElement(tag);
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = String(opts.text);
    for (const [k, v] of Object.entries(opts.attrs || {})) node.setAttribute(k, v);
    for (const c of children) if (c) node.append(c);
    return node;
  };
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const storage = {
    get() { try { return JSON.parse(sessionStorage.getItem(STORE_KEY) || "null"); } catch { return null; } },
    set(v) { try { sessionStorage.setItem(STORE_KEY, JSON.stringify(v)); } catch { /* ignore */ } },
    clear() { try { sessionStorage.removeItem(STORE_KEY); } catch { /* ignore */ } },
  };

  const mode = () => (document.querySelector('input[name="loc-mode"]:checked') || {}).value || "address";
  const checkedValues = () =>
    Array.from(form.querySelectorAll('input[name="observations"]:checked')).map((c) => c.value);

  // ---------- form behavior ----------
  imageInput.addEventListener("change", () => {
    idemKey = null; // a new photo means a new intended submission
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    const file = imageInput.files[0];
    if (file && ALLOWED.includes(file.type)) {
      previewUrl = URL.createObjectURL(file);
      preview.src = previewUrl;
      preview.hidden = false;
    } else {
      preview.removeAttribute("src");
      preview.hidden = true;
    }
  });

  function applyMode() {
    const gps = mode() === "gps";
    $("address-group").hidden = gps;
    $("gps-group").hidden = !gps;
    addressInput.disabled = gps;
    latInput.disabled = !gps;
    lngInput.disabled = !gps;
  }
  document.querySelectorAll('input[name="loc-mode"]').forEach((r) => r.addEventListener("change", applyMode));

  $("use-gps").addEventListener("click", () => {
    const status = $("gps-status");
    if (!("geolocation" in navigator)) {
      status.textContent = "Location isn't available in this browser. Enter an address instead.";
      return;
    }
    status.textContent = "Getting your location…";
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        latInput.value = pos.coords.latitude.toFixed(6);
        lngInput.value = pos.coords.longitude.toFixed(6);
        status.textContent = "Location captured.";
      },
      (err) => {
        status.textContent = err.code === err.PERMISSION_DENIED
          ? "Location permission was denied. Enter an address instead."
          : "We couldn't get your location. Enter coordinates or an address instead.";
      },
      { enableHighAccuracy: true, timeout: 10000 }
    );
  });

  function buildLocation() {
    if (mode() === "gps") {
      return { type: "gps", latitude: Number(latInput.value.trim()), longitude: Number(lngInput.value.trim()) };
    }
    return { type: "address", address: addressInput.value.trim() };
  }

  // ---------- validation ----------
  const FIELD_TARGET = { image: "image", location: null, email: "email", observations: "observations" };

  function locationTarget() { return mode() === "gps" ? "latitude" : "address"; }

  function clearErrors() {
    summary.hidden = true;
    summary.replaceChildren();
    for (const id of ["image", "location", "email", "observations"]) $(`${id}-error`).textContent = "";
    for (const id of ["image", "address", "latitude", "longitude", "email", "observations"]) $(id).removeAttribute("aria-invalid");
  }

  function setFieldError(field, message) {
    const errEl = $(`${field}-error`);
    if (!errEl) return null;
    errEl.textContent = message;
    const targets = field === "location"
      ? (mode() === "gps" ? ["latitude", "longitude"] : ["address"])
      : [FIELD_TARGET[field]];
    targets.forEach((t) => $(t) && $(t).setAttribute("aria-invalid", "true"));
    return field === "location" ? locationTarget() : FIELD_TARGET[field];
  }

  function showSummary(items) {
    summary.replaceChildren();
    if (typeof items === "string") {
      summary.append(el("p", { text: items }));
    } else {
      const n = items.length;
      summary.append(el("h2", { text: `Please fix ${n} problem${n === 1 ? "" : "s"}` }));
      const ul = el("ul");
      for (const { target, message } of items) {
        const a = el("a", { text: message, attrs: { href: `#${target}` } });
        a.addEventListener("click", (e) => { e.preventDefault(); $(target).focus(); });
        ul.append(el("li", {}, a));
      }
      summary.append(ul);
    }
    summary.hidden = false;
    summary.focus();
  }

  function validate() {
    clearErrors();
    const errors = [];
    const add = (field, message) => errors.push({ target: setFieldError(field, message), message });

    const file = imageInput.files[0];
    if (!file || !ALLOWED.includes(file.type)) add("image", MSG.image);
    else if (file.size > MAX_BYTES) add("image", MSG.imageSize);

    if (mode() === "gps") {
      const lat = latInput.value.trim(), lng = lngInput.value.trim();
      const la = Number(lat), lo = Number(lng);
      if (lat === "" || lng === "" || !Number.isFinite(la) || !Number.isFinite(lo) ||
          la < -90 || la > 90 || lo < -180 || lo > 180) add("location", MSG.gps);
    } else {
      const a = addressInput.value.trim();
      if (a.length < 1 || a.length > 256) add("location", MSG.address);
    }

    const email = emailInput.value.trim();
    if (email.length > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) add("email", MSG.email);

    if (checkedValues().length === 0) add("observations", MSG.observations);

    if (errors.length) { showSummary(errors); return false; }
    return true;
  }

  // ---------- submit ----------
  function setBusy(busy) {
    submitBtn.disabled = busy;
    submitBtn.textContent = busy ? "Submitting…" : "Submit report";
    form.setAttribute("aria-busy", busy ? "true" : "false");
  }

  async function fetchWithRetry(url, options, retries = 2) {
    const delays = [1000, 2000];
    for (let attempt = 0; ; attempt++) {
      try {
        return await fetch(url, options);
      } catch (err) {
        if (attempt >= retries) throw err;
        await sleep(delays[attempt]);
      }
    }
  }

  function showServerError(status, body) {
    const err = (body && body.error) || {};
    if (status >= 500 || status === 0) { showSummary(MSG.server503); return; }
    const field = err.field && $(`${err.field}-error`) ? err.field : null;
    const message = err.message || "Please check the form and try again.";
    if (field) showSummary([{ target: setFieldError(field, message), message }]);
    else showSummary(message);
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (inFlight || !validate()) return;
    inFlight = true;
    idemKey = idemKey || newKey();
    setBusy(true);

    const fd = new FormData();
    fd.append("image", imageInput.files[0]);
    fd.append("location", JSON.stringify(buildLocation()));
    fd.append("email", emailInput.value.trim());
    fd.append("observations", JSON.stringify(checkedValues()));

    try {
      const res = await fetchWithRetry("/api/v1/submissions", {
        method: "POST", body: fd, headers: { "Idempotency-Key": idemKey },
      });
      let body = null;
      try { body = await res.json(); } catch { /* non-JSON */ }
      if (res.status === 202 && body && body.receipt_token) {
        idemKey = null;
        onAccepted(body);
      } else {
        showServerError(res.status, body);
      }
    } catch {
      showSummary(MSG.network);
    } finally {
      inFlight = false;
      setBusy(false);
    }
  });

  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = new Uint8Array(16);
    crypto.getRandomValues(b);
    return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  }

  // ---------- receipt + polling ----------
  function onAccepted(body) {
    const rec = { reference_id: body.reference_id, receipt_token: body.receipt_token };
    storage.set(rec);
    showReceipt(rec, body.submission_status || "pending");
    $("receipt-heading").focus();
    startPolling(rec);
  }

  function showReceipt(rec, status, title = "Submitted successfully") {
    clearErrors();
    form.hidden = true;
    lookupForm.hidden = true;
    $("lookup-switch").hidden = true;
    receipt.hidden = false;
    $("receipt-title").textContent = title;
    $("ref-id").textContent = rec.reference_id;
    $("ref-token").textContent = rec.receipt_token;
    renderStatus(status, null, true);
  }

  let lastStatus = null;
  function renderStatus(status, error, silent = false) {
    const node = $("status-text");
    node.dataset.status = status;
    let text = STATUS_TEXT[status] || status;
    if (status === "failed") text = `Failed: ${(error && error.message) || "We couldn't generate a report."}`;
    node.textContent = text;
    if (status !== lastStatus) {
      if (!silent) $("status-live").textContent = `Status: ${text}`;
      lastStatus = status;
    }
  }

  function stopPolling() {
    if (poll && poll.timer) clearTimeout(poll.timer);
    poll = null;
  }

  function startPolling(rec) {
    stopPolling();
    $("poll-message").textContent = "";
    $("check-again").hidden = true;
    poll = { rec, started: Date.now(), failures: 0, timer: null, delay: POLL_MS };
    schedule(0);
  }

  function schedule(ms) {
    if (!poll) return;
    poll.timer = setTimeout(tick, ms);
  }

  async function tick() {
    if (!poll) return;
    if (document.hidden) return; // resumed by visibilitychange
    if (Date.now() - poll.started > POLL_LIMIT_MS) {
      $("poll-message").textContent = "Still processing. Check back in this tab later.";
      $("check-again").hidden = false;
      stopPolling();
      return;
    }
    const { rec } = poll;
    try {
      const res = await fetch(`/api/v1/submissions/${encodeURIComponent(rec.reference_id)}`, {
        headers: { Authorization: `Bearer ${rec.receipt_token}` }, cache: "no-store",
      });
      if (!poll) return;
      if (res.status === 404 || res.status === 401) {
        $("poll-message").textContent = "This report can't be found in this session.";
        storage.clear();
        stopPolling();
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      poll.failures = 0;
      poll.delay = POLL_MS;
      renderStatus(data.status, data.error);
      if (data.status === "completed" && data.report) {
        stopPolling();
        renderReport(data);
        return;
      }
      if (data.status === "failed") { stopPolling(); return; }
      schedule(POLL_MS);
    } catch {
      if (!poll) return;
      poll.failures += 1;
      poll.delay = Math.min(poll.delay * 2, POLL_MAX_MS);
      if (poll.failures >= 3) $("poll-message").textContent = "Having trouble reaching the server. Still trying…";
      schedule(poll.delay);
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && poll) { clearTimeout(poll.timer); schedule(0); }
  });

  $("check-again").addEventListener("click", () => {
    const rec = storage.get();
    if (rec) startPolling(rec);
  });

  async function copyText(id, name) {
    const value = $(id).textContent;
    try {
      await navigator.clipboard.writeText(value);
      $("status-live").textContent = `${name} copied.`;
    } catch {
      $("status-live").textContent = `${name} is ${value}`;
    }
  }
  $("copy-ref").addEventListener("click", () => copyText("ref-id", "Reference"));
  $("copy-token").addEventListener("click", () => copyText("ref-token", "Receipt token"));

  $("new-report").addEventListener("click", () => {
    stopPolling();
    storage.clear();
    lastStatus = null;
    form.reset();
    applyMode();
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
    preview.hidden = true;
    preview.removeAttribute("src");
    $("gps-status").textContent = "";
    $("report-body").replaceChildren();
    reportView.hidden = true;
    receipt.hidden = true;
    lookupForm.hidden = true;
    lookupForm.reset();
    $("lookup-switch").hidden = false;
    form.hidden = false;
    idemKey = null;
    imageInput.focus();
  });

  // ---------- look up an existing report ----------
  const REF_RE = /^BUS-\d{4}-[A-Z0-9]{8}$/;
  const TOKEN_RE = /^[A-Za-z0-9_-]{20,128}$/;
  const LOOKUP_MSG = {
    ref: "Enter a reference like BUS-2026-AB12CD34.",
    token: "Enter the receipt token you saved when you submitted.",
    notFound: "We couldn't find a report with that reference and receipt token. Check both and try again.",
    server: "We couldn't load this report right now. Please try again.",
  };

  function clearLookupErrors() {
    clearErrors();
    for (const id of ["lookup-ref", "lookup-token"]) {
      $(`${id}-error`).textContent = "";
      $(id).removeAttribute("aria-invalid");
    }
  }

  function lookupFieldError(id, message) {
    $(`${id}-error`).textContent = message;
    $(id).setAttribute("aria-invalid", "true");
    return { target: id, message };
  }

  function setLookupMode(on) {
    clearErrors();
    form.hidden = on;
    lookupForm.hidden = !on;
    $("lookup-switch").hidden = on;
    if (on) $("lookup-heading").focus();
    else imageInput.focus();
  }
  $("show-lookup").addEventListener("click", () => setLookupMode(true));
  $("hide-lookup").addEventListener("click", () => setLookupMode(false));

  lookupForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (inFlight) return;
    clearLookupErrors();
    const ref = $("lookup-ref").value.trim().toUpperCase();
    const token = $("lookup-token").value.trim();
    const errors = [];
    if (!REF_RE.test(ref)) errors.push(lookupFieldError("lookup-ref", LOOKUP_MSG.ref));
    if (!TOKEN_RE.test(token)) errors.push(lookupFieldError("lookup-token", LOOKUP_MSG.token));
    if (errors.length) { showSummary(errors); return; }

    inFlight = true;
    const btn = $("lookup-btn");
    btn.disabled = true;
    btn.textContent = "Looking up…";
    try {
      const res = await fetchWithRetry(`/api/v1/submissions/${encodeURIComponent(ref)}`, {
        headers: { Authorization: `Bearer ${token}` }, cache: "no-store",
      });
      if (res.status === 404 || res.status === 401) { showSummary(LOOKUP_MSG.notFound); return; }
      if (!res.ok) { showSummary(LOOKUP_MSG.server); return; }
      const data = await res.json();
      const rec = { reference_id: data.reference_id, receipt_token: token };
      storage.set(rec);
      showReceipt(rec, data.status, "Report found");
      renderStatus(data.status, data.error, true);
      if (data.status === "completed" && data.report) renderReport(data);
      else {
        $("receipt-heading").focus();
        if (data.status !== "failed") startPolling(rec);
      }
    } catch {
      showSummary("We couldn't reach the server. Check your connection and try again.");
    } finally {
      inFlight = false;
      btn.disabled = false;
      btn.textContent = "View report";
    }
  });

  // ---------- report rendering (textContent only; model output is untrusted) ----------
  function fmtDate(iso) {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }
  function fmtLocation(loc) {
    if (!loc) return "";
    if (loc.type === "gps") return `GPS ${loc.latitude}, ${loc.longitude}`;
    return String(loc.address || "");
  }
  const label = (cat) => LABELS[cat] || String(cat);

  function renderReport(data) {
    const r = data.report;
    const body = $("report-body");
    body.replaceChildren();

    body.append(el("p", { className: "notice", text: r.report_notice || "AI-assisted observation; not an official inspection or agency submission." }));
    body.append(el("p", { className: "meta", text: `Reference ${r.reference_id || data.reference_id}` }));
    body.append(el("p", { className: "meta", text: `Submitted ${fmtDate(r.submitted_at || data.created_at)}` }));
    body.append(el("p", { className: "meta", text: `Location (reported by you): ${fmtLocation(r.location || data.location)}` }));

    body.append(el("h3", { text: "What you reported" }));
    const ul = el("ul");
    for (const c of r.user_observations || data.user_observations || []) ul.append(el("li", { text: label(c) }));
    body.append(ul);

    body.append(el("h3", { text: "What the photo shows" }));
    if (r.image_description) body.append(el("p", { text: r.image_description }));
    if (Array.isArray(r.visual_findings) && r.visual_findings.length) {
      const list = el("ul");
      for (const f of r.visual_findings) {
        const li = el("li");
        li.append(el("strong", { text: `${label(f.category)}: ` }));
        li.append(el("span", { className: "badge badge-photo", text: VISIBILITY[f.visibility] || String(f.visibility) }));
        li.append(el("span", { text: f.description || "" }));
        list.append(li);
      }
      body.append(list);
    }

    body.append(el("h3", { text: "Issues to review" }));
    if (Array.isArray(r.identified_issues) && r.identified_issues.length) {
      const list = el("ul");
      for (const i of r.identified_issues) {
        const li = el("li");
        li.append(el("strong", { text: `${label(i.category)} ` }));
        const [evText, evClass] = EVIDENCE[i.evidence_source] || [String(i.evidence_source), "badge-user"];
        li.append(el("span", { className: `badge ${evClass}`, text: evText }));
        if (i.requires_human_review) li.append(el("span", { className: "badge badge-review", text: "Needs human review" }));
        li.append(el("div", { text: i.description || "" }));
        list.append(li);
      }
      body.append(list);
    } else {
      body.append(el("p", { text: "No issues were identified." }));
    }

    body.append(el("h3", { text: "Suggested next steps" }));
    if (Array.isArray(r.recommended_actions) && r.recommended_actions.length) {
      const ol = el("ol");
      for (const a of r.recommended_actions) ol.append(el("li", { text: a }));
      body.append(ol);
    } else {
      body.append(el("p", { text: "No suggestions." }));
    }

    body.append(el("h3", { text: "Who to contact" }));
    const org = r.responsible_organization || {};
    if (!org.name) {
      body.append(el("p", { className: "contact-null", text: "Contact not verified." }));
    } else {
      body.append(el("p", { text: org.name }));
      if (org.contact) body.append(el("p", { text: org.contact }));
      if (typeof org.source_url === "string" && org.source_url.startsWith("https://")) {
        const p = el("p");
        p.append(el("a", { text: "Source", attrs: { href: org.source_url, rel: "noopener noreferrer", target: "_blank" } }));
        if (org.verified_on) p.append(document.createTextNode(` (verified on ${org.verified_on})`));
        body.append(p);
      }
    }

    if (r.model) body.append(el("p", { className: "model-line", text: `Generated with ${r.model}` }));

    reportView.hidden = false;
    $("report-heading").focus();
  }

  // ---------- init ----------
  applyMode();
  const saved = storage.get();
  if (saved && saved.reference_id && saved.receipt_token) {
    showReceipt(saved, "pending");
    startPolling(saved);
  }
})();
