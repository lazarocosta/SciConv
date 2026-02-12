from __future__ import annotations

import copy
import json
import os
import re
import shutil
import zipfile
from typing import Any, Dict, List, Optional, Union, IO, Tuple
import requests
from PyPDF2 import PdfReader
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from helpers.article.metadata_template import ZENODO_METADATA_TEMPLATE
from helpers.index import appendMessage, callGPTModel, _extract_json_safe

ZENODO_API_BASE = "https://zenodo.org/api"
BASE = "https://www.f-uji.net"

ZENODO_REF_PATTERN = re.compile(
    r"(10\.5281\/zenodo\.\d+|https?:\/\/(?:www\.)?zenodo\.org\/records\/\d+|https?:\/\/doi\.org\/10\.5281\/zenodo\.\d+)",
    re.IGNORECASE
)

def _extract_zenodo_identifier(input_str: str):
    import re
    from urllib.parse import urlparse

    if not input_str:
        return {"record_id": None, "doi": None}

    s = input_str.strip()

    # 1) DOI anywhere in the string (highest priority)
    doi_match = re.search(r"(10\.5281\/zenodo\.\d+)", s, re.IGNORECASE)
    if doi_match:
        return {"record_id": None, "doi": doi_match.group(1)}

    # 2) Zenodo record URL (records or record)
    if s.startswith("http://") or s.startswith("https://"):
        parsed = urlparse(s)
        parts = [p for p in parsed.path.split("/") if p]

        for i, p in enumerate(parts):
            if p in ("record", "records") and i + 1 < len(parts):
                rid = parts[i + 1]
                if rid.isdigit():
                    return {"record_id": rid, "doi": None}

    # 3) Raw numeric record ID
    if re.fullmatch(r"\d+", s):
        return {"record_id": s, "doi": None}

    # 4) Raw DOI (without URL)
    if s.startswith("10.5281/zenodo."):
        return {"record_id": None, "doi": s}

    # 5) Nothing usable found
    return {"record_id": None, "doi": None}


