# RoleRadar

RoleRadar is a Job Intelligence & Skill-Gap Analysis Platform for students.

The goal of this project is to help users parse job descriptions, identify required skills, analyze skill gaps, and track job applications.

## V1 Features

- Extract required skills from job descriptions
- Compare detected job skills with user skills
- Calculate a FitScore
- Display matched and missing skills
- Save application records to SQLite
- Track application status changes
- Display total applications, status counts, and average FitScore
- Visualize application status and FitScore distributions
- Display the Top 5 Missing Skills across saved applications
- Preserve application history across backend and frontend restarts

## How V1 Works

1. The user enters a job description and a comma-separated skills list.
2. The Streamlit frontend sends the input to `POST /analyze-job`.
3. The FastAPI backend extracts job skills and calculates matched skills, missing skills, and FitScore.
4. The frontend displays the analysis result.
5. The user adds company, job title, and application status information.
6. The frontend saves the application through `POST /applications`.
7. The Dashboard loads saved records through `GET /applications`.
8. The user can update an application status through `PATCH /applications/{application_id}`.
9. Dashboard metrics, charts, and application history refresh from the updated records.

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

### Day 9

Completed:

- Replaced temporary in-memory application storage with SQLite
- Added SQLAlchemy as the backend ORM
- Added the database engine, session factory, declarative base, and per-request database dependency
- Added the `Application` SQLAlchemy ORM model
- Created the `applications` database table
- Added database-generated application IDs and creation timestamps
- Stored matched and missing skills using JSON columns
- Updated `POST /applications` to persist records in SQLite
- Added the `ApplicationResponse` Pydantic response model
- Added the `GET /applications` endpoint
- Verified saved application records through Swagger
- Verified that application records persist after a backend restart
- Added the local SQLite database file to `.gitignore`


### Day 10

Completed:

- Connected the Streamlit frontend to the persistent Application Tracker API
- Added Streamlit session state for preserving analysis results across reruns
- Added company, job title, and application status inputs
- Reused FitScore, matched skills, and missing skills in application payloads
- Added the `Save Application` frontend workflow
- Added required-field validation before saving
- Added current-session duplicate submission protection
- Connected the frontend to `GET /applications`
- Added expandable application history records
- Verified application creation through the frontend
- Verified that application history persists after backend and frontend restarts

### Day 11

Completed:

- Added the `ApplicationStatusUpdate` Pydantic request model
- Added the `PATCH /applications/{application_id}` endpoint
- Added application ID path parameter handling
- Queried application records by primary key
- Added `404 Not Found` handling for missing application records
- Added application status updates using SQLAlchemy ORM objects
- Committed and refreshed updated application records
- Returned updated records using the `ApplicationResponse` model
- Verified successful status updates through Swagger
- Verified missing-record errors through Swagger
- Added shared application status options in the Streamlit frontend
- Added per-record status selectors and update buttons
- Connected the frontend to the application status PATCH endpoint
- Added automatic history refresh after successful updates
- Disabled status update buttons when the selected status is unchanged
- Verified that updated statuses persist after backend and frontend restarts

### Day 12

Completed:

- Formally completed Stage 3: Application Tracker
- Started Stage 4: Dashboard and V1 Completion
- Reused application records returned by `GET /applications`
- Added total application count aggregation
- Added application status count aggregation
- Added average FitScore calculation
- Added empty-data and division-by-zero protection
- Added Dashboard metric cards using Streamlit columns
- Displayed Interested, Applied, Interviewing, Offer, and Rejected counts
- Verified Dashboard metrics with multiple application records
- Verified the Dashboard with an empty database
- Verified the Dashboard with a single application record
- Confirmed that Application History still displays correctly below the Dashboard

### Day 13

Completed:

- Added an Application Status Distribution bar chart
- Preserved the application workflow order in the status chart
- Defined four non-overlapping FitScore ranges
- Added FitScore range frequency counting
- Added a FitScore Distribution bar chart
- Flattened nested missing-skills lists across application records
- Added missing-skill frequency counting
- Sorted missing skills by frequency and selected the Top 5
- Added a horizontal Top Missing Skills bar chart
- Added an empty state when no missing skills are found
- Reused real application records returned by `GET /applications`
- Used Streamlit built-in charts without adding new dependencies
- Verified multiple-record, empty-database, single-record, and empty-missing-skills scenarios
- Confirmed that Application History still works below the Dashboard

### Day 14

Completed:

- Ran the final V1 regression test using an isolated SQLite database
- Verified job analysis, FitScore, matched skills, and missing skills
- Confirmed that analysis does not create an application record
- Verified application saving through the complete frontend-backend workflow
- Verified Dashboard metrics, charts, Top Missing Skills, and Application History
- Verified application status updates and Dashboard synchronization
- Verified data persistence after backend and frontend restarts
- Restored the original local application database after regression testing
- Cleaned frontend formatting without changing application behavior
- Added V1 features and end-to-end workflow documentation
- Verified all Python files with syntax and whitespace checks
- Validated the final V1 demo flow
- Completed Stage 4 and RoleRadar V1

## Tech Stack

- Python
- FastAPI
- Uvicorn
- Streamlit
- SQLite
- SQLAlchemy

Planned later:

- PostgreSQL
- Plotly
- LLM API integration

## Project Structure

```txt
roleradar/
├── backend/
│   ├── app/
│   │   ├── analyzer.py
│   │   ├── database.py
│   │   ├── main.py
│   │   ├── models.py
│   │   └── parser.py
│   ├── requirements.txt
│   └── roleradar.db        # Local SQLite database, gitignored
├── frontend/
│   ├── requirements.txt
│   └── streamlit_app.py
├── .gitignore
└── README.md
```

## API Overview

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/` | Confirm that the backend is running |
| GET | `/health` | Return the backend health status |
| POST | `/parse-jd` | Extract known skills from a job description |
| POST | `/analyze-job` | Calculate detected, matched, and missing skills and FitScore |
| POST | `/applications` | Save an application record |
| GET | `/applications` | Return all saved application records |
| PATCH | `/applications/{application_id}` | Update an application status |

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

## Demo Flow

Use the following example:

```txt
Job description:
We are looking for a backend engineer with Python, SQL, AWS, and Docker experience.

User skills:
Python, SQL
```

Expected analysis:

- FitScore: 50%
- Matched Skills: Python, SQL
- Missing Skills: AWS, Docker

Demo steps:

1. Start the FastAPI backend and Streamlit frontend
2. Confirm the backend health endpoint
3. Analyze the example job description
4. Save the result as an application record
5. Show the updated Dashboard metrics and charts
6. Show the Top Missing Skills chart
7. Open the saved record in Application History
8. Update its application status
9. Confirm that the Dashboard and status chart refresh
10. Restart the application and confirm that the record persists

## V1 Status

RoleRadar V1 implementation and final regression testing are complete.

- Stage 3: Application Tracker completed
- Stage 4: Dashboard and V1 Completion implementation completed
- The FastAPI backend and Streamlit frontend are connected locally
- Application records persist through SQLite and SQLAlchemy
- Dashboard metrics, charts, Top Missing Skills, and Application History use real saved records
- Empty application and missing-skills data are handled safely
- The local SQLite database file is excluded from Git
- RoleRadar V1 is complete and ready for local demonstration

## V1 Limitations

- Skill extraction uses a fixed rule-based known-skills list
- The application runs locally and does not include authentication or multiple users
- Application data is stored in a local SQLite database
- Duplicate-save protection only applies to the current Streamlit session
- Application records support create, read, and status update operations, but not deletion
- Application status is the only editable field after a record is saved
- Regression testing is currently manual rather than automated
