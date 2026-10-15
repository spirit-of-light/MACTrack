// Acceptance checks for extension/parse.js (pure, DOM-free helpers). Run: node test_extension.js
// Plain node:assert; prints `FAIL <name>: <message>` per failure and exits 1 if any fail.
// Fixtures are synthetic text lines in the childsmath layout (DOMParser output, one text node per line).
'use strict';
const assert = require('node:assert/strict');
const P = require('./extension/parse.js');

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- childsmath ------------------------------------------------------------------

const COURSE = 'Math 1B03 F26';
const WITH_ANSWERS = ['(with', 'answers', ', and', 'marks', ')'];
const ADMIN_LINES = [
  'Student administration',
  'Assignment #4', '(Due date: 11:59pm on Tuesday October 20)', '3/8',
  'Matlab Lab #2', '(Due date: 9:00am on Monday November 2)', '0/4',
  'Assignment #1', '(Due date: 11:59pm on Tuesday September 22)', '5/8', ...WITH_ANSWERS,
  'Assignment #2', '(Due date: 12:30pm on Friday October 2)', '8/8',
  'Assignment #5', '0/8',
];
const MARKS_LINES = [
  'Marks',
  'Assignment #1:', '7/8',
  'Assignment #2:', '12.5/15',
  'Assignment #4:', 'Marks for the class not posted yet.',
  'Test #1 Mark:', '31/40',
  'Subject:', 'Send',
];

const items = () => P.childsmathItems(ADMIN_LINES, COURSE);
const byTitle = (list) => Object.fromEntries(list.map((i) => [i.title, i]));

test('AC25 childsmath items: titles, course label, unique stable source_id', () => {
  const list = items();
  assert.deepEqual(list.map((i) => i.title), ['Assignment #4', 'Matlab Lab #2', 'Assignment #1', 'Assignment #2', 'Assignment #5']);
  for (const i of list) assert.equal(i.course, COURSE);
  const ids = list.map((i) => i.source_id);
  assert.equal(new Set(ids).size, ids.length, `source_id not unique: ${ids}`);
  assert.deepEqual(items().map((i) => i.source_id), ids, 'source_id not stable across runs');
});

test('AC25 childsmath items: due date and time when the line shows one, none otherwise', () => {
  const t = byTitle(items());
  assert.equal(t['Assignment #4'].due, '2026-10-20T23:59');
  assert.equal(t['Matlab Lab #2'].due, '2026-11-02T09:00');
  assert.equal(t['Assignment #2'].due, '2026-10-02T12:30');
  assert.ok(!t['Assignment #5'].due, `unexpected due: ${t['Assignment #5'].due}`);
});

test('AC59 progress value kept as-is', () => {
  const t = byTitle(items());
  assert.equal(t['Assignment #4'].progress, '3/8');
  assert.equal(t['Matlab Lab #2'].progress, '0/4');
});

test('AC60 "(with answers, and marks)" marks the item closed', () => {
  const t = byTitle(items());
  assert.equal(t['Assignment #1'].closed, true);
  assert.ok(!t['Assignment #4'].closed);
  assert.ok(!t['Assignment #2'].closed);
});

test('AC70 done = closed, or progress a/b with a = b', () => {
  const t = byTitle(items());
  assert.equal(t['Assignment #1'].done, true, 'closed (5/8) not done');
  assert.equal(t['Assignment #2'].done, true, '8/8 not done');
  assert.ok(!t['Assignment #4'].done, '3/8 done');
  assert.ok(!t['Matlab Lab #2'].done, '0/4 done');
  assert.ok(!t['Assignment #5'].done, '0/8 done');
});

test('AC69 dueDate("11:59pm on Tuesday October 20", "F26") is 2026-10-20 23:59', () => {
  assert.equal(P.dueDate('11:59pm on Tuesday October 20', 'F26'), '2026-10-20T23:59');
  assert.equal(P.dueDate('9:00am on Monday November 2', 'F26'), '2026-11-02T09:00');
  assert.equal(P.dueDate('12:30pm on Friday October 2', 'F26'), '2026-10-02T12:30');
});

test('Session default: term letter + two digits gives year 20yy (W27 -> 2027)', () => {
  assert.equal(P.dueDate('11:59pm on Tuesday March 2', 'Math 1B03 W27'), '2027-03-02T23:59');
  assert.equal(P.dueDate('12:00am on Monday May 3', 'S27'), '2027-05-03T00:00');
});

const marked = () => byTitle(P.applyMarks(items(), P.parseMarks(MARKS_LINES)));

test('AC71 marks-page label matched by normalized title; a/b (decimals) gives mark/out_of', () => {
  const t = marked();
  assert.deepEqual([t['Assignment #1'].mark, t['Assignment #1'].out_of], [7, 8]);
  assert.deepEqual([t['Assignment #2'].mark, t['Assignment #2'].out_of], [12.5, 15]);
});

test('AC76 "Marks for the class not posted yet." sets no mark', () => {
  const entry = P.parseMarks(MARKS_LINES).find((m) => /Assignment #4/.test(m.label));
  assert.ok(entry, 'no entry for the not-posted label');
  assert.ok(!('mark' in entry), `mark set: ${JSON.stringify(entry)}`);
  const it = marked()['Assignment #4'];
  assert.ok(!('mark' in it), `item carries a mark key, which would overwrite the stored mark: ${JSON.stringify(it)}`);
});

test('AC71 only label lines followed by a/b or "not posted" become marks', () => {
  const labels = P.parseMarks(MARKS_LINES).map((m) => m.label);
  assert.equal(labels.length, 4, `labels: ${JSON.stringify(labels)}`);
  assert.ok(!labels.some((l) => /Subject/.test(l)), `email form parsed as a mark: ${labels}`);
});

test('AC77 an unmatched "Test #N Mark:" creates a new test item with no due', () => {
  const list = P.applyMarks(items(), P.parseMarks(MARKS_LINES));
  const t = byTitle(list)['Test #1'];
  assert.ok(t, `no Test #1 item in ${JSON.stringify(list.map((i) => i.title))}`);
  assert.equal(t.kind, 'test');
  assert.ok(!t.due, `unexpected due: ${t.due}`);
  assert.deepEqual([t.mark, t.out_of], [31, 40]);
  assert.equal(t.course, COURSE);
  assert.equal(new Set(list.map((i) => i.source_id)).size, list.length, 'new item reuses a source_id');
});

// ---------------------------------------------------------------------------------

let failed = 0;
for (const [name, fn] of tests) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    failed++;
    console.log(`FAIL ${name}: ${e.message}`);
  }
}
process.exit(failed ? 1 : 0);
