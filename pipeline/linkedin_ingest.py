import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib
import json
import re
import time
from datetime import datetime, timezone

import feedparser
import psycopg2
import yaml
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

FEEDS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "rss_feeds.yaml",
)

# Normalize competitor keys from yaml (lowercase) to display names
COMPETITOR_DISPLAY = {
    "tcs":       "TCS",
    "infosys":   "Infosys",
    "wipro":     "Wipro",
    "accenture": "Accenture",
    "hcl":       "HCL",
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

COMPANY_POST_PROMPT = f"""\
{UST_PROFILE_SUMMARY}

You are classifying a LinkedIn company page post from a competitor of UST.
LinkedIn company posts signal strategy, culture, partnerships, and product direction
— treat them as soft intelligence, not hard news.

Respond in JSON only:
{{
  "category": one of [Partnership, Leadership, Product_Launch, Strategic_Narrative, Award, Hiring_Campaign, Financial, Other],
  "significance": integer 1-5:
    5 = Partnership with major tech firm, C-suite announcement, acquisition hint
    4 = New product/platform launch, VP-level hire announcement, major client win
    3 = Strategic positioning post, new vertical/market entry signal
    2 = Award, certification, generic content
    1 = Promotional/recruitment content, filler
  "ust_relevance": one sentence — name the UST vertical or capability affected and say threat/opportunity/watch,
  "raw_summary": one sentence summary of the post,
  "named_entities": comma-separated list of notable people, companies, technologies, or products mentioned (empty string if none)
}}

If you cannot determine UST relevance, write "Monitor — insufficient detail" for ust_relevance.
Base your answer ONLY on the title and description provided.\
"""

JOBS_POST_PROMPT = f"""\
{UST_PROFILE_SUMMARY}

You are classifying a LinkedIn job posting from a competitor of UST.
Job postings reveal hiring intent — volume and seniority patterns signal strategic bets.

Respond in JSON only:
{{
  "category": one of [AI_ML_Hire, Cloud_Hire, Leadership_Hire, Domain_Hire, Engineering_Hire, Sales_Hire, Other],
  "significance": integer 1-5:
    5 = C-suite or VP hiring (signals strategic shift)
    4 = Director/Principal in AI, GenAI, or a specific platform (e.g. Salesforce, SAP, AWS)
    3 = Senior manager or cluster pattern in a vertical
    2 = Mid-level technical role in a strategic area
    1 = Junior, support, or non-strategic role
  "ust_relevance": one sentence — name the UST vertical or capability affected and say threat/opportunity/watch,
  "raw_summary": one sentence describing the role and its strategic signal,
  "job_title": extracted job title,
  "seniority": one of [C-suite, VP, Director, Principal, Senior_Manager, Senior, Mid, Junior],
  "key_skills": comma-separated skills or technologies mentioned,
  "location": location if mentioned, else empty string
}}

If you cannot determine UST relevance, write "Monitor — insufficient detail" for ust_relevance.
Base your answer ONLY on the title and description provided.\
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def parse_feedparser_date(entry) -> datetime | None:
    """Convert feedparser's time_struct to a UTC-aware datetime."""
    ts = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    if ts is None:
        return None
    try:
        return datetime(*ts[:6], tzinfo=timezone.utc)
    except Exception:
        return None


def is_duplicate(cur, uhash: str) -> bool:
    cur.execute("SELECT 1 FROM signals WHERE url_hash = %s", (uhash,))
    return cur.fetchone() is not None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def load_feeds() -> dict:
    with open(FEEDS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fetch_feed(source_type: str, competitor_key: str, url: str) -> list[dict]:
    """Fetch and parse one RSS feed. Returns list of article dicts."""
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        print(f"  [{COMPETITOR_DISPLAY[competitor_key]}][{source_type}] Feed error: {e}")
        return []

    if parsed.get("bozo") and not parsed.entries:
        exc = parsed.get("bozo_exception", "unknown error")
        print(f"  [{COMPETITOR_DISPLAY[competitor_key]}][{source_type}] Feed parse error: {exc}")
        return []

    articles = []
    for entry in parsed.entries:
        title = strip_html(getattr(entry, "title", "") or "")
        link  = getattr(entry, "link", "") or ""
        desc  = strip_html(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
        pub   = parse_feedparser_date(entry)

        if not title or not link:
            continue

        articles.append({
            "competitor":     COMPETITOR_DISPLAY[competitor_key],
            "title":          title,
            "url":            link,
            "description":    desc,
            "published_date": pub,
            "source_type":    source_type,
        })

    return articles


def classify(client: OpenAI, article: dict) -> dict:
    """Call GPT-4o with the appropriate prompt; return parsed JSON classification."""
    system_prompt = (
        COMPANY_POST_PROMPT if article["source_type"] == "linkedin_company"
        else JOBS_POST_PROMPT
    )
    user_msg = f"Title: {article['title']}\nDescription: {article['description']}"

    t0 = time.monotonic()
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=AZURE_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
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

    latency_ms = int((time.monotonic() - t0) * 1000)
    usage = resp.usage
    print(
        f"    [GPT-4o] model={resp.model} "
        f"prompt_tokens={usage.prompt_tokens} "
        f"completion_tokens={usage.completion_tokens} "
        f"latency_ms={latency_ms}"
    )

    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def save_signal(cur, article: dict, classification: dict) -> None:
    cur.execute(
        """
        INSERT INTO signals
            (competitor, title, url, published_date,
             category, significance, ust_relevance, raw_summary, url_hash,
             source_type)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (url_hash) DO NOTHING
        """,
        (
            article["competitor"],
            article["title"],
            article["url"],
            article["published_date"],
            classification["category"],
            int(classification["significance"]),
            classification["ust_relevance"],
            classification["raw_summary"],
            url_hash(article["url"]),
            article["source_type"],
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    feeds = load_feeds()
    conn   = psycopg2.connect(DB_DSN)
    client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)

    totals = {
        "linkedin_company": {"found": 0, "saved": 0, "skipped": 0, "errors": 0},
        "linkedin_jobs":    {"found": 0, "saved": 0, "skipped": 0, "errors": 0},
    }

    try:
        print(f"\nLinkedIn Ingest -- {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        for source_type, competitor_feeds in feeds.items():
            if source_type not in totals:
                print(f"  Unknown source_type '{source_type}' in feeds YAML — skipping")
                continue

            label = "Company posts" if source_type == "linkedin_company" else "Job postings"
            print(f"-- {label} --\n")

            for competitor_key, url in competitor_feeds.items():
                display = COMPETITOR_DISPLAY.get(competitor_key, competitor_key.upper())
                articles = fetch_feed(source_type, competitor_key, url)
                saved = skipped = errors = 0

                for article in articles:
                    uhash = url_hash(article["url"])

                    with conn.cursor() as cur:
                        if is_duplicate(cur, uhash):
                            skipped += 1
                            continue

                    time.sleep(1)
                    try:
                        classification = classify(client, article)
                    except Exception as e:
                        print(f"  [{display}] Classification error: {e}")
                        errors += 1
                        continue

                    try:
                        with conn.cursor() as cur:
                            save_signal(cur, article, classification)
                        conn.commit()
                        saved += 1
                    except Exception as e:
                        conn.rollback()
                        print(f"  [{display}] DB error: {e}")
                        errors += 1

                totals[source_type]["found"]   += len(articles)
                totals[source_type]["saved"]   += saved
                totals[source_type]["skipped"] += skipped
                totals[source_type]["errors"]  += errors
                print(
                    f"  {display:10s} -- fetched {len(articles):3d} | new {saved:3d}"
                    f" | skipped {skipped:3d} | errors {errors:2d}"
                )

            print()

    finally:
        conn.close()

    print("-" * 65)
    for source_type, t in totals.items():
        print(
            f"  {source_type:20s} -- found: {t['found']:3d}  saved: {t['saved']:3d}"
            f"  skipped: {t['skipped']:3d}  errors: {t['errors']:2d}"
        )
    print("-" * 65 + "\n")


if __name__ == "__main__":
    main()
