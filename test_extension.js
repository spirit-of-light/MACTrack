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

// ---- Simple Syllabus (AC54, AC55 client side) --------------------------------------

test('AC55 ssCategories: % rows become {name, weights:[raw strings]}; other rows dropped', () => {
  const rows = [
    ['Component', 'Scheme A', 'Scheme B'],
    ['Term Test 1', '10%', '15%'],
    ['Final Exam', '40-55%**', '45%'],
    ['Notes', 'see the policy below'],
  ];
  assert.deepEqual(P.ssCategories(rows), [
    { name: 'Term Test 1', weights: ['10%', '15%'] },
    { name: 'Final Exam', weights: ['40-55%**', '45%'] },
  ]);
});

test('AC54 ssCode: doc code from the sso-redirect URL, "/doc/" stripped', () => {
  const base = 'https://mcmaster.simplesyllabusca.com/en-US/sso-redirect?doc_url=';
  assert.equal(P.ssCode(base + '/doc/abc123XYZ'), 'abc123XYZ');
  assert.equal(P.ssCode(base + '%2Fdoc%2Fq9w8e7'), 'q9w8e7');
  assert.ok(!P.ssCode('https://mcmaster.simplesyllabusca.com/ui/lti-login'), 'code from a URL without doc_url');
});

test('AC54 toolLaunchPath: escaped toolLaunch path found in componentLaunch HTML, &amp; unescaped', () => {
  const html = '<html><script>var x = "/d2l/home"; launch(\'/d2l/le/lti/123/toolLaunch/45/678?resourceCode=MCM-SS-1&amp;ou=123\');</script></html>';
  assert.equal(P.toolLaunchPath(html), '/d2l/le/lti/123/toolLaunch/45/678?resourceCode=MCM-SS-1&ou=123');
  assert.ok(!P.toolLaunchPath('<html><body>no launch here</body></html>'));
});

// ---- Avenue (AC23, AC52, AC65-AC68, AC72, AC79, AC81) ------------------------------

const AVENUE = {
  folders: [
    { Id: 11, Name: 'Assignment 1', DueDate: '2026-10-01T03:59:00.000Z', GradeItemId: 101, Assessment: { ScoreDenominator: 20 } },
    { Id: 12, Name: 'Assignment 2', DueDate: '2026-10-15T03:59:00.000Z', GradeItemId: 102, Assessment: { ScoreDenominator: 20 } },
    { Id: 13, Name: 'Lab Report', DueDate: null, GradeItemId: null, Assessment: { ScoreDenominator: 10 } },
  ],
  subs: {
    11: [{ Status: 2, Submissions: [{ SubmissionDate: '2026-09-30T20:00:00.000Z' }], Feedback: { Score: 18, IsGraded: true }, CompletionDate: null }],
    12: [{ Status: 1, Submissions: [{ SubmissionDate: '2026-10-14T20:00:00.000Z' }], Feedback: { Score: 5, IsGraded: false }, CompletionDate: null }],
    13: [{ Status: 0, Submissions: [], Feedback: null, CompletionDate: null }],
  },
  quizzes: [
    { QuizId: 501, Name: 'Quiz 1', DueDate: '2026-10-05T03:59:00.000Z', GradeItemId: 201 },
    { QuizId: 502, Name: 'Quiz 2 - Kinetics', DueDate: '2026-10-12T03:59:00.000Z', GradeItemId: null },
    { QuizId: 504, Name: 'Quiz 4', DueDate: null, GradeItemId: 204 },
  ],
  grades: [
    { Id: 101, Name: 'Assignment 1', MaxPoints: 20, Weight: 5, GradeType: 'Numeric' },
    { Id: 102, Name: 'Assignment 2', MaxPoints: 20, Weight: 5, GradeType: 'Numeric' },
    { Id: 201, Name: 'Quiz 1', MaxPoints: 10, Weight: 2, GradeType: 'Numeric' },
    { Id: 202, Name: 'Quiz 2: Kinetics', MaxPoints: 10, Weight: 2, GradeType: 'Numeric' },
    { Id: 204, Name: 'Quiz 4 (practice)', MaxPoints: 10, Weight: 0, GradeType: 'Numeric' },
    { Id: 206, Name: 'Quiz 4', MaxPoints: 10, Weight: 3, GradeType: 'Numeric' },
    { Id: 301, Name: 'Participation Bonus', MaxPoints: 5, Weight: 1, GradeType: 'Numeric' },
    { Id: 302, Name: 'Midterm', MaxPoints: 50, Weight: 25, GradeType: 'Numeric' },
  ],
  values: [
    { GradeObjectIdentifier: '101', GradeObjectName: 'Assignment 1', GradeObjectTypeName: 'Numeric', PointsNumerator: 17, PointsDenominator: 20 },
    { GradeObjectIdentifier: '102', GradeObjectName: 'Assignment 2', GradeObjectTypeName: 'Numeric', PointsNumerator: 15, PointsDenominator: 20 },
    { GradeObjectIdentifier: '201', GradeObjectName: 'Quiz 1', GradeObjectTypeName: 'Numeric', PointsNumerator: 8, PointsDenominator: 10 },
    { GradeObjectIdentifier: '202', GradeObjectName: 'Quiz 2: Kinetics', GradeObjectTypeName: 'Numeric', PointsNumerator: 9, PointsDenominator: 10 },
    { GradeObjectIdentifier: '206', GradeObjectName: 'Quiz 4', GradeObjectTypeName: 'Numeric', PointsNumerator: 7, PointsDenominator: 10 },
    { GradeObjectIdentifier: '302', GradeObjectName: 'Midterm', GradeObjectTypeName: 'Numeric', PointsNumerator: 41, PointsDenominator: 50 },
    { GradeObjectIdentifier: '9001', GradeObjectName: 'Online Homework', GradeObjectTypeName: 'Category', PointsNumerator: 45, PointsDenominator: 50 },
  ],
};
const avenue = () => P.avenueItems(JSON.parse(JSON.stringify(AVENUE)));
const bySid = (list) => Object.fromEntries(list.map((i) => [i.source_id, i]));