def run_fuji_fair_assessment(article_uuid: str, doi: str) -> dict:
    """
    Run a F-UJI FAIR assessment for a given DOI, save full JSON, and also
    compute + save a compact summary with:
      - maturity.FAIR
      - score_percent.FAIR
      - score_earned.FAIR, score_total.FAIR, diff

    Saves:
      articles/<uuid>/fuji_result.json
      articles/<uuid>/fuji_summary.json

    Returns:
      {
        "doi": ...,
        "fuji_summary": {...},
        "fuji_result": {...}
      }
    """

    BASE = "https://www.f-uji.net"
    session = requests.Session()

    # Stable session cookie (F-UJI is session-based)
    session.cookies.set(
        "PHPSESSID",
        article_uuid,
        domain="www.f-uji.net",
        path="/"
    )

    headers = {
        "accept": "*/*",
        "accept-language": "pt-PT,pt;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
        "referer": f"{BASE}/index.php",
    }

    data = {
        "pid": doi,
        "service_url": "",
        "service_type": "oai_pmh",
        "use_datacite": "true",
        "enable_cache": "false",
        "metric_id": "metrics_v0.8",
    }

    # 1) Start assessment
    r1 = session.post(f"{BASE}/inc_result.php", headers=headers, data=data, timeout=60)
    if r1.status_code != 200:
        raise RuntimeError(f"F-UJI assessment start failed (status={r1.status_code})")

    # 2) Export JSON
    r2 = session.get(f"{BASE}/export.php", headers={"referer": f"{BASE}/index.php"}, timeout=60)
    if r2.status_code != 200:
        raise RuntimeError(f"F-UJI export failed (status={r2.status_code})")

    content_type = r2.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise RuntimeError(f"F-UJI export did not return JSON (content-type={content_type})")

    fuji_result: Dict[str, Any] = r2.json()

    # ---------- Save full result ----------
    base_dir = os.path.join("articles", article_uuid)
    os.makedirs(base_dir, exist_ok=True)

    full_path = os.path.join(base_dir, "fuji_result.json")
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(fuji_result, f, indent=2, ensure_ascii=False)

    # ---------- Extract requested fields ----------
    summary = fuji_result.get("summary", {}) or {}

    score_earned = summary.get("score_earned", {}) or {}
    score_total = summary.get("score_total", {}) or {}
    score_percent = summary.get("score_percent", {}) or {}
    maturity = summary.get("maturity", {}) or {}

    def _num(x, default=0.0) -> float:
        try:
            if x is None:
                return float(default)
            return float(x)
        except Exception:
            return float(default)

    # ---------- Extract WARNING messages grouped by FAIR dimension ----------
    warnings_by_dimension = {
        "findable": [],
        "accessible": [],
        "interoperable": [],
        "reusable": [],
    }

    # Skip dimensions that are already perfect (100%)
    skip_dims = set()
    if _num(score_percent.get("F")) == 100:
        skip_dims.add("findable")
    if _num(score_percent.get("A")) == 100:
        skip_dims.add("accessible")
    if _num(score_percent.get("I")) == 100:
        skip_dims.add("interoperable")
    if _num(score_percent.get("R")) == 100:
        skip_dims.add("reusable")

    results = fuji_result.get("results", []) or []

    for metric in results:
        metric_id = (metric.get("metric_identifier") or "").strip()

        if metric_id.startswith("FsF-F"):
            category = "findable"
        elif metric_id.startswith("FsF-A"):
            category = "accessible"
        elif metric_id.startswith("FsF-I"):
            category = "interoperable"
        elif metric_id.startswith("FsF-R"):
            category = "reusable"
        else:
            continue

        # 🚫 Skip warnings if this dimension has 100%
        if category in skip_dims:
            continue

        debug_lines = metric.get("test_debug", []) or []
        for line in debug_lines:
            if isinstance(line, str) and line.startswith("WARNING:"):
                cleaned = line[len("WARNING:"):].strip()
                warnings_by_dimension[category].append(
                    f"{metric_id}: {cleaned}"
                )

    # Remove duplicates, keep order
    def _dedupe(seq):
        seen = set()
        out = []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    for k in warnings_by_dimension:
        warnings_by_dimension[k] = _dedupe(warnings_by_dimension[k])

    def _num(x, default=0.0) -> float:
        try:
            if x is None:
                return float(default)
            return float(x)
        except Exception:
            return float(default)

    # -------- PER-ELEMENT SCORES --------
    score_by_element = {}

    ALLOWED_KEYS = {"A", "F", "I", "R", "FAIR"}

    # Union of all keys that appear anywhere
    all_keys = set(score_earned) | set(score_total) | set(score_percent)
    for key in sorted(all_keys):
        if key not in ALLOWED_KEYS:
            continue
        earned = _num(score_earned.get(key))
        total = _num(score_total.get(key))
        percent = _num(score_percent.get(key))

        score_by_element[key] = {
            "earned": earned,
            "total": total,
            "missing": max(total - earned, 0),
            "percent": percent}

    # -------- FINAL SUMMARY --------
    fuji_summary = {
        # High-level FAIR indicators
        "maturity_fair": _num(maturity.get("FAIR")),
        "score_percent_fair": _num(score_percent.get("FAIR")),

        # Full per-element breakdown (THIS is what you asked for)
        "score_by_element": score_by_element,
        "warnings_by_dimension": warnings_by_dimension
    }

    # ---------- Save summary ----------
    summary_path = os.path.join(base_dir, "fuji_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(fuji_summary, f, indent=2, ensure_ascii=False)

    # Return both (you can choose to return only summary if you prefer)
    return {
        "fuji_summary": fuji_summary,
    }


def _zenodo_fetch_by_id(record_id: str, token: str | None = None):
    url = f"{ZENODO_API_BASE}/records/{record_id}"
    params = {}
    if token:
        params["access_token"] = token
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _zenodo_fetch_by_doi(doi: str, token: str | None = None):
    url = f"{ZENODO_API_BASE}/records"
    params = {"q": f'doi:"{doi}"'}
    if token:
        params["access_token"] = token
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    hits = r.json().get("hits", {}).get("hits", [])
    if not hits:
        raise ValueError(f"No Zenodo record found for DOI {doi}")
    return hits[0]


def check_zenodo_metadata(identifier: str, article_uuid: str, api_token: str | None = None) -> dict:
    """
    Fetch a Zenodo record by DOI / URL / ID, normalize metadata,
    and save it under articles/<article_uuid>/zenodo_metadata.json
    """

    parsed = _extract_zenodo_identifier(identifier)

    # If no token passed, try via ENV (needed only for private/drafts)
    if api_token is None:
        api_token = os.getenv("ZENODO_API_TOKEN")

    if parsed["record_id"]:
        record = _zenodo_fetch_by_id(parsed["record_id"], api_token)
    else:
        record = _zenodo_fetch_by_doi(parsed["doi"], api_token)

    metadata = record.get("metadata", {}) or {}

    # ---------- SAVE METADATA ----------
    base_dir = os.path.join("articles", article_uuid)
    os.makedirs(base_dir, exist_ok=True)

    metadata_path = os.path.join(base_dir, "zenodo_metadata.json")
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return metadata


def create_zenodo_deposition_with_files(metadata_json: dict, files: List[FileStorage]) -> dict:
    """
    Create a Zenodo deposition using a metadata dict of the form:
    {
      "metadata": {
        "title": "...",
        "upload_type": "dataset",
        ...
      }
    }
    and upload one or more files to that deposition.

    Returns the full deposition JSON returned by Zenodo.
    """

    token = os.getenv("ZENODO_API_TOKEN")
    if not token:
        raise RuntimeError("ZENODO_API_TOKEN environment variable is not set")

    # 1) Create the deposition with metadata
    create_url = f"{ZENODO_API_BASE}/deposit/depositions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    resp = requests.post(create_url, data="{}", headers=headers)
    resp.raise_for_status()
    deposition = resp.json()

    url = f"https://zenodo.org/api/deposit/depositions/{deposition.get('id')}"

    resp = requests.put(url, data=json.dumps(metadata_json), headers=headers)
    resp.raise_for_status()
    deposition = resp.json()

    # llm_data should already have the top-level "metadata" key
    # resp = requests.post(create_url, params=params, json=llm_data, timeout=30)

    # 2) Upload files into the bucket associated with the deposition
    bucket_url = deposition["links"]["bucket"]

    for f in files:
        if not f or not f.filename:
            continue

        upload_url = f"{bucket_url}/{f.filename}"
        # Zenodo requires a PUT to the bucket URL
        put_resp = requests.put(
            upload_url,
            params={"access_token": token},
            data=f.stream
        )
        put_resp.raise_for_status()

    # If you want to publish automatically, you could POST:
    publish_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}/actions/publish"
    pub_resp = requests.post(publish_url, headers=headers, timeout=30)
    pub_resp.raise_for_status()
    deposition = pub_resp.json()

    return deposition


