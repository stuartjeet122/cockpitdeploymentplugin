const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "..");
test("deployment prefixes parsed local asset and API literals only in the release copy", () => {
  const os = require("node:os");
  const { spawnSync } = require("node:child_process");
  const { parse } = require("@babel/parser");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "app-manager-assets-"));
  try {
    const checkout = path.join(temporary, "checkout");
    const release = path.join(temporary, "release");
    fs.mkdirSync(path.join(checkout, "src"), { recursive: true });
    fs.mkdirSync(path.join(checkout, "public", "icons"), { recursive: true });
    fs.writeFileSync(path.join(checkout, "package.json"), "{}");
    fs.writeFileSync(path.join(checkout, "infomil-logo.svg"), "<svg/>");
    fs.writeFileSync(path.join(checkout, "public", "icons", "logo.svg"), "<svg/>");
    fs.writeFileSync(path.join(checkout, "public", ".env"), "FIXTURE=private");
    fs.mkdirSync(path.join(checkout, "src", "app", "api", "teams"), { recursive: true });
    fs.writeFileSync(path.join(checkout, "src", "app", "icon.svg"), "<svg/>");
    fs.mkdirSync(path.join(checkout, "src", "app", "participants"));
    fs.writeFileSync(path.join(checkout, "src", "app", "participants", "page.tsx"), "export default function Page() { return null; }");
    fs.writeFileSync(path.join(checkout, "src", "app", "manifest.ts"),
      'export default function manifest() { return { id: "/", scope: "/", start_url: "/participants", icons: [{ src: "/icon.svg" }] }; }');
    fs.writeFileSync(path.join(checkout, "src", "app", "api", "teams", "route.ts"),
      'export function GET() { return Response.json({ configured: Boolean(process.env.DATABASE_URL) }); }');
    fs.mkdirSync(path.join(checkout, "src", "pages", "api"), { recursive: true });
    fs.writeFileSync(path.join(checkout, "src", "pages", "api", "team.js"),
      'export default function handler(req, res) { res.json({ configured: Boolean(process.env.DATABASE_URL) }); }');
    const requests = 'function cloudFetch(input) { return fetch(input); }\ncloudFetch("/api/teams?refresh=1");\nfetch("/api/team");';
    fs.writeFileSync(path.join(checkout, "src", "cloud.js"), requests);
    const navigation = 'window.history.replaceState(null, "", "/teams?team=" + "fixture");\n' +
      'window.history.pushState(null, "", "/scrumtimer/?team=fixture");\n' +
      'window.history.pushState(null, "", "https://example.com/elsewhere");\n' +
      'document.documentElement.requestFullscreen().catch(() => {});';
    fs.writeFileSync(path.join(checkout, "src", "navigation.js"), navigation);
    fs.writeFileSync(path.join(checkout, "src", "pathname.js"),
      'import { usePathname as currentPath } from "next/navigation";\n' +
      'const pathname = currentPath();\nconst routeLoaded = pathname === "/teams";');
    const source = [
      'import logo from "/infomil-logo.svg";',
      '// Keep /infomil-logo.svg in comments.',
      'const generated: string = "/infomil-logo.svg?version=1#mark";',
      'const template = `/icons/logo.svg`;',
      'const dynamic = `/icons/${name}.svg`;',
      'const logoImage = <img src="/infomil-logo.svg" />;',
      'const quoted = <img src="/icons/logo.svg?label=&quot;logo&quot;" />;',
      'const unchanged = ["//cdn.example/logo.svg", "https://cdn.example/logo.svg",',
      '"/scrumtimer/icons/logo.svg", "/another-app/logo.svg", "/api/unmanaged", "/.env",',
      '"/%2e%2e/infomil-logo.svg", "/icons/../infomil-logo.svg", "/missing.svg"];',
    ].join("\n");
    fs.writeFileSync(path.join(checkout, "src", "navbar.tsx"), source);
    fs.writeFileSync(path.join(checkout, "src", "typed.ts"), 'const mode = <string>"fixture";\n');
    fs.cpSync(checkout, release, { recursive: true });
    const outside = path.join(temporary, "outside");
    fs.mkdirSync(outside);
    fs.writeFileSync(path.join(outside, "logo.svg"), "<svg/>");
    fs.symlinkSync(outside, path.join(release, "public", "linked"),
      process.platform === "win32" ? "junction" : "dir");
    fs.writeFileSync(path.join(release, "src", "linked.ts"), 'const logo = "/linked/logo.svg";');
    const parserBridge = path.join(release, "node_modules", "next", "dist", "compiled", "babel");
    fs.mkdirSync(parserBridge, { recursive: true });
    fs.writeFileSync(path.join(parserBridge, "core.js"),
      `exports.parseSync = (source, options) => require(${JSON.stringify(require.resolve("@babel/parser"))}).parse(source, { ...options.parserOpts, sourceType: options.sourceType });`);
    const helper = path.join(root, "backend", "prepare_next_assets.cjs");
    const prepared = spawnSync(process.execPath, [helper, "/scrumtimer"], {
      cwd: release, encoding: "utf8", timeout: 10000,
    });
    assert.equal(prepared.status, 0, prepared.stderr);
    const output = fs.readFileSync(path.join(release, "src", "navbar.tsx"), "utf8");
    assert.ok(output.includes('const generated: string = "/scrumtimer/infomil-logo.svg?version=1#mark";'));
    assert.ok(output.includes('const template = "/scrumtimer/icons/logo.svg";'));
    assert.ok(output.includes('src="/scrumtimer/infomil-logo.svg"'));
    assert.ok(output.includes('src="/scrumtimer/icons/logo.svg?label=&quot;logo&quot;"'));
    for (const line of source.split("\n").filter(line => !line.startsWith("const generated") &&
      !line.startsWith("const template") && !line.startsWith("const logoImage") && !line.startsWith("const quoted"))) {
      assert.ok(output.includes(line), line);
    }
    parse(output, { sourceType: "module", plugins: ["typescript", "jsx"] });
    assert.equal(fs.readFileSync(path.join(checkout, "src", "navbar.tsx"), "utf8"), source);
    assert.equal(fs.readFileSync(path.join(release, "public", ".env"), "utf8"), "FIXTURE=private");
    assert.equal(fs.readFileSync(path.join(release, "src", "linked.ts"), "utf8"), 'const logo = "/linked/logo.svg";');
    assert.equal(fs.readFileSync(path.join(release, "src", "typed.ts"), "utf8"), 'const mode = <string>"fixture";\n');
    const requestedUrls = [];
    require("node:vm").runInNewContext(fs.readFileSync(path.join(release, "src", "cloud.js"), "utf8"), {
      fetch: (url) => { requestedUrls.push(url); },
    });
    assert.deepEqual(requestedUrls, ["/scrumtimer/api/teams?refresh=1", "/scrumtimer/api/team"]);
    assert.equal(fs.readFileSync(path.join(checkout, "src", "cloud.js"), "utf8"), requests);
    assert.ok(fs.readFileSync(path.join(release, "src", "app", "api", "teams", "route.ts"), "utf8")
      .includes("process.env.DATABASE_URL"));
    const manifest = fs.readFileSync(path.join(release, "src", "app", "manifest.ts"), "utf8");
    assert.ok(manifest.includes('id: "/scrumtimer/"'));
    assert.ok(manifest.includes('scope: "/scrumtimer/"'));
    assert.ok(manifest.includes('start_url: "/scrumtimer/participants"'));
    assert.ok(manifest.includes('src: "/scrumtimer/icon.svg"'));
    const historyUrls = [];
    let fullscreenAttempts = 0;
    const navigationContext = {
      window: { history: {
        replaceState: (state, title, url) => historyUrls.push(url),
        pushState: (state, title, url) => historyUrls.push(url),
      } },
      navigator: { userActivation: { isActive: false } },
      document: { documentElement: { requestFullscreen: () => { fullscreenAttempts += 1; return Promise.resolve(); } } },
    };
    const preparedNavigation = fs.readFileSync(path.join(release, "src", "navigation.js"), "utf8");
    require("node:vm").runInNewContext(preparedNavigation, navigationContext);
    assert.deepEqual(historyUrls, ["/scrumtimer/teams?team=fixture", "/scrumtimer/?team=fixture", "https://example.com/elsewhere"]);
    assert.equal(fullscreenAttempts, 0);
    navigationContext.navigator.userActivation.isActive = true;
    require("node:vm").runInNewContext(preparedNavigation, navigationContext);
    assert.equal(fullscreenAttempts, 1);
    const preparedPathname = fs.readFileSync(path.join(release, "src", "pathname.js"), "utf8");
    const routeLoaded = require("node:vm").runInNewContext(
      preparedPathname.replace(/^import[^\n]*\n/, "") + "\nrouteLoaded;", { currentPath: () => "/teams/" });
    assert.equal(routeLoaded, true);
    assert.match(prepared.stdout, /Prepared 15 local asset\/API URL\(s\) in 5 release file/);
    const repeated = spawnSync(process.execPath, [helper, "/scrumtimer"], {
      cwd: release, encoding: "utf8", timeout: 10000,
    });
    assert.equal(repeated.status, 0, repeated.stderr);
    assert.match(repeated.stdout, /Prepared 0 local asset\/API/);
    assert.equal(fs.readFileSync(path.join(release, "src", "navbar.tsx"), "utf8"), output);
    fs.writeFileSync(path.join(release, "src", "broken.tsx"), "export const invalid = <;");
    const malformed = spawnSync(process.execPath, [helper, "/scrumtimer"], {
      cwd: release, encoding: "utf8", timeout: 10000,
    });
    assert.notEqual(malformed.status, 0);
    assert.match(malformed.stderr, /Cannot safely prepare asset URLs in src/);
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

for (const manager of process.env.APP_MANAGER_TEST_PACKAGE_TOOLS ? ["npm", "pnpm"] : ["npm"]) {
test(`deployment waits for generated assets and preserves ${manager} build lifecycle hooks`, () => {
  const os = require("node:os");
  const { spawnSync } = require("node:child_process");
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "app-manager-build-"));
  try {
    fs.mkdirSync(path.join(temporary, "src"));
    fs.mkdirSync(path.join(temporary, "scripts"));
    const packageData = { scripts: {
      prebuild: "node scripts/generate.cjs",
      build: "node scripts/verify.cjs",
      postbuild: "node scripts/post.cjs",
    } };
    fs.writeFileSync(path.join(temporary, "package.json"), JSON.stringify(packageData));
    fs.writeFileSync(path.join(temporary, "src", "page.tsx"), 'export const page = <img src="/guide/participants.svg" />;');
    fs.writeFileSync(path.join(temporary, "scripts", "generate.cjs"),
      "const fs = require('node:fs'); fs.mkdirSync('public/guide', { recursive: true }); " +
      "fs.writeFileSync('public/guide/participants.svg', '<svg/>'); " +
      "fs.writeFileSync('public/manifest.webmanifest', JSON.stringify({ scope: '/', start_url: '/', icons: [{src:'/guide/participants.svg'}] })); " +
      "fs.appendFileSync('order.txt', 'prebuild\\n');");
    fs.writeFileSync(path.join(temporary, "scripts", "verify.cjs"),
      "const fs = require('node:fs'); require('node:assert/strict').ok(" +
      "fs.readFileSync('src/page.tsx', 'utf8').includes('/demo/guide/participants.svg')); " +
      "fs.appendFileSync('order.txt', 'build\\n');");
    fs.writeFileSync(path.join(temporary, "scripts", "post.cjs"),
      "require('node:fs').appendFileSync('order.txt', 'postbuild\\n');");
    const parserBridge = path.join(temporary, "node_modules", "next", "dist", "compiled", "babel");
    fs.mkdirSync(parserBridge, { recursive: true });
    fs.writeFileSync(path.join(parserBridge, "core.js"),
      `exports.parseSync = (source, options) => require(${JSON.stringify(require.resolve("@babel/parser"))}).parse(source, { ...options.parserOpts, sourceType: options.sourceType });`);
    const helper = path.join(root, "backend", "prepare_next_assets.cjs");
    fs.writeFileSync(path.join(temporary, ".npmrc"), "enable-pre-post-scripts=true\nmanage-package-manager-versions=false\n");
    const setup = spawnSync(process.execPath, [helper, "/demo", "--install-hook", manager, "20"], {
      cwd: temporary, encoding: "utf8", timeout: 10000,
    });
    assert.equal(setup.status, 0, setup.stderr);
    assert.equal(fs.existsSync(path.join(temporary, "order.txt")), false);
    const built = spawnSync(process.platform === "win32" ? `${manager}.cmd` : manager, ["run", "build"], {
      cwd: temporary, encoding: "utf8", timeout: 20000, shell: process.platform === "win32",
    });
    assert.equal(built.status, 0, built.stdout + built.stderr);
    assert.equal(fs.readFileSync(path.join(temporary, "order.txt"), "utf8"), "prebuild\nbuild\npostbuild\n");
    assert.match(built.stdout, /Prepared 4 local asset\/API URL/);
    const manifest = JSON.parse(fs.readFileSync(path.join(temporary, "public", "manifest.webmanifest"), "utf8"));
    assert.deepEqual(manifest, { scope: "/demo/", start_url: "/demo/", icons: [{ src: "/demo/guide/participants.svg" }] });
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
}

test("shared registry reuses tarballs across isolated stores and rejects writes", {
  skip: !process.env.APP_MANAGER_TEST_PACKAGE_TOOLS,
  timeout: 120000,
}, async () => {
  const http = require("node:http");
  const crypto = require("node:crypto");
  const { spawn, spawnSync } = require("node:child_process");
  const temporary = fs.mkdtempSync(path.join(require("node:os").tmpdir(), "app-manager-cache-"));
  const tool = (name) => {
    const directory = process.env.PATH.split(path.delimiter).map((entry) => path.resolve(entry, "..", name))
      .find((entry) => fs.existsSync(path.join(entry, "package.json")));
    assert.ok(directory, `${name} must be available through npm exec`);
    const metadata = JSON.parse(fs.readFileSync(path.join(directory, "package.json"), "utf8"));
    return path.join(directory, typeof metadata.bin === "string" ? metadata.bin : metadata.bin[name]);
  };
  const pnpm = tool("pnpm");
  let registry;
  let registryOutput = "";
  let tarballDownloads = 0;
  const fixture = path.join(temporary, "fixture");
  fs.mkdirSync(fixture);
  fs.writeFileSync(path.join(fixture, "package.json"), JSON.stringify({
    name: "app-manager-cache-fixture", version: "1.0.0", main: "index.js",
    scripts: { postinstall: "node -e \"require('fs').writeFileSync('built.txt', 'built')\"" },
  }));
  fs.writeFileSync(path.join(fixture, "index.js"), "module.exports = 'shared-download';");
  const packed = spawnSync(process.platform === "win32" ? "npm.cmd" : "npm", ["pack", "--ignore-scripts", "--pack-destination", temporary], {
    cwd: fixture, encoding: "utf8", timeout: 20000, shell: process.platform === "win32",
  });
  assert.equal(packed.status, 0, packed.stdout + packed.stderr);
  const tarball = fs.readFileSync(path.join(temporary, "app-manager-cache-fixture-1.0.0.tgz"));
  const integrity = `sha512-${crypto.createHash("sha512").update(tarball).digest("base64")}`;
  const upstream = http.createServer((request, response) => {
    if (request.url.endsWith(".tgz")) {
      tarballDownloads += 1;
      response.end(tarball);
    } else {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({
        name: "app-manager-cache-fixture", "dist-tags": { latest: "1.0.0" },
        versions: { "1.0.0": {
          name: "app-manager-cache-fixture", version: "1.0.0",
          dist: { tarball: `http://127.0.0.1:${upstream.address().port}/app-manager-cache-fixture/-/app-manager-cache-fixture-1.0.0.tgz`, integrity },
        } },
      }));
    }
  });
  try {
    await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
    const reservation = http.createServer();
    await new Promise((resolve) => reservation.listen(0, "127.0.0.1", resolve));
    const port = reservation.address().port;
    await new Promise((resolve) => reservation.close(resolve));
    const config = JSON.parse(fs.readFileSync(path.join(root, "config", "npm-cache.json"), "utf8"));
    assert.equal(config.listen, "127.0.0.1:4873");
    assert.equal(config.uplinks.npmjs.url, "https://registry.npmjs.org/");
    config.storage = path.join(temporary, "shared-cache");
    config.auth.htpasswd.file = path.join(temporary, "htpasswd");
    config.uplinks.npmjs.url = `http://127.0.0.1:${upstream.address().port}/`;
    config.listen = `127.0.0.1:${port}`;
    const configPath = path.join(temporary, "cache.json");
    fs.writeFileSync(configPath, JSON.stringify(config));
    registry = spawn(process.execPath, [tool("verdaccio"), "--config", configPath], { stdio: ["ignore", "pipe", "pipe"] });
    registry.stdout.on("data", (data) => { registryOutput += data; });
    registry.stderr.on("data", (data) => { registryOutput += data; });
    const url = `http://127.0.0.1:${port}/`;
    for (let attempt = 0; ; attempt += 1) {
      try {
        const response = await fetch(url + "-/ping");
        if (response.ok) break;
      } catch {}
      assert.ok(attempt < 100 && registry.exitCode === null, registryOutput);
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    for (const endpoint of ["app-manager-cache-fixture", "-/user/org.couchdb.user:fixture"]) {
      const response = await fetch(url + endpoint, { method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: "fixture", password: "fixture", type: "user", roles: [] }) });
      assert.ok(response.status === 401 || response.status === 403 || response.status === 409, `${endpoint}: ${response.status}`);
    }
    const execute = async (command, args, cwd, environment = {}) => {
      const child = spawn(command, args, { cwd, env: { ...process.env, CI: "true", ...environment }, stdio: ["ignore", "pipe", "pipe"] });
      let output = "";
      child.stdout.on("data", (data) => { output += data; });
      child.stderr.on("data", (data) => { output += data; });
      const timer = setTimeout(() => child.kill(), 30000);
      try {
        const status = await new Promise((resolve, reject) => { child.once("error", reject); child.once("exit", resolve); });
        assert.equal(status, 0, output);
        return output;
      } finally { clearTimeout(timer); }
    };
    for (const app of ["first", "second"]) {
      const project = path.join(temporary, app);
      fs.mkdirSync(project);
      fs.writeFileSync(path.join(project, "package.json"), JSON.stringify({ name: app, version: "1.0.0",
        dependencies: { "app-manager-cache-fixture": "1.0.0" }, pnpm: { onlyBuiltDependencies: ["app-manager-cache-fixture"] } }));
      fs.writeFileSync(path.join(project, "package-lock.json"), JSON.stringify({ lockfileVersion: 3, packages: {
        "": { dependencies: { "app-manager-cache-fixture": "1.0.0" } },
        "node_modules/app-manager-cache-fixture": { version: "1.0.0", resolved: "https://registry.npmjs.org/app-manager-cache-fixture/-/app-manager-cache-fixture-1.0.0.tgz", integrity },
      } }));
      await execute(process.env.PYTHON || (process.platform === "win32" ? "python" : "python3"), ["-c",
        "import subprocess,sys; from pathlib import Path; from backend.adapters import _convert_to_pnpm; " +
        "project=Path(sys.argv[1]); _convert_to_pnpm(project, 'npm', project/'package-lock.json', " +
        "lambda command,cwd,timeout: subprocess.run([sys.argv[2],sys.argv[3],*command[1:]],cwd=cwd,timeout=timeout,capture_output=True,text=True),30)",
        project, process.execPath, pnpm], root, { npm_config_registry: url });
      await execute(process.execPath, [pnpm, "install", "--frozen-lockfile", "--prod=false", "--store-dir", path.join(temporary, `${app}-store`)], project, { npm_config_registry: url });
      assert.equal(fs.readFileSync(path.join(project, "node_modules", "app-manager-cache-fixture", "built.txt"), "utf8"), "built");
      await execute(process.execPath, [pnpm, "install", "--frozen-lockfile", "--prod=true", "--store-dir", path.join(temporary, `${app}-store`)], project, { npm_config_registry: url });
    }
    assert.equal(tarballDownloads, 1, "independent stores must reuse the service-owned cached tarball");
  } finally {
    if (registry && registry.exitCode === null) {
      const stopped = new Promise((resolve) => registry.once("exit", resolve));
      registry.kill();
      await stopped;
    }
    upstream.closeAllConnections();
    await new Promise((resolve) => upstream.close(resolve));
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});

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