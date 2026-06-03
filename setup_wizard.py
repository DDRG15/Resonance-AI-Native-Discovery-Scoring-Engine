"""
setup_wizard.py — First-run configuration wizard for Project GEMA.

Intercepts the Streamlit boot cycle when no LLM key is configured.
Writes validated keys to .env via python-dotenv set_key(), reloads the
config module, then triggers st.rerun() to boot the main application.

Integration in main.py (after st.set_page_config(), before sidebar):
    from setup_wizard import needs_setup, render_setup_wizard
    if needs_setup():
        render_setup_wizard()
        st.stop()
"""

import importlib
import time
from pathlib import Path

import streamlit as st
from dotenv import set_key

import config

_ENV_PATH = Path(__file__).parent / ".env"


def needs_setup() -> bool:
    """Return True if no LLM API key is configured in the current environment."""
    importlib.reload(config)
    return not any([
        config.GROQ_API_KEY.strip(),
        config.GEMINI_API_KEY.strip(),
        config.OPENROUTER_API_KEY.strip(),
        config.COHERE_API_KEY.strip(),
    ])


def _test_groq_key(api_key: str) -> tuple[bool, str]:
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
        client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=3,
        )
        return True, "Key verified."
    except Exception as exc:
        return False, str(exc)


def _test_gemini_key(api_key: str) -> tuple[bool, str]:
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash-lite")
        model.generate_content("ping", generation_config={"max_output_tokens": 3})
        return True, "Key verified."
    except Exception as exc:
        return False, str(exc)


def _save_keys(
    groq_key: str,
    gemini_key: str,
    openrouter_key: str,
    cohere_key: str,
    discord_url: str,
) -> None:
    """Write non-empty values to .env and reload config."""
    env = str(_ENV_PATH)
    if groq_key:
        set_key(env, "GROQ_API_KEY", groq_key)
    if gemini_key:
        set_key(env, "GEMINI_API_KEY", gemini_key)
    if openrouter_key:
        set_key(env, "OPENROUTER_API_KEY", openrouter_key)
    if cohere_key:
        set_key(env, "COHERE_API_KEY", cohere_key)
    if discord_url:
        set_key(env, "DISCORD_WEBHOOK_URL", discord_url)
    importlib.reload(config)


def render_setup_wizard() -> None:
    """Render the full-page first-run wizard. Call st.stop() immediately after in main.py."""

    st.markdown(
        """
        <style>
        .wizard-header { font-size: 2.4rem; font-weight: 800; margin-bottom: 0.2rem; }
        .wizard-sub { font-size: 1.1rem; color: #888; margin-bottom: 2rem; }
        .step-label { font-size: 1rem; font-weight: 600; margin-top: 1.5rem; margin-bottom: 0.3rem; }
        .step-hint { font-size: 0.85rem; color: #999; margin-bottom: 0.6rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    _, col, _ = st.columns([1, 2, 1])

    with col:
        st.markdown('<p class="wizard-header">💎 Welcome to GEMA</p>', unsafe_allow_html=True)
        st.markdown(
            '<p class="wizard-sub">First-time setup — takes about 2 minutes.</p>',
            unsafe_allow_html=True,
        )

        st.divider()

        # ── Step 1 — LLM Key ─────────────────────────────────────────────────
        st.markdown('<p class="step-label">Step 1 — LLM API Key (required)</p>', unsafe_allow_html=True)
        st.markdown(
            '<p class="step-hint">'
            'GEMA needs at least one LLM key to analyze job postings. '
            '<a href="https://console.groq.com" target="_blank">Groq</a> is free and the recommended choice. '
            '<a href="https://aistudio.google.com" target="_blank">Gemini</a> works as a free backup.'
            '</p>',
            unsafe_allow_html=True,
        )

        groq_key = st.text_input(
            "Groq API Key (recommended — free)",
            type="password",
            placeholder="gsk_...",
            key="wiz_groq",
        )
        gemini_key = st.text_input(
            "Gemini API Key (optional backup)",
            type="password",
            placeholder="AIza...",
            key="wiz_gemini",
        )

        with st.expander("More LLM providers (optional)"):
            openrouter_key = st.text_input(
                "OpenRouter API Key",
                type="password",
                placeholder="sk-or-...",
                key="wiz_openrouter",
            )
            cohere_key = st.text_input(
                "Cohere API Key",
                type="password",
                placeholder="...",
                key="wiz_cohere",
            )

        st.divider()

        # ── Step 2 — Discord (optional) ──────────────────────────────────────
        st.markdown('<p class="step-label">Step 2 — Discord Notifications (optional)</p>', unsafe_allow_html=True)
        st.markdown(
            '<p class="step-hint">'
            'GEMA will message you when it finds Tier 1 jobs. '
            'Create a webhook: Discord server → Settings → Integrations → Webhooks.'
            '</p>',
            unsafe_allow_html=True,
        )
        discord_url = st.text_input(
            "Discord Webhook URL",
            placeholder="https://discord.com/api/webhooks/...",
            key="wiz_discord",
        )

        st.divider()

        # ── Save button ───────────────────────────────────────────────────────
        save_clicked = st.button(
            "Save and Launch GEMA →",
            type="primary",
            use_container_width=True,
            key="wiz_save",
        )

        if save_clicked:
            if not any([groq_key, gemini_key, openrouter_key, cohere_key]):
                st.error("Enter at least one LLM API key to continue. Groq is free at console.groq.com")
                return

            # Validate the first provided key (warn but never block)
            if groq_key:
                with st.spinner("Verifying Groq key..."):
                    ok, msg = _test_groq_key(groq_key)
                if ok:
                    st.success("Groq key verified.")
                else:
                    st.warning(
                        f"Could not verify Groq key ({msg}). "
                        "Saving anyway — GEMA will report errors at runtime if the key is invalid."
                    )
            elif gemini_key:
                with st.spinner("Verifying Gemini key..."):
                    ok, msg = _test_gemini_key(gemini_key)
                if ok:
                    st.success("Gemini key verified.")
                else:
                    st.warning(f"Could not verify Gemini key ({msg}). Saving anyway.")

            _save_keys(
                groq_key=groq_key,
                gemini_key=gemini_key,
                openrouter_key=openrouter_key,
                cohere_key=cohere_key,
                discord_url=discord_url,
            )

            st.success("Configuration saved! Launching GEMA...")
            time.sleep(1.2)
            st.rerun()
