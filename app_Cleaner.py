#!/usr/bin/env python3
"""
X (Twitter) HAR to CSV Parser — Streamlit App

Upload one or more HAR (HTTP Archive) files captured from X / Twitter and
export just the tweet-relevant fields found in the network traffic:

    - tweet_id
    - tweet_text
    - username (the account that posted the tweet)
    - impressions (view count, when present)

The app scans every response body in the HAR for JSON, then recursively
searches that JSON for tweet objects (the shape X's API returns them in,
e.g. a dict containing a "legacy" object with "full_text"/"id_str", an
optional "core.user_results.result" for the author, and an optional
"views.count" for impressions). Everything else in the HAR is ignored.

Run with:
    pip install streamlit
    streamlit run app.py
"""

import csv
import io
import json

import streamlit as st


# --------------------------------------------------------------------------
# Core parsing logic
# --------------------------------------------------------------------------

def _get_username(tweet_obj):
    """Pull the posting user's screen_name (falling back to display name)
    out of a tweet-result object's embedded user data, if present."""
    core = tweet_obj.get("core")
    if not isinstance(core, dict):
        return None
    user_results = core.get("user_results")
    if not isinstance(user_results, dict):
        return None
    user_result = user_results.get("result")
    if not isinstance(user_result, dict):
        return None
    user_legacy = user_result.get("legacy")
    if isinstance(user_legacy, dict):
        screen_name = user_legacy.get("screen_name")
        if screen_name:
            return screen_name
        name = user_legacy.get("name")
        if name:
            return name
    # Some payloads (e.g. user results wrapped a level deeper) - try core-level fields
    core_data = user_result.get("core")
    if isinstance(core_data, dict):
        return core_data.get("screen_name") or core_data.get("name")
    return None


def _get_impressions(tweet_obj):
    """Pull the view/impression count out of a tweet-result object, if present."""
    views = tweet_obj.get("views")
    if isinstance(views, dict):
        count = views.get("count")
        if count is not None:
            return count
    # Fallback: some legacy payloads expose an ext_views or similar field
    legacy = tweet_obj.get("legacy")
    if isinstance(legacy, dict):
        for key in ("ext_views", "view_count", "impression_count"):
            if key in legacy:
                return legacy[key]
    return None


def _extract_tweet_fields(tweet_obj):
    """
    Given a dict that looks like an X "tweet result" object (has a
    "legacy" sub-object with tweet text/id), pull out just the fields
    we care about. Returns None if it doesn't actually look like a tweet.
    """
    legacy = tweet_obj.get("legacy")
    if not isinstance(legacy, dict):
        return None

    text = legacy.get("full_text")
    if text is None:
        text = legacy.get("text")

    tweet_id = tweet_obj.get("rest_id") or legacy.get("id_str") or legacy.get("id")

    # Require at least an id or text to consider this a real tweet object,
    # rather than some unrelated dict that happens to have a "legacy" key.
    if tweet_id is None and text is None:
        return None

    return {
        "tweet_id": tweet_id,
        "tweet_text": text,
        "username": _get_username(tweet_obj),
        "impressions": _get_impressions(tweet_obj),
    }


def find_tweets(obj, found_rows, seen_ids):
    """
    Recursively walk an arbitrary JSON structure (dicts/lists) looking for
    tweet-result objects, collecting one row per unique tweet_id found.
    """
    if isinstance(obj, dict):
        tweet = _extract_tweet_fields(obj)
        if tweet is not None:
            dedupe_key = tweet["tweet_id"] if tweet["tweet_id"] is not None else id(obj)
            if dedupe_key not in seen_ids:
                seen_ids.add(dedupe_key)
                found_rows.append(tweet)
        for v in obj.values():
            find_tweets(v, found_rows, seen_ids)
    elif isinstance(obj, list):
        for item in obj:
            find_tweets(item, found_rows, seen_ids)


def looks_like_har(data) -> bool:
    """
    Structural check for HAR content, independent of file extension/name.
    A valid HAR is a JSON object with a top-level "log" object that has
    an "entries" list (entries may be empty).
    """
    if not isinstance(data, dict):
        return False
    log = data.get("log")
    if not isinstance(log, dict):
        return False
    return isinstance(log.get("entries"), list)


def _response_body_text(entry):
    """
    Pull the raw text of a HAR entry's response body, if any. HAR stores
    this at response.content.text, optionally base64-encoded.
    """
    response = entry.get("response")
    if not isinstance(response, dict):
        return None
    content = response.get("content")
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    if not text:
        return None
    if content.get("encoding") == "base64":
        import base64
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return None
    return text


