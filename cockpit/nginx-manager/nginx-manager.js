// Nginx Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const root = document.querySelector(".nginx-shell");
  const state = { sites: [], conflicts: [], warnings: [], busy: false, query: "" };
  const byId = (id) => document.getElementById(id);
  const form = byId("siteForm");
  const iconPaths = {
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/>',
    proxy: '<path d="M4 7h15m-4-4 4 4-4 4M20 17H5m4-4-4 4 4 4"/>',
    lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 1 1 8 0v3m-4 5v2"/>',
    edit: '<path d="m14 5 5 5M4 20l4-.8L19 8a2.1 2.1 0 0 0-3-3L5 16l-1 4Z"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>',
    pause: '<path d="M8 5h3v14H8zM15 5h3v14h-3z"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    warning: '<path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5m0 3h.01"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/>',
  };
  const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.globe}</svg>`;
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  async function callBackend(command, args = []) {
    if (!globalThis.cockpit || typeof globalThis.cockpit.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Reload Nginx Manager inside Cockpit.");
    }
    const process = globalThis.cockpit.spawn([BACKEND, command, ...args], { superuser: "require" });
    let output = "";
    if (typeof process.stream === "function") process.stream((chunk) => { output += chunk; });
    try {
      const completed = await process;
      if (!output) output = completed;
    } catch (failure) {
      let result;
      try { result = JSON.parse(output); } catch (_parseError) { result = null; }
      throw new Error(result?.error || failure.message || "The backend command failed.");
    }
    let result;
    try {
      result = JSON.parse(output);
    } catch (failure) {
      throw new Error(`Backend returned invalid JSON: ${failure.message}`);
    }
    if (!result || typeof result !== "object" || Array.isArray(result) || result.ok === false) {
      throw new Error(result?.error || "The backend returned an invalid response.");
    }
    return result;
  }

  function showError(failure, target = byId("globalError")) {
    target.hidden = false;
    target.textContent = failure?.message || String(failure);
    if (target === byId("globalError")) {
      byId("globalNotice").hidden = true;
      setConnection("error", "Needs attention");
    }
  }

  function showNotice(message) {
    byId("globalError").hidden = true;
    byId("globalNotice").hidden = false;
    byId("globalNotice").textContent = message;
  }

  function setConnection(status, label) {
    byId("connectionStatus").dataset.state = status;
    byId("connectionStatus").querySelector(".connection-label").textContent = label;
  }

  function setBusy(busy) {
    state.busy = busy;
    root.setAttribute("aria-busy", String(busy));
    document.querySelectorAll("button").forEach((button) => {
      button.disabled = busy;
    });
    if (busy) setConnection("busy", "Working…");
    else if (byId("connectionStatus").dataset.state === "busy") setConnection("ready", "Connected");
  }

  function renderService(overview) {
    const serviceState = overview.service_state || "unknown";
    const active = serviceState === "active";
    const stateCard = byId("serviceStateCard");
    stateCard.dataset.state = serviceState;
    byId("serviceState").textContent = active ? "Nginx is running" : `Nginx is ${serviceState}`;
    byId("serviceDescription").textContent = active
      ? "The host web server is serving traffic"
      : "Check the system service before applying changes";
    byId("sidebarServiceState").textContent = active ? "Running" : serviceState;
    byId("serviceLight").dataset.state = serviceState;
    byId("nginxVersion").textContent = overview.nginx_version ? `v${overview.nginx_version}` : "";
  }

  function renderSummary() {
    byId("siteCount").textContent = String(state.sites.length);
    byId("siteCountCaption").textContent = `${state.sites.filter((site) => site.managed).length} managed here`;
    byId("enabledCount").textContent = String(state.sites.filter((site) => site.enabled).length);
    const warningCount = state.conflicts.length + state.warnings.length;
    byId("warningCount").textContent = String(warningCount);
    byId("warningCaption").textContent = warningCount ? "Review the notes below" : "No known conflicts";
  }

  function siteBadge(site) {
    const enabled = site.enabled
      ? '<span class="badge badge-enabled">Enabled</span>'
      : '<span class="badge badge-disabled">Disabled</span>';
    const managed = site.managed
      ? '<span class="badge badge-managed">Managed</span>'
      : '<span class="badge badge-readonly">Inventory</span>';
    return `${enabled}${managed}`;
  }

  function renderSites() {
    const query = state.query.toLowerCase();
    const sites = state.sites.filter((site) => {
      const haystack = [site.name, ...(site.domains || []), site.path, site.mode]
        .join(" ").toLowerCase();
      return haystack.includes(query);
    });
    byId("siteSummary").textContent = `${sites.length} of ${state.sites.length} configured ${state.sites.length === 1 ? "website" : "websites"}`;
    if (!state.sites.length) {
      byId("siteList").innerHTML = `
        <div class="empty-state">
          ${icon("globe")}
          <h3>No virtual hosts found</h3>
          <p>Existing Nginx sites will appear here. Create a managed site when you're ready.</p>
          <button type="button" class="button button-primary" data-action="create">${icon("check")} Create a website</button>
        </div>`;
      return;
    }
    if (!sites.length) {
      byId("siteList").innerHTML = '<div class="empty-state"><h3>No matching websites</h3><p>Try another domain or path.</p></div>';
      return;
    }
    byId("siteList").innerHTML = sites.map((site) => {
      const domains = site.domains?.length ? site.domains : [site.name || "Unnamed server"];
      const modeLabel = site.mode === "proxy" ? "Reverse proxy" : site.mode === "static" ? "Static files" : "Custom Nginx site";
      const ports = site.listen_ports?.length ? site.listen_ports : [site.listen_port || 80];
      const tls = site.tls ? `<span>${icon("lock")} HTTPS / TLS</span>` : "";
      const modeIcon = site.mode === "proxy" ? icon("proxy") : icon("globe");
      const actions = site.managed
        ? `<div class="site-actions">
            <button type="button" class="button button-quiet" data-action="edit" data-id="${escapeHtml(site.site_id)}">${icon("edit")} Edit</button>
            <span class="action-spacer"></span>
            <button type="button" class="button" data-action="${site.enabled ? "disable" : "enable"}" data-id="${escapeHtml(site.site_id)}">${icon(site.enabled ? "pause" : "play")} ${site.enabled ? "Disable" : "Enable"}</button>
          </div>`
        : "";
      const issue = site.management_issue
        ? `<p class="site-issue">${escapeHtml(site.management_issue)}</p>`
        : "";
      return `
        <article class="site-card">
          <div class="site-card-top">
            <span class="site-avatar">${modeIcon}</span>
            <div class="site-title">
              <h3 title="${escapeHtml(domains.join(", "))}">${escapeHtml(domains[0])}</h3>
              <p>${escapeHtml(domains.slice(1).join(" · ") || (site.managed ? "Nginx Manager configuration" : site.path))}</p>
            </div>
            <div class="site-badges">${siteBadge(site)}</div>
          </div>
          <div class="site-meta">
            <span>${icon("layers")}${escapeHtml(modeLabel)}</span>
            <span>${icon("proxy")} Port${ports.length === 1 ? "" : "s"} ${escapeHtml(ports.join(", "))}</span>
            ${tls}
          </div>
          ${issue}
          ${actions}
        </article>`;
    }).join("");
  }

  function renderConflicts() {
    const items = [
      ...state.conflicts.map((item) => ({ ...item, severity: item.blocking ? "error" : "warning" })),
      ...state.warnings.map((message) => ({ message, severity: "warning" })),
    ];
    if (!items.length) {
      byId("conflictList").innerHTML = `<div class="conflict-clear">${icon("check")} No domain or port conflicts detected.</div>`;
      return;
    }
    byId("conflictList").innerHTML = items.map((item) => `
      <div class="conflict-item" data-severity="${item.severity}">
        ${icon("warning")}<span>${escapeHtml(item.message)}</span>
      </div>`).join("");
  }

  async function refreshData() {
    const overview = await callBackend("nginx-overview");
    state.sites = Array.isArray(overview.sites) ? overview.sites : [];
    state.conflicts = Array.isArray(overview.conflicts) ? overview.conflicts : [];
    state.warnings = Array.isArray(overview.warnings) ? overview.warnings : [];
    renderService(overview);
    renderSummary();
    renderSites();
    renderConflicts();
    setConnection("ready", "Connected");
  }

  async function run(operation, successMessage = "", refresh = true) {
    if (state.busy) return;
    setBusy(true);
    byId("globalError").hidden = true;
    byId("globalNotice").hidden = true;
    try {
      const result = await operation();
      if (refresh) await refreshData();
      if (successMessage) showNotice(typeof successMessage === "function" ? successMessage(result) : successMessage);
    } catch (failure) {
      showError(failure);
    } finally {
      setBusy(false);
    }
  }

  function element(name) {
    return form.elements.namedItem(name);
  }

  function updateFormFields() {
    const mode = element("mode").value;
    const tlsEnabled = element("tls").checked;
    document.querySelector("[data-static-field]").hidden = mode !== "static";
    document.querySelector("[data-proxy-field]").hidden = mode !== "proxy";
    document.querySelector("[data-tls-fields]").hidden = !tlsEnabled;
  }

  function clearFormFeedback() {
    byId("formError").hidden = true;
    byId("formConflicts").hidden = true;
    byId("formConflicts").innerHTML = "";
  }

  function showFormConflicts(conflicts) {
    const target = byId("formConflicts");
    target.innerHTML = conflicts.map((item) => `
      <div class="conflict-item" data-severity="${item.blocking ? "error" : "warning"}">
        ${icon("warning")}<span>${escapeHtml(item.message)}</span>
      </div>`).join("");
    target.hidden = conflicts.length === 0;
  }

  function openEditor(site = null) {
    form.reset();
    clearFormFeedback();
    element("site_id").value = site?.site_id || "";
    element("domains").value = site?.domains?.join(", ") || "";
    element("mode").value = site?.mode === "proxy" ? "proxy" : "static";
    element("listen_port").value = String(site?.listen_port || 80);
    element("document_root").value = site?.document_root || "";
    element("upstream_port").value = String(site?.upstream_port || 3000);
    element("tls").checked = Boolean(site?.tls);
    element("certificate_file").value = site?.certificate_file || "";
    element("private_key_file").value = site?.private_key_file || "";
    element("enabled").checked = site ? Boolean(site.enabled) : true;
    byId("dialogTitle").textContent = site ? "Edit website" : "Create a website";
    byId("saveButton").textContent = site ? "Save changes" : "Save website";
    updateFormFields();
    byId("siteDialog").showModal();
  }

  function readFormPayload() {
    const domains = [...new Set(element("domains").value.split(/[,\s]+/).map((value) => value.trim()).filter(Boolean))];
    const mode = element("mode").value;
    const payload = {
      site_id: element("site_id").value || null,
      domains,
      mode,
      listen_port: Number(element("listen_port").value),
      tls: element("tls").checked,
      enabled: element("enabled").checked,
    };
    if (mode === "static") payload.document_root = element("document_root").value.trim();
    if (mode === "proxy") payload.upstream_port = Number(element("upstream_port").value);
    if (payload.tls) {
      payload.certificate_file = element("certificate_file").value.trim();
      payload.private_key_file = element("private_key_file").value.trim();
    }
    return payload;
  }

  async function saveSite(event) {
    event.preventDefault();
    if (state.busy) return;
    clearFormFeedback();
    setBusy(true);
    try {
      const payload = readFormPayload();
      const check = await callBackend("nginx-conflicts", [JSON.stringify(payload)]);
      showFormConflicts(check.conflicts || []);
      if (payload.enabled && check.can_enable === false) {
        throw new Error(check.conflicts?.find((item) => item.blocking)?.message || "Resolve the blocking domain or port conflicts before enabling this website.");
      }
      const result = await callBackend("nginx-site-save", [JSON.stringify(payload)]);
      byId("siteDialog").close();
      await refreshData();
      showNotice(`${result.message || "Website saved."} Reload Nginx when you're ready to apply it.`);
    } catch (failure) {
      showError(failure, byId("formError"));
    } finally {
      setBusy(false);
    }
  }

  async function handleAction(button) {
    const action = button.dataset.action;
    if (action === "create") {
      openEditor();
      return;
    }
    if (action === "close-dialog") {
      byId("siteDialog").close();
      return;
    }
    if (action === "edit") {
      const site = state.sites.find((item) => item.site_id === button.dataset.id);
      if (site?.managed) openEditor(site);
      return;
    }
    if (action === "enable" || action === "disable") {
      const enabling = action === "enable";
      const site = state.sites.find((item) => item.site_id === button.dataset.id);
      const name = site?.name || "this website";
      await run(
        () => callBackend(`nginx-site-${action}`, [button.dataset.id]),
        `${enabling ? "Enabled" : "Disabled"} ${name}'s configuration. Reload Nginx to apply the change.`,
      );
      return;
    }
    if (action === "test") {
      await run(
        () => callBackend("nginx-test"),
        (result) => result.output || "Nginx configuration test passed.",
        false,
      );
      return;
    }
    if (action === "reload" || action === "restart") {
      const verb = action === "reload" ? "Reload" : "Restart";
      if (!window.confirm(`${verb} the Nginx service? The configuration will be tested first.`)) return;
      await run(
        () => callBackend("nginx-service", [action]),
        (result) => result.message || `Nginx ${action} completed successfully.`,
      );
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (button && !button.disabled) void handleAction(button);
  });
  byId("refreshButton").addEventListener("click", () => {
    void run(refreshData, "", false);
  });
  byId("siteSearch").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    renderSites();
  });
  form.addEventListener("submit", saveSite);
  element("mode").addEventListener("change", updateFormFields);
  element("tls").addEventListener("change", updateFormFields);

  refreshData().catch((failure) => {
    showError(failure);
    byId("siteSummary").textContent = "Could not read the Nginx configuration";
    byId("siteList").innerHTML = `<div class="empty-state"><h3>Nginx data is unavailable</h3><p>${escapeHtml(failure.message)}</p></div>`;
  }).finally(() => root.setAttribute("aria-busy", "false"));
})();
