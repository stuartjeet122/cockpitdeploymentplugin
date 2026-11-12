const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "cockpit/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "cockpit/app-manager.js"), "utf8");
const fixtureApps = [
  { id: "portal", name: "Customer Portal", framework: "nextjs", branch: "main", route: "/portal/", active_route: "/portal/", hosting_active: true, public_url: "https://stuserver.online/portal/", repo_url: "https://github.com/example/portal.git", port: 8100, status: "running", last_deploy: "2026-11-11T09:00:00Z", deployments: [] },
  { id: "docs", name: "Documentation", framework: "static", branch: "main", route: "/docs/", hosting_active: false, repo_url: "https://github.com/example/docs.git", port: 8101, status: "stopped", deployments: [] },
];

async function loadUi(overrides = {}, configureWindow) {
  const dom = new JSDOM(html, { runScripts: "outside-only", url: "https://localhost/cockpit/app-manager/index.html" });
  const { window } = dom;
  const calls = [];
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new window.Event("close")); };
  window.confirm = () => true;
  window.TextEncoder = TextEncoder;
  window.cockpit = {
    spawn(args) {
      const [, command, ...values] = args;
      const record = { command, values };
      calls.push(record);
      const defaults = {
        overview: { total_apps: fixtureApps.length, running_apps: 1, stopped_apps: 1, failed_deployments: 0 },
        list: { apps: fixtureApps }, deployments: { deployments: [] }, settings: { config: {} },
        "env-get": { content: "" }, "env-save": { key_count: 1 },
      };
      const handler = overrides[command];
      let stream;
      const promise = Promise.resolve().then(() => typeof handler === "function" ? handler(record) : handler || defaults[command] || {})
        .then((result) => {
          const output = JSON.stringify({ ok: true, ...result });
          if (stream) { stream(output); return ""; }
          return output;
        });
      promise.stream = (callback) => { stream = callback; return promise; };
      promise.input = (input) => { record.input = input; return promise; };
      promise.close = () => { throw new Error("Do not terminate the Cockpit process after sending stdin"); };
      return promise;
    },
  };
  configureWindow?.(window);
  window.eval(script);
  await settle(window);
  return { dom, window, document: window.document, calls };
}

async function settle(window) {
  for (let turn = 0; turn < 4; turn += 1) await new Promise((resolve) => window.setTimeout(resolve, 0));
}

test("applications are the initial view and only active routes have public links", async () => {
  const ui = await loadUi();
  try {
    assert.equal(ui.document.querySelector('[data-tab="websites"]').getAttribute("aria-selected"), "true");
    assert.equal(ui.document.querySelectorAll(".am-app-card").length, 2);
    assert.equal(ui.document.querySelectorAll('.am-app-route a[href="https://stuserver.online/portal/"]').length, 1);
    assert.match(ui.document.querySelector("#appsTableContainer").textContent, /Deploy to activate/);
    assert.equal(ui.document.querySelector('[data-action="start"][data-id="docs"]').disabled, true);
  } finally { ui.dom.window.close(); }
});

test("application editing submits automatic deployment policy and numeric resource limits", async () => {
  const ui = await loadUi();
  try {
    ui.document.querySelector('[data-action="edit"][data-id="portal"]').click();
    const form = ui.document.querySelector(".am-dialog-form");
    assert.equal(form.querySelectorAll("details:not([open])").length, 3);
    assert.equal(form.elements.auto_deploy.closest("label").className, "am-checkbox");
    form.elements.port.value = "8080";
    form.checkValidity();
    assert.equal(form.elements.port.closest("details").open, true);
    form.elements.port.value = "8100";
    form.elements.auto_deploy.checked = false;
    form.elements.window_start.value = "23:00";
    form.elements.window_end.value = "02:00";
    form.elements.limit_memory_mb.value = "512";
    form.elements.storage_directories.value = "data, uploads";
    form.elements.required_environment.value = "DATABASE_URL, API_KEY";
    form.elements.runtime_requirements.value = '{"node":"20.9"}';
    form.elements.deployment_mode.value = "approval";
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    const payload = JSON.parse(ui.calls.find((record) => record.command === "edit").values[1]);
    assert.equal(payload.auto_deploy, false);
    assert.equal(payload.resource_limits.memory_mb, 512);
    assert.deepEqual(payload.deployment_window, { start: "23:00", end: "02:00" });
    assert.equal(payload.window_start, undefined);
    assert.deepEqual(payload.storage_directories, ["data", "uploads"]);
    assert.deepEqual(payload.required_environment, ["DATABASE_URL", "API_KEY"]);
    assert.deepEqual(payload.runtime_requirements, { node: "20.9" });
    assert.equal(payload.deployment_mode, "approval");
  } finally { ui.dom.window.close(); }
});

