import streamlit as st

st.title("RoleRadar")
st.write("Job intelligence & Skill-Gap Analysis Platform")

jd_text = st.text_area("Paste the job description here:")
st.write("Characters:", len(jd_text))