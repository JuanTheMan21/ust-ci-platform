# UST Competitive Intelligence
AI-powered competitor monitoring for the CTO office.
Tracks TCS, Infosys, Wipro, Accenture, HCL across
news, partnerships, acquisitions, hiring, and legal signals.

## Project structure
```
├── pipeline/
│   └── competitor_news.py   # RSS fetch, classify, persist
├── dashboard/
│   └── app.py               # Streamlit dashboard
├── data/
│   └── ust_context.py       # UST_PROFILE constant shared across modules
└── requirements.txt
```

## Setup
```
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your credentials
```

## Running the pipeline
Run from the **project root** (not from inside `pipeline/`):
```
python pipeline/competitor_news.py
```

## Running the dashboard
Also from the **project root**:
```
streamlit run dashboard/app.py
```

## What it does
Fetches latest news for 5 competitors via Google News RSS,
classifies each article using GPT-4o (category, significance
1-5, UST strategic relevance), deduplicates by URL and story,
and saves to PostgreSQL (Neon).