test("health observations distinguish service readiness and public failures", async () => {
  const ui = await loadUi({ list: { apps: [{ ...fixtureApps[0], health: {
    checked_at: "2026-11-11T09:00:00Z", service_state: "active", local_healthy: true,
    public_healthy: false, public_error: "Public DNS unavailable",
  }, auto_deploy: false }] } });
  try {
    const card = ui.document.querySelector(".am-app-card");
    assert.match(card.textContent, /ReadinessHealthy/);
    assert.match(card.textContent, /Public routeFailed/);
    assert.match(card.textContent, /Auto-deployPaused/);
    assert.ok(card.querySelector('[title="Public DNS unavailable"]'));
  } finally { ui.dom.window.close(); }
});

test("successful service transitions override older health observations", async () => {
  const app = { ...fixtureApps[0], health: { checked_at: new Date(Date.now() - 1000).toISOString(), service_state: "active", local_healthy: true } };
  const ui = await loadUi({ list: () => ({ apps: [app] }), stop: () => { app.status = "stopped"; app.updated_at = new Date().toISOString(); return { status: "stopped" }; } });
  try {
    ui.document.querySelector('[data-action="stop"]').click();
    await settle(ui.window);
    assert.ok(ui.calls.some((record) => record.command === "stop"));
    assert.equal(ui.document.querySelector('[data-action="stop"]'), null);
    assert.ok(ui.document.querySelector('[data-action="start"]'));
    ui.document.querySelector('[data-filter="running"]').click();
    assert.equal(ui.document.querySelectorAll(".am-app-card").length, 0);
  } finally { ui.dom.window.close(); }
});

test("forbidden public probes are blocked without marking a healthy runtime failed", async () => {
  const ui = await loadUi({ list: { apps: [{ ...fixtureApps[0], health: {
    checked_at: new Date().toISOString(), service_state: "active", local_healthy: true,
    public_healthy: false, public_status: 403, public_error: "Cloudflare returned HTTP 403",
  } }] } });
  try {
    const card = ui.document.querySelector(".am-app-card");
    assert.equal(card.dataset.status, "running");
    assert.match(card.textContent, /Blocked · 403/);
    assert.match(card.textContent, /ReadinessHealthy/);
    assert.equal(card.querySelector(".am-runtime-details").open, false);
  } finally { ui.dom.window.close(); }
});

test("offline bundle renders every requested interface icon", async () => {
  const bundle = fs.readFileSync(path.join(root, "cockpit/icons.js"), "utf8");
  const ui = await loadUi({}, (window) => window.eval(bundle));
  try {
    const names = new Set([...script.matchAll(/icon\("([a-z-]+)"\)/g)].map((match) => match[1]));
    for (const name of names) {
      const element = ui.document.createElement("i");
      element.dataset.lucide = name;
      ui.document.body.append(element);
    }
    ui.window.renderAppManagerIcons();
    assert.equal(ui.document.querySelectorAll("i[data-lucide]").length, 0);
    assert.ok(ui.document.querySelectorAll("svg.lucide").length > 20);
  } finally { ui.dom.window.close(); }
});

test("preflight and metrics render structured backend observations", async () => {
  const ui = await loadUi({ preflight: { passed: false, checks: [{ name: "node", passed: false, message: "minimum 20.9" }], errors: ["Missing API_KEY"] }, metrics: { metrics: { memory_bytes: 1048576, cpu_percent: 5, restarts: 2, checked_at: "2026-11-11T09:00:00Z" } } });
  try {
    ui.document.querySelector('[data-action="preflight"][data-id="portal"]').click();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /Missing API_KEY/);
    ui.document.querySelector('[data-action="metrics"][data-id="portal"]').click();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /1.0 MiB/);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /5%/);
  } finally { ui.dom.window.close(); }
});

