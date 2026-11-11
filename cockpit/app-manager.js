// App Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const state = {
    apps: [],
    deployments: [],
    busy: false,
    deploymentMonitor: null,
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
    const cockpitApi = globalThis.cockpit;
    if (!cockpitApi || typeof cockpitApi.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Ensure ../base1/cockpit.js loads before app-manager.js.");
    }
    const output = await cockpitApi.spawn([BACKEND, command, ...args], { superuser: "require" });
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

  async function callBackendInput(command, args, input) {
    const cockpitApi = globalThis.cockpit;
    if (!cockpitApi || typeof cockpitApi.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Ensure ../base1/cockpit.js loads before app-manager.js.");
    }
    const process = cockpitApi.spawn([BACKEND, command, ...args], { superuser: "require" });
    process.input(input);
    process.close();
    const output = await process;
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
        control.disabled = busy;
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
          <button type="button" class="am-button am-button--primary" data-open-add>Add your first application</button>
        </div>
      `;
      return;
    }

    const cards = state.apps.map((app) => {
      const safeUrl = getSafePublicUrl(app.public_url);
      const routeIsActive = Boolean(app.hosting_active && app.active_route);
      const openControl = safeUrl && routeIsActive
        ? `<a class="am-button am-app-link" href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">Open application</a>`
        : `<span class="am-field-hint">Application route is not active yet</span>`;
      const serviceControl = app.status === "running"
        ? `<button type="button" class="am-button" data-action="stop" data-id="${escapeHtml(app.id)}" title="Stop ${escapeHtml(app.name)}">Stop</button>`
        : `<button type="button" class="am-button" data-action="start" data-id="${escapeHtml(app.id)}" title="Start ${escapeHtml(app.name)}">Start</button>`;
      const routeStatus = routeIsActive && app.active_route === app.route
        ? "Active"
        : routeIsActive
          ? `Change pending (currently live: ${app.active_route})`
          : "Deploy to activate";
      const appInitial = String(app.name || "A").trim().charAt(0).toUpperCase() || "A";
      const deployControl = app.status === "deploying"
        ? `<button type="button" class="am-button am-button--primary" data-action="deployment-log" data-id="${escapeHtml(app.id)}">View deployment</button>`
        : `<button type="button" class="am-button am-button--primary" data-action="deploy" data-id="${escapeHtml(app.id)}">Deploy</button>`;
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
            <div><dt>Public URL</dt><dd>${safeUrl && routeIsActive ? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(app.public_url)}</a>` : escapeHtml(app.public_url || "Not available")}</dd></div>
            <div><dt>Hosted route</dt><dd>${escapeHtml(app.route || `/${app.id}/`)} · ${escapeHtml(routeStatus)}</dd></div>
          </dl>
          <div class="am-app-actions" aria-label="Actions for ${escapeHtml(app.name)}">
            ${openControl}
            ${deployControl}
            <button type="button" class="am-button" data-action="pull" data-id="${escapeHtml(app.id)}">Pull</button>
            <button type="button" class="am-button" data-action="edit" data-id="${escapeHtml(app.id)}">Edit</button>
            <button type="button" class="am-button" data-action="health" data-id="${escapeHtml(app.id)}">Health</button>
            <button type="button" class="am-button" data-action="environment" data-id="${escapeHtml(app.id)}">Environment</button>
            ${serviceControl}
            <button type="button" class="am-button" data-action="restart" data-id="${escapeHtml(app.id)}">Restart</button>
            <button type="button" class="am-button" data-action="logs" data-id="${escapeHtml(app.id)}">Logs</button>
            <button type="button" class="am-button am-button--danger" data-action="remove" data-id="${escapeHtml(app.id)}">Remove</button>
          </div>
          ${routeIsActive ? "" : `<p class="am-field-hint" role="note">Deploy will add the managed route include to the matching HTTPS site, validate Nginx, and reload it. If the site is ambiguous or invalid, the deployment reports the error and restores its configuration.</p>`}
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
      resumeActiveDeployment();
    } catch (error) {
      updateConnection("error", "Could not load data");
      throw error;
    }
  }

  function openDialog(title, body, submitLabel, onSubmit, onSuccess) {
    const dialog = byId("appManagerDialog");
    byId("appManagerError").classList.add("hidden");
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
        if (typeof onSuccess === "function") onSuccess();
      } catch (error) {
        showError(error);
        submitButton.disabled = false;
      }
    });
    dialog.querySelectorAll("[data-dialog-close]").forEach((button) => {
      button.addEventListener("click", () => dialog.close());
    });
    dialog.showModal();
    const firstInput = dialog.querySelector("input, select, textarea");
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
    openDialog("Add an application", body, "Add application", async (form) => {
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.build_config = parseBuildConfig(payload.build_config || "");
      return runOperation(async () => {
        await callBackend("add", [JSON.stringify(payload)]);
      }, "Application added. Select Deploy on its card to build and start it.");
    }, () => byId("tab-websites").click());

    const form = byId("appManagerDialog").querySelector("form");
    const nameInput = form.elements.namedItem("name");
    const slugInput = form.elements.namedItem("slug");
    const routeInput = form.elements.namedItem("route");
    slugInput.dataset.autogenerated = "true";
    routeInput.dataset.autogenerated = "true";
    nameInput.addEventListener("input", () => {
      if (slugInput.dataset.autogenerated === "true") {
        slugInput.value = suggestSlug(nameInput.value);
      }
      if (routeInput.dataset.autogenerated === "true") {
        routeInput.value = `/${slugInput.value}/`;
      }
    });
    slugInput.addEventListener("input", () => {
      slugInput.dataset.autogenerated = "false";
      if (routeInput.dataset.autogenerated === "true") {
        routeInput.value = `/${slugInput.value}/`;
      }
    });
    routeInput.addEventListener("input", () => {
      routeInput.dataset.autogenerated = "false";
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
        <label>Hosted route<input name="route" value="${escapeHtml(app.route || `/${app.id}/`)}" required autocomplete="off"></label>
        <label>Port<input type="number" name="port" min="8100" max="8999" value="${escapeHtml(app.port)}" required></label>
        <label>Health-check URL<input name="health_check_url" value="${escapeHtml(app.health_check_url || "/")}" required></label>
        <label>Description<textarea name="description" rows="3">${escapeHtml(app.description || "")}</textarea></label>
        <label>Build configuration (JSON object)<textarea name="build_config" rows="5">${escapeHtml(JSON.stringify(app.build_config || {}, null, 2))}</textarea></label>
      </div>
      <small class="am-field-hint">A route change is applied by the next deployment. Next.js projects must build with the same basePath (APP_BASE_PATH is supplied during build).</small>
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
        <label>Value<input type="text" data-env-value value="${escapeHtml(value)}" autocomplete="off" spellcheck="false" placeholder="Value"></label>
        <button type="button" class="am-button am-button--danger" data-env-remove aria-label="Remove environment variable ${escapeHtml(key || "row")}">Remove</button>
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

  async function manageEnvironment(app) {
    const result = await callBackend("env-get", [app.id]);
    const body = `
      <p class="am-dialog-intro">Manage private runtime variables for ${escapeHtml(app.name)}. Values are stored outside the source checkout.</p>
      <div class="am-env-tools">
        <label class="am-button" for="environmentImport">Import .env or text file</label>
        <input id="environmentImport" class="am-visually-hidden" type="file" accept=".env,.env.*,.txt,text/plain">
        <button type="button" class="am-button" data-env-add>Add variable</button>
        <button type="button" class="am-button" data-env-export>Export .env</button>
      </div>
      <div class="am-env-row am-env-row--heading" aria-hidden="true"><span>Key</span><span>Value</span><span></span></div>
      <div id="environmentRows" class="am-env-rows">${environmentMarkup(parseEnvironmentContent(result.content || ""))}</div>
      <small class="am-field-hint">Import files with one KEY=value assignment per line. Values are applied when the application next starts or deploys. Maximum size: 64 KiB.</small>
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
            `${response.key_count} environment ${response.key_count === 1 ? "variable" : "variables"} saved. Restart or deploy the application to apply changes.`,
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
    };
    dialog.querySelector("[data-env-add]").addEventListener("click", () => addRow());
    container.addEventListener("click", (event) => {
      const removeButton = event.target instanceof Element ? event.target.closest("[data-env-remove]") : null;
      if (removeButton) removeButton.closest(".am-env-row").remove();
    });
    importInput.addEventListener("change", async () => {
      const file = importInput.files && importInput.files[0];
      if (!file) return;
      if (file.size > 64 * 1024) {
        showError(new Error("The selected environment file is larger than 64 KiB."));
        importInput.value = "";
        return;
      }
      try {
        container.innerHTML = environmentMarkup(parseEnvironmentContent(await file.text()));
      } catch (error) {
        showError(new Error(`Could not import the selected file: ${error.message}`));
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

  function renderDeploymentLog(result) {
    const output = byId("deploymentLiveLog");
    const status = byId("deploymentLiveStatus");
    if (!output || !status) return;
    const text = result.logs || "Waiting for deployment output…";
    output.innerHTML = text.split("\n").map((line) => {
      const level = /^\[stderr\]/i.test(line) || /\b(error|failed|failure|aborted)\b/i.test(line)
        ? "error"
        : /\b(success|completed|deployed)\b/i.test(line)
          ? "success"
          : /^\[stdout\]/i.test(line)
            ? "output"
            : "info";
      return `<span class="am-log-line am-log-line--${level}">${escapeHtml(line)}</span>`;
    }).join("\n");
    output.scrollTop = output.scrollHeight;
    if (result.active) {
      status.textContent = result.abortable === false
        ? "Finalizing deployment"
        : "Deployment in progress";
    } else {
      const finalStatuses = {
        deployed: "Deployment complete",
        failed: "Deployment failed",
        aborted: "Deployment aborted",
      };
      status.textContent = finalStatuses[result.status] || "Preparing deployment";
    }
    const abortButton = byId("abortDeploymentButton");
    if (abortButton) {
      abortButton.hidden = !result.active || result.abortable === false;
      abortButton.disabled = result.aborting === true;
      abortButton.textContent = result.aborting ? "Aborting…" : "Abort deployment";
      abortButton.dataset.deploymentId = result.deployment_id || "";
      abortButton.dataset.appId = result.app_id || "";
    }
  }

  function openDeploymentLogDialog(appId) {
    openDialog(
      "Live deployment log",
      `<p id="deploymentLiveStatus" class="am-dialog-intro" role="status" aria-live="polite">Starting deployment…</p>
       <div class="am-deployment-log-actions"><button id="abortDeploymentButton" type="button" class="am-button am-button--danger" data-action="abort-deployment" hidden>Abort deployment</button></div>
       <pre id="deploymentLiveLog" class="am-log-output" aria-live="polite">Waiting for deployment output…</pre>`,
    );
  }

  async function monitorDeployment(appId, deploymentId) {
    if (state.deploymentMonitor?.deploymentId === deploymentId) return state.deploymentMonitor.promise;
    const monitor = { appId, deploymentId, promise: null };
    state.deploymentMonitor = monitor;
    monitor.promise = (async () => {
      while (state.deploymentMonitor === monitor) {
        try {
          const result = await callBackend("deployment-logs", [appId, deploymentId]);
          renderDeploymentLog(result);
          if (!result.active) break;
        } catch (error) {
          const output = byId("deploymentLiveLog");
          if (output) {
            output.textContent += `\nLive log polling failed: ${error.message}`;
            output.scrollTop = output.scrollHeight;
          }
        }
        await new Promise((resolve) => window.setTimeout(resolve, 750));
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
    if (state.deploymentMonitor || byId("appManagerDialog").open) return;
    const active = state.deployments.find((deployment) => deployment.status === "deploying");
    if (!active) return;
    openDeploymentLogDialog(active.app_id);
    void monitorDeployment(active.app_id, active.id);
  }

  function showPluginUpdate(result) {
    const latest = String(result.latest_commit || "");
    const body = `
      <p class="am-dialog-intro">A newer App Manager revision is available. The update will be downloaded from the project's fixed HTTPS GitHub repository and reinstalled.</p>
      <dl class="am-settings-paths">
        <div><dt>Installed</dt><dd>${escapeHtml(result.current_commit || "Unknown")}</dd></div>
        <div><dt>Available</dt><dd>${escapeHtml(latest || "Unknown")}</dd></div>
      </dl>
      <p class="am-field-hint">The plugin files will be replaced. Application source, releases, and host configuration are not changed.</p>
    `;
    openDialog("App Manager update available", body, "Pull and reinstall", async () => {
      setBusy(true);
      try {
        const update = await callBackend("plugin-update");
        showNotice(update.updated
          ? `App Manager updated to ${String(update.commit || "").slice(0, 12)}. Reload Cockpit to use the new plugin files.`
          : "App Manager is already up to date.");
        return true;
      } finally {
        setBusy(false);
      }
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
    if (action === "deployment-log") {
      try {
        await viewDeploymentLog(appId, deploymentId);
      } catch (error) {
        showError(error);
      }
      return;
    }
    if (action === "environment") {
      void manageEnvironment(app).catch(showError);
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
    if (action === "deploy") {
      try {
        await deployWithLiveLog(appId);
      } catch (error) {
        showError(error);
      }
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
    const target = event.target instanceof Element ? event.target.closest("[data-open-add]") : null;
    if (!target || state.busy) return;
    openAddApplicationDialog();
  });
  byId("refreshButton").addEventListener("click", () => {
    void runOperation(refreshData, "Application data refreshed.", false);
  });
  byId("checkUpdateButton").addEventListener("click", checkPluginUpdates);
  byId("appsTableContainer").addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("button[data-action]") : null;
    if (button) void handleAppAction(button.dataset.action, button.dataset.id, button.dataset.deploymentId).catch(showError);
  });
  byId("deploymentHistory").addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest('button[data-action="deployment-log"]') : null;
    if (button) void viewDeploymentLog(button.dataset.id, button.dataset.deploymentId).catch(showError);
  });
  byId("appManagerDialog").addEventListener("click", (event) => {
    const abortButton = event.target instanceof Element ? event.target.closest('[data-action="abort-deployment"]') : null;
    if (abortButton) {
      abortButton.disabled = true;
      abortButton.textContent = "Aborting…";
      void callBackend("deployment-abort", [
        abortButton.dataset.appId,
        abortButton.dataset.deploymentId,
      ]).catch((error) => {
        abortButton.disabled = false;
        abortButton.textContent = "Abort deployment";
        showError(error);
      });
    }
    if (event.target === event.currentTarget) event.currentTarget.close();
  });
  bindTabs();
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
