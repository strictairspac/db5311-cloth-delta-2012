"""裁切区车次预约：已签配额单的转裁批次按车次预约、换车、装车与减容。

预约单位是已签署配额单（证据）里「建议转裁批次」的一行，(evidence_id, batch_id)
唯一对应一条预约记录，因此重试同一请求不会重复占用车次容量。预约、换车与减容
都在单事务内先锁定车次行再核对重量与件数上限，并发提交不会突破限额。
"""

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import engine
from src.constants.workflow import Booking
from src.models.tables import bookings, evidence, trips
from src.services.workflow import found, log
from src.utils.time import utc_time

CUTS_SECTION = '建议转裁批次'
ACTIVE = (Booking.BOOKED, Booking.LOADED)


def _offer(frontier, candidate):
    count, weight, pieces = candidate
    for c, w, p in frontier:
        if c >= count and w <= weight and p <= pieces:
            return
    frontier[:] = [state for state in frontier
                   if not (count >= state[0] and weight <= state[1] and pieces <= state[2])]
    frontier.append(candidate)


def plan_retention(batches, weight_limit, piece_limit):
    """从按预约先后排列的批次中选出能留下的最多批次。

    返回 (kept, squeezed) 两组下标。数量相同的方案里优先留下先预约的批次，
    因此同一输入永远得到同一结果。状态数为帕累托前沿，随批次数多项式增长。
    """
    suffix = [None] * (len(batches) + 1)
    suffix[len(batches)] = [(0, 0, 0)]
    for i in range(len(batches) - 1, -1, -1):
        frontier = list(suffix[i + 1])
        for count, weight, pieces in suffix[i + 1]:
            _offer(frontier, (count + 1, weight + batches[i]['weight_g'], pieces + batches[i]['pieces']))
        suffix[i] = frontier
    need = max(count for count, weight, pieces in suffix[0]
               if weight <= weight_limit and pieces <= piece_limit)
    kept, squeezed = [], []
    weight = pieces = 0
    for i, batch in enumerate(batches):
        next_weight = weight + batch['weight_g']
        next_pieces = pieces + batch['pieces']
        take = (need > 0 and next_weight <= weight_limit and next_pieces <= piece_limit
                and any(count == need - 1 and w <= weight_limit - next_weight and p <= piece_limit - next_pieces
                        for count, w, p in suffix[i + 1]))
        if take:
            kept.append(i)
            weight, pieces, need = next_weight, next_pieces, need - 1
        else:
            squeezed.append(i)
    return kept, squeezed


def transfer_batches(report):
    """配额单证据中可预约的转裁批次，按批次号去重前的原始顺序。"""
    for section in report.get('sections', []):
        if section.get('title') == CUTS_SECTION:
            return section.get('rows') or []
    return []


def _cut_row(report, batch_id):
    for row in transfer_batches(report):
        if row['批次'] == batch_id:
            return row
    raise HTTPException(422, f'批次「{batch_id}」不在该配额单的建议转裁批次中')


def _signed_evidence(conn, evidence_id):
    row = found(conn, evidence, evidence_id)
    if not row['reviewed_at']:
        raise HTTPException(409, '该仓位配额单尚未签署复核，不能预约车次')
    return row


def _lock_trip(conn, trip_id):
    row = conn.execute(select(trips).where(trips.c.id == trip_id)
                       .with_for_update()).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该车次')
    return dict(row)


def _usage(conn, trip_id, statuses=ACTIVE):
    row = conn.execute(select(func.coalesce(func.sum(bookings.c.weight_g), 0).label('weight_g'),
                              func.coalesce(func.sum(bookings.c.pieces), 0).label('pieces'))
                       .where(bookings.c.trip_id == trip_id,
                              bookings.c.status.in_(statuses))).mappings().one()
    return dict(row)


def _fits(trip, usage, weight_g, pieces):
    return (usage['weight_g'] + weight_g <= trip['capacity_g']
            and usage['pieces'] + pieces <= trip['capacity_pieces'])


def _get_booking(conn, evidence_id, batch_id):
    row = conn.execute(select(bookings).where(bookings.c.evidence_id == evidence_id,
                                              bookings.c.batch_id == batch_id)).mappings().first()
    return dict(row) if row else None


def _booking_view(conn, row):
    trip_no = None
    if row['trip_id'] is not None:
        trip_no = conn.execute(select(trips.c.trip_no).where(trips.c.id == row['trip_id'])).scalar_one_or_none()
    return {**row, 'trip_no': trip_no}


