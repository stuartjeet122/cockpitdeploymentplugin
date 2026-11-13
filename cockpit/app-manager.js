// App Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const state = {
    apps: [],
    deployments: [],
    busy: false,
    deploymentMonitor: null,
    filter: "all",
    query: "",
    sort: "recent",
    settings: {},
    logTarget: null,
  };
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);

  const icon = (name) => `<i data-lucide="${name}" aria-hidden="true"></i>`;
  const renderIcons = () => globalThis.renderAppManagerIcons?.();
  const frameworkName = (value) => ({ auto: "Auto detect", nextjs: "Next.js", node: "Node.js", go: "Go", python: "Python", static: "Static HTML", docker: "Docker" })[value] || value;
  function formatDate(value) {
    const date = new Date(value);
    return value && !Number.isNaN(date.valueOf())
      ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date)
      : "Not deployed";
  }

  async function executeBackend(command, args, input) {
    const cockpitApi = globalThis.cockpit;
    if (!cockpitApi || typeof cockpitApi.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Ensure ../base1/cockpit.js loads before app-manager.js.");
    }
    const process = cockpitApi.spawn([BACKEND, command, ...args], { superuser: "require" });
    let output = "";
    if (typeof process.stream === "function") process.stream((chunk) => { output += chunk; });
    if (input !== undefined) process.input(input);
    try {
      const completed = await process;
      if (!output) output = completed;
    } catch (error) {
      let failure;
      try { failure = JSON.parse(output); } catch (_parseError) { failure = null; }
      throw new Error(failure?.error || error.message || "The backend command failed.");
    }
    let result;
    try {
      result = JSON.parse(output);
    } catch (error) {
      throw new Error(`Backend returned invalid JSON: ${error.message}`);
    }
    if (!result || typeof result !== "object" || Array.isArray(result) || result.ok === false) {
      throw new Error(result?.error || "The backend returned an invalid response.");
    }
    return result;
  }

  const callBackend = (command, args = []) => executeBackend(command, args);
  const callBackendInput = (command, args, input) => executeBackend(command, args, input);

  async function waitForCockpitApi(timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (globalThis.cockpit && typeof globalThis.cockpit.spawn === "function") {
        return globalThis.cockpit;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 50));
    }
    throw new Error("Cockpit API failed to load from ../base1/cockpit.js. Reload App Manager inside Cockpit and check for blocked or missing scripts.");
  }

  function showNotice(message) {
    const notice = byId("appManagerNotice");
    notice.textContent = message;
    notice.classList.remove("hidden");
    byId("appManagerError").classList.add("hidden");
    const connection = byId("appManagerConnection");
    if (connection.dataset.state === "error") updateConnection("connected", "Ready");
  }

  function showError(error) {
    const message = error?.message || String(error);
    const dialog = byId("appManagerDialog");
    if (dialog.open) {
      let dialogError = dialog.querySelector(".am-dialog-error");
      if (!dialogError) {
        dialogError = document.createElement("div");
        dialogError.className = "am-error am-dialog-error";
        dialogError.setAttribute("role", "alert");
        dialogError.setAttribute("aria-live", "assertive");
        dialogError.tabIndex = -1;
        dialog.querySelector(".am-dialog-body").prepend(dialogError);
      }
      dialogError.textContent = message;
      dialogError.focus();
    } else {
      byId("appManagerError").textContent = message;
      byId("appManagerError").classList.remove("hidden");
    }
    byId("appManagerNotice").classList.add("hidden");
    updateConnection("error", "Action needs attention");
  }

  function updateConnection(status, message) {
    const connection = byId("appManagerConnection");
    connection.dataset.state = status;
    connection.lastElementChild.textContent = message;
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll(".am-app-manager button, .am-app-manager input, .am-app-manager select, .am-app-manager textarea, .am-dialog button, .am-dialog input, .am-dialog select, .am-dialog textarea")
      .forEach((control) => {
        control.disabled = busy || control.hasAttribute("data-unavailable");
      });
    const refreshButton = byId("refreshButton");
    refreshButton.querySelector("span").textContent = busy ? "Working…" : "Refresh";
    refreshButton.setAttribute("aria-label", busy ? "App Manager is working" : "Refresh application data");
    const updateButton = byId("checkUpdateButton");
    updateButton.querySelector("span").textContent = busy ? "Please wait…" : "Check for updates";
    document.querySelector(".am-app-manager").setAttribute("aria-busy", String(busy));
    if (busy) updateConnection("busy", "Working…");
    else if (byId("appManagerConnection").dataset.state === "busy") updateConnection("connected", "Ready");
  }

  async function runOperation(operation, successMessage, refresh = true) {
    if (state.busy) {
      showNotice("Another operation is still in progress. Please wait a moment.");
      return false;
    }
    setBusy(true);
    try {
      await operation();
      if (refresh) await refreshData();
      if (successMessage) showNotice(successMessage);
      return true;
    } catch (error) {
      showError(error);
      return false;
    } finally {
      setBusy(false);
    }
  }

  function renderStatus(status) {
    const value = status || "unknown";
    const label = { running: "Live", stopped: "Stopped", deploying: "Deploying", failed: "Failed", deployed: "Deployed", aborted: "Aborted" }[value] || value;
    return `<span class="am-status-pill" data-status="${escapeHtml(value)}">${escapeHtml(label)}</span>`;
  }

  function renderOverview(data) {
    const cards = [
      ["Applications", data.total_apps, "boxes"],
      ["Live", data.running_apps, "circle-check"],
      ["Stopped", data.stopped_apps, "pause"],
      ["Needs attention", state.apps.filter(needsAttention).length, "circle-alert"],
    ];
    byId("overviewCards").innerHTML = cards.map(([label, value, symbol]) => `
      <div class="am-metric">
        <span class="am-metric-label">${icon(symbol)}${escapeHtml(label)}</span>
        <strong class="am-metric-value">${escapeHtml(value)}</strong>
      </div>
    `).join("");

    const activity = (data.recent_activity || []).map((item) => `
      <div class="am-activity-item">
        <span class="am-activity-icon" aria-hidden="true">${icon("git-branch")}</span>
        <div>
          <strong class="am-activity-title">${escapeHtml(item.name || "Application")}</strong>
          <span class="am-activity-description">${escapeHtml(item.action || "Updated")}</span>
        </div>
        <time class="am-activity-time">${escapeHtml(formatDate(item.when))}</time>
      </div>
    `).join("");
    byId("recentActivity").innerHTML = activity || `
      <div class="am-empty-state">
        <span class="am-empty-state__icon" aria-hidden="true">${icon("activity")}</span>
        <h3>No activity yet</h3>
      </div>
    `;
  }

  function getSafePublicUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_error) {
      return null;
    }
  }

  function renderApps(data) {
    state.apps = data.apps || [];
    renderAppList();
  }

  function observedStatus(app) {
    const health = app.health;
    if (app.status === "deploying" || !health || health.service_state === "not-required" || Date.now() - Date.parse(health.checked_at) > 600000 || !Number.isFinite(Date.parse(health.checked_at)) || Date.parse(health.checked_at) < Date.parse(app.updated_at)) return app.status;
    return health.service_state === "active" ? "running" : health.service_state === "failed" ? "failed" : health.service_state === "inactive" ? "stopped" : app.status;
  }

  function needsAttention(app) {
    return observedStatus(app) === "failed" || app.deployments?.at(-1)?.status === "failed"
      || app.health?.local_healthy === false || app.health?.public_healthy === false
      || Boolean(app.auto_deploy_state?.error || app.deployments?.at(-1)?.cleanup_error
        || app.pending_update?.commit && app.pending_update.commit !== app.deployed_commit);
  }

  function renderAppList() {
    byId("appCount").textContent = String(state.apps.length);
    const filtered = state.apps.filter((app) => {
      const matchesStatus = state.filter === "all" || (state.filter === "failed"
        ? needsAttention(app)
        : observedStatus(app) === state.filter);
      return matchesStatus && [app.name, app.repo_url, app.route, frameworkName(app.framework)]
        .join(" ").toLowerCase().includes(state.query.toLowerCase());
    }).sort((first, second) => state.sort === "name"
      ? first.name.localeCompare(second.name)
      : String(second.updated_at || "").localeCompare(String(first.updated_at || "")));
    byId("appsResultCount").textContent = `${filtered.length} of ${state.apps.length} ${state.apps.length === 1 ? "application" : "applications"}`;
    if (!state.apps.length) {
      byId("appsTableContainer").innerHTML = `
        <div class="am-empty-state">
          <span class="am-empty-state__icon" aria-hidden="true">${icon("boxes")}</span>
          <h3>Your next application starts here</h3>
          <p>No repositories connected.</p>
          <button type="button" class="am-button am-button--primary" data-open-add>${icon("plus")} New application</button>
        </div>
      `;
      renderIcons();
      return;
    }
    if (!filtered.length) {
      byId("appsTableContainer").innerHTML = `<div class="am-empty-state">${icon("search")}<h3>No matching applications</h3><button type="button" class="am-button" data-clear-filters>Clear filters</button></div>`;
      renderIcons();
      return;
    }

    const cards = filtered.map((app) => {
      const safeUrl = getSafePublicUrl(app.public_url);
      const routeIsActive = Boolean(app.hosting_active && app.active_route);
      const deploying = app.status === "deploying";
      const unavailable = deploying ? " disabled data-unavailable" : "";
      const runtimeUnavailable = deploying || !app.last_deploy ? " disabled data-unavailable" : "";
      const appId = escapeHtml(app.id);
      const serviceControl = observedStatus(app) === "running"
        ? `<button type="button" data-action="stop" data-id="${appId}"${runtimeUnavailable}>${icon("pause")} Stop application</button>`
        : `<button type="button" data-action="start" data-id="${appId}"${runtimeUnavailable}>${icon("play")} Start application</button>`;
      const routeStatus = routeIsActive && app.active_route === app.route
        ? "Active"
        : routeIsActive
          ? "Route change pending"
          : "Deploy to activate";
      const appInitial = String(app.name || "A").trim().charAt(0).toUpperCase() || "A";
      const deployControl = app.status === "deploying"
        ? `<button type="button" class="am-button am-button--primary" data-action="deployment-log" data-id="${appId}">${icon("loader-circle")} View deployment</button>`
        : app.deployment_mode === "approval"
          ? `<button type="button" class="am-button am-button--primary" data-action="review-update" data-id="${appId}">${icon("git-branch")} Review update</button>`
          : `<button type="button" class="am-button am-button--primary" data-action="deploy" data-id="${appId}">${icon("arrow-up-right")} ${app.last_deploy ? "Redeploy" : "Deploy"}</button>`;
      const lastFailure = app.deployments?.[0]?.status === "failed";
      return `
        <article class="am-app-card" data-status="${escapeHtml(observedStatus(app))}" data-framework="${escapeHtml(app.framework || "auto")}">
          <header class="am-app-card__heading">
            <span class="am-app-avatar" aria-hidden="true">${escapeHtml(appInitial)}</span>
            <div class="am-app-title-wrap">
              <h3>${escapeHtml(app.name)}</h3>
              <span class="am-app-framework">${escapeHtml(frameworkName(app.framework || "auto"))}</span>
            </div>
            ${renderStatus(observedStatus(app))}
          </header>
          <div class="am-app-route">${icon("globe")}${safeUrl && routeIsActive
            ? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(app.active_route)}${icon("external-link")}</a>`
            : `<code>${escapeHtml(app.route)}</code>`}<span class="am-route-state" data-active="${routeIsActive}">${escapeHtml(routeStatus)}</span></div>
          <p class="am-repository" title="${escapeHtml(app.repo_url)}">${icon("git-branch")}<span>${escapeHtml(app.repo_url)}</span></p>
          <dl class="am-app-meta">
            <div><dt>Branch</dt><dd>${escapeHtml(app.branch || "main")}</dd></div>
            <div><dt>${app.framework === "static" && !app.build_config?.start_command ? "Hosting" : "Runtime"}</dt><dd>${app.framework === "static" && !app.build_config?.start_command ? "Nginx" : `127.0.0.1:${escapeHtml(app.port)}`}</dd></div>
            <div class="am-meta-wide"><dt>Last deployed</dt><dd>${escapeHtml(formatDate(app.last_deploy))}${app.deployed_commit ? ` <code>${escapeHtml(app.deployed_commit.slice(0, 7))}</code>` : ""}</dd></div>
            <div><dt>Readiness</dt><dd data-health="${app.health ? (app.health.local_healthy ? "healthy" : "failed") : "unknown"}" title="${escapeHtml(app.health?.local_error || "")}">${app.health ? (app.health.local_healthy ? "Healthy" : "Failed") : "Not checked"}</dd></div>
            <div><dt>Public route</dt><dd data-health="${app.health?.public_healthy == null ? "unknown" : app.health.public_healthy ? "healthy" : "failed"}" title="${escapeHtml(app.health?.public_error || "")}">${app.health?.public_healthy == null ? "Not checked" : app.health.public_healthy ? "Reachable" : app.health.public_status === 403 ? "Blocked · 403" : "Failed"}</dd></div>
          </dl>
          <details class="am-runtime-details">
            <summary>${icon("activity")} Runtime details ${app.auto_deploy === false ? '<span class="am-runtime-policy">Auto-deploy paused</span>' : ""}${icon("plus")}</summary>
            <dl class="am-app-meta">
            <div><dt>Service</dt><dd>${escapeHtml(app.health?.service_state || "Not checked")}</dd></div>
            <div><dt>Checked</dt><dd>${escapeHtml(app.health?.checked_at ? formatDate(app.health.checked_at) : "Never")}</dd></div>
            <div><dt>Auto-deploy</dt><dd>${app.auto_deploy === false ? "Paused" : "Enabled"}</dd></div>
            <div><dt>Last repository check</dt><dd>${escapeHtml(app.auto_deploy_state?.checked_at ? formatDate(app.auto_deploy_state.checked_at) : "Never")}</dd></div>
            <div><dt>CPU</dt><dd>${app.metrics?.cpu_percent == null ? "Not sampled" : `${escapeHtml(app.metrics.cpu_percent)}%`}</dd></div>
            <div><dt>Memory</dt><dd>${formatBytes(app.metrics?.memory_bytes)}</dd></div>
            <div><dt>Metrics sampled</dt><dd>${app.metrics?.checked_at ? escapeHtml(formatDate(app.metrics.checked_at)) : "Never"}</dd></div>
            <div><dt>Build phase</dt><dd>${escapeHtml(deploying ? app.deployments?.[0]?.build_status || "pending" : "Idle")}</dd></div>
            <div><dt>Deployment policy</dt><dd>${app.deployment_mode === "approval" ? "Approval required" : "Automatic"}</dd></div>
            </dl>
          </details>
          ${app.deployment_mode === "approval" && app.pending_update && app.pending_update.commit !== app.deployed_commit ? `<p class="am-route-state">Awaiting approval: <code>${escapeHtml(app.pending_update.commit.slice(0, 7))}</code></p>` : ""}
          ${app.health && (app.health.local_healthy === false || app.health.public_healthy === false || app.health.service_error) ? `<p class="am-deploy-error">${icon("circle-alert")}<span>${escapeHtml(app.health.local_error || app.health.public_error || app.health.service_error)}</span></p>` : ""}
          ${app.auto_deploy_state?.error ? `<p class="am-deploy-error">${icon("circle-alert")}<span>Automatic deployment: ${escapeHtml(app.auto_deploy_state.error)}</span></p>` : ""}
          ${app.deployments?.[0]?.cleanup_error ? `<p class="am-deploy-error">${icon("circle-alert")}<span>Deployment is live; cleanup needs attention: ${escapeHtml(app.deployments[0].cleanup_error)}</span></p>` : ""}
          ${lastFailure ? `<p class="am-deploy-error">${icon("circle-alert")}<span>${app.status === "running" ? "Latest deployment failed. Previous release is still live." : "Latest deployment failed."} <button type="button" data-action="deployment-log" data-id="${appId}">View log</button></span></p>` : ""}
          <div class="am-app-actions" aria-label="Actions for ${escapeHtml(app.name)}">
            ${deployControl}
            <span class="am-action-spacer"></span>
            <button type="button" class="am-button am-icon-button" data-action="environment" data-id="${appId}" title="Environment variables" aria-label="Environment variables for ${escapeHtml(app.name)}"${unavailable}>${icon("key-round")}</button>
            <button type="button" class="am-button am-icon-button" data-action="logs" data-id="${appId}" title="Application logs" aria-label="Logs for ${escapeHtml(app.name)}">${icon("square-terminal")}</button>
            <details class="am-app-menu"><summary class="am-button am-icon-button" title="More actions" aria-label="More actions for ${escapeHtml(app.name)}">${icon("ellipsis")}</summary>
              <div class="am-menu-panel">
                ${serviceControl}
                <button type="button" data-action="restart" data-id="${appId}"${runtimeUnavailable}>${icon("refresh-cw")} Restart</button>
                <button type="button" data-action="health" data-id="${appId}"${runtimeUnavailable}>${icon("activity")} Check health</button>
                <button type="button" data-action="preflight" data-id="${appId}"${unavailable}>${icon("search")} Run preflight</button>
                ${app.python_services?.worker ? `<button type="button" data-action="worker-status" data-id="${appId}">${icon("activity")} Worker status</button>
                <button type="button" data-action="worker-logs" data-id="${appId}">${icon("square-terminal")} Worker logs</button>
                <button type="button" data-action="worker-restart" data-id="${appId}"${runtimeUnavailable}>${icon("refresh-cw")} Restart worker</button>` : ""}
                <button type="button" data-action="metrics" data-id="${appId}">${icon("activity")} Metrics and storage</button>
                <button type="button" data-action="edit" data-id="${appId}"${unavailable}>${icon("settings-2")} Edit configuration</button>
                <button type="button" data-action="pull" data-id="${appId}"${unavailable}>${icon("download")} Pull repository</button>
                <button type="button" class="am-menu-danger" data-action="remove" data-id="${appId}"${unavailable}>${icon("trash-2")} Remove application</button>
              </div>
            </details>
          </div>
        </article>
      `;
    }).join("");

    byId("appsTableContainer").innerHTML = cards;
    renderIcons();
  }

  function renderDeployments(data) {
    state.deployments = data.deployments || [];
    if (!state.deployments.length) {
      byId("deploymentHistory").innerHTML = `
        <div class="am-empty-state">
          <span class="am-empty-state__icon" aria-hidden="true">↗</span>
          <h3>No deployments yet</h3>
          <p>Once you deploy an application, its release history will appear here.</p>
        </div>
      `;
      return;
    }
    const rows = state.deployments.map((deployment) => {
      const viewControl = deployment.id
        ? `<button type="button" class="am-button" data-action="deployment-log" data-id="${escapeHtml(deployment.app_id)}" data-deployment-id="${escapeHtml(deployment.id)}">View log</button>`
        : "";
      return `
        <tr>
          <td>${escapeHtml(deployment.name)}</td>
          <td>${escapeHtml(deployment.date)}</td>
          <td>${escapeHtml(deployment.commit || "n/a")}</td>
          <td>${escapeHtml(deployment.build_status || "pending")}</td>
          <td>${escapeHtml(deployment.status || "pending")}</td>
          <td>${escapeHtml(deployment.error || "—")}</td>
          <td>${viewControl}</td>
        </tr>
      `;
    }).join("");
    byId("deploymentHistory").innerHTML = `
      <div class="am-deployment-table-wrap">
      <table class="table">
        <thead><tr>
          <th>Name</th><th>Date</th><th>Commit</th><th>Build status</th>
          <th>Deployment status</th><th>Error</th><th>Log</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      </div>
    `;
  }

  function renderSettings(data) {
    const config = data.config || {};
    state.settings = config;
    const existing = byId("settingsForm");
    if (existing && Array.from(existing.elements).some((input) => input.name && input.value !== input.defaultValue)) return;
    byId("settingsPanel").innerHTML = `
      <section class="am-surface am-settings">
        <div class="am-surface-heading">
          <div>
            <h3>Deployment settings</h3>
            <p>All managed applications use these safe, isolated host locations.</p>
          </div>
        </div>
        <dl class="am-settings-paths">
          <div><dt>Port range</dt><dd>${escapeHtml(config.port_range ? `${config.port_range.start}-${config.port_range.end}` : "8100-8999")}</dd></div>
          <div><dt>Source root</dt><dd>${escapeHtml(config.source_root || "/var/lib/cockpit/app-manager/sources")}</dd></div>
          <div><dt>Production root</dt><dd>${escapeHtml(config.production_root || "/opt/webapps")}</dd></div>
          <div><dt>Log root</dt><dd>${escapeHtml(config.log_root || "/var/log/cockpit-app-manager")}</dd></div>
        </dl>
        <form id="settingsForm" class="am-settings-form">
          <div class="am-form-grid">
            <div>
              <label for="settingBuildTimeout">Build timeout (seconds)</label>
              <input id="settingBuildTimeout" type="number" min="1" max="86400" step="1" name="build_timeout_seconds" value="${escapeHtml(config.build_timeout_seconds ?? 900)}" required>
            </div>
            <div>
              <label for="settingHealthTimeout">Health timeout (seconds)</label>
              <input id="settingHealthTimeout" type="number" min="1" max="3600" step="1" name="health_timeout_seconds" value="${escapeHtml(config.health_timeout_seconds ?? 30)}" required>
            </div>
            <div>
              <label for="settingCommandTimeout">Command timeout (seconds)</label>
              <input id="settingCommandTimeout" type="number" min="1" max="86400" step="1" name="command_timeout_seconds" value="${escapeHtml(config.command_timeout_seconds ?? 30)}" required>
            </div>
            <label>Concurrent builds<input type="number" name="max_concurrent_builds" min="1" max="8" value="${escapeHtml(config.max_concurrent_builds ?? 1)}" required></label>
            <label>Minimum free disk (MiB)<input type="number" name="min_free_disk_mb" min="1" max="1048576" value="${escapeHtml(config.min_free_disk_mb ?? 512)}" required></label>
          </div>
          <div class="am-form-actions"><button type="submit" class="am-button am-button--primary">Save settings</button></div>
        </form>
      </section>
    `;
    byId("settingsForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      for (const key of Object.keys(payload)) payload[key] = Number(payload[key]);
      void runOperation(
        async () => {
          await callBackend("settings", [JSON.stringify(payload)]);
          Array.from(form.elements).forEach((input) => { if (input.name) input.defaultValue = input.value; });
        },
        "Settings saved.",
      );
    });
  }

  async function refreshData() {
    try {
      const [overview, apps, deployments, settings] = await Promise.all([
        callBackend("overview"),
        callBackend("list"),
        callBackend("deployments"),
        callBackend("settings"),
      ]);
      renderApps(apps);
      renderOverview(overview);
      renderDeployments(deployments);
      renderSettings(settings);
      byId("lastRefreshed").textContent = `Updated ${new Intl.DateTimeFormat(undefined, { timeStyle: "short" }).format(new Date())}`;
      renderIcons();
      updateConnection("connected", `${state.apps.length} ${state.apps.length === 1 ? "application" : "applications"}`);
      resumeActiveDeployment();
    } catch (error) {
      updateConnection("error", "Could not load data");
      throw error;
    }
  }

  function openDialog(title, body, submitLabel, onSubmit, onSuccess) {
    const dialog = byId("appManagerDialog");
    dialog.classList.remove("am-dialog--deployment");
    state.logTarget = null;
    byId("appManagerError").classList.add("hidden");
    const submit = submitLabel
      ? `<button type="submit" class="am-button pf-m-primary">${escapeHtml(submitLabel)}</button>`
      : "";
    dialog.innerHTML = `
      <form class="am-dialog-form">
        <header class="am-dialog-header">
          <h2 id="appManagerDialogTitle">${escapeHtml(title)}</h2>
          <button type="button" class="am-dialog-close am-icon-button" aria-label="Close" title="Close dialog" data-dialog-close>${icon("x")}</button>
        </header>
        <div class="am-dialog-body">${body}</div>
        <footer class="am-dialog-footer">
          <button type="button" class="am-button am-button--quiet" data-dialog-close>${submitLabel ? "Cancel" : "Close"}</button>${submit}
        </footer>
      </form>
    `;
    const form = dialog.querySelector("form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!onSubmit || state.busy) return;
      const submitButton = form.querySelector('button[type="submit"]');
      submitButton.disabled = true;
      try {
        const submitted = await onSubmit(form);
        if (submitted === false) {
          submitButton.disabled = false;
          return;
        }
        dialog.close();
        if (typeof onSuccess === "function") onSuccess();
      } catch (error) {
        showError(error);
        submitButton.disabled = false;
      }
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => { if (!state.busy) dialog.close(); });
    });
    if (!dialog.open) dialog.showModal();
    renderIcons();
    const firstInput = dialog.querySelector("input:not([type=hidden]), select, textarea");
    if (firstInput) firstInput.focus();
  }

  function parseBuildConfig(value) {
    if (!value.trim()) return {};
    let parsed;
    try {
      parsed = JSON.parse(value);
    } catch (error) {
      throw new Error(`Build configuration must be valid JSON: ${error.message}`);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Build configuration must be a JSON object.");
    }
    return parsed;
  }

  function suggestSlug(value) {
    return value.trim().toLowerCase()
      .replace(/[^a-z0-9-]+/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 24)
      .replace(/-$/g, "") || "app";
  }

  function openAddApplicationDialog() {
    const body = byId("addAppFormTemplate").innerHTML;
    let step = 0;
    let created = null;
    let deployAfterSetup = true;
    const collect = (form) => {
      const values = Object.fromEntries(new FormData(form).entries());
      const { app_directory, go_package, start_command, docker_mode, dockerfile, compose_file, compose_service, container_port, environment, deploy_now, ...payload } = values;
      payload.build_config = {};
      if (app_directory.trim()) payload.build_config.app_directory = app_directory.trim();
      if (go_package.trim()) payload.build_config.go_package = go_package.trim();
      if (payload.framework === "docker") {
        payload.build_config.docker_mode = docker_mode;
        if (docker_mode === "compose") {
          payload.build_config.compose_file = compose_file.trim() || "compose.yaml";
          payload.build_config.compose_service = compose_service.trim() || "web";
        } else {
          payload.build_config.dockerfile = dockerfile.trim() || "Dockerfile";
        }
        payload.build_config.container_port = Number(container_port);
      }
      if (start_command.trim()) {
        let command;
        try { command = JSON.parse(start_command); } catch (_error) { throw new Error('Start command must be a JSON array, such as ["node", "server.js"].'); }
        if (!Array.isArray(command) || !command.length || !command.every((part) => typeof part === "string" && part && !/[\x00-\x1f\x7f]/.test(part))) {
          throw new Error("Start command must contain non-empty executable arguments, not a shell command.");
        }
        payload.build_config.start_command = command;
      }
      const environmentRows = parseEnvironmentContent(environment);
      const reserved = new Set(["HOME", "PATH", "PORT", "HOST", "NODE_ENV", "APP_BASE_PATH", "APP_CONFIG_DIR", "APP_DATA_DIR", "APP_MODELS_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "NUMBA_CACHE_DIR", "QT_QPA_PLATFORM"]);
      const blocked = environmentRows.find((row) => reserved.has(row.key) || row.key.startsWith("APP_STORAGE_"));
      if (blocked) throw new Error(`${blocked.key} is managed automatically and cannot be overridden.`);
      const content = serializeEnvironmentRows(environmentRows);
      if (new TextEncoder().encode(content).length > 64 * 1024) throw new Error("Environment variables exceed 64 KiB.");
      return { payload, environmentRows, content, deploy: deploy_now === "on" };
    };
    openDialog("New application", body, "Create and deploy", async (form) => {
      if (step !== 2) { nextButton.click(); return false; }
      const setup = collect(form);
      deployAfterSetup = setup.deploy;
      const successful = await runOperation(async () => {
        if (!created) {
          created = (await callBackend("add", [JSON.stringify(setup.payload)])).app;
          if (!created?.id) throw new Error("The backend did not return the registered application.");
          form.querySelectorAll("input, select, textarea").forEach((input) => {
            if (!["environment", "deploy_now"].includes(input.name)) input.setAttribute("readonly", "");
          });
        }
        if (setup.content) await callBackendInput("env-save", [created.id], setup.content);
      }, deployAfterSetup ? "" : "Application created.");
      if (!successful && created) submitButton.textContent = "Retry setup";
      return successful;
    }, () => {
      byId("tab-websites").click();
      if (deployAfterSetup) void runOperation(() => deployWithLiveLog(created.id), "", false);
    });

    const form = byId("appManagerDialog").querySelector("form");
    form.noValidate = true;
    bindEnvironmentImport(byId("appEnvironmentImport"), (content) => {
      form.elements.namedItem("environment").value = content;
    });
    const footer = form.querySelector(".am-dialog-footer");
    footer.insertAdjacentHTML("afterbegin", `<button type="button" id="setupBack" class="am-button am-button--quiet" hidden>${icon("arrow-left")} Back</button>`);
    footer.insertAdjacentHTML("beforeend", `<button type="button" id="setupNext" class="am-button pf-m-primary">Continue ${icon("arrow-right")}</button>`);
    const backButton = byId("setupBack");
    const nextButton = byId("setupNext");
    const submitButton = form.querySelector('[type="submit"]');
    submitButton.hidden = true;
    const nameInput = form.elements.namedItem("name");
    const slugInput = form.elements.namedItem("slug");
    const routeInput = form.elements.namedItem("route");
    const frameworkInput = form.elements.namedItem("framework");
    const dockerOptions = form.querySelector("[data-docker-options]");
    const containerPortInput = form.elements.namedItem("container_port");
    const dockerModeInput = form.elements.namedItem("docker_mode");
    const dockerfileOptions = form.querySelector("[data-dockerfile-options]");
    const composeOptions = form.querySelector("[data-compose-options]");
    const composeFileInput = form.elements.namedItem("compose_file");
    const composeServiceInput = form.elements.namedItem("compose_service");
    const updateDockerOptions = () => {
      const dockerSelected = frameworkInput.value === "docker";
      dockerOptions.hidden = !dockerSelected;
      containerPortInput.required = dockerSelected;
      const composeSelected = dockerSelected && dockerModeInput.value === "compose";
      dockerfileOptions.hidden = !dockerSelected || composeSelected;
      composeOptions.hidden = !composeSelected;
      composeFileInput.required = composeSelected;
      composeServiceInput.required = composeSelected;
    };
    frameworkInput.addEventListener("change", updateDockerOptions);
    dockerModeInput.addEventListener("change", updateDockerOptions);
    updateDockerOptions();
    form.elements.namedItem("branch").value = state.settings.default_branch || "main";
    const updatePreview = () => { byId("setupUrlPreview").textContent = `https://stuserver.online${routeInput.value || "/"}`; };
    const showStep = () => {
      form.querySelectorAll("[data-setup-step]").forEach((section) => { section.hidden = Number(section.dataset.setupStep) !== step; });
      form.querySelectorAll("[data-setup-marker]").forEach((marker) => {
        if (Number(marker.dataset.setupMarker) === step) marker.setAttribute("aria-current", "step");
        else marker.removeAttribute("aria-current");
      });
      backButton.hidden = step === 0 || (created && step === 1);
      nextButton.hidden = step === 2;
      submitButton.hidden = step !== 2;
      form.querySelector(`[data-setup-step="${step}"] input`)?.focus();
      form.querySelector(".am-dialog-body").scrollTop = 0;
      renderIcons();
    };
    nextButton.addEventListener("click", () => {
      try {
        const current = form.querySelector(`[data-setup-step="${step}"]`);
        const invalid = Array.from(current.querySelectorAll("input, select, textarea")).find((input) => !input.checkValidity());
        if (invalid) { invalid.reportValidity(); return; }
        const repo = form.elements.namedItem("repo_url").value;
        if (!/^(https:\/\/[^\s@]+\/|ssh:\/\/[^\s]+\/|git@[^\s:]+:)[^\s]+$/.test(repo)) {
          throw new Error("Enter an HTTPS or SSH Git repository URL without embedded credentials.");
        }
        if (step === 1) {
          const setup = collect(form);
          if (state.apps.some((app) => app.id !== created?.id && app.id === setup.payload.slug)) throw new Error("That application ID is already registered.");
          if (state.apps.some((app) => app.id !== created?.id && [app.route, app.active_route].includes(setup.payload.route))) throw new Error("That hosted route is already assigned.");
          const review = [
            ["Application", setup.payload.name], ["Repository", setup.payload.repo_url], ["Branch", setup.payload.branch],
            ["Framework", frameworkName(setup.payload.framework)], ["Public URL", `https://stuserver.online${setup.payload.route}`],
            ["Environment", setup.environmentRows.length ? setup.environmentRows.map((row) => row.key).join(", ") : "No variables"],
          ];
          byId("setupReview").innerHTML = review.map(([label, value]) => `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
        }
        form.querySelector(".am-dialog-error")?.remove();
        step += 1;
        showStep();
      } catch (error) { showError(error); }
    });
    backButton.addEventListener("click", () => { if (step > 0) { step -= 1; showStep(); } });
    form.elements.namedItem("deploy_now").addEventListener("change", (event) => {
      submitButton.textContent = event.target.checked ? "Create and deploy" : "Create application";
    });
    slugInput.dataset.autogenerated = "true";
    routeInput.dataset.autogenerated = "true";
    nameInput.addEventListener("input", () => {
      if (slugInput.dataset.autogenerated === "true") {
        slugInput.value = suggestSlug(nameInput.value);
      }
      if (routeInput.dataset.autogenerated === "true") {
        routeInput.value = `/${slugInput.value}/`;
      }
      updatePreview();
    });
    slugInput.addEventListener("input", () => {
      slugInput.dataset.autogenerated = "false";
      if (routeInput.dataset.autogenerated === "true") {
        routeInput.value = `/${slugInput.value}/`;
      }
      updatePreview();
    });
    routeInput.addEventListener("input", () => {
      routeInput.dataset.autogenerated = "false";
      updatePreview();
    });
    form.elements.namedItem("repo_url").addEventListener("change", (event) => {
      if (!nameInput.value) {
        nameInput.value = event.target.value.split(/[/:]/).pop().replace(/\.git$/, "").replace(/[-_]/g, " ");
        nameInput.dispatchEvent(new Event("input"));
      }
    });
    updatePreview();
    renderIcons();
  }

  function editApplication(app) {
    const frameworks = ["auto", "nextjs", "go", "node", "python", "static", "docker"];
    const options = frameworks.map((framework) => `
      <option value="${framework}"${app.framework === framework ? " selected" : ""}>${framework === "auto" ? "Auto detect" : escapeHtml(framework)}</option>
    `).join("");
    const body = `
      <div class="am-form-grid">
        <label>Name<input name="name" value="${escapeHtml(app.name)}" required></label>
        <label>Git repository URL<input name="repo_url" value="${escapeHtml(app.repo_url)}" required></label>
        <label>Branch<input name="branch" value="${escapeHtml(app.branch)}" required></label>
        <label>Hosted route<input name="route" value="${escapeHtml(app.route || `/${app.id}/`)}" required autocomplete="off"></label>
        <label>Description<textarea name="description" rows="3">${escapeHtml(app.description || "")}</textarea></label>
        <label>Deployment policy<select name="deployment_mode"><option value="automatic"${app.deployment_mode !== "approval" ? " selected" : ""}>Automatic</option><option value="approval"${app.deployment_mode === "approval" ? " selected" : ""}>Approval required</option></select></label>
        <label class="am-checkbox"><input type="checkbox" name="auto_deploy"${app.auto_deploy !== false ? " checked" : ""}> Automatic deployment</label>
      </div>
      <small class="am-field-hint">A route change is applied by the next deployment. Next.js projects must build with the same basePath (APP_BASE_PATH is supplied during build).</small>
      <details class="am-advanced"><summary>Build and runtime</summary><div class="am-form-grid">
        <label>Framework<select name="framework">${options}</select></label>
        <label>Port<input type="number" name="port" min="8100" max="8999" value="${escapeHtml(app.port)}" required></label>
        <label>Health-check URL<input name="health_check_url" value="${escapeHtml(app.health_check_url || "/")}" required></label>
        <label>Required environment names<input name="required_environment" value="${escapeHtml((app.required_environment || []).join(", "))}"></label>
        <label>Persistent directory names<input name="storage_directories" value="${escapeHtml((app.storage_directories || ["data"]).join(", "))}"></label>
        <label>Build configuration (JSON object)<textarea name="build_config" rows="5">${escapeHtml(JSON.stringify(app.build_config || {}, null, 2))}</textarea></label>
        <label>Minimum runtime versions (JSON object)<textarea name="runtime_requirements" rows="3">${escapeHtml(JSON.stringify(app.runtime_requirements || {}, null, 2))}</textarea></label>
      </div></details>
      <details class="am-advanced"><summary>Native Python services</summary><div class="am-form-grid">
        <label class="am-checkbox"><input type="checkbox" name="native_python"${Object.keys(app.python_services || {}).length ? " checked" : ""}> Enable native Python services</label>
        <label class="am-checkbox"><input type="checkbox" name="native_redis"${app.python_services?.redis ? " checked" : ""}> Administrator-provisioned private Redis</label>
        <label class="am-checkbox"><input type="checkbox" name="native_drum"${app.python_services?.drum_prerequisites ? " checked" : ""}> Drum audio prerequisites</label>
        <label>Worker command (JSON argument array)<textarea name="worker_command" rows="4">${escapeHtml(JSON.stringify(app.python_services?.worker?.command || []))}</textarea></label>
        <label>Worker shutdown timeout (seconds)<input name="worker_timeout" type="number" min="10" max="3600" value="${escapeHtml(app.python_services?.worker?.stop_timeout_seconds ?? 300)}"></label>
        ${Object.entries({ memory_mb: ["Worker memory (MiB)", 1024], cpu_percent: ["Worker CPU (%)", 100], tasks_max: ["Worker process limit", 256] }).map(([key, [label, fallback]]) => `<label>${label}<input type="number" name="worker_${key}" min="1" max="${key === "memory_mb" ? 1048576 : 10000}" value="${escapeHtml(app.python_services?.worker?.resource_limits?.[key] ?? fallback)}"></label>`).join("")}
        <label>Model artifact paths (relative to shared/models)<textarea name="model_files" rows="3">${escapeHtml((app.python_services?.model_files || []).join("\n"))}</textarea></label>
        <label>Upload body limit (MiB)<input name="upload_limit_mb" type="number" min="1" max="1024" value="${escapeHtml(app.python_services?.upload_limit_mb ?? 101)}"></label>
        <label>Route access<select name="native_access"><option value="blocked"${app.python_services?.access_control !== "external-auth" ? " selected" : ""}>Blocked at Nginx</option><option value="external-auth"${app.python_services?.access_control === "external-auth" ? " selected" : ""}>Administrator-confirmed upstream authentication</option></select></label>
      </div></details>
      <details class="am-advanced"><summary>Deployment schedule (UTC)</summary><div class="am-form-grid">
        <label>Window start<input type="time" name="window_start" value="${escapeHtml(app.deployment_window?.start || "")}"></label>
        <label>Window end<input type="time" name="window_end" value="${escapeHtml(app.deployment_window?.end || "")}"></label>
      </div></details>
      <details class="am-advanced"><summary>Resource limits</summary><div class="am-form-grid">
        ${Object.entries({ memory_mb: ["Runtime memory (MiB)", 1024], cpu_percent: ["Runtime CPU (%)", 100], tasks_max: ["Runtime process limit", 256], build_memory_mb: ["Build memory (MiB)", 2048], build_cpu_percent: ["Build CPU (%)", 100], build_tasks_max: ["Build process limit", 512] }).map(([key, [label, fallback]]) => `<label>${label}<input type="number" name="limit_${key}" min="1" max="${key.endsWith("memory_mb") ? 1048576 : 10000}" value="${escapeHtml(app.resource_limits?.[key] ?? fallback)}" required></label>`).join("")}
      </div></details>
    `;
    openDialog("Edit application", body, "Save changes", async (form) => {
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.port = Number(payload.port);
      payload.build_config = parseBuildConfig(payload.build_config || "");
      payload.runtime_requirements = parseBuildConfig(payload.runtime_requirements || "{}");
      payload.required_environment = payload.required_environment.split(/[\s,]+/).filter(Boolean);
      payload.storage_directories = payload.storage_directories.split(/[\s,]+/).filter(Boolean);
      payload.python_services = {};
      if (form.elements.native_python.checked) {
        let command;
        try { command = JSON.parse(payload.worker_command || "[]"); } catch (_error) { throw new Error("Worker command must be a JSON argument array."); }
        if (!Array.isArray(command)) throw new Error("Worker command must be a JSON argument array.");
        payload.python_services = {
          redis: form.elements.native_redis.checked, drum_prerequisites: form.elements.native_drum.checked,
          model_files: payload.model_files.split(/[\s,]+/).filter(Boolean),
          upload_limit_mb: Number(payload.upload_limit_mb), access_control: payload.native_access,
          ...(command.length ? { worker: { command, stop_timeout_seconds: Number(payload.worker_timeout), resource_limits: {
            memory_mb: Number(payload.worker_memory_mb), cpu_percent: Number(payload.worker_cpu_percent), tasks_max: Number(payload.worker_tasks_max),
          } } } : {}),
        };
        if (payload.native_access === "external-auth" && !window.confirm("Confirm authentication/access control protects this entire application prefix and every route to the origin. CORS is not authentication.")) return false;
      }
      for (const key of ["native_python", "native_redis", "native_drum", "native_access", "worker_command", "worker_timeout", "worker_memory_mb", "worker_cpu_percent", "worker_tasks_max", "model_files", "upload_limit_mb"]) delete payload[key];
      payload.auto_deploy = form.elements.auto_deploy.checked;
      if (Boolean(payload.window_start) !== Boolean(payload.window_end)) throw new Error("Set both UTC deployment window times or leave both empty.");
      payload.deployment_window = payload.window_start ? { start: payload.window_start, end: payload.window_end } : {};
      delete payload.window_start;
      delete payload.window_end;
      payload.resource_limits = {};
      for (const key of Object.keys(payload).filter((key) => key.startsWith("limit_"))) {
        payload.resource_limits[key.slice(6)] = Number(payload[key]);
        delete payload[key];
      }
      return runOperation(
        () => callBackend("edit", [app.id, JSON.stringify(payload)]),
        "Application updated.",
      );
    });
    byId("appManagerDialog").querySelector("form").addEventListener("invalid", (event) => {
      event.target.closest("details")?.setAttribute("open", "");
    }, true);
  }

  function parseEnvironmentContent(content) {
    const rows = [];
    const seen = new Set();
    for (const [index, rawLine] of String(content || "").split(/\r?\n/).entries()) {
      let line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      if (line.startsWith("export ")) line = line.slice(7).trimStart();
      const match = line.match(/^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
      if (!match) throw new Error(`Invalid environment assignment on line ${index + 1}.`);
      const key = match[1];
      let value = match[2].trim();
      if (seen.has(key)) throw new Error(`Duplicate environment key "${key}".`);
      seen.add(key);
      if (value.startsWith('"') || value.startsWith("'")) {
        const quote = value[0];
        let closingQuote = -1;
        let escaped = false;
        for (let position = 1; position < value.length; position += 1) {
          if (quote === '"' && value[position] === "\\" && !escaped) {
            escaped = true;
            continue;
          }
          if (value[position] === quote && !escaped) {
            closingQuote = position;
            break;
          }
          escaped = false;
        }
        const suffix = closingQuote >= 0 ? value.slice(closingQuote + 1).trim() : "";
        if (closingQuote < 0 || (suffix && !suffix.startsWith("#"))) {
          throw new Error(`Unclosed quoted value on line ${index + 1}.`);
        }
        value = value.slice(1, closingQuote);
        if (quote === '"') {
          value = value.replace(/\\(["\\$`])/g, "$1");
        }
      } else {
        value = value.replace(/\s+#.*$/, "").trim();
      }
      rows.push({ key, value });
    }
    return rows;
  }

  function environmentMarkup(rows) {
    return rows.map(({ key, value }) => `
      <div class="am-env-row">
        <label>Key<input type="text" data-env-key value="${escapeHtml(key)}" autocomplete="off" spellcheck="false" placeholder="VARIABLE_NAME"></label>
        <label>Value<span class="am-secret-input"><input type="password" data-env-value value="${escapeHtml(value)}" autocomplete="new-password" spellcheck="false" placeholder="Value"><button type="button" class="am-icon-button" data-env-toggle aria-label="Reveal value" title="Reveal value">${icon("eye")}</button></span></label>
        <button type="button" class="am-button am-button--danger am-icon-button" data-env-remove aria-label="Remove environment variable ${escapeHtml(key || "row")}" title="Remove variable">${icon("trash-2")}</button>
      </div>
    `).join("");
  }

  function readEnvironmentRows(container) {
    const rows = [];
    const seen = new Set();
    for (const row of container.querySelectorAll(".am-env-row")) {
      const key = row.querySelector("[data-env-key]").value.trim();
      const value = row.querySelector("[data-env-value]").value;
      if (!key && !value) continue;
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
        throw new Error(`"${key || "Blank"}" is not a valid environment variable name.`);
      }
      if (seen.has(key)) throw new Error(`Environment variable "${key}" is duplicated.`);
      seen.add(key);
      rows.push({ key, value });
    }
    return rows;
  }

  function serializeEnvironmentRows(rows) {
    return rows.map(({ key, value }) => {
      const escaped = value
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"')
        .replace(/\$/g, "\\$")
        .replace(/`/g, "\\`");
      return `${key}="${escaped}"`;
    }).join("\n") + (rows.length ? "\n" : "");
  }

  async function readEnvironmentFile(file) {
    if (file.size > 64 * 1024) {
      throw new Error("The selected environment file is larger than 64 KiB.");
    }
    return serializeEnvironmentRows(parseEnvironmentContent(await file.text()));
  }

  function bindEnvironmentImport(input, onImport) {
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        onImport(await readEnvironmentFile(file));
      } catch (error) {
        showError(new Error(`Could not import the selected file: ${error.message}`));
      } finally {
        input.value = "";
      }
    });
  }

  async function manageEnvironment(app) {
    const result = await callBackend("env-get", [app.id]);
    const body = `
      <p class="am-dialog-intro">Private build and runtime environment for ${escapeHtml(app.name)}.</p>
      <div class="am-env-tools">
        <label class="am-button" for="environmentImport">Import .env or text file</label>
        <input id="environmentImport" class="am-visually-hidden" type="file" accept=".env,.env.*,.txt,text/plain">
        <button type="button" class="am-button" data-env-add>Add variable</button>
        <button type="button" class="am-button" data-env-export>Export .env</button>
      </div>
      <div class="am-env-row am-env-row--heading" aria-hidden="true"><span>Key</span><span>Value</span><span></span></div>
      <div id="environmentRows" class="am-env-rows">${environmentMarkup(parseEnvironmentContent(result.content || ""))}</div>
      <small class="am-field-hint">Import one KEY=value assignment per line. Maximum size: 64 KiB. Build-time changes require redeployment; runtime-only changes require a restart. Static HTML has no runtime environment.</small>
    `;
    openDialog(`Environment: ${app.name}`, body, "Save variables", async (form) => {
      const rows = readEnvironmentRows(form.querySelector("#environmentRows"));
      const content = serializeEnvironmentRows(rows);
      if (new TextEncoder().encode(content).length > 64 * 1024) {
        throw new Error("The environment file must be no larger than 64 KiB.");
      }
      return runOperation(
        async () => {
          const response = await callBackendInput("env-save", [app.id], content);
          showNotice(
            `${response.key_count} environment ${response.key_count === 1 ? "variable" : "variables"} saved. Redeploy for build-time changes; restart for runtime-only changes.`,
          );
        },
        "",
      );
    });

    const dialog = byId("appManagerDialog");
    const container = byId("environmentRows");
    const importInput = byId("environmentImport");
    const addRow = (key = "", value = "") => {
      container.insertAdjacentHTML("beforeend", environmentMarkup([{ key, value }]));
      container.lastElementChild.querySelector("[data-env-key]").focus();
      renderIcons();
    };
    dialog.querySelector("[data-env-add]").addEventListener("click", () => addRow());
    bindEnvironmentImport(importInput, (content) => {
      container.innerHTML = environmentMarkup(parseEnvironmentContent(content));
      renderIcons();
    });
    container.addEventListener("click", (event) => {
      const removeButton = event.target instanceof Element ? event.target.closest("[data-env-remove]") : null;
      if (removeButton) removeButton.closest(".am-env-row").remove();
      const toggle = event.target instanceof Element ? event.target.closest("[data-env-toggle]") : null;
      if (toggle) {
        const input = toggle.closest(".am-secret-input").querySelector("input");
        const reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        toggle.setAttribute("aria-label", reveal ? "Hide value" : "Reveal value");
        toggle.title = reveal ? "Hide value" : "Reveal value";
        toggle.innerHTML = icon(reveal ? "eye-off" : "eye");
        renderIcons();
      }
    });
    dialog.querySelector("[data-env-export]").addEventListener("click", () => {
      try {
        const content = serializeEnvironmentRows(readEnvironmentRows(container));
        const file = new Blob([content], { type: "text/plain;charset=utf-8" });
        const url = URL.createObjectURL(file);
        const link = document.createElement("a");
        link.href = url;
        link.download = ".env";
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (error) {
        showError(error);
      }
    });
  }

  function showLogs(appId) {
    void runOperation(async () => {
      const result = await callBackend("logs", [appId, "--lines", "200"]);
      openDialog(`Logs: ${appId}`, `<pre class="am-log-output">${escapeHtml(result.logs || result.message || "No logs available.")}</pre>`);
    }, "Logs loaded.", false);
  }

  function renderDeploymentLogLine(line, channelState) {
    let message = line;
    let timestamp = "";
    const timestampMatch = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$/.exec(message);
    if (timestampMatch) {
      timestamp = timestampMatch[1];
      message = timestampMatch[2];
      channelState.value = null;
    }

    const channelMatch = /^\[(stdout|stderr)\]\s*/i.exec(message);
    if (channelMatch) {
      channelState.value = channelMatch[1].toLowerCase();
      message = message.slice(channelMatch[0].length);
    }

    let level = "info";
    if (/^(?:FAIL\b|ERROR\b)|\b(error|failed|failure|exception|traceback)\b|cannot find module|exit code [1-9]\d*/i.test(message)) {
      level = "error";
    } else if (/\b(?:warn(?:ing)?|aborted|cancelled)\b/i.test(message)) {
      level = "warning";
    } else if (/^(?:PASS\b)|\b(?:successfully|completed)\b/i.test(message)) {
      level = "success";
    } else if (/^Build phase:/i.test(message)) {
      level = "phase";
    } else if (channelState.value === "stderr") {
      level = "stderr";
    } else if (channelState.value === "stdout") {
      level = "stdout";
    }

    const timeLabel = timestamp
      ? `<time class="am-log-time" datetime="${timestamp}" title="${timestamp}">${timestamp.slice(11, 19)}</time>`
      : '<span class="am-log-time" aria-hidden="true"></span>';
    return `<span class="am-log-line am-log-line--${level}" title="${level}">${timeLabel}<span class="am-log-message">${escapeHtml(message)}</span></span>`;
  }

  function renderDeploymentLog(result) {
    const output = byId("deploymentLiveLog");
    const status = byId("deploymentLiveStatus");
    if (!output || !status || state.logTarget?.appId !== result.app_id
      || (state.logTarget.deploymentId && state.logTarget.deploymentId !== result.deployment_id)) return;
    state.logTarget.deploymentId = result.deployment_id;
    const text = result.logs || "Waiting for deployment output…";
    if (typeof result.logs === "string" && state.logTarget.logs !== text) {
      const previousScroll = output.scrollTop;
      const follow = state.logTarget.logs === undefined || output.scrollHeight - previousScroll - output.clientHeight < 32;
      const channelState = { value: null };
      output.innerHTML = text.split("\n")
        .map((line) => renderDeploymentLogLine(line, channelState))
        .join("\n");
      output.scrollTop = follow ? output.scrollHeight : previousScroll;
      state.logTarget.logs = text;
    }
    if (status) {
      status.dataset.status = result.active ? "running" : result.status || "unknown";
    }
    if (result.active) {
      status.textContent = result.abortable === false
        ? "Finalizing deployment"
        : result.aborting ? "Cancellation requested"
        : result.build_status === "queued" ? "Queued · waiting for build capacity"
          : result.build_status === "preflight" ? "Checking requirements"
            : result.build_status === "running" ? "Building application"
              : "Deployment in progress";
    } else {
      const finalStatuses = {
        deployed: "Deployment completed",
        failed: "Deployment failed",
        aborted: "Deployment was aborted",
      };
      status.textContent = finalStatuses[result.status] || "Deployment status unavailable";
    }
    const copyButton = byId("copyDeploymentLogButton");
    const downloadButton = byId("downloadDeploymentLogButton");
    if (copyButton) copyButton.disabled = !state.logTarget.logs;
    if (downloadButton) downloadButton.disabled = !state.logTarget.logs;
    const deploymentId = byId("deploymentLogId");
    if (deploymentId) deploymentId.textContent = result.deployment_id || "Starting";
    const revision = byId("deploymentLogRevision");
    if (revision && result.commit) {
      revision.textContent = result.commit.slice(0, 7);
      revision.title = result.commit;
    }
    const outputState = byId("deploymentOutputState");
    if (outputState) outputState.textContent = result.active ? "Live" : "Finished";
    const abortButton = byId("abortDeploymentButton");
    if (abortButton) {
      abortButton.hidden = !result.active || result.abortable === false;
      abortButton.disabled = abortButton.dataset.pending === "true";
      abortButton.textContent = abortButton.disabled ? "Aborting…" : result.aborting ? "Retry abort" : "Abort deployment";
      abortButton.dataset.deploymentId = result.deployment_id || "";
      abortButton.dataset.appId = result.app_id || "";
    }
  }

  async function copyDeploymentLog() {
    const text = state.logTarget?.logs || "";
    if (!text) throw new Error("There is no deployment log to copy yet.");
    if (window.navigator.clipboard?.writeText) {
      await window.navigator.clipboard.writeText(text);
      return;
    }
    const fallback = document.createElement("textarea");
    fallback.value = text;
    fallback.setAttribute("readonly", "");
    fallback.className = "am-visually-hidden";
    document.body.append(fallback);
    fallback.select();
    let copied = false;
    try {
      copied = typeof document.execCommand === "function" && document.execCommand("copy");
    } finally {
      fallback.remove();
    }
    if (!copied) throw new Error("Clipboard access is unavailable in this Cockpit session.");
  }

  function downloadDeploymentLog() {
    const text = state.logTarget?.logs || "";
    if (!text) throw new Error("There is no deployment log to download yet.");
    const appId = String(state.logTarget.appId || "application").replace(/[^a-z0-9-]/gi, "-");
    const deploymentId = String(state.logTarget.deploymentId || "latest").replace(/[^a-z0-9-]/gi, "-");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `deployment-${appId}-${deploymentId}.txt`;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function openDeploymentLogDialog(appId) {
    const app = state.apps.find((item) => item.id === appId);
    const branch = app?.branch || "Unavailable";
    const route = app?.active_route || app?.route || `/${appId}/`;
    openDialog(
      app?.name || appId,
      `<section class="am-deployment-summary" aria-label="Deployment details">
         <p id="deploymentLiveStatus" class="am-deployment-status" data-status="running" role="status" aria-live="polite">Starting deployment…</p>
         <dl class="am-deployment-facts">
           <div><dt>Branch</dt><dd>${icon("git-branch")}<span>${escapeHtml(branch)}</span></dd></div>
           <div><dt>Route</dt><dd><span>${escapeHtml(route)}</span></dd></div>
           <div><dt>Source revision</dt><dd><code id="deploymentLogRevision">Pending</code></dd></div>
           <div class="am-deployment-facts__id"><dt>Deployment ID</dt><dd><code id="deploymentLogId">Starting</code></dd></div>
         </dl>
       </section>
       <section class="am-log-window" aria-labelledby="deploymentOutputHeading">
         <div class="am-log-window__bar">
           <h3 id="deploymentOutputHeading">${icon("terminal")} Build output <span id="deploymentOutputState" class="am-output-state">Live</span></h3>
           <div class="am-deployment-log-actions">
             <button id="copyDeploymentLogButton" type="button" class="am-button am-icon-button" title="Copy log" aria-label="Copy log" disabled>${icon("copy")}<span class="am-visually-hidden">Copy log</span></button>
             <button id="downloadDeploymentLogButton" type="button" class="am-button am-icon-button" title="Download log (.txt)" aria-label="Download log (.txt)" disabled>${icon("download")}</button>
           </div>
         </div>
         <pre id="deploymentLiveLog" class="am-log-output" aria-label="Deployment log output" aria-live="off" tabindex="0">Waiting for deployment output…</pre>
       </section>`,
    );
    const dialog = byId("appManagerDialog");
    dialog.classList.add("am-dialog--deployment");
    byId("appManagerDialogTitle").insertAdjacentHTML("afterbegin", '<span class="am-deployment-eyebrow">Deployment</span>');
    dialog.querySelector(".am-dialog-footer").insertAdjacentHTML("afterbegin",
      '<button id="abortDeploymentButton" type="button" class="am-button am-button--danger" data-action="abort-deployment" hidden>Abort deployment</button>');
    state.logTarget = { appId, deploymentId: null };
    byId("copyDeploymentLogButton").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      try {
        await copyDeploymentLog();
        button.innerHTML = `${icon("check")}<span class="am-visually-hidden">Copied</span>`;
        button.title = "Copied";
        button.setAttribute("aria-label", "Copied");
        renderIcons();
        window.setTimeout(() => {
          if (button.isConnected) {
            button.innerHTML = `${icon("copy")}<span class="am-visually-hidden">Copy log</span>`;
            button.title = "Copy log";
            button.setAttribute("aria-label", "Copy log");
            renderIcons();
          }
        }, 1800);
      } catch (error) {
        showError(error);
      }
    });
    byId("downloadDeploymentLogButton").addEventListener("click", () => {
      try {
        downloadDeploymentLog();
      } catch (error) {
        showError(error);
      }
    });
  }

  async function monitorDeployment(appId, deploymentId) {
    if (state.deploymentMonitor?.deploymentId === deploymentId) return state.deploymentMonitor.promise;
    const monitor = { appId, deploymentId, promise: null };
    state.deploymentMonitor = monitor;
    monitor.promise = (async () => {
      while (state.deploymentMonitor === monitor) {
        const logsVisible = byId("appManagerDialog").open && state.logTarget?.appId === appId
          && state.logTarget?.deploymentId === deploymentId;
        try {
          const result = await callBackend("deployment-logs", [appId, deploymentId, ...(logsVisible ? [] : ["--status-only"])]);
          renderDeploymentLog(result);
          if (!result.active) break;
        } catch (error) {
          const status = byId("deploymentLiveStatus");
          if (status && state.logTarget?.appId === appId && state.logTarget?.deploymentId === deploymentId) {
            status.textContent = `Live log unavailable; retrying: ${error.message}`;
          }
        }
        await new Promise((resolve) => window.setTimeout(resolve, logsVisible ? 1500 : 3000));
      }
      if (state.deploymentMonitor === monitor) state.deploymentMonitor = null;
      void refreshData().catch(showError);
    })();
    return monitor.promise;
  }

  async function deployWithLiveLog(appId) {
    openDeploymentLogDialog(appId);
    const started = await callBackend("deployment-start", [appId]);
    if (!started.deployment_id) throw new Error("The backend did not return a deployment identifier.");
    renderDeploymentLog({
      app_id: appId,
      deployment_id: started.deployment_id,
      active: true,
      status: "deploying",
      logs: "Deployment queued",
    });
    await refreshData();
    void monitorDeployment(appId, started.deployment_id);
  }

  async function viewDeploymentLog(appId, deploymentId) {
    const args = deploymentId ? [appId, deploymentId] : [appId];
    const result = await callBackend("deployment-logs", args);
    if (!result.deployment_id) {
      throw new Error("No deployment log is available for this application.");
    }
    openDeploymentLogDialog(appId);
    renderDeploymentLog(result);
    if (result.active) void monitorDeployment(appId, result.deployment_id);
  }

  function resumeActiveDeployment() {
    if (state.deploymentMonitor) return;
    const active = state.deployments.find((deployment) => deployment.status === "deploying");
    if (!active) return;
    void monitorDeployment(active.app_id, active.id);
  }

  function showPluginUpdate(result) {
    let installed = null;
    const latest = String(result.latest_commit || "");
    const body = `
      <p class="am-dialog-intro">A new App Manager update from the main branch is available.</p>
      <dl class="am-settings-paths">
        <div><dt>Installed</dt><dd>${escapeHtml(result.current_commit || "Unknown")}</dd></div>
        <div><dt>Available</dt><dd>${escapeHtml(latest || "Unknown")}</dd></div>
        <div><dt>Branch</dt><dd>${escapeHtml(result.branch || "main")}</dd></div>
      </dl>
      <p class="am-update-warning">${icon("circle-alert")} Updates from GitHub main are installed as root. Use this only if you trust the repository and its branch protections.</p>
      <p class="am-update-warning">${icon("circle-alert")} Cockpit will restart after installation. Running applications are not restarted.</p>
    `;
    openDialog("Update App Manager", body, "Install update and restart Cockpit", async () => {
      setBusy(true);
      try {
        const update = await callBackend("plugin-update");
        installed = update;
        return true;
      } finally {
        setBusy(false);
      }
    }, () => {
      if (!installed.updated) { showNotice("App Manager is already up to date."); return; }
      if (!installed.restart_scheduled) {
        showError(new Error(`Update installed, but Cockpit could not restart: ${installed.restart_error || "Restart Cockpit manually to apply the update."}`));
        return;
      }
      showNotice("Update installed. Cockpit is restarting; reconnecting automatically...");
      updateConnection("busy", "Restarting Cockpit...");
      window.setTimeout(() => {
        try { window.top.location.reload(); } catch (_error) { window.location.reload(); }
      }, Math.max(12, Number(installed.restart_delay_seconds || 5) + 7) * 1000);
    });
  }

  function checkPluginUpdates() {
    void runOperation(async () => {
      const result = await callBackend("plugin-update-check");
      if (result.available) {
        showPluginUpdate(result);
      } else {
        showNotice("App Manager is up to date.");
      }
    }, "", false);
  }

  function formatBytes(value) {
    if (value == null || !Number.isFinite(Number(value))) return "Unavailable";
    return `${(Number(value) / 1048576).toFixed(1)} MiB`;
  }

  async function showMetrics(app) {
    const result = await callBackend("metrics", [app.id]);
    const metrics = result.metrics || {};
    app.metrics = metrics;
    renderAppList();
    const fields = [
      ["Service", metrics.service_state || "Unknown"], ["CPU", metrics.cpu_percent == null ? "Not sampled" : `${metrics.cpu_percent}%`],
      ["Memory", formatBytes(metrics.memory_bytes)], ["Peak memory", formatBytes(metrics.peak_memory_bytes)],
      ["Processes", metrics.tasks ?? "Unavailable"], ["Restarts", metrics.restarts ?? "Unavailable"],
      ["Uptime", metrics.uptime_seconds == null ? "Unavailable" : `${Math.floor(metrics.uptime_seconds)} seconds`],
      ["Production files", `${formatBytes(metrics.disk_bytes)}${metrics.disk_truncated ? " (partial)" : ""}`],
      ["Persistent data", `${formatBytes(metrics.storage_bytes)}${metrics.storage_truncated ? " (partial)" : ""}`],
      ["Logs", `${formatBytes(metrics.logs_bytes)}${metrics.logs_truncated ? " (partial)" : ""}`],
      ["Source checkout", `${formatBytes(metrics.source_bytes)}${metrics.source_truncated ? " (partial)" : ""}`],
      ["Sampled", formatDate(metrics.checked_at)], ["Persistent directories", (app.storage_directories || ["data"]).join(", ")],
    ];
    openDialog(`Metrics: ${app.name}`, `<dl class="am-settings-paths">${fields.map(([name, value]) => `<div><dt>${escapeHtml(name)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>
      ${(metrics.errors || []).map((error) => `<p class="am-deploy-error">${escapeHtml(error)}</p>`).join("")}
      <button type="button" class="am-button" data-action="metrics" data-id="${escapeHtml(app.id)}">${icon("refresh-cw")} Refresh metrics</button>`);
  }

  async function showPreflight(app) {
    const report = await callBackend("preflight", [app.id]);
    openDialog(`Preflight: ${app.name}`, `<p class="am-dialog-intro">${report.passed ? "Checks passed" : "Checks failed"}</p>
      <dl class="am-settings-paths">${(report.checks || []).map((check) => `<div><dt>${escapeHtml(check.name)}</dt><dd>${check.passed ? "PASS" : "FAIL"}: ${escapeHtml(check.message)}</dd></div>`).join("")}</dl>
      ${(report.errors || []).map((error) => `<p class="am-deploy-error">${escapeHtml(error)}</p>`).join("")}
      ${(report.warnings || []).map((warning) => `<p>${escapeHtml(warning)}</p>`).join("")}`);
  }

  async function showUpdateReview(app) {
    const result = await callBackend("review-update", [app.id]);
    const pending = result.pending_update;
    let started;
    openDialog(`Review: ${app.name}`, `<dl class="am-settings-paths"><div><dt>Live commit</dt><dd>${escapeHtml(pending.base_commit || "Not deployed")}</dd></div><div><dt>Candidate</dt><dd>${escapeHtml(pending.commit)}</dd></div></dl>
      <pre class="am-log-output">${escapeHtml(pending.commits || "No commit summary")}</pre><pre class="am-log-output">${escapeHtml(pending.diffstat || "No file changes")}</pre>`, "Approve deployment", async () => {
      return runOperation(async () => {
        started = await callBackend("approve", [app.id, pending.commit]);
        if (!started.deployment_id) throw new Error("The backend did not return a deployment ID.");
      }, "", false);
    }, () => {
      openDeploymentLogDialog(app.id);
      state.logTarget.deploymentId = started.deployment_id;
      void monitorDeployment(app.id, started.deployment_id).catch(showError);
    });
  }

  async function handleAppAction(action, appId, deploymentId) {
    const app = state.apps.find((item) => item.id === appId);
    if (!app) {
      showError(new Error("Application is no longer in the list. Refresh and try again."));
      return;
    }
    if (action === "edit") {
      editApplication(app);
      return;
    }
    if (action === "logs") {
      showLogs(appId);
      return;
    }
    if (["worker-status", "worker-logs", "worker-restart"].includes(action)) {
      if (action === "worker-restart" && !window.confirm(`Restart the worker for "${app.name}"? Running work may be terminated after its shutdown timeout.`)) return;
      await runOperation(async () => {
        const result = await callBackend(action, [appId]);
        openDialog(`Worker: ${app.name}`, action === "worker-logs"
          ? `<pre class="am-log-output">${escapeHtml(result.logs || result.message || "No worker logs")}</pre>`
          : `<dl class="am-settings-paths">${Object.entries(result.worker || {}).map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("")}</dl>`);
      }, "", false);
      return;
    }
    if (action === "metrics" || action === "preflight" || action === "review-update" || action === "deploy" && app.deployment_mode === "approval") {
      await runOperation(() => action === "metrics" ? showMetrics(app) : action === "preflight" ? showPreflight(app) : showUpdateReview(app), "", false);
      return;
    }
    if (action === "deployment-log") {
      await runOperation(() => viewDeploymentLog(appId, deploymentId), "", false);
      return;
    }
    if (action === "environment") {
      await runOperation(() => manageEnvironment(app), "", false);
      return;
    }
    if (action === "remove") {
      if (!window.confirm(
        `Permanently remove "${app.name}"? This deletes its source, releases, storage, logs, environment secrets, service account, and managed configuration. This cannot be undone.`
      )) return;
      await runOperation(
        () => callBackend("remove", [appId]),
        "Application and its managed data were permanently removed.",
      );
      return;
    }
    if (action === "health") {
      await runOperation(async () => {
        const result = await callBackend("health", [appId]);
        await refreshData();
        if (!result.healthy) throw new Error(`Health check failed (${result.url}).`);
        if (result.public_healthy === false) throw new Error(`Local readiness passed, but the public route failed: ${result.public_error || result.public_url || "unavailable"}.`);
        showNotice(app.framework === "static" && !app.build_config?.start_command
          ? "Static release is available."
          : `Runtime health check passed (${result.url}).`);
      }, "", false);
      return;
    }
    const confirmations = {
      deploy: `Deploy "${app.name}" now? This builds a new release and may restart the application.`,
      pull: `Pull updates for "${app.name}" from its configured branch?`,
      restart: `Restart "${app.name}"?`,
      stop: `Stop "${app.name}"?`,
      start: `Start "${app.name}"?`,
    };
    if (["stop", "restart"].includes(action) && !window.confirm(confirmations[action])) return;
    if (action === "deploy") {
      await runOperation(() => deployWithLiveLog(appId), "", false);
      return;
    }
    const labels = {
      deploy: "Deployment completed.",
      pull: "Repository update completed.",
      restart: "Application restarted.",
      stop: "Application stopped.",
      start: "Application started.",
    };
    await runOperation(
      () => callBackend(action, [appId]),
      labels[action] || `${action} completed.`,
    );
  }

  function bindTabs() {
    const tabs = Array.from(document.querySelectorAll('.am-app-manager [role="tab"][data-tab]'));
    function renderDockerStatus(status) {
      const rows = [
        ["Engine", status.installed ? (status.engine_version || "Installed; version unavailable") : "Not installed"],
        ["Daemon", status.daemon_active ? "Active" : "Inactive"],
        ["Compose", status.compose_available ? (status.compose_version || "Installed") : "Not installed"],
      ];
      byId("dockerStatus").innerHTML = rows.map(([label, value]) =>
        `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`
      ).join("");
      renderIcons();
    }
    async function refreshDockerStatus() {
      renderDockerStatus(await callBackend("docker-status"));
    }
    function activate(tab) {
      tabs.forEach((item) => {
        const selected = item === tab;
        item.classList.toggle("active", selected);
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        const panel = byId(item.dataset.tab);
        panel.classList.toggle("hidden", !selected);
        panel.hidden = !selected;
      });
    }

    tabs.forEach((tab) => {
      tab.addEventListener("click", () => {
        activate(tab);
        if (tab.dataset.tab === "docker" && !state.busy) {
          void runOperation(refreshDockerStatus, "", false);
        }
      });
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const currentIndex = tabs.indexOf(tab);
        const nextIndex = event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : (currentIndex + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
        tabs[nextIndex].focus();
        activate(tabs[nextIndex]);
      });
    });
    byId("dockerRefreshButton").addEventListener("click", () => {
      void runOperation(refreshDockerStatus, "", false);
    });
    byId("dockerInstallButton").addEventListener("click", () => {
      if (state.busy || !window.confirm("Install or update Docker Engine and Compose using Ubuntu APT packages?")) return;
      void runOperation(async () => {
        renderDockerStatus(await callBackend("docker-install"));
      }, "Docker Engine and Compose are installed and running.", false);
    });
  }

  document.querySelector(".am-app-manager").addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-open-add]") : null;
    if (!target || state.busy) return;
    openAddApplicationDialog();
  });
  byId("refreshButton").addEventListener("click", () => {
    void runOperation(refreshData, "Application data refreshed.", false);
  });
  byId("checkUpdateButton").addEventListener("click", checkPluginUpdates);
  byId("appSearch").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderAppList();
  });
  byId("appSort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    renderAppList();
  });
  document.querySelectorAll("[data-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      document.querySelectorAll("[data-filter]").forEach((filter) => filter.setAttribute("aria-pressed", String(filter === button)));
      renderAppList();
    });
  });
  byId("appsTableContainer").addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest("[data-clear-filters]")) {
      state.query = "";
      byId("appSearch").value = "";
      document.querySelector('[data-filter="all"]').click();
      return;
    }
    const button = event.target instanceof Element ? event.target.closest("button[data-action]") : null;
    if (button && !button.disabled && !state.busy) {
      button.closest("details")?.removeAttribute("open");
      void handleAppAction(button.dataset.action, button.dataset.id, button.dataset.deploymentId).catch(showError);
    }
  });
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".am-app-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") document.querySelectorAll(".am-app-menu[open]").forEach((menu) => menu.removeAttribute("open"));
  });
  byId("deploymentHistory").addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest('button[data-action="deployment-log"]') : null;
    if (button) void runOperation(() => viewDeploymentLog(button.dataset.id, button.dataset.deploymentId), "", false);
  });
  byId("appManagerDialog").addEventListener("click", (event) => {
    const metricsButton = event.target instanceof Element ? event.target.closest('[data-action="metrics"]') : null;
    if (metricsButton && !metricsButton.disabled && !state.busy) {
      void handleAppAction("metrics", metricsButton.dataset.id).catch(showError);
    }
    const abortButton = event.target instanceof Element ? event.target.closest('[data-action="abort-deployment"]') : null;
    if (abortButton && !abortButton.disabled) {
      const appId = abortButton.dataset.appId;
      const deploymentId = abortButton.dataset.deploymentId;
      abortButton.dataset.pending = "true";
      abortButton.disabled = true;
      abortButton.textContent = "Aborting…";
      void callBackend("deployment-abort", [
        appId,
        deploymentId,
      ]).then((result) => {
        if (result.aborted) {
          renderDeploymentLog({ app_id: appId, deployment_id: deploymentId, active: false, status: "aborted" });
        }
      }).catch(showError).finally(() => {
        delete abortButton.dataset.pending;
        abortButton.disabled = false;
        abortButton.textContent = "Retry abort";
      });
    }
    if (event.target === event.currentTarget && !state.busy) event.currentTarget.close();
  });
  byId("appManagerDialog").addEventListener("cancel", (event) => { if (state.busy) event.preventDefault(); });
  bindTabs();
  renderIcons();
  async function initialize() {
    setBusy(true);
    try {
      await waitForCockpitApi();
      setBusy(false);
      await runOperation(refreshData, "", false);
    } catch (error) {
      showError(error);
    } finally {
      setBusy(false);
    }
  }
  void initialize();
})();
