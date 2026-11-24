"""车次预约 API 与并发集成测试。"""
from concurrent.futures import ThreadPoolExecutor

from conftest import book, make_evidence, make_trip, token


def test_create_trip_validation_and_conflict(client, users):
    operator = token(client, 'operator')
    assert client.post('/trips', json={'trip_no': 'CD-1', 'capacity_g': -1,
                                       'capacity_pieces': 5}, headers=operator).status_code == 422
    assert client.post('/trips', json={'trip_no': '  ', 'capacity_g': 1,
                                       'capacity_pieces': 1}, headers=operator).status_code == 422
    make_trip(client, 'CD-1')
    assert client.post('/trips', json={'trip_no': 'CD-1', 'capacity_g': 1,
                                       'capacity_pieces': 1}, headers=operator).status_code == 409


def test_viewer_cannot_manage_trips_but_can_read(client, users):
    viewer = token(client, 'viewer')
    assert client.post('/trips', json={'trip_no': 'CD-1', 'capacity_g': 1,
                                       'capacity_pieces': 1}, headers=viewer).status_code == 403
    assert client.get('/trips', headers=viewer).status_code == 200


def test_unsigned_evidence_cannot_book(client, users, database):
    cuts = [{'等级': 'A级', '批次': 'BL-1', '修订号': 1, '转裁重量（克）': 100, '转裁件数': 1,
             '转出后在库重量（克）': 0, '转出后在库件数': 0}]
    evidence_id = make_evidence(client, database, cuts, sign=False)
    trip_id = make_trip(client)
    assert book(client, trip_id, evidence_id, 'BL-1').status_code == 409


def test_booking_requires_listed_batch(client, signed_evidence):
    trip_id = make_trip(client)
    assert book(client, trip_id, signed_evidence, 'BL-999').status_code == 422


def test_two_signed_orders_share_one_trip(client, signed_evidence, database):
    other = make_evidence(client, database, [
        {'等级': 'C级', '批次': 'BL-301', '修订号': 2, '转裁重量（克）': 7000, '转裁件数': 6,
         '转出后在库重量（克）': 0, '转出后在库件数': 0}])
    trip_id = make_trip(client)
    assert book(client, trip_id, signed_evidence, 'BL-102').status_code == 201
    assert book(client, trip_id, other, 'BL-301').status_code == 201
    trip = client.get(f'/trips/{trip_id}', headers=token(client, 'viewer')).json()
    assert len(trip['bookings']) == 2
    assert {row['evidence_id'] for row in trip['bookings']} == {signed_evidence, other}


def test_capacity_limits_are_enforced(client, signed_evidence, database):
    trip_id = make_trip(client, capacity_g=13000, capacity_pieces=100)
    assert book(client, trip_id, signed_evidence, 'BL-102').status_code == 201  # 12000 克
    assert book(client, trip_id, signed_evidence, 'BL-101').status_code == 409  # 再加 20000 克超重
    other = make_evidence(client, database, [
        {'等级': 'C级', '批次': 'BL-301', '修订号': 2, '转裁重量（克）': 7000, '转裁件数': 6,
         '转出后在库重量（克）': 0, '转出后在库件数': 0},
        {'等级': 'C级', '批次': 'BL-302', '修订号': 1, '转裁重量（克）': 1000, '转裁件数': 5,
         '转出后在库重量（克）': 0, '转出后在库件数': 0}])
    trip_id = make_trip(client, trip_no='CD-0902', capacity_g=100000, capacity_pieces=10)
    assert book(client, trip_id, other, 'BL-301').status_code == 201   # 6 件
    assert book(client, trip_id, other, 'BL-302').status_code == 409   # 再加 5 件超件数


def test_retry_does_not_double_occupy(client, signed_evidence):
    trip_id = make_trip(client)
    first = book(client, trip_id, signed_evidence, 'BL-102')
    assert first.status_code == 201
    retry = book(client, trip_id, signed_evidence, 'BL-102')
    assert retry.status_code == 200
    assert retry.json()['id'] == first.json()['id']
    trips = client.get('/trips', headers=token(client, 'viewer')).json()
    assert trips[0]['booked_weight'] == 12000  # 只占用一次


def test_same_batch_cannot_book_two_trips(client, signed_evidence):
    first = make_trip(client, 'CD-0901')
    second = make_trip(client, 'CD-0902')
    assert book(client, first, signed_evidence, 'BL-102').status_code == 201
    assert book(client, second, signed_evidence, 'BL-102').status_code == 409


