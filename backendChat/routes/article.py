# routes/article.py
from __future__ import annotations

import os
import json
import re
from datetime import datetime
from werkzeug.datastructures import FileStorage

import config as cfg
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint
from flasgger import swag_from
from flask_cors import cross_origin
from auth import require_auth
from helpers.article.articleHelper import check_zenodo_metadata, create_zenodo_deposition_with_files, \
    _extract_text_from_pdf, _split_body_and_references, respond_define_next_step, \
    run_fuji_fair_assessment, upsert_zenodo_deposition_metadata_and_files, \
    _extract_text_snippets_from_article_files, _safe_list_article_files, _filter_metadata_to_template, \
    _ensure_top_level_metadata, get_zenodo_metadata_payload_for_article, _list_local_article_files, \
    _save_uploaded_files_to_article_folder, _paths_to_filestorage, fetch_doi_citation
from flask import request
from helpers.index import makeResponse, appendMessage, callGPTModel, _extract_json_safe
from helpers.article.metadata_template import ZENODO_METADATA_TEMPLATE

article_bp = Blueprint("article", __name__)


@article_bp.route("/article/upload-file", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/article/upload-article.yml")
def analyze_author_datasets_citation_status_using_gpt():
    messagesToUser = []
    messagesToChat = []
    article_uuid = datetime.now(cfg.timezone).strftime('%Y%m%d_%H%M%S')

    # ---- GET UPLOADED FILE(S) ----
    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        single = request.files.get("file")
        if single:
            uploaded_files = [single]

    if not uploaded_files:
        appendMessage(messagesToUser, "I can’t find your file", stage="Start")
        return makeResponse(messagesToUser, 400, True)

    # Remove empty filenames
    uploaded_files = [f for f in uploaded_files if f and getattr(f, "filename", "").strip()]
    if not uploaded_files:
        appendMessage(messagesToUser, "I can’t select your file", stage="Start")
        return makeResponse(messagesToUser, 400, True)

    # SAVE ALL FILES under articles/<article_uuid>/ (and extract zip if present)
    try:
        saved_paths = _save_uploaded_files_to_article_folder(article_uuid, uploaded_files)
    except Exception as e:
        appendMessage(messagesToUser, f"Failed to save uploaded files: {str(e)}", stage="Start")
        return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 500, True)

    if not saved_paths:
        appendMessage(messagesToUser, "Uploaded files were empty or invalid", stage="Start")
        return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 400, True)

    pdf_candidates = [p for p in saved_paths if isinstance(p, str) and p.lower().endswith(".pdf")]
    if not pdf_candidates:
        appendMessage(messagesToUser, "No PDF found in upload/zip to analyze.", stage="Start")
        return makeResponse({
            "article_uuid": article_uuid,
            "used_files": [os.path.basename(p) for p in saved_paths],
            "messages": messagesToUser
        }, 400, True)

    pdf_path = pdf_candidates[0]  # pick first pdf (change if you want all PDFs)

    # ------ EXTRACT TEXT FROM SAVED PDF PATH ------
    full_text = _extract_text_from_pdf(pdf_path)
    body_text, refs_text = _split_body_and_references(full_text)

    max_chars = 30000
    body_snippet = body_text[:max_chars]
    refs_snippet = refs_text[:max_chars]

    system_prompt = {
        "role": "system",
        "content": (
            "You are an academic integrity assistant. "
            "Your task is to analyze research papers and identify datasets. "
            "For this task, a dataset is ANY author-created artifact that contains data, "
            "including raw data, processed data, experimental results, logs, tables, "
            "spreadsheets, metrics, reproducibility packages, and experiment bundles."
        )
    }

    user_prompt = {
        "role": "user",
        "content": f"""
    Identify datasets created, collected, curated, released, or newly proposed by the authors.

    IMPORTANT DEFINITION:
    In this task, a "dataset" means ANY author-created artifact that CONTAINS DATA, including:
    - Raw, processed, or derived data
    - Experimental results, outputs, metrics, logs
    - Tables or spreadsheets of results
    - Reproducibility packages, experiment bundles, replication archives
    - Containers, scripts, workflows, or configuration files ONLY IF they include data files or recorded results

    IGNORE well-known public datasets (e.g., MNIST, CIFAR-10, COCO, UCI, ImageNet), unless the authors created a NEW dataset, subset, or derived data.

    --------------------------------
    STRICT CLASSIFICATION RULES
    --------------------------------

    You MUST classify datasets into EXACTLY ONE of the following categories.

    1) author_datasets_with_references

    Include a dataset here ONLY IF at least ONE of the following is TRUE:
    - The dataset has an EXPLICIT and RESOLVABLE URL, DOI, or repository link written in the paper body
    - The dataset is clearly linked to a SPECIFIC bibliographic entry in the REFERENCES section
    - The reference is NOT a placeholder, NOT an example, and NOT a template

    IMPORTANT:
    - If you CANNOT point to a real URL, DOI, repository link, or a concrete REFERENCES entry,
      the dataset MUST NOT be classified as referenced.

    For each referenced dataset, you MUST return:
    - dataset_name
    - reference_or_link (URL, DOI, or full reference entry)

    2) author_datasets_without_references

    Include a dataset here if:
    - The dataset is described or named by the authors
    - BUT there is NO explicit URL, DOI, or repository link
    - AND there is NO concrete matching entry in the REFERENCES section

    --------------------------------
    CRITICAL VALIDATION RULES (DO NOT VIOLATE)
    --------------------------------

    - A dataset name alone does NOT count as a reference
    - Titles that *sound like datasets* (e.g., "X: A Dataset for Y") are NOT references by themselves
    - Phrases like "we introduce a dataset called X" WITHOUT a link or citation are NOT references
    - If you are UNSURE whether a reference is valid, classify the dataset as WITHOUT reference
    - DO NOT infer, assume, or invent references

    --------------------------------
    OUTPUT RULES
    --------------------------------

    - Use the dataset’s explicit name if available; otherwise use the most specific descriptive name
    - Only include datasets attributable to the authors of THIS paper
    - If no valid author-created datasets exist, return empty arrays
    - Return ONLY valid JSON
    - DO NOT add explanations, comments, or extra text

    --------------------------------
    RETURN FORMAT (STRICT)
    --------------------------------

    {{
      "author_datasets_with_references": [
        {{
          "dataset_name": "",
          "reference_or_link": ""
        }}
      ],
      "author_datasets_without_references": [
        {{
          "dataset_name": ""
        }}
      ]
    }}

    --------------------------------
    BODY TEXT:
    {body_snippet}

    --------------------------------
    REFERENCES:
    {refs_snippet}
    """
    }

    # # ---- CALL GPT ----
    raw_response = callGPTModel([system_prompt, user_prompt])

    # ---- JSON EXTRACTION ----
    data = _extract_json_safe(raw_response)
    # todo delete
    # data = {}

    # Normalize to always produce arrays
    raw_with_refs = (data or {}).get("author_datasets_with_references", []) or []
    raw_without_refs = (data or {}).get("author_datasets_without_references", []) or []

    referenced = []
    for item in raw_with_refs:
        if isinstance(item, dict):
            name = str(item.get("dataset_name", "")).strip()
            ref = str(item.get("reference_or_link", "")).strip()
            referenced.append(f"{name} | {ref}".strip(" |"))

    non_referenced = []
    for item in raw_without_refs:
        if isinstance(item, dict):
            non_referenced.append(str(item.get("dataset_name", "")).strip())
        else:
            non_referenced.append(str(item).strip())

    # If extraction failed, keep empty lists and show an error summary
    if data is None:
        summary_prefix = "I couldn't extract dataset information from the article. Please try another PDF."
    else:
        summary_prefix = "I found datasets in the article."

    # TODO delete
    # summary_prefix = "I found datasets in the article."
    # referenced = [
    #     "Curated dataset of 18 computational experiments (E1-E18) | https://doi.org/10.5281/zenodo.15492423",
    #     "Reproducibility package of a curated dataset of 18 computational experiments | https://doi.org/10.5281/zenodo.15166258"
    # ]
    # non_referenced = ["new dataset"]
    # # TODO    END

    actions = ["infer", "improve", "add", "update", "delete"]

    # ---- BUILD CLIENT-COMPATIBLE CHAT RESPONSE (SINGLE MESSAGE) ----
    respond_define_next_step(
        messagesToUser,
        referenced,
        non_referenced,
        summary_prefix=summary_prefix
    )

    return makeResponse({
        "article_uuid": article_uuid,
        "actions": actions,
        "messages": messagesToUser,
    }, 200, True)


