// Pure, DOM-free helpers for background.js. Loaded as a background script; node `require`s it for tests.
'use strict';

const MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
  'september', 'october', 'november', 'december'];
const TITLE_LINE = /^[A-Za-z][A-Za-z0-9 ]*#\d+$/; // "Assignment #4", "Matlab Lab #2"
const FRACTION = /^(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/;
const NOT_POSTED = 'Marks for the class not posted yet.';

// ponytail: JS copy of server.norm_title (AC36) for childsmath label matching only (AC71)
const normTitle = (s) => s.toLowerCase().replace(/[^\w\s]|_/g, ' ').replace(/\s+/g, ' ').trim();

// AC71: "Test #1 Mark:" -> "Test #1"
const labelTitle = (label) => label.replace(/:\s*$/, '').replace(/\s*Mark\s*$/i, '').replace(/:\s*$/, '').trim();

// AC69: "11:59pm on Tuesday October 20" + term "F26" -> "2026-10-20T23:59" (naive local), else null.
// Session default: any term letter F/W/S + two digits gives year 20yy.
function dueDate(s, term) {
  const m = /(\d{1,2}):(\d{2})\s*(am|pm)\s+on\s+[A-Za-z]+\s+([A-Za-z]+)\s+(\d{1,2})/i.exec(s);
  const t = /\b[FWS](\d{2})\b/i.exec(term || '');
  const month = m ? MONTHS.indexOf(m[4].toLowerCase()) : -1;
  if (!m || !t || month < 0) return null;
  const hour = (Number(m[1]) % 12) + (m[3].toLowerCase() === 'pm' ? 12 : 0);
  const pad = (n) => String(n).padStart(2, '0');
  return `20${t[1]}-${pad(month + 1)}-${pad(m[5])}T${pad(hour)}:${m[2]}`;
}

// AC25/AC59/AC60/AC70: student_admin text lines -> items. A block is a title line plus the
// lines up to the next title.
// AC86: the area's course is the last course-label line ("Math 1B03 F26") before the first item title.
function childsmathLabel(lines) {
  let label = null;
  for (const line of lines.map((l) => l.trim())) {
    if (TITLE_LINE.test(line)) break;
    if (/\b[A-Za-z]+\s*\d[A-Za-z0-9]{3}\b.*\b[FWS]\d{2}\b/.test(line)) label = line;
  }
  return label;
}

function childsmathItems(lines, courseLabel = childsmathLabel(lines)) {
  const blocks = [];
  for (const line of lines.map((l) => l.trim())) {
    if (TITLE_LINE.test(line)) blocks.push([line]);
    else if (blocks.length) blocks[blocks.length - 1].push(line);
  }
  const seen = {};
  return blocks.map(([title, ...rest]) => {
    seen[title] = (seen[title] || 0) + 1;
    const text = rest.join(' ');
    const due = /\(Due date:\s*([^)]*)\)/.exec(text);
    const progress = rest.find((l) => FRACTION.test(l)) || null;
    const closed = rest.join('').replace(/\s+/g, '').toLowerCase().includes('(withanswers,andmarks)');
    const p = progress && FRACTION.exec(progress);
    const item = {
      source_id: `${courseLabel}:${title}${seen[title] > 1 ? `#${seen[title]}` : ''}`,
      course: courseLabel, title, due: due ? dueDate(due[1], courseLabel) : null, progress,
    };
    if (closed) item.closed = true;
    if (closed || (p && Number(p[1]) === Number(p[2]))) item.done = true;
    return item;
  });
}

// AC71/AC76: marks text lines -> [{label, mark, out_of}] or [{label}] when not posted yet.
function parseMarks(lines) {
  const out = [];
  const ls = lines.map((l) => l.trim());
  for (let i = 0; i + 1 < ls.length; i++) {
    if (!ls[i].endsWith(':')) continue;
    const f = FRACTION.exec(ls[i + 1]);
    if (f) out.push({ label: ls[i], mark: Number(f[1]), out_of: Number(f[2]) });
    else if (ls[i + 1] === NOT_POSTED) out.push({ label: ls[i] });
  }
  return out;
}

