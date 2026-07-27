from fastapi import APIRouter

from fire_viewer.api import admin, discovery, incidents, operator

api_router = APIRouter()
api_router.include_router(incidents.router, prefix="/incident")
# The discovery endpoints must precede the historical dynamic plural alias,
# otherwise `/incidents/search` would be interpreted as a fire identifier.
api_router.include_router(discovery.router)
api_router.include_router(
    incidents.router,
    prefix="/incidents",
    include_in_schema=False,
)
api_router.include_router(operator.router)
api_router.include_router(admin.router)
