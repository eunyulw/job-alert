// Tesla relay: reads Tesla's careers board in this browser and publishes its internships to the
// job-alert repository, where the GitHub monitor reads them as the `tesla-relay` source.
//
// Tesla answers its careers API only to a real browser, so the read happens here, in a tab of
// this Chrome profile: an open www.tesla.com tab is reused, otherwise a background tab is opened
// (never focused) and closed again. The page's own same-origin request is repeated once
// (`/cua-api/apps/careers/state`); nothing else on tesla.com is requested.
//
// The feed is committed to `tesla-feed` only when the internship list changes, or every six hours
// as a heartbeat, so the monitor can tell a quiet board from a browser that stopped running.

const CAREERS_PAGE = "https://www.tesla.com/careers/search/?type=3&site=US";
const BOARD_PATH = "/cua-api/apps/careers/state";
const ALARM = "tesla-relay";
const PERIOD_MINUTES = 10;
const HEARTBEAT_MS = 6 * 60 * 60 * 1000;
const LOAD_TIMEOUT_MS = 45_000;
const SETTLE_MS = 3_000;
const DEFAULTS = { repo: "eunyulw/job-alert", branch: "tesla-feed", path: "tesla.json" };

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) run();
});
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message !== "run-now") return false;
  run().then(() => reply(true));
  return true; // the reply is sent asynchronously
});

function schedule() {
  chrome.alarms.create(ALARM, { delayInMinutes: 1, periodInMinutes: PERIOD_MINUTES });
}

let running = null;

function run() {
  running ??= relay()
    .catch((error) => record({ lastOutcome: `error: ${error.message}` }))
    .finally(() => {
      running = null;
    });
  return running;
}

async function relay() {
  const settings = { ...DEFAULTS, ...(await chrome.storage.local.get(["token", "repo", "branch"])) };
  if (!settings.token) throw new Error("no GitHub token; open the extension's options");
  const board = await withTeslaTab(readBoard);
  const internships = board.internships.sort((a, b) => a.id.localeCompare(b.id));
  const fingerprint = JSON.stringify(internships);
  const state = await chrome.storage.local.get(["fingerprint", "publishedAt"]);
  const now = Date.now();
  const due = !state.publishedAt || now - state.publishedAt >= HEARTBEAT_MS;
  if (fingerprint === state.fingerprint && !due) {
    await record({ lastRunAt: now, lastOutcome: `unchanged: ${internships.length} internships` });
    return;
  }
  const feed = {
    schema: 1,
    generated_at: new Date(now).toISOString(),
    board_total: board.total,
    internships,
  };
  const changed = fingerprint === state.fingerprint ? "heartbeat" : "internships changed";
  await publish(settings, JSON.stringify(feed, null, 1) + "\n", `Tesla feed: ${changed}`);
  await chrome.storage.local.set({ fingerprint, publishedAt: now });
  await record({
    lastRunAt: now,
    lastOutcome: `published (${changed}): ${internships.length} internships`,
  });
}

function record(fields) {
  return chrome.storage.local.set({ lastRunAt: Date.now(), ...fields });
}

// ---- Reading Tesla -------------------------------------------------------------------------

async function withTeslaTab(task) {
  const open = await chrome.tabs.query({ url: "https://www.tesla.com/*" });
  const usable = open.find((tab) => tab.status === "complete" && !tab.discarded);
  if (usable) return task(usable.id);
  const tab = await openBackgroundTab();
  try {
    await loaded(tab.id);
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    return await task(tab.id);
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function openBackgroundTab() {
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  if (windows.length) {
    const target = windows.find((window) => window.focused) ?? windows[0];
    return chrome.tabs.create({ url: CAREERS_PAGE, active: false, windowId: target.id });
  }
  const window = await chrome.windows.create({ url: CAREERS_PAGE, focused: false, state: "minimized" });
  return window.tabs[0];
}

function loaded(tabId) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("the Tesla careers page did not finish loading"));
    }, LOAD_TIMEOUT_MS);
    function listener(id, change) {
      if (id !== tabId || change.status !== "complete") return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function readBoard(tabId) {
  const [injection] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: boardInPage,
    args: [BOARD_PATH],
  });
  const result = injection?.result;
  if (!result) throw new Error("the Tesla tab returned nothing");
  if (result.error) throw new Error(result.error);
  return result;
}

// Runs inside the tesla.com page. It returns every internship (type 3) with its labels resolved,
// and fails rather than return a list that a format change could have emptied.
async function boardInPage(path) {
  try {
    const response = await fetch(path, { headers: { Accept: "application/json, text/plain, */*" } });
    if (!response.ok) return { error: `Tesla board answered HTTP ${response.status}` };
    const state = await response.json();
    const listings = state.listings;
    const lookup = state.lookup ?? {};
    if (!Array.isArray(listings) || !listings.length) return { error: "Tesla board has no listings" };
    if (lookup.types?.["3"] !== "intern") return { error: "Tesla board no longer labels type 3 intern" };
    const internships = listings
      .filter((listing) => listing.y === 3)
      .map((listing) => ({
        id: String(listing.id),
        title: listing.t,
        location: lookup.locations?.[listing.l] ?? null,
        department: lookup.departments?.[listing.dp] ?? null,
      }));
    if (!internships.length) return { error: "Tesla board lists no internships" };
    return { total: listings.length, internships };
  } catch (error) {
    return { error: `Tesla board could not be read: ${error.message}` };
  }
}

// ---- Publishing to GitHub ------------------------------------------------------------------

async function publish({ token, repo, branch }, text, message) {
  const api = (path, init = {}) =>
    fetch(`https://api.github.com/repos/${repo}${path}`, {
      ...init,
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
      },
    });
  const json = async (response, what) => {
    if (!response.ok) throw new Error(`GitHub ${what} answered HTTP ${response.status}`);
    return response.json();
  };

  const branchResponse = await api(`/branches/${encodeURIComponent(branch)}`);
  if (branchResponse.status === 404) {
    // First publish: an orphan branch holding only the feed, so it never carries the code.
    const tree = await json(
      await api("/git/trees", {
        method: "POST",
        body: JSON.stringify({ tree: [{ path: DEFAULTS.path, mode: "100644", type: "blob", content: text }] }),
      }),
      "tree",
    );
    const commit = await json(
      await api("/git/commits", {
        method: "POST",
        body: JSON.stringify({ message, tree: tree.sha, parents: [] }),
      }),
      "commit",
    );
    await json(
      await api("/git/refs", {
        method: "POST",
        body: JSON.stringify({ ref: `refs/heads/${branch}`, sha: commit.sha }),
      }),
      "branch",
    );
    return;
  }
  await json(branchResponse, "branch lookup");
  const existing = await api(`/contents/${DEFAULTS.path}?ref=${encodeURIComponent(branch)}`);
  const sha = existing.status === 404 ? undefined : (await json(existing, "feed lookup")).sha;
  await json(
    await api(`/contents/${DEFAULTS.path}`, {
      method: "PUT",
      body: JSON.stringify({ message, content: base64(text), branch, sha }),
    }),
    "feed update",
  );
}

function base64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let start = 0; start < bytes.length; start += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  }
  return btoa(binary);
}
