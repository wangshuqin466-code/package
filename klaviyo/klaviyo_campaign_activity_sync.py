import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.exceptions import ChunkedEncodingError, ConnectionError, ReadTimeout


API_BASE = "https://a.klaviyo.com/api"
REVISION = "2026-04-15"

STATUS_METRICS = {
    "送达": ["Received Email", "Delivered Email"],
    "打开": ["Opened Email"],
    "点击": ["Clicked Email"],
    "转化": ["Placed Order"],
    "退信": ["Bounced Email"],
    "退订": ["Unsubscribed", "Unsubscribed from List"],
    "标记为垃圾邮件": ["Marked Email as Spam"],
    "跳过": ["Skipped Email", "Dropped Email"],
}

STATUS_EN = {status: metric_names[0] for status, metric_names in STATUS_METRICS.items()}


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_dt(value):
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def get_json(session, url, params=None, retries=3):
    for attempt in range(retries):
        try:
            response = session.get(url, params=params, timeout=45)
        except (ChunkedEncodingError, ConnectionError, ReadTimeout):
            if attempt + 1 >= retries:
                raise
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < retries:
            wait = int(response.headers.get("Retry-After", "2"))
            try:
                wait = max(wait, int(response.json().get("retry_after", wait)))
            except ValueError:
                pass
            time.sleep(wait)
            continue
        response.raise_for_status()
        return response.json()
    response.raise_for_status()


def make_session(api_key):
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Klaviyo-API-Key {api_key}",
            "revision": REVISION,
            "accept": "application/vnd.api+json",
        }
    )
    return session


def iter_pages(session, url, params=None, max_pages=None):
    pages = 0
    while url:
        payload = get_json(session, url, params=params)
        yield payload
        pages += 1
        if max_pages and pages >= max_pages:
            break
        next_url = payload.get("links", {}).get("next")
        url = next_url
        params = None


def discover_metrics(session):
    found = {}
    wanted = {name for names in STATUS_METRICS.values() for name in names}
    for page in iter_pages(session, f"{API_BASE}/metrics/", max_pages=20):
        for metric in page.get("data", []):
            name = metric.get("attributes", {}).get("name")
            integration = metric.get("attributes", {}).get("integration", {}).get("name")
            if name in wanted and (integration in (None, "Klaviyo", "Shopify") or name == "Placed Order"):
                found[name] = metric["id"]
    return found


def fetch_campaigns(session, limit):
    params = {
        "filter": "equals(messages.channel,'email')",
        "sort": "-updated_at",
        "page[size]": 100 if not limit else min(max(limit * 5, limit), 100),
    }
    campaigns = []
    max_pages = None if not limit else 3
    for page in iter_pages(session, f"{API_BASE}/campaigns/", params=params, max_pages=max_pages):
        for item in page.get("data", []):
            attrs = item.get("attributes", {})
            if attrs.get("status") == "Draft":
                continue
            campaigns.append(
                {
                    "id": item["id"],
                    "name": attrs.get("name"),
                    "status": attrs.get("status"),
                    "send_time": attrs.get("send_time"),
                    "updated_at": attrs.get("updated_at"),
                }
            )
            if limit and len(campaigns) >= limit:
                return campaigns
    return campaigns


def profile_map(payload):
    profiles = {}
    for item in payload.get("included", []) or []:
        if item.get("type") != "profile":
            continue
        attrs = item.get("attributes") or {}
        profiles[item.get("id")] = {
            "email": attrs.get("email"),
            "first_name": attrs.get("first_name"),
            "last_name": attrs.get("last_name"),
        }
    return profiles


def contains_campaign(value, campaign_id, campaign_name):
    if value is None:
        return False
    if isinstance(value, str):
        return value == campaign_id or value == campaign_name
    if isinstance(value, dict):
        return any(contains_campaign(v, campaign_id, campaign_name) for v in value.values())
    if isinstance(value, list):
        return any(contains_campaign(v, campaign_id, campaign_name) for v in value)
    return False


def event_matches_campaign(props, campaign):
    if props.get("$campaign") == campaign["id"]:
        return True
    if props.get("Campaign Name") == campaign["name"]:
        return True
    if props.get("Message Name") == campaign["name"]:
        return True
    return False


def event_email(props, profile):
    return (
        (profile or {}).get("email")
        or props.get("Recipient Email Address")
        or props.get("$email")
        or props.get("email")
        or props.get("$originating_email")
        or props.get("Email")
    )


