from fastapi import APIRouter, Depends, Response

from src.schemas.workflow import BookingForm, MoveForm, ResizeForm, TripForm
from src.services import trips
from src.services.security import current_user, staff

router = APIRouter()


@router.get('/trips')
def list_trips(_=Depends(current_user)):
    return trips.list_trips()


@router.post('/trips', status_code=201)
def create_trip(form: TripForm, actor=Depends(staff)):
    return trips.create_trip(form.trip_no, form.capacity_g, form.capacity_pieces, actor)


@router.get('/trips/{identity}')
def get_trip(identity: int, _=Depends(current_user)):
    return trips.trip_detail(identity)


@router.post('/trips/{identity}/bookings', status_code=201)
def book_batch(identity: int, form: BookingForm, response: Response, actor=Depends(staff)):
    result, created = trips.book_batch(identity, form.evidence_id, form.batch_id, actor)
    if not created:
        response.status_code = 200  # 重复提交返回已有预约，不重复占用
    return result


@router.post('/trips/{identity}/resize')
def resize_trip(identity: int, form: ResizeForm, actor=Depends(staff)):
    return trips.resize_trip(identity, form.capacity_g, form.capacity_pieces, actor)


@router.post('/bookings/{identity}/move')
def move_booking(identity: int, form: MoveForm, actor=Depends(staff)):
    return trips.move_booking(identity, form.trip_id, actor)


@router.post('/bookings/{identity}/load')
def load_booking(identity: int, actor=Depends(staff)):
    return trips.load_booking(identity, actor)


@router.get('/evidence/{identity}/bookings')
def evidence_bookings(identity: int, _=Depends(current_user)):
    return trips.evidence_bookings(identity)