// AC71/AC76/AC77: copy of items with marks applied; an unmatched "Test #N" becomes a new test item.
function applyMarks(items, marks, courseLabel = items[0] && items[0].course) {
  const out = items.map((i) => ({ ...i }));
  for (const m of marks) {
    const title = labelTitle(m.label);
    let it = out.find((i) => normTitle(i.title) === normTitle(title));
    if (!it && /^test\s*#\d+$/i.test(title)) {
      it = { source_id: `${courseLabel}:${title}`, course: courseLabel, title, kind: 'test' };
      out.push(it);
    }
    if (it && 'mark' in m) Object.assign(it, { mark: m.mark, out_of: m.out_of });
  }
  return out;
}

// ---- Simple Syllabus (AC54/AC55) ----

// Grading-table rows (cell texts) -> [{name, weights: [raw % strings]}]; rows without a % cell dropped.
function ssCategories(rows) {
  return rows
    .map((cells) => cells.map((c) => String(c).trim()))
    .filter((cells) => cells.slice(1).some((c) => c.includes('%')))
    .map(([name, ...rest]) => ({ name, weights: rest.filter((c) => c.includes('%')) }));
}

// Final sso-redirect URL -> doc code ("/doc/" stripped), else null.
function ssCode(url) {
  const doc = new URL(url).searchParams.get('doc_url');
  return doc ? doc.replace(/^\/doc\//, '') : null;
}

// componentLaunch HTML -> the toolLaunch path (HTML-escaped &amp; undone), else null.
function toolLaunchPath(html) {
  const m = /["'](\/d2l\/le\/lti\/[^"']*toolLaunch[^"']*)["']/.exec(html);
  return m ? m[1].replace(/&amp;/g, '&') : null;
}

// ---- Avenue (AC23, AC52, AC65-AC68, AC72, AC79, AC81) ----

// {folders, subs: {folderId: [entry]}, quizzes, grades, values} -> {items, categoryMarks}.
// Items carry no course (the caller adds it) and never a weight (AC52).
// AC85: Avenue Code suffix _2YYT (T: 9 Fall, 1 Winter, 5 Spring/Summer) -> term start 'YYYY-MM-DD', else null.
function termStart(code) {
  const m = /_2(\d{2})([159])$/.exec(code || '');
  return m ? `20${m[1]}-${{ 9: '09', 1: '01', 5: '05' }[m[2]]}-01` : null;
}

function avenueItems({ code, folders = [], subs = {}, quizzes = [], grades: allGrades = [], values = [] }) {
  const value = {};
  const categoryMarks = [];
  for (const v of values) {
    if (v.GradeObjectTypeName !== 'Category') value[String(v.GradeObjectIdentifier)] = v;
    else if (v.PointsDenominator) {
      categoryMarks.push({ name: v.GradeObjectName, value: (v.PointsNumerator / v.PointsDenominator) * 100 });
    }
  }
  const gradeRow = (g) => {
    const v = value[String(g.Id)];
    return { source_id: `grade:${g.Id}`, title: g.Name, mark: v ? v.PointsNumerator : null,
      out_of: v ? v.PointsDenominator : g.MaxPoints, done: !!v && v.PointsNumerator != null };
  };
  const fill = (row, g) => { // AC67/AC81: the gradebook mark fills only an empty mark
    const gr = gradeRow(g);
    if (row.mark == null && gr.mark != null) Object.assign(row, { mark: gr.mark, out_of: gr.out_of });
    if (row.out_of == null) row.out_of = gr.out_of;
  };
  // AC91: non-Numeric gradebook items (e.g. Text: lab room) are course info, not items.
  const numeric = (g) => g.GradeType == null || g.GradeType === 'Numeric';
  const grades = allGrades.filter(numeric);
  const courseInfo = allGrades.filter((g) => !numeric(g)).map((g) => {
    const v = value[String(g.Id)];
    return { name: g.Name, value: (v && v.DisplayedGrade) || '' };
  });
  const used = new Set();
  const byId = Object.fromEntries(grades.map((g) => [String(g.Id), g]));

  const items = folders.map((f) => {
    const entries = subs[f.Id] || [];
    const graded = entries.find((e) => e.Feedback && e.Feedback.IsGraded);
    const row = { source_id: `dropbox:${f.Id}`, title: f.Name, due: f.DueDate,
      out_of: f.Assessment ? f.Assessment.ScoreDenominator : null,
      done: entries.some((e) => (e.Submissions || []).length > 0),
      mark: graded ? graded.Feedback.Score : null };
    const g = f.GradeItemId != null && byId[String(f.GradeItemId)];
    if (g) { fill(row, g); used.add(g.Id); }
    row.done = row.done || row.mark != null; // AC87
    return row;
  });

  for (const q of quizzes) {
    const row = { source_id: `quiz:${q.QuizId}`, title: q.Name, due: q.DueDate, mark: null, out_of: null };
    // AC79: GradeItemId when present; normalized title only when it is null.
    const g = q.GradeItemId != null ? byId[String(q.GradeItemId)]
      : grades.find((x) => !used.has(x.Id) && normTitle(x.Name) === normTitle(q.Name));
    if (g) { fill(row, g); used.add(g.Id); }
    row.done = row.mark != null;
    items.push(row);
  }

  for (const g of grades) if (!used.has(g.Id)) items.push(gradeRow(g)); // AC66
  // AC85: drop dropbox/quiz rows due before the course's term (gradebook rows have no due).
  const start = termStart(code);
  const current = (i) => !start || !i.due || new Date(i.due) >= new Date(`${start}T00:00:00`);
  return { items: items.filter(current), categoryMarks, courseInfo };
}

// ---- Crowdmark (AC24) ----

// assignments JSON:API doc + {assignmentId: results doc} -> items.
function crowdmarkItems(assignments, resultsById = {}) {
  const masters = Object.fromEntries((assignments.included || []).map((x) => [x.id, x.attributes || {}]));
  return (assignments.data || []).map((a) => {
    const at = a.attributes || {};
    const rel = a.relationships && a.relationships['exam-master'] && a.relationships['exam-master'].data;
    const em = (rel && masters[rel.id]) || {};
    const res = at['marks-sent-at'] && resultsById[a.id];
    const r = res && res.data && res.data.attributes;
    return { source_id: `assignment:${a.id}`, title: em.title, due: at.due, done: !!at['submitted-at'],
      mark: r ? r['total-after-penalty'] : null, out_of: r ? em['total-points'] : null };
  });
}

// ---- sync (AC20, AC21) ----

class LoginNeeded extends Error {
  constructor(source) { super(`login needed: ${source}`); this.name = 'LoginNeeded'; }
}

// Runs every fetcher in isolation and posts one body per source; one failure never stops the others.
async function syncAll(fetchers, post) {
  return Promise.allSettled(Object.entries(fetchers).map(async ([source, run]) => {
    let body;
    try {
      const r = (await run()) || {};
      body = { source, status: 'ok', items: r.items || [] };
      if (r.categoryMarks && r.categoryMarks.length) body.category_marks = r.categoryMarks;
      if (r.courseInfo && r.courseInfo.length) body.course_info = r.courseInfo;
    } catch (e) {
      body = e instanceof LoginNeeded || (e && e.name === 'LoginNeeded')
        ? { source, status: 'login needed', items: [] }
        : { source, status: 'error', error: String((e && e.message) || e), items: [] };
    }
    await post(body);
  }));
}

// Spec 0002 AC22/AC23/AC25: renewal entry URL per source and its "logged in" tab-URL test.
// Crowdmark has no static entry: it is discovered from an Avenue TOC topic (crowdmarkEntry, AC24).
const SITES = {
  avenue: {
    login: 'https://avenue.mcmaster.ca/login.php?target=%2Fd2l%2Fhome',
    origin: 'https://avenue.cllmcmaster.ca',
    in: (path) => path.startsWith('/d2l/') && !path.startsWith('/d2l/login'),
  },
  crowdmark: { login: null, origin: 'https://app.crowdmark.com', in: (path) => path.startsWith('/student/') },
  childsmath: {
    login: 'https://login.microsoftonline.com/44376307-b429-42ad-8c25-28cd496f4772/oauth2/v2.0/authorize?client_id=9132e1a8-fef6-4c34-bddb-de7608a2381a&response_type=code&response_mode=query&scope=https%3A%2F%2Fgraph.microsoft.com%2FUser.Read',
    origin: 'https://www.childsmath.ca',
    in: (path) => path === '/childsa/forms/main_intro.php',
  },
};

function loggedInUrl(source, url) {
  const site = SITES[source];
  let u;
  try { u = new URL(url); } catch { return false; }
  return Boolean(site) && u.origin === site.origin && site.in(u.pathname);
}

// AC6: quiet window is local 23:00 inclusive to 06:00 exclusive.
const quietHour = (date) => date.getHours() >= 23 || date.getHours() < 6;

// AC24: first topic titled like Crowdmark whose Url is an LTI launch, as an absolute Avenue URL.
function crowdmarkEntry(topics) {
  const t = (topics || []).find((x) => /crowdmark/i.test(x.Title || '') && /type=lti/.test(x.Url || ''));
  return t ? SITES.avenue.origin + t.Url : null;
}

const RENEW_ORDER = ['avenue', 'crowdmark', 'childsmath']; // AC26: Crowdmark signs in through Avenue

// Plan 0002 Step 3: renew login-needed sources one at a time in background tabs.
// d = { now() -> Date, entry(source) -> URL | null, open(source) -> {ok:true} | {tabId},
//       rerun(source) -> posted status, notify(sources) }; state = { fallback: {source: tabId}, skipped, busy }.
async function renewAll(sources, state, d) {
  if (state.busy) return; // an alarm sync and a click sync never open two tabs for one source
  state.busy = true;
  const affected = [];
  const skipped = [];
  let fellBack = false;
  try {
    for (const source of RENEW_ORDER.filter((s) => sources.includes(s))) {
      if (source in state.fallback) continue; // AC14: its fallback tab is still open
      if (quietHour(d.now())) continue; // AC6: checked when this renewal would start
      if (fellBack) { skipped.push(source); affected.push(source); continue; } // AC11
      // AC30: Crowdmark signs in through Avenue; retried once Avenue's fallback sign-in succeeds
      if (source === 'crowdmark' && 'avenue' in state.fallback) { skipped.push(source); continue; }
      if (!(await d.entry(source))) { affected.push(source); continue; } // Crowdmark not yet discovered: not an IdP failure
      const r = await d.open(source);
      if (r && r.ok) await d.rerun(source); // AC3; a rerun never renews again (AC5)
      else { state.fallback[source] = r.tabId; affected.push(source); fellBack = true; } // AC10
    }
    state.skipped = skipped;
    if (affected.length) await d.notify(affected);
  } finally {
    state.busy = false;
  }
}

// AC13/AC11: the student signed in through a fallback tab. Not ok yet: keep tracking that tab, retry nothing.
async function fallbackSignedIn(source, state, d) {
  if ((await d.rerun(source)) !== 'ok') return;
  delete state.fallback[source];
  const skipped = state.skipped;
  state.skipped = [];
  await renewAll(skipped, state, d);
}

if (typeof module !== 'undefined') {
  module.exports = { dueDate, childsmathLabel, childsmathItems, termStart, parseMarks, applyMarks, ssCategories, ssCode, toolLaunchPath,
    avenueItems, crowdmarkItems, syncAll, LoginNeeded, SITES, loggedInUrl, quietHour, crowdmarkEntry, renewAll, fallbackSignedIn };
}
