// Run: node tests/manager-item-performance.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../luuvcrm/www/manager.html'), 'utf8');
const start = html.indexOf('  let repItems=[]');
const end = html.indexOf('  function hideAllItems()', start);
assert(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(html.slice(start, end), context);
const run = code => vm.runInContext(code, context);
const plain = code => JSON.parse(JSON.stringify(run(code)));

for (const group of ['Kottu', 'Fried Rice', 'Loaded Fries', 'Chips', 'Desserts', 'Food']) {
  assert.equal(run(`itemReportGroup(${JSON.stringify(group)})`), 'Food');
}
for (const group of ['Drinks', 'Beverages', 'Coffee & Tea', 'Soft Drinks', 'Water Bottles', 'beer', ' Beverage ']) {
  assert.equal(run(`itemReportGroup(${JSON.stringify(group)})`), 'Beverage');
}
for (const group of ['Add-ons', 'Raw Materials', 'New category', '', null]) {
  assert.equal(run(`itemReportGroup(${JSON.stringify(group)})`), 'Others');
}
assert.equal(run(`itemReportGroup('  LOADED   FRIES  ')`), 'Food');

// Mixed branch-category fixtures: unsold items, counts and expanded list must
// use the same classification as all three ranked views without changing rows.
const sold = [
  {name:'Kottu', group:'Kottu', qty:5, revenue:500},
  {name:'Fries', group:'Loaded Fries', qty:2, revenue:800},
  {name:'Tea', group:'Coffee & Tea', qty:4, revenue:200},
  {name:'Smoothie', group:'Drinks', qty:3, revenue:600},
  {name:'Sauce', group:'Add-ons', qty:6, revenue:60},
  {name:'Unknown', group:'New category', qty:1, revenue:10}
];
const unsold = [
  {name:'Chips', group:'Chips', qty:0, revenue:0},
  {name:'Water', group:'Water Bottles', qty:0, revenue:0},
  {name:'Blank', group:null, qty:0, revenue:0}
];
run(`repItems=${JSON.stringify(sold)};repUnsold=${JSON.stringify(unsold)};`);
for (const [group, high, low, income] of [
  ['Food', ['Kottu','Fries'], ['Chips','Fries','Kottu'], ['Fries','Kottu']],
  ['Beverage', ['Tea','Smoothie'], ['Water','Smoothie','Tea'], ['Smoothie','Tea']],
  ['Others', ['Sauce','Unknown'], ['Blank','Unknown','Sauce'], ['Sauce','Unknown']]
]) {
  run(`itemGroup=${JSON.stringify(group)}`);
  for (const [view, expected] of [['high',high], ['low',low], ['income',income]]) {
    run(`itemView=${JSON.stringify(view)}`);
    assert.deepEqual(plain('itemRowsFor().map(r=>r.name)'), expected);
  }
  assert.equal(run('itemCountLine()'), '2 sold · 1 unsold');
  assert.deepEqual(plain('itemRowsAll().map(r=>r.name)'), [...high, low[0]]);
}
run(`itemGroup='__all__'`);
assert.equal(run('itemRowsAll().length'), 9);
assert.equal(run('itemCountLine()'), '6 sold · 3 unsold');
assert.deepEqual(plain('repItems'), sold);
assert.deepEqual(plain('repUnsold'), unsold);
assert.match(html, /data-group="Others" onclick="setItemGroup\('Others'\)">Others/);
assert(!html.includes('{#'), 'No accidental Jinja comment delimiters');
// Parse every inline script after replacing existing Jinja expressions/tags.
for (const [, script] of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)) {
  new vm.Script(script.replace(/\{\{[\s\S]*?\}\}/g, 'null').replace(/\{%[\s\S]*?%\}/g, ''));
}
console.log('PASS: classification, ranked views, unsold/counts, expanded list, unchanged data, inline JS syntax');