test("metrics refresh reloads samples, prevents duplicate clicks, and permits retry after errors", async () => {
  let samples = 0;
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const ui = await loadUi({ metrics: async () => {
    samples += 1;
    if (samples === 2) await pending;
    if (samples === 3) throw new Error("Sample unavailable");
    return { metrics: { cpu_percent: samples } };
  } });
  try {
    ui.document.querySelector('[data-action="metrics"]').click();
    await settle(ui.window);
    const refresh = ui.document.querySelector('#appManagerDialog [data-action="metrics"]');
    refresh.click();
    refresh.click();
    await settle(ui.window);
    assert.equal(samples, 2);
    assert.equal(refresh.disabled, true);
    release();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /2%/);
    ui.document.querySelector('#appManagerDialog [data-action="metrics"]').click();
    await settle(ui.window);
    assert.match(ui.document.querySelector(".am-dialog-error").textContent, /Sample unavailable/);
    ui.document.querySelector('#appManagerDialog [data-action="metrics"]').click();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /4%/);
  } finally { release(); ui.dom.window.close(); }
});

test("approval reviews changes before submitting the exact candidate revision", async () => {
  const commit = "b".repeat(40);
  const ui = await loadUi({ list: { apps: [{ ...fixtureApps[0], deployment_mode: "approval" }] }, "review-update": { pending_update: { commit, base_commit: "a".repeat(40), commits: "bbbbbbb Update", diffstat: "server.js | 2" } }, approve: { deployment_id: "release-approved" }, "deployment-logs": { app_id: "portal", deployment_id: "release-approved", active: false, status: "deployed" } });
  try {
    assert.equal(ui.document.querySelector('[data-action="deploy"]'), null);
    ui.document.querySelector('[data-action="review-update"]').click();
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "approve").length, 0);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /server.js/);
    ui.document.querySelector(".am-dialog-form").dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.deepEqual(ui.calls.find((record) => record.command === "approve").values, ["portal", commit]);
    assert.equal(ui.calls.filter((record) => record.command === "deployment-start").length, 0);
  } finally { ui.dom.window.close(); }
});

test("live logs preserve scroll, skip unchanged redraws, and poll only status when closed", async () => {
  const ticks = [];
  let logs = [
    "2026-11-12T02:48:14Z Build phase: preflight",
    "2026-11-12T02:48:14Z PASS Environment: Required variable names are present",
    "2026-11-12T02:48:14Z Preflight warning: Existing route settings are verified during build",
    "[stdout] Build output",
    "[stderr] Diagnostic output",
    "[stderr] npm warn install-scripts",
    "[stderr] Cannot find module '@swc/core'",
  ].join("\n");
  let pollingFails = false;
  const deployment = { app_id: "portal", id: "live-build", status: "deploying" };
  const ui = await loadUi({
    deployments: { deployments: [deployment] },
    "deployment-logs": (record) => {
      if (pollingFails) throw new Error("Temporary connection failure");
      return {
        app_id: "portal", deployment_id: deployment.id, active: true, status: "deploying",
        ...(record.values.includes("--status-only") ? {} : { logs }),
      };
    },
  }, (window) => {
    const schedule = window.setTimeout.bind(window);
    window.setTimeout = (callback, delay) => {
      if (delay >= 1500) { ticks.push(callback); return 0; }
      return schedule(callback, delay);
    };
  });
  try {
    assert.ok(ui.calls.find((record) => record.command === "deployment-logs").values.includes("--status-only"));
    ui.document.querySelector('[data-action="deployment-log"]').click();
    await settle(ui.window);
    const output = ui.document.querySelector("#deploymentLiveLog");
    assert.ok(output.querySelector(".am-log-line--phase"));
    assert.ok(output.querySelector(".am-log-line--success"));
    assert.ok(output.querySelector(".am-log-line--warning"));
    assert.ok(output.querySelector(".am-log-line--stdout"));
    assert.ok(output.querySelector(".am-log-line--stderr"));
    assert.ok(output.querySelector(".am-log-line--error"));
    assert.equal(output.querySelector(".am-log-time").textContent, "02:48:14");
    assert.ok(ui.document.querySelector(".am-deployment-facts"));
    assert.equal(output.querySelectorAll(".am-log-time").length, output.querySelectorAll(".am-log-message").length);
    assert.equal(output.getAttribute("tabindex"), "0");
    Object.defineProperties(output, { scrollHeight: { value: 1000 }, clientHeight: { value: 200 } });
    output.scrollTop = 300;
    const firstLine = output.firstChild;
    pollingFails = true;
    ticks.shift()();
    await settle(ui.window);
    assert.equal(output.firstChild, firstLine);
    assert.equal(output.scrollTop, 300);
    assert.match(ui.document.querySelector("#deploymentLiveStatus").textContent, /retrying: Temporary connection failure/);
    pollingFails = false;
    ticks.shift()();
    await settle(ui.window);
    assert.equal(output.firstChild, firstLine);
    assert.equal(output.scrollTop, 300);
    assert.equal(ui.document.querySelector("#deploymentLiveStatus").textContent, "Deployment in progress");
    logs += "\nNew output";
    ticks.shift()();
    await settle(ui.window);
    assert.notEqual(output.firstChild, firstLine);
    assert.equal(output.scrollTop, 300);
    output.scrollTop = 800;
    logs += "\nLatest output";
    ticks.shift()();
    await settle(ui.window);
    assert.equal(output.scrollTop, 1000);
    ui.document.querySelector("#appManagerDialog").close();
    ticks.shift()();
    await settle(ui.window);
    assert.ok(ui.calls.filter((record) => record.command === "deployment-logs").at(-1).values.includes("--status-only"));
  } finally { ui.dom.window.close(); }
});

