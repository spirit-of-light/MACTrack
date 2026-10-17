// Browser glue: fetch each source with the student's own session, parse with parse.js, post to the local server.
// Verified manually in Zen (plan Step 10); the pure logic is tested in test_extension.js.
'use strict';

const SERVER = 'http://127.0.0.1:8000';
const AVENUE = 'https://avenue.cllmcmaster.ca';
const SYLLABUS = 'https://mcmaster.simplesyllabusca.com';
const CROWDMARK = 'https://app.crowdmark.com';
const CHILDSMATH = 'https://www.childsmath.ca';
const MAX_LTI_HOPS = 6;

// AC21/AC22: GET with the browser session; a login page or 401 means the student must sign in.
async function get(url) {
  const r = await fetch(url, { credentials: 'include' });
  if (r.status === 401 || /log-?in|sign-?in/i.test(new URL(r.url).pathname)) throw new LoginNeeded(url);
  if (!r.ok) throw new Error(`${r.status} from ${url}`);
  return r;
}

// A JSON endpoint that answers with HTML is a login page.
async function json(url) {
  const r = await get(url);
  if (!/json/i.test(r.headers.get('Content-Type') || '')) throw new LoginNeeded(url);
  return r.json();
}

// A course without a tool (e.g. a training course) answers non-OK: warn and use `empty`.
// A login page still fails the whole source.
async function optional(url, empty) {
  try {
    return await json(url);
  } catch (e) {
    if (e instanceof LoginNeeded) throw e;
    console.warn(`MacTracker: skipped ${url}: ${e.message}`);
    return empty;
  }
}

function doc(html) {
  return new DOMParser().parseFromString(html, 'text/html');
}

// HTML -> trimmed non-empty text-node lines (AC25).
function textLines(html) {
  const d = doc(html);
  const walk = d.createTreeWalker(d.body, NodeFilter.SHOW_TEXT);
  const lines = [];
  for (let n = walk.nextNode(); n; n = walk.nextNode()) if (n.data.trim()) lines.push(n.data.trim());
  return lines;
}

// Every server request goes through here, with the token from the options page (AC35).
async function post(path, body, ctype = 'application/json') {
  const { token } = await browser.storage.local.get('token');
  const r = await fetch(SERVER + path, {
    method: 'POST',
    headers: { 'Content-Type': ctype, 'X-Token': token || '' },
    body: ctype === 'application/json' ? JSON.stringify(body) : body,
  });
  if (r.ok) return { ok: true, status: r.status, text: '' };
  const text = await r.text();
  console.error(`MacTracker: POST ${path} rejected: ${r.status} ${text}`);
  return { ok: false, status: r.status, text };
}

// A sync the server rejects (400) is replaced by a status-only error so the page shows it, not a stale
// status. One retry only: the error body itself is never retried.
async function ingest(body, retry = true) {
  const r = await post('/api/ingest', body);
  if (r.status === 400 && retry) {
    await ingest({ source: body.source, status: 'error', error: `server rejected sync: ${rejectReason(r.text)}`, items: [] }, false);
  }
  return r.ok;
}

