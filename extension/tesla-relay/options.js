const fields = ["token", "repo", "branch"];
const status = document.getElementById("status");

async function load() {
  const stored = await chrome.storage.local.get([...fields, "lastRunAt", "lastOutcome", "publishedAt"]);
  for (const field of fields) document.getElementById(field).value = stored[field] ?? "";
  const when = (ms) => (ms ? new Date(ms).toLocaleString() : "never");
  status.textContent =
    `Last run: ${when(stored.lastRunAt)}\n` +
    `Outcome: ${stored.lastOutcome ?? "none yet"}\n` +
    `Last published: ${when(stored.publishedAt)}`;
}

document.getElementById("save").addEventListener("click", async () => {
  const values = {};
  for (const field of fields) {
    const value = document.getElementById(field).value.trim();
    if (value) values[field] = value;
  }
  await chrome.storage.local.set(values);
  status.textContent = "Saved.";
  setTimeout(load, 800);
});

document.getElementById("run").addEventListener("click", async () => {
  status.textContent = "Running…";
  await chrome.runtime.sendMessage("run-now");
  await load();
});

load();