test("an aborting deployment can retry recovery without duplicate requests", async () => {
  const deployment = {
    app_id: "portal", deployment_id: "20261112024814-82d96f71",
    active: true, abortable: true, aborting: true, build_status: "running",
    status: "deploying", logs: "Abort requested",
  };
  let finishAbort;
  const ui = await loadUi({
    deployments: { deployments: [{ app_id: "portal", id: deployment.deployment_id, status: "deploying" }] },
    "deployment-logs": deployment,
    "deployment-abort": () => new Promise((resolve) => { finishAbort = resolve; }),
  });
  try {
    ui.document.querySelector('[data-action="deployment-log"]').click();
    await settle(ui.window);
    const button = ui.document.querySelector("#abortDeploymentButton");
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "Retry abort");
    assert.equal(ui.document.querySelector("#deploymentLiveStatus").textContent, "Cancellation requested");
    button.click();
    button.click();
    await settle(ui.window);
    assert.equal(button.disabled, true);
    assert.equal(ui.calls.filter((record) => record.command === "deployment-abort").length, 1);
    finishAbort({ aborting: true, aborted: false });
    await settle(ui.window);
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "Retry abort");
    button.click();
    await settle(ui.window);
    finishAbort({ aborting: false, aborted: true });
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "deployment-abort").length, 2);
    assert.equal(button.hidden, true);
    assert.equal(ui.document.querySelector("#deploymentLiveStatus").textContent, "Deployment was aborted");
  } finally { ui.dom.window.close(); }
});