test('AC23 dropbox folders: dropbox:<Id>, title, due, out_of, done from submissions, graded mark', () => {
  const a = bySid(avenue().items);
  const d11 = a['dropbox:11'];
  assert.ok(d11, `no dropbox:11 in ${Object.keys(a)}`);
  assert.deepEqual([d11.title, d11.due, d11.out_of, d11.done, d11.mark], ['Assignment 1', '2026-10-01T03:59:00.000Z', 20, true, 18]);
  assert.equal(a['dropbox:12'].done, true, 'submitted but ungraded folder not done');
  assert.ok(!a['dropbox:13'].done, 'folder with no submissions is done');
  assert.ok(a['dropbox:13'].mark == null, `ungraded folder has mark ${a['dropbox:13'].mark}`);
});

test('AC66 + AC65 gradebook items: grade:<Id>, title = Name, out_of = MaxPoints, Numeric value gives mark', () => {
  const a = bySid(avenue().items);
  assert.ok(a['grade:301'] && a['grade:302'], `missing grade:301/302 in ${Object.keys(a)}`);
  assert.deepEqual([a['grade:301'].title, a['grade:301'].out_of], ['Participation Bonus', 5]);
  assert.ok(a['grade:301'].mark == null, 'mark without a grade value');
  assert.deepEqual([a['grade:302'].title, a['grade:302'].mark, a['grade:302'].out_of], ['Midterm', 41, 50]);
});

test('AC67 gradebook item with a folder GradeItemId joins the dropbox row; its mark fills only an empty mark', () => {
  const a = bySid(avenue().items);
  assert.ok(!a['grade:101'] && !a['grade:102'], 'gradebook item emitted beside its dropbox row');
  assert.equal(a['dropbox:11'].mark, 18, 'gradebook mark replaced the dropbox feedback mark');
  assert.equal(a['dropbox:12'].mark, 15, 'empty dropbox mark not filled from the gradebook');
});

test('AC68 quizzes: quiz:<QuizId>, title = Name, due = DueDate', () => {
  const a = bySid(avenue().items);
  assert.ok(a['quiz:501'] && a['quiz:502'] && a['quiz:504'], `quiz rows missing in ${Object.keys(a)}`);
  assert.deepEqual([a['quiz:501'].title, a['quiz:501'].due], ['Quiz 1', '2026-10-05T03:59:00.000Z']);
  assert.deepEqual([a['quiz:502'].title, a['quiz:502'].due], ['Quiz 2 - Kinetics', '2026-10-12T03:59:00.000Z']);
});

test('AC79 + AC81 quiz <-> gradebook by GradeItemId, title fallback only when null; merged, mark fills, done follows mark', () => {
  const a = bySid(avenue().items);
  for (const id of ['grade:201', 'grade:202', 'grade:204']) assert.ok(!a[id], `${id} emitted beside its quiz row`);
  assert.deepEqual([a['quiz:501'].mark, a['quiz:501'].out_of, a['quiz:501'].done], [8, 10, true], 'linked by GradeItemId');
  assert.deepEqual([a['quiz:502'].mark, a['quiz:502'].out_of, a['quiz:502'].done], [9, 10, true], 'linked by normalized title');
  assert.ok(!a['quiz:504'].done && a['quiz:504'].mark == null, 'quiz 4 links to ungraded 204, not same-titled 206');
  assert.deepEqual([a['grade:206'] && a['grade:206'].mark], [7], 'unmatched gradebook item 206 not emitted per AC66');
});

test('AC72 + AC75 Category grade value: no item, recorded as categoryMarks (percent)', () => {
  const r = avenue();
  assert.ok(!r.items.some((i) => /online homework/i.test(i.title) || /9001/.test(i.source_id)), 'item made for a Category value');
  assert.deepEqual(r.categoryMarks, [{ name: 'Online Homework', value: 90 }]);
});

test('AC52 Avenue items never carry the gradebook Weight', () => {
  for (const i of avenue().items) assert.ok(i.weight === undefined && i.Weight === undefined, `weight on ${i.source_id}`);
});

// ---- Crowdmark (AC24) ----------------------------------------------------------------

