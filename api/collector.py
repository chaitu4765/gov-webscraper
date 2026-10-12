"""Stateless TenderDesk collector used by the Vercel cron endpoint."""
import hashlib
import json
import re
import time
from datetime import datetime, timezone

import app as local


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fingerprint(record):
    value = {key: value for key, value in record.items() if key not in ("id", "url")}
    if record["feed"] != "gem":
        value["description"] = re.sub(r"^\d+\.\s*", "", value["description"])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def settings(client):
    rows = client.table("tenderdesk_settings").select("key,value").execute().data
    values = {row["key"]: row["value"] for row in rows}
    return {"gem_pages": values.get("gem_pages", 3), "gem_backfill_pages": values.get("gem_backfill_pages", 2)}


def save_records(client, feed, records):
    at = stamp()
    records = [{**item, "url": local.public_notice_url(item["url"])} for item in records]
    records = list({item["id"]: item for item in records}.values())
    existing = client.table("tenderdesk_notices").select("id,fingerprint").in_("id", [item["id"] for item in records]).execute().data
    previous = {item["id"]: item for item in existing}
    rows, events, added, changed = [], [], 0, 0
    for record in records:
        digest = fingerprint(record)
        old = previous.get(record["id"])
        row = {**record, "last_seen": at, "fingerprint": digest}
        if not old:
            row.update(first_seen=at, updated_at=at)
            added += 1
            events.append({"notice_id": record["id"], "event_type": "new", "at": at, "payload": record})
        elif old["fingerprint"] != digest:
            row["updated_at"] = at
            changed += 1
            events.append({"notice_id": record["id"], "event_type": "updated", "at": at, "payload": record})
        else:
            # Upsert preserves the original first_seen / updated_at columns because
            # they are not present in this unchanged record.
            row.pop("fingerprint")
        rows.append(row)
    client.table("tenderdesk_notices").upsert(rows, on_conflict="id").execute()
    if events:
        client.table("tenderdesk_events").insert(events).execute()
    return len(records), added, changed


def collect_feed(client, feed, config):
    started, started_clock = stamp(), time.monotonic()
    client.table("tenderdesk_sources").update({"state": "running", "last_attempt": started, "error": None}).eq("id", feed).execute()
    try:
        remote_total, cursor = None, 1
        source = client.table("tenderdesk_sources").select("cursor").eq("id", feed).single().execute().data
        cursor = source.get("cursor", 1)
        http = local.Client()
        body = http.get(local.FEEDS[feed][1])
        if feed == "gem":
            match = re.search(r"'csrf_bd_gem_nk':\s*'([^']+)'", body)
            if not match:
                raise ValueError("GeM listing/token unavailable; possible block or portal change")
            first = config["gem_pages"]
            cursor = max(cursor, first + 1)
            pages = list(range(1, first + 1)) + list(range(cursor, cursor + config["gem_backfill_pages"]))
            records, next_cursor = [], cursor
            for page in pages:
                payload = {"page": page, "param": {"searchBid": "", "searchType": "fullText"}, "filter": {"bidStatusType": "ongoing_bids", "byType": "all", "highBidValue": "", "byEndDate": {"from": "", "to": ""}, "sort": "Bid-Start-Date-Latest"}}
                encoded = __import__("urllib.parse").parse.urlencode({"payload": json.dumps(payload), "csrf_bd_gem_nk": match.group(1)}).encode()
                page_records, remote_total = local.parse_gem(json.loads(http.get("https://bidplus.gem.gov.in/all-bids-data", encoded)))
                records.extend(page_records)
                if page >= cursor:
                    next_cursor = page + 1
                if page * 10 >= remote_total:
                    next_cursor = first + 1
                    break
                time.sleep(0.6)
        else:
            records = local.parse_gep(body, feed, local.FEEDS[feed][1])
            next_cursor = cursor
        if not records:
            raise ValueError("No notice records parsed; the portal may be empty, blocked, or changed")
        fetched, added, changed = save_records(client, feed, records)
        finished = stamp()
        client.table("tenderdesk_sources").update({"state": "ok", "last_success": finished, "error": None, "fetched": fetched, "new_count": added, "changed_count": changed, "duration": round(time.monotonic() - started_clock, 2), "remote_total": remote_total, "cursor": next_cursor}).eq("id", feed).execute()
        client.table("tenderdesk_runs").insert({"feed": feed, "started": started, "finished": finished, "state": "ok", "fetched": fetched, "new_count": added, "changed_count": changed}).execute()
        return {"feed": feed, "state": "ok", "fetched": fetched, "new": added, "changed": changed}
    except Exception as error:
        message = f"{type(error).__name__}: {error}"[:500]
        client.table("tenderdesk_sources").update({"state": "error", "error": message, "duration": round(time.monotonic() - started_clock, 2)}).eq("id", feed).execute()
        client.table("tenderdesk_runs").insert({"feed": feed, "started": started, "finished": stamp(), "state": "error", "error": message}).execute()
        return {"feed": feed, "state": "error", "error": message}


def run_collection(client):
    config = settings(client)
    return [collect_feed(client, feed, config) for feed in local.FEEDS]
