// Docker Manager Cockpit UI loaded
(() => {
  "use strict";

  const BACKEND = "/usr/libexec/cockpit/app-manager/app-manager";
  const root = document.querySelector(".docker-manager");
  const status = document.getElementById("dockerStatus");
  const error = document.getElementById("dockerError");
  const notice = document.getElementById("dockerNotice");
  const connection = document.getElementById("connectionStatus");
  const installButton = document.getElementById("installButton");
  const refreshButton = document.getElementById("refreshButton");
  let busy = false;

  async function callBackend(command) {
    if (!globalThis.cockpit || typeof globalThis.cockpit.spawn !== "function") {
      throw new Error("Cockpit API is unavailable. Reload Docker Manager inside Cockpit.");
    }
    const process = globalThis.cockpit.spawn([BACKEND, command], { superuser: "require" });
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

  function renderStatus(result) {
    const rows = [
      ["Engine", result.installed ? (result.engine_version || "Installed; version unavailable") : "Not installed"],
      ["Daemon", result.daemon_active ? "Active" : "Inactive"],
      ["Compose", result.compose_available ? (result.compose_version || "Installed") : "Not installed"],
    ];
    status.replaceChildren(...rows.map(([label, value]) => {
      const item = document.createElement("div");
      const term = document.createElement("dt");
      const description = document.createElement("dd");
      term.textContent = label;
      description.textContent = value;
      item.append(term, description);
      return item;
    }));
  }

  async function run(operation, successMessage = "") {
    if (busy) return;
    busy = true;
    root.setAttribute("aria-busy", "true");
    installButton.disabled = true;
    refreshButton.disabled = true;
    error.hidden = true;
    notice.hidden = true;
    connection.textContent = "Working…";
    try {
      renderStatus(await callBackend(operation));
      connection.textContent = "Ready";
      if (successMessage) {
        notice.hidden = false;
        notice.textContent = successMessage;
      }
    } catch (failure) {
      error.hidden = false;
      error.textContent = failure.message || String(failure);
      connection.textContent = "Action needs attention";
    } finally {
      busy = false;
      root.setAttribute("aria-busy", "false");
      installButton.disabled = false;
      refreshButton.disabled = false;
    }
  }

  refreshButton.addEventListener("click", () => { void run("docker-status"); });
  installButton.addEventListener("click", () => {
    if (busy || !window.confirm("Install or update Docker Engine and Compose using Ubuntu APT packages?")) return;
    void run("docker-install", "Docker Engine and Compose are installed and running.");
  });
  void run("docker-status");
})();
