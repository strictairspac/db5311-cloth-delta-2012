import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {bookingLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

function cutOptions(state) {
  const options = [];
  for (const row of state.evidence.filter(x=>x.reviewer)) {
    const cuts = (row.report.sections || []).find(x=>x.title==='建议转裁批次');
    for (const cut of (cuts?.rows || [])) {
      options.push(`<option value="${row.id}|${esc(cut['批次'])}">配额单 ${row.id} · ${esc(cut['批次'])} 第 ${esc(cut['修订号'])} 版（${esc(cut['转裁重量（克）'])} 克 / ${esc(cut['转裁件数'])} 件）</option>`);
    }
  }
  return options.join('');
}

export function tripsPage(state, identity) {
  const staff = ['operator','reviewer'].includes(state.session.role);
  const trips = state.trips || [];
  const selected = trips.find(row=>row.id===identity) || trips[0];
  const rows = trips.map(row=>[`<a href="#trips/${row.id}" ${row.id===selected?.id?'aria-current="true"':''}>${esc(row.trip_no)}</a>`,
    `${esc(row.capacity_g)} 克 / ${esc(row.capacity_pieces)} 件`,
    `${esc(row.booked_weight)} 克 / ${esc(row.booked_pieces)} 件`,
    `${esc(row.loaded_weight)} 克 / ${esc(row.loaded_pieces)} 件`,
    `${esc(row.remaining_weight)} 克 / ${esc(row.remaining_pieces)} 件`]);
  const create = staff ? `<aside class="brief"><h2>建立车次</h2><form id="trip-form">
    <label>车次号<input name="trip_no" maxlength="64" required></label>
    <label>重量上限（克）<input name="capacity_g" type="number" min="0" max="1000000000" required></label>
    <label>件数上限<input name="capacity_pieces" type="number" min="0" max="1000000000" required></label>
    <button>建立车次</button></form></aside>` : '';
  let detail = '<p class="empty">裁切区按车次收布。建立车次后，已签署配额单的转裁批次可以预约上车。</p>';
  if (selected) {
    const bookings = (state.tripDetail?.id===selected.id ? state.tripDetail.bookings : []) || [];
    const others = trips.filter(row=>row.id!==selected.id);
    const lines = bookings.map(row=>{
      const actions = staff && row.status==='booked' ? `<button type="button" data-load="${row.id}">装车</button>
        ${others.length?`<form class="move-form" data-id="${row.id}"><select name="trip_id">${others.map(x=>`<option value="${x.id}">${esc(x.trip_no)}</option>`).join('')}</select><button>换车</button></form>`:''}` : '';
      return [`配额单 ${esc(row.evidence_id)}`, esc(row.batch_id), esc(row.revision), esc(row.grade),
        `${esc(row.weight_g)} 克 / ${esc(row.pieces)} 件`, esc(bookingLabels[row.status] || row.status), esc(date(row.booked_at)), actions];
    });
    const manage = staff ? `<div class="spread"><section><h2>预约转裁批次</h2><form id="book-form">
      <label>配额单批次<select name="cut" required>${cutOptions(state)}</select></label><button>预约到 ${esc(selected.trip_no)}</button></form></section>
      <section><h2>调整限额</h2><form id="resize-form">
      <label>新重量上限（克）<input name="capacity_g" type="number" min="0" max="1000000000" value="${esc(selected.capacity_g)}" required></label>
      <label>新件数上限<input name="capacity_pieces" type="number" min="0" max="1000000000" value="${esc(selected.capacity_pieces)}" required></label>
      <button>调整限额</button><p class="note">减容时已装车批次不动；装不下已装车批次会被拒绝。其余批次按能留下最多批次的方案保留，挤出的转候补。</p></form></section></div>` : '';
    detail = `${panel(`车次 ${esc(selected.trip_no)} 的预约`, records(['配额单','批次','修订号','等级','重量 / 件数','状态','预约时间','操作'], lines, '该车次暂无预约。'))}${manage}`;
  }
  return `${heading(topic.modules[4], '几张已签的仓位配额单可以约同一班车；每批只能约一班车，重量与件数都不许超限。')}
    <div class="spread"><section aria-label="车次目录">${records(['车次号','限额（重量 / 件数）','已约','已装车','剩余'], rows, '尚未建立车次。')}</section>${create}</div>
    ${detail}`;
}

export function bindTrips(state, api, refresh, fail) {
  const selected = (state.trips || []).find(row=>row.id===state.tripDetail?.id);
  submit('trip-form', async data=>{
    await api('/trips', {method:'POST', data:{trip_no:data.get('trip_no'), capacity_g:Number(data.get('capacity_g')), capacity_pieces:Number(data.get('capacity_pieces'))}});
    await refresh();
  }, fail);
  if (!selected) return;
  submit('book-form', async data=>{
    const cut = String(data.get('cut') || '');
    const sep = cut.indexOf('|');
    await api(`/trips/${selected.id}/bookings`, {method:'POST', data:{evidence_id:Number(cut.slice(0, sep)), batch_id:cut.slice(sep + 1)}});
    await refresh();
  }, fail);
  submit('resize-form', async data=>{
    await api(`/trips/${selected.id}/resize`, {method:'POST', data:{capacity_g:Number(data.get('capacity_g')), capacity_pieces:Number(data.get('capacity_pieces'))}});
    await refresh();
  }, fail);
  document.querySelectorAll('[data-load]').forEach(button=>button.addEventListener('click', async()=>{
    try { await api(`/bookings/${button.dataset.load}/load`, {method:'POST'}); await refresh(); }
    catch (error) { fail(error); }
  }));
  document.querySelectorAll('.move-form').forEach(form=>form.addEventListener('submit', async event=>{
    event.preventDefault();
    try {
      await api(`/bookings/${form.dataset.id}/move`, {method:'POST', data:{trip_id:Number(new FormData(form).get('trip_id'))}});
      await refresh();
    } catch (error) { fail(error); }
  }));
}