test("deployment logs can be copied and downloaded as plain text", async () => {
  const deployment = {
    app_id: "portal",
    deployment_id: "20261112024814-82d96f71",
    commit: "b".repeat(40),
    active: false,
    status: "failed",
    logs: "Build phase: running\nCannot find module '@swc/core'",
  };
  const ui = await loadUi({
    deployments: { deployments: [{ app_id: "portal", id: deployment.deployment_id, status: "failed" }] },
    "deployment-logs": deployment,
  });
  let copied = "";
  let downloaded;
  Object.defineProperty(ui.window.navigator, "clipboard", {
    configurable: true,
    value: { writeText: async (text) => { copied = text; } },
  });
  Object.defineProperty(ui.window.URL, "createObjectURL", {
    configurable: true,
    value: (blob) => { downloaded = { blob, url: "blob:deployment-log" }; return downloaded.url; },
  });
  Object.defineProperty(ui.window.URL, "revokeObjectURL", { configurable: true, value: () => {} });
  let downloadName = "";
  ui.window.HTMLAnchorElement.prototype.click = function () { downloadName = this.download; };
  try {
    ui.document.querySelector('[data-action="deployment-log"]').click();
    await settle(ui.window);
    assert.equal(ui.document.querySelector("#deploymentLogRevision").textContent, "bbbbbbb");
    assert.equal(ui.document.querySelector("#deploymentLogRevision").title, deployment.commit);
    assert.equal(ui.document.querySelector("#deploymentOutputState").textContent, "Finished");
    assert.equal(ui.document.querySelector("#deploymentLogId").textContent, deployment.deployment_id);
    assert.ok(ui.document.querySelector(".am-dialog-footer #abortDeploymentButton"));
    assert.equal(ui.document.querySelector("#copyDeploymentLogButton").getAttribute("aria-label"), "Copy log");
    ui.document.querySelector("#copyDeploymentLogButton").click();
    await settle(ui.window);
    assert.equal(copied, deployment.logs);
    assert.match(ui.document.querySelector("#copyDeploymentLogButton").textContent, /Copied/);
    ui.document.querySelector("#downloadDeploymentLogButton").click();
    assert.equal(downloadName, `deployment-${deployment.app_id}-${deployment.deployment_id}.txt`);
    assert.equal(downloaded.blob.type, "text/plain;charset=utf-8");
    assert.equal(ui.document.querySelector("#appManagerDialog").classList.contains("am-dialog--deployment"), true);
  } finally { ui.dom.window.close(); }
});

test("delayed dialog loads prevent competing edits and duplicate requests", async () => {
  for (const command of ["env-get", "deployment-logs"]) {
    let release;
    const pending = new Promise((resolve) => { release = resolve; });
    const ui = await loadUi({
      [command]: () => pending,
      deployments: { deployments: [{ app_id: "portal", id: "last-build", status: "deployed" }] },
    });
    try {
      const selector = command === "env-get" ? '[data-action="environment"]' : '#deploymentHistory [data-action="deployment-log"]';
      const trigger = ui.document.querySelector(selector);
      trigger.click();
      trigger.click();
      ui.document.querySelector('[data-action="edit"]').click();
      await settle(ui.window);
      assert.equal(ui.calls.filter((record) => record.command === command).length, 1);
      assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
      release({ content: "", app_id: "portal", deployment_id: "last-build", active: false, logs: "Build complete" });
      await settle(ui.window);
      assert.equal(ui.document.querySelector("#appManagerDialog").open, true);
      assert.equal(ui.document.querySelector(".am-app-manager").getAttribute("aria-busy"), "false");
    } finally { release({}); ui.dom.window.close(); }
  }
});

test("pending approval cannot be dismissed or submitted twice", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const ui = await loadUi({
    list: { apps: [{ ...fixtureApps[0], deployment_mode: "approval" }] },
    "review-update": { pending_update: { commit: "b".repeat(40) } },
    approve: () => pending,
    "deployment-logs": { app_id: "portal", deployment_id: "approved", active: false },
  });
  try {
    ui.document.querySelector('[data-action="review-update"]').click();
    await settle(ui.window);
    const dialog = ui.document.querySelector("#appManagerDialog");
    const form = dialog.querySelector("form");
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    dialog.querySelector("[data-dialog-close]").click();
    assert.equal(dialog.dispatchEvent(new ui.window.Event("cancel", { cancelable: true })), false);
    assert.equal(dialog.open, true);
    assert.equal(ui.calls.filter((record) => record.command === "approve").length, 1);
    release({ deployment_id: "approved" });
    await settle(ui.window);
    assert.equal(dialog.querySelector(".am-deployment-eyebrow").textContent, "Deployment");
    assert.match(dialog.querySelector("#appManagerDialogTitle").textContent, /Customer Portal/);
  } finally { release({ deployment_id: "approved" }); ui.dom.window.close(); }
});

test("search, filters, and clear preserve the full application list", async () => {
  const ui = await loadUi();
  try {
    ui.document.querySelector('[data-filter="running"]').click();
    assert.equal(ui.document.querySelectorAll(".am-app-card").length, 1);
    const search = ui.document.querySelector("#appSearch");
    search.value = "missing";
    search.dispatchEvent(new ui.window.Event("input", { bubbles: true }));
    assert.match(ui.document.querySelector("#appsTableContainer").textContent, /No matching applications/);
    ui.document.querySelector("[data-clear-filters]").click();
    assert.equal(ui.document.querySelectorAll(".am-app-card").length, 2);
    assert.equal(search.value, "");
  } finally { ui.dom.window.close(); }
});

