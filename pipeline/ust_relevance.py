import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import re
import time
from datetime import datetime

import psycopg2
from dotenv import load_dotenv
from openai import OpenAI

from data.ust_context import UST_PROFILE_SUMMARY

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

DB_DSN         = os.getenv("DB_DSN")
AZURE_ENDPOINT = os.getenv("AZURE_ENDPOINT")
AZURE_API_KEY  = os.getenv("AZURE_API_KEY")
AZURE_MODEL    = os.getenv("AZURE_MODEL")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""\
You are a senior competitive intelligence analyst at UST Global.
{UST_PROFILE_SUMMARY}

Your job: given one competitor signal, assess its relevance to UST.
CRITICAL RULES:
- Base your answer ONLY on the signal text provided. No outside knowledge.
- If the signal doesn't clearly support a claim, output null for that field.
- Never speculate. Never invent facts.
- Temperature is 0.1 — be precise and consistent.

Scoring rubric for ust_relevance_score:
  5 = Direct threat to UST's top vertical or core differentiator
  4 = Competitor entering UST's space or landing a major win in UST's target accounts
  3 = Relevant to UST's strategic priorities (GenAI, Healthcare, BFSI)
  2 = Tangentially related — worth monitoring
  1 = Irrelevant to UST's competitive position

If ust_relevance_score <= 2, set ust_so_what and ust_action_hint to null — \
do not waste tokens on low-signal items.

Respond in JSON only:
{{
  "ust_relevance_score": integer 1-5,
  "ust_so_what": "one sentence on what this means for UST specifically, or null",
  "ust_action_hint": "one sentence on what UST should consider doing, or null",
  "ust_affected_verticals": ["Healthcare", "BFSI"] or [],
  "ust_threat_or_opportunity": "threat" | "opportunity" | "neutral" | "unknown",
  "ust_confidence": "high" | "medium" | "low" | "unknown"
}}\
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

_NEW_COLUMNS = [
    ("ust_relevance_score",        "INTEGER"),
    ("ust_so_what",                "TEXT"),
    ("ust_action_hint",            "TEXT"),
    ("ust_affected_verticals",     "TEXT"),   # comma-separated list
    ("ust_threat_or_opportunity",  "TEXT"),
    ("ust_confidence",             "TEXT"),
]


def run_migration(conn) -> None:
    """Add UST-relevance enrichment columns to signals if they don't already exist."""
    with conn.cursor() as cur:
        for col_name, col_type in _NEW_COLUMNS:
            cur.execute(
                f"ALTER TABLE signals ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
            )
    conn.commit()
    print("Migration complete — enrichment columns ready.")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_signal(conn, signal_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, competitor, title, raw_summary,
                   category, significance, source_type, ust_relevance
            FROM signals
            WHERE id = %s
            """,
            (signal_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))


def _build_user_prompt(signal: dict) -> str:
    return (
        f"Competitor: {signal['competitor']}\n"
        f"Category: {signal['category']}\n"
        f"AI Significance: {signal['significance']}\n"
        f"Source: {signal['source_type']}\n"
        f"Title: {signal['title']}\n"
        f"Summary: {signal['raw_summary'] or ''}\n"
        f"Existing relevance note: {signal['ust_relevance'] or ''}"
    )


def _call_gpt(client: OpenAI, signal: dict) -> dict:
    user_msg = _build_user_prompt(signal)
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=AZURE_MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.1,
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


def _save_enrichment(conn, signal_id: int, result: dict) -> None:
    score = result.get("ust_relevance_score")
    # Enforce: no so_what/action_hint for low-signal items
    so_what     = result.get("ust_so_what")     if score and score > 2 else None
    action_hint = result.get("ust_action_hint") if score and score > 2 else None

    verticals = result.get("ust_affected_verticals") or []
    verticals_str = ", ".join(verticals) if isinstance(verticals, list) else (verticals or "")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE signals SET
                ust_relevance_score       = %s,
                ust_so_what               = %s,
                ust_action_hint           = %s,
                ust_affected_verticals    = %s,
                ust_threat_or_opportunity = %s,
                ust_confidence            = %s
            WHERE id = %s
            """,
            (
                score,
                so_what,
                action_hint,
                verticals_str,
                result.get("ust_threat_or_opportunity"),
                result.get("ust_confidence"),
                signal_id,
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_with_ust_relevance(signal_id: int) -> dict:
    """
    Fetch signal by ID, score its UST relevance via GPT-4o, persist results.
    Creates its own DB connection and Azure client — suitable for one-off calls.
    Returns the raw classification dict.
    """
    conn   = psycopg2.connect(DB_DSN)
    client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)
    try:
        signal = _fetch_signal(conn, signal_id)
        if signal is None:
            raise ValueError(f"Signal id={signal_id} not found in database")
        result = _call_gpt(client, signal)
        _save_enrichment(conn, signal_id, result)
        return result
    finally:
        conn.close()


def backfill_ust_relevance(min_ai_significance: int = 3) -> int:
    """
    Enrich all signals where ust_relevance_score IS NULL and
    significance >= min_ai_significance.
    Reuses one DB connection and one Azure client for the full run.
    Returns count of successfully enriched signals.
    """
    conn   = psycopg2.connect(DB_DSN)
    client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)
    enriched = 0

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM signals
                WHERE ust_relevance_score IS NULL
                  AND significance >= %s
                ORDER BY significance DESC, created_at DESC
                """,
                (min_ai_significance,),
            )
            ids = [row[0] for row in cur.fetchall()]

        total = len(ids)
        print(f"Found {total} signals to enrich (significance >= {min_ai_significance})\n")

        for i, signal_id in enumerate(ids, 1):
            try:
                signal = _fetch_signal(conn, signal_id)
                if signal is None:
                    print(f"  [{i}/{total}] id={signal_id} — not found, skipping")
                    continue

                result  = _call_gpt(client, signal)
                _save_enrichment(conn, signal_id, result)
                enriched += 1

                score   = result.get("ust_relevance_score", "?")
                t_or_o  = result.get("ust_threat_or_opportunity", "?")
                conf    = result.get("ust_confidence", "?")
                print(
                    f"  Enriched signal {i}/{total} "
                    f"(id={signal_id}, {signal['competitor']}) "
                    f"score={score} {t_or_o} conf={conf}"
                )
            except Exception as e:
                print(f"  Error on signal {i}/{total} (id={signal_id}): {e}")

            if i < total:
                time.sleep(2)

    finally:
        conn.close()

    return enriched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn = psycopg2.connect(DB_DSN)
    try:
        run_migration(conn)
    finally:
        conn.close()

    print(f"\nStarting backfill — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
    enriched = backfill_ust_relevance(min_ai_significance=3)

    print(f"\n{'─' * 50}")
    print(f"  Done. Enriched {enriched} signals.")
    print(f"{'─' * 50}\n")


if __name__ == "__main__":
    main()