def extract_rows(har_data, source_filename: str, warnings: list):
    """
    Given the parsed JSON of a single HAR file, return a list of tweet
    rows (tweet_id, tweet_text, username, impressions) found anywhere in
    the response bodies of that file's entries.
    """
    rows = []

    if not looks_like_har(har_data):
        warnings.append(
            f"'{source_filename}' does not match HAR structure "
            f"(expected a JSON object with log.entries); skipped."
        )
        return rows

    log = har_data.get("log", {})
    entries = log.get("entries", [])

    seen_ids = set()
    for entry in entries:
        body_text = _response_body_text(entry)
        if not body_text:
            continue
        try:
            body_json = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            # Not a JSON response body (images, scripts, HTML, etc.) - skip
            continue

        entry_rows = []
        find_tweets(body_json, entry_rows, seen_ids)
        for row in entry_rows:
            row["source_file"] = source_filename
        rows.extend(entry_rows)

    if not rows:
        warnings.append(
            f"'{source_filename}' matched HAR structure but no tweet data "
            f"was found in any response bodies."
        )

    return rows


FIELDNAMES = ["source_file", "tweet_id", "username", "tweet_text", "impressions"]


def parse_har_files(uploaded_files):
    """
    Parse a list of Streamlit UploadedFile objects (HAR JSON content).
    Returns (all_rows, per_file_summary, warnings).
    """
    all_rows = []
    warnings = []
    summary = []  # list of (filename, tweet_count) tuples

    for uploaded_file in uploaded_files:
        filename = uploaded_file.name
        try:
            raw_bytes = uploaded_file.getvalue()
            text = raw_bytes.decode("utf-8-sig")
            har_data = json.loads(text)
        except UnicodeDecodeError as e:
            warnings.append(f"'{filename}' could not be decoded as text ({e}); skipped.")
            continue
        except json.JSONDecodeError as e:
            warnings.append(f"'{filename}' is not valid JSON ({e}); skipped.")
            continue

        rows = extract_rows(har_data, filename, warnings)
        summary.append((filename, len(rows)))
        all_rows.extend(rows)

    return all_rows, summary, warnings


def rows_to_csv_bytes(all_rows):
    """Write rows to an in-memory CSV and return the bytes."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDNAMES, restval="", extrasaction="ignore")
    writer.writeheader()
    for row in all_rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="X HAR to CSV Parser", layout="wide")

st.title("X (Twitter) HAR to CSV Parser")
st.write(
    "Upload one or more HAR files captured from X / Twitter. The app pulls "
    "out just the tweet-relevant fields — **tweet ID**, **tweet text**, "
    "**posting user**, and **impressions** — from every tweet object found "
    "in the network responses, and exports them to a single CSV."
)
st.caption(
    "Files are identified by their JSON structure (a `log.entries` object), "
    "not by their file extension — so HAR content saved as `.txt`, `.json`, "
    "or with no extension at all will still be picked up."
)

uploaded_files = st.file_uploader(
    "Upload HAR file(s)",
    type=None,  # accept any extension — validity is checked by content, not by name
    accept_multiple_files=True,
    help="Select any file(s) containing HAR JSON content, regardless of extension.",
)

if uploaded_files:
    with st.spinner("Parsing HAR file(s) for tweet data..."):
        all_rows, summary, warnings = parse_har_files(uploaded_files)

    if warnings:
        for w in warnings:
            st.warning(w)

    if not all_rows:
        st.error("No tweet data was found in the uploaded file(s). Nothing to export.")
    else:
        st.success(
            f"Parsed {len(uploaded_files)} file(s): {len(all_rows)} tweet(s) found."
        )

        st.subheader("Per-file summary")
        st.table(
            {
                "File": [s[0] for s in summary],
                "Tweets found": [s[1] for s in summary],
            }
        )

        st.subheader("Preview (first 50 rows)")
        try:
            import pandas as pd

            df = pd.DataFrame(all_rows, columns=FIELDNAMES)
            st.dataframe(df.head(50), width="stretch")
        except ImportError:
            st.info("Install `pandas` to see an in-app preview table. CSV download still works below.")

        csv_bytes = rows_to_csv_bytes(all_rows)

        st.download_button(
            label="Download CSV",
            data=csv_bytes,
            file_name="x_tweets_export.csv",
            mime="text/csv",
        )
else:
    st.info("Waiting for HAR file upload(s)...")
