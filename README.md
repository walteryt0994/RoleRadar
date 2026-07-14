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

### Day 3

Completed:

- Refactored JD parsing logic into a separate parser module
- Added `backend/app/parser.py`
- Moved `KNOWN_SKILLS` into the parser module
- Added `extract_skills()` helper function
- Updated `/parse-jd` to call `extract_skills()`
- Verified the FastAPI `/parse-jd` endpoint after refactor
- Verified the Streamlit frontend still displays detected skills

### Day 4

Completed:

- Started Stage 2 : SkillMap and FitScore preparation
- Added a user skills input field to the Streamlit frontend
- Parsed comma-separated user skills into a Python list
- Trimmed extra spaces from user skill input
- Filtered out empty skill entries
- Displayed user skills in the frontend
- Verified the existing JD skill extraction still works after the frontend update
- Committed and pushed the Day 4 user skills input features

### Day 5

Completed:

- Started comparing detected JD skills with user skills
- Added `jd_skills` as a named frontend variable from the `parse-jd` response
- Added case-insensitive matching between JD skills and user skills
- Displayed matched skills in the Streamlit frontend
- Displayed missing skills in the Streamlit frontend
- Added empty-state messages for no matched skills and no missing skills
- Improved user skills display formatting
- Kept the comparison logic in the frontend for now
- Did not implement FitScore yet

### Day 6

Completed:

- Added a basic FitScore calculation
- Calculated FitScore from matched skills and detected JD skills
- Formatted FitScore as a rounded percentage
- Added protection against division by ZERO
- Added an empty state when no known skills are detected
- Hid matched and missing skill sections when no JD skills are detected
- Verified partial-match, full-match, and empty-detection scenarios
- Kept FitScore calculation in the Streamlit frontend for now 

### Day 7

Completed:

- Added `backend/app/analyzer.py`
- Added the `analyze_skill_gap()` backend helper function
- Moved case-insensitive skill comparison into the backend
- Moved matched skills, missing skills, and FitScore calculation into the backend
- Added the `JobAnalysisRequest` Pydantic model
- Added the `POST /analyze-job` endpoint
- Reused `extract_skills()` inside the new analysis endpoint
- Updated the Streamlit frontend to send JD text and user skills together
- Updated the frontend to read analysis results from the backend
- Removed duplicate skill-gap and FitScore calculations from the frontend
- Verified partial-match, full-match, no-match, and empty-detection scenarios
- Kept the existing `POST /parse-jd` endpoint available


### Day 8

Completed:

- Started Stage 3: Application Tracker
- Added the `ApplicationCreate` Pydantic request model
- Defined company, job title, status, FitScore, matched skills, and missing skills fields
- Added temporary in-memory application storage
- Added the `POST /applications` endpoint
- Converted validated Pydantic payloads into dictionaries
- Added temporary sequential application IDs
- Verified successful application creation through Swagger
- Verified invalid JSON and invalid FitScore validation errors
- Documented that in-memory records are lost when the backend restarts
- Identified duplicate POST requests as a limitation for future handling

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

The backend and frontend run locally and are connected through the FastAPI `/parse-jd` endpoint.

Current focus:

- Stage 2: SkillMap and FitScore
- User skills input has been added to the frontend
- Next step: compare detected JD skills with user skills