"""Daily invoices ETL. Owner: data team. Do not touch the retry logic."""

import csv
import io
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

import boto3
import pandas as pd
import requests
from openai import OpenAI
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------- config
API_URL = os.environ["VENDOR_API_URL"]
API_TOKEN = os.environ["VENDOR_API_TOKEN"]
DB_URL = os.environ["DATABASE_URL"]
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")
BUCKET = os.environ.get("RAW_BUCKET", "raw-invoices")
MAX_RETRIES = 5
PAGE_SIZE = 500
BATCH = 1000
MODEL = "gpt-4o-mini"
STATE_FILE = "/var/lib/etl/invoices_state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("/var/log/etl/invoices.log")],
)
log = logging.getLogger("invoices")
client = OpenAI()
s3 = boto3.client("s3")
engine = create_engine(DB_URL, pool_pre_ping=True)


# ---------------------------------------------------------------- state
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_run": None, "cache": {}}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- retries
def with_retries(fn, *args, **kwargs):
    delay = 1
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status < 500 and status != 429:
                raise
            log.warning("http %s, retry %s/%s in %ss", status, attempt, MAX_RETRIES, delay)
        except (requests.ConnectionError, requests.Timeout) as e:
            log.warning("network error %s, retry %s/%s", e, attempt, MAX_RETRIES)
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError(f"giving up after {MAX_RETRIES} attempts")


# ---------------------------------------------------------------- extract
def fetch_page(page, since):
    resp = requests.get(
        f"{API_URL}/invoices",
        params={"page": page, "per_page": PAGE_SIZE, "updated_since": since},
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_invoices(since):
    rows, page = [], 1
    while True:
        data = with_retries(fetch_page, page, since)
        rows.extend(data["items"])
        log.info("page %s: %s invoices", page, len(data["items"]))
        if not data.get("next_page"):
            break
        page += 1
    return rows


def fetch_customers():
    resp = with_retries(
        requests.get,
        f"{API_URL}/customers/export.csv",
        headers={"Authorization": f"Bearer {API_TOKEN}"},
        timeout=120,
    )
    resp.raise_for_status()
    sample = resp.text[:2048]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    return list(csv.DictReader(io.StringIO(resp.text), dialect=dialect))


def archive_raw(rows, day):
    key = f"invoices/{day:%Y/%m/%d}/raw.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(rows).encode())
    log.info("archived %s rows to s3://%s/%s", len(rows), BUCKET, key)


# ---------------------------------------------------------------- transform
def clean(invoices, customers):
    df = pd.DataFrame(invoices)
    if df.empty:
        return df
    df = df.dropna(subset=["number"]).drop_duplicates("number", keep="last")
    df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0).round(2)
    df["issued_at"] = pd.to_datetime(df["issued_at"], errors="coerce", utc=True)
    df["tax_id"] = df["tax_id"].astype(str).str.replace(r"\D", "", regex=True)
    cust = pd.DataFrame(customers)
    cust["tax_id"] = cust["tax_id"].astype(str).str.replace(r"\D", "", regex=True)
    df = df.merge(cust[["tax_id", "name", "segment"]], on="tax_id", how="left")
    df["customer_known"] = df["name"].notna()
    return df


def validate(df):
    bad = df[(df["total"] <= 0) | df["issued_at"].isna()]
    if len(bad):
        log.warning("%s invalid invoices dropped", len(bad))
    return df.drop(bad.index), bad


# ---------------------------------------------------------------- ai
PROMPT = """Classify the expense category of this invoice.
Answer only JSON: {"category": string, "needs_review": bool, "reason": string}.
Categories: software, travel, office, marketing, services, other.

Invoice: """


def classify_one(row, cache):
    key = f"{row['number']}:{row['description']}"
    if key in cache:
        return cache[key]
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": PROMPT + json.dumps(row, default=str)}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            data = json.loads(resp.choices[0].message.content)
            if data.get("category") not in {"software", "travel", "office", "marketing", "services", "other"}:
                raise ValueError(f"bad category {data.get('category')}")
            data["needs_review"] = bool(data.get("needs_review")) or not row["customer_known"]
            cache[key] = data
            return data
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            log.warning("invalid answer for %s (%s), attempt %s", row["number"], e, attempt + 1)
        except Exception as e:  # rate limits, timeouts
            log.warning("llm error %s, sleeping", e)
            time.sleep(2**attempt)
    return None


def classify(df, cache):
    out, rejected = [], []
    for _, row in df.iterrows():
        result = classify_one(row.to_dict(), cache)
        if result is None:
            rejected.append(row["number"])
            continue
        out.append({**row.to_dict(), **result})
    if df.shape[0] and len(rejected) / df.shape[0] > 0.05:
        notify(f":warning: {len(rejected)} invoices could not be classified")
    return pd.DataFrame(out), rejected


# ---------------------------------------------------------------- load
UPSERT = text("""
INSERT INTO invoices (number, tax_id, name, segment, total, issued_at, category, needs_review, reason)
VALUES (:number, :tax_id, :name, :segment, :total, :issued_at, :category, :needs_review, :reason)
ON CONFLICT (number) DO UPDATE SET
  total = EXCLUDED.total, category = EXCLUDED.category,
  needs_review = EXCLUDED.needs_review, reason = EXCLUDED.reason
""")


def load(df):
    rows = df.to_dict("records")
    with engine.begin() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS invoices (
              number TEXT PRIMARY KEY, tax_id TEXT, name TEXT, segment TEXT, total NUMERIC,
              issued_at TIMESTAMPTZ, category TEXT, needs_review BOOLEAN, reason TEXT)""")
        )
        for i in range(0, len(rows), BATCH):
            conn.execute(UPSERT, rows[i : i + BATCH])
    log.info("upserted %s invoices", len(rows))


# ---------------------------------------------------------------- notify
def notify(message):
    if not SLACK_WEBHOOK:
        return
    try:
        requests.post(SLACK_WEBHOOK, json={"text": message}, timeout=10)
    except Exception:
        log.exception("slack notification failed")


# ---------------------------------------------------------------- main
def main():
    started = time.time()
    state = load_state()
    since = state["last_run"] or (datetime.utcnow() - timedelta(days=1)).isoformat()
    try:
        invoices = fetch_invoices(since)
        if not invoices:
            log.info("nothing new since %s", since)
            return 0
        archive_raw(invoices, datetime.utcnow())
        customers = fetch_customers()
        df = clean(invoices, customers)
        df, invalid = validate(df)
        df, rejected = classify(df, state["cache"])
        load(df)
        state["last_run"] = datetime.utcnow().isoformat()
        save_state(state)
        notify(
            f":white_check_mark: invoices ETL: {len(df)} loaded, {len(rejected)} rejected, "
            f"{len(invalid)} invalid in {time.time() - started:.0f}s"
        )
        return 0
    except Exception as e:
        log.error("ETL failed: %s\n%s", e, traceback.format_exc())
        notify(f":rotating_light: invoices ETL failed: {e}")
        save_state(state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
