// Docker Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const root = document.querySelector(".docker-shell");
  const state = {
    apps: [],
    deployments: [],
    engine: {},
    busy: false,
    query: "",
    statusFilter: "all",
    currentDeployment: null,
    deploymentMonitor: null,
    pendingCreate: null,
  };
  const byId = (id) => document.getElementById(id);
  const iconPaths = {
    box: '<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="m4.3 7.6 7.7 4.3 7.7-4.3M12 12v9"/>',
    play: '<path d="m8 5 11 7-11 7V5Z"/>',
    pause: '<path d="M8 5h3v14H8zM15 5h3v14h-3z"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
    upload: '<path d="M12 16V4m0 0L7 9m5-5 5 5"/><path d="M5 14v5h14v-5"/>',
    trash: '<path d="M4 7h16M10 11v6m4-6v6M6 7l1 14h10l1-14M9 7V4h6v3"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
  };
  const icon = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${iconPaths[name] || iconPaths.box}</svg>`;
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);

  async function callBackend(command, args = [], input) {
    if (!globalThis.cockpit || typeof globalThis.cockpit.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Reload Docker Manager inside Cockpit.");
    }
    const process = globalThis.cockpit.spawn([BACKEND, command, ...args], { superuser: "require" });
    let output = "";
    if (typeof process.stream === "function") process.stream((chunk) => { output += chunk; });
    if (input !== undefined) process.input(input);
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
      byId("connectionStatus").dataset.state = "error";
      byId("connectionStatus").querySelector(".connection-label").textContent = "Needs attention";
    }
  }

  function showNotice(message) {
    const notice = byId("globalNotice");
    notice.hidden = false;
    notice.textContent = message;
    byId("globalError").hidden = true;
  }

  function setBusy(busy) {
    state.busy = busy;
    root.setAttribute("aria-busy", String(busy));
    byId("refreshButton").disabled = busy;
    document.querySelectorAll("[data-open-create], #installButton").forEach((button) => { button.disabled = busy; });
    if (busy) {
      byId("connectionStatus").dataset.state = "busy";
      byId("connectionStatus").querySelector(".connection-label").textContent = "Working…";
    } else if (byId("connectionStatus").dataset.state === "busy") {
      byId("connectionStatus").dataset.state = "ready";
      byId("connectionStatus").querySelector(".connection-label").textContent = "Connected";
    }
  }

  async function run(operation, successMessage = "", refresh = true) {
    if (state.busy) return;
    setBusy(true);
    byId("globalError").hidden = true;
    byId("globalNotice").hidden = true;
    try {
      await operation();
      if (refresh) await refreshData();
      if (successMessage) showNotice(successMessage);
    } catch (failure) {
      showError(failure);
    } finally {
      setBusy(false);
    }
  }

  function observedStatus(app) {
    if (app.status === "deploying") return "deploying";
    if (app.health?.service_state === "active" && app.health.local_healthy) return "running";
    if (app.health?.service_state === "inactive" && app.status !== "failed") return "stopped";
    return app.status || "unknown";
  }

  function safePublicUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
    } catch (_error) {
      return null;
    }
  }

  function renderEngine() {
    const engine = state.engine;
    const daemonActive = Boolean(engine.daemon_active);
    const stateLabel = !engine.installed ? "Not installed" : daemonActive ? "Running" : "Stopped";
    const stateName = !engine.installed ? "missing" : daemonActive ? "active" : "inactive";
    const status = byId("engineState");
    status.textContent = stateLabel;
    status.dataset.state = stateName;
    const badge = byId("engineBadge");
    badge.textContent = stateLabel;
    badge.dataset.status = stateName;
    const details = [
      ["Engine version", engine.engine_version || (engine.installed ? "Version unavailable" : "Not installed")],
      ["Docker daemon", daemonActive ? "Active" : "Inactive"],
      ["Compose", engine.compose_available ? (engine.compose_version || "Installed") : "Not installed"],
    ];
    byId("engineDetails").innerHTML = details.map(([label, value]) => `
      <div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>
    `).join("");
  }

  function renderSummary() {
    const total = state.apps.length;
    const running = state.apps.filter((app) => observedStatus(app) === "running").length;
    const deploying = state.apps.filter((app) => observedStatus(app) === "deploying").length;
    const attention = state.apps.filter((app) => observedStatus(app) === "failed").length;
    const cards = [
      ["Containers", total, "box"],
      ["Running", running, "play"],
      ["Deploying", deploying, "upload"],
      ["Needs attention", attention, "refresh"],
    ];
    byId("summaryCards").innerHTML = cards.map(([label, value, symbol]) => `
      <article class="summary-card">
        <div class="summary-card-top"><span>${label}</span><span class="summary-icon">${icon(symbol)}</span></div>
        <strong>${value}</strong>
      </article>
    `).join("");
    byId("containerCount").textContent = String(total);
  }

  function renderApps() {
    const matches = state.apps.filter((app) => {
      const currentStatus = observedStatus(app);
      const statusMatches = state.statusFilter === "all" ||
        (state.statusFilter === "failed" ? currentStatus === "failed" : currentStatus === state.statusFilter);
      const queryMatches = [app.name, app.repo_url, app.id, app.route]
        .join(" ").toLowerCase().includes(state.query.toLowerCase());
      return statusMatches && queryMatches;
    });
    byId("containerSummary").textContent = `${matches.length} of ${state.apps.length} ${state.apps.length === 1 ? "application" : "applications"}`;
    if (state.apps.length === 0) {
      byId("containerList").innerHTML = `
        <div class="empty-state">
          ${icon("box")}
          <h3>No Docker applications yet</h3>
          <p>Connect a Git repository and deploy your first container.</p>
          <button type="button" class="button button-primary" data-open-create>${icon("plus")} Deploy container</button>
        </div>`;
      return;
    }
    if (!matches.length) {
      byId("containerList").innerHTML = `
        <div class="empty-state">
          ${icon("box")}
          <h3>No matching containers</h3>
          <p>Try a different search or status filter.</p>
        </div>`;
      return;
    }
    byId("containerList").innerHTML = matches.map((app) => {
      const status = observedStatus(app);
      const busy = status === "deploying";
      const deployment = app.deployments?.at(-1);
      const isCompose = app.build_config?.docker_mode === "compose";
      const runtimePort = app.port ? `127.0.0.1:${app.port}` : "Not assigned";
      const url = safePublicUrl(app.public_url);
      const isActive = Boolean(url && app.hosting_active && app.active_route === app.route);
      const route = isActive
        ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${icon("globe")}${escapeHtml(app.active_route)}</a>`
        : `<span>${icon("globe")}${escapeHtml(app.route || "No route assigned")}</span>`;
      const serviceControl = status === "running"
        ? `<button type="button" class="button" data-action="stop" data-id="${escapeHtml(app.id)}"${busy ? " disabled" : ""}>${icon("pause")} Stop</button>`
        : `<button type="button" class="button" data-action="start" data-id="${escapeHtml(app.id)}"${busy || !app.last_deploy ? " disabled" : ""}>${icon("play")} Start</button>`;
      const deployControl = busy
        ? `<button type="button" class="button button-primary" data-action="view-deployment" data-id="${escapeHtml(app.id)}">${icon("upload")} View progress</button>`
        : `<button type="button" class="button button-primary" data-action="deploy" data-id="${escapeHtml(app.id)}">${icon("upload")} ${app.last_deploy ? "Redeploy" : "Deploy"}</button>`;
      return `
        <article class="container-card" data-status="${escapeHtml(status)}">
          <div class="container-card-heading">
            <span class="container-icon">${icon("box")}</span>
            <div class="container-title">
              <h3 title="${escapeHtml(app.name)}">${escapeHtml(app.name)}</h3>
              <p title="${escapeHtml(app.repo_url)}">${escapeHtml(app.repo_url)}</p>
            </div>
            <span class="status-badge" data-status="${escapeHtml(status)}">${escapeHtml(status)}</span>
          </div>
          <div class="container-route">${route}</div>
          <dl class="container-meta">
            <div><dt>Host port</dt><dd>${escapeHtml(runtimePort)}</dd></div>
            <div><dt>Deployment</dt><dd>${isCompose ? "Compose" : "Dockerfile"}</dd></div>
            <div><dt>Last release</dt><dd>${escapeHtml(deployment?.commit?.slice(0, 7) || "Not deployed")}</dd></div>
          </dl>
          <div class="container-actions">
            ${deployControl}
            ${serviceControl}
            <span class="action-spacer"></span>
            <button type="button" class="button" data-action="logs" data-id="${escapeHtml(app.id)}" aria-label="View logs for ${escapeHtml(app.name)}">${icon("terminal")} Logs</button>
            <button type="button" class="button button-danger" data-action="remove" data-id="${escapeHtml(app.id)}" aria-label="Remove ${escapeHtml(app.name)}">${icon("trash")}</button>
          </div>
        </article>`;
    }).join("");
  }

  function renderDeployments() {
    const active = state.deployments.find((deployment) => deployment.status === "deploying");
    byId("containerList").querySelectorAll('[data-action="deploy"]').forEach((button) => {
      if (active && button.dataset.id === active.app_id) {
        button.dataset.action = "view-deployment";
        button.innerHTML = `${icon("upload")} View progress`;
      }
    });
    if (active && !state.deploymentMonitor) {
      state.currentDeployment = { appId: active.app_id, deploymentId: active.id };
      void monitorDeployment(active.app_id, active.id);
    }
  }

  async function refreshData() {
    const [apps, deployments, engine] = await Promise.all([
      callBackend("list", ["--framework", "docker"]),
      callBackend("deployments", ["--framework", "docker"]),
      callBackend("docker-status"),
    ]);
    state.apps = apps.apps || [];
    state.deployments = deployments.deployments || [];
    state.engine = engine;
    renderEngine();
    renderSummary();
    renderApps();
    renderDeployments();
    byId("connectionStatus").dataset.state = "ready";
    byId("connectionStatus").querySelector(".connection-label").textContent = "Connected";
  }

  async function startDeployment(appId) {
    const result = await callBackend("deployment-start", [appId]);
    if (!result.deployment_id) throw new Error("The backend did not return a deployment ID.");
    state.currentDeployment = { appId, deploymentId: result.deployment_id };
    byId("deploymentTitle").textContent = `Deploying ${state.apps.find((app) => app.id === appId)?.name || appId}`;
    byId("deploymentStatus").textContent = "Deployment queued";
    byId("deploymentOutput").textContent = "Deployment queued";
    byId("abortDeploymentButton").hidden = false;
    byId("deploymentDialog").showModal();
    await refreshData();
    if (!state.deploymentMonitor) void monitorDeployment(appId, result.deployment_id);
  }

  async function monitorDeployment(appId, deploymentId) {
    if (state.deploymentMonitor) return;
    const monitor = `${appId}:${deploymentId}`;
    state.deploymentMonitor = monitor;
    try {
      while (state.deploymentMonitor === monitor) {
        const result = await callBackend("deployment-logs", [appId, deploymentId]);
        byId("deploymentStatus").textContent = result.active
          ? `Building · ${result.build_status || "starting"}`
          : result.status === "deployed" ? "Deployment complete" : `Deployment ${result.status || "finished"}`;
        byId("deploymentOutput").textContent = result.logs || "Waiting for deployment output…";
        byId("deploymentOutput").scrollTop = byId("deploymentOutput").scrollHeight;
        if (!result.active) {
          state.deploymentMonitor = null;
          await refreshData();
          if (result.status === "deployed") showNotice("Container deployment completed successfully.");
          else if (result.status === "aborted") showNotice("Container deployment was canceled.");
          else showError(new Error("Container deployment failed. Review the deployment output for details."));
          byId("abortDeploymentButton").hidden = true;
          return;
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
    } catch (failure) {
      state.deploymentMonitor = null;
      showError(failure);
    }
  }

  function openCreateDialog() {
    const form = byId("dockerAppForm");
    form.reset();
    form.elements.branch.value = "main";
    form.elements.dockerfile.value = "Dockerfile";
    form.elements.compose_file.value = "compose.yaml";
    form.elements.compose_service.value = "web";
    form.elements.container_port.value = "3000";
    form.elements.slug.dataset.manual = "false";
    form.elements.route.dataset.manual = "false";
    byId("formError").hidden = true;
    state.pendingCreate = null;
    form.querySelectorAll("input, select, textarea").forEach((field) => { field.disabled = false; });
    byId("createButton").textContent = "Deploy application";
    updateBuildFields(form);
    byId("appDialog").showModal();
    form.elements.repo_url.focus();
  }

  function updateBuildFields(form) {
    const compose = form.elements.docker_mode.value === "compose";
    form.querySelector("[data-dockerfile-field]").hidden = compose;
    form.querySelector("[data-compose-file-field]").hidden = !compose;
    form.querySelector("[data-compose-service-field]").hidden = !compose;
    form.elements.dockerfile.required = !compose;
    form.elements.compose_file.required = compose;
    form.elements.compose_service.required = compose;
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
        if (quote === '"') value = value.replace(/\\(["\\$`])/g, "$1");
      } else {
        value = value.replace(/\s+#.*$/, "").trim();
      }
      rows.push({ key, value });
    }
    return rows;
  }

  function serializeEnvironmentRows(rows) {
    return rows.map(({ key, value }) => {
      const escaped = value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\$/g, "\\$").replace(/`/g, "\\`");
      return `${key}="${escaped}"`;
    }).join("\n") + (rows.length ? "\n" : "");
  }

  function collectEnvironmentContent(content) {
    const rows = parseEnvironmentContent(content);
    const reserved = new Set(["HOME", "PATH", "PORT", "HOST", "NODE_ENV", "APP_BASE_PATH", "APP_CONFIG_DIR", "APP_DATA_DIR", "APP_MODELS_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "NUMBA_CACHE_DIR", "QT_QPA_PLATFORM"]);
    const blocked = rows.find((row) => reserved.has(row.key) || row.key.startsWith("APP_STORAGE_"));
    if (blocked) throw new Error(`${blocked.key} is managed automatically and cannot be overridden.`);
    const serialized = serializeEnvironmentRows(rows);
    if (new TextEncoder().encode(serialized).length > 64 * 1024) throw new Error("Environment variables exceed 64 KiB.");
    return serialized;
  }

  function collectCreateForm(form) {
    const values = Object.fromEntries(new FormData(form).entries());
    const content = collectEnvironmentContent(values.environment);
    const repo = values.repo_url.trim();
    if (!/^(https:\/\/[^\s@]+\/|ssh:\/\/[^\s]+\/|git@[^\s:]+:)[^\s]+$/.test(repo)) {
      throw new Error("Enter an HTTPS or SSH Git repository URL without embedded credentials.");
    }
    const id = values.slug.trim();
    const route = values.route.trim();
    if (state.apps.some((app) => app.id !== state.pendingCreate?.id && app.id === id)) {
      throw new Error("That container ID is already registered.");
    }
    if (state.apps.some((app) => app.id !== state.pendingCreate?.id && [app.route, app.active_route].includes(route))) {
      throw new Error("That hosted route is already assigned.");
    }
    const buildConfig = {
      docker_mode: values.docker_mode,
      container_port: Number(values.container_port),
      app_directory: values.app_directory.trim(),
    };
    if (values.docker_mode === "compose") {
      buildConfig.compose_file = values.compose_file.trim();
      buildConfig.compose_service = values.compose_service.trim();
    } else {
      buildConfig.dockerfile = values.dockerfile.trim();
    }
    if (!buildConfig.app_directory) delete buildConfig.app_directory;
    return {
      payload: {
        name: values.name.trim(),
        repo_url: repo,
        branch: values.branch.trim(),
        slug: id,
        route,
        framework: "docker",
        health_check_url: values.health_check_url.trim(),
        description: values.description.trim(),
        build_config: buildConfig,
      },
      content,
      deploy: form.elements.deploy_now.checked,
    };
  }

  async function submitCreateForm(event) {
    event.preventDefault();
    if (state.busy) return;
    const form = event.currentTarget;
    const formError = byId("formError");
    formError.hidden = true;
    try {
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }
      setBusy(true);
      const setup = state.pendingCreate
        ? { content: collectEnvironmentContent(form.elements.environment.value), deploy: form.elements.deploy_now.checked }
        : collectCreateForm(form);
      let appId;
      if (!state.pendingCreate) {
        const created = await callBackend("add", [JSON.stringify(setup.payload)]);
        if (!created.app?.id) throw new Error("The backend did not return the registered application.");
        appId = created.app.id;
        state.pendingCreate = { id: appId, payload: setup.payload };
        form.querySelectorAll("input, select, textarea").forEach((field) => {
          if (!["environment", "deploy_now"].includes(field.name)) field.disabled = true;
        });
        byId("createButton").textContent = "Save and continue";
      } else {
        appId = state.pendingCreate.id;
      }
      if (setup.content) await callBackend("env-save", [appId], setup.content);
      const shouldDeploy = setup.deploy;
      state.pendingCreate = null;
      byId("appDialog").close();
      await refreshData();
      if (shouldDeploy) {
        await startDeployment(appId);
      } else {
        showNotice("Docker application registered.");
      }
    } catch (failure) {
      showError(failure, formError);
    } finally {
      setBusy(false);
    }
  }

  async function handleAppAction(action, appId) {
    const app = state.apps.find((item) => item.id === appId);
    if (!app) throw new Error("Container application is no longer available. Refresh and try again.");
    if (action === "deploy") {
      await startDeployment(appId);
    } else if (action === "view-deployment") {
      const deployment = app.deployments?.at(-1);
      if (!deployment?.id) throw new Error("No deployment progress is available.");
      state.currentDeployment = { appId, deploymentId: deployment.id };
      byId("deploymentTitle").textContent = `Deploying ${app.name}`;
      byId("deploymentDialog").showModal();
      void monitorDeployment(appId, deployment.id);
    } else if (action === "logs") {
      const result = await callBackend("logs", [appId, "--lines", "200"]);
      byId("logsTitle").textContent = `${app.name} logs`;
      byId("logsOutput").textContent = result.logs || result.message || "No application logs are available.";
      byId("logsDialog").showModal();
    } else if (action === "remove") {
      if (!window.confirm(`Permanently remove "${app.name}" and its managed source, releases, storage, and configuration? This cannot be undone.`)) return;
      await callBackend("remove", [appId]);
      await refreshData();
      showNotice(`${app.name} was removed.`);
    } else {
      if (["stop", "restart"].includes(action) && !window.confirm(`${action === "stop" ? "Stop" : "Restart"} "${app.name}"?`)) return;
      await callBackend(action, [appId]);
      await refreshData();
      showNotice(`${app.name} ${action === "start" ? "started" : action === "stop" ? "stopped" : "restarted"}.`);
    }
  }

  function bindEvents() {
    document.querySelectorAll("[data-page]").forEach((button) => {
      button.addEventListener("click", () => {
        const page = button.dataset.page;
        document.querySelectorAll("[data-page]").forEach((item) => {
          item.classList.toggle("active", item === button);
          if (item === button) item.setAttribute("aria-current", "page");
          else item.removeAttribute("aria-current");
        });
        byId("containersPage").hidden = page !== "containers";
        byId("enginePage").hidden = page !== "engine";
      });
    });
    root.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-open-create]") : null;
      if (target && !state.busy) openCreateDialog();
    });
    byId("refreshButton").addEventListener("click", () => { void run(refreshData, "Docker data refreshed.", false); });
    byId("installButton").addEventListener("click", () => {
      if (state.busy || !window.confirm("Install or update Docker Engine and Compose using Ubuntu APT packages?")) return;
      void run(async () => {
        const result = await callBackend("docker-install");
        state.engine = result;
      }, "Docker Engine and Compose are installed and running.");
    });
    byId("containerSearch").addEventListener("input", (event) => {
      state.query = event.target.value;
      renderApps();
    });
    byId("statusFilter").addEventListener("change", (event) => {
      state.statusFilter = event.target.value;
      renderApps();
    });
    byId("containerList").addEventListener("click", (event) => {
      const button = event.target instanceof Element ? event.target.closest("button[data-action]") : null;
      if (button && !button.disabled && !state.busy) {
        void run(() => handleAppAction(button.dataset.action, button.dataset.id), "", false);
      }
    });
    byId("dockerAppForm").addEventListener("submit", (event) => { void submitCreateForm(event); });
    byId("dockerAppForm").elements.docker_mode.addEventListener("change", (event) => updateBuildFields(event.currentTarget.form));
    byId("dockerAppForm").elements.name.addEventListener("input", updateSlugAndRoute);
    byId("dockerAppForm").elements.slug.addEventListener("input", updateRouteFromSlug);
    byId("dockerAppForm").elements.route.addEventListener("input", updateRoutePreview);
    byId("dockerAppForm").elements.repo_url.addEventListener("change", (event) => {
      const form = event.currentTarget.form;
      if (!form.elements.name.value) {
        form.elements.name.value = event.target.value.split(/[/:]/).pop().replace(/\.git$/, "").replace(/[-_]/g, " ");
        updateSlugAndRoute({ currentTarget: form.elements.name });
      }
    });
    byId("environmentImport").addEventListener("change", async (event) => {
      const file = event.currentTarget.files?.[0];
      if (!file) return;
      try {
        if (file.size > 64 * 1024) throw new Error("The selected environment file is larger than 64 KiB.");
        const rows = parseEnvironmentContent(await file.text());
        byId("dockerAppForm").elements.environment.value = serializeEnvironmentRows(rows);
      } catch (failure) {
        showError(new Error(`Could not import the selected file: ${failure.message}`), byId("formError"));
      } finally {
        event.currentTarget.value = "";
      }
    });
    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => button.closest("dialog").close());
    });
    byId("abortDeploymentButton").addEventListener("click", () => {
      const target = state.currentDeployment;
      if (!target || state.busy || !window.confirm("Cancel this Docker deployment?")) return;
      void run(() => callBackend("deployment-abort", [target.appId, target.deploymentId]), "Deployment cancellation requested.", false);
    });
    byId("appDialog").addEventListener("close", () => {
      if (state.pendingCreate) {
        state.pendingCreate = null;
        void refreshData().catch(showError);
      }
    });
  }

  function updateSlugAndRoute(event) {
    const form = byId("dockerAppForm");
    const slug = form.elements.slug;
    const route = form.elements.route;
    if (slug.dataset.manual !== "true") {
      slug.value = event.currentTarget.value.trim().toLowerCase()
        .replace(/[^a-z0-9-]+/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 24).replace(/-$/g, "") || "app";
    }
    if (route.dataset.manual !== "true") route.value = `/${slug.value}/`;
    updateRoutePreview();
  }

  function updateRouteFromSlug(event) {
    const form = byId("dockerAppForm");
    event.currentTarget.dataset.manual = "true";
    if (form.elements.route.dataset.manual !== "true") form.elements.route.value = `/${event.currentTarget.value}/`;
    updateRoutePreview();
  }

  function updateRoutePreview() {
    byId("routePreview").textContent = `https://stuserver.online${byId("dockerAppForm").elements.route.value || "/"}`;
  }

  bindEvents();
  void refreshData().catch(showError);
})();
