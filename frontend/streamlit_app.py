import streamlit as st
import requests


# ----------------------------------------
# APP configuration
# ----------------------------------------

API_URL = "http://127.0.0.1:8000/parse-jd"


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
# Core logic to send the job description to the backend API and display the detected skills
# ----------------------------------------

if st.button("Analyze JD"):  # create a button to trigger the analysis
    # send the job description to the backend API for parsing
    # method: POST, endpoint: /parse-jd, payload: {"text": jd_text}
    if not jd_text.strip():
        st.warning("Please paste a job description first.")
    else:
        response = requests.post(
            API_URL,
            json={"text": jd_text}
        )
        result = response.json()
        jd_skills = result["skills"]

        # ----------------------------------------
        # match the skills from the job description with the user's skills
        # ----------------------------------------

        user_skills_lower = [skill.lower() for skill in user_skills]


        matched_skills = [
            skill for skill in jd_skills
            if skill.lower() in user_skills_lower
        ]

        #----------------------------------------
        # Missing skills
        #----------------------------------------

        missing_skills = [
            skill
            for skill in jd_skills
            if skill.lower() not in user_skills_lower
        ]

        # ----------------------------------------
        # display the detected skills
        # ----------------------------------------

        st.write("Detected Skills:")
    
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