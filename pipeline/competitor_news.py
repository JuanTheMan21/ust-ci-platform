import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
import hashlib
import html
import json
import re
import time

import psycopg2
import requests
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

COMPETITORS = {
    "TCS":       "https://news.google.com/rss/search?q=TCS+Tata+Consultancy+Services&hl=en-US&gl=US&ceid=US:en",
    "Wipro":     "https://news.google.com/rss/search?q=Wipro&hl=en-US&gl=US&ceid=US:en",
    "HCL":       "https://news.google.com/rss/search?q=HCL+Technologies&hl=en-US&gl=US&ceid=US:en",
    "Infosys":   "https://news.google.com/rss/search?q=Infosys&hl=en-US&gl=US&ceid=US:en",
    "Accenture": "https://news.google.com/rss/search?q=Accenture&hl=en-US&gl=US&ceid=US:en",
}

BSE_CODES = {
    "TCS":     "532540",
    "Infosys": "500209",
    "Wipro":   "507685",
    "HCL":     "532281",
}

PATENT_ASSIGNEES = {
    "TCS":       "Tata Consultancy",
    "Infosys":   "Infosys",
    "Wipro":     "Wipro",
    "Accenture": "Accenture",
    "HCL":       "HCL Technologies",
}

SYSTEM_PROMPT = UST_PROFILE

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def parse_date(raw):
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%d",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y",
    ):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def url_hash(url):
    return hashlib.sha256(url.encode()).hexdigest()


def fetch_articles(competitor, feed_url, source_type='news'):
    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
    except Exception as e:
        print(f"  [{competitor}] Feed error: {e}")
        return []

    if source_type == 'sec_filing':
        try:
            payload = json.loads(data)
            hits = payload.get("hits", {}).get("hits", [])
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"  [{competitor}] SEC JSON parse error: {e}")
            return []
        articles = []
        for hit in hits:
            src = hit.get("_source", {})
            display_names = src.get("display_names", "")
            if "Accenture" not in display_names:
                continue
            form_type = src.get("form_type", "8-K")
            period    = src.get("period_of_report", "")
            title = (
                f"SEC {form_type}: {display_names} — period {period}"
                if period else
                f"SEC {form_type}: {display_names}"
            )
            url = f"https://efts.sec.gov/LATEST/search-index?q={hit['_id']}"
            pub = parse_date(src.get("file_date", ""))
            articles.append({
                "competitor": competitor,
                "title": title,
                "url": url,
                "description": f"{form_type} filing — period of report: {period}",
                "published_date": pub,
                "source_type": source_type,
            })
        return articles[:10]

    # RSS path (news and bse_filing)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"  [{competitor}] XML parse error: {e}")
        return []

    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")

    articles = []
    for item in items:
        title = strip_html(item.findtext("title", ""))
        link  = strip_html(item.findtext("link", ""))
        desc  = strip_html(item.findtext("description", ""))
        pub   = parse_date(item.findtext("pubDate", ""))
        if title and link:
            articles.append({
                "competitor": competitor,
                "title": title,
                "url": link,
                "description": desc,
                "published_date": pub,
                "source_type": source_type,
            })
    return articles[:10]


def fetch_bse_announcements(competitor: str, scrip_code: str) -> list:
    import tempfile
    from datetime import datetime, timedelta
    try:
        from bse import BSE as BseClient
    except ImportError:
        print(f"  [{competitor}] bse library not installed — run: pip install bse")
        return []
    articles = []
    try:
        with BseClient(download_folder=tempfile.gettempdir()) as bse_client:
            from_dt = datetime.now() - timedelta(days=30)
            to_dt = datetime.now()
            data = bse_client.announcements(
                scripcode=scrip_code,
                from_date=from_dt,
                to_date=to_dt,
            )
            rows = data.get("Table", [])[:5]
            for item in rows:
                headline = item.get("HEADLINE") or item.get("SUBCATNAME") or ""
                attachment = item.get("ATTACHMENTNAME", "")
                url = (
                    f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{attachment}"
                    if attachment
                    else "https://www.bseindia.com"
                )
                pub_date = item.get("NEWS_DT", "")
                if headline:
                    articles.append({
                        "competitor": competitor,
                        "title": f"BSE Filing: {headline}",
                        "url": url,
                        "description": headline,
                        "published_date": pub_date,
                        "source_type": "bse_filing",
                    })
    except Exception as e:
        print(f"  [{competitor}] BSE error: {e}")
    return articles


