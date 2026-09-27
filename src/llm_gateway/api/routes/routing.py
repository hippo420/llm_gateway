"""Health routing observations for this worker, protected by the admin credential."""

from typing import Any

from fastapi import APIRouter, Depends

from ...service.chat_service import ChatService
from ..dependencies import get_chat_service, verify_admin_key

router = APIRouter(
    prefix="/admin/routing", dependencies=[Depends(verify_admin_key)], tags=["routing"]
)


@router.get("/status")
async def routing_status(service: ChatService = Depends(get_chat_service)) -> dict[str, Any]:
    return service.routing_status()
