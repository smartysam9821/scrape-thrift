const state = {
  inventory: [],
  summary: {},
  config: {},
};

const pages = {
  overview: {
    title: "Scrape operations",
    eyebrow: "Overview",
    description: "Run safe, resumable inventory scrapes across multiple sources.",
  },
  jobs: {
    title: "Job launcher",
    eyebrow: "Jobs",
    description: "Set limits, rate controls, and batch flushing before starting a scrape.",
  },
  inventory: {
    title: "Inventory ledger",
    eyebrow: "Inventory",
    description: "Review latest scraped rows from the CSV output.",
  },
  sources: {
    title: "Scraping sources",
    eyebrow: "Sources",
    description: "ThriftBooks is live. More marketplaces can plug into this shell later.",
  },
  config: {
    title: "Configurations",
    eyebrow: "Config",
    description: "Operational defaults used by FastAPI and the scraper worker.",
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
  limit: el("#limitInput"),
  rpm: el("#rpmInput"),
  concurrency: el("#concurrencyInput"),
  batch: el("#batchInput"),
  freshness: el("#freshnessInput"),
};

function payload() {
  return {
    limit: Number(inputs.limit.value || 780),
    requests_per_minute: Number(inputs.rpm.value || 20),
    concurrency: Number(inputs.concurrency.value || 3),
    batch_size: Number(inputs.batch.value || 25),
    rescrape_hours: Number(inputs.freshness.value || 12),
  };
}

function command() {
  const p = payload();
  return [
    ".\\.venv\\Scripts\\python.exe thriftbooks_scraper.py",
    `--limit ${p.limit}`,
    "--write-mysql",
    `--batch-size ${p.batch_size}`,
    `--requests-per-minute ${p.requests_per_minute}`,
    `--concurrency ${p.concurrency}`,
    "--min-delay-ms 1000",
    "--max-delay-ms 3000",
    `--rescrape-hours ${p.rescrape_hours}`,
  ].join(" ");
}

function renderCommand() {
  commandPreview.textContent = command();
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function refresh() {
  const [summary, progress, inventory, config] = await Promise.all([
    api("/api/summary"),
    api("/api/progress?limit=20"),
    api("/api/inventory?limit=100"),
    api("/api/config"),
  ]);

  state.summary = summary;
  state.inventory = inventory.rows || [];
  state.config = config;

  el("#isbnCount").textContent = Number(summary.isbn_count || 0).toLocaleString();
  el("#resultCount").textContent = Number(summary.result_count || 0).toLocaleString();
  el("#jobState").textContent = summary.job_running ? "Running" : "Idle";
  el("#progressTag").textContent = summary.job_running ? "running" : "ready";
  el("#progressTag").className = `tag ${summary.job_running ? "in-stock" : ""}`;
  el("#progressLog").textContent = progress.lines.length ? progress.lines.join("\n") : "No progress yet.";

  renderRecentInventory();
  renderInventory();
  renderConfig();
}

function renderRecentInventory() {
  const rows = state.inventory.slice(0, 6);
  el("#recentInventory").innerHTML = rows.length
    ? rows.map((row) => `
      <div class="mini-row">
        <span>${row.isbn || ""}</span>
        <strong>${row.publisher || "Unknown"}</strong>
        <span>${money(row.price)}</span>
      </div>
    `).join("")
    : "<p>No inventory rows yet.</p>";
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
  const groups = [
    ["Login", state.config.login],
    ["MySQL", state.config.mysql],
    ["Scraper", state.config.scraper],
  ];

  el("#configGrid").innerHTML = groups.map(([title, values]) => `
    <article class="card config-card">
      <h3>${title}</h3>
      <dl>
        ${Object.entries(values || {}).map(([key, value]) => `
          <div class="config-row"><dt>${key.replaceAll("_", " ")}</dt><dd>${value}</dd></div>
        `).join("")}
      </dl>
    </article>
  `).join("");
}

function switchPage(name) {
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === name));
  el(`#${name}Page`).classList.add("active");
  pageTitle.textContent = pages[name].title;
  pageEyebrow.textContent = pages[name].eyebrow;
  pageDescription.textContent = pages[name].description;
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

el("#logoutButton").addEventListener("click", () => setAuthed(false));
el("#refreshButton").addEventListener("click", refresh);
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

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.page));
});

document.querySelectorAll("[data-jump]").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.jump));
});

Object.values(inputs).forEach((input) => input.addEventListener("input", renderCommand));
el("#inventorySearch").addEventListener("input", renderInventory);
el("#stockFilter").addEventListener("change", renderInventory);

renderCommand();
if (localStorage.getItem("scrapeLedgerAuthed") === "true") {
  setAuthed(true);
  refresh();
}
