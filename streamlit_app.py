from __future__ import annotations

import base64
import secrets
from urllib.parse import urlencode

import requests
import streamlit as st

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
st.caption("Monthly QC report automation for Monday.com + Frame.io + OpenAI")

client_id = st.secrets.get("FRAMEIO_CLIENT_ID", "")
client_secret = st.secrets.get("FRAMEIO_CLIENT_SECRET", "")

if not client_id or not client_secret:
    st.markdown(
        """
### Current build stage
- Monday.com connectivity: **validated**
- Monthly project selection: **validated**
- Frame.io OAuth: **waiting for Streamlit credentials**
- QC classification: **not connected yet**
- Excel report generation: **not connected yet**
"""
    )
    st.info(
        "Add FRAMEIO_CLIENT_ID and FRAMEIO_CLIENT_SECRET in Streamlit Secrets."
    )
    st.stop()

query_params = st.query_params
code = query_params.get("code")
returned_state = query_params.get("state")

if "frameio_access_token" not in st.session_state:
    st.session_state.frameio_access_token = None

if code and not st.session_state.frameio_access_token:
    expected_state = st.session_state.get("oauth_state")
    if expected_state and returned_state != expected_state:
        st.error("OAuth state validation failed. Please start the Frame.io connection again.")
        st.stop()

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    response = requests.post(
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

    if not response.ok:
        st.error(f"Adobe token exchange failed with HTTP {response.status_code}.")
        st.stop()

    token_payload = response.json()
    st.session_state.frameio_access_token = token_payload.get("access_token")
    st.query_params.clear()
    st.rerun()

if not st.session_state.frameio_access_token:
    st.markdown(
        """
### Current build stage
- Monday.com connectivity: **validated**
- Monthly project selection: **validated**
- Frame.io OAuth: **ready to connect**
- QC classification: **not connected yet**
- Excel report generation: **not connected yet**
"""
    )

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

    st.subheader("Frame.io connection")
    st.write("Sign in with the Adobe account that has access to the production Frame.io projects.")
    st.link_button("Connect Frame.io", auth_url, type="primary")
    st.caption("No Frame.io credentials or tokens are written to the public repository.")
    st.stop()

st.markdown(
    """
### Current build stage
- Monday.com connectivity: **validated**
- Monthly project selection: **validated**
- Frame.io OAuth: **connected**
- Frame.io hierarchy discovery: **testing now**
- QC classification: **not connected yet**
- Excel report generation: **not connected yet**
"""
)

headers = {"Authorization": f"Bearer {st.session_state.frameio_access_token}"}


def api_get(path: str, params: dict | None = None) -> requests.Response:
    return requests.get(
        f"{FRAMEIO_BASE_URL}{path}",
        headers=headers,
        params=params,
        timeout=45,
    )


def rows_from_payload(payload):
    if isinstance(payload, dict):
        rows = payload.get("data")
        if isinstance(rows, list):
            return rows
    if isinstance(payload, list):
        return payload
    return []


me_response = api_get("/me")
accounts_response = api_get("/accounts", {"page_size": 100})

if not (me_response.ok and accounts_response.ok):
    st.error(
        "Adobe login succeeded, but the Frame.io API connectivity check failed. "
        f"/me={me_response.status_code}, /accounts={accounts_response.status_code}"
    )
    st.stop()

me_payload = me_response.json()
accounts_payload = accounts_response.json()

me_data = me_payload.get("data", me_payload) if isinstance(me_payload, dict) else {}
account_rows = rows_from_payload(accounts_payload)

st.success("Frame.io connection successful.")
st.write(f"Authenticated user: **{me_data.get('name', 'Available')}**")
st.write(f"Accessible Frame.io accounts: **{len(account_rows)}**")

if not account_rows:
    st.error("No Frame.io accounts were returned for this user.")
    st.stop()

account_labels = {
    (row.get("display_name") or row.get("name") or f"Account {idx + 1}"): row
    for idx, row in enumerate(account_rows)
}
selected_account_label = st.selectbox(
    "Frame.io account",
    list(account_labels.keys()),
)
selected_account = account_labels[selected_account_label]
account_id = selected_account.get("id")

workspaces_response = api_get(
    f"/accounts/{account_id}/workspaces",
    {"page_size": 100, "include_total_count": "true"},
)

if not workspaces_response.ok:
    st.error(
        "Could not list Frame.io workspaces for the selected account. "
        f"HTTP {workspaces_response.status_code}"
    )
    st.stop()

workspace_rows = rows_from_payload(workspaces_response.json())
st.write(f"Accessible workspaces in selected account: **{len(workspace_rows)}**")

if not workspace_rows:
    st.warning("No workspaces are accessible in this account.")
else:
    workspace_labels = {
        (row.get("name") or f"Workspace {idx + 1}"): row
        for idx, row in enumerate(workspace_rows)
    }
    selected_workspace_label = st.selectbox(
        "Workspace",
        list(workspace_labels.keys()),
    )
    selected_workspace = workspace_labels[selected_workspace_label]
    workspace_id = selected_workspace.get("id")

    projects_response = api_get(
        f"/accounts/{account_id}/workspaces/{workspace_id}/projects",
        {"page_size": 100, "include_total_count": "true", "sort": "name_asc"},
    )

    if not projects_response.ok:
        st.error(
            "Could not list projects for the selected workspace. "
            f"HTTP {projects_response.status_code}"
        )
    else:
        project_rows = rows_from_payload(projects_response.json())
        st.write(f"Projects visible in selected workspace: **{len(project_rows)}**")

        if project_rows:
            project_names = [
                row.get("name") or f"Project {idx + 1}"
                for idx, row in enumerate(project_rows)
            ]
            selected_project_name = st.selectbox(
                "Project access check",
                project_names,
            )
            st.success(f"Project access confirmed: {selected_project_name}")
            st.caption(
                "Next milestone: use a real Monday Frame.io review link to resolve the "
                "linked asset, inspect its version stack, and extract comments read-only."
            )
        else:
            st.warning("No projects are visible in this workspace.")

if st.button("Disconnect Frame.io"):
    st.session_state.frameio_access_token = None
    st.session_state.pop("oauth_state", None)
    st.rerun()
