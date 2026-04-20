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
# Graceful imports of new pipeline modules
# ---------------------------------------------------------------------------

try:
    from pipeline.trends import get_competitor_activity, get_ust_threat_summary
    _trends_ok = True
except ImportError:
    _trends_ok = False

try:
    from pipeline.signal_clustering import cluster_signals
    _clustering_ok = True
except ImportError:
    _clustering_ok = False

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
LINKEDIN_BADGE   = ("#E8F4FD", "#0A66C2")

THREAT_OPP_COLORS = {
    "threat":      ("#FFE4E4", "#C62828"),
    "opportunity": ("#E4F9E4", "#1B5E20"),
    "neutral":     ("#F3F4F6", "#374151"),
    "unknown":     ("#F3F4F6", "#374151"),
}

MOMENTUM_COLORS = {
    "building": ("#D1FAE5", "#065F46"),
    "steady":   ("#FEF3C7", "#92400E"),
    "fading":   ("#FEE2E2", "#991B1B"),
}

SOURCE_LABEL = {
    "news":             "News",
    "bse_filing":       "BSE Filing",
    "sec_filing":       "SEC 8-K",
    "linkedin_company": "LinkedIn",
    "linkedin_jobs":    "LinkedIn Jobs",
    "patent":           "Patent",
}

COMPETITORS_ORDERED = ["TCS", "Infosys", "Wipro", "Accenture", "HCL"]

