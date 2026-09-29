import streamlit as st

st.set_page_config(
    page_title="Video QC Automation",
    page_icon="🎬",
    layout="wide",
)

st.title("Video QC Automation")
st.caption("Monthly QC report automation for Monday.com + Frame.io + OpenAI")

st.success("Deployment foundation is live.")

st.markdown(
    """
### Current build stage
- Monday.com connectivity: **validated**
- Monthly project selection: **validated**
- Frame.io OAuth: **setup in progress**
- QC classification: **not connected yet**
- Excel report generation: **not connected yet**

This public deployment currently shows **no production QC data**.
"""
)
