"""
Password gate for the CS Streamlit apps.

Mirrors Jess O'Mahoney's pattern from jessicaomahoney/performance-tracker:
the app blocks on a password prompt until the right value is entered. The
password lives in st.secrets['APP_PASSWORD'] on Streamlit Cloud; locally
it defaults to 'changeme' so dev never gets locked out.

Call require_login() once, right after st.set_page_config(...), in every
entry-point script.
"""
import streamlit as st

_DEFAULT_LOCAL_PASSWORD = 'changeme'


def require_login() -> None:
    """Block the app until the right password is entered.

    On Streamlit Cloud: reads APP_PASSWORD from st.secrets.
    Locally (no secrets): falls back to 'changeme'. Override with a
    .streamlit/secrets.toml file at the repo root if you want a local password.
    """
    try:
        expected = st.secrets.get('APP_PASSWORD', _DEFAULT_LOCAL_PASSWORD)
    except Exception:
        expected = _DEFAULT_LOCAL_PASSWORD

    if st.session_state.get('cs_authed'):
        return

    # Branded login screen
    st.markdown(
        """
        <style>
        [data-testid="stAppViewContainer"] > .main > div.block-container {
          max-width: 420px;
          padding-top: 6rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("### 🔒 Juno CS — please sign in")
    st.caption("Ask Jo for the password if you don't have it.")
    pw = st.text_input("Password", type="password", key='_login_pw',
                        label_visibility='collapsed', placeholder='Password')
    if st.button('Sign in', type='primary'):
        if pw == expected:
            st.session_state['cs_authed'] = True
            st.rerun()
        else:
            st.error("That password isn't right.")
    st.stop()
