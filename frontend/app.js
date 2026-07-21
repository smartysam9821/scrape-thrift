console.log("🚀 app.js loaded successfully");

const state = {
  inventory: [],
  summary: {},
  config: {},
};

const pages = {
  overview: {
    title: "Good afternoon",
    eyebrow: "Overview",
    description: "Last completed run finished recently. Inventory is current as of then.",
  },
  jobs: {
    title: "Run a scrape",
    eyebrow: "Jobs",
    description: "Pick a source file and set the safety controls. Jobs are resumable automatically.",
  },
  inventory: {
    title: "thriftbooks_inv",
    eyebrow: "Inventory",
    description: "Rows update as jobs flush every 25 ISBNs.",
  },
  settings: {
    title: "Connection and defaults",
    eyebrow: "Settings",
    description: "These become defaults for new jobs. Individual jobs can still override them from Jobs.",
  },
};

const el = (selector) => document.querySelector(selector);
const money = (value) => `$${Number(value || 0).toFixed(2)}`;

const loginView = el("#loginView");
const appView = el("#appView");
const loginForm = el("#loginForm");
const loginError = el("#loginError");
const pageTitle = el("#pageTitle");
const pageEyebrow = el("#pageEyebrow");
const pageDescription = el("#pageDescription");
const commandPreview = el("#commandPreview");

const inputs = {
  scraper: el("#scraperSelect"),
  source: el("#sourceSelect"),
  limit: el("#limitInput"),
  rpm: el("#rpmInput"),
  concurrency: el("#concurrencyInput"),
  batch: el("#batchInput"),
  freshness: el("#freshnessInput"),
};

function payload() {
  const scraper = inputs.scraper.value || "thriftbooks";
  return {
    scraper: scraper,
    urls_file: scraper === "hamelyn" ? (inputs.source.value || "urls.txt") : "",
    limit: Number(inputs.limit.value || 780),
    requests_per_minute: Number(inputs.rpm.value || 20),
    concurrency: Number(inputs.concurrency.value || 3),
    batch_size: Number(inputs.batch.value || 25),
    rescrape_hours: Number(inputs.freshness.value || 12),
  };
}

function command() {
  const p = payload();
  const mysql = state.config.mysql || {};
  
  if (p.scraper === "hamelyn") {
    return [
      ".\\.venv\\Scripts\\python.exe scrape_hamelyn.py",
      `--urls-file ${p.urls_file || "urls.txt"}`,
      `--mysql-host ${mysql.host || "<mysql-host>"}`,
      `--mysql-port ${mysql.port || 3306}`,
      `--mysql-user ${mysql.user || "<mysql-user>"}`,
      "--mysql-password ******",
      `--mysql-db ${mysql.database || "<mysql-database>"}`,
      `--rpm ${p.requests_per_minute}`,
    ].join(" ");
  } else {
    return [
      ".\\.venv\\Scripts\\python.exe thriftbooks_scraper.py",
      `--limit ${p.limit}`,
      "--write-mysql",
      `--mysql-host ${mysql.host || "<mysql-host>"}`,
      `--mysql-port ${mysql.port || 3306}`,
      `--mysql-database ${mysql.database || "<mysql-database>"}`,
      `--mysql-user ${mysql.user || "<mysql-user>"}`,
      "--mysql-password ******",
      `--batch-size ${p.batch_size}`,
      `--requests-per-minute ${p.requests_per_minute}`,
      `--concurrency ${p.concurrency}`,
      "--min-delay-ms 1000",
      "--max-delay-ms 3000",
      `--rescrape-hours ${p.rescrape_hours}`,
    ].join(" ");
  }
}

function renderCommand() {
  commandPreview.textContent = command();
}