const CM_ASSIGNMENTS = {
  data: [
    { id: 'a1', type: 'assignments', attributes: { due: '2026-10-09T03:59:00Z', 'submitted-at': '2026-10-08T22:00:00Z', 'marks-sent-at': '2026-10-12T15:00:00Z', 'normalized-points': 0.79 },
      relationships: { 'exam-master': { data: { id: 'em1', type: 'exam-masters' } } } },
    { id: 'a2', type: 'assignments', attributes: { due: '2026-10-16T03:59:00Z', 'submitted-at': '2026-10-15T22:00:00Z', 'marks-sent-at': null, 'normalized-points': null },
      relationships: { 'exam-master': { data: { id: 'em2', type: 'exam-masters' } } } },
    { id: 'a3', type: 'assignments', attributes: { due: '2026-10-23T03:59:00Z', 'submitted-at': null, 'marks-sent-at': null, 'normalized-points': null },
      relationships: { 'exam-master': { data: { id: 'em3', type: 'exam-masters' } } } },
  ],
  included: [
    { id: 'em1', type: 'exam-masters', attributes: { title: 'Assignment 3', 'total-points': 24 } },
    { id: 'em2', type: 'exam-masters', attributes: { title: 'Assignment 4', 'total-points': 30 } },
    { id: 'em3', type: 'exam-masters', attributes: { title: 'Assignment 5', 'total-points': 30 } },
  ],
};
const CM_RESULTS = {
  a1: { data: { id: 'a1', type: 'results', attributes: { marked: true, total: 20, 'total-after-penalty': 19 } },
        included: [{ id: 'em1', type: 'exam-masters', attributes: { title: 'Assignment 3', 'total-points': 24 } }] },
};

test('AC24 Crowdmark: title from exam-master, due, done = submitted-at, mark/out_of from results', () => {
  const list = P.crowdmarkItems(CM_ASSIGNMENTS, CM_RESULTS);
  const t = byTitle(list);
  assert.deepEqual(list.map((i) => i.title), ['Assignment 3', 'Assignment 4', 'Assignment 5']);
  assert.deepEqual([t['Assignment 3'].due, t['Assignment 3'].done, t['Assignment 3'].mark, t['Assignment 3'].out_of],
    ['2026-10-09T03:59:00Z', true, 19, 24]);
  assert.equal(t['Assignment 4'].done, true);
  assert.ok(t['Assignment 4'].mark == null, 'mark without marks-sent-at');
  assert.ok(!t['Assignment 5'].done && t['Assignment 5'].mark == null);
  assert.equal(new Set(list.map((i) => i.source_id)).size, 3, 'source_id not unique');
});

// ---- syncAll (AC20, AC21) --------------------------------------------------------------

async function runSync(fetchers) {
  const posted = [];
  await P.syncAll(fetchers, async (body) => { posted.push(body); });
  return Object.fromEntries(posted.map((b) => [b.source, b]));
}

test('AC20 one fetcher failing: the others still post their items; the failing source posts error + message', async () => {
  const got = await runSync({
    avenue: async () => ({ items: [{ source_id: 'dropbox:1', title: 'A1' }], categoryMarks: [] }),
    crowdmark: () => { throw new Error('boom: bad JSON'); },
    childsmath: async () => ({ items: [{ source_id: 'c1', title: 'Assignment #1' }] }),
  });
  assert.deepEqual(Object.keys(got).sort(), ['avenue', 'childsmath', 'crowdmark']);
  assert.equal(got.avenue.status, 'ok');
  assert.deepEqual(got.avenue.items, [{ source_id: 'dropbox:1', title: 'A1' }]);
  assert.equal(got.childsmath.status, 'ok');
  assert.equal(got.childsmath.items.length, 1);
  assert.equal(got.crowdmark.status, 'error');
  assert.match(got.crowdmark.error, /boom: bad JSON/);
  assert.deepEqual(got.crowdmark.items, []);
});

test('AC21 a fetcher throwing LoginNeeded reports "login needed"', async () => {
  const got = await runSync({
    avenue: async () => { throw new P.LoginNeeded('avenue'); },
    childsmath: async () => ({ items: [] }),
  });
  assert.equal(got.avenue.status, 'login needed');
  assert.deepEqual(got.avenue.items, []);
  assert.equal(got.childsmath.status, 'ok');
});

// ---- AC61 / AC75: no MyLab or Pearson code ---------------------------------------------

test('AC61 + AC75 extension source has no MyLab/Pearson fetcher', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const dir = path.join(__dirname, 'extension');
  for (const f of fs.readdirSync(dir)) {
    const text = fs.readFileSync(path.join(dir, f), 'utf8');
    assert.ok(!/mylab|pearson/i.test(text), `MyLab/Pearson in extension/${f}`);
  }
});

// ---- Live-run fixes (bugs 1, 2, 3, 7; ACs from AC85) ------------------------------

test('Bug 1 termStart: Avenue Code suffix 2yyT gives the term start (9 Fall Sep 1, 1 Winter Jan 1, 5 Spring May 1)', () => {
  assert.equal(P.termStart('MATH_1B03_x_2269'), '2026-09-01');
  assert.equal(P.termStart('CHEM_1A03_abc_2271'), '2027-01-01');
  assert.equal(P.termStart('PHYSICS_1C03_y_2265'), '2026-05-01');
  assert.ok(!P.termStart('Sandbox course'), 'term from a code with no term suffix');
  assert.ok(!P.termStart('SANDBOX_TEST_2263'), 'term from suffix digit 3 (only 1, 5, 9 are terms)');
  assert.ok(!P.termStart('MATH_1B03'), 'term from a code with no suffix');
});