# print("WITH REFERENCES:", result["author_datasets_with_references"])
# print("WITHOUT REFERENCES:", result["author_datasets_without_references"])

# {
#   "author_datasets_with_references": [
#     {
#       "dataset_name": "HospitalX-CXR Collection",
#       "reference_or_link": "Silva et al. Protocols for Chest X-ray Acquisition in Regional Hospitals. 2021."
#     }
#   ],
#   "author_datasets_without_references": [
#     {
#       "dataset_name": "the CT scan dataset"
#     }
#   ]
# }

# artigo rep sem referencia dos 2 zenodos
# {
#     "role": "assistant",
#     "jsonObject": true,
#     "contentShort": {
#         "summary": "I found datasets in the article.\nReferenced: 0\nNot referenced: 1",
#         "referencedDatasets": [],
#         "nonReferencedDatasets": [
#             "CompRep: A Dataset For Computational Reproducibility"
#         ]
#     },
#     "content": {
#         "summary": "I found datasets in the article.\n\nReferenced datasets: 0\nDatasets without references: 1",
#         "referencedDatasets": [],
#         "nonReferencedDatasets": [
#             "CompRep: A Dataset For Computational Reproducibility"
#         ]
#     },
# }


# artigo rep com as 2 referencias
# [
#     {
#         "role": "assistant",
#         "jsonObject": true,
#         "contentShort": {
#             "summary": "I found datasets in the article.\nReferenced: 2\nNot referenced: 0",
#             "referencedDatasets": [
#                 "Curated dataset of 18 computational experiments (E1-E18) | https://doi.org/10.5281/zenodo.15492423",
#                 "Reproducibility package of a curated dataset of 18 computational experiments | https://doi.org/10.5281/zenodo.15166258"
#             ],
#             "nonReferencedDatasets": []
#         },
#         "content": {
#             "summary": "I found datasets in the article.\n\nReferenced datasets: 2\nDatasets without references: 0",
#             "referencedDatasets": [
#                 "Curated dataset of 18 computational experiments (E1-E18) | https://doi.org/10.5281/zenodo.15492423",
#                 "Reproducibility package of a curated dataset of 18 computational experiments | https://doi.org/10.5281/zenodo.15166258"
#             ],
#             "nonReferencedDatasets": []
#         },
#     }
# ]