def upsert_zenodo_deposition_metadata_and_files(deposition_id: int, llm_data: dict,
                                                files: List[FileStorage], *, zenodo_token: str,
                                                replace_files: bool = False, publish: bool = False, ) -> dict:
    """
    Behavior:
      - If files is empty:
          - If deposition is DRAFT (state='inprogress'): update metadata on SAME deposition.
          - If deposition is PUBLISHED (state='done'): raise (cannot edit published); caller should request new version.
      - If files exist:
          - If an inprogress draft exists: discard it (optional safety)
          - Create new version draft from the PUBLISHED deposition (state='done')
          - Update metadata + (optionally replace files) + upload files to draft
          - Optionally publish draft (creates version 2/3/4 ... under same concept)

    Returns:
      Updated deposition JSON (draft or published depending on publish flag).
    """

    if not zenodo_token:
        raise RuntimeError("zenodo_token is required")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {zenodo_token}",
    }

    def _get(url: str) -> dict:
        r = requests.get(url, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def _post(url: str) -> dict:
        r = requests.post(url, headers=headers, timeout=60)
        r.raise_for_status()
        return r.json()

    def _delete(url: str) -> None:
        r = requests.delete(url, headers=headers, timeout=60)
        r.raise_for_status()

    def _get_dep(dep_id: int) -> dict:
        return _get(f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}")

    def _put_json(url: str, payload: dict) -> dict:
        r = requests.put(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        return r.json()

    # -----------------------------------------
    # 0) Load deposition
    # -----------------------------------------
    dep = _get_dep(deposition_id)
    state = (dep.get("state") or "").lower()

    has_files = bool(files and len(files) > 0)

    # -----------------------------------------
    # A) METADATA ONLY
    #   - done  -> actions/edit then PUT metadata (same deposition)
    #   - inprogress -> PUT metadata (same deposition)
    # -----------------------------------------
    # --- inside METADATA ONLY branch ---

    if not has_files:
        if state == "done":
            edited = _post(f"{ZENODO_API_BASE}/deposit/depositions/{deposition_id}/actions/edit")

            dep = edited if isinstance(edited, dict) and edited.get("id") else _get_dep(deposition_id)
            state = (dep.get("state") or "").lower()

            if state != "inprogress":
                raise RuntimeError(
                    f"actions/edit did not create an editable draft. "
                    f"Deposition id={deposition_id} state='{dep.get('state')}'."
                )

            dep_id = int(dep["id"])  # IMPORTANT: this is the draft id after edit
            dep_self_url = (dep.get("links") or {}).get("self") or f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}"

            updated = _put_json(dep_self_url, llm_data)

            # TODO
            publish = True

            if publish:
                pub_url = f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}/actions/publish"
                return _post(pub_url)

            return updated

        if state == "inprogress":
            dep_id = int(dep["id"])
            dep_self_url = (dep.get("links") or {}).get("self") or f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}"

            updated = _put_json(dep_self_url, llm_data)
            # TODO
            publish = True

            if publish:
                pub_url = f"{ZENODO_API_BASE}/deposit/depositions/{dep_id}/actions/publish"
                return _post(pub_url)

            return updated

        raise RuntimeError(
            "Metadata-only update requested, but deposition is not editable.\n"
            f"Deposition id={deposition_id} has state='{dep.get('state')}'."
        )

    # Now we must be on a published deposition
    if state != "done":
        raise RuntimeError(
            "To upload files as a new version, you must start from a PUBLISHED deposition (state='done').\n"
            f"Got state='{dep.get('state')}' for id={deposition_id}."
        )

    # Create new version draft under the same concept (version 2/3/4...)
    newv = _post(f"{ZENODO_API_BASE}/deposit/depositions/{deposition_id}/actions/newversion")
    latest_draft_link = (newv.get("links") or {}).get("latest_draft")
    if not latest_draft_link:
        raise RuntimeError("Zenodo did not return links.latest_draft after newversion")

    draft = _get(latest_draft_link)
    draft_id = int(draft["id"])
    draft_self_url = (draft.get("links") or {}).get("self") or f"{ZENODO_API_BASE}/deposit/depositions/{draft_id}"

    # Update metadata on the draft (then continue with replace_files/upload/publish using draft_id)
    draft = _put_json(draft_self_url, llm_data)

    # ... continue with:
    # - optional delete files on /deposit/depositions/{draft_id}/files/{file_id}
    # - upload to draft["links"]["bucket"]
    # - publish draft_id if desired

    # Optionally delete existing draft files
    if replace_files:
        for existing in (draft.get("files") or []):
            file_id = existing.get("id")
            if not file_id:
                continue
            del_url = f"{ZENODO_API_BASE}/deposit/depositions/{draft_id}/files/{file_id}"
            _delete(del_url)
        draft = _get_dep(draft_id)

    # Upload files to draft bucket
    bucket_url = (draft.get("links") or {}).get("bucket")
    if not bucket_url:
        raise RuntimeError("No bucket URL found in draft deposition links")

    for f in files:
        if not f or not f.filename:
            continue
        try:
            f.stream.seek(0)
        except Exception:
            pass

        upload_url = f"{bucket_url}/{f.filename}"
        put_resp = requests.put(
            upload_url,
            params={"access_token": zenodo_token},
            data=f.stream,
            timeout=300,
        )
        put_resp.raise_for_status()

    draft = _get_dep(draft_id)

    # Optionally publish
    if publish:
        pub_url = f"{ZENODO_API_BASE}/deposit/depositions/{draft_id}/actions/publish"
        return _post(pub_url)

    return draft