test('Bug 1 avenueItems drops items due before the term start; items without a due are kept', () => {
  const r = P.avenueItems({
    code: 'MATH_1B03_x_2269',
    folders: [
      { Id: 71, Name: 'Old Assignment 1', DueDate: '2024-10-29T03:59:00.000Z', GradeItemId: null, Assessment: { ScoreDenominator: 10 } },
      { Id: 72, Name: 'Old Assignment 2', DueDate: '2024-12-07T04:59:00.000Z', GradeItemId: null, Assessment: { ScoreDenominator: 10 } },
      { Id: 73, Name: 'Assignment 1', DueDate: '2026-10-01T03:59:00.000Z', GradeItemId: null, Assessment: { ScoreDenominator: 10 } },
      { Id: 74, Name: 'Reflection', DueDate: null, GradeItemId: null, Assessment: { ScoreDenominator: 5 } },
    ],
    subs: { 71: [], 72: [], 73: [], 74: [] },
    quizzes: [{ QuizId: 81, Name: 'Old Quiz', DueDate: '2025-03-01T03:59:00.000Z', GradeItemId: null }],
  });
  assert.deepEqual(r.items.map((i) => i.source_id).sort(), ['dropbox:73', 'dropbox:74']);
});

test('AC85 a Code with no _YYYT term suffix skips nothing', () => {
  const r = P.avenueItems({
    code: 'SANDBOX_TEST_2263',
    folders: [{ Id: 75, Name: 'Old Sandbox Task', DueDate: '2019-01-01T00:00:00.000Z', GradeItemId: null, Assessment: { ScoreDenominator: 1 } }],
    subs: { 75: [] },
  });
  assert.deepEqual(r.items.map((i) => i.source_id), ['dropbox:75']);
});

test('Bug 2 childsmath course label = last course-label line before the first item title', () => {
  const lines = ['Your Courses:', 'Math 1A03/1ZA3 F26', 'Math 1B03 F26', 'Math 1B03 F26',
    'Assignment #1', '(Due date: 11:59pm on Tuesday September 22)', '0/8',
    'Assignment #2', '(Due date: 11:59pm on Tuesday October 6)', '2/8'];
  const list = P.childsmathItems(lines);
  assert.deepEqual(list.map((i) => i.title), ['Assignment #1', 'Assignment #2']);
  for (const i of list) assert.equal(i.course, 'Math 1B03 F26');
  assert.equal(list[0].due, '2026-09-22T23:59');
});

test('Bug 2 two childsmath areas with the same titles give distinct source_ids', () => {
  const area = (label) => P.childsmathItems(['Your Courses:', label, 'Assignment #1', '0/8', 'Assignment #2', '0/8']);
  const ids = [...area('Math 1B03 F26'), ...area('Math 1ZA3 F26')].map((i) => i.source_id);
  assert.equal(new Set(ids).size, 4, `source_ids collide: ${ids}`);
});

// Live 0.1.2 bug: an expired childsmath session serves its login page (HTTP 200) for student_admin.php.
const CM_LOGIN_PAGE = ['Childsmath', 'Welcome to the McMaster Mathematics online system', 'MacID login',
  'Please sign in with your MacID to continue.'];
const CM_ADMIN_PAGE = ['Student Admin Page', 'smithj', 'Log Out', 'Your Courses:', 'Math 1B03 F26',
  'Assignment #1', '(Due date: 11:59pm on Tuesday September 22)', '0/8'];

test('Bug childsmath login page served as 200 is recognised as logged out', () => {
  assert.equal(P.childsmathLoggedOut(CM_LOGIN_PAGE), true);
  assert.equal(P.childsmathLoggedOut([' MacID Login ']), true, 'whitespace and case');
});

test('Bug childsmath admin page, even without a course label, is not logged out', () => {
  assert.equal(P.childsmathLoggedOut(CM_ADMIN_PAGE), false);
  assert.equal(P.childsmathLoggedOut(['Student Admin Page', 'smithj', 'Log Out', 'Your Courses:']), false);
  // An admin page that happens to mention MacID is still the admin page.
  assert.equal(P.childsmathLoggedOut(['Student Admin Page', 'Log Out', 'Your MacID: smithj']), false);
  assert.equal(P.childsmathLoggedOut([]), false, 'an empty page is not evidence of a login page');
});

