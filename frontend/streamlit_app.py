import streamlit as st

import requests

API_URL = "http://127.0.0.1:8000/parse-jd"

st.title("RoleRadar")
st.write("Job intelligence & Skill-Gap Analysis Platform")

jd_text = st.text_area("Paste the job description here:")
st.write("Characters:", len(jd_text))

# Core logic to send the job description to the backend API and display the detected skills

if st.button("Analyze JD"):  #create a button to trigger the analysis
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

        st.write("Detected Skills:")
    
        for skill in result["skills"]:
            st.write("-",skill)