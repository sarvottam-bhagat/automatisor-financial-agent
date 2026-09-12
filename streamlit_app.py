from __future__ import annotations

from typing import Any

import streamlit as st

from automatisor.contracts import Settings
from automatisor.personas import PERSONA_CONFIGS
from automatisor.ui_client import APIClientError, FinancialAgentClient
from automatisor.ui_formatting import format_answer


PERSONA_LABELS = {
    "mutual_fund_analyst": "Mutual fund analyst",
    "equity_analyst": "Equity analyst",
    "pe_analyst": "PE analyst",
}
SECTOR_LABELS = {"tech": "Tech", "retail": "Retail", "logistics": "Logistics"}
SUGGESTIONS = {
    "tech": {
        "Sector allocation": "Is this sector a good place to be putting money to work right now?",
        "Take-private screen": "If I had to pick one company here to take private, which would it be and why?",
    },
    "retail": {
        "Core versus avoid": "Which companies look like long-term core holdings and which should I avoid?",
        "Margin leaders": "Which retailers have the strongest and weakest margin profiles?",
    },
    "logistics": {
        "Buyout candidates": "Which companies look like attractive buyout targets based on the available data?",
        "Cash conversion": "Compare free-cash-flow conversion and leverage capacity across the sector.",
    },
}
TOOL_LABELS = {
    "list_sector_companies": "Loaded the covered company universe",
    "resolve_company": "Matched the requested company",
    "get_company_snapshot": "Read the company financial snapshot",
    "compare_company_metrics": "Compared company metrics",
    "get_sector_benchmark": "Loaded the sector benchmark",
    "search_company_evidence": "Searched evidence stored in the database",
}
ANALYSIS_TIMEOUT_SECONDS = 180.0
GROUNDING_FALLBACK_MARKER = "did not pass grounding validation"


st.set_page_config(
    page_title="Automatisor financial agent",
    page_icon=":material/query_stats:",
    layout="centered",
)


def clear_conversation() -> None:
    st.session_state["messages"] = []


@st.cache_resource
def get_api_client(api_url: str, timeout_seconds: float) -> FinancialAgentClient:
    return FinancialAgentClient(api_url, timeout=timeout_seconds)


def render_assistant(payload: dict[str, Any]) -> None:
    sections, facts = format_answer(payload["answer"], payload.get("persona", ""))
    headcount = next(
        (fact for fact in facts if fact.Metric.casefold() == "headcount"),
        None,
    )
    if headcount:
        st.metric(
            "Latest reported headcount",
            headcount.Value,
            help=f"Source: {headcount.Source}",
        )
        st.caption(f"{headcount.Company} · Reported {headcount.Period}")

    for index, section in enumerate(sections):
        target = st.container(border=True) if index == 0 else st.container()
        with target:
            st.subheader(section.title)
            st.markdown(section.body)

    if facts:
        with st.expander(
            f"Verified data ({len(facts)})",
            icon=":material/table_view:",
        ):
            st.dataframe(
                [fact.__dict__ for fact in facts],
                hide_index=True,
                width="stretch",
                height=min(420, 38 + 35 * len(facts)),
            )

    metadata = []
    if payload.get("confidence"):
        metadata.append(f"Confidence: {payload['confidence']}")
    if payload.get("data_as_of"):
        metadata.append(f"Data as of: {payload['data_as_of']}")
    if payload.get("companies_referenced"):
        metadata.append(f"Companies: {', '.join(payload['companies_referenced'])}")
    if metadata:
        st.caption(" · ".join(metadata))

    limitations = list(dict.fromkeys(payload.get("limitations", [])))
    synthetic_limitations = [item for item in limitations if "synthetic" in item.casefold()]
    if synthetic_limitations:
        st.warning(
            "Sample data is active. These results are evaluation fixtures, not reported company or live-market data.",
            icon=":material/science:",
        )
    other_limitations = [item for item in limitations if item not in synthetic_limitations]
    if other_limitations:
        with st.expander(
            f"Limitations ({len(other_limitations)})",
            icon=":material/warning:",
        ):
            for limitation in other_limitations:
                st.markdown(f"- {limitation}")

    if payload.get("sources"):
        with st.expander(
            f"Sources ({len(payload['sources'])})",
            icon=":material/source:",
        ):
            with st.container(horizontal=True):
                for source in payload["sources"]:
                    st.link_button(
                        source["title"], source["url"], icon=":material/open_in_new:"
                    )
    with st.expander("Reviewer details", icon=":material/code:"):
        st.caption(f"Request ID: {payload.get('request_id', 'not available')}")
        st.write("Tools used:", ", ".join(payload.get("tools_used", [])) or "None")


