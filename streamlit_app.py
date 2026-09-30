from __future__ import annotations

import base64
import secrets
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
import streamlit as st

from src.pipeline import run_pipeline
from src.version import BUILD_LABEL, BUILD_VERSION

ADOBE_AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"
ADOBE_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
FRAMEIO_BASE_URL = "https://api.frame.io/v4"
REDIRECT_URI = "https://video-qc-automation-abmjs82sxu76zjmt26beda.streamlit.app"
SCOPES = "offline_access,openid,email,profile,additional_info.roles"

st.set_page_config(
    page_title="Video QC Automation",
    page_icon="🎬",
    layout="wide",
)

st.title("Video QC Automation")
st.caption("Monday.com → Frame.io → OpenAI → two monthly QC reports")
st.info(f"Build: **{BUILD_VERSION}** — {BUILD_LABEL}")


@st.cache_resource
def frameio_token_store() -> dict[str, str | None]:
    # Shared only inside the active Streamlit worker. This removes repeat Adobe
    # sign-ins across browser refreshes/new sessions while the app stays awake.
    return {"access_token": None, "refresh_token": None}


token_store = frameio_token_store()

# Never carry a completed report from an older deployed build into a newer one.
if st.session_state.get("_app_build_version") != BUILD_VERSION:
    st.session_state.pop("qc_result", None)
    st.session_state.pop("qc_run_key", None)
    st.session_state["_app_build_version"] = BUILD_VERSION


def previous_month(today: date) -> tuple[int, int]:
    first = today.replace(day=1)
    previous = first - timedelta(days=1)
    return previous.year, previous.month


def secret(name: str) -> str:
    return str(st.secrets.get(name, "") or "").strip()


client_id = secret("FRAMEIO_CLIENT_ID")
client_secret = secret("FRAMEIO_CLIENT_SECRET")
monday_token = secret("MONDAY_API_TOKEN")
openai_key = secret("OPENAI_API_KEY")
openai_model = secret("OPENAI_MODEL") or "gpt-5.6-terra"

missing = [
    name
    for name, value in (
        ("FRAMEIO_CLIENT_ID", client_id),
        ("FRAMEIO_CLIENT_SECRET", client_secret),
        ("MONDAY_API_TOKEN", monday_token),
        ("OPENAI_API_KEY", openai_key),
    )
    if not value
]

if missing:
    st.error(
        "Missing Streamlit secret(s): " + ", ".join(missing) + ". "
        "Add them under Manage app → Settings → Secrets."
    )
    st.stop()

if "frameio_access_token" not in st.session_state:
    st.session_state.frameio_access_token = token_store.get("access_token")
if "frameio_refresh_token" not in st.session_state:
    st.session_state.frameio_refresh_token = token_store.get("refresh_token")

query_params = st.query_params
code = query_params.get("code")
returned_state = query_params.get("state")

if code and not st.session_state.frameio_access_token:
    expected_state = st.session_state.get("oauth_state")
    if expected_state and returned_state != expected_state:
        st.error("Adobe OAuth state validation failed. Please connect Frame.io again.")
        st.stop()

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    token_response = requests.post(
        ADOBE_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=45,
    )

    if not token_response.ok:
        st.error(
            f"Adobe token exchange failed with HTTP {token_response.status_code}. "
            "Reconnect Frame.io and try again."
        )
        st.stop()

    token_payload = token_response.json()
    st.session_state.frameio_access_token = token_payload.get("access_token")
    st.session_state.frameio_refresh_token = token_payload.get("refresh_token")
    token_store["access_token"] = st.session_state.frameio_access_token
    token_store["refresh_token"] = st.session_state.frameio_refresh_token
    st.query_params.clear()
    st.rerun()

if not st.session_state.frameio_access_token:
    if "oauth_state" not in st.session_state:
        st.session_state.oauth_state = secrets.token_urlsafe(24)

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
        "state": st.session_state.oauth_state,
    }
    auth_url = f"{ADOBE_AUTHORIZE_URL}?{urlencode(params)}"

    st.info("Frame.io authentication is required before a report can be generated.")
    st.link_button("Connect Frame.io", auth_url, type="primary")
    st.caption(
        "Authenticate with Jack's Adobe account, which has access to the production Frame.io content."
    )
    st.stop()

headers = {"Authorization": f"Bearer {st.session_state.frameio_access_token}"}
me_response = requests.get(f"{FRAMEIO_BASE_URL}/me", headers=headers, timeout=45)

if me_response.status_code == 401 and st.session_state.frameio_refresh_token:
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    refresh_response = requests.post(
        ADOBE_TOKEN_URL,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": st.session_state.frameio_refresh_token,
        },
        timeout=45,
    )
    if refresh_response.ok:
        refresh_payload = refresh_response.json()
        st.session_state.frameio_access_token = refresh_payload.get("access_token")
        st.session_state.frameio_refresh_token = (
            refresh_payload.get("refresh_token")
            or st.session_state.frameio_refresh_token
        )
        token_store["access_token"] = st.session_state.frameio_access_token
        token_store["refresh_token"] = st.session_state.frameio_refresh_token
        st.rerun()

