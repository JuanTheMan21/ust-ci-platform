import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DB_DSN         = os.getenv("DB_DSN")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_KEY  = os.getenv("AZURE_API_KEY")
AZURE_MODEL    = os.getenv("AZURE_MODEL")

_SYSTEM_PROMPT = """\
You are a competitive intelligence analyst. Group the following signals into \
thematic stories. Base groupings ONLY on shared entities, topics, or companies \
actually named in the signal titles. Do not invent themes. Ungrouped signals \
are valid. Output JSON only.\
"""

_SCHEMA = """
Expected JSON output — an array of cluster objects:
[
  {
    "theme": "short descriptive label grounded in the signal titles",
    "signal_ids": [list of integer IDs from the input],
    "competitors": ["Infosys"],
    "narrative": "one sentence, grounded in signal titles only",
    "ust_implication": "one sentence or null",
    "momentum": "building" | "steady" | "fading"
  }
]
Omit any cluster with fewer than 2 signal_ids.
Output only the JSON array — no prose, no markdown fences.
"""


def _ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signal_clusters (
                id           SERIAL PRIMARY KEY,
                theme        TEXT NOT NULL,
                narrative    TEXT,
                competitors  TEXT,
                ust_implication TEXT,
                momentum     TEXT,
                signal_count INTEGER,
                signal_ids   TEXT,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()


def _fetch_signals(conn, days: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, title, competitor, category,
                   created_at::date AS date
            FROM signals
            WHERE created_at >= NOW() - INTERVAL %(interval)s
            ORDER BY created_at DESC
        """, {"interval": f"{days} days"})
        return [dict(r) for r in cur.fetchall()]


def _call_gpt(client: OpenAI, signals: list[dict]) -> list[dict]:
    payload = [
        {
            "id":         s["id"],
            "title":      s["title"],
            "competitor": s["competitor"],
            "category":   s["category"],
            "date":       str(s["date"]),
        }
        for s in signals
    ]
    user_msg = json.dumps(payload, ensure_ascii=False) + "\n" + _SCHEMA

    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=AZURE_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0,
            )
            break
        except Exception as e:
            if attempt == 0 and "429" in str(e):
                time.sleep(10)
                continue
            raise

    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$",           "", raw)
    return json.loads(raw)


def _save_cluster(conn, cluster: dict) -> None:
    competitors = ", ".join(cluster.get("competitors") or [])
    signal_ids  = ",".join(str(i) for i in cluster.get("signal_ids") or [])
    count       = len(cluster.get("signal_ids") or [])

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO signal_clusters
                (theme, narrative, competitors, ust_implication,
                 momentum, signal_count, signal_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            cluster.get("theme"),
            cluster.get("narrative"),
            competitors,
            cluster.get("ust_implication"),
            cluster.get("momentum"),
            count,
            signal_ids,
        ))
    conn.commit()


def cluster_signals(days: int = 7) -> list[dict]:
    conn   = psycopg2.connect(DB_DSN)
    client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)

    try:
        _ensure_table(conn)

        signals = _fetch_signals(conn, days)
        if len(signals) < 5:
            print(f"Only {len(signals)} signals in last {days}d — need at least 5 to cluster.")
            return []

        print(f"Clustering {len(signals)} signals from last {days}d...")

        try:
            clusters = _call_gpt(client, signals)
        except (json.JSONDecodeError, Exception) as e:
            print(f"GPT-4o clustering error: {e}")
            return []

        if not isinstance(clusters, list):
            print(f"Unexpected GPT-4o response shape: {type(clusters)}")
            return []

        saved = []
        for cluster in clusters:
            ids = cluster.get("signal_ids") or []
            if len(ids) < 2:
                continue
            try:
                _save_cluster(conn, cluster)
                saved.append(cluster)
            except Exception as e:
                print(f"  DB error saving cluster '{cluster.get('theme', '?')}': {e}")
                conn.rollback()

        print(f"Saved {len(saved)} clusters (>= 2 signals each).")
        return saved

    finally:
        conn.close()


if __name__ == "__main__":
    results = cluster_signals(days=7)
    if not results:
        print("No clusters produced.")
    else:
        print(f"\n{len(results)} clusters:\n")
        for c in results:
            ids     = c.get("signal_ids", [])
            comps   = ", ".join(c.get("competitors") or [])
            imply   = c.get("ust_implication") or "—"
            print(f"  [{c.get('momentum','?'):8s}] {c.get('theme','?')}")
            print(f"             competitors: {comps}  signals: {ids}")
            print(f"             narrative:   {c.get('narrative','')}")
            print(f"             UST:         {imply}")
            print()