def _extract_text_from_pdf(pdf_source: Union[str, os.PathLike, IO[bytes]]) -> str:
    close_file = False
    if isinstance(pdf_source, (str, os.PathLike)):
        f = open(pdf_source, "rb")
        close_file = True
    else:
        f = pdf_source

    try:
        reader = PdfReader(f)
        pages_text = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages_text)
    finally:
        if close_file:
            f.close()


def _split_body_and_references(full_text: str):
    lower = full_text.lower()
    markers = ["\nreferences", "\nreference", "\nbibliography", "\nrefs"]

    for m in markers:
        pos = lower.find(m)
        if pos != -1:
            return full_text[:pos], full_text[pos:]

    return full_text, ""


def respond_define_next_step(messagesToUser, referencedDatasets, nonReferencedDatasets,
                             summary_prefix="I analyzed the article and identified datasets used by the authors."):
    summary = (
        f"{summary_prefix}\n"
        f"Referenced: {len(referencedDatasets)}\n"
        f"Not referenced: {len(nonReferencedDatasets)}"
    )

    instructions = (
        "How would you like to proceed?\n\n"
        "A) Infer metadata (datasets WITHOUT a reference)\n"
        "   - Use when the dataset has no DOI/URL/citation.\n"
        "   - In the UI: click \"infer\" and choose a dataset from the Non-referenced list.\n\n"
        "B) Improve metadata (datasets WITH a Zenodo reference)\n"
        "   - Use when the dataset is in the Referenced list (has Zenodo DOI/URL).\n"
        "   - In the UI: click \"improve\" and choose a dataset from the Referenced list.\n\n"
        "C) Edit dataset lists (add / update / delete)  [handled in the UI]\n"
        "   - Choose the action (add/update/delete), then choose the target list (referenced or non-referenced).\n"
        "   - Referenced entries MUST include a Zenodo DOI/URL.\n"
        "     - Fields required: dataset name + Zenodo DOI/URL\n"
        "   - Non-referenced entries:\n"
        "     - Field required: dataset name only\n\n"
        "Zenodo reference accepted formats:\n"
        "  - 10.5281/zenodo.1234567\n"
        "  - https://doi.org/10.5281/zenodo.1234567\n"
        "  - https://zenodo.org/records/1234567"
    )

    appendMessage(messagesToUser,
                  role="assistant",
                  jsonObject=True,
                  contentShort={"summary": summary,
                                "referencedDatasets": referencedDatasets,
                                "nonReferencedDatasets": nonReferencedDatasets,
                                "instructions": instructions},
                  content={"summary": summary,
                           "referencedDatasets": referencedDatasets,
                           "nonReferencedDatasets": nonReferencedDatasets,
                           "instructions": instructions},
                  )