def fetch_activity(session, campaigns, metric_ids, page_size, max_pages_per_metric):
    campaign_by_id = {campaign["id"]: campaign for campaign in campaigns}
    earliest = min((parse_dt(c["send_time"]) for c in campaigns if c.get("send_time")), default=None)
    if earliest:
        earliest = earliest - timedelta(days=1)

    aggregates = {}
    counts = defaultdict(int)
    last_action = {}

    for status, metric_names in STATUS_METRICS.items():
        for metric_name in metric_names:
            metric_id = metric_ids.get(metric_name)
            if not metric_id:
                continue
            filters = [f"equals(metric_id,'{metric_id}')"]
            if earliest:
                filters.append(f"greater-or-equal(datetime,{earliest.isoformat()})")
            params = {
                "filter": ",".join(filters),
                "include": "profile",
                "sort": "-datetime",
                "page[size]": page_size,
            }
            for page in iter_pages(session, f"{API_BASE}/events/", params=params, max_pages=max_pages_per_metric):
                profiles = profile_map(page)
                for event in page.get("data", []):
                    attrs = event.get("attributes") or {}
                    props = attrs.get("event_properties") or {}
                    event_dt = parse_dt(attrs.get("datetime"))
                    if earliest and event_dt and event_dt < earliest:
                        continue
                    matched_campaign = None
                    for campaign in campaigns:
                        if event_matches_campaign(props, campaign):
                            matched_campaign = campaign
                            break
                    if not matched_campaign:
                        continue
                    profile_id = ((event.get("relationships") or {}).get("profile") or {}).get("data", {}).get("id")
                    profile = profiles.get(profile_id, {})
                    email = event_email(props, profile)
                    if not email:
                        continue
                    key_base = (matched_campaign["id"], email.lower())
                    counts[(key_base, status)] += 1
                    last = last_action.get(key_base)
                    if not last or (event_dt and event_dt > last[0]):
                        last_action[key_base] = (event_dt, status, STATUS_EN.get(status))
                    key = (matched_campaign["id"], email.lower(), status)
                    current = aggregates.get(key)
                    if not current:
                        aggregates[key] = {
                            "campaign_id": matched_campaign["id"],
                            "campaign": matched_campaign["name"],
                            "campaign_send_time": matched_campaign.get("send_time"),
                            "email": email,
                            "first_name": profile.get("first_name"),
                            "last_name": profile.get("last_name"),
                            "profile_id": profile_id,
                            "status": status,
                            "last_event_datetime": attrs.get("datetime"),
                        }
                    elif event_dt and parse_dt(current["last_event_datetime"]) < event_dt:
                        current["last_event_datetime"] = attrs.get("datetime")

    rows = []
    for key, row in aggregates.items():
        base = (key[0], key[1])
        row["opens"] = counts.get((base, "打开"), 0)
        row["clicks"] = counts.get((base, "点击"), 0)
        row["conversions"] = counts.get((base, "转化"), 0)
        row["event_count"] = counts.get((base, row["status"]), 0)
        row["last_action"] = (last_action.get(base) or (None, None))[1]
        row["last_action_en"] = (last_action.get(base) or (None, None, None))[2]
        row["status_en"] = STATUS_EN.get(row["status"])
        rows.append(row)
    rows.sort(key=lambda r: (r["campaign"], r["email"], r["status"]))
    return rows


