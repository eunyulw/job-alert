// Browser relay: reads career boards that answer only a real browser, in this Chrome, and
// publishes them to the job-alert repository, where the GitHub monitor reads them (Tesla as the
// `tesla-relay` source, the others as `browser-relay` sources).
//
// Each board is read in a tab of this Chrome profile: an open tab on the board's site is reused,
// otherwise a background tab is opened (never focused) and closed again. Inside it the page's own
// same-origin requests are repeated; nothing else on the site is requested. A challenge this
// browser has not passed fails the read; nothing here solves one.
//
// Each feed is committed to the `tesla-feed` branch (named for the first board) only when its list
// changes, or every six hours as a heartbeat, so the monitor can tell a quiet board from a browser
// that stopped running.

const ALARM = "tesla-relay";
const PERIOD_MINUTES = 10;
const HEARTBEAT_MS = 6 * 60 * 60 * 1000;
const LOAD_TIMEOUT_MS = 45_000;
const SETTLE_MS = 3_000;
const DEFAULTS = { repo: "eunyulw/job-alert", branch: "tesla-feed" };

// Tesla keeps its original feed shape and storage keys, so the `tesla-relay` source and an
// installed copy's state carry over unchanged.
const BOARDS = [
  {
    name: "tesla",
    path: "tesla.json",
    site: "https://www.tesla.com/*",
    page: "https://www.tesla.com/careers/search/?type=3&site=US",
    everyMs: 0,
    read: (tabId) => inTab(tabId, teslaInPage, ["/cua-api/apps/careers/state"]),
    feed: (board, now) => ({
      schema: 1,
      generated_at: new Date(now).toISOString(),
      board_total: board.total,
      internships: board.jobs,
    }),
    keys: { fingerprint: "fingerprint", publishedAt: "publishedAt" },
  },
  {
    name: "parker",
    path: "parker.json",
    site: "https://parkercareers.ttcportals.com/*",
    page: "https://parkercareers.ttcportals.com/search/students/jobs/in/country/united-states",
    everyMs: 30 * 60 * 1000,
    read: (tabId) => inTab(tabId, ttcPortalInPage, ["/search/students/jobs/in/country/united-states"]),
    feed: (board, now) => ({
      schema: 1,
      board: "parker",
      generated_at: new Date(now).toISOString(),
      declared_total: board.total,
      jobs: board.jobs,
    }),
    keys: { fingerprint: "fingerprint:parker", publishedAt: "publishedAt:parker" },
  },
];

chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) run();
});
chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  if (message !== "run-now") return false;
  run({ force: true }).then(() => reply(true));
  return true; // the reply is sent asynchronously
});

function schedule() {
  chrome.alarms.create(ALARM, { delayInMinutes: 1, periodInMinutes: PERIOD_MINUTES });
}

let running = null;

function run({ force = false } = {}) {
  running ??= relayAll(force).finally(() => {
    running = null;
  });
  return running;
}

async function relayAll(force) {
  const settings = { ...DEFAULTS, ...(await chrome.storage.local.get(["token", "repo", "branch"])) };
  const { outcomes = {}, lastReadAt = {} } = await chrome.storage.local.get(["outcomes", "lastReadAt"]);
  for (const board of BOARDS) {
    const now = Date.now();
    if (!force && board.everyMs && now - (lastReadAt[board.name] ?? 0) < board.everyMs) continue;
    lastReadAt[board.name] = now;
    try {
      if (!settings.token) throw new Error("no GitHub token; open the extension's options");
      outcomes[board.name] = await relay(board, settings);
    } catch (error) {
      outcomes[board.name] = `error: ${error.message}`;
    }
  }
  await chrome.storage.local.set({
    outcomes,
    lastReadAt,
    lastRunAt: Date.now(),
    lastOutcome: BOARDS.map((board) => `${board.name}: ${outcomes[board.name] ?? "not run yet"}`).join("\n"),
  });
}

async function relay(board, settings) {
  const read = await withTab(board, board.read);
  const jobs = read.jobs.sort((a, b) => a.id.localeCompare(b.id));
  const fingerprint = JSON.stringify(jobs);
  const state = await chrome.storage.local.get([board.keys.fingerprint, board.keys.publishedAt]);
  const now = Date.now();
  const publishedAt = state[board.keys.publishedAt];
  const due = !publishedAt || now - publishedAt >= HEARTBEAT_MS;
  if (fingerprint === state[board.keys.fingerprint] && !due) return `unchanged: ${jobs.length} postings`;
  const text = JSON.stringify(board.feed({ total: read.total, jobs }, now), null, 1) + "\n";
  const changed = fingerprint === state[board.keys.fingerprint] ? "heartbeat" : "postings changed";
  await publish(settings, board.path, text, `${board.name} feed: ${changed}`);
  await chrome.storage.local.set({ [board.keys.fingerprint]: fingerprint, [board.keys.publishedAt]: now });
  return `published (${changed}): ${jobs.length} postings`;
}

// ---- Reading a board in a tab -----------------------------------------------------------------

