import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

import pandas as pd
import plotly.express as px
import psycopg2
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

from data.ust_context import UST_PROFILE

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

DB_DSN         = os.getenv("DB_DSN")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_KEY  = os.getenv("AZURE_API_KEY")
AZURE_MODEL    = os.getenv("AZURE_MODEL")

st.set_page_config(
    page_title="UST CI",
    layout="wide",
    page_icon="🔍",
)

st.markdown("""
<style>
    .main { background-color: #ffffff; }
    .stApp { background-color: #f8f9fa; }
    section[data-testid="stSidebar"] {
        background-color: #ffffff;
        border-right: 1px solid #e9ecef;
    }
    .stMultiSelect [data-baseweb="tag"] {
        background-color: #EEEDFE;
        color: #3C3489;
    }
    h1, h2, h3 { color: #1a1a2e; }
    .stMetric {
        background: #f8f9fa;
        border-radius: 8px;
        padding: 12px;
        border: 1px solid #e9ecef;
    }
</style>
""", unsafe_allow_html=True)

COMPETITOR_COLORS = {
    "TCS":       "#378ADD",
    "Wipro":     "#EF9F27",
    "Infosys":   "#639922",
    "Accenture": "#7F77DD",
    "HCL":       "#D85A30",
}

COMPETITOR_BADGE = {
    "TCS":       ("#E6F1FB", "#0C447C"),
    "Wipro":     ("#FAEEDA", "#633806"),
    "Infosys":   ("#EAF3DE", "#27500A"),
    "Accenture": ("#EEEDFE", "#3C3489"),
    "HCL":       ("#FAECE7", "#712B13"),
}
REGULATORY_BADGE = ("#FFF3CD", "#856404")
NEUTRAL_BADGE    = ("#F3F4F6", "#374151")

SOURCE_LABEL = {
    "news":        "News",
    "bse_filing":  "BSE Filing",
    "sec_filing":  "SEC 8-K",
}

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def fetch_signals_cached():
    """Pure data fetch — no st.* calls allowed here"""
    import psycopg2, os
    import pandas as pd
    from dotenv import load_dotenv
    load_dotenv()
    conn = psycopg2.connect(os.getenv("DB_DSN"))
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT competitor, title, url, published_date,
                   created_at, category, significance,
                   ust_relevance, raw_summary, source_type
            FROM signals
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(rows, columns=cols)
        if not df.empty:
            df["created_at"] = pd.to_datetime(
                df["created_at"], errors="coerce", utc=True)
            df["published_date"] = pd.to_datetime(
                df["published_date"], errors="coerce", utc=True)
            df["source_type"] = df["source_type"].fillna("news")
        return df
    finally:
        conn.close()


