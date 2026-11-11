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

async function loadUi(overrides = {}) {
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
    form.elements.environment.value = "API_URL=https://api.example.test";
    ui.document.querySelector("#setupNext").click();
    assert.match(ui.document.querySelector("#setupReview").textContent, /API_URL/);
    assert.doesNotMatch(ui.document.querySelector("#setupReview").textContent, /api.example.test/);
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 0);
    form.elements.deploy_now.checked = false;
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 1);
    assert.equal(ui.calls.find((record) => record.command === "env-save").input, 'API_URL="https://api.example.test"\n');
    assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
  } finally { ui.dom.window.close(); }
});

test("plugin updates announce automatic Cockpit restart", async () => {
  const ui = await loadUi({
    "plugin-update-check": { available: true, current_commit: "a".repeat(40), latest_commit: "b".repeat(40) },
    "plugin-update": { updated: true, commit: "b".repeat(40), restart_scheduled: true, restart_delay_seconds: 5 },
  });
  try {
    ui.document.querySelector("#checkUpdateButton").click();
    await settle(ui.window);
    assert.match(ui.document.querySelector("#appManagerDialog").textContent, /Cockpit will restart/);
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
    form.dispatchEvent(new ui.window.Event("submit", { bubbles: true, cancelable: true }));
    await settle(ui.window);
    assert.equal(saves, 2);
    assert.equal(ui.calls.filter((record) => record.command === "add").length, 1);
    assert.equal(ui.document.querySelector("#appManagerDialog").open, false);
  } finally { ui.dom.window.close(); }
});