def test_move_failure_keeps_original_and_retry_is_safe(client, signed_evidence):
    source = make_trip(client, 'CD-0901')
    target = make_trip(client, 'CD-0902', capacity_g=100, capacity_pieces=1)
    booking = book(client, source, signed_evidence, 'BL-102').json()
    operator = token(client, 'operator')
    failed = client.post(f"/bookings/{booking['id']}/move", json={'trip_id': target}, headers=operator)
    assert failed.status_code == 409
    assert client.get(f'/trips/{source}', headers=operator).json()['bookings'][0]['id'] == booking['id']
    # 扩大目标车次限额后换车成功；重复同一换车请求幂等，不重复占用
    assert client.post(f'/trips/{target}/resize',
                       json={'capacity_g': 40000, 'capacity_pieces': 25}, headers=operator).status_code == 200
    moved = client.post(f"/bookings/{booking['id']}/move", json={'trip_id': target}, headers=operator)
    assert moved.status_code == 200
    assert moved.json()['trip_id'] == target
    again = client.post(f"/bookings/{booking['id']}/move", json={'trip_id': target}, headers=operator)
    assert again.status_code == 200
    trips = {row['id']: row for row in client.get('/trips', headers=operator).json()}
    assert trips[source]['booked_weight'] == 0
    assert trips[target]['booked_weight'] == 12000


def test_loaded_batch_cannot_move(client, signed_evidence):
    source = make_trip(client, 'CD-0901')
    target = make_trip(client, 'CD-0902')
    booking = book(client, source, signed_evidence, 'BL-102').json()
    operator = token(client, 'operator')
    assert client.post(f"/bookings/{booking['id']}/load", headers=operator).status_code == 200
    assert client.post(f"/bookings/{booking['id']}/move",
                       json={'trip_id': target}, headers=operator).status_code == 409
    # 重复装车上报按成功处理
    assert client.post(f"/bookings/{booking['id']}/load", headers=operator).status_code == 200


def test_resize_rejects_when_loaded_do_not_fit(client, signed_evidence):
    trip_id = make_trip(client)
    booking = book(client, trip_id, signed_evidence, 'BL-102').json()
    operator = token(client, 'operator')
    client.post(f"/bookings/{booking['id']}/load", headers=operator)
    rejected = client.post(f'/trips/{trip_id}/resize',
                           json={'capacity_g': 11000, 'capacity_pieces': 25}, headers=operator)
    assert rejected.status_code == 409
    trip = client.get(f'/trips/{trip_id}', headers=operator).json()
    assert trip['capacity_g'] == 40000  # 减容被拒绝，限额不变


def test_resize_squeezes_to_waitlist_and_rebook(client, signed_evidence):
    trip_id = make_trip(client)
    operator = token(client, 'operator')
    first = book(client, trip_id, signed_evidence, 'BL-102').json()   # 12000 克 8 件，先约
    second = book(client, trip_id, signed_evidence, 'BL-101').json()  # 20000 克 13 件，已装车
    client.post(f"/bookings/{second['id']}/load", headers=operator)
    # 减容到 25000 克 / 20 件：已装车的 BL-101 不动，剩余 5000 克 / 7 件装不下 BL-102。
    result = client.post(f'/trips/{trip_id}/resize',
                         json={'capacity_g': 25000, 'capacity_pieces': 20}, headers=operator)
    assert result.status_code == 200, result.text
    assert result.json()['squeezed'] == [first['id']]
    rows = client.get(f'/evidence/{signed_evidence}/bookings', headers=operator).json()
    status = {row['batch_id']: (row['status'], row['trip_no']) for row in rows}
    assert status['BL-101'] == ('loaded', 'CD-0901')
    assert status['BL-102'] == ('waitlisted', None)
    # 候补批次可以重新约到别的车次
    other = make_trip(client, 'CD-0903')
    assert book(client, other, signed_evidence, 'BL-102').status_code == 201