def load_signals():
    """Wrapper that handles errors with st.* calls"""
    try:
        return fetch_signals_cached()
    except Exception as e:
        st.error(f"Database error: {e}")
        import pandas as pd
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_signal_card(row):
    bg, fg = COMPETITOR_BADGE.get(row["competitor"], NEUTRAL_BADGE)
    left_color = COMPETITOR_COLORS.get(row["competitor"], "#7F77DD")
    source = row["source_type"] or "news"
    is_filing = source in ("bse_filing", "sec_filing")
    src_bg, src_fg = REGULATORY_BADGE if is_filing else NEUTRAL_BADGE
    source_label = SOURCE_LABEL.get(source, source)

    sig = int(row["significance"]) if pd.notna(row["significance"]) else 0
    sig = max(0, min(5, sig))
    dots = "●" * sig + "○" * (5 - sig)

    pub = row["published_date"]
    pub_str = pub.strftime("%Y-%m-%d") if pd.notna(pub) else "—"

    title = (row["title"] or "").replace("<", "&lt;").replace(">", "&gt;")
    ust   = (row["ust_relevance"] or "").replace("<", "&lt;").replace(">", "&gt;")
    url   = row["url"] or "#"

    with st.container():
        st.markdown(
            f"""
            <div style="border:1px solid #E5E7EB;border-left:3px solid {left_color};border-radius:10px;padding:16px 18px;margin-bottom:14px;background:white;">
              <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
                <span style="background:{bg};color:{fg};padding:3px 10px;border-radius:12px;font-size:0.78em;font-weight:600;">
                  {row["competitor"]}
                </span>
                <span style="background:{NEUTRAL_BADGE[0]};color:{NEUTRAL_BADGE[1]};padding:3px 10px;border-radius:12px;font-size:0.78em;">
                  {row["category"] or "—"}
                </span>
                <span style="background:{src_bg};color:{src_fg};padding:3px 10px;border-radius:12px;font-size:0.78em;font-weight:600;">
                  {source_label}
                </span>
                <span style="color:#D85A30;font-size:0.95em;letter-spacing:3px;margin-left:auto;">
                  {dots}
                </span>
              </div>
              <div style="font-size:1.02em;font-weight:600;margin-bottom:10px;line-height:1.35;">
                <a href="{url}" target="_blank" style="color:#111;text-decoration:none;">{title}</a>
              </div>
              <div style="border-left:3px solid #7F77DD;background:#F8F7FE;color:#3C3489;padding:8px 12px;margin-bottom:10px;font-size:0.9em;line-height:1.45;">
                {ust}
              </div>
              <div style="color:#6B7280;font-size:0.8em;">
                {source_label} · {pub_str} · <a href="{url}" target="_blank" style="color:#7F77DD;text-decoration:none;">View source →</a>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

df = load_signals()

# --- Top bar ---
h1, h2 = st.columns([4, 1])
with h1:
    st.title("🔍 UST Competitive Intelligence")
    st.caption("Competitive signals — TCS · Infosys · Wipro · Accenture · HCL")
with h2:
    st.markdown(
        f"""
        <div style="background:#EAF3DE;color:#27500A;padding:8px 14px;border-radius:20px;
                    text-align:center;font-size:0.82em;margin-top:1.8em;font-weight:600;">
          Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
        </div>
        """,
        unsafe_allow_html=True,
    )

if df.empty:
    st.warning("No signals found. Run the pipeline first: `python pipeline/competitor_news.py`")
    st.stop()

# --- 7-day slice for metrics + charts ---
week_ago = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
df_recent = df[df["created_at"] >= week_ago]

# --- Metric cards ---
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.metric("Total signals (7d)", len(df_recent))
with m2:
    high_pri = int((df_recent["significance"].fillna(0) >= 4).sum())
    st.metric("High priority (sig ≥ 4)", high_pri)
with m3:
    top_comp = df_recent["competitor"].value_counts().idxmax() if not df_recent.empty else "—"
    st.metric("Most active competitor", top_comp)
with m4:
    cat_series = df_recent["category"].dropna()
    top_cat = cat_series.value_counts().idxmax() if not cat_series.empty else "—"
    st.metric("Top category", top_cat)

st.markdown("")

# --- Charts ---
c1, c2 = st.columns(2)
with c1:
    st.subheader("Signals by competitor (7d)")
    comp_counts = df_recent["competitor"].value_counts().reset_index()
    comp_counts.columns = ["competitor", "count"]
    if comp_counts.empty:
        st.info("No signals in the last 7 days.")
    else:
        fig = px.bar(
            comp_counts,
            x="count", y="competitor", orientation="h",
            color="competitor",
            color_discrete_map=COMPETITOR_COLORS,
        )
        fig.update_layout(
            showlegend=False, height=320,
            margin=dict(l=0, r=10, t=10, b=0),
            yaxis_title="", xaxis_title="",
            yaxis={"categoryorder": "total ascending"},
        )
        st.plotly_chart(fig, use_container_width=True)

with c2:
    st.subheader("Signals by category (7d)")
    cat_counts = df_recent["category"].dropna().value_counts().reset_index()
    cat_counts.columns = ["category", "count"]
    if cat_counts.empty:
        st.info("No signals in the last 7 days.")
    else:
        fig = px.bar(
            cat_counts,
            x="count", y="category", orientation="h",
            color_discrete_sequence=["#7F77DD"],
        )
        fig.update_layout(
            showlegend=False, height=320,
            margin=dict(l=0, r=10, t=10, b=0),
            yaxis_title="", xaxis_title="",
            yaxis={"categoryorder": "total ascending"},
        )
        st.plotly_chart(fig, use_container_width=True)

# --- Sidebar filters ---
with st.sidebar:
    st.header("Filters")
    competitors = sorted(df["competitor"].dropna().unique().tolist())
    categories  = sorted(df["category"].dropna().unique().tolist())
    sources     = sorted(df["source_type"].dropna().unique().tolist())

    f_comp = st.multiselect("Competitor",   competitors, default=competitors)
    f_cat  = st.multiselect("Category",     categories,  default=categories)
    f_src  = st.multiselect("Source type",  sources,     default=sources)
    f_sig  = st.slider("Minimum significance", min_value=1, max_value=5, value=3)
    show_low_conf = st.checkbox("Show low-confidence signals", value=False)

    st.markdown("---")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# --- Signal feed ---
st.markdown("---")
st.subheader("Signal feed")

mask = (
    df["competitor"].isin(f_comp)
    & df["category"].isin(f_cat)
    & df["source_type"].isin(f_src)
    & (df["significance"].fillna(0) >= f_sig)
)
if not show_low_conf:
    low_conf = (
        df["ust_relevance"].fillna("").str.contains(
            "Monitor — insufficient detail", regex=False
        )
        & (df["significance"].fillna(0) <= 3)
    )
    mask &= ~low_conf
df_filtered = df[mask].sort_values("created_at", ascending=False)

if df_filtered.empty:
    st.info("No signals match the current filters.")
else:
    shown = min(len(df_filtered), 100)
    st.caption(f"Showing {shown} of {len(df_filtered)} matching signals")
    for _, row in df_filtered.head(100).iterrows():
        render_signal_card(row)

# --- Weekly strategic brief ---
st.markdown("---")
st.subheader("Weekly strategic brief")
st.caption("Summarises all signals from the last 7 days with significance ≥ 3.")

if st.button("Generate this week's brief", type="primary"):
    top_signals = df[
        (df["created_at"] >= week_ago)
        & (df["significance"].fillna(0) >= 3)
    ].sort_values("significance", ascending=False).head(30)

    if top_signals.empty:
        st.info("No signals from the last 7 days meet the significance threshold.")
    else:
        signals_text = ""
        for _, row in top_signals.iterrows():
            signals_text += (
                f"- [{row['competitor']}] {row['category']} "
                f"(sig {int(row['significance'])}): "
                f"{row['raw_summary']} | UST: {row['ust_relevance']}\n"
            )

        user_msg = f"""You are a senior strategy advisor to UST's CTO office.

Here are this week's competitive intelligence signals:

{signals_text}

Write a strategic brief in exactly this format:

**This week's top moves**
[2-3 sentences on the most significant competitor actions this week, naming companies and deals]

**Market pattern**
[1-2 sentences on what trend emerges when you look across all 5 competitors together]

**Recommended actions for UST**
1. [Specific action, naming vertical and competitor]
2. [Specific action, naming vertical and competitor]
3. [Specific action, naming vertical and competitor]

Ground every statement in the signals above.
Be specific — name companies, deals, and verticals.
Do not be vague or generic."""

        try:
            client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)
            with st.spinner("Generating strategic brief..."):
                resp = client.chat.completions.create(
                    model=AZURE_MODEL,
                    messages=[
                        {"role": "system", "content": UST_PROFILE},
                        {"role": "user",   "content": user_msg},
                    ],
                    temperature=0.3,
                )
            brief_text = (resp.choices[0].message.content or "").strip()
            st.markdown(
                f"""
                <div style="background:#f8f0ff;
                            border-left:3px solid #534AB7;
                            border-radius:8px;
                            padding:16px 20px;
                            margin-top:12px;">
                {brief_text}
                </div>
                """,
                unsafe_allow_html=True,
            )
        except Exception as e:
            st.error(f"Brief generation failed: {e}")