@article_bp.route("/article/<article_uuid>/infer-metadata", methods=["POST"])
@cross_origin()
@require_auth
@swag_from("../swagger/article/infer-dataset-metadata.yml")
def infer_dataset_metadata_from_article(article_uuid: str):
    """
    Adds retry validation:
      - Call LLM up to 3 times until JSON is valid AND matches required schema:
          {"metadata_list":[{"metadata":{...}}, ...]}
      - If still invalid after retries -> return error
    Also keeps:
      - LLM uses "<TOBeFilledByUser>" when unsure
      - Python converts placeholders into {"_tobefilledbyuser":true,...} with options for license.
    """
    # TODO DO
    TOBE = "<TOBeFilledByUser>"
    MAX_TRIES = 3

    messagesToUser: List[Dict[str, Any]] = []
    data = request.get_json(silent=True) or {}

    datasets = data.get("datasets") or []
    max_return = int(data.get("max_return") or 5)

    # 1) Gather ALL files
    files_inventory = _safe_list_article_files(article_uuid)
    if not files_inventory:
        appendMessage(
            messagesToUser,
            f"No local files found under articles/{article_uuid}/. Upload files first."
        )
        return makeResponse(messagesToUser, 400, True)

    # 2) Extract snippets
    snippets = _extract_text_snippets_from_article_files(
        article_uuid,
        max_files=6,
        max_chars_per_file=40000
    )

    # -----------------------------
    # Snippet-based license helpers (robust for "snippet"/"text")
    # -----------------------------
    def _snippets_text() -> str:
        parts: List[str] = []
        for s in snippets or []:
            txt = s.get("snippet")
            if not isinstance(txt, str) or not txt.strip():
                txt = s.get("text")
            if isinstance(txt, str) and txt.strip():
                parts.append(txt)
        return "\n\n".join(parts)

    def _has_clear_open_sharing_evidence(text: str) -> bool:
        t = (text or "").lower()
        markers = [
            "creative commons", "cc-by", "cc by", "cc0", "public domain",
            "mit license", "apache license", "gnu general public license", "gpl",
            "bsd license", "mozilla public license", "mpl",
            "released under", "distributed under the terms of",
            "this dataset is licensed under", "this software is licensed under",
            "open access", "openly available",
        ]
        return any(m in t for m in markers)

    LICENSE_CANDIDATES = [
        "cc-by-4.0", "cc0-1.0", "mit", "apache-2.0",
        "gpl-3.0-only", "gpl-2.0-only", "bsd-3-clause-lbnl", "bsd-2-clause", "mpl-2.0", "cc-by-nc-nd-4.0",
    ]

    def _extract_explicit_license_hint(text: str) -> str:
        t = (text or "")
        for c in LICENSE_CANDIDATES:
            if c in t:
                return c
        return ""

    snippet_text_all = _snippets_text()
    open_evidence = _has_clear_open_sharing_evidence(snippet_text_all)
    explicit_license_hint = _extract_explicit_license_hint(snippet_text_all)

    # -----------------------------
    # Template-driven TOBE objects
    # -----------------------------
    def _is_placeholder(v: Any) -> bool:
        return isinstance(v, str) and v.strip() == TOBE

    def _make_tobe_object(field: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        obj: Dict[str, Any] = {"_tobefilledbyuser": True, "field": field}

        enum_vals = spec.get("enum")
        if isinstance(enum_vals, list) and enum_vals:
            obj["allowed_values"] = enum_vals

        fmt = spec.get("format")
        if isinstance(fmt, str) and fmt:
            if fmt == "date":
                obj["expected_format"] = "YYYY-MM-DD"
                obj["example"] = "2025-01-31"
            else:
                obj["expected_format"] = fmt

        if "required_if" in spec:
            obj["required_if"] = spec["required_if"]

        if "allowed_values" in obj:
            obj["message"] = "Select one of the allowed values."
        elif "expected_format" in obj:
            obj["message"] = "Fill in the value using the expected format."
        else:
            obj["message"] = "Fill in this field."

        return obj

    def _ensure_required_fields(md: Dict[str, Any]) -> Dict[str, Any]:
        md["upload_type"] = "dataset"

        # title
        if not isinstance(md.get("title"), str) or not md["title"].strip() or _is_placeholder(md["title"]):
            if datasets and isinstance(datasets[0], str) and datasets[0].strip():
                md["title"] = datasets[0].strip()
            else:
                md["title"] = _make_tobe_object("title", ZENODO_METADATA_TEMPLATE.get("title", {}))

        # publication_date
        pd = md.get("publication_date")
        if (not isinstance(pd, str) or not pd.strip() or _is_placeholder(pd)):
            md["publication_date"] = _make_tobe_object(
                "publication_date", ZENODO_METADATA_TEMPLATE.get("publication_date", {})
            )

        # description
        desc = md.get("description")
        if (not isinstance(desc, str) or not desc.strip() or _is_placeholder(desc)):
            md["description"] = _make_tobe_object("description", ZENODO_METADATA_TEMPLATE.get("description", {}))

        # creators
        creators = md.get("creators")
        if not isinstance(creators, list) or len(creators) == 0:
            md["creators"] = [
                {
                    "_tobefilledbyuser": True,
                    "field": "creators[0].name",
                    "required_fields": ["name"],
                    "optional_fields": ["affiliation", "orcid", "gnd"],
                    "message": "At least one creator is required."
                }
            ]
        else:
            cleaned = []
            for c in creators:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                if not isinstance(name, str) or not name.strip() or _is_placeholder(name):
                    c["name"] = TOBE
                for k in ("orcid", "gnd"):
                    if k in c and (not isinstance(c[k], str) or not c[k].strip() or _is_placeholder(c[k])):
                        c.pop(k, None)
                cleaned.append(c)
            md["creators"] = cleaned if cleaned else [{"name": TOBE}]

            fixed = []
            for i, c in enumerate(md["creators"]):
                if isinstance(c, dict) and _is_placeholder(c.get("name")):
                    fixed.append({
                        "_tobefilledbyuser": True,
                        "field": f"creators[{i}].name",
                        "required": True,
                        "message": "Fill creator name (e.g., 'Last, First')."
                    })
                else:
                    fixed.append(c)
            md["creators"] = fixed

        # access_right (required enum)
        ar = md.get("access_right")
        if not isinstance(ar, str) or not ar.strip() or _is_placeholder(ar):
            md["access_right"] = _make_tobe_object("access_right", ZENODO_METADATA_TEMPLATE.get("access_right", {}))

        return md

    def _enforce_access_right_and_license(md: Dict[str, Any]) -> Dict[str, Any]:
        ar = md.get("access_right")

        # access_right unresolved -> suggest license candidates conditionally
        if isinstance(ar, dict) and ar.get("_tobefilledbyuser") is True:
            if _is_placeholder(md.get("license")) or "license" not in md:
                md["license"] = {
                    "_tobefilledbyuser": True,
                    "field": "license",
                    "message": "Select a license ID if you choose access_right=open/embargoed.",
                    "required_if": {"access_right": ["open", "embargoed"]},
                    "possible_solutions": (
                                              [explicit_license_hint] if explicit_license_hint else []) + LICENSE_CANDIDATES
                }
            return md

        ar_norm = ar.strip() if isinstance(ar, str) else ""

        # prevent hallucinated "open"
        if ar_norm == "open" and not open_evidence:
            ar_norm = "restricted"
            md["access_right"] = "restricted"

        if ar_norm not in ("open", "embargoed", "restricted", "closed"):
            md["access_right"] = "restricted"
            ar_norm = "restricted"

        if ar_norm in ("open", "embargoed"):
            lic = md.get("license")

            # --- Normalize license ---
            if isinstance(lic, str):
                lic = lic.strip().lower()

            # --- If explicit license exists in snippets and model didn't give it, prefer explicit ---
            if (not isinstance(lic, str) or not lic) and explicit_license_hint:
                md["license"] = explicit_license_hint
                return md

            # --- Validate license against allowed list ---
            if isinstance(lic, str):

                # license provided but not allowed
                if lic not in LICENSE_CANDIDATES:

                    if explicit_license_hint and explicit_license_hint in LICENSE_CANDIDATES:
                        md["license"] = explicit_license_hint
                    else:
                        md["license"] = {
                            "_tobefilledbyuser": True,
                            "field": "license",
                            "message": "License must be one of the allowed values.",
                            "required": True,
                            "allowed_values": LICENSE_CANDIDATES
                        }

                    return md

            # --- License missing or placeholder ---
            if (not isinstance(lic, str) or not lic or _is_placeholder(lic)):
                md["license"] = {
                    "_tobefilledbyuser": True,
                    "field": "license",
                    "message": "License is required when access_right is open/embargoed.",
                    "required": True,
                    "allowed_values": LICENSE_CANDIDATES
                }
        else:
            md.pop("license", None)
            md.pop("embargo_date", None)
            md.pop("access_conditions", None)

        return md

    def _remove_placeholder_identifiers(md: Dict[str, Any]) -> Dict[str, Any]:
        if "doi" in md and (_is_placeholder(md["doi"]) or (isinstance(md["doi"], str) and not md["doi"].strip())):
            md.pop("doi", None)
        return md

    # -----------------------------
    # LLM prompt + retry validator
    # -----------------------------
    system = (
        "You are a research data curator creating Zenodo deposit metadata.\n"
        "You will be given an article folder with files inventory + text snippets.\n"
        "Goal: infer metadata objects suitable to CREATE Zenodo deposits.\n\n"
        "STRICT RULES:\n"
        "- Return ONLY valid JSON.\n"
        "- Output MUST be exactly: {\"metadata_list\": [ {\"metadata\": {...}}, ... ]}\n"
        "- Each item MUST follow allowed_metadata_template keys/types.\n"
        "- Do NOT invent DOIs/URLs/ORCIDs/licenses.\n"
        "- If you are NOT SURE about an IMPORTANT field, set it to the literal string \"<TOBeFilledByUser>\".\n"
        "- access_right: use \"open\" ONLY if text clearly indicates open sharing; otherwise \"restricted\".\n"
        "- If access_right is open/embargoed and license not explicitly stated, use \"<TOBeFilledByUser>\".\n"
    )

    user_payload = {
        "article_uuid": article_uuid,
        "datasets_hint": datasets,
        "max_return": max_return,
        "allowed_metadata_template": ZENODO_METADATA_TEMPLATE,
        "files_inventory": files_inventory,
        "text_snippets": snippets,
        "required_output_schema": {
            "metadata_list": [
                {"metadata": {"title": "", "upload_type": "dataset", "publication_date": "", "description": "",
                              "creators": [], "access_right": ""}}
            ]
        }
    }

    def _try_parse_required_schema(raw_text: str) -> Optional[Dict[str, Any]]:
        """
        Returns parsed dict if it matches required schema, else None.
        Required schema:
          - dict with key "metadata_list" as list
          - each item is dict with key "metadata" as dict
        """
        parsed_local = _extract_json_safe(raw_text)

        if parsed_local is None:
            m = re.search(r"\{.*\}", raw_text or "", flags=re.DOTALL)
            if not m:
                return None
            try:
                parsed_local = json.loads(m.group(0))
            except Exception:
                return None

        if not isinstance(parsed_local, dict):
            return None
        ml = parsed_local.get("metadata_list")
        if not isinstance(ml, list):
            return None
        for it in ml:
            if not isinstance(it, dict):
                return None
            md = it.get("metadata")
            if not isinstance(md, dict):
                return None
        return parsed_local

    parsed: Optional[Dict[str, Any]] = None
    raw: str = ""

    for attempt in range(1, MAX_TRIES + 1):
        messagesToChat = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        # On retries, provide corrective instruction with the prior raw output (short)
        if attempt > 1:
            messagesToChat.append({
                "role": "user",
                "content": (
                    "Your previous output did NOT match the required schema.\n"
                    "Return ONLY valid JSON in EXACT shape:\n"
                    "{\"metadata_list\": [{\"metadata\": { ... }}]}\n"
                    "Do not add any other keys at top-level.\n"
                )
            })

        raw = callGPTModel(messagesToChat)
        parsed = _try_parse_required_schema(raw)
        # parsed= {
        #     "metadata_list": [
        #         {
        #             "metadata": {
        #                 "upload_type": "dataset",
        #                 "title": "The \"Podcast\" ECoG dataset",
        #                 "creators": [
        #                     {
        #                         "name": "Zaid Zada",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Samuel A. Nastase",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Bobbi Aubrey",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Itamar Jalon",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Ariel Goldstein",
        #                         "affiliation": "Department of Cognitive and Brain Sciences and Business School, Hebrew University; Jerusalem, 9190501, Israel."
        #                     },
        #                     {
        #                         "name": "Sebastian Michelmann",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Haocheng Wang",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Liat Hasenfratz",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     },
        #                     {
        #                         "name": "Werner Doyle",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Daniel Friedman",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Patricia Dugan",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Lucia Melloni",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Sasha Devore",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Orrin Devinsky",
        #                         "affiliation": "Grossman School of Medicine, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Adeen Flinker",
        #                         "affiliation": "Grossman School of Medicine and Tandon School of Engineering, New York University; New York, 10016, USA."
        #                     },
        #                     {
        #                         "name": "Uri Hasson",
        #                         "affiliation": "Princeton Neuroscience Institute and Department of Psychology, Princeton University; New Jersey, 08544, USA."
        #                     }
        #                 ],
        #                 "description": "Naturalistic electrocorticography (ECoG) data are a rare but essential resource for studying the brain’s linguistic capabilities. This dataset shares recordings from nine participants (1,330 electrodes) listening to a 30-minute audio podcast (This American Life, “So a Monkey and a Horse Walk Into a Bar: Act One, Monkey in the Middle”). It includes raw and high-gamma band preprocessed ECoG data, auditory stimuli (audio files and aligned word-level transcript), and extracted linguistic features ranging from spectrotemporal properties to phonetic, syntactic, non-contextual and contextual word embeddings. Detailed tutorials are provided for preprocessing, feature extraction, and encoding analyses. The dataset follows BIDS-iEEG standards and is available under CC0 license on OpenNeuro (doi:10.18112/openneuro.ds005574.v1.0.2).",
        #                 "access_right": "open",
        #                 "license": "CC0",
        #                 "doi": "doi:10.18112/openneuro.ds005574.v1.0.2",
        #                 "keywords": [
        #                     "ECoG",
        #                     "electrocorticography",
        #                     "language comprehension",
        #                     "naturalistic stimulus",
        #                     "podcast",
        #                     "neuroscience",
        #                     "encoding models",
        #                     "linguistic features"
        #                 ],
        #                 "references": [
        #                     "Honey et al. (2012). Neuron 76(2):423–434.",
        #                     "Zada et al. (2025). bioRxiv 2025.02.14.638352"
        #                 ],
        #                 "related_identifiers": [
        #                     {
        #                         "identifier": "https://hassonlab.github.io/podcast-ecog-tutorials",
        #                         "relation": "isSupplementTo",
        #                         "resource_type": "software"
        #                     },
        #                     {
        #                         "identifier": "https://github.com/hassonlab/podcast-ecog-paper",
        #                         "relation": "isSupplementTo",
        #                         "resource_type": "software"
        #                     }
        #                 ]
        #             }
        #         }
        #     ]
        # }
        if parsed is not None:
            break

    if parsed is None:
        appendMessage(
            messagesToUser,
            "LLM did not return the required JSON schema after 3 attempts.",
        )
        return makeResponse(messagesToUser, 500, True)

    # 5) Normalize + filter + enrich placeholders
    out_list: List[Dict[str, Any]] = []
    for item in parsed["metadata_list"][:max_return]:
        item = _ensure_top_level_metadata(item)
        md = item.get("metadata", {}) if isinstance(item, dict) else {}
        if not isinstance(md, dict):
            continue

        md = _filter_metadata_to_template(md, ZENODO_METADATA_TEMPLATE)

        if not md.get("upload_type"):
            md["upload_type"] = "dataset"

        md = _ensure_required_fields(md)
        md = _enforce_access_right_and_license(md)
        md = _remove_placeholder_identifiers(md)

        out_list.append({"metadata": md})

    payload = {
        "zenodo_metadata": out_list[:max_return],
        "template": ZENODO_METADATA_TEMPLATE

    }
    # payload = {
    #     "zenodo_metadata": [
    #         {
    #             "metadata": {
    #                 "upload_type": "dataset",
    #                 "publication_type": "conference paper",
    #                 "publication_date": "2025-07-29",
    #                 "title": "CompRep: A Dataset For Computational Reproducibility",
    #                 "creators": [
    #                     {
    #                         "name": "Lázaro Costa",
    #                         "affiliation": "University of Porto & INESC TEC, Portugal"
    #                     },
    #                     {
    #                         "name": "Susana Barbosa",
    #                         "affiliation": "INESC TEC, Portugal"
    #                     },
    #                     {
    #                         "name": "Jácome Cunha",
    #                         "affiliation": "University of Porto & HASLab/INESC TEC, Portugal"
    #                     }
    #                 ],
    #                 "description": "Reproducibility in computational science is increasingly dependent on the ability to faithfully re-execute experiments involving code, data, and software environments. However, assessing the effectiveness of reproducibility tools is difficult due to the lack of standardized benchmarks. To address this, we collected 38 computational experiments from diverse scientific domains and attempted to reproduce each using 8 different reproducibility tools. From this initial pool, we identified 18 experiments that could be successfully reproduced using at least one tool. These experiments form our curated benchmark dataset, which we release along with reproducibility packages to support ongoing evaluation efforts.",
    #                 "access_right": "open",
    #                 "keywords": [
    #                     "Reproducibility",
    #                     "Open Science",
    #                     "Empirical Evaluation",
    #                     "Dataset"
    #                 ],
    #                 "conference_title": "ACM Conference on Reproducibility and Replicability",
    #                 "conference_acronym": "ACM REP ’25",
    #                 "conference_dates": "July 29–31, 2025",
    #                 "conference_place": "Vancouver, Canada",
    #                 "imprint_publisher": "ACM",
    #                 "imprint_place": "Rennes, France",
    #                 "language": "en"
    #             }
    #         }
    #     ],
    #     "template": ZENODO_METADATA_TEMPLATE
    # }
    actions = ["create", "update metadata"]

    appendMessage(
        messagesToUser,
        role="assistant",
        jsonObject=True,
        contentShort=payload,
        content=payload
    )

    return makeResponse({
        "actions": actions,
        "messages": messagesToUser,
    }, 200, True)


@article_bp.route("/article/<article_uuid>/create-dataset-zenodo", methods=["POST"])
@cross_origin()
@require_auth
@swag_from("../swagger/article/create-dataset-zenodo.yml")
def zenodo_create_dataset_route(article_uuid):
    """
    Upload to Zenodo all files available under articles/<article_uuid>/.

    If the request includes uploaded files (pdf/zip/etc), you can choose:
      - merge: add uploaded files to existing local repository, then upload ALL local files
      - replace: ignore existing local repository and upload ONLY the received files (still saved locally)

    Control via form field:
      file_mode = "merge" (default) | "replace"
    """
    messagesToUser: List[Dict[str, Any]] = []

    # ---- CHECK METADATA JSON ----
    metadata_str = request.form.get("metadata")
    if not metadata_str:
        appendMessage(messagesToUser, "Missing 'metadata' field in form data")
        return makeResponse(messagesToUser, 400, True)

    try:
        metadata_json = json.loads(metadata_str)
    except json.JSONDecodeError:
        appendMessage(messagesToUser, "Invalid JSON in 'metadata' field")
        return makeResponse(messagesToUser, 400, True)

    if not isinstance(metadata_json, dict) or "metadata" not in metadata_json:
        appendMessage(messagesToUser, "JSON must have top-level 'metadata' key")
        return makeResponse(messagesToUser, 400, True)

    # ---- MODE: merge vs replace ----
    file_mode = (request.form.get("file_mode") or "merge").strip().lower()
    if file_mode not in ("merge", "replace"):
        file_mode = "merge"

    # ---- GET UPLOADED FILES (IF ANY) ----
    uploaded_files = request.files.getlist("files") or []
    if not uploaded_files:
        single_file = request.files.get("file")
        if single_file:
            uploaded_files = [single_file]

    # Remove empties
    uploaded_files = [f for f in uploaded_files if f and getattr(f, "filename", "").strip()]

    local_paths: List[str] = []

    # ---- DECIDE WHAT TO UPLOAD ----
    if uploaded_files:
        # Always save incoming files to local repository (and extract zip if your helper does)
        try:
            saved_paths = _save_uploaded_files_to_article_folder(article_uuid, uploaded_files)
        except Exception as e:
            appendMessage(messagesToUser, f"Failed to save uploaded files: {str(e)}")
            return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 500, True)

        if not saved_paths:
            appendMessage(messagesToUser, "Uploaded files were empty or invalid")
            return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 400, True)

        if file_mode == "replace":
            # Upload ONLY what we just received/saved (including extracted files)
            local_paths = saved_paths
        else:
            # Upload ALL files now available in the article folder (existing + newly saved)
            local_paths = _list_local_article_files(article_uuid)

        if not local_paths:
            appendMessage(messagesToUser, f"No files found under articles/{article_uuid}/ after saving upload.")
            return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 400, True)

    else:
        # ALL LOCAL
        local_paths = _list_local_article_files(article_uuid)

        if not local_paths:
            appendMessage(messagesToUser, f"No files provided and no local files found under articles/{article_uuid}/")
            return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 400, True)

    # (Optional) de-duplicate paths while preserving order
    seen = set()
    deduped_paths: List[str] = []
    for p in local_paths:
        if not isinstance(p, str):
            continue
        if p not in seen:
            seen.add(p)
            deduped_paths.append(p)
    local_paths = deduped_paths

    # Wrap local paths as FileStorage so your existing helper can be reused
    files_for_zenodo: List[FileStorage] = _paths_to_filestorage(local_paths)
    if not files_for_zenodo:
        appendMessage(messagesToUser, "Could not open any files for upload")
        return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 400, True)
    zenodo_metadata = {}
    citation=""

    try:
        deposition = create_zenodo_deposition_with_files(metadata_json, files_for_zenodo)

        zenodo_metadata = deposition.get("metadata") or {}
        citation=fetch_doi_citation(zenodo_metadata.get("doi"), style="apa", lang="en-US")

    except Exception as e:
        appendMessage(messagesToUser, f"Error creating Zenodo deposition: {str(e)}")
        return makeResponse({"article_uuid": article_uuid, "messages": messagesToUser}, 500, True)
    finally:
        for fs in files_for_zenodo:
            try:
                fs.stream.close()
            except Exception:
                pass

    # zenodo_metadata = {
    #     "title": "CompRep: A Dataset For Computational Reproducibility",
    #     "doi": "10.5281/zenodo.18134102",
    #     "publication_date": "2025-07-29",
    #     "description": "Reproducibility in computational science is increasingly dependent on the ability to faithfully re-execute experiments involving code, data, and software environments. However, assessing the effectiveness of reproducibility tools is difficult due to the lack of standardized benchmarks. To address this, we collected 38 computational experiments from diverse scientific domains and attempted to reproduce each using 8 different reproducibility tools. From this initial pool, we identified 18 experiments that could be successfully reproduced using at least one tool. These experiments form our curated benchmark dataset, which we release along with reproducibility packages to support ongoing evaluation efforts.",
    #     "access_right": "open",
    #     "creators": [
    #         {
    #             "name": "L\u00e1zaro Costa",
    #             "affiliation": "University of Porto & INESC TEC, Portugal"
    #         },
    #         {
    #             "name": "Susana Barbosa",
    #             "affiliation": "INESC TEC, Portugal"
    #         },
    #         {
    #             "name": "J\u00e1come Cunha",
    #             "affiliation": "University of Porto & HASLab/INESC TEC, Portugal"
    #         }
    #     ],
    #     "keywords": [
    #         "Reproducibility",
    #         "Open Science",
    #         "Empirical Evaluation",
    #         "Dataset"
    #     ],
    #     "language": "eng",
    #     "license": "cc-zero",
    #     "imprint_publisher": "Zenodo",
    #     "upload_type": "dataset",
    #     "prereserve_doi": {
    #         "doi": "10.5281/zenodo.18134102",
    #         "recid": 18134102
    #     }}

    actions = ["go to menu", "update metadata"]

    payload = {
        "zenodo_status": "Zenodo repository created",
        "zenodo_metadata": zenodo_metadata,
        "citation": citation
    }

    appendMessage(
        messagesToUser,
        role="assistant",
        jsonObject=True,
        contentShort=payload,
        content=payload
    )

    return makeResponse({
        "actions": actions,
        "messages": messagesToUser,
    }, 200, True)