def test_resize_keeps_max_batches_and_other_trips_untouched(client, signed_evidence, database):
    trip_id = make_trip(client, 'CD-0901')
    other = make_trip(client, 'CD-0902')
    operator = token(client, 'operator')
    book(client, trip_id, signed_evidence, 'BL-102')  # 12000/8，先约
    book(client, trip_id, signed_evidence, 'BL-101')  # 20000/13，后约
    other_evidence = make_evidence(client, database, [
        {'等级': 'C级', '批次': 'BL-301', '修订号': 2, '转裁重量（克）': 7000, '转裁件数': 6,
         '转出后在库重量（克）': 0, '转出后在库件数': 0}])
    book(client, other, other_evidence, 'BL-301')
    # 减容到 20000 克 / 13 件：两批各自都能单独留下，先约的 BL-102 优先留下。
    result = client.post(f'/trips/{trip_id}/resize',
                         json={'capacity_g': 20000, 'capacity_pieces': 13}, headers=operator)
    assert result.status_code == 200
    rows = {row['batch_id']: row['status']
            for row in client.get(f'/evidence/{signed_evidence}/bookings', headers=operator).json()}
    assert rows['BL-102'] == 'booked'
    assert rows['BL-101'] == 'waitlisted'
    # 别的车次照常：限额与预约都不受影响
    other_trip = client.get(f'/trips/{other}', headers=operator).json()
    assert other_trip['capacity_g'] == 40000
    assert [row['batch_id'] for row in other_trip['bookings']] == ['BL-301']


def test_concurrent_bookings_never_exceed_capacity(client, signed_evidence, database):
    from src.services.trips import book_batch
    with database.begin() as conn:
        from src.models.tables import trips as trips_table
        from src.utils.time import utc_time
        tight_id = conn.execute(trips_table.insert().values(
            trip_no='CD-TIGHT', capacity_g=25000, capacity_pieces=15,
            created_by='operator', created_at=utc_time(), updated_at=utc_time())).inserted_primary_key[0]
        roomy_id = conn.execute(trips_table.insert().values(
            trip_no='CD-ROOMY', capacity_g=40000, capacity_pieces=25,
            created_by='operator', created_at=utc_time(), updated_at=utc_time())).inserted_primary_key[0]

    def attempt(args):
        trip_id, batch_id = args
        try:
            _, created = book_batch(trip_id, signed_evidence, batch_id, 'operator')
            return created
        except Exception:
            return False

    # 紧车次容量只装得下其中一批：8 个并发请求竞争两个批次
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(attempt, [(tight_id, batch) for batch in ['BL-102', 'BL-101'] * 4]))
    # 宽车次装得下另一张配额单的两批：并发预约都应成功且各占一次
    other = make_evidence(client, database, [
        {'等级': 'A级', '批次': 'BL-401', '修订号': 1, '转裁重量（克）': 8000, '转裁件数': 5,
         '转出后在库重量（克）': 0, '转出后在库件数': 0},
        {'等级': 'D级', '批次': 'BL-501', '修订号': 1, '转裁重量（克）': 3000, '转裁件数': 2,
         '转出后在库重量（克）': 0, '转出后在库件数': 0}])

    def attempt_other(batch_id):
        try:
            _, created = book_batch(roomy_id, other, batch_id, 'operator')
            return created
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt_other, ['BL-401', 'BL-501'] * 4))

    viewer = token(client, 'viewer')
    trips = {row['trip_no']: row for row in client.get('/trips', headers=viewer).json()}
    assert trips['CD-TIGHT']['booked_weight'] <= 25000
    assert trips['CD-TIGHT']['booked_pieces'] <= 15
    tight = client.get(f'/trips/{tight_id}', headers=viewer).json()
    assert len(tight['bookings']) == 1  # 只挤进一批，且没有重复占用
    roomy = client.get(f'/trips/{roomy_id}', headers=viewer).json()
    assert len(roomy['bookings']) == 2
    assert {row['batch_id'] for row in roomy['bookings']} == {'BL-401', 'BL-501'}
    assert results.count(True) == 2  # 每批只新占用一次，其余是幂等返回


def test_concurrent_same_batch_booking_stays_single(client, signed_evidence, database):
    from src.services.trips import book_batch
    with database.begin() as conn:
        from src.models.tables import trips as trips_table
        from src.utils.time import utc_time
        trip_id = conn.execute(trips_table.insert().values(
            trip_no='CD-SAME', capacity_g=40000, capacity_pieces=25,
            created_by='operator', created_at=utc_time(), updated_at=utc_time())).inserted_primary_key[0]

    def attempt(_):
        try:
            return book_batch(trip_id, signed_evidence, 'BL-102', 'operator')[0]['id']
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(attempt, range(6)))
    detail = client.get(f'/trips/{trip_id}', headers=token(client, 'viewer')).json()
    assert len(detail['bookings']) == 1  # 无论成败时序，最终只有一条预约
    assert detail['bookings'][0]['weight_g'] == 12000
