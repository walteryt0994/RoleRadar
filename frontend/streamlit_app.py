import streamlit as st
import requests


# ----------------------------------------
# APP configuration
# ----------------------------------------

API_BASE_URL = "http://127.0.0.1:8000"

ANALYZE_API_URL = f"{API_BASE_URL}/analyze-job"
APPLICATIONS_API_URL = f"{API_BASE_URL}/applications"

# ----------------------------------------
# Session state
# ----------------------------------------

if "analysis_result" not in st.session_state:
    st.session_state.analysis_result = None

if "last_saved_payload" not in st.session_state:
    st.session_state.last_saved_payload = None

# ----------------------------------------
# Page header
# ----------------------------------------

st.title("RoleRadar")
st.write("Job intelligence & Skill-Gap Analysis Platform")

# ----------------------------------------
# Job Description Input
# ----------------------------------------

jd_text = st.text_area("Paste the job description here:")
st.write("Characters:", len(jd_text))


# ----------------------------------------
# User Skills Input
# ----------------------------------------

user_skills_text = st.text_input("Enter your skills, separated by commas:")
user_skills = [skill.strip() for skill in user_skills_text.split(",") if skill.strip()]

display_user_skills =[skill.title() for skill in user_skills]

if user_skills:
    st.write("Your skills:", display_user_skills)

# ----------------------------------------
# Send the JD and user skills to the backend for analysis
# ----------------------------------------

if st.button("Analyze JD"):  # create a button to trigger the analysis
   # method: POST, endpoint: /analyze-job
   # payload: {"text": jd_text, "user_skills": user_skills}
    if not jd_text.strip():
        st.warning("Please paste a job description first.")
    else:
        response = requests.post(
            ANALYZE_API_URL,
            json={"text": jd_text,
                  "user_skills":user_skills}
        )

        result = response.json()
        st.session_state.analysis_result = result

# ----------------------------------------
# display the analysis results
# ----------------------------------------


if st.session_state.analysis_result is not None:
    result = st.session_state.analysis_result

    jd_skills = result["skills"]
    matched_skills = result["matched_skills"]
    missing_skills = result["missing_skills"]
    fit_score = result["fit_score"]


    st.write("Fit Score:",f"{fit_score:.0f}%")

    st.write("Detected Skills:")

    if not jd_skills:
        st.write("No known skills were detected in the job description.")
    else:
        for skill in jd_skills:
            st.write("-", skill)

        st.write("Matched Skills:")

        if matched_skills:
            for skill in matched_skills:
                st.write("-", skill)
        else:
            st.write("You don't have any of the required skills for this job description.")



        st.write("Missing Skills:")

        if missing_skills:
            for skill in missing_skills:
                st.write("-", skill)
        else:
            st.write("Congratulations! You have all the required skills for this job description.")

    st.divider()
    st.subheader("Save Application")

    company = st.text_input("Company")
    job_title = st.text_input("Job title")
    status = st.selectbox(
        "Application status",
        [
            "Interested",
            "Applied",
            "Interviewing",
            "Offer",
            "Rejected",
        ]
    )
    application_payload = {
        "company": company.strip(),
        "job_title": job_title.strip(),
        "status": status,
        "fit_score": fit_score,
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
    }


    if st.button("Save Application"):
        if not company.strip() or not job_title.strip():
            st.warning(
                "Please enter both company and job title."
            )
        elif st.session_state.last_saved_payload == application_payload:
            st.warning(
                "This application has already been saved."
            )
        else:
            save_response = requests.post(
                APPLICATIONS_API_URL,
                json=application_payload,
            )

            if save_response.ok:
                saved_application = save_response.json()
                st.session_state.last_saved_payload = (
                    application_payload.copy()
                )

                st.success(
                    "Application saved successfully. "
                    f"ID: {saved_application['id']}"
                )
            else:
                st.error(
                    "Unable to save application. "
                    f"Status code: {save_response.status_code}"
                )

# ----------------------------
# Application history
# ----------------------------

st.divider()
st.header("Application History")

history_response = requests.get(
    APPLICATIONS_API_URL
)

if history_response.ok:
    applications = history_response.json()

    if applications:
        for application in applications:
            title = (
                f"#{application['id']} | "
                f"{application['company']} - "
                f"{application['job_title']}"
            )

            with st.expander(title):
                st.write("Status:", application["status"])
                st.write(
                    "Fit score:",
                    f"{application['fit_score']:.0f}%",
                )
                st.write(
                    "Matched Skills:",
                    ", ".join(application["matched_skills"]) or "None",
                )
                st.write(
                    "Missing Skills:",
                    ", ".join(application["missing_skills"]) or "None",
                )
                st.write(
                    "Created At:",
                    application["created_at"],
                )
    else:
        st.info("No saved applications yet.")
else:
    st.error(
        "Unable to load application history. "
        f"Status code: {history_response.status_code}"
    )