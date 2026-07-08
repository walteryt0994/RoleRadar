from fastapi import FastAPI
from pydantic import BaseModel
from app.parser import extract_skills


class JobDescription(BaseModel):
    text:str

app = FastAPI(
    title="RoleRadar API",
    version="0.1.0",
    description="Backend API for RoleRadar job intelligence and skill-gap analysis.",
)


@app.get("/")
def read_root():
    return {"message": "RoleRadar backend is running"}   

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/parse-jd")
def parse_jd(payload: JobDescription):

    found_skills = extract_skills(payload.text)

    return{"message": "JD parser endpoint is ready" ,
           "jd_text_length": len(payload.text),
           "skills":found_skills,
           }