def create_trip(trip_no, capacity_g, capacity_pieces, actor):
    trip_no = trip_no.strip()
    if not 1 <= len(trip_no) <= 64 or not trip_no.isprintable():
        raise HTTPException(422, '车次号须为 1 到 64 个可打印字符')
    with engine.begin() as conn:
        try:
            trip_id = conn.execute(trips.insert().values(
                trip_no=trip_no, capacity_g=capacity_g, capacity_pieces=capacity_pieces,
                created_by=actor, created_at=utc_time(), updated_at=utc_time())).inserted_primary_key[0]
        except IntegrityError:
            raise HTTPException(409, f'车次「{trip_no}」已存在') from None
        log(conn, actor, 'create_trip', trip_id)
        return found(conn, trips, trip_id)


def list_trips():
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(select(trips).order_by(trips.c.id)).mappings()]
        for row in rows:
            booked = _usage(conn, row['id'], (Booking.BOOKED,))
            loaded = _usage(conn, row['id'], (Booking.LOADED,))
            row.update({
                'booked_weight': booked['weight_g'], 'booked_pieces': booked['pieces'],
                'loaded_weight': loaded['weight_g'], 'loaded_pieces': loaded['pieces'],
                'remaining_weight': row['capacity_g'] - booked['weight_g'] - loaded['weight_g'],
                'remaining_pieces': row['capacity_pieces'] - booked['pieces'] - loaded['pieces'],
            })
        return rows


def trip_detail(identity):
    with engine.connect() as conn:
        trip = found(conn, trips, identity)
        rows = conn.execute(select(bookings).where(bookings.c.trip_id == identity)
                            .order_by(bookings.c.booked_at, bookings.c.id)).mappings().all()
        trip['bookings'] = [_booking_view(conn, dict(row)) for row in rows]
        return trip


def book_batch(trip_id, evidence_id, batch_id, actor):
    """预约批次到车次。同一 (配额单, 批次) 重复请求返回已有预约，不重复占用。

    返回 (预约视图, 是否新占用)：重试已占车的同一预约时为 False。"""
    now = utc_time()
    with engine.begin() as conn:
        trip = _lock_trip(conn, trip_id)
        signed = _signed_evidence(conn, evidence_id)
        cut = _cut_row(signed['report'], batch_id)
        existing = _get_booking(conn, evidence_id, batch_id)
        if existing and existing['status'] != Booking.WAITLISTED:
            if existing['trip_id'] == trip_id:
                return _booking_view(conn, existing), False  # 重试同一预约，直接返回
            raise HTTPException(409, '该批次已约在其他车次，如需调整请使用换车')
        usage = _usage(conn, trip_id)
        if not _fits(trip, usage, cut['转裁重量（克）'], cut['转裁件数']):
            raise HTTPException(409, '该车次的重量或件数容量不足，无法预约')
        values = {'trip_id': trip_id, 'status': Booking.BOOKED, 'booked_at': now, 'updated_at': now}
        if existing:  # 候补批次重新预约
            booking_id = existing['id']
            conn.execute(bookings.update().where(bookings.c.id == booking_id).values(**values))
        else:
            try:
                booking_id = conn.execute(bookings.insert().values(
                    evidence_id=evidence_id, batch_id=batch_id, revision=cut['修订号'],
                    grade=cut['等级'], weight_g=cut['转裁重量（克）'], pieces=cut['转裁件数'],
                    **values)).inserted_primary_key[0]
            except IntegrityError:
                raise HTTPException(409, '该批次已存在预约，请勿重复提交') from None
        log(conn, actor, 'book_trip', booking_id)
        return _booking_view(conn, dict(conn.execute(
            select(bookings).where(bookings.c.id == booking_id)).mappings().one())), True