async function withTab(board, task) {
  const open = await chrome.tabs.query({ url: board.site });
  const usable = open.find((tab) => tab.status === "complete" && !tab.discarded);
  if (usable) return task(usable.id);
  const tab = await openBackgroundTab(board.page);
  try {
    await loaded(tab.id, board.name);
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    return await task(tab.id);
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function openBackgroundTab(url) {
  const windows = await chrome.windows.getAll({ windowTypes: ["normal"] });
  if (windows.length) {
    const target = windows.find((window) => window.focused) ?? windows[0];
    return chrome.tabs.create({ url, active: false, windowId: target.id });
  }
  const window = await chrome.windows.create({ url, focused: false, state: "minimized" });
  return window.tabs[0];
}

function loaded(tabId, name) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error(`the ${name} page did not finish loading`));
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

async function inTab(tabId, func, args) {
  const [injection] = await chrome.scripting.executeScript({ target: { tabId }, world: "MAIN", func, args });
  const result = injection?.result;
  if (!result) throw new Error("the tab returned nothing");
  if (result.error) throw new Error(result.error);
  return result;
}

// Runs inside the tesla.com page. It returns every internship (type 3) with its labels resolved,
// and fails rather than return a list that a format change could have emptied.
async function teslaInPage(path) {
  try {
    const response = await fetch(path, { headers: { Accept: "application/json, text/plain, */*" } });
    if (!response.ok) return { error: `Tesla board answered HTTP ${response.status}` };
    const state = await response.json();
    const listings = state.listings;
    const lookup = state.lookup ?? {};
    if (!Array.isArray(listings) || !listings.length) return { error: "Tesla board has no listings" };
    if (lookup.types?.["3"] !== "intern") return { error: "Tesla board no longer labels type 3 intern" };
    const jobs = listings
      .filter((listing) => listing.y === 3)
      .map((listing) => ({
        id: String(listing.id),
        title: listing.t,
        location: lookup.locations?.[listing.l] ?? null,
        department: lookup.departments?.[listing.dp] ?? null,
      }));
    if (!jobs.length) return { error: "Tesla board lists no internships" };
    return { total: listings.length, jobs };
  } catch (error) {
    return { error: `Tesla board could not be read: ${error.message}` };
  }
}

// Runs inside a TTC portal page (Parker Hannifin's). It walks the search's pages, 25 rows each,
// and returns them only when the unique postings read equal the "Viewing 1-25 of N" total.
async function ttcPortalInPage(path) {
  try {
    const read = async (page) => {
      const response = await fetch(page > 1 ? `${path}?page=${page}` : path);
      if (!response.ok) throw new Error(`the portal answered HTTP ${response.status}`);
      return new DOMParser().parseFromString(await response.text(), "text/html");
    };
    const first = await read(1);
    const match = first.body.textContent.match(/Viewing\s+\d+\s*-\s*\d+\s+of\s+(\d+)/);
    if (!match) return { error: "the portal page declares no result total" };
    const total = Number(match[1]);
    const jobs = new Map();
    for (let page = 1, doc = first; jobs.size < total && page <= 40; page += 1) {
      if (page > 1) doc = await read(page);
      const rows = [...doc.querySelectorAll("div.row")].filter((row) =>
        row.querySelector(':scope > div > p > a[href*="/jobs/"]'),
      );
      if (!rows.length) break;
      for (const row of rows) {
        const link = row.querySelector('a[href*="/jobs/"]');
        const id = (link.getAttribute("href").match(/\/jobs\/(\d+)/) ?? [])[1];
        if (!id || jobs.has(id)) continue;
        const cells = row.querySelectorAll(":scope > div");
        const text = (cell) => (cell ? cell.textContent.replace(/\s+/g, " ").trim() : null);
        const posted = text(cells[2])?.replace(/^Date Posted:\s*/, "") ?? null;
        jobs.set(id, {
          id,
          title: link.textContent.replace(/\s+/g, " ").trim(),
          location: text(cells[1])?.replace(/^Location:\s*/, "") || null,
          url: new URL(link.getAttribute("href"), location.origin).href,
          posted: posted && !Number.isNaN(Date.parse(posted)) ? new Date(`${posted} UTC`).toISOString() : null,
        });
      }
    }
    if (jobs.size !== total) return { error: `read ${jobs.size} postings but the portal declares ${total}` };
    return { total, jobs: [...jobs.values()] };
  } catch (error) {
    return { error: `the portal could not be read: ${error.message}` };
  }
}

// ---- Publishing to GitHub ------------------------------------------------------------------

async function publish({ token, repo, branch }, path, text, message) {
  const api = (route, init = {}) =>
    fetch(`https://api.github.com/repos/${repo}${route}`, {
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
    // First publish: an orphan branch holding only the feeds, so it never carries the code.
    const tree = await json(
      await api("/git/trees", {
        method: "POST",
        body: JSON.stringify({ tree: [{ path, mode: "100644", type: "blob", content: text }] }),
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
  const existing = await api(`/contents/${path}?ref=${encodeURIComponent(branch)}`);
  const sha = existing.status === 404 ? undefined : (await json(existing, "feed lookup")).sha;
  await json(
    await api(`/contents/${path}`, {
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