@article_bp.route("/article/<article_uuid>/zenodo/<int:deposition_id>/edit", methods=["POST"])
@cross_origin()
@require_auth
@swag_from("../swagger/article/edit-dataset-zenodo.yml")
def zenodo_edit_dataset_route(article_uuid, deposition_id):
    messagesToUser = []

    # ---- CHECK TOKEN ----
    zenodo_token = request.form.get("zenodo_token")
    token = zenodo_token or os.getenv("ZENODO_API_TOKEN")
    if not token:
        raise RuntimeError("Zenodo token not provided and ZENODO_API_TOKEN is not set")

    # ---- CHECK METADATA JSON ----
    metadata_str = request.form.get("metadata")
    if not metadata_str:
        appendMessage(messagesToUser, "Missing 'metadata' field in form data")
        return makeResponse(messagesToUser, 400, True)

    try:
        metadata_json = json.loads(metadata_str)
    except json.JSONDecodeError:
        appendMessage(messagesToUser, "Invalid JSON in 'metadata' field", )
        return makeResponse(messagesToUser, 400, True)

    if not isinstance(metadata_json, dict) or "metadata" not in metadata_json:
        appendMessage(messagesToUser, "JSON must have top-level 'metadata' key")
        return makeResponse(messagesToUser, 400, True)

    # ---- CHECK FILES (optional) ----
    files = request.files.getlist("files") or []

    single_file = request.files.get("file")
    if not files and single_file:
        files = [single_file]

    # publish new version ONLY if we actually received files
    publish = len(files) > 0

    # ---- OPTIONAL FLAGS ----
    replace_files = (request.form.get("replace_files", "false").lower() == "true")

    # ---- CALL ZENODO EDIT ----
    try:
        dep = upsert_zenodo_deposition_metadata_and_files(
            deposition_id=deposition_id,
            llm_data=metadata_json,
            files=files,
            zenodo_token=token,
            replace_files=replace_files,
            publish=publish
        )
    except Exception as e:
        appendMessage(messagesToUser, f"Error editing Zenodo deposition: {str(e)}")
        return makeResponse(messagesToUser, 500, True)

    return makeResponse({"deposition": dep}, 200, True)


