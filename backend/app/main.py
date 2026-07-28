from fastapi import FastAPI

from app.database import Base, engine
from app.routers import router


Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="RoleRadar API",
    version="0.1.0",
    description="Backend API for RoleRadar job intelligence and skill-gap analysis.",
)

app.include_router(router)
