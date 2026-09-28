from fastapi import APIRouter, Depends

from ...service.chat_service import ChatService
from ..dependencies import get_chat_service, verify_admin_key

router = APIRouter(prefix="/admin/experiments", dependencies=[Depends(verify_admin_key)])


@router.get("")
async def experiments(service: ChatService = Depends(get_chat_service)) -> dict:
    return service.experiment_status()
