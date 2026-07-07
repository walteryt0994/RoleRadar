 # RoleRadar

RoleRadar is a Job Intelligence & Skill-Gap Analysis Platform for students.

The goal of this project is to help users parse job descriptions, identify required skills, analyze skill gaps, and track job applications.

## Progress

### Day 1

Completed:

- Set up the project folder structure
- Created and activated a Python virtual environment
- Built a minimal FastAPI backend
- Added backend endpoints:
  - `GET /`
  - `GET /health`
  - `POST /parse-jd`
- Added a rule-based JD parser using a known skills list
- Built a minimal Streamlit frontend
- Added a JD text area and character counter
- Verified backend and frontend locally

### Day 2

Completed:

- Added `requests` to the frontend dependencies
- Connected the Streamlit frontend to the FastAPI `/parse-jd` endpoint
- Added an `Analyze JD` button
- Sent JD text from the frontend to the backend using a POST request
- Displayed detected skills in the frontend
- Added a warning for empty JD input
- Committed the Day 2 frontend-backend integration


## Tech Stack

- Python
- FastAPI
- Uvicorn
- Streamlit

Planned later:

- PostgreSQL
- SQLAlchemy or SQLModel
- Plotly
- LLM API integration

## Project Structure

```txt
roleradar/
├── backend/
│   ├── app/
│   │   └── main.py
│   └── requirements.txt
├── frontend/
│   ├── requirements.txt
│   └── streamlit_app.py
└── README.md
```

## Run Backend

From the project root:

```bash
cd backend
uvicorn app.main:app --reload
```

Backend URL:

```txt
http://127.0.0.1:8000
```

API docs:

```txt
http://127.0.0.1:8000/docs
```

## Run Frontend

Open another terminal.

From the project root:

```bash
cd frontend
streamlit run streamlit_app.py
```

Frontend URL:

```txt
http://localhost:8501
```

## Current Notes

The backend and frontend both run locally, but they are not connected yet.

Next step:

- Connect the Streamlit frontend to the FastAPI `/parse-jd` endpoint.