test("setup patterns are valid Unicode Sets expressions and reject unsafe paths", async () => {
  const ui = await loadUi();
  try {
    ui.document.querySelector("[data-open-add]").click();
    const form = ui.document.querySelector(".am-dialog-form");
    for (const input of form.querySelectorAll("input[pattern]")) {
      assert.doesNotThrow(() => new RegExp(input.pattern, "v"));
    }
    for (const [value, valid] of [["customer-portal", true], ["a", true], ["../escape", false], ["Uppercase", false], ["trailing-", false]]) {
      form.elements.slug.value = value;
      assert.equal(form.elements.slug.checkValidity(), valid, value);
    }
    for (const [value, valid] of [["/customer-portal/", true], ["/apps/api/", true], ["/bad.name/", false], ["/../escape/", false], ["//", false]]) {
      form.elements.route.value = value;
      assert.equal(form.elements.route.checkValidity(), valid, value);
    }
  } finally { ui.dom.window.close(); }
});

test("environment content is sent on stdin without terminating the backend process", async () => {
  const ui = await loadUi();
  try {
    ui.document.querySelector('[data-action="environment"][data-id="portal"]').click();
    await settle(ui.window);
    ui.document.querySelector("[data-env-add]").click();
    assert.equal(ui.document.querySelector("[data-env-value]").type, "password");
    ui.document.querySelector("[data-env-toggle]").click();
    assert.equal(ui.document.querySelector("[data-env-value]").type, "text");
    ui.document.querySelector("[data-env-toggle]").click();
    assert.equal(ui.document.querySelector("[data-env-value]").type, "password");
    ui.document.querySelector("[data-env-key]").value = "API_URL";
    ui.document.querySelector("[data-env-value]").value = "https://api.example.test";
    ui.document.querySelector(".am-dialog-form").dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(ui.calls.find((record) => record.command === "env-save").input, 'API_URL="https://api.example.test"\n');
    assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
    assert.match(ui.document.querySelector("#appManagerNotice").textContent, /Redeploy for build-time changes; restart for runtime-only changes/);
  } finally { ui.dom.window.close(); }
});

test("setup reviews repository, route, and environment before registering once", async () => {
  let savedApps = [...fixtureApps];
  const ui = await loadUi({
    list: () => ({ apps: savedApps }),
    add: (record) => {
      const payload = JSON.parse(record.values[0]);
      const app = { ...payload, id: payload.slug, status: "stopped", deployments: [] };
      savedApps = [...savedApps, app];
      return { app };
    },
  });
  try {
    ui.document.querySelector("[data-open-add]").click();
    const form = ui.document.querySelector(".am-dialog-form");
    form.elements.repo_url.value = "https://github.com/example/shop.git";
    form.elements.name.value = "Shop";
    form.elements.name.dispatchEvent(new ui.window.Event("input"));
    ui.document.querySelector("#setupNext").click();
    assert.equal(form.querySelector('[data-setup-step="1"]').hidden, false);
    assert.equal(form.elements.route.value, "/shop/");
    form.elements.framework.value = "go";
    form.elements.go_package.value = "./cmd/server";
    form.elements.environment.value = "API_URL=https://api.example.test";
    ui.document.querySelector("#setupNext").click();
    assert.match(ui.document.querySelector("#setupReview").textContent, /API_URL/);
    assert.doesNotMatch(ui.document.querySelector("#setupReview").textContent, /api.example.test/);
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 0);
    form.elements.deploy_now.checked = false;
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 1);
    const payload = JSON.parse(ui.calls.find((record) => record.command === "add").values[0]);
    assert.equal(payload.build_config.go_package, "./cmd/server");
    assert.equal(payload.go_package, undefined);
    assert.equal(ui.calls.find((record) => record.command === "env-save").input, 'API_URL="https://api.example.test"\n');
    assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
  } finally { ui.dom.window.close(); }
});