def fetch_sec_filings_accenture() -> list:
    url = "https://data.sec.gov/submissions/CIK0001467373.json"
    headers = {"User-Agent": "UST-CI-Tool contact@ust.com"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [Accenture] SEC EDGAR error: {e}")
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    descriptions = recent.get("primaryDocDescription", [])
    articles = []
    for i, form in enumerate(forms):
        if form in ("8-K", "6-K", "DEF 14A") and len(articles) < 5:
            filing_url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                f"&CIK=0001467373&type={form}&dateb=&owner=include&count=10"
            )
            desc = descriptions[i] if i < len(descriptions) and descriptions[i] else f"Accenture {form} filing dated {dates[i]}"
            articles.append({
                "competitor": "Accenture",
                "title": f"SEC {form}: Accenture — {dates[i]}",
                "url": filing_url,
                "description": desc,
                "published_date": dates[i],
                "source_type": "sec_filing",
            })
    return articles


def fetch_patents(competitor: str, assignee: str) -> list:
    """
    Patent fetching is temporarily disabled.
    PatentsView legacy API was discontinued May 2025.
    New API registration portal (Atlassian) is currently broken.
    USPTO ODP API requires government ID verification — not suitable for POC.
    Add back in Phase 2 once a working key is obtained.
    """
    print(f"  [{competitor}] Patents: skipped (API registration unavailable — Phase 2)")
    return []


def is_duplicate(cur, uhash):
    cur.execute("SELECT 1 FROM signals WHERE url_hash = %s", (uhash,))
    return cur.fetchone() is not None


def is_duplicate_story(cursor, competitor, title, threshold=0.4):
    stopwords = {
        'the','a','an','in','on','at','to','for','of','and','or','is','was',
        'its','it','by','with','as','from','that','this','be','are','were',
        'has','have','had','will','after','-','|','&',
    }

    new_words = set(title.lower().split()) - stopwords
    if not new_words:
        return False

    cursor.execute(
        """
        SELECT title FROM signals
        WHERE competitor = %s
        AND created_at > NOW() - INTERVAL '7 days'
        """,
        (competitor,),
    )

    for (existing_title,) in cursor.fetchall():
        existing_words = set(existing_title.lower().split()) - stopwords
        if not existing_words:
            continue
        shared = len(new_words & existing_words)
        total  = len(new_words | existing_words)
        if total > 0 and shared / total >= threshold:
            return True
    return False