test('Bug 3 any Avenue item with a mark is done (gradebook 1/1 and 0/5)', () => {
  const r = P.avenueItems({
    grades: [{ Id: 601, Name: 'Lab 1: Excel Exercises', MaxPoints: 1, GradeType: 'Numeric' },
             { Id: 602, Name: 'Lab 2', MaxPoints: 5, GradeType: 'Numeric' },
             { Id: 603, Name: 'Lab 3', MaxPoints: 5, GradeType: 'Numeric' }],
    values: [{ GradeObjectIdentifier: '601', GradeObjectTypeName: 'Numeric', PointsNumerator: 1, PointsDenominator: 1 },
             { GradeObjectIdentifier: '602', GradeObjectTypeName: 'Numeric', PointsNumerator: 0, PointsDenominator: 5 }],
  });
  const a = bySid(r.items);
  assert.equal(a['grade:601'].done, true, '1/1 not done');
  assert.equal(a['grade:602'].done, true, '0/5 (a mark of zero) not done');
  assert.ok(!a['grade:603'].done, 'unmarked item done');
});

test('Bug 7 Text gradebook items are not items; they come back as courseInfo [{name, value}]', () => {
  const r = P.avenueItems({
    grades: [
      { Id: 701, Name: 'Lab Room', MaxPoints: null, GradeType: 'Text' },
      { Id: 702, Name: 'Lab Section', MaxPoints: null, GradeType: 'Text' },
      { Id: 703, Name: 'Lab Bench Number', MaxPoints: null, GradeType: 'Text' },
      { Id: 704, Name: 'Test 1 Room', MaxPoints: null, GradeType: 'Text' },
      { Id: 705, Name: 'Lab 1', MaxPoints: 10, GradeType: 'Numeric' },
    ],
    values: [
      { GradeObjectIdentifier: '701', GradeObjectName: 'Lab Room', GradeObjectTypeName: 'Text', DisplayedGrade: 'ABB 102' },
      { GradeObjectIdentifier: '702', GradeObjectName: 'Lab Section', GradeObjectTypeName: 'Text', DisplayedGrade: 'L07' },
      { GradeObjectIdentifier: '703', GradeObjectName: 'Lab Bench Number', GradeObjectTypeName: 'Text', DisplayedGrade: '14' },
    ],
  });
  assert.deepEqual(r.items.map((i) => i.source_id), ['grade:705']);
  assert.deepEqual(r.courseInfo, [
    { name: 'Lab Room', value: 'ABB 102' }, { name: 'Lab Section', value: 'L07' },
    { name: 'Lab Bench Number', value: '14' }, { name: 'Test 1 Room', value: '' }]);
  assert.deepEqual(r.categoryMarks, []);
});

// ---- Spec 0002 session renewal: pure helpers (plan 0002 Step 2) -------------------

const AVENUE_ENTRY = 'https://avenue.mcmaster.ca/login.php?target=%2Fd2l%2Fhome';
const CHILDSMATH_ENTRY = 'https://login.microsoftonline.com/44376307-b429-42ad-8c25-28cd496f4772/oauth2/v2.0/authorize?client_id=9132e1a8-fef6-4c34-bddb-de7608a2381a&response_type=code&response_mode=query&scope=https%3A%2F%2Fgraph.microsoft.com%2FUser.Read';
const LOGGED_IN = {
  avenue: 'https://avenue.cllmcmaster.ca/d2l/home',
  crowdmark: 'https://app.crowdmark.com/student/courses',
  childsmath: 'https://www.childsmath.ca/childsa/forms/main_intro.php',
};
const IDP_URLS = [
  CHILDSMATH_ENTRY,
  'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x',
  'https://cap.mcmaster.ca/mcauth/login.jsp?app_id=1492',
];
const CM_LTI = '/d2l/common/dialogs/quickLink/quickLink.d2l?ou=787224&type=lti&rcode=Mac-4028664&srcou=787224';

test('0002 AC22 + AC23 + AC25 SITES: login entry URLs per source; Crowdmark has none', () => {
  assert.ok(P.SITES, 'P.SITES is not exported');
  assert.equal(P.SITES.avenue.login, AVENUE_ENTRY);
  assert.equal(P.SITES.childsmath.login, CHILDSMATH_ENTRY);
  assert.ok(P.SITES.crowdmark, 'P.SITES.crowdmark missing');
  assert.ok(!P.SITES.crowdmark.login, `crowdmark has a static login: ${P.SITES.crowdmark.login}`);
});

test('0002 AC21 + AC22 loggedInUrl avenue: /d2l/ on cllmcmaster is in; /d2l/login and avenue.mcmaster.ca are not', () => {
  assert.equal(P.loggedInUrl('avenue', 'https://avenue.cllmcmaster.ca/d2l/home'), true);
  assert.equal(P.loggedInUrl('avenue', 'https://avenue.cllmcmaster.ca/d2l/login?sessionExpired=0&target=%2fd2l%2fhome'), false);
  assert.equal(P.loggedInUrl('avenue', AVENUE_ENTRY), false);
  assert.equal(P.loggedInUrl('avenue', 'https://avenue.mcmaster.ca/d2l/home'), false);
});

test('0002 AC21 + AC25 loggedInUrl crowdmark: /student/ on app.crowdmark.com is in; root is not', () => {
  assert.equal(P.loggedInUrl('crowdmark', 'https://app.crowdmark.com/student/courses'), true);
  assert.equal(P.loggedInUrl('crowdmark', 'https://app.crowdmark.com/'), false);
  assert.equal(P.loggedInUrl('crowdmark', 'https://avenue.cllmcmaster.ca' + CM_LTI), false);
});