test("application setup imports .env variants and text files into private environment values", async () => {
  let savedApps = [...fixtureApps];
  const ui = await loadUi({
    list: () => ({ apps: savedApps }),
    add: (record) => {
      const payload = JSON.parse(record.values[0]);
      const app = { ...payload, id: payload.slug, status: "stopped", deployments: [] };
      savedApps = [...savedApps, app];
      return { app };
    },
  });
  try {
    ui.document.querySelector("[data-open-add]").click();
    const form = ui.document.querySelector(".am-dialog-form");
    form.elements.repo_url.value = "https://github.com/example/imported.git";
    form.elements.name.value = "Imported";
    form.elements.name.dispatchEvent(new ui.window.Event("input"));
    ui.document.querySelector("#setupNext").click();
    const importInput = ui.document.querySelector("#appEnvironmentImport");
    assert.match(importInput.accept, /\.env/);
    assert.match(importInput.accept, /\.txt/);
    Object.defineProperty(importInput, "files", {
      configurable: true,
      value: [{
        name: ".env.production",
        size: 37,
        text: async () => "export API_KEY='secret-value'\nREGION=west\n",
      }],
    });
    importInput.dispatchEvent(new ui.window.Event("change", { bubbles: true }));
    await settle(ui.window);
    assert.match(form.elements.environment.value, /API_KEY="secret-value"/);
    assert.match(form.elements.environment.value, /REGION="west"/);
    Object.defineProperty(importInput, "files", {
      configurable: true,
      value: [{
        name: "production.txt",
        size: 20,
        text: async () => "TOKEN=from-text-file\n",
      }],
    });
    importInput.dispatchEvent(new ui.window.Event("change", { bubbles: true }));
    await settle(ui.window);
    assert.equal(form.elements.environment.value, 'TOKEN="from-text-file"\n');
    ui.document.querySelector("#setupNext").click();
    assert.match(ui.document.querySelector("#setupReview").textContent, /TOKEN/);
    assert.doesNotMatch(ui.document.querySelector("#setupReview").textContent, /from-text-file/);
    form.elements.deploy_now.checked = false;
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(
      ui.calls.find((record) => record.command === "env-save").input,
      'TOKEN="from-text-file"\n',
    );
  } finally { ui.dom.window.close(); }
});

test("attention includes public failures and matches the summary", async () => {
  const cases = [
    { health: { public_healthy: false } }, { health: { local_healthy: false } },
    { auto_deploy_state: { error: "Fetch failed" } },
    { deployments: [{ status: "deployed", cleanup_error: "Cleanup failed" }] },
    { pending_update: { commit: "new" }, deployed_commit: "old" },
    { pending_update: { commit: "same" }, deployed_commit: "same" },
  ];
  const ui = await loadUi({ list: { apps: cases.map((extra, index) => ({ ...fixtureApps[0], id: `app-${index}`, ...extra })) } });
  try {
    assert.match(ui.document.querySelector("#overviewCards").textContent, /Needs attention\s+5/);
    ui.document.querySelector('[data-filter="failed"]').click();
    assert.equal(ui.document.querySelectorAll(".am-app-card").length, 5);
  } finally { ui.dom.window.close(); }
});

test("refresh preserves settings drafts and successful saves accept server values", async () => {
  let saved = false;
  const ui = await loadUi({ settings: (record) => {
    if (record.values.length) saved = true;
    return { config: { max_concurrent_builds: saved ? 4 : 1 } };
  } });
  try {
    ui.document.querySelector('[name="max_concurrent_builds"]').value = "4";
    ui.document.querySelector("#refreshButton").click();
    await settle(ui.window);
    assert.equal(ui.document.querySelector('[name="max_concurrent_builds"]').value, "4");
    ui.document.querySelector("#settingsForm").dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(saved, true);
    assert.equal(ui.document.querySelector('[name="max_concurrent_builds"]').defaultValue, "4");
  } finally { ui.dom.window.close(); }
});

