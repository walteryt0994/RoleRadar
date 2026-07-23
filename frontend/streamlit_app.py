import streamlit as st
import requests


# ----------------------------------------
# App configuration
# ----------------------------------------

API_BASE_URL = "http://127.0.0.1:8000"

ANALYZE_API_URL = f"{API_BASE_URL}/analyze-job"
APPLICATIONS_API_URL = f"{API_BASE_URL}/applications"
APPLICATION_STATUSES = [
    "Interested",
    "Applied",
    "Interviewing",
    "Offer",
    "Rejected",
]

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

user_skills_text = st.text_input(
    "Enter your skills, separated by commas:"
)
user_skills = [
    skill.strip()
    for skill in user_skills_text.split(",")
    if skill.strip()
]

display_user_skills = [
    skill.title()
    for skill in user_skills
]

if user_skills:
    st.write("Your skills:", display_user_skills)

# ----------------------------------------
# Send the JD and user skills to the backend for analysis
# ----------------------------------------

if st.button("Analyze JD"):
    # Method: POST, endpoint: /analyze-job
    # Payload: {"text": jd_text, "user_skills": user_skills}
    if not jd_text.strip():
        st.warning("Please paste a job description first.")
    else:
        response = requests.post(
            ANALYZE_API_URL,
            json={
                "text": jd_text,
                "user_skills": user_skills,
            },
        )

        result = response.json()
        st.session_state.analysis_result = result

# ----------------------------------------
# Display the analysis results
# ----------------------------------------

if st.session_state.analysis_result is not None:
    result = st.session_state.analysis_result

    jd_skills = result["skills"]
    matched_skills = result["matched_skills"]
    missing_skills = result["missing_skills"]
    fit_score = result["fit_score"]

    st.write("FitScore:", f"{fit_score:.0f}%")
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
            st.write(
                "You don't have any of the required skills "
                "for this job description."
            )

        st.write("Missing Skills:")

        if missing_skills:
            for skill in missing_skills:
                st.write("-", skill)
        else:
            st.write(
                "Congratulations! You have all the required "
                "skills for this job description."
            )

    st.divider()
    st.subheader("Save Application")

    company = st.text_input("Company")
    job_title = st.text_input("Job title")
    status = st.selectbox(
        "Application status",
        APPLICATION_STATUSES,
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
# Load application data
# ----------------------------

history_response = requests.get(
    APPLICATIONS_API_URL
)

if history_response.ok:
    applications = history_response.json()

    # ----------------------------
    # Dashboard
    # ----------------------------

    st.divider()
    st.header("Dashboard")

    total_applications = len(applications)

    status_counts = {
        status: 0 for status in APPLICATION_STATUSES
    }

    for application_record in applications:
        application_status = application_record["status"]

        if application_status in status_counts:
            status_counts[application_status] += 1

    total_fit_score = sum(
        application_record["fit_score"]
        for application_record in applications
    )

    if total_applications > 0:
        average_fit_score = (
            total_fit_score / total_applications
        )
    else:
        average_fit_score = 0

    summary_columns = st.columns(2)

    summary_columns[0].metric(
        "Total Applications",
        total_applications,
    )
    summary_columns[1].metric(
        "Average FitScore",
        f"{average_fit_score:.1f}%",
    )

    status_columns = st.columns(
        len(APPLICATION_STATUSES)
    )

    for index, status_name in enumerate(
        APPLICATION_STATUSES
    ):
        status_columns[index].metric(
            status_name,
            status_counts[status_name],
        )

    # ---------------------------
    # Dashboard charts
    # ---------------------------

    status_chart_data = {
        "Status": APPLICATION_STATUSES,
        "Applications": [
            status_counts[status_name]
            for status_name in APPLICATION_STATUSES
        ],
    }

    st.subheader("Application Status Distribution")

    st.bar_chart(
        status_chart_data,
        x="Status",
        y="Applications",
        x_label="Application Status",
        y_label="Number of Applications",
        sort=False,
    )

    fit_score_counts = {
        "Below 25%": 0,
        "25% to <50%": 0,
        "50% to <75%": 0,
        "75% to 100%": 0,
    }

    for application_record in applications:
        fit_score = application_record["fit_score"]

        if fit_score < 25:
            fit_score_counts["Below 25%"] += 1
        elif fit_score < 50:
            fit_score_counts["25% to <50%"] += 1
        elif fit_score < 75:
            fit_score_counts["50% to <75%"] += 1
        else:
            fit_score_counts["75% to 100%"] += 1

    fit_score_chart_data = {
        "FitScore Range": list(
            fit_score_counts.keys()
        ),
        "Applications": list(
            fit_score_counts.values()
        ),
    }

    st.subheader("FitScore Distribution")

    st.bar_chart(
        fit_score_chart_data,
        x="FitScore Range",
        y="Applications",
        x_label="FitScore Range",
        y_label="Number of Applications",
        sort=False,
    )

    all_missing_skills = []

    for application_record in applications:
        all_missing_skills.extend(
            application_record["missing_skills"]
        )

    missing_skill_counts = {}

    for missing_skill in all_missing_skills:
        if missing_skill not in missing_skill_counts:
            missing_skill_counts[missing_skill] = 0

        missing_skill_counts[missing_skill] += 1

    sorted_missing_skills = sorted(
        missing_skill_counts.items(),
        key=lambda skill_count: skill_count[1],
        reverse=True,
    )

    top_missing_skills = sorted_missing_skills[:5]

    top_missing_skills_chart_data = {
        "Skill": [
            skill
            for skill, count in top_missing_skills
        ],
        "Missing Count": [
            count
            for skill, count in top_missing_skills
        ],
    }

    st.subheader("Top Missing Skills")

    if top_missing_skills:
        st.bar_chart(
            top_missing_skills_chart_data,
            x="Skill",
            y="Missing Count",
            horizontal=True,
            sort=False,
        )
    else:
        st.info(
            "No missing skills found across saved applications."
        )

    # ---------------------------
    # Application history
    # ---------------------------

    st.divider()
    st.header("Application History")

    if applications:
        for application in applications:
            title = (
                f"#{application['id']} | "
                f"{application['company']} - "
                f"{application['job_title']}"
            )

            with st.expander(title):
                st.write("Status:", application["status"])

                updated_status = st.selectbox(
                    "Update status",
                    APPLICATION_STATUSES,
                    index=APPLICATION_STATUSES.index(
                        application["status"]
                    ),
                    key=f"status_{application['id']}",
                )

                if st.button(
                    "Update status",
                    key=f"update_status_{application['id']}",
                    disabled=updated_status == application["status"],
                ):
                    update_response = requests.patch(
                        f"{APPLICATIONS_API_URL}/{application['id']}",
                        json={"status": updated_status},
                    )

                    if update_response.ok:
                        updated_application = update_response.json()

                        st.success(
                            "Status updated successfully: "
                            f"{updated_application['status']}"
                        )

                        st.rerun()

                    else:
                        st.error(
                            "Unable to update application status. "
                            f"Status code: {update_response.status_code}"
                        )

                st.write(
                    "FitScore:",
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