function updateSourcesDropdown() {
  const scraper = inputs.scraper.value || "thriftbooks";
  const sources = state.sources || {};
  const scraperSources = sources[scraper] || [];
  
  // Show/hide limit input based on scraper
  const limitLabel = document.querySelector('label:has(#limitInput)');
  if (limitLabel) limitLabel.style.display = scraper === "thriftbooks" ? "" : "none";
  
  // Update source dropdown
  inputs.source.innerHTML = scraperSources.map(source => 
    `<option value="${source.file}">${source.name}</option>`
  ).join("");
  
  renderCommand();
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function refresh() {
  const [summary, progress, inventory, config, sources] = await Promise.all([
    api("/api/summary"),
    api("/api/progress?limit=20"),
    api("/api/inventory?limit=100"),
    api("/api/config"),
    api("/api/sources"),
  ]);

  state.summary = summary;
  state.inventory = inventory.rows || [];
  state.config = config;
  state.sources = sources;

  // Update sources dropdown based on current scraper
  updateSourcesDropdown();

  el("#isbnCount").textContent = Number(summary.isbn_count || 0).toLocaleString();
  el("#resultCount").textContent = Number(summary.result_count || 0).toLocaleString();
  el("#inventoryRowCount").textContent = Number(summary.result_count || 0).toLocaleString();
  el("#progressTag").textContent = summary.job_running ? "running" : "ready";
  el("#progressTag").className = `tag ${summary.job_running ? "in-stock" : ""}`;
  const job = summary.job || {};
  const unitLabel = job.scraper === "hamelyn" ? "URLs" : "ISBNs";
  el("#jobStatusTag").textContent = summary.job_running ? "running" : "idle";
  el("#jobStatusTag").className = `tag ${summary.job_running ? "in-stock" : ""}`;
  el("#jobProgressFill").style.width = summary.job_running ? `${Math.min(job.percent || 0, 100)}%` : "0";
  el("#jobProgressCount").textContent = summary.job_running
    ? `${Number(job.processed || 0).toLocaleString()} / ${Number(job.limit || payload().limit).toLocaleString()} ${unitLabel}`
    : "No active job";
  el("#jobProgressMeta").textContent = summary.job_running
    ? `concurrency ${job.concurrency || payload().concurrency} - ${job.requests_per_minute || payload().requests_per_minute} req/min`
    : "ready to start";
  el("#stopJobButton").classList.toggle("hidden", !summary.job_running);
  el("#progressLog").textContent = progress.lines.length ? progress.lines.join("\n") : "No progress yet.";

  renderStockStats();
  renderRecentInventory();
  renderInventory();
  renderConfig();
  applyConfigToJobForm();
  await renderJobHistory();
}

function renderRecentInventory() {
  const rows = state.inventory.filter((row) => row.stock_status !== "IN_STOCK").slice(0, 3);
  el("#recentInventory").innerHTML = rows.length
    ? rows.map((row) => `
      <div class="mini-row">
        <span>${row.isbn || ""}</span>
        <strong>${row.stock_status || "UNKNOWN"}</strong>
        <span>${row.last_seen_timestamp || ""}</span>
      </div>
    `).join("")
    : "<p>No flagged rows.</p>";
}

function renderStockStats() {
  const total = state.inventory.length || 1;
  const inStock = state.inventory.filter((row) => row.stock_status === "IN_STOCK").length;
  const outStock = state.inventory.filter((row) => row.stock_status === "OUT_OF_STOCK").length;
  const blocked = state.inventory.filter((row) => row.stock_status?.startsWith("BLOCKED")).length;
  const unknown = Math.max(total - inStock - outStock, 0);
  const pct = (value) => `${Math.round((value / total) * 100)}%`;
  el("#inStockPct").textContent = pct(inStock);
  el("#blockCount").textContent = blocked;
  el("#stockInLabel").textContent = pct(inStock);
  el("#stockOutLabel").textContent = pct(outStock);
  el("#stockUnknownLabel").textContent = pct(unknown);
  const job = state.summary.job || {};
  const unitLabel = job.scraper === "hamelyn" ? "URLs" : "ISBNs";
  el("#progressCount").textContent = state.summary.job_running
    ? `${Number(job.processed || 0).toLocaleString()} / ${Number(job.limit || payload().limit).toLocaleString()} ${unitLabel}`
    : "No active job";
}

function renderInventory() {
  const q = el("#inventorySearch").value.toLowerCase();
  const stock = el("#stockFilter").value;
  const rows = state.inventory.filter((row) => {
    const haystack = `${row.isbn} ${row.publisher} ${row.stock_status}`.toLowerCase();
    return (!q || haystack.includes(q)) && (!stock || row.stock_status === stock);
  });

  el("#inventoryBody").innerHTML = rows.map((row) => `
    <tr>
      <td>${row.id || ""}</td>
      <td>${row.isbn || ""}</td>
      <td>${row.publisher || "Unknown"}</td>
      <td>${money(row.price)}</td>
      <td>${row.format || ""}</td>
      <td>${row.condition || ""}</td>
      <td><span class="tag ${row.stock_status === "IN_STOCK" ? "in-stock" : row.stock_status?.startsWith("BLOCKED") ? "blocked" : ""}">${row.stock_status || "UNKNOWN"}</span></td>
      <td>${row.last_seen_timestamp || ""}</td>
    </tr>
  `).join("");
}

function renderConfig() {
  const mysql = state.config.mysql || {};
  const login = state.config.login || {};
  const scraper = state.config.scraper || {};
  const setValue = (selector, value) => {
    const node = el(selector);
    if (node && value !== undefined) node.value = value;
  };

  setValue("#configMysqlHost", mysql.host);
  setValue("#configMysqlPort", mysql.port);
  setValue("#configMysqlDatabase", mysql.database);
  setValue("#configMysqlUser", mysql.user);
  setValue("#configMysqlPassword", mysql.password);
  setValue("#configLoginUser", login.username);
  setValue("#configConcurrency", scraper.concurrency);
  setValue("#configRpm", scraper.requests_per_minute);
  setValue("#configRescrape", scraper.rescrape_hours);
  setValue("#configBatch", scraper.batch_size);
}

async function renderJobHistory() {
  const data = await api("/api/jobs/history");
  const rows = data.jobs || [];
  el("#jobHistoryBody").innerHTML = rows.length
    ? rows.map((job) => `
      <tr>
        <td>#${job.id}</td>
        <td>${formatStarted(job.started_at)}</td>
        <td>${Number(job.processed || 0).toLocaleString()} / ${Number(job.limit || 0).toLocaleString()}</td>
        <td><span class="tag ${job.status === "failed" ? "blocked" : ""}">${job.status}</span></td>
      </tr>
    `).join("")
    : `<tr><td colspan="4">No completed jobs yet.</td></tr>`;
}

function formatStarted(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function configPayload({ includeAccount = false } = {}) {
  const body = {
    mysql: {
      host: el("#configMysqlHost").value,
      port: Number(el("#configMysqlPort").value || 3306),
      database: el("#configMysqlDatabase").value,
      user: el("#configMysqlUser").value,
      password: el("#configMysqlPassword").value,
    },
    scraper: {
      concurrency: Number(el("#configConcurrency").value || 3),
      requests_per_minute: Number(el("#configRpm").value || 20),
      rescrape_hours: Number(el("#configRescrape").value || 12),
      batch_size: Number(el("#configBatch").value || 25),
    },
  };
  if (includeAccount) {
    body.login = {
      username: el("#configLoginUser").value,
      password: el("#configLoginPassword").value,
    };
  }
  return body;
}

function applySettingsToJobForm() {
  inputs.concurrency.value = el("#configConcurrency").value || inputs.concurrency.value;
  inputs.rpm.value = el("#configRpm").value || inputs.rpm.value;
  inputs.freshness.value = el("#configRescrape").value || inputs.freshness.value;
  inputs.batch.value = el("#configBatch").value || inputs.batch.value;
  renderCommand();
}

function applyConfigToJobForm() {
  const scraper = state.config.scraper || {};
  if (scraper.concurrency !== undefined) inputs.concurrency.value = scraper.concurrency;
  if (scraper.requests_per_minute !== undefined) inputs.rpm.value = scraper.requests_per_minute;
  if (scraper.rescrape_hours !== undefined) inputs.freshness.value = scraper.rescrape_hours;
  if (scraper.batch_size !== undefined) inputs.batch.value = scraper.batch_size;
  renderCommand();
}

function switchPage(name) {
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === name));
  el(`#${name}Page`).classList.add("active");
  pageTitle.textContent = pages[name].title;
  pageEyebrow.textContent = pages[name].eyebrow;
  pageDescription.textContent = pages[name].description;
  document.querySelector(".head-actions").classList.toggle("hidden", name === "settings");
}