# ---------------------------------------------------------------------------
# Data — cached fetches (no st.* calls inside)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def fetch_signals_cached():
    """Pure data fetch — no st.* calls allowed here."""
    import psycopg2, os, pandas as pd
    from dotenv import load_dotenv
    load_dotenv()
    conn = psycopg2.connect(os.getenv("DB_DSN"))
    try:
        cur = conn.cursor()
        # Try full query with enrichment columns; fall back if migration not run yet
        try:
            cur.execute("""
                SELECT id, competitor, title, url, published_date,
                       created_at, category, significance,
                       ust_relevance, raw_summary, source_type,
                       ust_relevance_score, ust_so_what, ust_action_hint,
                       ust_threat_or_opportunity, ust_confidence
                FROM signals
                ORDER BY created_at DESC
            """)
        except Exception:
            conn.rollback()
            cur.execute("""
                SELECT 0 AS id, competitor, title, url, published_date,
                       created_at, category, significance,
                       ust_relevance, raw_summary, source_type,
                       NULL::int  AS ust_relevance_score,
                       NULL       AS ust_so_what,
                       NULL       AS ust_action_hint,
                       NULL       AS ust_threat_or_opportunity,
                       NULL       AS ust_confidence
                FROM signals
                ORDER BY created_at DESC
            """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(rows, columns=cols)
        if not df.empty:
            df["created_at"]     = pd.to_datetime(df["created_at"],     errors="coerce", utc=True)
            df["published_date"] = pd.to_datetime(df["published_date"], errors="coerce", utc=True)
            df["source_type"]    = df["source_type"].fillna("news")
        return df
    finally:
        conn.close()


def load_signals():
    """Wrapper that handles errors with st.* calls."""
    try:
        return fetch_signals_cached()
    except Exception as e:
        st.error(f"Database error: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=300)
def fetch_clusters_cached():
    """Fetch recent signal clusters from DB."""
    import psycopg2, os, pandas as pd
    from dotenv import load_dotenv
    load_dotenv()
    conn = psycopg2.connect(os.getenv("DB_DSN"))
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, theme, narrative, competitors, ust_implication,
                   momentum, signal_count, signal_ids, created_at
            FROM signal_clusters
            ORDER BY created_at DESC
            LIMIT 50
        """)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        df = pd.DataFrame(rows, columns=cols)
        if not df.empty:
            df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce", utc=True)
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


@st.cache_data(ttl=300)
def load_competitor_activity():
    if not _trends_ok:
        return []
    try:
        return get_competitor_activity(days=7)
    except Exception:
        return []


@st.cache_data(ttl=300)
def load_threat_summary():
    if not _trends_ok:
        return {}
    try:
        return get_ust_threat_summary(days=7)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _esc(text):
    if text is None or isinstance(text, (int, float)):
        return ""
    return str(text).replace("<", "&lt;").replace(">", "&gt;")


def render_signal_card(row):
    source     = row.get("source_type") or "news"
    is_li      = source in ("linkedin_company", "linkedin_jobs")
    is_filing  = source in ("bse_filing", "sec_filing")

    bg, fg     = LINKEDIN_BADGE if is_li else COMPETITOR_BADGE.get(row.get("competitor", ""), NEUTRAL_BADGE)
    left_color = COMPETITOR_COLORS.get(row.get("competitor", ""), "#7F77DD")
    src_bg, src_fg = REGULATORY_BADGE if is_filing else NEUTRAL_BADGE
    source_label   = SOURCE_LABEL.get(source, source)

    sig = int(row.get("significance") or 0) if pd.notna(row.get("significance")) else 0
    sig = max(0, min(5, sig))
    dots = "●" * sig + "○" * (5 - sig)

    pub = row.get("published_date")
    pub_str = pub.strftime("%Y-%m-%d") if pd.notna(pub) else "—"

    title = _esc(row.get("title") or "")
    url   = row.get("url") or "#"

    # --- UST relevance score badge ---
    ust_score = row.get("ust_relevance_score")
    ust_score_html = ""
    if pd.notna(ust_score) and ust_score:
        ust_score_html = (
            f'<span style="background:#EDE9FE;color:#5B21B6;padding:3px 10px;'
            f'border-radius:12px;font-size:0.78em;font-weight:600;">'
            f'UST {int(ust_score)}/5</span>'
        )

    # --- threat/opportunity chip ---
    t_or_o = str(row.get("ust_threat_or_opportunity") or "").lower()
    threat_html = ""
    if t_or_o and t_or_o not in ("unknown", ""):
        t_bg, t_fg = THREAT_OPP_COLORS.get(t_or_o, NEUTRAL_BADGE)
        threat_html = (
            f'<span style="background:{t_bg};color:{t_fg};padding:3px 10px;'
            f'border-radius:12px;font-size:0.78em;font-weight:600;">'
            f'{t_or_o}</span>'
        )

    # --- relevance box: prefer ust_so_what, fall back to ust_relevance ---
    relevance_text = _esc(row.get("ust_so_what")) or _esc(row.get("ust_relevance"))
    relevance_html = (
        f'<div style="border-left:3px solid #7F77DD;background:#F8F7FE;color:#3C3489;'
        f'padding:8px 12px;margin-bottom:6px;font-size:0.9em;line-height:1.45;">'
        f'{relevance_text}</div>'
    ) if relevance_text else ""

    # --- action hint ---
    action_hint = _esc(row.get("ust_action_hint"))
    action_html = (
        f'<div style="color:#6B7280;font-size:0.82em;font-style:italic;'
        f'margin-bottom:8px;">{action_hint}</div>'
    ) if action_hint else ""

    with st.container():
        st.markdown(
            f"""
            <div style="border:1px solid #E5E7EB;border-left:3px solid {left_color};
                        border-radius:10px;padding:16px 18px;margin-bottom:14px;background:white;">
              <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
                <span style="background:{bg};color:{fg};padding:3px 10px;
                             border-radius:12px;font-size:0.78em;font-weight:600;">
                  {row.get("competitor", "—")}
                </span>
                <span style="background:{NEUTRAL_BADGE[0]};color:{NEUTRAL_BADGE[1]};
                             padding:3px 10px;border-radius:12px;font-size:0.78em;">
                  {row.get("category") or "—"}
                </span>
                <span style="background:{src_bg};color:{src_fg};padding:3px 10px;
                             border-radius:12px;font-size:0.78em;font-weight:600;">
                  {source_label}
                </span>
                {ust_score_html}
                {threat_html}
                <span style="color:#D85A30;font-size:0.95em;letter-spacing:3px;margin-left:auto;">
                  {dots}
                </span>
              </div>
              <div style="font-size:1.02em;font-weight:600;margin-bottom:10px;line-height:1.35;">
                <a href="{url}" target="_blank" style="color:#111;text-decoration:none;">{title}</a>
              </div>
              {relevance_html}
              {action_html}
              <div style="color:#6B7280;font-size:0.8em;">
                {source_label} · {pub_str} ·
                <a href="{url}" target="_blank" style="color:#7F77DD;text-decoration:none;">View source →</a>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_cluster_card(cluster, df_signals):
    theme        = _esc(str(cluster.get("theme") or "Unknown theme"))
    narrative    = _esc(str(cluster.get("narrative") or ""))
    ust_impl     = _esc(str(cluster.get("ust_implication") or ""))
    momentum     = str(cluster.get("momentum") or "").lower()
    signal_count = int(cluster.get("signal_count") or 0)

    competitors = [c.strip() for c in str(cluster.get("competitors") or "").split(",") if c.strip()]
    try:
        signal_ids = [int(x.strip()) for x in str(cluster.get("signal_ids") or "").split(",") if x.strip()]
    except ValueError:
        signal_ids = []

    m_bg, m_fg = MOMENTUM_COLORS.get(momentum, NEUTRAL_BADGE)

    comp_badges = " ".join(
        f'<span style="background:{COMPETITOR_BADGE.get(c, NEUTRAL_BADGE)[0]};'
        f'color:{COMPETITOR_BADGE.get(c, NEUTRAL_BADGE)[1]};'
        f'padding:3px 8px;border-radius:12px;font-size:0.78em;font-weight:600;">{c}</span>'
        for c in competitors
    )
    ust_impl_html = (
        f'<div style="border-left:3px solid #7F77DD;background:#F8F7FE;color:#3C3489;'
        f'padding:8px 12px;font-size:0.88em;margin-top:8px;">{ust_impl}</div>'
    ) if ust_impl else ""

    with st.container():
        st.markdown(
            f"""
            <div style="border:1px solid #E5E7EB;border-left:3px solid #534AB7;
                        border-radius:10px;padding:16px 18px;margin-bottom:4px;background:white;">
              <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
                {comp_badges}
                <span style="background:{m_bg};color:{m_fg};padding:3px 10px;
                             border-radius:12px;font-size:0.78em;font-weight:600;margin-left:auto;">
                  {momentum or "unknown"}
                </span>
                <span style="background:{NEUTRAL_BADGE[0]};color:{NEUTRAL_BADGE[1]};
                             padding:3px 10px;border-radius:12px;font-size:0.78em;">
                  {signal_count} signals
                </span>
              </div>
              <div style="font-size:1.05em;font-weight:700;margin-bottom:6px;color:#1a1a2e;">
                {theme}
              </div>
              <div style="font-size:0.9em;color:#374151;margin-bottom:4px;">{narrative}</div>
              {ust_impl_html}
            </div>
            """,
            unsafe_allow_html=True,
        )
        if signal_ids and "id" in df_signals.columns:
            matching = df_signals[df_signals["id"].isin(signal_ids)]
            if not matching.empty:
                with st.expander(f"View {len(matching)} signals in this cluster"):
                    for _, srow in matching.iterrows():
                        render_signal_card(srow)
        st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)


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

# --- Competitor activity bar ---
activity_data    = load_competitor_activity()
activity_by_comp = {r["competitor"]: r for r in activity_data}

act_cols = st.columns(5)
for col, comp in zip(act_cols, COMPETITORS_ORDERED):
    entry      = activity_by_comp.get(comp, {})
    count      = entry.get("count", 0)
    delta      = entry.get("wow_delta_pct")
    left_color = COMPETITOR_COLORS[comp]

    if delta is None:
        delta_html = '<span style="color:#9CA3AF;font-size:0.8em;">WoW: —</span>'
    elif float(delta) >= 0:
        delta_html = f'<span style="color:#16A34A;font-size:0.8em;">&#9650; {float(delta):+.0f}%</span>'
    else:
        delta_html = f'<span style="color:#DC2626;font-size:0.8em;">&#9660; {float(delta):.0f}%</span>'

    with col:
        st.markdown(
            f"""
            <div style="border:1px solid #E5E7EB;border-top:3px solid {left_color};
                        border-radius:8px;padding:12px 14px;background:white;text-align:center;">
              <div style="font-size:0.82em;font-weight:600;color:#374151;margin-bottom:4px;">{comp}</div>
              <div style="font-size:1.6em;font-weight:700;color:#1a1a2e;line-height:1;">{count}</div>
              <div style="margin-top:4px;">{delta_html}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.markdown("")

if df.empty:
    st.warning("No signals found. Run the pipeline first: `python pipeline/competitor_news.py`")
    st.stop()

# --- 7-day slice ---
week_ago  = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)
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
            comp_counts, x="count", y="competitor", orientation="h",
            color="competitor", color_discrete_map=COMPETITOR_COLORS,
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
            cat_counts, x="count", y="category", orientation="h",
            color_discrete_sequence=["#7F77DD"],
        )
        fig.update_layout(
            showlegend=False, height=320,
            margin=dict(l=0, r=10, t=10, b=0),
            yaxis_title="", xaxis_title="",
            yaxis={"categoryorder": "total ascending"},
        )
        st.plotly_chart(fig, use_container_width=True)

# --- Sidebar ---
with st.sidebar:
    st.header("Filters")
    competitors = sorted(df["competitor"].dropna().unique().tolist())
    categories  = sorted(df["category"].dropna().unique().tolist())
    sources     = sorted(df["source_type"].dropna().unique().tolist())

    f_comp        = st.multiselect("Competitor",  competitors, default=competitors)
    f_cat         = st.multiselect("Category",    categories,  default=categories)
    f_src         = st.multiselect("Source type", sources,     default=sources)
    f_sig         = st.slider("Minimum significance", min_value=1, max_value=5, value=3)
    show_low_conf = st.checkbox("Show low-confidence signals", value=False)

    st.markdown("---")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    # --- UST Threat Radar ---
    st.markdown("---")
    st.subheader("UST Threat Radar (7d)")
    threat_summary = load_threat_summary()
    if not threat_summary:
        st.caption("Run `pipeline/ust_relevance.py` to populate.")
    else:
        for comp in COMPETITORS_ORDERED:
            buckets = threat_summary.get(comp, {})
            if not buckets:
                continue
            threats = buckets.get("threat", 0)
            opps    = buckets.get("opportunity", 0)
            neutral = buckets.get("neutral", 0) + buckets.get("unknown", 0)
            st.markdown(
                f"""
                <div style="font-size:0.82em;padding:5px 0;
                            border-bottom:1px solid #f0f0f0;
                            display:flex;justify-content:space-between;align-items:center;">
                  <span style="font-weight:600;color:#374151;">{comp}</span>
                  <span>
                    <span style="color:#C62828;">&#128308; {threats}</span>&nbsp;
                    <span style="color:#1B5E20;">&#128994; {opps}</span>&nbsp;
                    <span style="color:#6B7280;">&#9898; {neutral}</span>
                  </span>
                </div>
                """,
                unsafe_allow_html=True,
            )

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_feed, tab_linkedin, tab_clusters = st.tabs(
    ["Signal Feed", "LinkedIn Signals", "Story Clusters"]
)

# --- Tab 1: Signal Feed ---
with tab_feed:
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
            df["ust_relevance"].fillna("").str.contains("Monitor — insufficient detail", regex=False)
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

# --- Tab 2: LinkedIn Signals ---
with tab_linkedin:
    st.subheader("LinkedIn Signals")

    df_li      = df[df["source_type"].isin(["linkedin_company", "linkedin_jobs"])]
    df_li      = df_li[df_li["competitor"].isin(f_comp)]
    li_company = df_li[df_li["source_type"] == "linkedin_company"]
    li_jobs    = df_li[df_li["source_type"] == "linkedin_jobs"]

    if df_li.empty:
        st.info("No LinkedIn signals found. Run `python pipeline/linkedin_ingest.py` to populate.")
    else:
        lc1, lc2 = st.columns(2)
        with lc1:
            st.metric("Company posts", len(li_company))
        with lc2:
            st.metric("Job postings", len(li_jobs))
        st.markdown("")

        if not li_company.empty:
            st.markdown("#### Company posts")
            for _, row in li_company.head(50).iterrows():
                if row is not None:
                    render_signal_card(row)

        if not li_jobs.empty:
            st.markdown("#### Job postings")
            for _, row in li_jobs.head(50).iterrows():
                if row is not None:
                    render_signal_card(row)

# --- Tab 3: Story Clusters ---
with tab_clusters:
    st.subheader("Story Clusters")
    st.caption("Thematic groupings of related signals, detected by AI.")

    df_clusters = fetch_clusters_cached()

    btn_col, info_col = st.columns([1, 3])
    with btn_col:
        if st.button("Run clustering now", type="primary"):
            if _clustering_ok:
                with st.spinner("Clustering signals via GPT-4o..."):
                    try:
                        cluster_signals(days=7)
                        st.cache_data.clear()
                        st.rerun()
                    except Exception as e:
                        st.error(f"Clustering failed: {e}")
            else:
                st.error("signal_clustering module not available.")
    with info_col:
        if not df_clusters.empty:
            max_ts   = df_clusters["created_at"].max()
            last_run = max_ts.strftime("%Y-%m-%d %H:%M UTC") if pd.notna(max_ts) else "unknown"
            st.caption(f"{len(df_clusters)} clusters in DB · Last run: {last_run}")

    st.markdown("")

    if df_clusters.empty:
        st.info("No clusters yet. Click 'Run clustering now' to generate them.")
    else:
        for _, cluster_row in df_clusters.iterrows():
            render_cluster_card(cluster_row, df)

# ---------------------------------------------------------------------------
# Weekly strategic brief
# ---------------------------------------------------------------------------

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
                <div style="background:#f8f0ff;border-left:3px solid #534AB7;
                            border-radius:8px;padding:16px 20px;margin-top:12px;">
                {brief_text}
                </div>
                """,
                unsafe_allow_html=True,
            )
        except Exception as e:
            st.error(f"Brief generation failed: {e}")

# ---------------------------------------------------------------------------
# This Week in Numbers strip
# ---------------------------------------------------------------------------

st.markdown("---")
st.subheader("This week in numbers")

linkedin_count = int(df_recent["source_type"].isin(["linkedin_company", "linkedin_jobs"]).sum())
high_sig_count = int((df_recent["significance"].fillna(0) >= 4).sum())

if "ust_relevance_score" in df.columns:
    ust_high = int((df_recent["ust_relevance_score"].fillna(0) >= 4).sum())
    ust_high_label = str(ust_high)
else:
    ust_high_label = "—"

_num  = "font-size:1.6em;font-weight:700;color:#534AB7;"
_lbl  = "font-size:0.78em;color:#6B7280;margin-top:2px;"
_card = ("border:1px solid #E5E7EB;border-radius:8px;padding:14px;"
         "background:white;text-align:center;")

n1, n2, n3, n4, n5 = st.columns(5)
for col, number, label in [
    (n1, len(df_recent),    "Total signals (7d)"),
    (n2, high_sig_count,    "High-significance (4-5)"),
    (n3, linkedin_count,    "LinkedIn signals"),
    (n4, ust_high_label,    "UST high-relevance (4-5)"),
    (n5, 5,                 "Competitors monitored"),
]:
    with col:
        st.markdown(
            f'<div style="{_card}"><div style="{_num}">{number}</div>'
            f'<div style="{_lbl}">{label}</div></div>',
            unsafe_allow_html=True,
        )
