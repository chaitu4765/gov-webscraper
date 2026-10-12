"""Vercel entry point for the TenderDesk web app and stateless API."""
import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from supabase import create_client

from api.collector import run_collection
import app as local

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DOMAINS = local.auth.DOMAINS

app = Flask(__name__)


def database():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY must be configured in Vercel")
    return create_client(url, key)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def public_config():
    url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY must be configured in Vercel")
    return {"url": url, "key": key}


def profile_for(user):
    client, user_id = database(), str(user.id)
    existing = client.table("profiles").select("id,email,name,interests,keywords").eq("id", user_id).execute().data
    if not existing:
        email = user.email or ""
        name = (getattr(user, "user_metadata", {}) or {}).get("name") or email.split("@")[0] or "TenderDesk user"
        client.table("profiles").insert({"id": user_id, "email": email, "name": name}).execute()
        existing = client.table("profiles").select("id,email,name,interests,keywords").eq("id", user_id).execute().data
    return existing[0]


def current_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        result = database().auth.get_user(header.removeprefix("Bearer "))
        return result.user if result and result.user else None
    except Exception:
        return None


def require_user(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify(error="Please sign in"), 401
        return fn(user, *args, **kwargs)
    return wrapped


def terms(profile):
    domain_terms = [term for domain in profile.get("interests") or [] for term in DOMAINS.get(domain, [])]
    keywords = [item.strip().lower() for item in (profile.get("keywords") or "").split(",") if item.strip()]
    return list(dict.fromkeys(domain_terms + keywords))


def bookmarked_ids(user_id):
    return {row["notice_id"] for row in database().table("tenderdesk_bookmarks").select("notice_id").eq("user_id", user_id).execute().data}


def fetch_all(table, columns="*"):
    client, rows, page_size = database(), [], 1000
    while True:
        batch = client.table(table).select(columns).range(len(rows), len(rows) + page_size - 1).execute().data
        rows.extend(batch)
        if len(batch) < page_size:
            return rows


def notices_for(profile, params):
    rows = fetch_all("tenderdesk_notices")
    now = iso_now()
    query, source, kind, status = (params.get("q", "").strip().lower(), params.get("source", ""), params.get("kind", ""), params.get("status", ""))
    saved = bookmarked_ids(profile["id"])
    filtered = []
    for row in rows:
        searchable = " ".join(str(row.get(key) or "") for key in ("title", "reference", "organisation")).lower()
        if query and query not in searchable:
            continue
        if source == "cppp" and row["feed"] not in ("cppp", "epublish"):
            continue
        if source and source != "cppp" and row["feed"] != source:
            continue
        if kind and row["kind"] != kind:
            continue
        deadline = row.get("deadline") or ""
        if status == "open" and deadline <= now:
            continue
        if status == "closing" and not (now < deadline <= (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()):
            continue
        if status == "closed" and (not deadline or deadline > now):
            continue
        if params.get("saved") == "1" and row["id"] not in saved:
            continue
        matches = [term for term in terms(profile) if term in " ".join(str(row.get(key) or "") for key in ("title", "reference", "organisation", "category", "description")).lower()]
        if params.get("recommended") == "1" and (not matches or not deadline or deadline <= now or row["kind"] == "corrigendum"):
            continue
        row["bookmarked"], row["match_terms"] = row["id"] in saved, matches
        filtered.append(row)
    sort = params.get("sort", "latest")
    if sort == "closing":
        filtered.sort(key=lambda item: (not bool(item.get("deadline")), item.get("deadline") or ""))
    else:
        filtered.sort(key=lambda item: item.get("updated_at" if sort == "updated" else "first_seen") or "", reverse=True)
    return filtered


@app.errorhandler(Exception)
def unexpected(error):
    app.logger.exception("Request failed: %s", error)
    return jsonify(error="Server error"), 500


@app.get("/api/config")
def config():
    return jsonify(public_config())


@app.get("/api/me")
@require_user
def me(user):
    return jsonify(user=profile_for(user), domains=DOMAINS)


@app.get("/api/notices")
@require_user
def list_notices(user):
    profile = profile_for(user)
    items = notices_for(profile, request.args)
    page, limit = max(1, int(request.args.get("page", "1"))), 12
    if request.args.get("recommended") == "1":
        limit = 12
    return jsonify(items=items[(page - 1) * limit: page * limit], total=len(items), page=page, limit=limit)


@app.get("/api/notices/<notice_id>")
@require_user
def notice_detail(user, notice_id):
    profile = profile_for(user)
    rows = database().table("tenderdesk_notices").select("*").eq("id", notice_id).execute().data
    if not rows:
        return jsonify(error="Notice not found"), 404
    notice = rows[0]
    notice["bookmarked"] = notice_id in bookmarked_ids(profile["id"])
    history = database().table("tenderdesk_events").select("event_type,at,payload").eq("notice_id", notice_id).order("id", desc=True).execute().data
    return jsonify(notice=notice, history=history)


@app.get("/api/dashboard")
@require_user
def dashboard(user):
    profile = profile_for(user)
    rows = fetch_all("tenderdesk_notices", "id,feed,kind,deadline")
    now, week = iso_now(), (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    sources = database().table("tenderdesk_sources").select("*").execute().data
    source_counts = {}
    for row in rows:
        source_counts[row["feed"]] = source_counts.get(row["feed"], 0) + 1
    for source in sources:
        source["stored"] = source_counts.get(source["id"], 0)
    events = database().table("tenderdesk_events").select("id,notice_id,event_type,at,tenderdesk_notices(title,feed,reference)").order("id", desc=True).limit(12).execute().data
    for event in events:
        notice = event.pop("tenderdesk_notices", None) or {}
        event.update(title=notice.get("title"), feed=notice.get("feed"), reference=notice.get("reference"))
    runs = database().table("tenderdesk_runs").select("*").order("id", desc=True).limit(20).execute().data
    activity = {}
    for event in fetch_all("tenderdesk_events", "at"):
        day = event["at"][:10]
        activity[day] = activity.get(day, 0) + 1
    return jsonify(stats={"total": len(rows), "open": sum(row["kind"] != "corrigendum" and (row.get("deadline") or "") > now for row in rows), "closing": sum(row["kind"] != "corrigendum" and now < (row.get("deadline") or "") <= week for row in rows), "corrigenda": sum(row["kind"] == "corrigendum" for row in rows), "saved": len(bookmarked_ids(profile["id"]))}, sources=sources, events=events, activity=[{"day": day, "count": count} for day, count in sorted(activity.items())[-7:]], runs=runs, sync={"running": False, "current": [], "last_finished": runs[0]["finished"] if runs else None, "next_run": None}, settings={row["key"]: row["value"] for row in database().table("tenderdesk_settings").select("key,value").execute().data}, server_time=now)


@app.post("/api/profile")
@require_user
def update_profile(user):
    body, valid = request.get_json(silent=True) or {}, set(DOMAINS)
    interests, keywords = body.get("interests"), body.get("keywords", "")
    if not isinstance(interests, list) or any(value not in valid for value in interests) or not isinstance(keywords, str) or len(keywords) > 300:
        return jsonify(error="Invalid interests"), 400
    database().table("profiles").update({"interests": interests, "keywords": keywords.strip()}).eq("id", str(user.id)).execute()
    return jsonify(ok=True)


@app.post("/api/bookmark")
@require_user
def bookmark(user):
    body, notice_id, saved = request.get_json(silent=True) or {}, None, None
    notice_id, saved = body.get("id"), body.get("saved")
    if not isinstance(notice_id, str) or not isinstance(saved, bool):
        return jsonify(error="Invalid saved flag"), 400
    client = database()
    if saved:
        client.table("tenderdesk_bookmarks").upsert({"user_id": str(user.id), "notice_id": notice_id}, on_conflict="user_id,notice_id").execute()
    else:
        client.table("tenderdesk_bookmarks").delete().eq("user_id", str(user.id)).eq("notice_id", notice_id).execute()
    return jsonify(saved=saved)


@app.get("/api/export")
@require_user
def export(user):
    rows = notices_for(profile_for(user), request.args)
    stream = io.StringIO()
    fields = ["feed", "kind", "title", "reference", "organisation", "deadline", "url"]
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for row in rows[:10000]:
        writer.writerow({field: "'" + str(row.get(field) or "") if str(row.get(field) or "").startswith(("=", "+", "-", "@", "\t", "\r")) else row.get(field) or "" for field in fields})
    return Response("\ufeff" + stream.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=tenderdesk-export.csv"})


def document_rows(notice_id):
    return database().table("tenderdesk_documents").select("id,name,fetched,object_path").eq("notice_id", notice_id).order("name").execute().data


def presented_documents(rows):
    store = database().storage.from_("tenderdesk-documents")
    return [{"id": row["id"], "name": row["name"], "fetched": row["fetched"], "url": store.create_signed_url(row["object_path"], 300)["signedURL"]} for row in rows]


@app.post("/api/documents")
@require_user
def fetch_documents(user):
    notice_id = (request.get_json(silent=True) or {}).get("id")
    notices = database().table("tenderdesk_notices").select("*").eq("id", notice_id).execute().data
    if not notices:
        return jsonify(error="Notice not found"), 404
    cached = document_rows(notice_id)
    if cached:
        return jsonify(documents=presented_documents(cached), message="Saved PDF copies. Check the official portal for later revisions.")
    notice, found, client = notices[0], [], local.Client()
    try:
        body, base = client.binary(notice["url"])
        candidates = [(base, notice["reference"], body)] if body.startswith(b"%PDF-") else []
        if not candidates:
            for url, label in local.document_candidates(body.decode("utf-8", errors="replace"), base):
                try:
                    pdf, final_url = client.binary(url)
                    if pdf.startswith(b"%PDF-"):
                        candidates.append((final_url, label, pdf))
                except Exception:
                    continue
        for url, label, pdf in candidates:
            name = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:120].strip("._") or "tender-document"
            name = name if name.lower().endswith(".pdf") else name + ".pdf"
            doc_id = hashlib.sha256((notice_id + hashlib.sha256(pdf).hexdigest()).encode()).hexdigest()[:32]
            object_path = f"notices/{notice_id}/{doc_id}/{name}"
            database().storage.from_("tenderdesk-documents").upload(object_path, pdf, {"content-type": "application/pdf", "upsert": "false"})
            database().table("tenderdesk_documents").upsert({"id": doc_id, "notice_id": notice_id, "name": name, "source_url": url, "object_path": object_path}, on_conflict="id").execute()
        cached = document_rows(notice_id)
        message = "PDFs were saved securely." if cached else "No directly accessible PDF was returned. The portal may require login, CAPTCHA, or an interactive document selection."
        return jsonify(documents=presented_documents(cached), message=message)
    except Exception as error:
        return jsonify(error=f"Could not retrieve documents: {error}"), 502


@app.get("/api/documents/<document_id>")
@require_user
def download_document(user, document_id):
    rows = database().table("tenderdesk_documents").select("object_path").eq("id", document_id).execute().data
    if not rows:
        return jsonify(error="Document not found"), 404
    signed = database().storage.from_("tenderdesk-documents").create_signed_url(rows[0]["object_path"], 300)
    return redirect(signed["signedURL"])


@app.post("/api/settings")
@require_user
def update_settings(user):
    body = request.get_json(silent=True) or {}
    values = {"gem_pages": body.get("gem_pages"), "gem_backfill_pages": body.get("gem_backfill_pages")}
    if not (isinstance(values["gem_pages"], int) and 1 <= values["gem_pages"] <= 20 and isinstance(values["gem_backfill_pages"], int) and 0 <= values["gem_backfill_pages"] <= 10):
        return jsonify(error="Invalid settings"), 400
    database().table("tenderdesk_settings").upsert([{"key": key, "value": value} for key, value in values.items()], on_conflict="key").execute()
    return jsonify(values)


@app.route("/api/collect", methods=["GET", "POST"])
def collect():
    expected = os.getenv("CRON_SECRET")
    supplied = request.headers.get("Authorization", "")
    if not expected or supplied != f"Bearer {expected}":
        return jsonify(error="Unauthorized"), 401
    return jsonify(results=run_collection(database()))


@app.post("/api/sync")
@require_user
def manual_sync(user):
    # A normal user cannot invoke the protected cron secret from their browser.
    return jsonify(queued=False, running=False, message="A protected Vercel Cron job performs collection on its schedule."), 202


@app.get("/")
def home():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:asset>")
def static_files(asset):
    if asset in {"app.js", "session.js", "styles.css"}:
        return send_from_directory(WEB, asset)
    return jsonify(error="Not found"), 404