function setAuthed(authed) {
  loginView.classList.toggle("hidden", authed);
  appView.classList.toggle("hidden", !authed);
  localStorage.setItem("scrapeLedgerAuthed", String(authed));
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(loginForm);
  try {
    await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    loginError.textContent = "";
    setAuthed(true);
    await refresh();
  } catch {
    loginError.textContent = "Invalid username or password.";
  }
});

el("#logoutButton").addEventListener("click", () => {
  localStorage.removeItem("scrapeLedgerAuthed");
  setAuthed(false);
  loginForm.reset();
});
el("#copyCommand").addEventListener("click", async () => navigator.clipboard.writeText(command()));
el("#startJobButton").addEventListener("click", async () => {
  const button = el("#startJobButton");
  button.textContent = "Starting";
  try {
    await api("/api/jobs/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    button.textContent = "Running";
    await refresh();
  } catch {
    button.textContent = "Job already running";
  }
  setTimeout(() => { button.textContent = "Start job"; }, 1600);
});

el("#saveDefaultsButton").addEventListener("click", async () => {
  await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configPayload()),
  });
  applySettingsToJobForm();
});

el("#saveAccountButton").addEventListener("click", async () => {
  await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(configPayload({ includeAccount: true })),
  });
});

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.page));
});

document.querySelectorAll("[data-jump]").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.jump));
});

Object.values(inputs).forEach((input) => {
  if (input) input.addEventListener("input", renderCommand);
});
if (inputs.scraper) inputs.scraper.addEventListener("change", updateSourcesDropdown);
el("#inventorySearch").addEventListener("input", renderInventory);
el("#stockFilter").addEventListener("change", renderInventory);

renderCommand();
if (localStorage.getItem("scrapeLedgerAuthed") === "true") {
  setAuthed(true);
  refresh();
}
