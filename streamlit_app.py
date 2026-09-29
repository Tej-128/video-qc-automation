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

me_response = requests.get(f"{FRAMEIO_BASE_URL}/me", headers=headers, timeout=45)
accounts_response = requests.get(f"{FRAMEIO_BASE_URL}/accounts", headers=headers, timeout=45)

if not (me_response.ok and accounts_response.ok):
    st.error(
        "Adobe login succeeded, but the Frame.io API connectivity check failed. "
        f"/me={me_response.status_code}, /accounts={accounts_response.status_code}"
    )
    st.stop()

me_payload = me_response.json()
accounts_payload = accounts_response.json()
me_data = me_payload.get("data", me_payload) if isinstance(me_payload, dict) else {}

if isinstance(accounts_payload, dict):
    account_rows = accounts_payload.get("data") or accounts_payload.get("accounts") or []
else:
    account_rows = accounts_payload if isinstance(accounts_payload, list) else []

st.success("Frame.io connection successful.")
st.write(f"Authenticated user: **{me_data.get('name', 'Available')}**")
st.write(f"Accessible Frame.io accounts: **{len(account_rows)}**")

st.subheader("Direct review-link test")
st.write(
    "For this milestone, paste one Frame.io Review Link from the Monday board. "
    "We will use the link itself as the source of truth; no project/workspace browsing is required."
)

review_link = st.text_input(
    "Frame.io Review Link",
    placeholder="https://f.io/...",
)

if st.button("Resolve review link", type="primary", disabled=not review_link):
    from urllib.parse import urlparse
    import re

    parsed = urlparse(review_link.strip())
    allowed_hosts = {"f.io", "www.f.io", "frame.io", "www.frame.io", "next.frame.io"}

    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        st.error("Please enter an HTTPS Frame.io / f.io review link.")
    else:
        try:
            response = requests.get(
                review_link.strip(),
                allow_redirects=True,
                timeout=30,
            )
            final_url = response.url
            final_parsed = urlparse(final_url)

            st.write(f"Link resolution HTTP status: **{response.status_code}**")
            st.write(f"Resolved host: **{final_parsed.hostname or 'Unknown'}**")
            st.write(f"Resolved path: **{final_parsed.path or '/'}**")

            uuid_candidates = re.findall(
                r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
                r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b",
                final_url,
            )

            if uuid_candidates:
                st.success(
                    f"Resolved link contains {len(set(uuid_candidates))} Frame.io-style UUID candidate(s)."
                )
            else:
                st.info(
                    "The short link resolved, but no asset UUID was visible in the URL. "
                    "That is still useful: the next step will use the resolved page/share metadata "
                    "to identify the linked asset."
                )
        except requests.RequestException as exc:
            st.error(f"Could not resolve the review link: {exc}")

st.caption(
    "This is a temporary diagnostic step. Final production flow will read these links "
    "directly from Monday for the selected month."
)

if st.button("Disconnect Frame.io"):
    st.session_state.frameio_access_token = None
    st.session_state.pop("oauth_state", None)
    st.rerun()
