/* Re-evaluate a static snapshot against the current Shanghai clock. */
(function(root){
"use strict";
const endedStates = new Set(["ended", "已结束", "cancelled", "canceled", "已取消"]);
const saleStates = new Set(["on_sale", "预售中", "selling", "sold_out", "售罄"]);
function shanghaiDay(now = new Date()){
  return new Date(now.getTime()+8*3600000).toISOString().slice(0,10);
}
function saleInstant(value){
  if(!value) return NaN;
  const s = String(value).replace(" ", "T");
  return Date.parse(s.length===10 ? s+"T00:00:00+08:00"
    : /(?:Z|[+-]\d\d:\d\d)$/.test(s) ? s : s+"+08:00");
}
function eventStatus(event, now = new Date()){
  if(event.show_date && event.show_date < shanghaiDay(now)) return "ended";
  const raw = String(event.sale_status||"").trim();
  if(endedStates.has(raw)) return "ended";
  if(raw==="scheduled"){
    const start = saleInstant(event.sale_time), end = saleInstant(event.sale_end_time);
    return Number.isFinite(start) && now.getTime()>=start &&
      (!Number.isFinite(end) || now.getTime()<end) ? "on_sale" : "upcoming";
  }
  if(saleStates.has(raw)) return "on_sale";
  if(["upcoming", "announced", "paused", "postponed", "已延期", "待开票", "即将开售"].includes(raw)) return "upcoming";
  const start = saleInstant(event.sale_time);
  if(Number.isFinite(start)) return now.getTime()>=start ? "on_sale" : "upcoming";
  return event.status || "upcoming";
}
function projectSnapshot(snapshot, now = new Date()){
  const result = {...snapshot, on_sale:[], upcoming:[], ended:[]};
  for(const group of ["on_sale", "upcoming", "ended"]){
    for(const original of snapshot[group]||[]){
      const event = {...original};
      event.status = eventStatus({...event, status:event.status||group}, now);
      result[event.status].push(event);
    }
  }
  const showKey = e => (e.show_date||"9999-99-99")+"|"+(e.show_time||"")+"|"+(e.artist_name||"");
  const compare = (a,b) => a<b ? -1 : a>b ? 1 : 0;
  result.on_sale.sort((a,b)=>compare(showKey(a),showKey(b)));
  result.upcoming.sort((a,b)=>compare(a.sale_time||"9999",b.sale_time||"9999") || compare(showKey(a),showKey(b)));
  result.ended.sort((a,b)=>compare(showKey(b),showKey(a)));
  // Older snapshots retain only the latest 40 ended rows; preserve their total.
  const omitted = Math.max(0, (snapshot.counts?.ended||0)-(snapshot.ended||[]).length);
  result.counts = {...snapshot.counts, on_sale:result.on_sale.length,
    upcoming:result.upcoming.length, ended:result.ended.length+omitted};
  return result;
}
const api = {shanghaiDay, saleInstant, eventStatus, projectSnapshot};
if(typeof module!=="undefined" && module.exports) module.exports = api;
else root.CMState = api;
})(typeof window!=="undefined" ? window : globalThis);