def is_grounding_fallback(limitations: list[str]) -> bool:
    return any(GROUNDING_FALLBACK_MARKER in item.casefold() for item in limitations)


def render_grounding_step(tools_used: list[str], *, failed: bool = False) -> None:
    label = "Grounding validation failed safely" if failed else "Grounding checks passed"
    state = "error" if failed else "complete"
    with st.status(label, state=state, type="step"):
        if tools_used:
            for tool in tools_used:
                st.markdown(
                    f":material/check_circle: {TOOL_LABELS.get(tool, tool.replace('_', ' ').capitalize())}"
                )
        else:
            st.caption("No database tool was reported.")


st.session_state.setdefault("messages", [])
settings = Settings()

with st.sidebar:
    st.header("Research settings", icon=":material/tune:")
    persona = st.selectbox(
        "Analytical persona",
        options=list(PERSONA_LABELS),
        format_func=PERSONA_LABELS.get,
        key="persona_selector",
        on_change=clear_conversation,
    )
    sector = st.selectbox(
        "Sector",
        options=list(SECTOR_LABELS),
        format_func=SECTOR_LABELS.get,
        key="sector_selector",
        on_change=clear_conversation,
    )
    st.caption(PERSONA_CONFIGS[persona].voice)
    st.caption("Demo research only — not fiduciary advice or full underwriting.")

st.title("Financial research agent", icon=":material/query_stats:")
st.caption(
    f"{PERSONA_LABELS[persona]} · {SECTOR_LABELS[sector]} · answers grounded in the project database through MCP"
)

for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant" and message.get("response"):
            render_assistant(message["response"])
        else:
            st.markdown(message["content"])

suggested_prompt = None
if not st.session_state["messages"]:
    labels = list(SUGGESTIONS[sector])
    selected = st.pills("Try a question", labels, label_visibility="collapsed")
    if selected:
        suggested_prompt = SUGGESTIONS[sector][selected]

typed_prompt = st.chat_input("Ask about the covered companies", submit_mode="disable")
prompt = suggested_prompt or typed_prompt

if prompt:
    prior_history = [
        {"role": item["role"], "content": item["content"]}
        for item in st.session_state["messages"][-8:]
    ]
    st.session_state["messages"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        try:
            with st.status(
                "Request sent to analysis agent",
                state="complete",
                type="step",
            ):
                st.caption(f"{PERSONA_LABELS[persona]} · {SECTOR_LABELS[sector]}")

            query_status = st.status(
                ":shimmer[Agent is using MCP tools to query the database]",
                type="step",
                expanded=True,
            )
            try:
                result = get_api_client(settings.api_url, ANALYSIS_TIMEOUT_SECONDS).analyze(
                    prompt, persona, sector, prior_history
                )
            except APIClientError:
                query_status.update(label="MCP or analysis request failed", state="error")
                raise
            query_status.update(
                label="MCP and database query complete",
                state="complete",
                expanded=False,
            )
            grounding_failed = is_grounding_fallback(result.limitations)
            render_grounding_step(result.tools_used, failed=grounding_failed)
            st.status(
                "Safe response ready" if grounding_failed else "Answer ready",
                state="complete",
                type="step",
            )
            payload = result.model_dump(mode="json")
            render_assistant(payload)
            st.session_state["messages"].append(
                {"role": "assistant", "content": result.answer, "response": payload}
            )
        except APIClientError as error:
            st.error(str(error), icon=":material/error:")
            st.session_state["messages"].append(
                {"role": "assistant", "content": str(error), "response": None}
            )