def classify(client, article):
    user_msg = f"Title: {article['title']}\nDescription: {article['description']}"
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=AZURE_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
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
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def save_signal(cur, article, classification):
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
            classification["significance"],
            classification["ust_relevance"],
            classification["raw_summary"],
            url_hash(article["url"]),
            article.get("source_type", "news"),
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    conn   = psycopg2.connect(DB_DSN)
    client = OpenAI(api_key=AZURE_API_KEY, base_url=AZURE_ENDPOINT)

    total_found = total_saved = total_skipped = total_story_dupes = 0
    filing_found = filing_saved = filing_skipped = 0
    patent_found = patent_saved = patent_skipped = 0

    try:
        print(f"\nCompetitor Intelligence Fetcher — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        for competitor, feed_url in COMPETITORS.items():
            articles = fetch_articles(competitor, feed_url, source_type='news')
            saved = skipped = story_dupes = errors = 0

            for article in articles:
                uhash = url_hash(article["url"])

                with conn.cursor() as cur:
                    if is_duplicate(cur, uhash):
                        skipped += 1
                        continue

                    if is_duplicate_story(cur, competitor, article["title"]):
                        print(f"  [{competitor}] Duplicate story skipped: {article['title'][:60]}")
                        story_dupes += 1
                        continue

                time.sleep(2)
                try:
                    classification = classify(client, article)
                except Exception as e:
                    print(f"  [{competitor}] Classification error for '{article['title'][:60]}': {e}")
                    errors += 1
                    continue

                try:
                    with conn.cursor() as cur:
                        save_signal(cur, article, classification)
                    conn.commit()
                    saved += 1
                except Exception as e:
                    conn.rollback()
                    print(f"  [{competitor}] DB error for '{article['title'][:60]}': {e}")
                    errors += 1

            total_found        += len(articles)
            total_saved        += saved
            total_skipped      += skipped
            total_story_dupes  += story_dupes
            print(
                f"  {competitor:10s} — fetched {len(articles):3d} | new {saved:3d}"
                f" | skipped {skipped:3d} | story dupes {story_dupes:3d} | errors {errors:2d}"
            )

        print(f"\n-- Regulatory Filings --\n")

        for competitor, scrip_code in BSE_CODES.items():
            articles = fetch_bse_announcements(competitor, scrip_code)
            saved = skipped = errors = 0

            for article in articles:
                uhash = url_hash(article["url"])

                with conn.cursor() as cur:
                    if is_duplicate(cur, uhash):
                        skipped += 1
                        continue

                time.sleep(2)
                try:
                    classification = classify(client, article)
                except Exception as e:
                    print(f"  [BSE][{competitor}] Classification error for '{article['title'][:60]}': {e}")
                    errors += 1
                    continue

                try:
                    with conn.cursor() as cur:
                        save_signal(cur, article, classification)
                    conn.commit()
                    saved += 1
                except Exception as e:
                    conn.rollback()
                    print(f"  [BSE][{competitor}] DB error for '{article['title'][:60]}': {e}")
                    errors += 1

            filing_found   += len(articles)
            filing_saved   += saved
            filing_skipped += skipped
            print(
                f"  [BSE]    {competitor:10s} — fetched {len(articles):3d} | new {saved:3d}"
                f" | skipped {skipped:3d} | errors {errors:2d}"
            )

        articles = fetch_sec_filings_accenture()
        saved = skipped = errors = 0

        for article in articles:
            uhash = url_hash(article["url"])

            with conn.cursor() as cur:
                if is_duplicate(cur, uhash):
                    skipped += 1
                    continue

            time.sleep(2)
            try:
                classification = classify(client, article)
            except Exception as e:
                print(f"  [SEC][Accenture] Classification error for '{article['title'][:60]}': {e}")
                errors += 1
                continue

            try:
                with conn.cursor() as cur:
                    save_signal(cur, article, classification)
                conn.commit()
                saved += 1
            except Exception as e:
                conn.rollback()
                print(f"  [SEC][Accenture] DB error for '{article['title'][:60]}': {e}")
                errors += 1

        filing_found   += len(articles)
        filing_saved   += saved
        filing_skipped += skipped
        print(
            f"  [SEC]    {'Accenture':10s} — fetched {len(articles):3d} | new {saved:3d}"
            f" | skipped {skipped:3d} | errors {errors:2d}"
        )

        print(f"\n-- Patent Filings --\n")

        for competitor, assignee in PATENT_ASSIGNEES.items():
            articles = fetch_patents(competitor, assignee)
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
                    print(f"  [PATENT][{competitor}] Classification error for '{article['title'][:60]}': {e}")
                    errors += 1
                    continue

                try:
                    with conn.cursor() as cur:
                        save_signal(cur, article, classification)
                    conn.commit()
                    saved += 1
                except Exception as e:
                    conn.rollback()
                    print(f"  [PATENT][{competitor}] DB error for '{article['title'][:60]}': {e}")
                    errors += 1

            patent_found   += len(articles)
            patent_saved   += saved
            patent_skipped += skipped
            print(
                f"  [PATENT] {competitor:10s} — fetched {len(articles):3d} | new {saved:3d}"
                f" | skipped {skipped:3d} | errors {errors:2d}"
            )

    finally:
        conn.close()

    print(f"\n{'-'*65}")
    print(
        f"  News    — found: {total_found}  |  saved: {total_saved}"
        f"  |  skipped: {total_skipped}  |  story dupes: {total_story_dupes}"
    )
    print(
        f"  Filings — found: {filing_found}  |  saved: {filing_saved}"
        f"  |  skipped: {filing_skipped}"
    )
    print(
        f"  Patents — found: {patent_found}  |  saved: {patent_saved}"
        f"  |  skipped: {patent_skipped}"
    )
    print(f"{'-'*65}\n")


if __name__ == "__main__":
    main()