test("manual health checks reload observations and report public failures", async () => {
  let checked = false;
  const ui = await loadUi({
    list: () => ({ apps: [{ ...fixtureApps[0], health: { public_healthy: checked ? false : null } }] }),
    health: () => { checked = true; return { healthy: true, public_healthy: false, public_error: "DNS failed" }; },
  });
  try {
    ui.document.querySelector('[data-action="health"]').click();
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "list").length, 2);
    assert.match(ui.document.querySelector("#appManagerError").textContent, /public route failed: DNS failed/);
    assert.match(ui.document.querySelector(".am-app-card").textContent, /Failed/);
  } finally { ui.dom.window.close(); }
});

test("plugin updates announce automatic Cockpit restart", async () => {
  const ui = await loadUi({
    "plugin-update-check": { available: true, branch: "main", current_commit: "a".repeat(40), latest_commit: "b".repeat(40) },
    "plugin-update": { updated: true, commit: "b".repeat(40), restart_scheduled: true, restart_delay_seconds: 5 },
  });
  try {
    ui.document.querySelector("#checkUpdateButton").click();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /Cockpit will restart/);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /Updates from GitHub main are installed as root/);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /main/);
    ui.document.querySelector(".am-dialog-form").dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "plugin-update").length, 1);
    assert.match(ui.document.querySelector("#appManagerNotice").textContent, /Cockpit is restarting/);
  } finally { ui.dom.window.close(); }
});

test("rapid deploy clicks enqueue only one deployment", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const ui = await loadUi({ "deployment-start": () => pending });
  try {
    const deploy = ui.document.querySelector('[data-action="deploy"][data-id="portal"]');
    deploy.click();
    deploy.click();
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "deployment-start").length, 1);
    release({ deployment_id: "20261111090000-abcd1234" });
    await settle(ui.window);
    assert.equal(ui.document.querySelector(".am-app-manager").getAttribute("aria-busy"), "false");
  } finally { release({ deployment_id: "20261111090000-abcd1234" }); ui.dom.window.close(); }
});

test("retrying a failed environment save never registers the application twice", async () => {
  let saves = 0;
  const ui = await loadUi({
    add: (record) => ({ app: { ...JSON.parse(record.values[0]), id: "shop", deployments: [] } }),
    "env-save": () => {
      saves += 1;
      if (saves === 1) throw new Error("Temporary save error");
      return { key_count: 1 };
    },
  });
  try {
    ui.document.querySelector("[data-open-add]").click();
    const form = ui.document.querySelector(".am-dialog-form");
    form.elements.repo_url.value = "https://github.com/example/shop.git";
    form.elements.name.value = "Shop";
    form.elements.name.dispatchEvent(new ui.window.Event("input"));
    ui.document.querySelector("#setupNext").click();
    form.elements.environment.value = "API_URL=private";
    ui.document.querySelector("#setupNext").click();
    form.elements.deploy_now.checked = false;
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(ui.document.querySelector("#appManagerDialog").open, true);
    assert.match(ui.document.querySelector(".am-dialog-error").textContent, /Temporary save error/);
    ui.document.querySelector("#setupBack").click();
    assert.equal(form.querySelector('[data-setup-step="1"]').hidden, false);
    form.elements.environment.value = "API_URL=corrected";
    ui.document.querySelector("#setupNext").click();
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(saves, 2);
    assert.equal(ui.calls.filter((record) => record.command === "env-save")[1].input, 'API_URL="corrected"\n');
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 1);
    assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
  } finally { ui.dom.window.close(); }
});

test("setup rejects managed storage variables before registration", async () => {
  const ui = await loadUi();
  try {
    ui.document.querySelector("[data-open-add]").click();
    const form = ui.document.querySelector(".am-dialog-form");
    form.elements.repo_url.value = "https://github.com/example/shop.git";
    form.elements.name.value = "Shop";
    form.elements.name.dispatchEvent(new ui.window.Event("input"));
    ui.document.querySelector("#setupNext").click();
    for (const key of ["APP_DATA_DIR", "APP_STORAGE_DIR", "APP_STORAGE_UPLOADS"]) {
      form.elements.environment.value = `${key}=/tmp/unmanaged`;
      ui.document.querySelector("#setupNext").click();
      assert.equal(form.querySelector('[data-setup-step="1"]').hidden, false);
      assert.match(ui.document.querySelector(".am-dialog-error").textContent, /managed automatically/);
    }
    assert.equal(ui.calls.some((record) => record.command === "add"), false);
  } finally { ui.dom.window.close(); }
});