test('0002 AC21 + AC23 loggedInUrl childsmath: exactly main_intro.php is in; other childsmath pages are not', () => {
  assert.equal(P.loggedInUrl('childsmath', 'https://www.childsmath.ca/childsa/forms/main_intro.php'), true);
  assert.equal(P.loggedInUrl('childsmath', 'https://www.childsmath.ca/childsa/forms/1bStuff/student_admin.php'), false);
});

test('0002 AC21 + AC3 loggedInUrl: IdP pages and other sources\' logged-in URLs are never logged in', () => {
  for (const source of Object.keys(LOGGED_IN)) {
    for (const url of IDP_URLS) assert.equal(P.loggedInUrl(source, url), false, `${source} ${url}`);
    for (const [other, url] of Object.entries(LOGGED_IN)) {
      if (other !== source) assert.equal(P.loggedInUrl(source, url), false, `${source} accepted ${other} ${url}`);
    }
  }
});

test('0002 AC21 + AC6 quietHour: 23:00 inclusive to 06:00 exclusive, local time', () => {
  const at = (h, m) => P.quietHour(new Date(2026, 9, 15, h, m));
  assert.equal(at(22, 59), false, '22:59');
  assert.equal(at(23, 0), true, '23:00');
  assert.equal(at(2, 0), true, '02:00');
  assert.equal(at(5, 59), true, '05:59');
  assert.equal(at(6, 0), false, '06:00');
  assert.equal(at(12, 0), false, '12:00');
});

test('0002 AC21 + AC24 crowdmarkEntry: first Crowdmark-titled lti topic, prefixed with the Avenue origin', () => {
  const topics = [
    { Title: 'Course Outline', Url: '/x' },
    { Title: 'Crowdmark', TypeIdentifier: 'Link', Url: CM_LTI },
  ];
  assert.equal(P.crowdmarkEntry(topics), 'https://avenue.cllmcmaster.ca' + CM_LTI);
  assert.equal(P.crowdmarkEntry([{ Title: 'crowdmark assignments', Url: CM_LTI }, { Title: 'Crowdmark', Url: CM_LTI + '&second=1' }]),
    'https://avenue.cllmcmaster.ca' + CM_LTI, 'title match is case-insensitive and the first hit wins');
});

test('0002 AC24 crowdmarkEntry: a Crowdmark-titled topic without type=lti is skipped; no match gives null', () => {
  assert.equal(P.crowdmarkEntry([{ Title: 'Crowdmark', Url: '/content/crowdmark-instructions.html' }]), null);
  assert.equal(P.crowdmarkEntry([
    { Title: 'Crowdmark', Url: '/content/crowdmark-instructions.html' },
    { Title: 'Crowdmark', Url: CM_LTI },
  ]), 'https://avenue.cllmcmaster.ca' + CM_LTI);
  assert.equal(P.crowdmarkEntry([{ Title: 'Course Outline', Url: '/x' }, { Title: 'Lab', Url: '/l?type=lti' }]), null);
  assert.equal(P.crowdmarkEntry([]), null);
});

// ---- Spec 0002 session renewal: renewal decisions (plan 0002 Step 3) --------------
// Fakes stand in for the browser: open(source) -> {ok:true} | {tabId}, rerun(source) -> posted status,
// notify(sources), entry(source) -> URL | null, now() -> Date. They record what renewal asked the browser to do.

const at = (h, m) => new Date(2026, 9, 15, h, m);
const tick = () => new Promise((r) => setImmediate(r));
const newState = () => ({ fallback: {}, skipped: [], busy: false });

function fakeBrowser({ now = at(12, 0), open = () => ({ ok: true }), rerun = () => 'ok', entry } = {}) {
  const calls = { open: [], rerun: [], notify: [], log: [] };
  const d = {
    clock: now,
    now: () => d.clock,
    entry: entry || ((s) => `https://entry.example/${s}`),
    open: async (s) => { calls.open.push(s); calls.log.push(`open ${s}`); const r = await open(s, d); calls.log.push(`done ${s}`); return r; },
    rerun: async (s) => { calls.rerun.push(s); return rerun(s, d); },
    notify: async (sources) => { calls.notify.push([...sources]); },
  };
  return { d, calls };
}

test('0002 AC2 one login-needed source: its renewal tab is opened once', async () => {
  const { d, calls } = fakeBrowser();
  await P.renewAll(['crowdmark'], newState(), d);
  assert.deepEqual(calls.open, ['crowdmark']);
});

test('0002 AC3 renewal succeeds: that source alone is synced again; no notification', async () => {
  const { d, calls } = fakeBrowser();
  await P.renewAll(['crowdmark'], newState(), d);
  assert.deepEqual(calls.rerun, ['crowdmark']);
  assert.deepEqual(calls.notify, []);
});

test('0002 AC5 still login needed after the rerun: no second renewal tab in that sync', async () => {
  const { d, calls } = fakeBrowser({ rerun: () => 'login needed' });
  await P.renewAll(['crowdmark'], newState(), d);
  assert.deepEqual(calls.open, ['crowdmark']);
  assert.deepEqual(calls.rerun, ['crowdmark']);
});

