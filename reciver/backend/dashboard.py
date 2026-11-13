"""Streamlit dashboard.   streamlit run dashboard.py      (env DATABASE_URL, same DB as the backend)"""
import time
import pandas as pd
import streamlit as st

from pulse_rx.db import Store
from pulse_rx.service import Pipeline

st.set_page_config(page_title="PULSE-Box Cold-Chain", layout="wide")


@st.cache_resource
def get_pipeline():
    pl = Pipeline(Store(), train_if_missing=True)
    pl.restore()
    return pl


pl = get_pipeline()
st.title("PULSE-Box - shelf-life monitor")

with st.sidebar:
    st.header("Controls")
    live = st.checkbox("Auto-refresh (10 s)", value=False)
    if st.button("Load simulated fleet (demo)"):
        from pulse_rx.simulator import SimTag, cold_chain, with_excursion, run
        tags = [SimTag(0xB0A00001 + i, cold_chain(3 + i * 0.7)) for i in range(6)]
        tags[2].profile = with_excursion(cold_chain(), 20 * 3600, 8 * 3600, 42)
        tags[4].die_at = 30 * 3600
        run(pl, tags, 4 * 86400)
        st.success("Simulated 4 days of traffic from 6 tags")
    product = st.selectbox("Default product", ["dairy", "frozen", "vaccine", "packaged"])
    pl.default_product = product

tags = pl.store.tags()
rank = pl.dispatch_ranking()
alerts = pl.store.alerts(300)

c = st.columns(5)
c[0].metric("Tags", len(tags))
c[1].metric("CRITICAL", sum(t["stage"] == 2 for t in tags))
c[2].metric("AGING", sum(t["stage"] == 1 for t in tags))
c[3].metric("LOST", sum(bool(t["lost"]) for t in tags))
c[4].metric("Missed packets", sum(t["missed"] for t in tags))

tab1, tab2, tab3 = st.tabs(["Dispatch priority (FEFO)", "Alerts", "Temperature history"])
with tab1:
    if rank:
        df = pd.DataFrame(rank)[["rank", "box_id", "action", "stage", "remaining_days", "product"]]
        colour = {"CRITICAL": "background-color:#f8d7da", "AGING": "background-color:#fff3cd",
                  "FRESH": "background-color:#d4edda"}
        st.dataframe(df.style.map(lambda v: colour.get(v, ""), subset=["stage"]), use_container_width=True,
                     hide_index=True)
        st.caption("Quarantine/locate rows first; then closest-to-expiry first (predicted remaining shelf life).")
    else:
        st.info("No tags yet. Start the backend/bridge or use 'Load simulated fleet'.")
with tab2:
    if alerts:
        a = pd.DataFrame(alerts)
        a["time"] = pd.to_datetime(a["ts"], unit="s")
        a["box_id"] = a["box_id"].map(lambda v: f"{v:08X}")
        sev = st.multiselect("Severity", ["critical", "warning", "info"], default=["critical", "warning"])
        st.dataframe(a[a.severity.isin(sev)][["time", "box_id", "kind", "severity", "detail"]],
                     use_container_width=True, hide_index=True)
with tab3:
    if tags:
        sel = st.selectbox("Box", [t["box_id"] for t in tags], format_func=lambda v: f"{v:08X}")
        s = pd.DataFrame(pl.store.series(sel, 800))
        if not s.empty:
            s["time"] = pd.to_datetime(s["ts"], unit="s")
            st.line_chart(s.set_index("time")["temp_c"])
            st.caption("Per-unit temperature series + latched stage = evidence for spoilage disputes.")

if live:
    time.sleep(10)
    st.rerun()
