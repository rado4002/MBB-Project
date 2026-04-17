"""Dashboard auth utility — role check + API headers."""
import streamlit as st
import httpx


def get_api_headers() -> dict:
    """Return auth headers for FastAPI calls."""
    token = st.session_state.get("token", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def get_api_url() -> str:
    return st.session_state.get("api_url", "http://api:8000/api/v1")


def api_get(path: str, params: dict | None = None):
    """GET request to FastAPI backend."""
    try:
        resp = httpx.get(f"{get_api_url()}{path}", headers=get_api_headers(), params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def api_post(path: str, data: dict | None = None):
    """POST request to FastAPI backend."""
    try:
        resp = httpx.post(f"{get_api_url()}{path}", headers=get_api_headers(), json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None


def api_put(path: str, data: dict | None = None):
    """PUT request to FastAPI backend."""
    try:
        resp = httpx.put(f"{get_api_url()}{path}", headers=get_api_headers(), json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        st.error(f"API error: {e}")
        return None