@article_bp.route("/article/<article_uuid>/metadata", methods=["GET"])
@cross_origin()
@require_auth
# @swag_from("../swagger/article/metadata.yml")
def get_metadata_from_article(article_uuid: str):
    """
    GET /article/<article_uuid>/metadata?doi=<doi-or-zenodo-url-or-doi-url>

    Returns UI payload:
      - zenodo_metadata list (in your expected format)
      - template to follow
      - actions user can do next
    """
    messagesToUser: List[Dict[str, Any]] = []

    doi = (request.args.get("doi") or "").strip()

    if not doi:
        appendMessage(messagesToUser, "Missing required query param: doi. Example: ?doi=10.5281/zenodo.1234567", )
        return makeResponse({"messages": messagesToUser}, 400, True)

    try:
        payload = get_zenodo_metadata_payload_for_article(
            article_uuid=article_uuid,
            doi=doi,
            api_token=None,
        )
    except Exception as e:
        appendMessage(messagesToUser, f"Failed to fetch Zenodo metadata: {str(e)}")
        return makeResponse({"messages": messagesToUser}, 500, True)

    appendMessage(
        messagesToUser,
        role="assistant",
        jsonObject=True,
        contentShort=payload,
        content=payload,
    )
    actions = ["go to menu", "update metadata"]
    return makeResponse({"actions": actions, "messages": messagesToUser, }, 200, True, )


