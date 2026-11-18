from fastapi import APIRouter

from app.routers import core

api = APIRouter()
api.include_router(core.router)