// AC54 (plan Step 8): replay the LTI 1.3 launch, auto-submitting each form, until the
// sso-redirect URL carries the doc code; then read the grading tables from api2.
async function syllabusCategories(topicUrl) {
  const launch = toolLaunchPath(await (await get(AVENUE + topicUrl)).text());
  if (!launch) throw new Error(`no toolLaunch path behind ${topicUrl}`);
  let r = await get(AVENUE + launch);
  for (let hop = 0; hop < MAX_LTI_HOPS && !ssCode(r.url); hop++) {
    const form = doc(await r.text()).querySelector('form');
    if (!form) break;
    const params = new URLSearchParams();
    for (const input of form.querySelectorAll('input[name]')) params.append(input.name, input.value);
    const action = new URL(form.getAttribute('action'), r.url);
    if (![AVENUE, SYLLABUS].includes(action.origin)) throw new Error(`LTI form posts to ${action.origin}, refused`);
    // ponytail: LTI launch forms are always POST; a GET form would need the params in the URL.
    r = await fetch(action, { method: 'POST', body: params, credentials: 'include' });
    // Not get()'s login test: the chain legitimately passes through Simple Syllabus /ui/lti-login.
    if (/\/d2l\/login/i.test(new URL(r.url).pathname)) throw new LoginNeeded(action.href);
    if (!r.ok) throw new Error(`${r.status} from LTI hop ${action.href}`);
  }
  const code = ssCode(r.url);
  if (!code) throw new Error('Simple Syllabus launch did not reach a doc');
  const data = await json(`${SYLLABUS}/api2/doc-full-page-get?code=${encodeURIComponent(code)}`);
  const rows = [];
  for (const c of data.items[0].doc_data.components) {
    for (const tr of doc(c.html || '').querySelectorAll('table tr')) {
      rows.push([...tr.querySelectorAll('th, td')].map((cell) => cell.textContent));
    }
  }
  return ssCategories(rows);
}

function topics(modules) {
  return (modules || []).flatMap((m) => [...(m.Topics || []), ...topics(m.Modules)]);
}

