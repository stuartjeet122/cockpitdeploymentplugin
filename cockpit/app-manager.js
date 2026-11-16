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
    integration: {
      provider: "portainer",
      portainer: { token: "", endpoints: [], endpointId: "", resource: "containers", data: null },
      npm: { username: "", password: "", resource: "proxy-hosts", data: null, routeAppId: "" },
    },
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
      || app.health?.local_healthy === false
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
      const deploying = app.status === "deploying";
      const staticApp = app.framework === "static" && !app.build_config?.start_command;
      const appId = escapeHtml(app.id);
      const routeAction = app.last_deploy && !staticApp
        ? `<button type="button" class="am-route-state am-route-action" data-action="route" data-id="${appId}">Create NPM route</button>`
        : `<span class="am-route-state">Routing is managed separately in NPM</span>`;
      const unavailable = deploying ? " disabled data-unavailable" : "";
      const runtimeUnavailable = deploying || !app.last_deploy ? " disabled data-unavailable" : "";
      const serviceControl = staticApp ? "" : observedStatus(app) === "running"
        ? `<button type="button" data-action="stop" data-id="${appId}"${runtimeUnavailable}>${icon("pause")} Stop application</button>`
        : `<button type="button" data-action="start" data-id="${appId}"${runtimeUnavailable}>${icon("play")} Start application</button>`;
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
          <div class="am-app-route">${icon("globe")}<code>${escapeHtml(app.route)}</code>${routeAction}</div>
          <p class="am-repository" title="${escapeHtml(app.repo_url)}">${icon("git-branch")}<span>${escapeHtml(app.repo_url)}</span></p>
          <dl class="am-app-meta">
            <div><dt>Branch</dt><dd>${escapeHtml(app.branch || "main")}</dd></div>
            <div><dt>${app.framework === "static" && !app.build_config?.start_command ? "Content" : "Runtime"}</dt><dd>${app.framework === "static" && !app.build_config?.start_command ? "Static files" : `127.0.0.1:${escapeHtml(app.port)}`}</dd></div>
            <div class="am-meta-wide"><dt>Last deployed</dt><dd>${escapeHtml(formatDate(app.last_deploy))}${app.deployed_commit ? ` <code>${escapeHtml(app.deployed_commit.slice(0, 7))}</code>` : ""}</dd></div>
            <div><dt>Readiness</dt><dd data-health="${app.health ? (app.health.local_healthy ? "healthy" : "failed") : "unknown"}" title="${escapeHtml(app.health?.local_error || "")}">${app.health ? (app.health.local_healthy ? "Healthy" : "Failed") : "Not checked"}</dd></div>
            <div><dt>Container port</dt><dd>${app.framework === "docker" ? escapeHtml(app.build_config?.container_port || "Configured in deployment") : "—"}</dd></div>
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
          ${app.health && (app.health.local_healthy === false || app.health.service_error) ? `<p class="am-deploy-error">${icon("circle-alert")}<span>${escapeHtml(app.health.local_error || app.health.service_error)}</span></p>` : ""}
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
                ${staticApp ? "" : `<button type="button" data-action="restart" data-id="${appId}"${runtimeUnavailable}>${icon("refresh-cw")} Restart</button>`}
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

  const integrationResources = {
    portainer: [
      ["containers", "Containers"], ["stacks", "Stacks"], ["images", "Images"],
      ["volumes", "Volumes"], ["networks", "Networks"], ["info", "Engine info"],
    ],
    npm: [
      ["proxy-hosts", "Proxy hosts"], ["redirection-hosts", "Redirection hosts"],
      ["dead-hosts", "404 hosts"], ["streams", "Streams"],
      ["certificates", "Certificates"], ["access-lists", "Access lists"],
    ],
  };

  function integrationCall(provider, action, resource, extra = {}) {
    const connection = state.integration[provider];
    const payload = { provider, action, resource, ...extra };
    if (provider === "portainer") payload.token = connection.token;
    else payload.credentials = { username: connection.username, password: connection.password };
    return callBackendInput("integration", [], JSON.stringify(payload));
  }

  function integrationRows(data) {
    if (Array.isArray(data)) return data;
    if (Array.isArray(data?.data)) return data.data;
    if (Array.isArray(data?.records)) return data.records;
    if (Array.isArray(data?.Volumes)) return data.Volumes;
    return [];
  }

  function integrationLabel(provider, resource, item) {
    if (provider === "portainer") {
      if (resource === "containers") return (item.Names || [item.Name || item.Id || "Container"]).join(", ").replace(/^\//, "");
      if (resource === "images") return (item.RepoTags || [item.Id || "Image"]).join(", ");
      return item.Name || item.name || item.Id || `${resource} resource`;
    }
    return (item.domain_names || [item.name || item.display_name || `${resource} resource`]).join(", ");
  }

  function integrationColumns(provider, resource) {
    if (provider === "npm") return [
      ["enabled", "Enabled"], ["forward_host", "Forward host"], ["forward_port", "Port"],
      ["ssl_forced", "HTTPS"], ["domain_names", "Domains"],
    ];
    return ({
      containers: [["Image", "Image"], ["State", "State"], ["Status", "Status"], ["Ports", "Ports"]],
      stacks: [["Status", "Status"], ["Type", "Type"], ["EndpointId", "Environment"]],
      images: [["Size", "Size"], ["Created", "Created"]],
      volumes: [["Driver", "Driver"], ["Scope", "Scope"], ["Mountpoint", "Mount path"]],
      networks: [["Driver", "Driver"], ["Scope", "Scope"]],
    })[resource] || [];
  }

  function integrationCell(value) {
    if (Array.isArray(value)) return value.map((part) => typeof part === "object" ? JSON.stringify(part) : part).join(", ");
    if (value && typeof value === "object") return JSON.stringify(value);
    if (typeof value === "boolean") return value ? "Yes" : "No";
    return value == null || value === "" ? "—" : String(value);
  }

  function renderNpmRouteCreator() {
    const connection = state.integration.npm;
    const apps = state.apps.filter((app) => app.last_deploy && app.port
      && !(app.framework === "static" && !app.build_config?.start_command));
    const selectedApp = apps.find((app) => String(app.id) === connection.routeAppId);
    const options = apps.map((app) => `<option value="${escapeHtml(app.id)}" data-forward-port="${escapeHtml(app.port)}"${String(app.id) === connection.routeAppId ? " selected" : ""}>${escapeHtml(app.name)} · port ${escapeHtml(app.port)}</option>`).join("");
    return `<details class="am-resource-editor am-route-creator"${connection.routeAppId ? " open" : ""}>
      <summary>Create proxy route</summary>
      <form data-npm-route>
        <div class="am-form-grid">
          <label>Domain name(s)<input name="domain_names" required placeholder="app.example.com" autocomplete="off"></label>
          <label>Deployed App Manager app (optional)<select name="app_id" data-route-app>
            <option value="" data-forward-port=""${connection.routeAppId ? "" : " selected"}>Choose an app to fill its port</option>${options}
          </select></label>
          <label>Forward host (reachable from NPM)<input name="forward_host" required placeholder="127.0.0.1, container name, or reachable IP" autocomplete="off"></label>
          <label>Forward port<input name="forward_port" type="number" min="1" max="65535" step="1" value="${escapeHtml(selectedApp?.port || "")}" required></label>
          <label>Forward scheme<select name="forward_scheme"><option value="http">HTTP</option><option value="https">HTTPS</option></select></label>
          <label class="am-checkbox"><input type="checkbox" name="allow_websocket_upgrade" checked><span>Allow WebSocket upgrades</span></label>
          <label class="am-checkbox"><input type="checkbox" name="block_exploits" checked><span>Block common exploits</span></label>
        </div>
        <p class="am-field-hint">The selected app fills its managed port. The host must be reachable from NPM: 127.0.0.1 works only when NPM shares the host network. Portainer-managed containers can use a service name on a shared Docker network. App Manager-hosted apps bind to loopback, so a separate NPM container needs an explicit host-level network path.</p>
        <button type="submit" class="am-button am-button--primary">Create proxy route</button>
      </form>
    </details>`;
  }

  function renderIntegrationTable(provider, connection) {
    const resource = connection.resource;
    const rows = integrationRows(connection.data);
    if (provider === "portainer" && resource === "info") {
      return `<pre class="am-log-output">${escapeHtml(JSON.stringify(connection.data || {}, null, 2))}</pre>`;
    }
    const columns = integrationColumns(provider, resource);
    const createRoute = provider === "npm" && resource === "proxy-hosts" ? renderNpmRouteCreator() : "";
    const createEditor = provider === "npm" && resource !== "proxy-hosts" ? `
      <details class="am-resource-editor">
        <summary>Create ${escapeHtml(integrationResources.npm.find(([key]) => key === resource)?.[1] || "resource")}</summary>
        <form data-npm-create>
          <label>API payload (JSON)<textarea name="body" rows="8" required spellcheck="false">{}</textarea></label>
          <p class="am-field-hint">${resource === "certificates" ? "Use the fields supported by this NPM version. Custom certificate file uploads and user management remain in the NPM interface." : "Use the fields supported by this Nginx Proxy Manager resource. The list refreshes when the API confirms the change."}</p>
          <button type="submit" class="am-button am-button--primary">Create resource</button>
        </form>
      </details>` : "";
    if (!rows.length) {
        return `${createRoute}${createEditor}<div class="am-empty-state am-integration-empty">
        <span class="am-empty-state__icon" aria-hidden="true">${icon("boxes")}</span>
        <h3>No ${escapeHtml(integrationResources[provider].find(([key]) => key === resource)?.[1].toLowerCase() || "resources")} found</h3>
        <p>Refresh to check the connected service again.</p>
      </div>`;
    }
    const header = columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("");
    const body = rows.map((item) => {
      const id = item.Id ?? item.id ?? item.Name ?? item.name;
      const cells = columns.map(([key]) => `<td>${escapeHtml(integrationCell(item[key]))}</td>`).join("");
      let controls = "";
      if (provider === "portainer" && resource === "containers") {
        const running = String(item.State || "").toLowerCase() === "running";
        const paused = String(item.State || "").toLowerCase() === "paused";
        controls = `${running || paused ? `<button class="am-button am-button--quiet" type="button" data-resource-action="stop" data-id="${escapeHtml(id)}">Stop</button>` : `<button class="am-button am-button--quiet" type="button" data-resource-action="start" data-id="${escapeHtml(id)}">Start</button>`}
          <button class="am-button am-button--quiet" type="button" data-resource-action="restart" data-id="${escapeHtml(id)}">Restart</button>
          ${running ? `<button class="am-button am-button--quiet" type="button" data-resource-action="pause" data-id="${escapeHtml(id)}">Pause</button>` : paused ? `<button class="am-button am-button--quiet" type="button" data-resource-action="unpause" data-id="${escapeHtml(id)}">Resume</button>` : ""}
          ${running || paused ? `<button class="am-button am-button--danger" type="button" data-resource-action="kill" data-id="${escapeHtml(id)}">Kill</button>` : ""}
          <button class="am-button am-button--quiet" type="button" data-resource-action="logs" data-id="${escapeHtml(id)}">Logs</button>
          <button class="am-button am-button--quiet" type="button" data-resource-action="inspect" data-id="${escapeHtml(id)}">Inspect</button>
          <button class="am-button am-button--danger" type="button" data-resource-action="remove" data-id="${escapeHtml(id)}">Remove</button>`;
      } else if (provider === "portainer" && ["stacks", "images", "volumes", "networks"].includes(resource)) {
        controls = `<button class="am-button am-button--danger" type="button" data-resource-action="remove" data-id="${escapeHtml(id)}">Remove</button>`;
      } else if (provider === "npm") {
        controls = `${resource === "certificates" ? "" : `<details class="am-row-editor"><summary>Edit JSON</summary>
            <form data-npm-update data-id="${escapeHtml(id)}">
              <label>API payload (JSON)<textarea name="body" rows="8" required spellcheck="false">${escapeHtml(JSON.stringify(item, null, 2))}</textarea></label>
              <button type="submit" class="am-button am-button--primary">Save changes</button>
            </form>
          </details>`}
          <button class="am-button am-button--danger" type="button" data-resource-action="delete" data-id="${escapeHtml(id)}">Delete</button>`;
      }
      return `<tr>
        <td><strong>${escapeHtml(integrationLabel(provider, resource, item))}</strong></td>${cells}
        <td class="am-resource-actions">${controls || "—"}</td>
      </tr>`;
    }).join("");
    return `${createRoute}${createEditor}<div class="am-deployment-table-wrap am-resource-table-wrap"><table class="table">
      <thead><tr><th>Name</th>${header}<th>Actions</th></tr></thead><tbody>${body}</tbody>
    </table></div>`;
  }

  function renderInfrastructure() {
    const host = byId("integrationPanel");
    if (!host) return;
    const settings = state.settings.integrations || {};
    const provider = state.integration.provider;
    const current = state.integration[provider];
    const isConnected = provider === "portainer" ? Boolean(current.token) : Boolean(current.username && current.password);
    const configured = provider === "portainer" ? settings.portainer_url : settings.npm_url;
    const serviceName = provider === "portainer" ? "Portainer" : "Nginx Proxy Manager";
    const providerChoices = [
      ["portainer", "Portainer", "boxes"],
      ["npm", "Nginx Proxy Manager", "globe"],
    ];
    const serviceCards = providerChoices.map(([key, label, symbol]) => `
      <button type="button" class="am-integration-choice${provider === key ? " is-active" : ""}" data-integration-provider="${key}" aria-pressed="${provider === key}">
        <span class="am-integration-choice__icon">${icon(symbol)}</span>
        <span><strong>${label}</strong><small>${key === "portainer" ? "Docker environments" : "Proxy hosts, certificates and access"}</small></span>
        <span class="am-status-pill" data-status="${state.integration[key].data ? "running" : "unknown"}">${state.integration[key].data ? "Connected" : "Not connected"}</span>
      </button>`).join("");
    let panelContent = "";
    if (!configured) {
      panelContent = `<div class="am-integration-connect am-empty-state">
        <span class="am-empty-state__icon">${icon("key-round")}</span>
        <h3>Connect ${serviceName}</h3>
        <p>Save its API URL in Settings to begin. API credentials are not saved.</p>
        <button type="button" class="am-button am-button--primary" data-go-settings>Open Settings</button>
      </div>`;
    } else if (!isConnected) {
      panelContent = provider === "portainer" ? `
        <form id="portainerConnectionForm" class="am-integration-connect" data-integration-connect="portainer" autocomplete="on">
          <span class="am-integration-eyebrow">PORTAINER API</span>
          <h3>Connect to Docker</h3>
          <p>Save this account and API key in your browser's password manager, then select that saved entry next time. App Manager itself keeps the key only for this page session, not in server files.</p>
          <label>Password manager account<input name="username" type="text" autocomplete="username" value="${escapeHtml(`Portainer API key: ${configured}`)}" readonly></label>
          <label>API key<input name="token" type="password" autocomplete="current-password" required maxlength="4096" placeholder="ptr_…"></label>
          <button type="submit" class="am-button am-button--primary">Connect</button>
        </form>` : `
        <form class="am-integration-connect" data-integration-connect="npm">
          <span class="am-integration-eyebrow">NGINX PROXY MANAGER API</span>
          <h3>Connect to your proxy</h3>
          <p>Sign in with a Nginx Proxy Manager account. Credentials stay in this page's memory and are cleared when you reload or close Cockpit.</p>
          <div class="am-form-grid">
            <label>Email or username<input name="username" type="text" autocomplete="username" required maxlength="320"></label>
            <label>Password<input name="password" type="password" autocomplete="current-password" required maxlength="4096"></label>
          </div>
          <button type="submit" class="am-button am-button--primary">Connect</button>
        </form>`;
    } else {
      const resourceOptions = integrationResources[provider].map(([value, label]) => `<option value="${value}"${current.resource === value ? " selected" : ""}>${label}</option>`).join("");
      const endpoints = provider === "portainer" ? current.endpoints : [];
      const environmentSelector = provider === "portainer" ? `
        <label class="am-resource-select">Environment<select data-integration-endpoint>
          ${endpoints.map((endpoint) => `<option value="${escapeHtml(endpoint.Id)}"${String(endpoint.Id) === String(current.endpointId) ? " selected" : ""}>${escapeHtml(endpoint.Name || `Environment ${endpoint.Id}`)} · ${escapeHtml(endpoint.Status || "unknown")}</option>`).join("")}
        </select></label>` : "";
      panelContent = `<div class="am-resource-toolbar">
        ${environmentSelector}
        <label class="am-resource-select">Resource<select data-integration-resource>${resourceOptions}</select></label>
        <button type="button" class="am-button" data-resource-refresh>${icon("refresh-cw")} Refresh data</button>
        <button type="button" class="am-button am-button--quiet" data-disconnect="${provider}">Disconnect</button>
      </div>
      <div id="integrationResourceContent">${renderIntegrationTable(provider, current)}</div>`;
    }
    host.innerHTML = `<div class="am-infrastructure-grid">
      <aside class="am-integration-sidebar" aria-label="Connected services">${serviceCards}
        <div class="am-integration-safety">${icon("shield-check")} <span>API credentials are session-only. Destructive actions ask for confirmation.</span></div>
      </aside>
      <section class="am-surface am-integration-workspace">
        <div class="am-integration-workspace__heading"><div><p class="am-integration-eyebrow">SERVICE CONSOLE</p><h3>${serviceName}</h3><span class="am-field-hint">${escapeHtml(configured || "")}</span></div>
          ${provider === "portainer" ? '<a class="am-button am-button--quiet" href="/manage-docker/" target="_blank" rel="noopener noreferrer">Open Portainer</a>' : ""}
          <span class="am-status-pill" data-status="${isConnected ? "running" : configured ? "stopped" : "unknown"}">${isConnected ? "Connected for this session" : configured ? "Needs connection" : "Not configured"}</span>
        </div>${panelContent}
      </section>
    </div>`;
    renderIcons();
  }

  async function loadIntegrationResource() {
    const { provider } = state.integration;
    const connection = state.integration[provider];
    const extra = provider === "portainer" ? { endpoint_id: connection.endpointId } : {};
    const result = await integrationCall(provider, "list", connection.resource, extra);
    connection.data = result.data;
  }

  async function connectIntegration(form) {
    const provider = form.dataset.integrationConnect;
    const connection = state.integration[provider];
    try {
      if (provider === "portainer") {
        connection.token = form.elements.token.value;
        const result = await integrationCall(provider, "list", "endpoints");
        connection.endpoints = integrationRows(result.data);
        if (!connection.endpoints.length) throw new Error("Portainer returned no environments for this account.");
        connection.endpointId = String(connection.endpoints[0].Id);
        connection.resource = "containers";
      } else {
        connection.username = form.elements.username.value;
        connection.password = form.elements.password.value;
        connection.resource = "proxy-hosts";
      }
      await loadIntegrationResource();
    } catch (error) {
      connection.token = "";
      connection.username = "";
      connection.password = "";
      connection.data = null;
      throw error;
    }
    form.reset();
    renderInfrastructure();
  }

  async function runIntegrationAction(button) {
    const { provider } = state.integration;
    const connection = state.integration[provider];
    const action = button.dataset.resourceAction;
    const id = button.dataset.id;
    if (["remove", "delete", "kill"].includes(action) &&
      !window.confirm(action === "kill"
        ? "Force-stop this container immediately? Unsaved application work may be lost."
        : `Permanently ${action === "delete" ? "delete" : "remove"} this ${connection.resource} resource? This cannot be undone.`)) return;
    if (action === "inspect" || action === "logs") {
      const result = await integrationCall(provider, action, connection.resource, {
        endpoint_id: connection.endpointId, id,
      });
      openDialog(`${action === "inspect" ? "Container details" : "Container logs"}: ${id}`,
        `<pre class="am-log-output">${escapeHtml(typeof result.data === "string" ? result.data : JSON.stringify(result.data, null, 2) || "No data returned")}</pre>`);
      return;
    }
    await integrationCall(provider, action, connection.resource, {
      endpoint_id: connection.endpointId, id,
    });
    await loadIntegrationResource();
  }

  function npmRoutePayload(form) {
    const domainNames = Array.from(new Set(form.elements.domain_names.value
      .split(",").map((domain) => domain.trim()).filter(Boolean)));
    if (!domainNames.length) throw new Error("Enter at least one domain name.");
    const forwardHost = form.elements.forward_host.value.trim();
    if (!forwardHost) throw new Error("Enter a host that NPM can reach.");
    const forwardPort = Number(form.elements.forward_port.value);
    if (!Number.isInteger(forwardPort) || forwardPort < 1 || forwardPort > 65535) {
      throw new Error("Forward port must be a whole number from 1 to 65535.");
    }
    return {
      domain_names: domainNames,
      forward_scheme: form.elements.forward_scheme.value,
      forward_host: forwardHost,
      forward_port: forwardPort,
      access_list_id: 0,
      certificate_id: 0,
      ssl_forced: false,
      caching_enabled: false,
      block_exploits: form.elements.block_exploits.checked,
      advanced_config: "",
      meta: { letsencrypt_agree: false, dns_challenge: false },
      allow_websocket_upgrade: form.elements.allow_websocket_upgrade.checked,
      http2_support: false,
      hsts_enabled: false,
      hsts_subdomains: false,
      enabled: true,
    };
  }

  function bindInfrastructure() {
    const host = byId("integrationPanel");
    host.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("button") : null;
      if (!target || state.busy) return;
      if (target.dataset.integrationProvider) {
        state.integration.provider = target.dataset.integrationProvider;
        renderInfrastructure();
      } else if (target.hasAttribute("data-go-settings")) {
        byId("tab-settings").click();
      } else if (target.dataset.disconnect) {
        const connection = state.integration[target.dataset.disconnect];
        connection.token = "";
        connection.username = "";
        connection.password = "";
        connection.data = null;
        renderInfrastructure();
      } else if (target.hasAttribute("data-resource-refresh")) {
        void runOperation(loadIntegrationResource, "Resource list refreshed.", false).then(renderInfrastructure);
      } else if (target.dataset.resourceAction) {
        void runOperation(() => runIntegrationAction(target), "Infrastructure resource updated.", false).then(renderInfrastructure);
      }
    });
    host.addEventListener("change", (event) => {
      if (state.busy) return;
      const target = event.target;
      const provider = state.integration.provider;
      const connection = state.integration[provider];
      if (target.matches("[data-route-app]")) {
        connection.routeAppId = target.value;
        const selected = target.selectedOptions[0];
        target.form.elements.forward_port.value = selected?.dataset.forwardPort || "";
        return;
      }
      if (target.matches("[data-integration-endpoint]")) connection.endpointId = target.value;
      else if (target.matches("[data-integration-resource]")) connection.resource = target.value;
      else return;
      void runOperation(loadIntegrationResource, "", false).then(renderInfrastructure);
    });
    host.addEventListener("submit", (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || state.busy) return;
      event.preventDefault();
      const route = form.matches("[data-npm-route]");
      if (form.matches("[data-integration-connect]")) {
        void runOperation(() => connectIntegration(form), "Connected to the service.", false).then(renderInfrastructure);
        return;
      }
      const create = form.matches("[data-npm-create]");
      const update = form.matches("[data-npm-update]");
      if (route || create || update) {
        void runOperation(async () => {
          let body;
          if (route) {
            body = npmRoutePayload(form);
          } else {
            try { body = JSON.parse(form.elements.body.value); } catch (error) {
              throw new Error(`Resource data must be valid JSON: ${error.message}`);
            }
          }
          if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Resource data must be a JSON object.");
          const connection = state.integration.npm;
          await integrationCall("npm", route || create ? "create" : "update",
            route ? "proxy-hosts" : connection.resource, {
            ...(update ? { id: form.dataset.id } : {}), body,
          });
          await loadIntegrationResource();
          if (route) connection.routeAppId = "";
        }, route ? "Proxy route created in NPM." : create ? "Resource created." : "Resource updated.", false).then(renderInfrastructure);
      }
    });
  }

  function renderSettings(data) {
    const config = data.config || {};
    state.settings = config;
    const forms = Array.from(document.querySelectorAll("#settingsForm, #integrationSettingsForm"));
    if (forms.some((form) => Array.from(form.elements).some((input) => input.name && input.value !== input.defaultValue))) return;
    const serviceAddress = (value = "") => value.startsWith(`${window.location.origin}/`)
      ? value.slice(window.location.origin.length) : value;
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
      <section class="am-surface am-integration-settings">
        <div class="am-surface-heading">
          <div>
            <h3>Infrastructure APIs</h3>
            <p>Portainer API calls go directly to the local service, not through the browser's Cloudflare route. API keys and passwords are requested when you connect and are kept in memory for this Cockpit page.</p>
          </div>
        </div>
        <form id="integrationSettingsForm" class="am-settings-form">
          <div class="am-form-grid">
            <label for="settingPortainerUrl">Portainer backend API URL<input id="settingPortainerUrl" name="portainer_url" type="url" inputmode="url" placeholder="http://127.0.0.1:9000" aria-describedby="portainerApiHint" value="${escapeHtml(config.integrations?.portainer_url || "")}"></label>
            <label for="settingNpmUrl">Nginx Proxy Manager route or full URL<input id="settingNpmUrl" name="npm_url" type="text" inputmode="url" placeholder="/nginx/" aria-describedby="integrationAddressHint" value="${escapeHtml(serviceAddress(config.integrations?.npm_url))}"></label>
          </div>
          <p id="portainerApiHint" class="am-field-hint">For Portainer on this Ubuntu host, use http://127.0.0.1:9000 with no /manage-docker, /portainer, or /api suffix. Open Portainer uses /manage-docker/ in your browser; the backend uses the local API address.</p>
          <p id="integrationAddressHint" class="am-field-hint">Nginx Proxy Manager routes starting with / use <strong>${escapeHtml(window.location.origin)}</strong>, including its port. The route must allow backend API access without browser login cookies. A full HTTP(S) API URL reachable from the host also works. Do not include /api; API paths are added automatically. Leave blank to disconnect a service.</p>
          <p class="am-field-hint">Use the address reachable from this Ubuntu host. HTTPS certificates are verified and redirects are not followed. HTTP does not encrypt credentials; use it only on a trusted local network.</p>
          <div class="am-form-actions"><button type="submit" class="am-button am-button--primary">Save API URLs</button></div>
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
    byId("integrationSettingsForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const integrations = Object.fromEntries(new FormData(form).entries());
      void runOperation(async () => {
        for (const key of Object.keys(integrations)) {
          const value = integrations[key].trim();
          integrations[key] = key === "npm_url" && value.startsWith("/") && !value.startsWith("//")
            ? `${window.location.origin}${value}` : value;
        }
        const previous = state.settings.integrations || {};
        const result = await callBackend("settings", [JSON.stringify({ integrations })]);
        state.settings = result.config || state.settings;
        if (previous.portainer_url !== state.settings.integrations?.portainer_url) {
          state.integration.portainer.token = "";
          state.integration.portainer.data = null;
        }
        if (previous.npm_url !== state.settings.integrations?.npm_url) {
          state.integration.npm.username = "";
          state.integration.npm.password = "";
          state.integration.npm.data = null;
        }
        Array.from(form.elements).forEach((input) => { if (input.name) input.defaultValue = input.value; });
        renderInfrastructure();
      }, "Infrastructure API URLs saved.");
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
      renderInfrastructure();
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
          payload.build_config.compose_file = compose_file.trim();
          payload.build_config.compose_service = compose_service.trim();
          payload.build_config.container_port = Number(container_port);
        } else {
          payload.build_config.dockerfile = dockerfile.trim();
          payload.build_config.container_port = Number(container_port);
        }
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
    const dockerModeInput = form.elements.namedItem("docker_mode");
    const dockerSetupFields = byId("dockerSetupFields");
    const dockerfileInput = form.elements.namedItem("dockerfile");
    const composeFields = byId("composeSetupFields");
    const composeFileInput = form.elements.namedItem("compose_file");
    const composeServiceInput = form.elements.namedItem("compose_service");
    const containerPortInput = form.elements.namedItem("container_port");
    form.elements.namedItem("branch").value = state.settings.default_branch || "main";
    const updateDockerFields = () => {
      const isDocker = frameworkInput.value === "docker";
      const isCompose = isDocker && dockerModeInput.value === "compose";
      dockerSetupFields.hidden = !isDocker;
      dockerModeInput.disabled = !isDocker;
      dockerfileInput.disabled = !isDocker || isCompose;
      composeFields.hidden = !isCompose;
      composeFileInput.disabled = !isCompose;
      composeServiceInput.disabled = !isCompose;
      composeServiceInput.required = isCompose;
      containerPortInput.disabled = !isDocker;
    };
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
          const review = [
            ["Application", setup.payload.name], ["Repository", setup.payload.repo_url], ["Branch", setup.payload.branch],
            ["Framework", frameworkName(setup.payload.framework)], ["Application path", setup.payload.route],
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
      if (routeInput.dataset.autogenerated === "true") routeInput.value = `/${slugInput.value}/`;
    });
    slugInput.addEventListener("input", () => {
      slugInput.dataset.autogenerated = "false";
      if (routeInput.dataset.autogenerated === "true") routeInput.value = `/${slugInput.value}/`;
    });
    routeInput.addEventListener("input", () => { routeInput.dataset.autogenerated = "false"; });
    frameworkInput.addEventListener("change", updateDockerFields);
    dockerModeInput.addEventListener("change", updateDockerFields);
    form.elements.namedItem("repo_url").addEventListener("change", (event) => {
      if (!nameInput.value) {
        nameInput.value = event.target.value.split(/[/:]/).pop().replace(/\.git$/, "").replace(/[-_]/g, " ");
        nameInput.dispatchEvent(new Event("input"));
      }
    });
    updateDockerFields();
    renderIcons();
  }

  function editApplication(app) {
    const frameworks = ["auto", "nextjs", "go", "node", "python", "static", "docker"];
    const options = frameworks.map((framework) => `
      <option value="${framework}"${app.framework === framework ? " selected" : ""}>${frameworkName(framework)}</option>
    `).join("");
    const body = `
      <div class="am-form-grid">
        <label>Name<input name="name" value="${escapeHtml(app.name)}" required></label>
        <label>Git repository URL<input name="repo_url" value="${escapeHtml(app.repo_url)}" required></label>
        <label>Branch<input name="branch" value="${escapeHtml(app.branch)}" required></label>
        <label>Application path prefix<input name="route" value="${escapeHtml(app.route || `/${app.id}/`)}" required autocomplete="off"></label>
        <label>Description<textarea name="description" rows="3">${escapeHtml(app.description || "")}</textarea></label>
        <label>Deployment policy<select name="deployment_mode"><option value="automatic"${app.deployment_mode !== "approval" ? " selected" : ""}>Automatic</option><option value="approval"${app.deployment_mode === "approval" ? " selected" : ""}>Approval required</option></select></label>
        <label class="am-checkbox"><input type="checkbox" name="auto_deploy"${app.auto_deploy !== false ? " checked" : ""}> Automatic deployment</label>
      </div>
      <small class="am-field-hint">App Manager only configures the application path. Configure domains and routing through NPM in Infrastructure; Next.js receives APP_BASE_PATH during build.</small>
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
      </div></details>
      <small class="am-field-hint">Configure authentication or access restrictions through NPM in Infrastructure.</small>
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
          upload_limit_mb: Number(payload.upload_limit_mb),
          access_control: app.python_services?.access_control || "blocked",
          ...(command.length ? { worker: { command, stop_timeout_seconds: Number(payload.worker_timeout), resource_limits: {
            memory_mb: Number(payload.worker_memory_mb), cpu_percent: Number(payload.worker_cpu_percent), tasks_max: Number(payload.worker_tasks_max),
          } } } : {}),
        };
      }
      for (const key of ["native_python", "native_redis", "native_drum", "worker_command", "worker_timeout", "worker_memory_mb", "worker_cpu_percent", "worker_tasks_max", "model_files", "upload_limit_mb"]) delete payload[key];
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
    const route = app?.route || `/${appId}/`;
    openDialog(
      app?.name || appId,
      `<section class="am-deployment-summary" aria-label="Deployment details">
         <p id="deploymentLiveStatus" class="am-deployment-status" data-status="running" role="status" aria-live="polite">Starting deployment…</p>
         <dl class="am-deployment-facts">
           <div><dt>Branch</dt><dd>${icon("git-branch")}<span>${escapeHtml(branch)}</span></dd></div>
           <div><dt>Application path</dt><dd><span>${escapeHtml(route)}</span></dd></div>
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
  }

  document.querySelector(".am-app-manager").addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-open-add]") : null;
    if (!target || state.busy) return;
    openAddApplicationDialog();
  });
  byId("refreshButton").addEventListener("click", () => {
    void runOperation(refreshData, "Application data refreshed.", false);
  });
  byId("refreshInfrastructure").addEventListener("click", () => {
    const connection = state.integration[state.integration.provider];
    if (!connection.data) {
      renderInfrastructure();
      return;
    }
    void runOperation(loadIntegrationResource, "Resource list refreshed.", false).then(renderInfrastructure);
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
      if (button.dataset.action === "route") {
        state.integration.provider = "npm";
        state.integration.npm.resource = "proxy-hosts";
        state.integration.npm.routeAppId = button.dataset.id;
        byId("tab-infrastructure").click();
        renderInfrastructure();
        return;
      }
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
  bindInfrastructure();
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
