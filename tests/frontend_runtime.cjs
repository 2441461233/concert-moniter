const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const state = require('../site/event-state.js');
const html = fs.readFileSync('site/index.html','utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1];
let now = Date.parse('2026-09-17T23:59:00+08:00');
class Clock extends Date { constructor(...args){super(...(args.length ? args : [now]));} static now(){return now;} }
const old = {id:'old', artist_key:'illit', artist_name:'ILLIT', show_date:'2026-09-09', title:'PAST_SHOW', sale_status:'on_sale', sources:[{url:'https://example.com/old'}]};
const today = {...old,id:'today',title:'TODAY_SHOW',show_date:'2026-09-17'};
const future = {...old,id:'future',title:'FUTURE_SHOW',show_date:'2026-10-17'};
const japan = {...old,id:'japan',title:'JAPAN_SHOW',show_date:'2026-12-23',sale_status:'scheduled',sale_time:'2026-09-07T17:00+09:00',sale_end_time:'2026-09-15T23:59+09:00'};
const snapshot = {artists:[{key:'illit',name:'ILLIT',region:'kpop'}],on_sale:[old,today,future,japan],upcoming:[],ended:[],rumors:[],changes:[],counts:{on_sale:4,upcoming:0,ended:0}};
const original = JSON.stringify(snapshot);
const projected = state.projectSnapshot(snapshot, new Clock());
assert.deepEqual(projected.on_sale.map(x=>x.id), ['today','future']);
assert.deepEqual(projected.upcoming.map(x=>x.id), ['japan']);
assert.equal(projected.counts.ended, 1);
assert.equal(JSON.stringify(snapshot), original);
// A browser in another timezone still advances at Shanghai midnight.
assert.equal(state.shanghaiDay(new Date('2026-09-17T16:00:00Z')), '2026-09-18');
assert.equal(state.projectSnapshot({...snapshot,counts:{ended:12}},new Clock()).counts.ended,13);
const nodes = new Map();
const node = id => {if(!nodes.has(id)) nodes.set(id,{innerHTML:'',textContent:'',dataset:{},parentElement:{},classList:{toggle(){}},addEventListener(){},setAttribute(){},removeAttribute(){},focus(){},scrollLeft:0});return nodes.get(id);};
const intervals=[], listeners={};
const context = {Date:Clock, URL, console, setInterval(fn){intervals.push(fn);},setTimeout(){},clearTimeout(){},
  localStorage:{getItem(){return null;},setItem(){}},sessionStorage:{getItem(){return null;}},
  location:{href:'http://localhost/',origin:'http://localhost'},
  document:{getElementById:node,querySelectorAll(){return [];},addEventListener(name,fn){listeners[name]=fn;},documentElement:{dataset:{}},hidden:false},
  window:{__CM_DATA__:snapshot,addEventListener(name,fn){listeners[name]=fn;},scrollTo(){}}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('site/event-state.js','utf8'),context);
context.CMState=context.window.CMState;
vm.runInContext(script,context);
assert(!node('gigs').innerHTML.includes('PAST_SHOW'));
assert(node('past').innerHTML.includes('PAST_SHOW'));
assert(node('gigs').innerHTML.includes('TODAY_SHOW'));
assert(node('gigs').innerHTML.includes('待后续开票'));
assert(node('rail').innerHTML.includes('<span class="c">3</span>'));
assert(!node('ov').innerHTML.includes('09/09'));
now = Date.parse('2026-09-18T00:00:01+08:00');
listeners.visibilitychange();
assert(!node('gigs').innerHTML.includes('TODAY_SHOW'));
assert(node('past').innerHTML.includes('TODAY_SHOW'));
assert(node('rail').innerHTML.includes('<span class="c">2</span>'));
assert(node('ov').innerHTML.includes('<div class="big">1</div>'));
assert(node('dayrail').innerHTML.includes('9/18'));
assert(!node('past').innerHTML.includes('购票'));
for(const fn of intervals) fn();
assert.equal((node('past').innerHTML.match(/PAST_SHOW/g)||[]).length,1);
console.log('Frontend runtime: stale snapshot, counts, midnight, timezone and ticket visibility passed.');