# TODO
# TO BE DELETED tem o fugi
@article_bp.route("/article/<article_uuid>/choose-next-step", methods=["POST"])
@cross_origin()
@require_auth
@swag_from("../swagger/article/choose_next_step.yml")
def choose_next_step(article_uuid):
    messagesToUser = []

    reference_or_link = ""
    zenodo_metadata = None
    fuji_error = ""
    fuji_result = ""

    zenodo_metadata = check_zenodo_metadata(reference_or_link, article_uuid)

    # We do NOT hard-fail if article_uuid is missing or F-UJI fails.
    if article_uuid:
        try:
            fuji_result = run_fuji_fair_assessment(article_uuid=article_uuid, doi=reference_or_link)
            fuji_path = os.path.join("articles", article_uuid, "fuji_result.json")
        except Exception as e:
            fuji_error = str(e)
            fuji_path = os.path.join("articles", article_uuid, "fuji_result.json")
    else:
        fuji_error = "Missing article_uuid in request body (required to save F-UJI result under articles/<uuid>/)."

    payload = {
        "zenodo_metadata": zenodo_metadata,
        "fuji_summary": fuji_result["fuji_summary"],
    }

    appendMessage(
        messagesToUser,
        role="assistant",
        jsonObject=True,
        contentShort=payload,
        content=payload
    )

    return makeResponse(messagesToUser, 200, True)