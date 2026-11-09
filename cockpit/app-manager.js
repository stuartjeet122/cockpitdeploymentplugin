// App Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const state = {
    apps: [],
    deployments: [],
    busy: false,
  };
  const byId = (id) => document.getElementById(id);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[character]);

  async function callBackend(command, args = []) {
    const output = await cockpit.spawn([BACKEND, command, ...args], { superuser: "require" });
    let result;
    try {
      result = JSON.parse(output);
    } catch (error) {
      throw new Error(`Backend returned invalid JSON: ${error.message}`);
    }
    if (!result || typeof result !== "object" || result.ok === false) {
      throw new Error(result?.error || "The backend returned an invalid response.");
    }
    return result;
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
    byId("appManagerError").textContent = message;
    byId("appManagerError").classList.remove("hidden");
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
    document.querySelectorAll(".am-app-manager button, .am-app-manager input, .am-app-manager select, .am-app-manager textarea")
      .forEach((control) => {
        control.disabled = busy;
      });
    const refreshButton = byId("refreshButton");
    refreshButton.querySelector("span").textContent = busy ? "Working…" : "Refresh";
    refreshButton.setAttribute("aria-label", busy ? "App Manager is working" : "Refresh application data");
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
    return `<span class="am-status-pill" data-status="${escapeHtml(value)}">${escapeHtml(value)}</span>`;
  }

  function renderOverview(data) {
    const cards = [
      ["Total applications", data.total_apps],
      ["Running", data.running_apps],
      ["Stopped", data.stopped_apps],
      ["Failed deployments", data.failed_deployments],
      ["Available ports", Array.isArray(data.available_ports) ? data.available_ports.length : 0],
    ];
    byId("overviewCards").innerHTML = cards.map(([label, value]) => `
      <div class="am-metric">
        <span class="am-metric-label">${escapeHtml(label)}</span>
        <strong class="am-metric-value">${escapeHtml(value)}</strong>
      </div>
    `).join("");

    const activity = (data.recent_activity || []).map((item) => `
      <div class="am-activity-item">
        <span class="am-activity-icon" aria-hidden="true">↗</span>
        <div>
          <strong class="am-activity-title">${escapeHtml(item.name || "Application")}</strong>
          <span class="am-activity-description">${escapeHtml(item.action || "Updated")}</span>
        </div>
        <time class="am-activity-time">${escapeHtml(item.when || "Recently")}</time>
      </div>
    `).join("");
    byId("recentActivity").innerHTML = activity || `
      <div class="am-empty-state">
        <span class="am-empty-state__icon" aria-hidden="true">↗</span>
        <h3>Nothing to report yet</h3>
        <p>Your recent application activity will appear here.</p>
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
    if (!state.apps.length) {
      byId("appsTableContainer").innerHTML = `
        <div class="am-empty-state">
          <span class="am-empty-state__icon" aria-hidden="true">+</span>
          <h3>No applications yet</h3>
          <p>Connect a Git repository to create your first managed application.</p>
          <button type="button" class="am-button am-button--primary" data-tab-target="add-app">Add your first application</button>
        </div>
      `;
      return;
    }

    const cards = state.apps.map((app) => {
      const safeUrl = getSafePublicUrl(app.public_url);
      const openControl = safeUrl
        ? `<a class="am-button am-app-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">Open application</a>`
        : "";
      const serviceControl = app.status === "running"
        ? `<button type="button" class="am-button" data-action="stop" data-id="${escapeHtml(app.id)}" title="Stop ${escapeHtml(app.name)}">Stop</button>`
        : `<button type="button" class="am-button" data-action="start" data-id="${escapeHtml(app.id)}" title="Start ${escapeHtml(app.name)}">Start</button>`;
      const appInitial = String(app.name || "A").trim().charAt(0).toUpperCase() || "A";
      return `
        <article class="am-app-card">
          <header class="am-app-card__heading">
            <span class="am-app-avatar" aria-hidden="true">${escapeHtml(appInitial)}</span>
            <div class="am-app-title-wrap">
              <h3>${escapeHtml(app.name)}</h3>
              <span class="am-app-framework">${escapeHtml(app.framework || "Framework not detected")}</span>
            </div>
            ${renderStatus(app.status)}
          </header>
          <dl class="am-app-meta">
            <div><dt>Repository</dt><dd title="${escapeHtml(app.repo_url)}">${escapeHtml(app.repo_url || "Not provided")}</dd></div>
            <div><dt>Branch</dt><dd>${escapeHtml(app.branch || "main")}</dd></div>
            <div><dt>Port</dt><dd>${escapeHtml(app.port || "—")}</dd></div>
            <div><dt>Last deployed</dt><dd>${escapeHtml(app.last_deploy || "Not yet deployed")}</dd></div>
            <div><dt>Latest commit</dt><dd title="${escapeHtml(app.latest_commit || "No commit yet")}">${escapeHtml(app.latest_commit || "—")}</dd></div>
            <div><dt>Public URL</dt><dd>${safeUrl ? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(app.public_url)}</a>` : escapeHtml(app.public_url || "Not available")}</dd></div>
          </dl>
          <div class="am-app-actions" aria-label="Actions for ${escapeHtml(app.name)}">
            ${openControl}
            <button type="button" class="am-button am-button--primary" data-action="deploy" data-id="${escapeHtml(app.id)}">Deploy</button>
            <button type="button" class="am-button" data-action="pull" data-id="${escapeHtml(app.id)}">Pull</button>
            <button type="button" class="am-button" data-action="edit" data-id="${escapeHtml(app.id)}">Edit</button>
            <button type="button" class="am-button" data-action="health" data-id="${escapeHtml(app.id)}">Health</button>
            ${serviceControl}
            <button type="button" class="am-button" data-action="restart" data-id="${escapeHtml(app.id)}">Restart</button>
            <button type="button" class="am-button" data-action="logs" data-id="${escapeHtml(app.id)}">Logs</button>
            <button type="button" class="am-button am-button--danger" data-action="remove" data-id="${escapeHtml(app.id)}">Remove</button>
          </div>
        </article>
      `;
    }).join("");

    byId("appsTableContainer").innerHTML = cards;
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
      const canRollback = deployment.status === "deployed" && deployment.release_available !== false;
      const rollbackControl = canRollback
        ? `<button type="button" class="am-button" data-action="rollback" data-id="${escapeHtml(deployment.app_id)}" data-release="${escapeHtml(deployment.id)}">Rollback</button>`
        : "";
      return `
        <tr>
          <td>${escapeHtml(deployment.name)}</td>
          <td>${escapeHtml(deployment.date)}</td>
          <td>${escapeHtml(deployment.commit || "n/a")}</td>
          <td>${escapeHtml(deployment.build_status || "pending")}</td>
          <td>${escapeHtml(deployment.status || "pending")}</td>
          <td>${escapeHtml(deployment.error || "—")}</td>
          <td>${rollbackControl}</td>
        </tr>
      `;
    }).join("");
    byId("deploymentHistory").innerHTML = `
      <div class="am-deployment-table-wrap">
      <table class="table">
        <thead><tr>
          <th>Name</th><th>Date</th><th>Commit</th><th>Build status</th>
          <th>Deployment status</th><th>Error</th><th>Actions</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      </div>
    `;
  }

  function renderSettings(data) {
    const config = data.config || {};
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
              <label for="settingRetention">Release retention</label>
              <input id="settingRetention" type="number" min="0" max="100" step="1" name="release_retention" value="${escapeHtml(config.release_retention ?? 5)}" required>
              <small class="am-field-hint">Number of previous releases to keep.</small>
            </div>
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
        () => callBackend("settings", [JSON.stringify(payload)]),
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
      renderOverview(overview);
      renderApps(apps);
      renderDeployments(deployments);
      renderSettings(settings);
      updateConnection("connected", `${state.apps.length} ${state.apps.length === 1 ? "application" : "applications"}`);
    } catch (error) {
      updateConnection("error", "Could not load data");
      throw error;
    }
  }

  function openDialog(title, body, submitLabel, onSubmit) {
    const dialog = byId("appManagerDialog");
    const submit = submitLabel
      ? `<button type="submit" class="pf-m-primary">${escapeHtml(submitLabel)}</button>`
      : "";
    dialog.innerHTML = `
      <form class="am-dialog-form">
        <header class="am-dialog-header">
          <h2 id="appManagerDialogTitle">${escapeHtml(title)}</h2>
          <button type="button" class="am-dialog-close" aria-label="Close" data-dialog-close>×</button>
        </header>
        <div class="am-dialog-body">${body}</div>
        <footer class="am-dialog-footer">
          <button type="button" data-dialog-close>Cancel</button>${submit}
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
      } catch (error) {
        showError(error);
        submitButton.disabled = false;
      }
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => dialog.close());
    });
    dialog.showModal();
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

  function bindAddForm() {
    byId("addAppForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      try {
        payload.build_config = parseBuildConfig(payload.build_config || "");
      } catch (error) {
        showError(error);
        return;
      }
      void runOperation(async () => {
        await callBackend("add", [JSON.stringify(payload)]);
        form.reset();
      }, "Application registered. Deploy it from the Websites tab to build and start it.");
    });
  }

  function editApplication(app) {
    const frameworks = ["auto", "nextjs", "go", "node", "python", "static"];
    const options = frameworks.map((framework) => `
      <option value="${framework}"${app.framework === framework ? " selected" : ""}>${framework === "auto" ? "Auto detect" : escapeHtml(framework)}</option>
    `).join("");
    const body = `
      <div class="am-form-grid">
        <label>Name<input name="name" value="${escapeHtml(app.name)}" required></label>
        <label>Git repository URL<input name="repo_url" value="${escapeHtml(app.repo_url)}" required></label>
        <label>Branch<input name="branch" value="${escapeHtml(app.branch)}" required></label>
        <label>Framework<select name="framework">${options}</select></label>
        <label>Port<input type="number" name="port" min="8100" max="8999" value="${escapeHtml(app.port)}" required></label>
        <label>Health-check URL<input name="health_check_url" value="${escapeHtml(app.health_check_url || "/")}" required></label>
        <label>Description<textarea name="description" rows="3">${escapeHtml(app.description || "")}</textarea></label>
        <label>Build configuration (JSON object)<textarea name="build_config" rows="5">${escapeHtml(JSON.stringify(app.build_config || {}, null, 2))}</textarea></label>
      </div>
    `;
    openDialog("Edit application", body, "Save changes", async (form) => {
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.port = Number(payload.port);
      payload.build_config = parseBuildConfig(payload.build_config || "");
      return runOperation(
        () => callBackend("edit", [app.id, JSON.stringify(payload)]),
        "Application updated.",
      );
    });
  }

  function showLogs(appId) {
    void runOperation(async () => {
      const result = await callBackend("logs", [appId, "--lines", "200"]);
      openDialog(`Logs: ${appId}`, `<pre class="am-log-output">${escapeHtml(result.logs || result.message || "No logs available.")}</pre>`);
    }, "Logs loaded.", false);
  }

  async function handleAppAction(action, appId) {
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
    if (action === "remove") {
      if (!window.confirm(`Remove "${app.name}" from App Manager? Its source and deployed files will be retained.`)) return;
      await runOperation(
        () => callBackend("remove", [appId]),
        "Application removed from the registry. Source and deployed files were retained.",
      );
      return;
    }
    if (action === "health") {
      await runOperation(async () => {
        const result = await callBackend("health", [appId]);
        showNotice(result.healthy ? `Health check passed (${result.url}).` : `Health check failed (${result.url}).`);
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
    if (confirmations[action] && !window.confirm(confirmations[action])) return;
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

  function handleRollback(button) {
    const appId = button.dataset.id;
    const releaseId = button.dataset.release;
    const deployment = state.deployments.find((item) => item.app_id === appId && item.id === releaseId);
    if (!deployment) {
      showError(new Error("That release is no longer available. Refresh and try again."));
      return;
    }
    if (!window.confirm(`Roll back to release ${releaseId} for "${deployment.name}"?`)) return;
    void runOperation(
      () => callBackend("rollback", [appId, releaseId]),
      `Rolled back "${deployment.name}" to ${releaseId}.`,
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
      tab.addEventListener("click", () => activate(tab));
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
    const target = event.target instanceof Element ? event.target.closest("[data-tab-target]") : null;
    if (!target || state.busy) return;
    const tab = Array.from(document.querySelectorAll('.am-app-manager [role="tab"][data-tab]'))
      .find((item) => item.dataset.tab === target.dataset.tabTarget);
    if (tab) tab.click();
  });
  byId("refreshButton").addEventListener("click", () => {
    void runOperation(refreshData, "Application data refreshed.", false);
  });
  byId("appsTableContainer").addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("button[data-action]") : null;
    if (button) void handleAppAction(button.dataset.action, button.dataset.id).catch(showError);
  });
  byId("deploymentHistory").addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest('button[data-action="rollback"]') : null;
    if (button) handleRollback(button);
  });
  byId("appManagerDialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  bindTabs();
  bindAddForm();
  void runOperation(refreshData, "", false);
})();
