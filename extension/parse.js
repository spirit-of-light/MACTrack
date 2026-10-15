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
function childsmathItems(lines, courseLabel) {
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
function avenueItems({ folders = [], subs = {}, quizzes = [], grades = [], values = [] }) {
  const value = {};
  const categoryMarks = [];
  for (const v of values) {
    if (v.GradeObjectTypeName === 'Numeric') value[String(v.GradeObjectIdentifier)] = v;
    else if (v.GradeObjectTypeName === 'Category' && v.PointsDenominator) {
      categoryMarks.push({ name: v.GradeObjectName, value: (v.PointsNumerator / v.PointsDenominator) * 100 });
    }
  }
  const gradeRow = (g) => {
    const v = value[String(g.Id)];
    return { source_id: `grade:${g.Id}`, title: g.Name, mark: v ? v.PointsNumerator : null,
      out_of: v ? v.PointsDenominator : g.MaxPoints };
  };
  const fill = (row, g) => { // AC67/AC81: the gradebook mark fills only an empty mark
    const gr = gradeRow(g);
    if (row.mark == null && gr.mark != null) Object.assign(row, { mark: gr.mark, out_of: gr.out_of });
    if (row.out_of == null) row.out_of = gr.out_of;
  };
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
  return { items, categoryMarks };
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
    } catch (e) {
      body = e instanceof LoginNeeded || (e && e.name === 'LoginNeeded')
        ? { source, status: 'login needed', items: [] }
        : { source, status: 'error', error: String((e && e.message) || e), items: [] };
    }
    await post(body);
  }));
}

if (typeof module !== 'undefined') {
  module.exports = { dueDate, childsmathItems, parseMarks, applyMarks, ssCategories, ssCode, toolLaunchPath,
    avenueItems, crowdmarkItems, syncAll, LoginNeeded };
}
