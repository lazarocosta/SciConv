"""
routes/data_upload.py

Standalone mode: Upload data directly to Zenodo with GPT-powered metadata inference.

Endpoints:
  POST /data-upload/upload           — accept file/ZIP, save, infer metadata, return draft
  POST /data-upload/<uuid>/confirm   — confirm metadata, upload to Zenodo (chunking if needed)
  GET  /data-upload/<uuid>/progress  — poll upload status and DOIs
"""

import datetime
import json
import os
import time
import threading
import uuid as _uuid
import zipfile

import requests
from flask import Blueprint, jsonify, request
from flask_cors import cross_origin
from werkzeug.utils import secure_filename

import config as cfg
from auth import require_auth

data_upload_bp = Blueprint("data_upload", __name__)

# Storage root — sibling of projects/
DATA_UPLOADS_LOCATION = os.path.join(
    os.path.dirname(os.path.abspath(cfg.PROJECTS_LOCATION)), "data_uploads"
)

ZENODO_API_BASE = "https://zenodo.org/api"
ZENODO_RECORD_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MB (temp test)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _du_dir(upload_uuid, *sub):
    return os.path.join(DATA_UPLOADS_LOCATION, upload_uuid, *sub)


def _load_info(upload_uuid):
    p = _du_dir(upload_uuid, "info.json")
    if not os.path.isfile(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_info(upload_uuid, info):
    os.makedirs(_du_dir(upload_uuid), exist_ok=True)
    with open(_du_dir(upload_uuid, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2, ensure_ascii=False)


def _folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def _upload_file_to_bucket(bucket_url, local_path, token):
    """Stream a local file into a Zenodo bucket URL."""
    with open(local_path, "rb") as fh:
        resp = requests.put(
            bucket_url,
            params={"access_token": token},
            data=fh,
            timeout=600,
        )
    resp.raise_for_status()
    return resp.json()


def _zenodo_request(method, url, *, token, retries=3, delay=5, **kwargs):
    """
    Thin wrapper around requests that retries on 5xx / connection errors.
    *method* is 'get', 'post', or 'put'.
    Raises the last exception if all attempts fail.
    """
    headers = kwargs.pop("headers", {})
    headers.setdefault("Content-Type", "application/json")
    headers["Authorization"] = f"Bearer {token}"

    fn = getattr(requests, method)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = fn(url, headers=headers, **kwargs)
            if resp.status_code < 500:
                return resp          # success or a 4xx we should surface immediately
            # 5xx — Zenodo server error, worth retrying
            print(f"[zenodo] {method.upper()} {url} → {resp.status_code} (attempt {attempt}/{retries})")
            last_exc = requests.HTTPError(
                f"{resp.status_code} Server Error: {resp.reason} for url: {url}",
                response=resp,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            print(f"[zenodo] {method.upper()} {url} network error (attempt {attempt}/{retries}): {exc}")
            last_exc = exc

        if attempt < retries:
            time.sleep(delay * attempt)   # 5 s, 10 s, …

    raise last_exc


def _create_empty_deposition(token):
    """
    Create a blank Zenodo draft and return (dep_id, pre_reserved_doi, bucket_url).
    The pre-reserved DOI is the same DOI that will be active after publishing.
    """
    resp = _zenodo_request("post", f"{ZENODO_API_BASE}/deposit/depositions",
                           token=token, data="{}", timeout=60)
    resp.raise_for_status()
    dep = resp.json()
    dep_id = dep["id"]
    pre_doi = (
        dep.get("doi")
        or dep.get("metadata", {}).get("prereserve_doi", {}).get("doi", "")
    )
    bucket_url = dep["links"]["bucket"]
    return dep_id, pre_doi, bucket_url


def _set_deposition_metadata(dep_id, metadata_json, token):
    """Set (or update) metadata on an existing draft deposition."""
    resp = _zenodo_request("put", f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}",
                           token=token, data=json.dumps(metadata_json), timeout=60)
    if not resp.ok:
        print(f"[zenodo] metadata PUT {dep_id} failed ({resp.status_code}): {resp.text[:1000]}")
        raise RuntimeError(
            f"Zenodo metadata update failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


def _publish_deposition(dep_id, token):
    """Publish an existing draft deposition. Returns the published record JSON."""
    resp = _zenodo_request("post",
                           f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}/actions/publish",
                           token=token, timeout=120)
    if not resp.ok:
        raise RuntimeError(
            f"Zenodo publish failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


def _create_deposition(file_paths, metadata_json):
    """
    Create a Zenodo deposition, upload files from local paths, and publish.

    file_paths: [{"path": "/abs/path", "zenodo_name": "dataset.tar"}, ...]
    metadata_json: {"metadata": {...}}
    Returns the published deposition JSON.
    """
    token = os.getenv("ZENODO_API_TOKEN")
    if not token:
        raise RuntimeError("ZENODO_API_TOKEN environment variable is not set")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    # 1) Create empty deposition
    resp = requests.post(
        f"{ZENODO_API_BASE}/deposit/depositions",
        data="{}", headers=headers, timeout=60,
    )
    resp.raise_for_status()
    deposition = resp.json()

    # 2) Set metadata
    _sanitize_metadata(metadata_json)
    dep_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}"
    resp = requests.put(dep_url, data=json.dumps(metadata_json),
                        headers=headers, timeout=60)
    resp.raise_for_status()
    deposition = resp.json()

    # 3) Upload files
    bucket_url = deposition["links"]["bucket"]
    for finfo in file_paths:
        _upload_file_to_bucket(
            f"{bucket_url}/{finfo['zenodo_name']}", finfo["path"], token
        )

    # 4) Publish
    pub_resp = requests.post(
        f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}/actions/publish",
        headers=headers, timeout=120,
    )
    if not pub_resp.ok:
        raise RuntimeError(
            f"Zenodo publish failed ({pub_resp.status_code}): {pub_resp.text[:500]}"
        )
    return pub_resp.json()


def _sanitize_metadata(metadata_json):
    """Ensure minimum Zenodo publish requirements are met. Mutates in place."""
    md = metadata_json.get("metadata", metadata_json)

    # title
    title = md.get("title", "")
    if not isinstance(title, str) or not title.strip():
        md["title"] = "Untitled Dataset"

    # upload_type
    valid_types = {
        "publication", "poster", "presentation", "dataset",
        "image", "video", "software", "lesson", "physicalobject", "other",
    }
    if md.get("upload_type") not in valid_types:
        md["upload_type"] = "dataset"

    # description (guard against placeholder dict being passed)
    desc = md.get("description", "")
    if not isinstance(desc, str) or not desc.strip():
        md["description"] = "Dataset uploaded via SciConv."

    # publication_date — required by Zenodo; always ensure it's a valid ISO date
    pub_date = md.get("publication_date", "")
    if not isinstance(pub_date, str) or not pub_date.strip():
        md["publication_date"] = datetime.date.today().isoformat()

    creators = md.get("creators", [])
    if not isinstance(creators, list) or not creators:
        creators = [{"name": "Unknown"}]
    md["creators"] = [
        c for c in creators
        if isinstance(c, dict) and c.get("name", "").strip()
    ] or [{"name": "Unknown"}]

    valid_access = {"open", "embargoed", "restricted", "closed"}
    if md.get("access_right") not in valid_access:
        md["access_right"] = "open"

    if md.get("access_right") in {"open", "embargoed"} and not md.get("license"):
        md["license"] = "cc-by-4.0"

    # Strip fields Zenodo rejects
    for key in ("grants", "thesis_supervisors", "thesis_university",
                "partof_title", "partof_pages"):
        md.pop(key, None)

    # Remove _tobefilledbyuser placeholders
    def _strip_placeholders(obj):
        if isinstance(obj, dict):
            if obj.get("_tobefilledbyuser"):
                return None
            return {k: _strip_placeholders(v) for k, v in obj.items()
                    if _strip_placeholders(v) is not None}
        if isinstance(obj, list):
            cleaned = [_strip_placeholders(i) for i in obj]
            return [i for i in cleaned if i is not None]
        return obj

    metadata_json["metadata"] = _strip_placeholders(md)
    return metadata_json


# ---------------------------------------------------------------------------
# Background upload worker
# ---------------------------------------------------------------------------

def _run_upload(upload_uuid, metadata):
    from helpers.dataset_chunker import prepare_dataset_for_upload

    info = _load_info(upload_uuid)
    raw_dir = _du_dir(upload_uuid, "raw")
    work_dir = _du_dir(upload_uuid, "work")

    try:
        info["progress"] = "Preparing data..."
        _save_info(upload_uuid, info)

        prep = prepare_dataset_for_upload(
            raw_dir, work_dir, zenodo_limit=ZENODO_RECORD_LIMIT_BYTES
        )
        dois = []

        if not prep["chunked"]:
            # ── Single deposition ────────────────────────────────────────────
            info["progress"] = "Uploading to Zenodo..."
            _save_info(upload_uuid, info)
            metadata_json = {"metadata": dict(metadata)}
            dep = _create_deposition(
                [{"path": prep["tar_path"], "zenodo_name": "dataset.tar"}],
                metadata_json,
            )
            dois.append(dep.get("doi", ""))

        else:
            # ── Multi-part: two-pass so each part can link to the others ─────
            chunks = prep["chunks"]
            n = len(chunks)

            token = os.getenv("ZENODO_API_TOKEN")
            if not token:
                raise RuntimeError("ZENODO_API_TOKEN environment variable is not set")

            # Pass 1 — create all drafts, collect pre-reserved DOIs
            info["progress"] = f"Reserving {n} Zenodo records..."
            _save_info(upload_uuid, info)

            drafts = []  # (dep_id, pre_doi, bucket_url, chunk)
            for chunk in chunks:
                dep_id, pre_doi, bucket_url = _create_empty_deposition(token)
                drafts.append((dep_id, pre_doi, bucket_url, chunk))

            all_dois = [pre_doi for _, pre_doi, _, _ in drafts]

            # Pass 2 — set metadata (with links), upload, publish
            for i, (dep_id, pre_doi, bucket_url, chunk) in enumerate(drafts, 1):
                info["progress"] = f"Uploading part {i} of {n}..."
                _save_info(upload_uuid, info)

                chunk_meta = dict(metadata)
                chunk_meta["title"] = (
                    f"{metadata.get('title', 'Dataset')} — Part {i} of {n}"
                )

                # Prepend split notice + links to sibling parts in description
                base_desc = chunk_meta.get("description", "")
                other_links = [
                    f"  • Part {j}: https://doi.org/{doi}"
                    for j, doi in enumerate(all_dois, 1)
                    if j != i and doi
                ]
                split_note = (
                    f"Note: This dataset has been split into {n} parts. "
                    f"This is part {i} of {n}."
                )
                if other_links:
                    split_note += "\nOther parts:\n" + "\n".join(other_links)
                chunk_meta["description"] = split_note + "\n\n" + base_desc

                # related_identifiers — cross-link sibling parts
                # "references" is used because Zenodo deposit v1 does not support isRelatedTo
                chunk_meta["related_identifiers"] = [
                    {"relation": "references", "identifier": doi, "scheme": "doi"}
                    for j, doi in enumerate(all_dois, 1)
                    if j != i and doi
                ]

                metadata_json = {"metadata": chunk_meta}
                _sanitize_metadata(metadata_json)
                _set_deposition_metadata(dep_id, metadata_json, token)

                _upload_file_to_bucket(
                    f"{bucket_url}/{chunk['filename']}", chunk["path"], token
                )

                pub = _publish_deposition(dep_id, token)
                dois.append(pub.get("doi", pre_doi))

        info["status"] = "completed"
        info["progress"] = "Upload complete."
        info["dois"] = dois
        _save_info(upload_uuid, info)

    except Exception as exc:
        info["status"] = "error"
        info["progress"] = f"Upload failed: {str(exc)}"
        _save_info(upload_uuid, info)
        print(f"[data_upload] Error for {upload_uuid}: {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@data_upload_bp.route("/data-upload/upload", methods=["POST"])
@cross_origin()
@require_auth
def upload_data():
    """
    Step 1 — Accept data file or ZIP, save it, infer Zenodo metadata with GPT.
    Returns: { uuid, total_bytes, zenodo_metadata, template }
    """
    from helpers.article.articleHelper import infer_dataset_metadata_from_data

    data_file = request.files.get("data_file")
    if not data_file or not getattr(data_file, "filename", ""):
        return jsonify({"error": "No file provided."}), 400

    upload_uuid = "du_" + _uuid.uuid4().hex[:12]
    raw_dir = _du_dir(upload_uuid, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    filename = secure_filename(data_file.filename)
    dest = os.path.join(raw_dir, filename)
    data_file.save(dest)

    # Extract ZIP
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(dest, "r") as zf:
            zf.extractall(raw_dir)
        os.remove(dest)

    total_bytes = _folder_size(raw_dir)

    # Infer metadata
    result = infer_dataset_metadata_from_data(raw_dir, upload_uuid)
    if result.get("skip"):
        return jsonify({"error": "Could not infer metadata from the uploaded data."}), 422

    _save_info(upload_uuid, {
        "uuid": upload_uuid,
        "total_bytes": total_bytes,
        "status": "pending_review",
        "dois": [],
        "progress": "",
    })

    return jsonify({
        "uuid": upload_uuid,
        "total_bytes": total_bytes,
        "zenodo_metadata": result.get("zenodo_metadata"),
        "template": result.get("template"),
    }), 200


@data_upload_bp.route("/data-upload/<upload_uuid>/confirm", methods=["POST"])
@cross_origin()
@require_auth
def confirm_upload(upload_uuid):
    """
    Step 2 — User confirmed metadata. Start background Zenodo upload.
    Poll /progress for status.
    """
    info = _load_info(upload_uuid)
    if not info:
        return jsonify({"error": "Upload session not found."}), 404

    req_data = request.get_json(silent=True) or {}
    metadata = req_data.get("metadata")
    if not metadata:
        return jsonify({"error": "No metadata provided."}), 400

    info["status"] = "uploading"
    info["progress"] = "Starting..."
    _save_info(upload_uuid, info)

    threading.Thread(
        target=_run_upload, args=(upload_uuid, metadata), daemon=True
    ).start()

    return jsonify({"status": "uploading"}), 202


@data_upload_bp.route("/data-upload/<upload_uuid>/progress", methods=["GET"])
@cross_origin()
@require_auth
def upload_progress(upload_uuid):
    """Poll upload status."""
    info = _load_info(upload_uuid)
    if not info:
        return jsonify({"error": "Not found."}), 404
    return jsonify({
        "status": info.get("status"),
        "progress": info.get("progress", ""),
        "dois": info.get("dois", []),
    }), 200