test('0002 AC6 quiet window 23:00-06:00: no tab and no notification at 23:00, 02:00, 05:59; renewal at 22:59, 06:00', async () => {
  for (const [h, m] of [[23, 0], [2, 0], [5, 59]]) {
    const { d, calls } = fakeBrowser({ now: at(h, m) });
    await P.renewAll(['avenue', 'childsmath'], newState(), d);
    assert.deepEqual(calls.open, [], `opened at ${h}:${m}`);
    assert.deepEqual(calls.notify, [], `notified at ${h}:${m}`);
  }
  for (const [h, m] of [[22, 59], [6, 0]]) {
    const { d, calls } = fakeBrowser({ now: at(h, m) });
    await P.renewAll(['avenue'], newState(), d);
    assert.deepEqual(calls.open, ['avenue'], `not opened at ${h}:${m}`);
  }
});

test('0002 AC6 quiet window is checked when each renewal would start, not once per sync', async () => {
  // Avenue starts at 22:59; its renewal ends at 23:00, so childsmath would start inside the window.
  const { d, calls } = fakeBrowser({ now: at(22, 59), open: (s, dd) => { dd.clock = at(23, 0); return { ok: true }; } });
  await P.renewAll(['avenue', 'childsmath'], newState(), d);
  assert.deepEqual(calls.open, ['avenue']);
  assert.deepEqual(calls.notify, []);
});

test('0002 AC7 a source left during the quiet window is renewed by the first sync at 06:00', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ now: at(23, 30) });
  await P.renewAll(['avenue'], state, d);
  assert.deepEqual(calls.open, []);
  d.clock = at(6, 0);
  await P.renewAll(['avenue'], state, d);
  assert.deepEqual(calls.open, ['avenue']);
});

test('0002 AC8 two sources renew one after another: the next tab opens only after the previous renewal ended', async () => {
  const { d, calls } = fakeBrowser({ open: async () => { await tick(); return { ok: true }; } });
  await P.renewAll(['avenue', 'childsmath'], newState(), d);
  assert.deepEqual(calls.log, ['open avenue', 'done avenue', 'open childsmath', 'done childsmath']);
});

test('0002 AC26 fixed order avenue -> crowdmark -> childsmath whatever the input order', async () => {
  const { d, calls } = fakeBrowser();
  await P.renewAll(['childsmath', 'crowdmark', 'avenue'], newState(), d);
  assert.deepEqual(calls.open, ['avenue', 'crowdmark', 'childsmath']);
});

test('0002 AC26 + AC11 Avenue goes to fallback: Crowdmark and childsmath get no tab', async () => {
  const { d, calls } = fakeBrowser({ open: (s) => (s === 'avenue' ? { tabId: 3 } : { ok: true }) });
  await P.renewAll(['childsmath', 'crowdmark', 'avenue'], newState(), d);
  assert.deepEqual(calls.open, ['avenue']);
});

test('0002 AC26 Crowdmark with no known entry URL: no tab, straight to the notification', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ entry: (s) => (s === 'crowdmark' ? null : `https://entry.example/${s}`) });
  await P.renewAll(['crowdmark'], state, d);
  assert.deepEqual(calls.open, []);
  assert.equal(calls.notify.length, 1);
  assert.ok(calls.notify[0].includes('crowdmark'), `notify ${JSON.stringify(calls.notify)}`);
  assert.ok(!('crowdmark' in state.fallback), 'crowdmark has a fallback tab entry without a tab');
});

test('0002 AC10 renewal times out: its tab is kept as the fallback tab and one notification names the source', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: () => ({ tabId: 7 }) });
  await P.renewAll(['avenue'], state, d);
  assert.equal(state.fallback.avenue, 7);
  assert.deepEqual(calls.notify, [['avenue']]);
  assert.deepEqual(calls.rerun, [], 'a fallback source is not re-synced before sign-in');
});

test('0002 AC11 first source falls back: others skipped, one notification names all three', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: () => ({ tabId: 7 }) });
  await P.renewAll(['avenue', 'crowdmark', 'childsmath'], state, d);
  assert.deepEqual(calls.open, ['avenue']);
  assert.deepEqual(calls.notify, [['avenue', 'crowdmark', 'childsmath']]);
  assert.deepEqual(state.fallback, { avenue: 7 });
  assert.deepEqual([...state.skipped].sort(), ['childsmath', 'crowdmark']);
});

test('0002 AC11 + AC13 sign-in in the fallback tab: the source re-syncs, then skipped sources renew in order', async () => {
  const state = newState();
  let first = true;
  const { d, calls } = fakeBrowser({ open: () => (first ? (first = false, { tabId: 7 }) : { ok: true }) });
  await P.renewAll(['avenue', 'crowdmark', 'childsmath'], state, d);
  calls.open.length = 0; calls.rerun.length = 0; calls.log.length = 0;
  await P.fallbackSignedIn('avenue', state, d);
  assert.equal(calls.rerun[0], 'avenue', `rerun ${calls.rerun}`);
  assert.deepEqual(calls.open, ['crowdmark', 'childsmath']);
  assert.deepEqual(calls.log, ['open crowdmark', 'done crowdmark', 'open childsmath', 'done childsmath']);
  assert.ok(!('avenue' in state.fallback), 'avenue still has a fallback tab entry');
  assert.deepEqual(state.skipped, []);
});

