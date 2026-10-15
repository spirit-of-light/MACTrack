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
  if (!r.ok) console.error(`MacTracker: POST ${path} rejected: ${r.status} ${await r.text()}`);
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
    // ponytail: LTI launch forms are always POST; a GET form would need the params in the URL.
    r = await fetch(new URL(form.getAttribute('action'), r.url), { method: 'POST', body: params, credentials: 'include' });
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
async function outline(ou, code) {
  const toc = await json(`${AVENUE}/d2l/api/le/1.75/${ou}/content/toc`);
  const topic = topics(toc.Modules).find((t) => /outline|syllabus/i.test(t.Title || ''));
  if (!topic || !topic.Url) return;
  if (topic.TypeIdentifier === 'File') {
    const bytes = await (await get(new URL(topic.Url, AVENUE).href)).arrayBuffer();
    await post(`/api/outline?course=${encodeURIComponent(code)}`, bytes, 'application/pdf');
  } else if (/type=lti/i.test(topic.Url)) {
    await post('/api/outline', { course: code, categories: await syllabusCategories(topic.Url) });
  }
}

async function avenue() {
  const { Courses } = await json(`${AVENUE}/d2l/le/manageCourses/api/mycourses`);
  const items = [];
  const categoryMarks = [];
  for (const c of Courses.filter((x) => x.IsActive)) { // AC53
    const api = `${AVENUE}/d2l/api/le/1.45/${c.OrgUnitId}`;
    const folders = await json(`${api}/dropbox/folders/`);
    const subs = {};
    for (const f of folders) subs[f.Id] = await json(`${api}/dropbox/folders/${f.Id}/submissions/`);
    const quizzes = [];
    for (let url = `${api}/quizzes/`; url;) { // AC68: follow Next
      const page = await json(url);
      quizzes.push(...page.Objects);
      url = page.Next ? new URL(page.Next, url).href : null;
    }
    const grades = await json(`${api}/grades/`);
    const values = await json(`${api}/grades/values/myGradeValues/`);
    const r = avenueItems({ folders, subs, quizzes, grades, values });
    items.push(...r.items.map((i) => ({ ...i, course: c.Code })));
    categoryMarks.push(...r.categoryMarks.map((m) => ({ ...m, course: c.Code })));
    // An outline failure is logged but never costs the course's items.
    await outline(c.OrgUnitId, c.Code).catch((e) => console.error(`MacTracker: outline for ${c.Code}:`, e));
  }
  return { items, categoryMarks };
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
  for (const area of ['1bStuff', '1zStuff']) {
    const base = `${CHILDSMATH}/childsa/forms/${area}`;
    const admin = textLines(await (await get(`${base}/student_admin.php`)).text());
    const m = admin.map((l) => /([A-Za-z]+)\s*(\d[A-Za-z0-9]{3})\b.*?\b([FWS]\d{2})\b/.exec(l)).find(Boolean);
    if (!m) throw new Error(`no course label with a term on ${area}/student_admin.php`);
    const label = `${m[1]} ${m[2]} ${m[3]}`;
    const marks = parseMarks(textLines(await (await get(`${base}/marks.php`)).text()));
    items.push(...applyMarks(childsmathItems(admin, label), marks, label));
  }
  return { items };
}

// AC18/AC19/AC20/AC45: every run starts at once and syncs all sources independently.
async function sync() {
  const results = await syncAll({ avenue, crowdmark, childsmath }, (body) => post('/api/ingest', body));
  for (const r of results) if (r.status === 'rejected') console.error('MacTracker: sync post failed:', r.reason);
}

browser.action.onClicked.addListener(() => sync());
browser.alarms.create('sync', { periodInMinutes: 180 });
browser.alarms.onAlarm.addListener((alarm) => alarm.name === 'sync' && sync());
