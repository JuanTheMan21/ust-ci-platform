import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
DB_DSN = os.getenv("DB_DSN")


def _connect():
    return psycopg2.connect(DB_DSN)


def get_competitor_activity(days: int = 7) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH current AS (
                    SELECT competitor,
                           COUNT(*)        AS count,
                           AVG(significance) AS avg_sig
                    FROM signals
                    WHERE created_at >= NOW() - INTERVAL %(interval)s
                    GROUP BY competitor
                ),
                previous AS (
                    SELECT competitor, COUNT(*) AS count
                    FROM signals
                    WHERE created_at >= NOW() - INTERVAL %(prev_start)s
                      AND created_at <  NOW() - INTERVAL %(interval)s
                    GROUP BY competitor
                )
                SELECT c.competitor,
                       c.count,
                       ROUND(c.avg_sig::numeric, 2) AS avg_sig,
                       CASE
                           WHEN p.count IS NULL OR p.count = 0 THEN NULL
                           ELSE ROUND(((c.count - p.count)::numeric / p.count) * 100, 1)
                       END AS wow_delta_pct
                FROM current c
                LEFT JOIN previous p USING (competitor)
                ORDER BY c.count DESC
            """, {
                "interval":   f"{days} days",
                "prev_start": f"{days * 2} days",
            })
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_top_categories(days: int = 7) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT competitor, category, COUNT(*) AS count
                FROM signals
                WHERE created_at >= NOW() - INTERVAL %(interval)s
                  AND category IS NOT NULL
                GROUP BY competitor, category
                ORDER BY competitor, count DESC
            """, {"interval": f"{days} days"})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_ust_threat_summary(days: int = 7) -> dict:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT competitor, ust_threat_or_opportunity, COUNT(*) AS count
                FROM signals
                WHERE created_at >= NOW() - INTERVAL %(interval)s
                  AND ust_threat_or_opportunity IS NOT NULL
                GROUP BY competitor, ust_threat_or_opportunity
                ORDER BY competitor
            """, {"interval": f"{days} days"})
            result: dict[str, dict] = {}
            for competitor, label, count in cur.fetchall():
                result.setdefault(competitor, {})
                result[competitor][label] = count
            return result
    finally:
        conn.close()


def get_high_relevance_signals(min_ust_score: int = 4, days: int = 7) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT *
                FROM signals
                WHERE ust_relevance_score >= %(min_score)s
                  AND created_at >= NOW() - INTERVAL %(interval)s
                ORDER BY ust_relevance_score DESC, created_at DESC
                LIMIT 20
            """, {"min_score": min_ust_score, "interval": f"{days} days"})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_linkedin_hiring_trends(days: int = 7) -> list[dict]:
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT competitor, source_type, category, COUNT(*) AS count
                FROM signals
                WHERE source_type IN ('linkedin_company', 'linkedin_jobs')
                  AND created_at >= NOW() - INTERVAL %(interval)s
                GROUP BY competitor, source_type, category
                ORDER BY competitor, source_type, count DESC
            """, {"interval": f"{days} days"})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n-- Competitor activity (7d) --")
    for row in get_competitor_activity():
        delta = f"{row['wow_delta_pct']:+.1f}%" if row["wow_delta_pct"] is not None else "n/a"
        print(f"  {row['competitor']:10s}  signals={row['count']:3d}  avg_sig={row['avg_sig']}  WoW={delta}")

    print("\n-- Top categories (7d) --")
    for row in get_top_categories():
        print(f"  {row['competitor']:10s}  {row['category']:25s}  {row['count']}")

    print("\n-- UST threat summary (7d) --")
    for comp, buckets in get_ust_threat_summary().items():
        print(f"  {comp:10s}  {buckets}")

    print("\n-- High-relevance signals (score >= 4, 7d) --")
    rows = get_high_relevance_signals()
    if rows:
        for row in rows:
            print(f"  [{row['competitor']}] score={row['ust_relevance_score']}  {row['title'][:70]}")
    else:
        print("  (none — run ust_relevance.py backfill first)")

    print("\n-- LinkedIn hiring trends (7d) --")
    rows = get_linkedin_hiring_trends()
    if rows:
        for row in rows:
            print(f"  {row['competitor']:10s}  {row['source_type']:18s}  {row['category']:25s}  {row['count']}")
    else:
        print("  (none — run linkedin_ingest.py first)")

    print()