def connect_db():
    db_type = os.getenv("DB_TYPE", "sqlite").lower()
    if db_type == "mysql":
        import pymysql

        conn = pymysql.connect(
            host=os.environ["DB_HOST"],
            port=int(os.getenv("DB_PORT", "3306")),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            database=os.environ["DB_NAME"],
            charset="utf8mb4",
            autocommit=False,
        )
        return conn, "mysql"
    path = Path(os.getenv("SQLITE_PATH", r"C:\tmp\google_trends_wedding_formal.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    return conn, "sqlite"


def execute(conn, db_type, sql, values=None):
    if db_type == "mysql":
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, values or [])
    return cur


def create_table(conn, db_type):
    if db_type == "mysql":
        execute(
            conn,
            db_type,
            """
            CREATE TABLE IF NOT EXISTS klaviyo_campaign_recipient_activity (
                campaign_id VARCHAR(64) NOT NULL,
                campaign VARCHAR(255) NOT NULL,
                campaign_send_time DATETIME NULL,
                email VARCHAR(255) NOT NULL,
                first_name VARCHAR(255) NULL,
                last_name VARCHAR(255) NULL,
                last_action VARCHAR(64) NULL,
                last_action_en VARCHAR(128) NULL,
                opens INT NOT NULL DEFAULT 0,
                clicks INT NOT NULL DEFAULT 0,
                conversions INT NOT NULL DEFAULT 0,
                status VARCHAR(64) NOT NULL,
                status_en VARCHAR(128) NULL,
                event_count INT NOT NULL DEFAULT 0,
                profile_id VARCHAR(64) NULL,
                last_event_datetime DATETIME NULL,
                source VARCHAR(64) NOT NULL DEFAULT 'klaviyo_api',
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (campaign_id, email, status)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """,
        )
    else:
        execute(
            conn,
            db_type,
            """
            CREATE TABLE IF NOT EXISTS klaviyo_campaign_recipient_activity (
                campaign_id TEXT NOT NULL,
                campaign TEXT NOT NULL,
                campaign_send_time TEXT,
                email TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                last_action TEXT,
                last_action_en TEXT,
                opens INTEGER NOT NULL DEFAULT 0,
                clicks INTEGER NOT NULL DEFAULT 0,
                conversions INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                status_en TEXT,
                event_count INTEGER NOT NULL DEFAULT 0,
                profile_id TEXT,
                last_event_datetime TEXT,
                source TEXT NOT NULL DEFAULT 'klaviyo_api',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (campaign_id, email, status)
            )
            """,
        )


def create_raw_table(conn, db_type):
    if db_type == "mysql":
        execute(
            conn,
            db_type,
            """
            CREATE TABLE IF NOT EXISTS klaviyo_campaign_recipient_events (
                event_id VARCHAR(128) NOT NULL PRIMARY KEY,
                campaign_id VARCHAR(64) NOT NULL,
                campaign VARCHAR(255) NOT NULL,
                campaign_send_time DATETIME NULL,
                email VARCHAR(255) NOT NULL,
                first_name VARCHAR(255) NULL,
                last_name VARCHAR(255) NULL,
                profile_id VARCHAR(64) NULL,
                status VARCHAR(64) NOT NULL,
                status_en VARCHAR(128) NULL,
                metric_name VARCHAR(128) NOT NULL,
                metric_id VARCHAR(64) NOT NULL,
                event_datetime DATETIME NOT NULL,
                event_properties_json JSON NULL,
                updated_at DATETIME NOT NULL,
                KEY idx_campaign_email (campaign_id, email),
                KEY idx_event_datetime (event_datetime),
                KEY idx_status (status)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """,
        )
    else:
        execute(
            conn,
            db_type,
            """
            CREATE TABLE IF NOT EXISTS klaviyo_campaign_recipient_events (
                event_id TEXT NOT NULL PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                campaign TEXT NOT NULL,
                campaign_send_time TEXT,
                email TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                profile_id TEXT,
                status TEXT NOT NULL,
                status_en TEXT,
                metric_name TEXT NOT NULL,
                metric_id TEXT NOT NULL,
                event_datetime TEXT NOT NULL,
                event_properties_json TEXT,
                updated_at TEXT NOT NULL
            )
            """,
        )


def column_exists(conn, db_type, table_name, column_name):
    if db_type == "mysql":
        cur = execute(
            conn,
            db_type,
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = ?
              AND column_name = ?
            """,
            [table_name, column_name],
        )
        return cur.fetchone()[0] > 0
    cur = execute(conn, db_type, f"PRAGMA table_info({table_name})")
    return any(row[1] == column_name for row in cur.fetchall())


def ensure_columns(conn, db_type):
    additions = [
        ("last_action_en", "VARCHAR(128) NULL" if db_type == "mysql" else "TEXT"),
        ("status_en", "VARCHAR(128) NULL" if db_type == "mysql" else "TEXT"),
    ]
    for column_name, column_type in additions:
        if not column_exists(conn, db_type, "klaviyo_campaign_recipient_activity", column_name):
            execute(
                conn,
                db_type,
                f"ALTER TABLE klaviyo_campaign_recipient_activity ADD COLUMN {column_name} {column_type}",
            )


def campaign_match(props, campaigns):
    campaign_id = props.get("$campaign")
    if campaign_id and campaign_id in campaigns["by_id"]:
        return campaigns["by_id"][campaign_id]
    campaign_name = props.get("Campaign Name")
    if campaign_name and campaign_name in campaigns["by_name"]:
        return campaigns["by_name"][campaign_name]
    message_name = props.get("Message Name")
    if message_name and message_name in campaigns["by_name"]:
        return campaigns["by_name"][message_name]
    return None


def campaign_lookup(campaigns):
    by_name = {}
    for campaign in campaigns:
        if campaign.get("name") and campaign["name"] not in by_name:
            by_name[campaign["name"]] = campaign
    return {"items": campaigns, "by_id": {c["id"]: c for c in campaigns}, "by_name": by_name}


def date_windows(start_dt, end_dt, days):
    current = start_dt
    delta = timedelta(days=days)
    while current < end_dt:
        window_end = min(current + delta, end_dt)
        yield current, window_end
        current = window_end


def fetch_window_events(session, campaigns, metric_ids, start_dt, end_dt, page_size, max_pages_per_metric):
    lookup = campaign_lookup(campaigns)
    rows = []
    for status, metric_names in STATUS_METRICS.items():
        for metric_name in metric_names:
            metric_id = metric_ids.get(metric_name)
            if not metric_id:
                continue
            filters = [
                f"equals(metric_id,'{metric_id}')",
                f"greater-or-equal(datetime,{start_dt.isoformat()})",
                f"less-than(datetime,{end_dt.isoformat()})",
            ]
            params = {
                "filter": ",".join(filters),
                "include": "profile",
                "sort": "-datetime",
                "page[size]": page_size,
            }
            for page in iter_pages(session, f"{API_BASE}/events/", params=params, max_pages=max_pages_per_metric):
                profiles = profile_map(page)
                for event in page.get("data", []):
                    attrs = event.get("attributes") or {}
                    props = attrs.get("event_properties") or {}
                    campaign = campaign_match(props, lookup)
                    if not campaign:
                        continue
                    profile_id = ((event.get("relationships") or {}).get("profile") or {}).get("data", {}).get("id")
                    profile = profiles.get(profile_id, {})
                    email = event_email(props, profile)
                    event_dt = attrs.get("datetime")
                    if not email or not event_dt:
                        continue
                    rows.append(
                        {
                            "event_id": event["id"],
                            "campaign_id": campaign["id"],
                            "campaign": campaign["name"],
                            "campaign_send_time": campaign.get("send_time"),
                            "email": email,
                            "first_name": profile.get("first_name"),
                            "last_name": profile.get("last_name"),
                            "profile_id": profile_id,
                            "status": status,
                            "status_en": STATUS_EN.get(status),
                            "metric_name": metric_name,
                            "metric_id": metric_id,
                            "event_datetime": event_dt,
                            "event_properties_json": json.dumps(props, ensure_ascii=False, default=str),
                        }
                    )
    return rows


def rows_from_event_page(page, lookup, status, status_en, metric_name, metric_id):
    rows = []
    profiles = profile_map(page)
    for event in page.get("data", []):
        attrs = event.get("attributes") or {}
        props = attrs.get("event_properties") or {}
        campaign = campaign_match(props, lookup)
        if not campaign:
            continue
        profile_id = ((event.get("relationships") or {}).get("profile") or {}).get("data", {}).get("id")
        profile = profiles.get(profile_id, {})
        email = event_email(props, profile)
        event_dt = attrs.get("datetime")
        if not email or not event_dt:
            continue
        rows.append(
            {
                "event_id": event["id"],
                "campaign_id": campaign["id"],
                "campaign": campaign["name"],
                "campaign_send_time": campaign.get("send_time"),
                "email": email,
                "first_name": profile.get("first_name"),
                "last_name": profile.get("last_name"),
                "profile_id": profile_id,
                "status": status,
                "status_en": status_en,
                "metric_name": metric_name,
                "metric_id": metric_id,
                "event_datetime": event_dt,
                "event_properties_json": json.dumps(props, ensure_ascii=False, default=str),
            }
        )
    return rows


def upsert_raw_events(conn, db_type, rows):
    if not rows:
        return 0
    now = iso_now()
    if db_type == "mysql":
        sql = """
            INSERT INTO klaviyo_campaign_recipient_events
            (event_id, campaign_id, campaign, campaign_send_time, email, first_name, last_name,
             profile_id, status, status_en, metric_name, metric_id, event_datetime, event_properties_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                campaign = VALUES(campaign),
                campaign_send_time = VALUES(campaign_send_time),
                email = VALUES(email),
                first_name = COALESCE(VALUES(first_name), first_name),
                last_name = COALESCE(VALUES(last_name), last_name),
                profile_id = VALUES(profile_id),
                status = VALUES(status),
                status_en = VALUES(status_en),
                metric_name = VALUES(metric_name),
                metric_id = VALUES(metric_id),
                event_datetime = VALUES(event_datetime),
                updated_at = VALUES(updated_at)
        """
    else:
        sql = """
            INSERT INTO klaviyo_campaign_recipient_events
            (event_id, campaign_id, campaign, campaign_send_time, email, first_name, last_name,
             profile_id, status, status_en, metric_name, metric_id, event_datetime, event_properties_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                campaign = excluded.campaign,
                campaign_send_time = excluded.campaign_send_time,
                email = excluded.email,
                first_name = COALESCE(excluded.first_name, first_name),
                last_name = COALESCE(excluded.last_name, last_name),
                profile_id = excluded.profile_id,
                status = excluded.status,
                status_en = excluded.status_en,
                metric_name = excluded.metric_name,
                metric_id = excluded.metric_id,
                event_datetime = excluded.event_datetime,
                updated_at = excluded.updated_at
        """
    if db_type == "mysql":
        sql = sql.replace("?", "%s")
    values = [
        (
            r["event_id"],
            r["campaign_id"],
            r["campaign"],
            normalize_db_dt(r["campaign_send_time"], db_type),
            r["email"],
            r["first_name"],
            r["last_name"],
            r["profile_id"],
            r["status"],
            r["status_en"],
            r["metric_name"],
            r["metric_id"],
            normalize_db_dt(r["event_datetime"], db_type),
            r["event_properties_json"],
            normalize_db_dt(now, db_type),
        )
        for r in rows
    ]
    for start in range(0, len(values), 100):
        chunk = values[start : start + 100]
        for attempt in range(3):
            try:
                if hasattr(conn, "ping"):
                    conn.ping(reconnect=True)
                cur = conn.cursor()
                cur.executemany(sql, chunk)
                conn.commit()
                break
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    if hasattr(conn, "ping"):
                        conn.ping(reconnect=True)
                except Exception:
                    pass
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
    return len(rows)


def aggregate_raw_to_activity(conn, db_type):
    if db_type != "mysql":
        return
    sql = """
        INSERT INTO klaviyo_campaign_recipient_activity
        (campaign_id, campaign, campaign_send_time, email, first_name, last_name, last_action, last_action_en,
         opens, clicks, conversions, status, status_en, event_count, profile_id, last_event_datetime, updated_at)
        SELECT
            s.campaign_id,
            MAX(s.campaign) AS campaign,
            MAX(s.campaign_send_time) AS campaign_send_time,
            s.email,
            MAX(s.first_name) AS first_name,
            MAX(s.last_name) AS last_name,
            MAX(l.status) AS last_action,
            MAX(l.status_en) AS last_action_en,
            MAX(c.opens) AS opens,
            MAX(c.clicks) AS clicks,
            MAX(c.conversions) AS conversions,
            s.status,
            MAX(s.status_en) AS status_en,
            COUNT(*) AS event_count,
            MAX(s.profile_id) AS profile_id,
            MAX(s.event_datetime) AS last_event_datetime,
            UTC_TIMESTAMP() AS updated_at
        FROM klaviyo_campaign_recipient_events s
        JOIN (
            SELECT
                campaign_id,
                email,
                SUM(CASE WHEN status = '打开' THEN 1 ELSE 0 END) AS opens,
                SUM(CASE WHEN status = '点击' THEN 1 ELSE 0 END) AS clicks,
                SUM(CASE WHEN status = '转化' THEN 1 ELSE 0 END) AS conversions
            FROM klaviyo_campaign_recipient_events
            GROUP BY campaign_id, email
        ) c ON c.campaign_id = s.campaign_id AND c.email = s.email
        JOIN (
            SELECT e.campaign_id, e.email, MAX(e.status) AS status, MAX(e.status_en) AS status_en
            FROM klaviyo_campaign_recipient_events e
            JOIN (
                SELECT campaign_id, email, MAX(event_datetime) AS max_event_datetime
                FROM klaviyo_campaign_recipient_events
                GROUP BY campaign_id, email
            ) m ON m.campaign_id = e.campaign_id
               AND m.email = e.email
               AND m.max_event_datetime = e.event_datetime
            GROUP BY e.campaign_id, e.email
        ) l ON l.campaign_id = s.campaign_id AND l.email = s.email
        GROUP BY s.campaign_id, s.email, s.status
        ON DUPLICATE KEY UPDATE
            campaign = VALUES(campaign),
            campaign_send_time = VALUES(campaign_send_time),
            first_name = VALUES(first_name),
            last_name = VALUES(last_name),
            last_action = VALUES(last_action),
            last_action_en = VALUES(last_action_en),
            opens = VALUES(opens),
            clicks = VALUES(clicks),
            conversions = VALUES(conversions),
            status_en = VALUES(status_en),
            event_count = VALUES(event_count),
            profile_id = VALUES(profile_id),
            last_event_datetime = VALUES(last_event_datetime),
            updated_at = VALUES(updated_at)
        """
    for attempt in range(3):
        try:
            if hasattr(conn, "ping"):
                conn.ping(reconnect=True)
            execute(conn, db_type, sql)
            conn.commit()
            break
        except Exception:
            conn.rollback()
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))


def sync_windows(
    session,
    conn,
    db_type,
    campaigns,
    metric_ids,
    start_dt,
    end_dt,
    window_days,
    page_size,
    max_pages_per_metric,
    include_conversions,
):
    total_raw = 0
    lookup = campaign_lookup(campaigns)
    for window_start, window_end in date_windows(start_dt, end_dt, window_days):
        for status, metric_names in STATUS_METRICS.items():
            for metric_name in metric_names:
                if metric_name == "Placed Order" and not include_conversions:
                    print(
                        json.dumps(
                            {
                                "window_start": window_start.isoformat(),
                                "window_end": window_end.isoformat(),
                                "status": status,
                                "status_en": STATUS_EN.get(status),
                                "metric_name": metric_name,
                                "skipped": "Placed Order events do not expose reliable campaign recipient attribution",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    continue
                metric_id = metric_ids.get(metric_name)
                if not metric_id:
                    continue
                filters = [
                    f"equals(metric_id,'{metric_id}')",
                    f"greater-or-equal(datetime,{window_start.isoformat()})",
                    f"less-than(datetime,{window_end.isoformat()})",
                ]
                params = {
                    "filter": ",".join(filters),
                    "include": "profile",
                    "sort": "-datetime",
                    "page[size]": page_size,
                }
                page_no = 0
                for page in iter_pages(session, f"{API_BASE}/events/", params=params, max_pages=max_pages_per_metric):
                    page_no += 1
                    rows = rows_from_event_page(page, lookup, status, STATUS_EN.get(status), metric_name, metric_id)
                    written = upsert_raw_events(conn, db_type, rows)
                    total_raw += written
                    print(
                        json.dumps(
                            {
                                "window_start": window_start.isoformat(),
                                "window_end": window_end.isoformat(),
                                "status": status,
                                "status_en": STATUS_EN.get(status),
                                "metric_name": metric_name,
                                "page": page_no,
                                "matched_raw_rows": written,
                                "total_raw_rows_seen": total_raw,
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
    aggregate_raw_to_activity(conn, db_type)
    return total_raw


def normalize_db_dt(value, db_type):
    if not value:
        return None
    dt = parse_dt(value)
    if db_type == "mysql":
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat()


def upsert_rows(conn, db_type, rows):
    now = iso_now()
    if db_type == "mysql":
        sql = """
            INSERT INTO klaviyo_campaign_recipient_activity
            (campaign_id, campaign, campaign_send_time, email, first_name, last_name, last_action, last_action_en,
             opens, clicks, conversions, status, status_en, event_count, profile_id, last_event_datetime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                campaign = VALUES(campaign),
                campaign_send_time = VALUES(campaign_send_time),
                first_name = VALUES(first_name),
                last_name = VALUES(last_name),
                last_action = VALUES(last_action),
                last_action_en = VALUES(last_action_en),
                opens = VALUES(opens),
                clicks = VALUES(clicks),
                conversions = VALUES(conversions),
                status_en = VALUES(status_en),
                event_count = VALUES(event_count),
                profile_id = VALUES(profile_id),
                last_event_datetime = VALUES(last_event_datetime),
                updated_at = VALUES(updated_at)
        """
    else:
        sql = """
            INSERT INTO klaviyo_campaign_recipient_activity
            (campaign_id, campaign, campaign_send_time, email, first_name, last_name, last_action, last_action_en,
             opens, clicks, conversions, status, status_en, event_count, profile_id, last_event_datetime, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id, email, status) DO UPDATE SET
                campaign = excluded.campaign,
                campaign_send_time = excluded.campaign_send_time,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_action = excluded.last_action,
                last_action_en = excluded.last_action_en,
                opens = excluded.opens,
                clicks = excluded.clicks,
                conversions = excluded.conversions,
                status_en = excluded.status_en,
                event_count = excluded.event_count,
                profile_id = excluded.profile_id,
                last_event_datetime = excluded.last_event_datetime,
                updated_at = excluded.updated_at
        """
    cur = conn.cursor()
    if db_type == "mysql":
        sql = sql.replace("?", "%s")
    values = [
        (
            r["campaign_id"],
            r["campaign"],
            normalize_db_dt(r["campaign_send_time"], db_type),
            r["email"],
            r["first_name"],
            r["last_name"],
            r["last_action"],
            r["last_action_en"],
            r["opens"],
            r["clicks"],
            r["conversions"],
            r["status"],
            r["status_en"],
            r["event_count"],
            r["profile_id"],
            normalize_db_dt(r["last_event_datetime"], db_type),
            normalize_db_dt(now, db_type),
        )
        for r in rows
    ]
    if values:
        cur.executemany(sql, values)
    conn.commit()


def sample_rows(conn, db_type, limit):
    cur = execute(
        conn,
        db_type,
        """
        SELECT campaign, email, first_name, last_name, last_action, opens, clicks,
               conversions, status, last_action_en, status_en, event_count, last_event_datetime
        FROM klaviyo_campaign_recipient_activity
        ORDER BY updated_at DESC, campaign, email, status
        LIMIT ?
        """,
        [limit],
    )
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def parse_arg_datetime(value):
    if not value:
        return None
    if len(value) == 10:
        value = value + "T00:00:00+00:00"
    return parse_dt(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-limit", type=int, default=2)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages-per-metric", type=int, default=4)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--window-sync", action="store_true")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--include-conversions", action="store_true")
    args = parser.parse_args()

    api_key = os.getenv("KLAVIYO_API_KEY")
    if not api_key:
        raise SystemExit("Missing KLAVIYO_API_KEY")

    session = make_session(api_key)
    metric_ids = discover_metrics(session)
    campaigns = fetch_campaigns(session, args.campaign_limit)

    conn, db_type = connect_db()
    try:
        create_table(conn, db_type)
        ensure_columns(conn, db_type)
        create_raw_table(conn, db_type)
        if args.window_sync:
            start_dt = parse_arg_datetime(args.start_date)
            if not start_dt:
                start_dt = min(parse_dt(c["send_time"]) for c in campaigns if c.get("send_time")) - timedelta(days=1)
            end_dt = parse_arg_datetime(args.end_date) or datetime.now(timezone.utc)
            rows_written = sync_windows(
                session,
                conn,
                db_type,
                campaigns,
                metric_ids,
                start_dt,
                end_dt,
                args.window_days,
                args.page_size,
                args.max_pages_per_metric,
                args.include_conversions,
            )
            rows = []
        else:
            rows = fetch_activity(session, campaigns, metric_ids, args.page_size, args.max_pages_per_metric)
            upsert_rows(conn, db_type, rows)
            rows_written = len(rows)
        sample = sample_rows(conn, db_type, args.sample_limit)
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "db_type": db_type,
                "campaign_count": len(campaigns),
                "campaigns_preview": campaigns[:3],
                "campaigns_tail": campaigns[-3:],
                "metric_ids": metric_ids,
                "rows_found": len(rows),
                "rows_written": rows_written,
                "sample": sample,
            },
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        body = exc.response.text[:1000] if exc.response is not None else ""
        print(json.dumps({"error": str(exc), "body": body}, ensure_ascii=False), file=sys.stderr)
        raise