def _deep_merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _deep_merge(out.get(k), v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)

def _filter_patch_to_template(metadata_patch: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    """
    Drop keys not present in the template (prevents hallucinated fields).
    """
    allowed_keys = set(template.keys())
    return {k: v for k, v in (metadata_patch or {}).items() if k in allowed_keys}


def _ensure_top_level_metadata(obj: Any) -> Dict[str, Any]:
    """
    Ensure {"metadata": {...}} shape.
    """
    if not isinstance(obj, dict):
        return {"metadata": {}}
    if "metadata" in obj and isinstance(obj["metadata"], dict):
        return obj
    return {"metadata": obj}


def propose_zenodo_metadata_patch_with_openai(
        *,
        zenodo_identifier: str,
        warnings_by_dimension: Dict[str, List[str]],
        current_metadata: Dict[str, Any],
        article_uuid: str,
        extra_files: Optional[List[Dict[str, Any]]] = None,
        model: str = "o4-mini",
) -> Dict[str, Any]:
    """
    Ask ChatGPT (via your callGPTModel) for a minimal Zenodo metadata PATCH.

    Inputs:
      - zenodo_identifier: DOI / record URL / record id (string)
      - warnings_by_dimension: dict with keys findable/accessible/interoperable/reusable (lists of warning strings)
      - current_metadata: current Zenodo deposit metadata (either {"metadata": {...}} or just {...})
      - article_uuid: used to list available local files under articles/<article_uuid>/
      - extra_files: optional list of extra file descriptors (filename/path/size_bytes/etc.)
      - model: OpenAI model name used by callGPTModel()

    Output:
      {"metadata": {...}}  # minimal patch, only changed/added fields, filtered to ZENODO_METADATA_TEMPLATE
    """

    # Normalize current_metadata to deposit representation
    if isinstance(current_metadata, dict) and "metadata" in current_metadata and isinstance(
            current_metadata["metadata"], dict):
        current_metadata_for_llm = current_metadata
    else:
        current_metadata_for_llm = {"metadata": current_metadata or {}}

    # File inventory for context (no file contents)
    files_in_dir = _safe_list_article_files(article_uuid)
    all_files = (extra_files or []) + files_in_dir

    system = (
        "You are a metadata engineer for Zenodo deposits.\n"
        "Goal: produce a JSON PATCH (not full replacement) to improve FAIRness.\n\n"
        "CRITICAL OUTPUT RULES:\n"
        "1) Return ONLY valid JSON.\n"
        "2) Output MUST be in Zenodo deposit PATCH form: {\"metadata\": {...}}.\n"
        "3) PATCH MUST conform to the allowed Zenodo metadata template provided.\n"
        "   - Only use keys that exist in the template.\n"
        "   - Keep value types consistent with the template.\n"
        "4) Do NOT invent DOIs/URLs, grant IDs, ORCIDs, licenses, access statements, or identifiers.\n"
        "5) Do NOT remove required fields; only add/update what is necessary.\n"
        "6) Avoid placing Creative Commons license URLs in access_conditions/description/notes.\n"
        "   - Use `license` as an ID value (e.g., \"cc-by-4.0\").\n"
        "7) If adding access rights, prefer COAR access-rights URIs.\n"
        "8) Return a MINIMAL patch: include only the fields you changed/added.\n"
    )

    user_payload = {
        "zenodo_identifier": zenodo_identifier,
        "warnings_by_dimension": warnings_by_dimension,
        "current_metadata": current_metadata_for_llm,
        "allowed_metadata_template": ZENODO_METADATA_TEMPLATE,
        "available_files_for_context": all_files,
        "task": (
            "Produce a minimal Zenodo metadata PATCH that addresses the warnings.\n"
            "Only include fields present in allowed_metadata_template.\n"
            "Return ONLY JSON in the exact shape: {\"metadata\": {...}}.\n"
        ),
        "output_example": {
            "metadata": {
                "notes": "<p>Access: open.</p>",
                "related_identifiers": [
                    {
                        "identifier": "http://purl.org/coar/access_right/c_abf2",
                        "relation": "isSupplementTo",
                        "resource_type": "other"
                    }
                ]
            }
        }
    }

    messagesToChat = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    # callGPTModel returns a STRING
    raw_text = callGPTModel(messagesToChat, modelUsed=model)

    # Parse JSON safely
    patch = _extract_json_safe(raw_text)

    # Regex fallback
    if patch is None:
        m = re.search(r"\{.*\}", raw_text or "", flags=re.DOTALL)
        if not m:
            raise ValueError("LLM did not return valid JSON.")
        patch = json.loads(m.group(0))

    # Ensure patch shape
    patch = _ensure_top_level_metadata(patch)

    if not isinstance(patch.get("metadata"), dict):
        raise ValueError("Patch must contain a dict at patch['metadata'].")

    # Hard-enforce template keys (prevents hallucinated / unsupported fields)
    patch["metadata"] = _filter_patch_to_template(patch["metadata"], ZENODO_METADATA_TEMPLATE)

    return patch


def _safe_list_article_files(article_uuid: str, max_files: int = 50) -> List[Dict[str, Any]]:
    """
    List local files under articles/<article_uuid>/.
    Only metadata about files is returned (not contents).
    """
    base_dir = os.path.join("articles", article_uuid)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(base_dir):
        return out

    for name in sorted(os.listdir(base_dir)):
        path = os.path.join(base_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
        except Exception:
            size = None
        out.append({"filename": name, "path": path, "size_bytes": size})
        if len(out) >= max_files:
            break
    return out

def fetch_doi_citation(doi: str, style: str = "apa", lang: str = "en-US") -> str:
    """
    Fetch formatted citation text from citation.doi.org.

    Example DOI: "10.5281/zenodo.18562168"
    It will be encoded as: "10.5281%2Fzenodo.18562168"
    """

    doi_encoded = quote(doi, safe="")  # converts "/" -> "%2F"

    url = f"https://citation.doi.org/format?doi={doi_encoded}&style={style}&lang={lang}"

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    return resp.text.strip()


def _extract_text_snippets_from_article_files(
        article_uuid: str,
        *,
        max_files: int = 6,
        max_chars_per_file: int = 15000,
) -> List[Dict[str, Any]]:
    """
    Extract limited text snippets from local files in articles/<article_uuid>/.
    - PDF: uses your _extract_text_from_pdf()
    - TXT/MD: reads directly
    - JSON/YAML: reads first chars (no parsing)
    For other types, just lists the file metadata without contents.
    """
    files = _safe_list_article_files(article_uuid)
    snippets: List[Dict[str, Any]] = []

    def _read_text_file(path: str) -> str:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    for f in files:
        if len(snippets) >= max_files:
            break

        name = f["filename"]
        path = f["path"]
        ext = os.path.splitext(name.lower())[1]

        try:
            if ext == ".pdf":
                # Your helper accepts file-like or path
                full = _extract_text_from_pdf(path)
                body, refs = _split_body_and_references(full)
                text = (body + "\n\n" + refs).strip()
                snippets.append({
                    "filename": name,
                    "type": "application/pdf",
                    "snippet": text[:max_chars_per_file],
                })
            elif ext in (".txt", ".md"):
                text = _read_text_file(path)
                snippets.append({
                    "filename": name,
                    "type": "text/plain",
                    "snippet": text[:max_chars_per_file],
                })
            elif ext in (".json", ".yml", ".yaml"):
                text = _read_text_file(path)
                snippets.append({
                    "filename": name,
                    "type": "text/structured",
                    "snippet": text[:max_chars_per_file],
                })
            else:
                # skip binary/unknown
                continue
        except Exception:
            continue

    return snippets


def _filter_metadata_to_template(metadata: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    """
    Keeps only keys defined in ZENODO_METADATA_TEMPLATE.
    Prevents hallucinated fields.
    """
    allowed = set(template.keys())
    return {k: v for k, v in (metadata or {}).items() if k in allowed}

def get_zenodo_metadata_payload_for_article(
    *,
    article_uuid: str,
    doi: str,
    api_token: str | None = None,
) -> dict[str, list[dict[str, dict[str, Any]]] | Any]:
    """
    Fetch Zenodo record metadata for a DOI/Zenodo identifier and return:
      payload = {"zenodo_metadata":[{"metadata": {...}}], "template": ZENODO_METADATA_TEMPLATE}
      actions = ["update metadata"]
    """
    if not article_uuid:
        raise ValueError("article_uuid is required")
    if not doi or not isinstance(doi, str):
        raise ValueError("doi is required")

    # 1) Fetch Zenodo metadata (your helper also saves under articles/<uuid>/zenodo_metadata.json)
    raw_metadata = check_zenodo_metadata(doi, article_uuid, api_token=api_token)

    # 2) Keep only keys supported by your template (prevents unsupported/hallucinated fields in UI)
    filtered = _filter_metadata_to_template(raw_metadata or {}, ZENODO_METADATA_TEMPLATE)

    # 3) (Optional) Ensure some required basics exist for UI consistency
    #    If Zenodo returns them, they’ll already be present.
    if not filtered.get("upload_type"):
        # Most Zenodo datasets have upload_type; default to dataset for your UI flow
        filtered["upload_type"] = "dataset"

    payload = {
        "zenodo_metadata": [
            {"metadata": filtered}
        ],
        "template": ZENODO_METADATA_TEMPLATE,
    }
    return payload

def _list_local_article_files(article_uuid: str) -> List[str]:
    """
    Collects file paths under articles/<article_uuid>/ excluding known non-artifact files.
    Tune the exclusions to your project.
    """
    root_dir = os.path.join("articles", article_uuid)
    if not os.path.isdir(root_dir):
        return []

    excluded_names = {
        "fuji_result.json",
        "metadata.json",
    }
    excluded_dirs = {
        "__pycache__",
        ".git",
        ".idea",
        ".vscode",
    }

    collected: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in excluded_dirs]

        for fn in filenames:
            if fn in excluded_names:
                continue
            full = os.path.join(dirpath, fn)
            if os.path.isfile(full):
                collected.append(full)

    return collected

def _safe_extract_zip(zip_path: str, dest_dir: str) -> List[str]:
    """
    Extract zip into dest_dir safely (prevents Zip Slip).
    Returns list of extracted file paths.
    """
    extracted_paths: List[str] = []
    dest_dir_abs = os.path.abspath(dest_dir)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Skip directories
            if member.is_dir():
                continue

            # Normalize the target path
            member_path = member.filename.replace("\\", "/")
            target_path = os.path.abspath(os.path.join(dest_dir, member_path))

            # 🚫 Prevent Zip Slip (path traversal)
            if not target_path.startswith(dest_dir_abs + os.sep) and target_path != dest_dir_abs:
                # skip suspicious entry
                continue

            # Ensure parent dirs exist
            os.makedirs(os.path.dirname(target_path), exist_ok=True)

            # Extract file content
            with zf.open(member, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)

            extracted_paths.append(target_path)

    return extracted_paths

def _save_uploaded_files_to_article_folder(article_uuid: str, incoming_files: List[FileStorage]) -> List[str]:
    """
    Saves uploaded files into articles/<article_uuid>/.
    If a ZIP is uploaded, it is saved and then extracted into the same folder.
    Returns all saved file paths (including extracted files).
    """
    root_dir = os.path.join("articles", article_uuid)
    os.makedirs(root_dir, exist_ok=True)

    saved_paths: List[str] = []

    for f in incoming_files:
        if not f or not getattr(f, "filename", ""):
            continue

        filename = secure_filename(f.filename)
        if not filename:
            continue

        # Accept all file types (your policy)
        dest_path = os.path.join(root_dir, filename)

        # Save upload
        f.save(dest_path)
        saved_paths.append(dest_path)

        # If ZIP -> extract
        ext = os.path.splitext(filename.lower())[1]
        if ext == ".zip":
            try:
                extracted = _safe_extract_zip(dest_path, root_dir)
                saved_paths.extend(extracted)
            except Exception:
                # keep the zip saved, but don't crash the whole upload
                # (you can also raise if you prefer hard-fail)
                pass

    return saved_paths

def _paths_to_filestorage(paths: List[str]) -> List[FileStorage]:
    """
    Wrap local file paths into FileStorage objects so you can reuse
    create_zenodo_deposition_with_files(metadata_json, files).
    """
    storages: List[FileStorage] = []
    for p in paths:
        try:
            stream = open(p, "rb")
        except Exception:
            continue

        filename = os.path.basename(p)
        storages.append(FileStorage(stream=stream, filename=filename))
    return storages