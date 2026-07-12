import streamlit as st
import requests


# ----------------------------------------
# APP configuration
# ----------------------------------------

API_URL = "http://127.0.0.1:8000/analyze-job"


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
            API_URL,
            json={"text": jd_text,
                  "user_skills":user_skills}
        )
        result = response.json()
        jd_skills = result["skills"]
        matched_skills = result["matched_skills"]
        missing_skills = result["missing_skills"]
        fit_score = result["fit_score"]

        
        # ----------------------------------------
        # display the analysis results
        # ----------------------------------------
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
            

        