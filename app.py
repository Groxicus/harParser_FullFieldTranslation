#!/usr/bin/env python3
"""
HAR to CSV Parser — Streamlit App

Upload one or more HAR (HTTP Archive) files and export EVERY field found in
each HAR entry into a single downloadable CSV. No data is cleaned, filtered,
or dropped — nested JSON structures are flattened into flat "dotted" column
names, and every unique column encountered across all uploaded files is
included in the output (missing values are left blank for rows that don't
have that field).

Run with:
    pip install streamlit
    streamlit run app.py
"""

import csv
import io
import json

import streamlit as st


# --------------------------------------------------------------------------
# Core parsing logic (same behavior as the standalone CLI version)
# --------------------------------------------------------------------------

def flatten(obj, parent_key="", sep="."):
    """
    Recursively flattens a nested structure (dicts/lists) into a single
    flat dict of {dotted.key.path: scalar_value}.

    Dict keys are appended as "<parent>.<key>".
    List indices are appended as "<parent>.<index>".
    Scalars (including None, empty dicts, empty lists) are stored directly.
    """
    items = {}

    if isinstance(obj, dict):
        if not obj:
            if parent_key:
                items[parent_key] = ""
            return items
        for k, v in obj.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else str(k)
            items.update(flatten(v, new_key, sep))

    elif isinstance(obj, list):
        if not obj:
            if parent_key:
                items[parent_key] = ""
            return items
        for i, v in enumerate(obj):
            new_key = f"{parent_key}{sep}{i}" if parent_key else str(i)
            items.update(flatten(v, new_key, sep))

    else:
        items[parent_key] = obj

    return items


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


def extract_rows(har_data, source_filename: str, warnings: list):
    """
    Given the parsed JSON of a single HAR file, return a list of flat
    dict rows -- one per entry in log.entries.
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

    for idx, entry in enumerate(entries):
        flat_row = flatten(entry)
        flat_row["source_file"] = source_filename
        flat_row["entry_index"] = idx
        rows.append(flat_row)

    return rows


def build_fieldnames(all_rows):
    """
    Compute the full ordered set of CSV column headers across all rows.
    source_file and entry_index are pinned first for readability; every
    other discovered column is included, sorted alphabetically so the
    column order is stable and deterministic run-to-run.
    """
    priority = ["source_file", "entry_index"]
    other_keys = set()

    for row in all_rows:
        other_keys.update(row.keys())

    other_keys.difference_update(priority)
    return priority + sorted(other_keys)


def parse_har_files(uploaded_files):
    """
    Parse a list of Streamlit UploadedFile objects (HAR JSON content).
    Returns (all_rows, fieldnames, per_file_summary, warnings).
    """
    all_rows = []
    warnings = []
    summary = []  # list of (filename, entry_count) tuples

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

    fieldnames = build_fieldnames(all_rows) if all_rows else []
    return all_rows, fieldnames, summary, warnings


def rows_to_csv_bytes(all_rows, fieldnames):
    """Write rows to an in-memory CSV and return the bytes."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, restval="", extrasaction="raise")
    writer.writeheader()
    for row in all_rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="HAR to CSV Parser", layout="wide")

st.title("HAR to CSV Parser")
st.write(
    "Upload one or more HAR files. Every field from every request/response "
    "entry is flattened and exported to a single CSV — no columns are dropped "
    "and no data is cleaned."
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
    with st.spinner("Parsing HAR file(s)..."):
        all_rows, fieldnames, summary, warnings = parse_har_files(uploaded_files)

    if warnings:
        for w in warnings:
            st.warning(w)

    if not all_rows:
        st.error("No entries were parsed from the uploaded file(s). Nothing to export.")
    else:
        st.success(
            f"Parsed {len(uploaded_files)} file(s): {len(all_rows)} total entries, "
            f"{len(fieldnames)} unique columns."
        )

        st.subheader("Per-file summary")
        st.table(
            {
                "File": [s[0] for s in summary],
                "Entries parsed": [s[1] for s in summary],
            }
        )

        st.subheader("Preview (first 50 rows)")
        try:
            import pandas as pd

            df = pd.DataFrame(all_rows, columns=fieldnames)
            st.dataframe(df.head(50), width="stretch")
        except ImportError:
            st.info("Install `pandas` to see an in-app preview table. CSV download still works below.")

        csv_bytes = rows_to_csv_bytes(all_rows, fieldnames)

        st.download_button(
            label="Download CSV",
            data=csv_bytes,
            file_name="har_export.csv",
            mime="text/csv",
        )
else:
    st.info("Waiting for HAR file upload(s)...")