test('0002 AC11 fallback source re-syncs but is not ok: skipped sources are not retried', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: () => ({ tabId: 7 }) });
  await P.renewAll(['avenue', 'crowdmark', 'childsmath'], state, d);
  calls.open.length = 0;
  d.rerun = async () => 'login needed';
  await P.fallbackSignedIn('avenue', state, d);
  assert.deepEqual(calls.open, []);
});

test('0002 AC14 open fallback tab is reused: a later sync opens no tab and shows no notification for it', async () => {
  const state = newState();
  state.fallback.avenue = 7;
  const { d, calls } = fakeBrowser();
  await P.renewAll(['avenue'], state, d);
  assert.deepEqual(calls.open, []);
  assert.deepEqual(calls.notify, []);
  assert.equal(state.fallback.avenue, 7);
});

test('0002 AC14 an older fallback tab for one source does not block renewal of another source', async () => {
  const state = newState();
  state.fallback.avenue = 7;
  const { d, calls } = fakeBrowser();
  await P.renewAll(['avenue', 'childsmath'], state, d);
  assert.deepEqual(calls.open, ['childsmath']);
});

test('0002 AC26 Avenue fallback tab open: no Crowdmark tab; Avenue sign-in then renews Crowdmark', async () => {
  const state = newState();
  state.fallback.avenue = 7;
  const { d, calls } = fakeBrowser();
  await P.renewAll(['crowdmark'], state, d);
  assert.deepEqual(calls.open, [], 'Crowdmark renewal started without a valid Avenue session');
  await P.fallbackSignedIn('avenue', state, d);
  assert.deepEqual(calls.open, ['crowdmark']);
});

test('0002 AC15 fallback tab closed without sign-in: the next sync tries renewal again', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: () => ({ tabId: 7 }) });
  await P.renewAll(['avenue'], state, d);
  delete state.fallback.avenue; // what tabs.onRemoved will do
  await P.renewAll(['avenue'], state, d);
  assert.deepEqual(calls.open, ['avenue', 'avenue']);
});

test('0002 overlap guard: an alarm sync and a click sync at once open one tab per source', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: async () => { await tick(); return { ok: true }; } });
  await Promise.all([P.renewAll(['avenue'], state, d), P.renewAll(['avenue'], state, d)]);
  assert.deepEqual(calls.open, ['avenue']);
  await P.renewAll(['avenue'], state, d);
  assert.deepEqual(calls.open, ['avenue', 'avenue'], 'guard stuck after the first run finished');
});

test('0002 session default: Crowdmark with no known entry URL is notified but is not a fallback; later sources still renew', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ entry: (s) => (s === 'crowdmark' ? null : `https://entry.example/${s}`) });
  await P.renewAll(['childsmath', 'crowdmark'], state, d);
  assert.deepEqual(calls.open, ['childsmath']);
  assert.deepEqual(calls.notify, [['crowdmark']]);
  assert.deepEqual(state.skipped, []);
});

test('0002 session default: sign-in seen but re-sync not ok keeps the fallback tab tracked and retries nothing', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: () => ({ tabId: 7 }) });
  await P.renewAll(['avenue', 'crowdmark', 'childsmath'], state, d);
  calls.open.length = 0;
  d.rerun = async () => 'login needed';
  await P.fallbackSignedIn('avenue', state, d);
  assert.deepEqual(calls.open, []);
  assert.deepEqual(state.fallback, { avenue: 7 });
  assert.deepEqual(state.skipped, ['crowdmark', 'childsmath']);
});

test('0002 renewal tab closed by the student: no fallback entry, no notification, later sources still renew', async () => {
  const state = newState();
  const { d, calls } = fakeBrowser({ open: (s) => (s === 'avenue' ? { closed: true } : { ok: true }) });
  await P.renewAll(['avenue', 'childsmath'], state, d);
  assert.deepEqual(calls.open, ['avenue', 'childsmath']);
  assert.deepEqual(state.fallback, {});
  assert.deepEqual(calls.notify, []);
  assert.deepEqual(calls.rerun, ['childsmath']);
});

test('0002 AC9 one source needs sign-in: the other two are posted ok with their items before syncAll resolves', async () => {
  const got = await runSync({
    avenue: async () => ({ items: [{ source_id: 'dropbox:1', title: 'A1' }], categoryMarks: [] }),
    crowdmark: async () => { throw new P.LoginNeeded('crowdmark'); },
    childsmath: async () => ({ items: [{ source_id: 'c1', title: 'Assignment #1' }] }),
  });
  assert.equal(got.crowdmark.status, 'login needed');
  assert.equal(got.avenue.status, 'ok');
  assert.deepEqual(got.avenue.items, [{ source_id: 'dropbox:1', title: 'A1' }]);
  assert.equal(got.childsmath.status, 'ok');
  assert.deepEqual(got.childsmath.items, [{ source_id: 'c1', title: 'Assignment #1' }]);
});

// ---------------------------------------------------------------------------------

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL ${name}: ${e.message}`);
    }
  }
  process.exit(failed ? 1 : 0);
})();