def move_booking(booking_id, trip_id, actor):
    """换车：目标车次容量不足或预约状态不符时保留原预约。重复同一换车请求是幂等的。"""
    with engine.begin() as conn:
        current = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
        if current is None:
            raise HTTPException(404, '找不到该预约')
        if current['trip_id'] == trip_id and current['status'] != Booking.WAITLISTED:
            return _booking_view(conn, dict(current))  # 重试已成功的换车
        if current['status'] == Booking.LOADED:
            raise HTTPException(409, '已装车的批次不能换车')
        if current['status'] == Booking.WAITLISTED:
            raise HTTPException(409, '候补批次尚未占车，请使用预约')
        # 先锁源车次与目标车次（按 id 升序避免死锁），再锁预约行。
        targets = {identity for identity in (current['trip_id'], trip_id) if identity is not None}
        locked_trips = {identity: _lock_trip(conn, identity) for identity in sorted(targets)}
        target = locked_trips[trip_id]
        locked = dict(conn.execute(select(bookings).where(bookings.c.id == booking_id)
                                   .with_for_update()).mappings().one())
        if locked['trip_id'] != current['trip_id'] or locked['status'] != current['status']:
            raise HTTPException(409, '该预约状态已变化，请刷新后重试')
        usage = _usage(conn, trip_id)
        if not _fits(target, usage, locked['weight_g'], locked['pieces']):
            raise HTTPException(409, '目标车次容量不足，保留原预约')
        conn.execute(bookings.update().where(bookings.c.id == booking_id)
                     .values(trip_id=trip_id, booked_at=utc_time(), updated_at=utc_time()))
        log(conn, actor, 'move_booking', booking_id)
        return _booking_view(conn, dict(conn.execute(
            select(bookings).where(bookings.c.id == booking_id)).mappings().one()))


def load_booking(booking_id, actor):
    with engine.begin() as conn:
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).mappings().first()
        if row is None:
            raise HTTPException(404, '找不到该预约')
        if row['status'] == Booking.LOADED:
            return _booking_view(conn, dict(row))  # 重复装车上报按成功处理
        if row['status'] != Booking.BOOKED:
            raise HTTPException(409, '只有已预约的批次才能装车')
        conn.execute(bookings.update().where(bookings.c.id == booking_id)
                     .values(status=Booking.LOADED, updated_at=utc_time()))
        log(conn, actor, 'load_booking', booking_id)
        return _booking_view(conn, dict(conn.execute(
            select(bookings).where(bookings.c.id == booking_id)).mappings().one()))


def resize_trip(trip_id, capacity_g, capacity_pieces, actor):
    """调整车次限额。已装车批次不动且必须装得下，否则拒绝减容；其余批次按
    能留下最多批次的方案保留，被挤出的批次转候补，其他车次不受影响。"""
    with engine.begin() as conn:
        trip = _lock_trip(conn, trip_id)
        loaded = _usage(conn, trip_id, (Booking.LOADED,))
        if loaded['weight_g'] > capacity_g or loaded['pieces'] > capacity_pieces:
            raise HTTPException(409, '新限额装不下已装车批次，拒绝减容')
        rows = [dict(row) for row in conn.execute(
            select(bookings).where(bookings.c.trip_id == trip_id, bookings.c.status == Booking.BOOKED)
            .order_by(bookings.c.booked_at, bookings.c.id)).mappings()]
        kept, squeezed = plan_retention(rows, capacity_g - loaded['weight_g'],
                                        capacity_pieces - loaded['pieces'])
        now = utc_time()
        for index in squeezed:
            conn.execute(bookings.update().where(bookings.c.id == rows[index]['id'])
                         .values(trip_id=None, status=Booking.WAITLISTED, updated_at=now))
        conn.execute(trips.update().where(trips.c.id == trip_id)
                     .values(capacity_g=capacity_g, capacity_pieces=capacity_pieces, updated_at=now))
        log(conn, actor, 'resize_trip', trip_id)
        return {'trip': found(conn, trips, trip_id),
                'kept': [rows[index]['id'] for index in kept],
                'squeezed': [rows[index]['id'] for index in squeezed]}


def evidence_bookings(identity):
    """配额单页面用：每个建议转裁批次约在哪班车，或仍是候补/未预约。"""
    with engine.connect() as conn:
        row = found(conn, evidence, identity)
        rows = {item['batch_id']: item for item in
                (dict(r) for r in conn.execute(select(bookings).where(bookings.c.evidence_id == identity)).mappings())}
        result = []
        for cut in transfer_batches(row['report']):
            booking = rows.get(cut['批次'])
            entry = {'batch_id': cut['批次'], 'revision': cut['修订号'], 'grade': cut['等级'],
                     'weight_g': cut['转裁重量（克）'], 'pieces': cut['转裁件数'],
                     'status': 'none', 'booking_id': None, 'trip_id': None, 'trip_no': None}
            if booking:
                entry.update({'status': booking['status'], 'booking_id': booking['id'],
                              'trip_id': booking['trip_id']})
                if booking['trip_id'] is not None:
                    entry['trip_no'] = conn.execute(select(trips.c.trip_no)
                                                    .where(trips.c.id == booking['trip_id'])).scalar_one_or_none()
            result.append(entry)
        return result