// AC16/AC54: the course outline topic, as a PDF file or a Simple Syllabus LTI link.
async function outline(tocTopics, code) {
  const topic = tocTopics.find((t) => /outline|syllabus/i.test(t.Title || ''));
  if (!topic || !topic.Url) return;
  if (topic.TypeIdentifier === 'File') {
    const r = await get(new URL(topic.Url, AVENUE).href);
    // By the file's extension, else the response type: PDF or DOCX (COMPSCI 1MD3); anything else is skipped.
    const ext = /\.(pdf|docx)(?:$|[?#])/i.exec(topic.Url);
    const sent = (ext ? ext[1] : (r.headers.get('Content-Type') || '')).toLowerCase();
    const ctype = /pdf/.test(sent) ? 'application/pdf'
      : /docx|wordprocessingml/.test(sent) ? 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' : null;
    if (!ctype) return console.warn(`MacTracker: outline is not a PDF or DOCX for ${code}, skipped`);
    await post(`/api/outline?course=${encodeURIComponent(code)}`, await r.arrayBuffer(), ctype);
  } else if (/type=lti/i.test(topic.Url)) {
    await post('/api/outline', { course: code, categories: await syllabusCategories(topic.Url) });
  }
}

async function avenue() {
  const { Courses } = await json(`${AVENUE}/d2l/le/manageCourses/api/mycourses`);
  const items = [];
  const categoryMarks = [];
  const courseInfo = [];
  let cmEntry = null;
  for (const c of Courses.filter((x) => x.IsActive)) { // AC53
    const api = `${AVENUE}/d2l/api/le/1.45/${c.OrgUnitId}`;
    const folders = await optional(`${api}/dropbox/folders/`, []);
    const subs = {};
    for (const f of folders) subs[f.Id] = await optional(`${api}/dropbox/folders/${f.Id}/submissions/`, []);
    const quizzes = [];
    for (let url = `${api}/quizzes/`; url;) { // AC68: follow Next
      const page = await optional(url, { Objects: [], Next: null });
      quizzes.push(...page.Objects);
      url = page.Next ? new URL(page.Next, url).href : null;
    }
    const grades = await optional(`${api}/grades/`, []);
    const values = await optional(`${api}/grades/values/myGradeValues/`, []);
    const r = avenueItems({ code: c.Code, folders, subs, quizzes, grades, values });
    items.push(...r.items.map((i) => ({ ...i, course: c.Code })));
    // A bare {course} entry tells the server this course was synced, so its old marks/info are replaced.
    categoryMarks.push({ course: c.Code }, ...r.categoryMarks.map((m) => ({ ...m, course: c.Code })));
    courseInfo.push({ course: c.Code }, ...r.courseInfo.map((i) => ({ ...i, course: c.Code })));
    // A content or outline failure is logged but never costs the course's items.
    let tocTopics = [];
    try {
      tocTopics = topics((await json(`${AVENUE}/d2l/api/le/1.75/${c.OrgUnitId}/content/toc`)).Modules);
    } catch (e) {
      console.error(`MacTracker: content for ${c.Code}:`, e);
    }
    cmEntry = cmEntry || crowdmarkEntry(tocTopics); // spec 0002 AC24: first hit wins
    await outline(tocTopics, c.Code).catch((e) => console.error(`MacTracker: outline for ${c.Code}:`, e));
  }
  if (cmEntry) await browser.storage.session.set({ crowdmarkEntry: cmEntry });
  return { items, categoryMarks, courseInfo };
}

async function crowdmark() {
  const courses = await json(`${CROWDMARK}/api/v2/student/courses`);
  const items = [];
  for (const c of courses.data) {
    const assignments = await json(`${CROWDMARK}/api/v2/student/assignments?filter[course]=${encodeURIComponent(c.id)}`);
    const results = {};
    for (const a of assignments.data) {
      if (a.attributes['marks-sent-at']) results[a.id] = await json(`${CROWDMARK}/api/v1/student/results/${a.id}`);
    }
    items.push(...crowdmarkItems(assignments, results).map((i) => ({ ...i, course: c.attributes.name })));
  }
  return { items };
}

// AC25/AC71/AC78: GET-only reads of the two childsmath course areas; the marks-page form is never sent.
async function childsmath() {
  const items = [];
  let read = 0;
  for (const area of ['1bStuff', '1zStuff']) { // an area the student is not in is skipped, not fatal
    try {
      const base = `${CHILDSMATH}/childsa/forms/${area}`;
      const admin = textLines(await (await get(`${base}/student_admin.php`)).text());
      if (childsmathLoggedOut(admin)) throw new LoginNeeded(`${base}/student_admin.php`);
      const label = childsmathLabel(admin); // AC86
      if (!label) throw new Error('no course label with a term on student_admin.php');
      const marks = parseMarks(textLines(await (await get(`${base}/marks.php`)).text()));
      items.push(...applyMarks(childsmathItems(admin, label), marks, label));
      read++;
    } catch (e) {
      if (e instanceof LoginNeeded) throw e;
      console.warn(`MacTracker: skipped childsmath ${area}: ${e.message}`);
    }
  }
  if (!read) throw new Error('no childsmath course area could be read');
  return { items };
}

// AC18/AC19/AC20/AC45: every run starts at once and syncs all sources independently.
// Spec 0002 AC1/AC2/AC9: every source is posted first; then the `login needed` ones are renewed.
async function sync() {
  const needed = [];
  const results = await syncAll(FETCHERS, (body) => {
    if (body.status === 'login needed') needed.push(body.source);
    return ingest(body);
  });
  for (const r of results) if (r.status === 'rejected') console.error('MacTracker: sync post failed:', r.reason);
  if (!needed.length) return;
  await renewalReady;
  await renewAll(needed, renewal, RENEW);
  await saveRenewal();
}

browser.action.onClicked.addListener(() => sync());
// Created once: re-creating it on every background start would push the next sync back.
browser.alarms.get('sync').then((a) => a || browser.alarms.create('sync', { periodInMinutes: 180 }));
browser.alarms.onAlarm.addListener((alarm) => alarm.name === 'sync' && sync());

// ---- Spec 0002 session renewal (plan 0002 Step 4) ---------------------------------
const FETCHERS = { avenue, crowdmark, childsmath };
const RENEW_TIMEOUT_MS = 45 * 1000;
// AC27: only tab ids and source names are stored; `busy` stays in memory, so a suspended page never sticks.
const renewal = { fallback: {}, skipped: [], busy: false };
const renewalReady = browser.storage.session.get({ fallback: {}, skipped: [] }).then((s) => Object.assign(renewal, s));
const watching = {}; // renewal tab id -> { source, resolve }; lost with the page, the next sync retries
const signingIn = new Set(); // sources whose fallback sign-in re-sync is running (one per sign-in)

function saveRenewal() {
  return browser.storage.session.set({ fallback: renewal.fallback, skipped: renewal.skipped });
}

// Fallback tab id -> its source, or undefined.
const fallbackSource = (tabId) => Object.keys(renewal.fallback).find((s) => renewal.fallback[s] === tabId);

// AC22/AC23/AC25: a static entry, or the Crowdmark entry avenue() discovered (AC24).
async function entry(source) {
  if (source !== 'crowdmark') return SITES[source].login;
  return (await browser.storage.session.get('crowdmarkEntry')).crowdmarkEntry || null;
}

// AC2/AC3/AC10: an inactive tab on the entry URL; {ok:true} once it reaches a logged-in URL, else {tabId} after 45 s.
async function openRenewalTab(source) {
  const tab = await browser.tabs.create({ url: await entry(source), active: false });
  return new Promise((resolve) => {
    watching[tab.id] = { source, resolve };
    browser.alarms.create(`renew-${tab.id}`, { when: Date.now() + RENEW_TIMEOUT_MS });
  });
}

function endRenewal(tabId, result) {
  const w = watching[tabId];
  if (!w) return;
  delete watching[tabId];
  browser.alarms.clear(`renew-${tabId}`);
  if (result.ok) browser.tabs.remove(tabId);
  w.resolve(result);
}

// AC3/AC4: sync one source again; its posted status decides what renewal does next.
async function rerun(source) {
  let status = 'error';
  await syncAll({ [source]: FETCHERS[source] }, (body) => {
    status = body.status;
    return ingest(body);
  });
  return status;
}

const RENEW = {
  now: () => new Date(),
  entry,
  open: openRenewalTab,
  rerun,
  // AC10/AC11: one notification per sync, replaced by the next; source names only (AC19).
  notify: (sources) => browser.notifications.create('mactracker-login', {
    type: 'basic', title: 'MacTracker: sign-in needed', message: sources.join(', '),
  }),
};

// A tab counts as signed in only once its logged-in page finished loading: closing it mid-load
// (Crowdmark) leaves the session unset.
browser.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (change.status !== 'complete' || !tab.url) return;
  const w = watching[tabId];
  if (w && loggedInUrl(w.source, tab.url)) return endRenewal(tabId, { ok: true });
  // AC13/AC29: the student signed in through a fallback tab; the tab stays open, they are using it.
  await renewalReady;
  const source = fallbackSource(tabId);
  if (!source || signingIn.has(source) || !loggedInUrl(source, tab.url)) return;
  signingIn.add(source);
  try {
    await fallbackSignedIn(source, renewal, RENEW);
    await saveRenewal();
  } finally {
    signingIn.delete(source);
  }
});

// AC12: only this click brings a tab to the front (AC14). Crowdmark with no fallback tab signs in through Avenue.
browser.notifications.onClicked.addListener(async (id) => {
  if (id !== 'mactracker-login') return;
  await renewalReady;
  const source = RENEW_ORDER.find((s) => s in renewal.fallback);
  if (!source) return browser.tabs.create({ url: SITES.avenue.login, active: true });
  const tab = await browser.tabs.update(renewal.fallback[source], { active: true });
  await browser.windows.update(tab.windowId, { focused: true });
});

// AC15: a closed fallback tab is forgotten, so the next sync renews again; a closed renewal tab ends that renewal.
browser.tabs.onRemoved.addListener(async (tabId) => {
  endRenewal(tabId, { closed: true });
  await renewalReady;
  const source = fallbackSource(tabId);
  if (!source) return;
  delete renewal.fallback[source];
  await saveRenewal();
});
browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name.startsWith('renew-')) endRenewal(Number(alarm.name.slice(6)), { tabId: Number(alarm.name.slice(6)) });
});