if me_response.status_code == 401:
    st.session_state.frameio_access_token = None
    st.session_state.frameio_refresh_token = None
    token_store["access_token"] = None
    token_store["refresh_token"] = None
    st.session_state.pop("oauth_state", None)
    st.warning(
        "The Adobe session expired. A fresh Adobe sign-in is required for this new Streamlit session."
    )
    st.rerun()

if not me_response.ok:
    st.error(f"Frame.io authentication check failed with HTTP {me_response.status_code}.")
    st.stop()

me_payload = me_response.json()
me_data = me_payload.get("data", me_payload) if isinstance(me_payload, dict) else {}
authenticated_name = me_data.get("name") or "Authenticated user"

top_left, top_right = st.columns([4, 1])
with top_left:
    st.success(f"Frame.io connected as {authenticated_name}.")
with top_right:
    if st.button("Disconnect Frame.io"):
        st.session_state.frameio_access_token = None
        st.session_state.frameio_refresh_token = None
        token_store["access_token"] = None
        token_store["refresh_token"] = None
        st.session_state.pop("oauth_state", None)
        for key in ("qc_result", "qc_run_key"):
            st.session_state.pop(key, None)
        st.rerun()

default_year, default_month = previous_month(date.today())

st.subheader("Generate monthly QC reports")
col1, col2, col3 = st.columns([1, 1, 2])
with col1:
    report_year = st.number_input(
        "Year",
        min_value=2020,
        max_value=2100,
        value=default_year,
        step=1,
    )
with col2:
    report_month = st.selectbox(
        "Month",
        list(range(1, 13)),
        index=default_month - 1,
        format_func=lambda value: date(2000, value, 1).strftime("%B"),
    )
with col3:
    st.text_input(
        "OpenAI model",
        value=openai_model,
        disabled=True,
        help="Configured in Streamlit Secrets. Default is gpt-5.6-terra.",
    )

st.caption(
    "The run reads Monday and Frame.io only. It does not write back to either system. "
    "Version 1 and the final/latest Frame.io version are excluded."
)

run_clicked = st.button("Generate QC Reports", type="primary", use_container_width=True)

if run_clicked:
    progress = st.progress(0)
    status = st.empty()

    def update_progress(message: str, fraction: float) -> None:
        progress.progress(int(fraction * 100))
        status.write(message)

    try:
        result = run_pipeline(
            monday_token=monday_token,
            frameio_access_token=st.session_state.frameio_access_token,
            openai_api_key=openai_key,
            year=int(report_year),
            month=int(report_month),
            openai_model=openai_model,
            progress_callback=update_progress,
        )
    except Exception as exc:
        progress.empty()
        status.empty()
        st.error(f"QC report generation failed: {type(exc).__name__}: {exc}")
    else:
        progress.progress(100)
        status.success("QC reports generated.")
        st.session_state.qc_result = result
        st.session_state.qc_run_key = f"{int(report_year):04d}-{int(report_month):02d}"

result = st.session_state.get("qc_result")
if result:
    metrics = result["metrics"]
    st.subheader(f"Run summary — {result['year']:04d}-{result['month']:02d}")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Monthly projects", metrics["monthly_projects"])
    m2.metric("Frame.io resolved", metrics["frameio_resolved"])
    m3.metric("Comments analyzed", metrics["comments_analyzed"])
    m4.metric("Errors counted", metrics["total_error_count"])
    m5.metric("Needs review", metrics["needs_review"])

    if metrics.get("classification_failures", 0):
        st.warning(
            f"{metrics['classification_failures']} comment(s) could not be classified by OpenAI "
            "after retries. The Excel files were still generated, and those comments are listed "
            "in the Needs Review sheet instead of blocking the whole run."
        )

    prefix = f"{result['year']:04d}_{result['month']:02d}"
    d1, d2 = st.columns(2)
    with d1:
        st.download_button(
            "Download Scripting QC Report",
            data=result["scripting_report"],
            file_name=f"{prefix}_Scripting_QC.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            "Download Video Editing QC Report",
            data=result["video_report"],
            file_name=f"{prefix}_Video_Editing_QC.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            use_container_width=True,
        )

    if result["unresolved"]:
        st.warning(
            f"{len(result['unresolved'])} project(s) could not be uniquely resolved in Frame.io. "
            "They are also listed in the Needs Review sheet and were not guessed."
        )
        st.dataframe(result["unresolved"], use_container_width=True, hide_index=True)
    else:
        st.success("All monthly Monday projects were resolved to Frame.io.")

    st.caption(
        "The Error Detail sheet includes editable manual override columns. "
        "Raw Frame.io comments are retained in a hidden audit sheet inside each workbook."
    )
