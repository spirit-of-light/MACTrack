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

if (typeof module !== 'undefined') module.exports = { dueDate, childsmathItems, parseMarks, applyMarks };
