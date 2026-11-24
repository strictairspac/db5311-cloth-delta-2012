"""车次预约测试。需要可写的 PostgreSQL：默认 127.0.0.1:15432，可用
DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME 环境变量覆盖。
数据库不可达时自动跳过集成测试。"""
import os
import uuid

os.environ.setdefault('DB_DRIVER', 'postgresql+psycopg')
os.environ.setdefault('DB_HOST', '127.0.0.1')
os.environ.setdefault('DB_PORT', '15432')
os.environ.setdefault('DB_USER', 'app')
os.environ.setdefault('DB_PASSWORD', 'local-db-demo')
os.environ.setdefault('DB_NAME', 'workflow_test')
os.environ.setdefault('AUTH_KEY', 'test-key')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')

import psycopg
import pytest

ADMIN_DSN = dict(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),
                 user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD'],
                 dbname='postgres', autocommit=True)
TEST_DB = os.environ['DB_NAME']

DEMO_CUTS = [
    {'等级': 'A级', '批次': 'BL-102', '修订号': 1, '转裁重量（克）': 12000, '转裁件数': 8,
     '转出后在库重量（克）': 18000, '转出后在库件数': 12},
    {'等级': 'B级', '批次': 'BL-101', '修订号': 3, '转裁重量（克）': 20000, '转裁件数': 13,
     '转出后在库重量（克）': 0, '转出后在库件数': 0},
]


def _ensure_database():
    with psycopg.connect(**ADMIN_DSN) as conn:
        exists = conn.execute('SELECT 1 FROM pg_database WHERE datname = %s', (TEST_DB,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')


@pytest.fixture(scope='session')
def database():
    try:
        _ensure_database()
    except Exception as exc:
        pytest.skip(f'PostgreSQL 不可达：{exc}')
    from src.models.tables import schema
    from src.config.settings import engine
    schema.create_all(engine)
    yield engine


@pytest.fixture(autouse=True)
def clean_tables(database):
    from src.models.tables import audit, bookings, cases, evidence, runs, trips, users
    with database.begin() as conn:
        for table in (bookings, trips, evidence, runs, cases, audit, users):
            conn.execute(table.delete())


@pytest.fixture()
def client(database):
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def users(database):
    from src.constants.workflow import Role
    from src.models.tables import users as table
    from src.services.security import hash_password
    with database.begin() as conn:
        for role in Role:
            conn.execute(table.insert().values(username=role.value, role=role.value,
                                               password_hash=hash_password('Demo-Walkthrough-2026')))


def token(client, username):
    response = client.post('/login', json={'username': username, 'password': 'Demo-Walkthrough-2026'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def make_trip(client, trip_no='CD-0901', capacity_g=40000, capacity_pieces=25, role='operator'):
    response = client.post('/trips', json={'trip_no': trip_no, 'capacity_g': capacity_g,
                                           'capacity_pieces': capacity_pieces},
                           headers=token(client, role))
    assert response.status_code == 201, response.text
    return response.json()['id']


def book(client, trip_id, evidence_id, batch_id, role='operator'):
    return client.post(f'/trips/{trip_id}/bookings',
                       json={'evidence_id': evidence_id, 'batch_id': batch_id},
                       headers=token(client, role))


def make_evidence(client, database, cuts, sign=True):
    """写入一张配额单证据（可选签署），返回 evidence_id。"""
    from src.models.tables import cases, evidence, runs
    from src.utils.time import utc_time
    report = {'summary': f'库存全量对账一致，建议转裁 {len(cuts)} 批。', 'metrics': {},
              'sections': [{'title': '建议转裁批次', 'rows': cuts}]}
    with database.begin() as conn:
        case_id = conn.execute(cases.insert().values(
            slug=uuid.uuid4().hex, title='测试档案', payload={}, author='operator',
            created_at=utc_time())).inserted_primary_key[0]
        run_id = conn.execute(runs.insert().values(
            case_id=case_id, status='complete', owner='operator',
            created_at=utc_time())).inserted_primary_key[0]
        evidence_id = conn.execute(evidence.insert().values(
            run_id=run_id, report=report, digest=uuid.uuid4().hex)).inserted_primary_key[0]
    if sign:
        response = client.post(f'/evidence/{evidence_id}/review', json={'note': '同意发单'},
                               headers=token(client, 'reviewer'))
        assert response.status_code == 200, response.text
    return evidence_id


@pytest.fixture()
def signed_evidence(client, users, database):
    """一张已签署配额单，含 BL-102（12000 克 / 8 件）与 BL-101（20000 克 / 13 件）。"""
    return make_evidence(client, database, [dict(row) for row in DEMO_CUTS])
