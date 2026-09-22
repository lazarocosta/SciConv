# routes/project.py
import os, re, json, zipfile, tempfile, shutil, subprocess, tarfile, io
from copy import copy
from datetime import datetime
from flasgger import swag_from

from flask import Blueprint, request, jsonify, send_from_directory
from flask_cors import cross_origin

import config as cfg
from auth import require_auth
from helpers.project.projectHelper import (
    find_files, read_first_50_lines, return_commands_to_use,
    write_file, startDockerClient, read_file,
    write_messagesUser_to_file, saveDockerImage
)
from helpers.index import makeResponse, appendMessage, return_messages, callGPTModel
from helpers.article.articleHelper import _extract_zenodo_identifier, _zenodo_fetch_by_id, _zenodo_fetch_by_doi
from packageExperiment.linux import writeLinuxFile
from packageExperiment.windows import writeWindowsFIle
from werkzeug.utils import secure_filename

project_bp = Blueprint("project", __name__)

# ---------------------------------------------------------------------------
# Data Extension Layer: constants
# ---------------------------------------------------------------------------
EMBED_THRESHOLD_BYTES = int(500 * 1024)          # 500 KB (temp test)
ZENODO_RECORD_LIMIT_BYTES = int(1 * 1024 * 1024) # 1 MB (temp test)


def _get_folder_size(path):
    """Calculate total size in bytes of all files under *path*."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def _detect_data_folder_with_gpt(files_location):
    """
    Scan immediate subfolders of files_location and ask GPT which one
    contains the dataset (not code). Returns the full path to the data
    folder, or None if no data folder is found.
    """
    subdirs = [e for e in os.scandir(files_location) if e.is_dir()]
    if not subdirs:
        return None

    # Build a compact summary of each subfolder
    lines = []
    for entry in subdirs:
        size_bytes = _get_folder_size(entry.path)
        size_str = (f"{round(size_bytes / (1024**3), 2)} GB"
                    if size_bytes >= 1024**3
                    else f"{round(size_bytes / (1024**2), 1)} MB"
                    if size_bytes >= 1024**2
                    else f"{round(size_bytes / 1024, 1)} KB")

        # Collect unique extensions (up to 10 samples)
        exts = set()
        samples = []
        for root, _dirs, files in os.walk(entry.path):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext:
                    exts.add(ext)
                if len(samples) < 6:
                    samples.append(fname)

        lines.append(
            f"- {entry.name}/ ({size_str}) — "
            f"extensions: {', '.join(sorted(exts)) or 'none'} — "
            f"sample files: {', '.join(samples)}"
        )

    folder_summary = "\n".join(lines)

    prompt = [
        {
            "role": "user",
            "content": (
                "A researcher uploaded a zip file containing a computational experiment. "
                "The zip has been extracted and the immediate subfolders are:\n\n"
                f"{folder_summary}\n\n"
                "Which folder contains the DATASET (raw data files, not source code)? "
                "Reply with the folder name only (e.g. 'demo_data'). "
                "If there is no data folder, reply with 'none'."
            )
        }
    ]

    try:
        result = callGPTModel(prompt).strip().strip("'\"").rstrip("/")
        print(f"  [GPT data detection] answer: {result!r}")

        if result.lower() == "none":
            return None

        candidate = os.path.join(files_location, result)
        if os.path.isdir(candidate):
            return candidate

        # GPT may have returned a slightly different name — case-insensitive match
        for entry in subdirs:
            if entry.name.lower() == result.lower():
                return entry.path

    except Exception as e:
        print(f"  [GPT data detection] failed: {e}")

    return None


# ---------------------------------------------------------------------------
# GPT helper: classify free-text data input intent (#1)
# ---------------------------------------------------------------------------

def _classify_data_input_with_gpt(text):
    """
    Classify the user's free-text reply to "Does your experiment use a dataset?".
    Returns {"intent": "no_data"|"doi"|"unclear", "doi": str|None}
    """
    prompt = [{
        "role": "user",
        "content": (
            f"A researcher was asked: 'Does your experiment use a dataset?'\n"
            f"They replied: \"{text}\"\n\n"
            "Classify their intent. Reply with exactly one of:\n"
            "  no_data           — they have no dataset\n"
            "  doi:<the_doi>     — they provided a Zenodo DOI/URL (extract just the DOI)\n"
            "  unclear           — intent cannot be determined\n\n"
            "Examples:\n"
            "  'no thanks' → no_data\n"
            "  'I don't have any data' → no_data\n"
            "  'https://zenodo.org/record/1234567' → doi:10.5281/zenodo.1234567\n"
            "  'the dataset is at 10.5281/zenodo.9999' → doi:10.5281/zenodo.9999\n"
            "  'maybe' → unclear"
        )
    }]
    try:
        result = callGPTModel(prompt).strip()
        if result.lower() == "no_data":
            return {"intent": "no_data", "doi": None}
        if result.lower().startswith("doi:"):
            return {"intent": "doi", "doi": result[4:].strip()}
        return {"intent": "unclear", "doi": None}
    except Exception as e:
        print(f"  [GPT data intent] failed: {e}")
        return {"intent": "unclear", "doi": None}


# ---------------------------------------------------------------------------
# GPT helper: detect data folder name referenced in code (#2)
# ---------------------------------------------------------------------------

_CODE_EXTENSIONS = {'.py', '.r', '.rmd', '.jl', '.ipynb', '.m', '.sh'}

def _detect_data_folder_alias(files_location):
    """
    Scan code files and ask GPT what folder name the code uses to load data.
    Returns a plain folder name string (e.g. 'mydata') or None if already
    'data', cannot be determined, or no data folder is referenced.
    """
    snippets = []
    for root, _, filenames in os.walk(files_location):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() in _CODE_EXTENSIONS:
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read(4000)
                    rel = os.path.relpath(fpath, files_location)
                    snippets.append(f"# {rel}\n{content}")
                except Exception:
                    pass
        if len(snippets) >= 5:
            break

    if not snippets:
        return None

    prompt = [{
        "role": "user",
        "content": (
            "Look at the following code files and identify the folder name used to READ or LOAD "
            "INPUT data files — such as pd.read_csv(), open() for reading, read.csv(), np.load(), etc.\n"
            "IMPORTANT: ignore folders used only for WRITING output/results (e.g. open('output/x','w'), "
            "os.makedirs('results'), savefig, to_csv for saving). Those are output folders, not input data.\n"
            "Return ONLY the input data folder name — no explanation, no quotes.\n"
            "Examples: 'mydata/file.csv' → mydata | 'raw/data.csv' → raw | './data/x.csv' → data\n"
            "If no input data folder is referenced, or it is already named 'data', return: none\n\n"
            + "\n\n".join(snippets)
        )
    }]
    try:
        result = callGPTModel(prompt).strip().strip("'\"./").lower()
        if result in ("none", "", "n/a", "no", "data"):
            return None
        # Sanitize: only plain folder name characters allowed
        if not re.match(r'^[\w.\-]+$', result):
            return None
        return result
    except Exception as e:
        print(f"  [GPT data alias] failed: {e}")
        return None


# ---------------------------------------------------------------------------
# GPT helper: scan code files for output folder (#3)
# ---------------------------------------------------------------------------

_OUTPUT_EXTENSIONS = {
    # Tabular / data
    '.csv', '.tsv', '.xlsx', '.xls', '.ods', '.parquet', '.feather', '.arrow',
    '.dta', '.sav', '.por',
    # Serialized arrays / matrices
    '.npy', '.npz', '.mat', '.h5', '.hdf5', '.nc', '.zarr',
    # ML models
    '.pt', '.pth', '.onnx', '.pb', '.tflite', '.pkl', '.pickle', '.joblib',
    # Documents / reports
    '.pdf', '.html', '.htm', '.txt', '.log', '.md', '.tex',
    # Images / figures
    '.png', '.jpg', '.jpeg', '.svg', '.eps', '.tif', '.tiff', '.gif',
    '.fig', '.ps',
    # Structured / config
    '.json', '.yaml', '.yml', '.xml', '.toml',
    # R formats
    '.rds', '.rda', '.rdata',
    # Bioinformatics
    '.bam', '.sam', '.vcf', '.bcf', '.fastq', '.fasta', '.fa', '.fq',
    '.bed', '.gff', '.gtf',
    # GIS / spatial
    '.shp', '.geojson', '.kml', '.gpkg',
    # Generic binary / compressed
    '.out', '.dat', '.bin', '.db', '.sqlite', '.gz', '.zip',
    # Video / audio (simulation outputs)
    '.mp4', '.avi', '.mov', '.wav',
}


def _scan_write_lines(files_location):
    """
    Walk all files under files_location.
    Return lines that contain a quoted string with a known output extension
    OR that look like file-write operations using variables.
    Both reads AND writes are included — GPT will distinguish them.
    """
    quoted = re.compile(r'["\']([^"\']{1,200}\.[a-zA-Z0-9]{1,6})["\']')
    # Also catch lines with path-like strings even without extensions (e.g. output dirs)
    path_like = re.compile(r'["\']([^"\']{1,100}/[^"\']{1,100})["\']')
    results = []

    for root, _dirs, filenames in os.walk(files_location):
        for fname in filenames:
            if fname.startswith('.'):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, files_location).replace("\\", "/")
            try:
                with open(fpath, 'r', encoding='utf-8', errors='ignore') as fh:
                    for line_no, line in enumerate(fh, 1):
                        added = False
                        # Primary: quoted string with known output extension
                        for match in quoted.findall(line):
                            ext = os.path.splitext(match)[1].lower()
                            if ext in _OUTPUT_EXTENSIONS:
                                results.append(f"{rel}:{line_no} → {line.strip()}")
                                added = True
                                break
                        if not added:
                            low = line.lower()
                            # Capture os.path.join lines — gives GPT context to reconstruct full paths
                            if 'os.path.join(' in low or 'path.join(' in low or 'file.path(' in low:
                                results.append(f"{rel}:{line_no} → {line.strip()}")
                                added = True
                            # Fallback: write-keyword lines with path-like strings (contains /)
                            # Keywords cover Python, R, Julia, MATLAB, shell, etc.
                            if not added and any(kw in low for kw in (
                                'open(', 'save(', 'write(', 'dump(', 'export(',
                                'to_csv', 'to_parquet', 'to_excel', 'to_json',
                                'savefig', 'imsave', 'imwrite',
                                'makedirs', 'mkdir',
                                'writecsv', 'writedlm', 'jldsave',  # Julia
                                'write.csv', 'write.table', 'saveRDS', 'save(',  # R
                                'fwrite(', 'fopen(', 'fprintf(',  # MATLAB/C
                                'np.save', 'torch.save', 'joblib.dump',  # Python libs
                            )):
                                for match in path_like.findall(line):
                                    results.append(f"{rel}:{line_no} → {line.strip()}")
                                    break
            except Exception:
                continue

    return results


def _infer_output_folder_with_gpt(files_location):
    """
    Ask GPT which specific files/patterns the experiment writes as output.
    Returns a space-separated pattern string (e.g. 'output/results.csv output/plot.png')
    or None if nothing identifiable.
    """
    lines = _scan_write_lines(files_location)
    if not lines:
        return None

    # Keep at most 60 lines to limit prompt size
    excerpt = "\n".join(lines[:60])

    prompt = [{
        "role": "user",
        "content": (
            "These lines were found in research experiment code files. "
            "Each line may contain a quoted filename or path — some are READ operations (input), "
            "some are WRITE operations (output).\n\n"
            f"{excerpt}\n\n"
            "Task: identify only the WRITE operations and return the full output file paths.\n\n"
            "Important rules:\n"
            "- Return file paths EXACTLY as they appear in the code. Do NOT add a directory "
            "prefix that is not present in the code. If the code writes 'results.csv' with no "
            "folder, return 'results.csv', not 'output/results.csv'.\n"
            "- If a line uses os.path.join(dir_var, 'filename.ext'), look at nearby lines "
            "to infer what dir_var is (e.g. 'output', 'results'). Reconstruct the full path "
            "as dir_var/filename.ext.\n"
            "- If the directory variable cannot be determined, return just the bare filename.\n"
            "- If the code writes many files to one folder, return a glob like 'output/'.\n"
            "- Return only the space-separated paths/patterns, "
            "e.g. 'output/results.csv output/plot.png results/report.txt'.\n"
            "- Reply with 'none' if no output files are identifiable."
        )
    }]

    try:
        result = callGPTModel(prompt).strip().strip("'\"")
        print(f"  [GPT output patterns] answer: {result!r}")
        return None if result.lower() == "none" else result
    except Exception as e:
        print(f"  [GPT output patterns] failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Docker output extraction helper
# ---------------------------------------------------------------------------

def _resolve_spec_paths(cont, workdir, patterns):
    """
    Expand each spec pattern to a list of (container_abs_path, display_name) tuples.
    Handles: exact files, directories (expanded to all files inside), and glob patterns.
    Returns (resolved, unresolved_patterns).
    """
    resolved = []
    unresolved = []
    for pattern in patterns:
        rel = pattern.strip('/')
        cpath = workdir + '/' + rel
        if '*' in rel or '?' in rel:
            # Glob: use find inside container to expand
            find_result = cont.exec_run(
                ["find", workdir, "-path", cpath, "-type", "f"],
                stderr=False
            )
            found = [
                p.strip() for p in
                find_result.output.decode('utf-8', errors='replace').splitlines()
                if p.strip()
            ]
            if found:
                for fp in found:
                    display = fp.replace(workdir.rstrip('/') + '/', '', 1)
                    resolved.append((fp, display))
            else:
                unresolved.append(rel)
        else:
            # Check if path exists
            check = cont.exec_run(["test", "-e", cpath], stderr=False)
            if check.exit_code == 0:
                is_dir = cont.exec_run(["test", "-d", cpath], stderr=False)
                if is_dir.exit_code == 0:
                    # Directory: expand to all contained files
                    find_result = cont.exec_run(
                        ["find", cpath, "-type", "f"],
                        stderr=False
                    )
                    for fp in find_result.output.decode('utf-8', errors='replace').splitlines():
                        fp = fp.strip()
                        if fp:
                            display = fp.replace(workdir.rstrip('/') + '/', '', 1)
                            resolved.append((fp, display))
                else:
                    resolved.append((cpath, rel))
            else:
                # Exact path not found — fall back to searching by filename anywhere in workdir.
                # This handles cases where the code saves to the working directory root
                # but the spec was written with a subdirectory prefix (e.g. "output/file.pdf"
                # when the script actually writes "file.pdf" at the root).
                fname = rel.rsplit('/', 1)[-1]
                fallback = cont.exec_run(
                    ["find", workdir, "-name", fname, "-type", "f"],
                    stderr=False
                )
                fb_paths = [
                    p.strip()
                    for p in fallback.output.decode('utf-8', errors='replace').splitlines()
                    if p.strip()
                ]
                if fb_paths:
                    for fp in fb_paths:
                        display = fp.replace(workdir.rstrip('/') + '/', '', 1)
                        resolved.append((fp, display))
                        print(f"  [resolve] fallback: '{rel}' not found, using '{fp}'")
                else:
                    unresolved.append(rel)
    return resolved, unresolved


# ---------------------------------------------------------------------------
# GPT helper: interpret run error from container logs (#3)
# ---------------------------------------------------------------------------

def _interpret_run_error_with_gpt(logs, command):
    """
    Given container logs from a failed run, ask GPT for a concise diagnosis.
    Returns a 2-3 sentence explanation string.
    """
    excerpt = logs[-3000:] if len(logs) > 3000 else logs
    prompt = [{
        "role": "user",
        "content": (
            f"A Docker container ran this command and failed:\n  {command}\n\n"
            f"Container logs:\n{excerpt}\n\n"
            "In 2-3 sentences, explain specifically what went wrong and suggest the most "
            "likely fix. Be concrete (e.g. missing package, wrong file path, syntax error, "
            "permission issue)."
        )
    }]
    try:
        return callGPTModel(prompt).strip()
    except Exception:
        return "An unexpected error occurred. Review the logs above for details."


def _resolve_zenodo_doi(dataset_doi):
    """
    Resolve a Zenodo DOI / URL / record-ID to a dataset_reference dict.
    Returns None if the identifier cannot be parsed.
    """
    parsed = _extract_zenodo_identifier(dataset_doi)
    if not parsed["record_id"] and not parsed["doi"]:
        return None

    token = os.getenv("ZENODO_API_TOKEN")
    if parsed["record_id"]:
        record = _zenodo_fetch_by_id(parsed["record_id"], token)
    else:
        record = _zenodo_fetch_by_doi(parsed["doi"], token)

    files_info = []
    total_size = 0
    for f in record.get("files", []):
        size = f.get("size", 0)
        total_size += size
        files_info.append({
            "filename": f.get("key") or f.get("filename", ""),
            "size": size,
            "checksum": f.get("checksum", ""),
            "download_url": (f.get("links") or {}).get("self", ""),
        })

    doi_files_are_archives = any(_is_archive_filename(f["filename"]) for f in files_info)

    return {
        "doi": parsed["doi"] or f"10.5281/zenodo.{record.get('id', '')}",
        "record_id": str(record.get("id", "")),
        "zenodo_metadata": {
            "title": record.get("metadata", {}).get("title", ""),
            "creators": record.get("metadata", {}).get("creators", []),
        },
        "files": files_info,
        "total_size_bytes": total_size,
        "doi_files_are_archives": doi_files_are_archives,
        "resolved_at": datetime.utcnow().isoformat() + "Z",
    }


ZENODO_MAX_FILES_PER_RECORD = 100

_ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".gz", ".bz2")


def _is_archive_filename(filename):
    name = filename.lower()
    return any(name.endswith(ext) for ext in _ARCHIVE_EXTENSIONS)


def _can_upload_individually(data_dir):
    """
    Return True if the data/ folder can be uploaded as individual files
    (no tar required) — i.e. all files are in the root of data_dir (no
    subdirectories), file count ≤ 100, and no single file > 50 GB.
    Subdirectory paths in the Zenodo bucket URL cause connection aborts.
    """
    files = []
    for root, _dirs, filenames in os.walk(data_dir):
        for fname in filenames:
            # Any file not directly in data_dir means subdirectories exist
            if root != data_dir:
                return False
            files.append(os.path.join(root, fname))

    if len(files) > ZENODO_MAX_FILES_PER_RECORD:
        return False

    for fpath in files:
        if os.path.getsize(fpath) > ZENODO_RECORD_LIMIT_BYTES:
            return False

    return True


def _evaluate_data_strategy(mode, local_data_size, remote_data_size,
                            data_dir=None):
    """
    Step 2 — Orchestration: decide how to handle the dataset.

    Returns one of:
      "no_data"                 — no dataset provided, existing workflow unchanged
      "embed"                   — data ≤ 5 GB, copy into Docker image
      "externalize_files"       — data 5–50 GB, ≤100 files, no single file >50 GB;
                                  upload individually → rclone mount possible
      "externalize"             — data 5–50 GB, but cannot upload individually;
                                  tar and upload to Zenodo as 1 record
      "chunk_and_externalize"   — data > 50 GB, tar, chunk, upload across records
      "external_doi"            — data already on Zenodo (Mode C)

    Zenodo constraints (per record): 50 GB total, 100 files max.
    """
    if mode == "C":
        return "external_doi"

    # Mode A — decision based on local data size
    if local_data_size == 0:
        return "no_data"
    elif local_data_size <= EMBED_THRESHOLD_BYTES:
        return "embed"
    elif local_data_size <= ZENODO_RECORD_LIMIT_BYTES:
        # Prefer individual file upload (enables rclone mount) if constraints allow
        if data_dir and _can_upload_individually(data_dir):
            return "externalize_files"
        return "externalize"
    else:
        return "chunk_and_externalize"


def _save_project_info(project_location, project_uuid, mode,
                       local_files_size, dataset_doi, dataset_reference,
                       data_dir=None):
    """Persist project_info.json inside the project folder."""
    remote_size = 0
    if dataset_reference:
        remote_size = dataset_reference.get("total_size_bytes", 0)

    # Step 2: evaluate data strategy (pass data_dir for externalize_files check)
    data_strategy = _evaluate_data_strategy(mode, local_files_size, remote_size,
                                            data_dir=data_dir)

    info = {
        "projectUuid": project_uuid,
        "mode": mode,
        "local_files_size_bytes": local_files_size,
        "dataset_doi": dataset_doi,
        "remote_data_size_bytes": remote_size,
        "total_data_size_bytes": local_files_size + remote_size,
        "data_strategy": data_strategy,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    info_path = os.path.join(project_location, "project_info.json")
    with open(info_path, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=2, ensure_ascii=False)

    if dataset_reference:
        ref_path = os.path.join(project_location, "dataset_reference.json")
        with open(ref_path, "w", encoding="utf-8") as fh:
            json.dump(dataset_reference, fh, indent=2, ensure_ascii=False)

    return info


def _extract_code_zip(file, cfg_projects, now_str):
    """
    Extract a code ZIP or save a single file.
    Returns (projectUuid, projectLocation, projectFilesLocation).
    Identical to the original upload logic.
    """
    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()

    if ext == '.zip':
        temp_path = os.path.join(cfg_projects, filename)
        file.save(temp_path)

        with zipfile.ZipFile(temp_path, 'r') as zip_ref:
            namelist = zip_ref.namelist()
            top_levels = set(
                name.split('/')[0] for name in namelist
                if not name.startswith('__MACOSX')
            )

            if len(top_levels) == 1 and all(
                name.startswith(f"{list(top_levels)[0]}/") for name in namelist
            ):
                clean_name = re.sub(r'[^\w.-]', '', list(top_levels)[0]).lower()
                projectUuid = f"{clean_name}_{now_str}"
            else:
                name_no_ext = os.path.splitext(filename)[0]
                clean_name = re.sub(r'[^\w.-]', '', name_no_ext).lower()
                projectUuid = f"{clean_name}_{now_str}"

        projectLocation = os.path.join(cfg_projects, projectUuid)
        projectFilesLocation = os.path.join(projectLocation, "files")

        with zipfile.ZipFile(temp_path, 'r') as zip_ref:
            zip_ref.extractall(projectLocation)
        os.remove(temp_path)

        if len(top_levels) == 1:
            extracted_root = os.path.join(projectLocation, list(top_levels)[0])
            os.rename(extracted_root, projectFilesLocation)
        else:
            os.makedirs(projectFilesLocation, exist_ok=True)
            for item in os.listdir(projectLocation):
                src = os.path.join(projectLocation, item)
                if item != "files":
                    os.rename(src, os.path.join(projectFilesLocation, item))
    else:
        name_no_ext = re.sub(r'[^\w.-]', '', os.path.splitext(filename)[0]).lower()
        projectUuid = f"{name_no_ext}_{now_str}"
        projectLocation = os.path.join(cfg_projects, projectUuid)
        projectFilesLocation = os.path.join(projectLocation, "files")
        os.makedirs(projectFilesLocation, exist_ok=True)
        file.save(os.path.join(projectFilesLocation, filename))

    return projectUuid, projectLocation, projectFilesLocation


def _save_data_upload(data_file, project_location):
    """
    Save an uploaded data file/ZIP into projects/<uuid>/data/.
    Returns the data directory path.
    """
    data_dir = os.path.join(project_location, "data")
    os.makedirs(data_dir, exist_ok=True)

    filename = secure_filename(data_file.filename)
    dest = os.path.join(data_dir, filename)
    data_file.save(dest)

    ext = os.path.splitext(filename)[1].lower()
    if ext == '.zip':
        with zipfile.ZipFile(dest, 'r') as zf:
            top_levels = {n.split('/')[0] for n in zf.namelist() if n.split('/')[0]}
            zf.extractall(data_dir)
        os.remove(dest)
        # Flatten single top-level folder so .npy/.csv/etc. are directly in data/
        if len(top_levels) == 1:
            sole = list(top_levels)[0]
            sole_path = os.path.join(data_dir, sole)
            if os.path.isdir(sole_path):
                for item in os.listdir(sole_path):
                    os.rename(os.path.join(sole_path, item),
                              os.path.join(data_dir, item))
                os.rmdir(sole_path)

    return data_dir


@project_bp.route("/project/upload-project", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/upload-project.yml")
def upload_file():
    """
    Step 1 — Experiment Request & Dataset Submission.

    Accepts:
      - "file"        (required)  code ZIP or single file
      - "data_file"   (optional)  data ZIP or file — saved to projects/<uuid>/data/
      - "dataset_doi"  (optional)  Zenodo DOI / URL / record-ID for remote dataset

    Modes:
      A  — file only           (existing behaviour, fully backwards-compatible)
      C  — file + data_file and/or dataset_doi  (Data Extension Layer)
    """
    messagesToUser = []
    messagesToChat = []

    # ------------------------------------------------------------------
    # 1. Validate that a code file was provided (always required)
    # ------------------------------------------------------------------
    if 'file' not in request.files:
        appendMessage(messagesToUser, "I can't find your file", stage="Start")
        return makeResponse(messagesToUser, 201, True)

    file = request.files["file"]

    if file.filename == '':
        appendMessage(messagesToUser, "I can't select your file", stage="Start")
        return makeResponse(messagesToUser, 201, True)

    # ------------------------------------------------------------------
    # 2. Read optional Data Extension Layer fields
    # ------------------------------------------------------------------
    data_file = request.files.get("data_file")
    dataset_doi = (request.form.get("dataset_doi") or "").strip()
    use_data_layer = (request.form.get("use_data_layer") or "").strip().lower() in ("true", "1", "yes")

    has_data_file = data_file is not None and getattr(data_file, "filename", "")
    has_doi = bool(dataset_doi)

    # If the Data Extension Layer flag is set, data is mandatory
    if use_data_layer and not has_data_file and not has_doi:
        appendMessage(messagesToUser,
                      "Data Extension Layer is enabled but no data was provided. "
                      "Please upload a data file or provide a Zenodo DOI.",
                      stage="Start")
        return makeResponse(messagesToUser, 201, True)

    # Cannot provide both — choose one mode
    if has_data_file and has_doi:
        appendMessage(messagesToUser,
                      "Please provide either a data file (Mode A) or a Zenodo DOI (Mode C), not both.",
                      stage="Start")
        return makeResponse(messagesToUser, 201, True)

    try:
        now_str = datetime.now(cfg.timezone).strftime("%d%m_%H%M")

        # --------------------------------------------------------------
        # 3. Extract / save code (same logic as before)
        # --------------------------------------------------------------
        projectUuid, projectLocation, projectFilesLocation = _extract_code_zip(
            file, cfg.PROJECTS_LOCATION, now_str
        )

        # --------------------------------------------------------------
        # 4. Handle data upload (Mode A with data)
        # --------------------------------------------------------------
        data_dir = None
        local_data_size = 0
        _embedded_data_size = 0   # set when combined-zip data/ stays inside the image

        if has_data_file:
            data_dir = _save_data_upload(data_file, projectLocation)
            local_data_size = _get_folder_size(data_dir)
        elif not has_doi:
            # Original approach: user zipped code + data/ together.
            # Use GPT to identify which subfolder contains the dataset.
            _files_data = _detect_data_folder_with_gpt(projectFilesLocation)

            if _files_data and os.path.isdir(_files_data):
                _auto_size = _get_folder_size(_files_data)
                if _auto_size > EMBED_THRESHOLD_BYTES:
                    _dest_data = os.path.join(projectLocation, "data")
                    shutil.move(_files_data, _dest_data)
                    data_dir = _dest_data
                    local_data_size = _auto_size
                    print(f"  [GPT detected] {os.path.basename(_files_data)}/ ({_auto_size} bytes) "
                          f"> embed threshold, moved to data/ for externalize pipeline")
                else:
                    _embedded_data_size = _auto_size  # small enough to bake into image

        # --------------------------------------------------------------
        # 5. Resolve Zenodo DOI (Mode C)
        # --------------------------------------------------------------
        dataset_reference = None

        if has_doi:
            try:
                dataset_reference = _resolve_zenodo_doi(dataset_doi)
                if dataset_reference is None:
                    appendMessage(
                        messagesToUser, f"Could not parse Zenodo identifier: {dataset_doi}",
                        stage="Start",
                    )
                    return makeResponse(messagesToUser, 201, True)
            except Exception as doi_err:
                appendMessage(
                    messagesToUser, f"Failed to resolve DOI: {str(doi_err)}",
                    stage="Start",
                )
                return makeResponse(messagesToUser, 201, True)

        # --------------------------------------------------------------
        # 6. Determine mode and save project_info.json
        # --------------------------------------------------------------
        if has_doi:
            mode = "C"   # code + DOI (data already on Zenodo)
        else:
            mode = "A"   # code only, or code + local data file

        project_info = _save_project_info(
            project_location=projectLocation,
            project_uuid=projectUuid,
            mode=mode,
            local_files_size=local_data_size,
            dataset_doi=dataset_doi or None,
            dataset_reference=dataset_reference,
            data_dir=data_dir,
        )

        # --------------------------------------------------------------
        # 6b. Auto-generate manifest for strategies that skip Step 3
        # (no_data, embed, external_doi — externalize/chunk handled
        #  in externalize-data endpoint)
        # --------------------------------------------------------------
        strategy = project_info.get("data_strategy", "no_data")
        if strategy in ("no_data", "embed", "external_doi"):
            # These strategies don't need externalize-data — build manifest now
            _build_manifest(projectLocation, projectUuid)
        # externalize_files / externalize / chunk_and_externalize: manifest
        # is built after externalize-data completes

        # --------------------------------------------------------------
        # 6c. Conversational data detection messages (Stage 1)
        # --------------------------------------------------------------
        if not has_data_file and not has_doi:
            if _embedded_data_size > 0:
                # Combined zip — small data, embedding in image
                size_mb = round(_embedded_data_size / (1024 * 1024))
                appendMessage(
                    messagesToUser,
                    f"I found a data/ folder in your project ({size_mb} MB). "
                    f"It's small enough to embed directly in the reproducibility artifact — "
                    f"no separate upload needed.",
                    stage="ProjectLocation"
                )
            elif local_data_size > 0 and strategy in (
                "externalize", "externalize_files", "chunk_and_externalize"
            ):
                # Combined zip — large data, will externalize
                size_gb = round(local_data_size / (1024 ** 3), 2)
                appendMessage(
                    messagesToUser,
                    f"I found a data/ folder in your project ({size_gb} GB). "
                    f"It's too large to embed in the Docker image. "
                    f"I'll upload it to Zenodo as a separate dataset after we set up "
                    f"the experiment environment.",
                    stage="ProjectLocation"
                )
            else:
                # No data detected — ask the user conversationally
                appendMessage(
                    messagesToUser,
                    "Got it. Does your experiment use a dataset?\n\n"
                    "• Upload a data file using the button below\n"
                    "• Provide a Zenodo DOI (e.g. 10.5281/zenodo.1234567)\n"
                    "• Click \"No dataset\" or type \"no data\" to proceed without one",
                    stage="WaitForDataInput"
                )

        # --------------------------------------------------------------
        # 7. Validate Docker tag name
        # --------------------------------------------------------------
        message1 = {
            "role": "system",
            "jsonObject": False,
            "contentShort": None,
            "content": f"I need to validate the variable 'projectUuid' for use in this function. "
                       f"\nIf 'projectUuid' is a valid Docker tag name, respond with 'YES'."
                       f"\nIf it's not valid, return an updated, valid version of 'projectUuid'."
                       f"\nCurrent value: projectUuid = {projectUuid}."
                       f"\nYour response should be exactly one word, either 'YES' or the updated 'projectUuid' value."
        }

        messagesToChat.append(message1)
        gpt_result = callGPTModel(messagesToChat).strip()

        if gpt_result.upper() == "YES" or gpt_result == projectUuid:
            appendMessage(messagesToUser, content=projectUuid, stage="FindProjectFiles")
        else:
            # Sanitize: only keep characters valid for Docker tags / folder names
            safe_result = re.sub(r'[^\w.-]', '', gpt_result)
            if not safe_result:
                safe_result = projectUuid  # fallback to original if GPT returned garbage
            if safe_result != projectUuid:
                newProjectLocation = os.path.join(cfg.PROJECTS_LOCATION, safe_result)
                os.rename(projectLocation, newProjectLocation)
                projectUuid = safe_result
            appendMessage(messagesToUser, content=projectUuid, stage="FindProjectFiles")

        return makeResponse(messagesToUser, 201, True)

    except Exception as e:
        print("Ups! There's an error:", str(e))
        appendMessage(messagesToUser, "Ups! There's an error: " + str(e), stage="Start")
        return makeResponse(messagesToUser, 201, True)


# ---------------------------------------------------------------------------
# Stage 1 follow-up — provide-data (user response to WaitForDataInput)
# ---------------------------------------------------------------------------

def _clear_stale_data_artifacts(project_location, old_strategy):
    """
    Called when the user goes back to WaitForDataInput to change their data.
    Removes all data-related files that would otherwise conflict with the new submission.

    If old_strategy was 'embed', the Dockerfile had 'COPY data/ /data' baked in and
    the Docker image contains the old data — both are deleted so the flow forces a rebuild.
    """
    # Remove uploaded/extracted data
    for name in ("data", "data_provisioned"):
        path = os.path.join(project_location, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    # Remove derived artefacts that depend on the data
    for name in ("dataset_reference.json", "reproducibility_manifest.json",
                 "run_progress.json", "output_spec.txt"):
        path = os.path.join(project_location, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except Exception:
                pass

    # If data was previously embedded in the Docker image, the image is now stale.
    # Deleting the Dockerfile forces the flow back through BuildDockerFile →
    # BuildDockerImage so a fresh image (without the old COPY data/ /data line) is built.
    if old_strategy == "embed":
        dockerfile_path = os.path.join(project_location, "Dockerfile")
        if os.path.isfile(dockerfile_path):
            try:
                os.remove(dockerfile_path)
            except Exception:
                pass


@project_bp.route("/project/<projectUuid>/provide-data", methods=['POST'])
@cross_origin()
@require_auth
def provide_data(projectUuid):
    """
    Called when the user responds to the WaitForDataInput conversational prompt.

    Accepts:
      JSON  { "no_data": true }
      JSON  { "dataset_doi": "10.5281/zenodo.xxxxx" }
      FormData with "data_file" (zip or single file)
    """
    messagesToUser = []

    projectLocation = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    info_path = os.path.join(projectLocation, "project_info.json")

    if not os.path.isfile(info_path):
        appendMessage(messagesToUser, "Project not found. Please upload your code first.",
                      stage="Start")
        return makeResponse(messagesToUser, 400, True)

    with open(info_path, "r", encoding="utf-8") as fh:
        project_info = json.load(fh)

    # If the project already had data state, clear it before accepting new data.
    # This handles the case where the user navigates back to change their dataset.
    old_strategy = project_info.get("data_strategy")
    if old_strategy and old_strategy != "no_data":
        _clear_stale_data_artifacts(projectLocation, old_strategy)

    # Reset data-related fields in project_info so they reflect the new submission
    for field in ("data_strategy", "dataset_doi", "local_files_size_bytes",
                  "remote_data_size_bytes", "total_data_size_bytes", "mode"):
        project_info.pop(field, None)

    req_body = request.get_json(silent=True) or {}
    data_file = request.files.get("data_file")
    dataset_doi = (req_body.get("dataset_doi") or request.form.get("dataset_doi") or "").strip()
    no_data = req_body.get("no_data") or request.form.get("no_data") in ("true", "1", "yes")
    free_text = (req_body.get("text") or "").strip()

    # ------------------------------------------------------------------
    # Free-text input: use GPT to classify intent
    # ------------------------------------------------------------------
    if free_text and not no_data and not dataset_doi and not (data_file and getattr(data_file, "filename", "")):
        classified = _classify_data_input_with_gpt(free_text)
        if classified["intent"] == "no_data":
            no_data = True
        elif classified["intent"] == "doi":
            dataset_doi = classified["doi"] or ""
        else:
            appendMessage(messagesToUser,
                          "I'm not sure I understood. Please say \"no data\", "
                          "provide a Zenodo DOI, or use the upload button to attach a file.",
                          stage="WaitForDataInput")
            return makeResponse(messagesToUser, 200, True)

    # ------------------------------------------------------------------
    # Case 1: no data
    # ------------------------------------------------------------------
    if no_data:
        project_info["data_strategy"] = "no_data"
        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(project_info, fh, indent=2, ensure_ascii=False)
        appendMessage(messagesToUser,
                      "Understood — no dataset. Let's continue with the experiment setup.",
                      stage="FindProjectFiles")
        return makeResponse(messagesToUser, 200, True)

    # ------------------------------------------------------------------
    # Case 2: Zenodo DOI
    # ------------------------------------------------------------------
    if dataset_doi:
        try:
            dataset_reference = _resolve_zenodo_doi(dataset_doi)
            if dataset_reference is None:
                appendMessage(messagesToUser,
                              f"Could not parse Zenodo identifier: {dataset_doi}",
                              stage="WaitForDataInput")
                return makeResponse(messagesToUser, 400, True)

            remote_size = dataset_reference.get("total_size_bytes", 0)
            project_info["mode"] = "C"
            project_info["dataset_doi"] = dataset_reference["doi"]
            project_info["data_strategy"] = "external_doi"
            project_info["remote_data_size_bytes"] = remote_size
            project_info["total_data_size_bytes"] = remote_size

            with open(info_path, "w", encoding="utf-8") as fh:
                json.dump(project_info, fh, indent=2, ensure_ascii=False)

            ref_path = os.path.join(projectLocation, "dataset_reference.json")
            with open(ref_path, "w", encoding="utf-8") as fh:
                json.dump(dataset_reference, fh, indent=2, ensure_ascii=False)

            _build_manifest(projectLocation, projectUuid)

            title = dataset_reference.get("zenodo_metadata", {}).get("title", dataset_doi)
            size_gb = round(remote_size / (1024 ** 3), 2) if remote_size else "unknown"
            appendMessage(messagesToUser,
                          f"Got it. Found your dataset on Zenodo: \"{title}\" ({size_gb} GB). "
                          f"It will be downloaded and mounted at runtime.",
                          stage="FindProjectFiles")
            return makeResponse(messagesToUser, 200, True)

        except Exception as e:
            appendMessage(messagesToUser, f"Failed to resolve DOI: {str(e)}",
                          stage="WaitForDataInput")
            return makeResponse(messagesToUser, 400, True)

    # ------------------------------------------------------------------
    # Case 3: data file upload
    # ------------------------------------------------------------------
    if data_file and getattr(data_file, "filename", ""):
        data_dir = _save_data_upload(data_file, projectLocation)
        local_data_size = _get_folder_size(data_dir)

        data_strategy = _evaluate_data_strategy("A", local_data_size, 0, data_dir=data_dir)

        # Detect what folder name the code uses to reference data — if it differs
        # from 'data', a symlink will be added to the Dockerfile so the original
        # path still resolves at /files/<alias> → /data.
        files_location = os.path.join(projectLocation, "files")
        alias = _detect_data_folder_alias(files_location)

        project_info["mode"] = "A"
        project_info["local_files_size_bytes"] = local_data_size
        project_info["data_strategy"] = data_strategy
        project_info["total_data_size_bytes"] = local_data_size
        if alias:
            project_info["data_folder_alias"] = alias
        else:
            project_info.pop("data_folder_alias", None)

        with open(info_path, "w", encoding="utf-8") as fh:
            json.dump(project_info, fh, indent=2, ensure_ascii=False)

        if data_strategy == "embed":
            _build_manifest(projectLocation, projectUuid)
            size_mb = round(local_data_size / (1024 ** 2))
            appendMessage(messagesToUser,
                          f"Your data ({size_mb} MB) fits inside the reproducibility artifact — "
                          f"no separate upload needed. Let's continue.",
                          stage="FindProjectFiles")
        else:
            size_gb = round(local_data_size / (1024 ** 3), 2)
            appendMessage(messagesToUser,
                          f"Your data is {size_gb} GB — too large to embed in the reproducibility artifact "
                          f"(limit: {round(EMBED_THRESHOLD_BYTES / (1024 ** 3), 1)} GB). "
                          f"I'll upload it to Zenodo as a standalone dataset.",
                          stage="InferDatasetMetadata")

        return makeResponse(messagesToUser, 200, True)

    # ------------------------------------------------------------------
    # Nothing useful provided
    # ------------------------------------------------------------------
    appendMessage(messagesToUser,
                  "Please upload a data file, provide a Zenodo DOI, or type \"no data\".",
                  stage="WaitForDataInput")
    return makeResponse(messagesToUser, 400, True)


# ---------------------------------------------------------------------------
# Step 4 — Manifest Construction
# ---------------------------------------------------------------------------

def _build_manifest(project_location, project_uuid):
    """
    Build reproducibility_manifest.json from project_info.json and
    optionally dataset_reference.json.

    This manifest is the single source of truth for Steps 5-6.
    It is placed in the project root so it gets packaged into the
    research artifact ZIP alongside the Docker image and run scripts.
    """
    info_path = os.path.join(project_location, "project_info.json")
    if not os.path.isfile(info_path):
        raise FileNotFoundError("project_info.json not found")

    with open(info_path, "r", encoding="utf-8") as f:
        project_info = json.load(f)

    strategy = project_info.get("data_strategy", "no_data")

    manifest = {
        "manifest_version": "1.0",
        "project_uuid": project_uuid,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "data_strategy": strategy,
        "dataset": None,
        "reconstruction": None,
    }

    if strategy in ("no_data", "embed"):
        manifest["dataset"] = {
            "external": False,
            "total_size_bytes": project_info.get("local_files_size_bytes", 0),
        }
        manifest["reconstruction"] = {
            "method": "docker_embed" if strategy == "embed" else "none",
            "steps": [] if strategy == "no_data" else [
                "Data is embedded inside the Docker image.",
                "No additional download is required.",
            ],
        }

    elif strategy in ("externalize", "chunk_and_externalize"):
        depositions = project_info.get("zenodo_depositions", [])
        chunked = project_info.get("chunked", False)

        chunks = []
        for dep in depositions:
            chunk_entry = {
                "doi": dep.get("doi"),
                "record_id": dep.get("record_id"),
                "filename": dep.get("filename"),
                "md5": dep.get("chunk_md5") or dep.get("tar_md5"),
                "size_bytes": dep.get("chunk_size") or project_info.get("tar_size"),
            }
            if chunked:
                chunk_entry["chunk_id"] = dep.get("chunk_id")
            chunks.append(chunk_entry)

        manifest["dataset"] = {
            "external": True,
            "total_size_bytes": project_info.get("total_data_size_bytes", 0),
            "tar_md5": project_info.get("tar_md5"),
            "tar_size_bytes": project_info.get("tar_size"),
            "chunked": chunked,
            "num_chunks": len(chunks) if chunked else 0,
            "depositions": chunks,
        }

        if chunked:
            manifest["reconstruction"] = {
                "method": "rclone_copy",
                "steps": [
                    "Download each chunk file via rclone or Zenodo API.",
                    "Verify each chunk MD5 against the manifest.",
                    "Concatenate chunks in order to reassemble dataset.tar.",
                    "Verify reassembled tar MD5 against tar_md5.",
                    "Extract: tar xf dataset.tar -C /data",
                    "Mount /data as read-only volume.",
                ],
            }
        else:
            manifest["reconstruction"] = {
                "method": "rclone_copy",
                "steps": [
                    "Download dataset.tar from Zenodo via rclone or API.",
                    "Verify tar MD5 against tar_md5.",
                    "Extract: tar xf dataset.tar -C /data",
                    "Mount /data as read-only volume.",
                ],
            }

    elif strategy == "externalize_files":
        zenodo_files = project_info.get("zenodo_files", [])
        manifest["dataset"] = {
            "external": True,
            "individual_files": True,
            "doi": project_info.get("zenodo_doi"),
            "record_id": project_info.get("zenodo_record_id"),
            "total_size_bytes": project_info.get("total_data_size_bytes", 0),
            "num_files": len(zenodo_files),
            "files": zenodo_files,
        }
        manifest["reconstruction"] = {
            "method": "rclone_mount",
            "steps": [
                "Mount the Zenodo record via rclone (no download required).",
                "Files are accessible directly — no extraction step needed.",
                "Mount as read-only volume into the Docker container.",
            ],
        }

    elif strategy == "external_doi":
        ref_path = os.path.join(project_location, "dataset_reference.json")
        dataset_ref = {}
        if os.path.isfile(ref_path):
            with open(ref_path, "r", encoding="utf-8") as f:
                dataset_ref = json.load(f)

        files_listing = []
        for fl in dataset_ref.get("files", []):
            files_listing.append({
                "filename": fl.get("filename"),
                "size_bytes": fl.get("size"),
                "md5": fl.get("checksum", "").replace("md5:", ""),
                "download_url": fl.get("download_url"),
            })

        doi_files_are_archives = dataset_ref.get("doi_files_are_archives", False)

        manifest["dataset"] = {
            "external": True,
            "doi": dataset_ref.get("doi") or project_info.get("dataset_doi"),
            "record_id": dataset_ref.get("record_id"),
            "total_size_bytes": dataset_ref.get("total_size_bytes", 0),
            "chunked": False,
            "doi_files_are_archives": doi_files_are_archives,
            "files": files_listing,
        }

        if doi_files_are_archives:
            manifest["reconstruction"] = {
                "method": "download_extract",
                "steps": [
                    "Download archive file(s) from Zenodo record via rclone copy or API.",
                    "Verify each file MD5 against the manifest.",
                    "Extract archive into /data directory.",
                    "Mount /data as read-only volume.",
                ],
            }
        else:
            manifest["reconstruction"] = {
                "method": "rclone_mount",
                "steps": [
                    "Mount the Zenodo record via rclone doi backend (no download required).",
                    "Falls back to rclone copy, then direct download if mount unavailable.",
                    "Mount as read-only volume into the Docker container.",
                ],
            }

    manifest_path = os.path.join(project_location, "reproducibility_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


@project_bp.route("/project/<projectUuid>/build-manifest", methods=['POST'])
@cross_origin()
@require_auth
def build_manifest(projectUuid):
    """
    Step 4 — Generate reproducibility_manifest.json from project state.
    Works for all data strategies.
    """
    messagesToUser = []
    projectLocation = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)

    if not os.path.isdir(projectLocation):
        appendMessage(messagesToUser, "Project not found.", stage="Start")
        return makeResponse(messagesToUser, 404, True)

    try:
        manifest = _build_manifest(projectLocation, projectUuid)
        appendMessage(messagesToUser, manifest,
                      contentShort="Reproducibility manifest generated successfully.",
                      jsonObject=True, stage="FindProjectFiles")
        return makeResponse(messagesToUser, 200, True)
    except Exception as e:
        print(f"Manifest build error: {e}")
        appendMessage(messagesToUser,
                      f"Error building manifest: {str(e)}",
                      stage="Start")
        return makeResponse(messagesToUser, 500, True)


@project_bp.route("/project/<projectUuid>/manifest", methods=['GET'])
@cross_origin()
@require_auth
def get_manifest(projectUuid):
    """Retrieve the reproducibility_manifest.json for a project."""
    messagesToUser = []
    projectLocation = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    manifest_path = os.path.join(projectLocation, "reproducibility_manifest.json")

    if not os.path.isfile(manifest_path):
        appendMessage(messagesToUser,
                      "Manifest not found. Call build-manifest first.",
                      stage="Start")
        return makeResponse(messagesToUser, 404, True)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    appendMessage(messagesToUser, manifest,
                  contentShort="Manifest retrieved.",
                  jsonObject=True, stage="FindProjectFiles")
    return makeResponse(messagesToUser, 200, True)


# ---------------------------------------------------------------------------
# Step 3 — Data Delegation: tar, chunk, upload to Zenodo
# ---------------------------------------------------------------------------

ZENODO_API_BASE = "https://zenodo.org/api"


def _upload_file_to_bucket(upload_url, file_path, token, max_retries=5):
    """
    Upload a file to a Zenodo bucket URL.

    Prefers curl (available on Windows 10+ natively) because it uses TCP
    keepalive and built-in retry logic, which avoids the ConnectionAbortedError
    10053 that Python's requests library triggers on Windows during long uploads.
    Falls back to requests if curl is not in PATH.
    """
    import time as _time
    import urllib.parse

    # Build the full URL with the access token as a query param
    sep = "&" if "?" in upload_url else "?"
    full_url = f"{upload_url}{sep}access_token={token}"

    # --- curl path (preferred on Windows) ---
    if shutil.which("curl"):
        print(f"  Uploading via curl: {os.path.basename(file_path)}")
        for attempt in range(max_retries):
            result = subprocess.run(
                [
                    "curl",
                    "--upload-file", file_path,
                    "--keepalive-time", "20",   # send TCP keepalive every 20 s
                    "--retry", "0",             # our own outer loop handles retries
                    "--progress-bar",           # show live progress in server terminal
                    "--write-out", "\nHTTP_STATUS:%{http_code}",
                    full_url,
                ],
                stdout=subprocess.PIPE,  # capture stdout (HTTP status) for parsing
                stderr=None,             # let stderr (progress bar) go to terminal
                text=True, timeout=None,
            )
            output = result.stdout or ""
            if "HTTP_STATUS:200" in output or "HTTP_STATUS:201" in output:
                print(f"  Upload complete.")
                return
            stderr_snippet = ""  # stderr goes to terminal, not captured
            if attempt < max_retries - 1:
                print(f"  curl attempt {attempt + 1} failed "
                      f"(exit {result.returncode}: {stderr_snippet}), retrying in 20s...")
                _time.sleep(20)
            else:
                raise RuntimeError(
                    f"curl upload failed after {max_retries} attempts: {stderr_snippet}"
                )

    # --- requests fallback ---
    print(f"  curl not found — uploading via requests: {os.path.basename(file_path)}")
    import requests as _req
    for attempt in range(max_retries):
        with open(file_path, "rb") as f:
            try:
                put_resp = _req.put(upload_url, params={"access_token": token},
                                    data=f, timeout=None)
                put_resp.raise_for_status()
                return
            except _req.exceptions.ConnectionError as conn_err:
                if attempt < max_retries - 1:
                    print(f"  requests attempt {attempt + 1} failed ({conn_err}), retrying in 20s...")
                    _time.sleep(20)
                else:
                    raise


def _write_run_progress(progress_path, **kwargs):
    """Write run_progress.json atomically. Never raises."""
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(kwargs, f)
    except Exception:
        pass


def _provision_data(manifest, project_uuid, project_location, progress_path):
    """
    Provision dataset files required by the experiment and return the host
    path to mount as /data inside the container.

    Handles external_doi (with DOI cache), externalize, externalize_files,
    and chunk_and_externalize strategies.  Returns None for no_data/embed.

    Raises RuntimeError / ValueError on provisioning failure.
    """
    from helpers.data_provisioning import provision_data as _prov

    strategy = manifest.get("data_strategy", "no_data")
    if strategy == "no_data":
        return None
    if strategy == "embed":
        _data_dir = os.path.join(project_location, "data")
        if os.path.isdir(_data_dir):
            return os.path.normpath(
                os.path.join(cfg.HOST_VOLUME_PATH, project_uuid, "data"))
        return None

    if strategy == "external_doi":
        _dataset_info = manifest.get("dataset", {})
        _record_id = str(_dataset_info.get("record_id", "")).strip()
        if _record_id:
            _cache_base = os.path.join(cfg.PROJECTS_LOCATION, "_doi_cache")
            _cache_entry = os.path.join(_cache_base, _record_id)
            _data_output = os.path.join(_cache_entry, "data")
            _host_data_path = os.path.normpath(
                os.path.join(cfg.HOST_VOLUME_PATH, "_doi_cache", _record_id, "data"))
            _complete_marker = os.path.join(_cache_entry, "_complete")
        else:
            _data_output = os.path.join(project_location, "data_provisioned")
            _host_data_path = os.path.normpath(
                os.path.join(cfg.HOST_VOLUME_PATH, project_uuid, "data_provisioned"))
            _complete_marker = os.path.join(_data_output, "_complete")

        _cache_valid = (
            os.path.isfile(_complete_marker)
            and os.path.isdir(_data_output)
            and any(True for _ in os.scandir(_data_output))
        )
        if _cache_valid:
            print(f"  [provision] DOI cache hit: {_data_output}")
            _write_run_progress(progress_path, phase="running", detail="Using cached dataset...")
        else:
            _zenodo_token = os.getenv("ZENODO_API_TOKEN")
            _manifest_path = os.path.join(project_location, "reproducibility_manifest.json")
            _write_run_progress(progress_path, phase="provisioning", detail="Connecting to Zenodo...")
            _prov(_manifest_path, _data_output, zenodo_token=_zenodo_token,
                  progress_path=progress_path)
            try:
                with open(_complete_marker, "w") as _cm:
                    _cm.write(project_uuid)
            except Exception:
                pass
        return _host_data_path
    else:
        # externalize / externalize_files / chunk_and_externalize
        # Prefer original uploaded data/ if it still exists (avoids re-download from Zenodo)
        _original_data = os.path.join(project_location, "data")
        if os.path.isdir(_original_data) and any(True for _ in os.scandir(_original_data)):
            print(f"  [provision] Using original uploaded data dir: {_original_data}")
            _write_run_progress(progress_path, phase="running", detail="Using local data...")
            return os.path.normpath(
                os.path.join(cfg.HOST_VOLUME_PATH, project_uuid, "data"))

        _data_output = os.path.join(project_location, "data_provisioned")
        _host_data_path = os.path.normpath(
            os.path.join(cfg.HOST_VOLUME_PATH, project_uuid, "data_provisioned"))
        _complete_marker = os.path.join(_data_output, "_complete")
        _cache_valid = (
            os.path.isfile(_complete_marker)
            and os.path.isdir(_data_output)
            and any(True for _ in os.scandir(_data_output))
        )
        if not _cache_valid:
            _manifest_path = os.path.join(project_location, "reproducibility_manifest.json")
            _write_run_progress(progress_path, phase="provisioning", detail="Provisioning data...")
            _prov(_manifest_path, _data_output,
                  zenodo_token=os.getenv("ZENODO_API_TOKEN"),
                  progress_path=progress_path)
            try:
                with open(_complete_marker, "w") as _cm:
                    _cm.write(project_uuid)
            except Exception:
                pass
        return _host_data_path


def _create_zenodo_deposition_from_path(file_path, filename, metadata_json):
    """
    Create a Zenodo deposition, upload a single local file, and publish.

    *metadata_json* must be {"metadata": {...}}.
    Returns the published deposition JSON (contains DOI, files with checksums).
    """
    import requests

    token = os.getenv("ZENODO_API_TOKEN")
    if not token:
        raise RuntimeError("ZENODO_API_TOKEN environment variable is not set")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    # 1) Create empty deposition
    resp = requests.post(f"{ZENODO_API_BASE}/deposit/depositions",
                         data="{}", headers=headers, timeout=60)
    resp.raise_for_status()
    deposition = resp.json()

    # 2) Set metadata (sanitize first to satisfy Zenodo publish requirements)
    _sanitize_zenodo_metadata(metadata_json)
    dep_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}"
    resp = requests.put(dep_url, data=json.dumps(metadata_json),
                        headers=headers, timeout=60)
    resp.raise_for_status()
    deposition = resp.json()

    # 3) Upload file to bucket
    bucket_url = deposition["links"]["bucket"]
    upload_url = f"{bucket_url}/{filename}"
    _upload_file_to_bucket(upload_url, file_path, token)

    # 4) Publish
    publish_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}/actions/publish"
    pub_resp = requests.post(publish_url, headers=headers, timeout=120)
    if not pub_resp.ok:
        raise RuntimeError(
            f"Zenodo publish failed ({pub_resp.status_code}): {pub_resp.text[:500]}"
        )
    deposition = pub_resp.json()

    return deposition


def _create_zenodo_deposition_multi_file(file_paths, metadata_json):
    """
    Create a Zenodo deposition, upload multiple local files to it, and publish.

    *file_paths* is a list of dicts: [{"path": "/abs/path", "zenodo_name": "subdir/file.csv"}, ...]
    *metadata_json* must be {"metadata": {...}}.
    Returns the published deposition JSON.
    """
    import requests

    token = os.getenv("ZENODO_API_TOKEN")
    if not token:
        raise RuntimeError("ZENODO_API_TOKEN environment variable is not set")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    # 1) Create empty deposition
    resp = requests.post(f"{ZENODO_API_BASE}/deposit/depositions",
                         data="{}", headers=headers, timeout=60)
    resp.raise_for_status()
    deposition = resp.json()

    # 2) Set metadata (sanitize first to satisfy Zenodo publish requirements)
    _sanitize_zenodo_metadata(metadata_json)
    dep_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}"
    resp = requests.put(dep_url, data=json.dumps(metadata_json),
                        headers=headers, timeout=60)
    resp.raise_for_status()
    deposition = resp.json()

    # 3) Upload each file to the bucket
    bucket_url = deposition["links"]["bucket"]
    for finfo in file_paths:
        upload_url = f"{bucket_url}/{finfo['zenodo_name']}"
        _upload_file_to_bucket(upload_url, finfo["path"], token)

    # 4) Publish
    publish_url = f"{ZENODO_API_BASE}/deposit/depositions/{deposition['id']}/actions/publish"
    pub_resp = requests.post(publish_url, headers=headers, timeout=120)
    if not pub_resp.ok:
        raise RuntimeError(
            f"Zenodo publish failed ({pub_resp.status_code}): {pub_resp.text[:500]}"
        )
    deposition = pub_resp.json()

    return deposition


def _sanitize_zenodo_metadata(metadata_json):
    """
    Ensure the metadata dict satisfies Zenodo's minimum publish requirements.
    Mutates and returns the inner metadata dict (not the wrapper).

    Required by Zenodo at publish time:
      - title (non-empty string)
      - upload_type (valid value)
      - creators (list with at least one entry having a non-empty 'name')
      - access_right ('open', 'embargoed', 'restricted', 'closed')
      - license (required when access_right is 'open' or 'embargoed')
      - description (non-empty string)
    """
    md = metadata_json.get("metadata", metadata_json)

    # title
    if not md.get("title", "").strip():
        md["title"] = "Untitled Dataset"

    # upload_type
    valid_types = {"publication", "poster", "presentation", "dataset",
                   "image", "video", "software", "lesson", "physicalobject", "other"}
    if md.get("upload_type") not in valid_types:
        md["upload_type"] = "dataset"

    # description
    if not md.get("description", "").strip():
        md["description"] = "Dataset uploaded via SciConv Data Extension Layer."

    # creators — must be a list of dicts with non-empty 'name'
    creators = md.get("creators", [])
    if not isinstance(creators, list) or not creators:
        creators = [{"name": "Unknown"}]
    sanitized_creators = []
    for c in creators:
        if isinstance(c, dict) and c.get("name", "").strip():
            sanitized_creators.append(c)
    md["creators"] = sanitized_creators if sanitized_creators else [{"name": "Unknown"}]

    # access_right
    valid_access = {"open", "embargoed", "restricted", "closed"}
    if md.get("access_right") not in valid_access:
        md["access_right"] = "open"

    # license (required for open/embargoed)
    if md.get("access_right") in {"open", "embargoed"} and not md.get("license"):
        md["license"] = "cc-by-4.0"

    # Remove fields Zenodo rejects at publish time
    _ZENODO_UNSUPPORTED = {"grants", "thesis_supervisors", "thesis_university",
                           "partof_title", "partof_pages"}
    for key in _ZENODO_UNSUPPORTED:
        md.pop(key, None)

    return md


def _build_zenodo_metadata(project_uuid, creator_name=None, description=None,
                           chunk_label=None):
    """
    Build a minimal Zenodo metadata payload for the dataset upload.
    """
    title = f"Dataset for experiment: {project_uuid}"
    if chunk_label:
        title = f"{title} ({chunk_label})"

    md = {
        "metadata": {
            "title": title,
            "upload_type": "dataset",
            "description": description or (
                f"Externalized dataset for reproducible experiment {project_uuid}. "
                "Uploaded automatically by SciConv Data Extension Layer."
            ),
            "creators": [{"name": creator_name or "Unknown"}],
            "access_right": "open",
            "license": "cc-by-4.0",
            "keywords": ["reproducibility", "computational experiment", "SciConv"],
        }
    }
    return md


@project_bp.route("/project/<projectUuid>/infer-dataset-metadata", methods=['POST'])
@cross_origin()
@require_auth
def infer_dataset_metadata(projectUuid):
    """
    Sample data files from projects/<uuid>/data/ and ask GPT to infer
    Zenodo metadata. Returns {"zenodo_metadata": [...], "template": {...}}
    or {"skip": true} for strategies that don't upload to Zenodo.
    """
    from helpers.article.articleHelper import infer_dataset_metadata_from_data

    project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    info_path = os.path.join(project_location, "project_info.json")

    if not os.path.isfile(info_path):
        return jsonify({"skip": True}), 200

    with open(info_path, "r", encoding="utf-8") as f:
        project_info = json.load(f)

    strategy = project_info.get("data_strategy", "no_data")
    if strategy not in ("externalize", "externalize_files", "chunk_and_externalize"):
        return jsonify({"skip": True}), 200

    data_dir = os.path.join(project_location, "data")
    files_dir = os.path.join(project_location, "files")
    try:
        result = infer_dataset_metadata_from_data(data_dir, projectUuid, files_dir=files_dir)
    except Exception as e:
        print(f"[infer_dataset_metadata] Error: {e}")
        result = {"skip": True}

    return jsonify(result), 200


@project_bp.route("/project/<projectUuid>/externalize-data", methods=['POST'])
@cross_origin()
@require_auth
def externalize_data(projectUuid):
    """
    Step 3 — Data Delegation and Automated Externalization.

    Reads project_info.json, and if data_strategy is "externalize" or
    "chunk_and_externalize":
      1) Tars the data/ folder
      2) Chunks if > 50 GB
      3) Uploads to Zenodo (one deposition per chunk, or one for the whole tar)
      4) Saves deposition info + checksums to project_info.json

    Optional form fields:
      - creator_name:  author name for Zenodo metadata (e.g. "Costa, Lázaro")
      - description:   dataset description for Zenodo

    Strategies that skip this step: "no_data", "embed", "external_doi".
    """
    from helpers.dataset_chunker import prepare_dataset_for_upload

    messagesToUser = []

    projectLocation = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    info_path = os.path.join(projectLocation, "project_info.json")

    if not os.path.isfile(info_path):
        appendMessage(messagesToUser,
                      "Project info not found. Upload a project first.",
                      stage="Start")
        return makeResponse(messagesToUser, 400, True)

    with open(info_path, "r", encoding="utf-8") as f:
        project_info = json.load(f)

    strategy = project_info.get("data_strategy")

    # Strategies that don't need externalization
    if strategy in ("no_data", "embed", "external_doi", None):

        appendMessage(
            messagesToUser,
            f"Data strategy is '{strategy}' — no externalization needed.",
            stage="FindProjectFiles",
        )
        return makeResponse(messagesToUser, 200, True)

    # All three externalize strategies reach here
    data_dir = os.path.join(projectLocation, "data")
    if not os.path.isdir(data_dir):
        appendMessage(messagesToUser,
                      "No data/ folder found in project.",
                      stage="Start")
        return makeResponse(messagesToUser, 400, True)

    # Accept full metadata override from JSON body (sent by frontend after inference)
    req_data = request.get_json(silent=True) or {}
    metadata_override = req_data.get("metadata") if req_data else None

    # Also support legacy form fields
    creator_name = (request.form.get("creator_name") or req_data.get("creator_name") or "").strip() or None
    description = (request.form.get("description") or req_data.get("description") or "").strip() or None

    try:
        # ---- Branch A: externalize_files — upload individually, no tar ----
        if strategy == "externalize_files":
            # Collect all files with paths relative to data_dir (used as Zenodo names)
            file_paths = []
            for root, _dirs, filenames in os.walk(data_dir):
                for fname in filenames:
                    abs_path = os.path.join(root, fname)
                    rel_name = os.path.relpath(abs_path, data_dir).replace("\\", "/")
                    file_paths.append({"path": abs_path, "zenodo_name": rel_name})

            if metadata_override:
                metadata_json = {"metadata": metadata_override}
            else:
                metadata_json = _build_zenodo_metadata(projectUuid, creator_name, description)
            dep = _create_zenodo_deposition_multi_file(file_paths, metadata_json)

            # Build file list from published deposition response
            zenodo_files = []
            for zf in dep.get("files", []):
                zenodo_files.append({
                    "filename": zf.get("filename") or zf.get("key", ""),
                    "size_bytes": zf.get("filesize", zf.get("size", 0)),
                    "md5": zf.get("checksum", "").replace("md5:", ""),
                    "download_url": (zf.get("links") or {}).get("download", ""),
                })

            project_info["zenodo_doi"] = dep.get("doi")
            project_info["zenodo_record_id"] = str(dep.get("id", ""))
            project_info["zenodo_files"] = zenodo_files

            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(project_info, f, indent=2, ensure_ascii=False)

            _build_manifest(projectLocation, projectUuid)

            doi = dep.get("doi", "")
            summary = (
                f"Upload complete. Your dataset ({len(file_paths)} file(s)) is now on Zenodo.\n"
                f"DOI: {doi}\n"
                f"I'll link it to your research artifact automatically."
            )
            appendMessage(messagesToUser, content={
                "summary": summary,
                "doi": doi,
                "num_files": len(file_paths),
                "zenodo_files": zenodo_files,
                "data_strategy": strategy,
            }, contentShort=summary, stage="FindProjectFiles")

            return makeResponse(messagesToUser, 200, True)

        # ---- Branch B: externalize / chunk_and_externalize — tar + upload ----
        # ---- 1) Tar and chunk ----
        work_dir = os.path.join(projectLocation, "_upload_work")
        prep = prepare_dataset_for_upload(data_dir, work_dir, zenodo_limit=ZENODO_RECORD_LIMIT_BYTES)

        depositions = []

        if not prep["chunked"]:
            # ---- 2a) Single tar upload ----
            if metadata_override:
                metadata_json = {"metadata": metadata_override}
            else:
                metadata_json = _build_zenodo_metadata(projectUuid, creator_name, description)
            tar_info = prep["files_to_upload"][0]
            dep = _create_zenodo_deposition_from_path(
                tar_info["path"], tar_info["filename"], metadata_json
            )
            depositions.append({
                "deposition_id": dep.get("id"),
                "doi": dep.get("doi"),
                "record_id": str(dep.get("record_id", dep.get("id", ""))),
                "filename": tar_info["filename"],
                "tar_md5": prep["tar_md5"],
                "files": dep.get("files", []),
            })
        else:
            # ---- 2b) Chunked upload — one deposition per chunk ----
            for i, chunk in enumerate(prep["chunks"], 1):
                label = f"chunk {i} of {len(prep['chunks'])}"
                if metadata_override:
                    import copy as _copy
                    _m = _copy.deepcopy(metadata_override)
                    _m["title"] = str(_m.get("title", projectUuid)) + f" ({label})"
                    metadata_json = {"metadata": _m}
                else:
                    metadata_json = _build_zenodo_metadata(
                        projectUuid, creator_name, description, chunk_label=label
                    )
                dep = _create_zenodo_deposition_from_path(
                    chunk["path"], chunk["filename"], metadata_json
                )
                depositions.append({
                    "deposition_id": dep.get("id"),
                    "doi": dep.get("doi"),
                    "record_id": str(dep.get("record_id", dep.get("id", ""))),
                    "chunk_id": chunk["chunk_id"],
                    "filename": chunk["filename"],
                    "chunk_md5": chunk["md5"],
                    "chunk_size": chunk["size"],
                    "files": dep.get("files", []),
                })

        # ---- 3) Update project_info.json ----
        project_info["zenodo_depositions"] = depositions
        project_info["tar_md5"] = prep["tar_md5"]
        project_info["tar_size"] = prep["tar_size"]
        project_info["chunked"] = prep["chunked"]
        project_info["num_chunks"] = len(prep["chunks"]) if prep["chunked"] else 0

        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(project_info, f, indent=2, ensure_ascii=False)

        # ---- 4) Clean up work directory ----
        shutil.rmtree(work_dir, ignore_errors=True)

        # ---- 5) Build reproducibility manifest (Step 4) ----
        _build_manifest(projectLocation, projectUuid)

        # ---- 6) Return result ----
        doi_list = [d["doi"] for d in depositions if d.get("doi")]
        if prep["chunked"]:
            summary = (
                f"Upload complete. Your dataset was split into {len(depositions)} chunk(s) "
                f"across {len(depositions)} Zenodo record(s).\n"
                f"DOI(s): {', '.join(doi_list)}\n"
                f"I'll link all chunks to your research artifact automatically."
            )
        else:
            summary = (
                f"Upload complete. Your dataset is now on Zenodo.\n"
                f"DOI: {doi_list[0] if doi_list else 'N/A'}\n"
                f"I'll link it to your research artifact automatically."
            )

        appendMessage(messagesToUser, content={
            "summary": summary,
            "depositions": depositions,
            "data_strategy": strategy,
        }, contentShort=summary, stage="FindProjectFiles")

        return makeResponse(messagesToUser, 200, True)

    except Exception as e:
        import traceback
        # Clean up the work directory to avoid leaving huge tar/chunk files on disk
        work_dir = os.path.join(projectLocation, "_upload_work")
        shutil.rmtree(work_dir, ignore_errors=True)
        print(f"Externalization error: {e}")
        traceback.print_exc()
        appendMessage(messagesToUser,
                      f"Error externalizing data: {str(e)}",
                      stage="Start")
        return makeResponse(messagesToUser, 500, True)


@project_bp.route("/project/find_files", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/find_files.yml")
def find_files_project():
    requestData = json.loads(request.data)
    messagesToUser = []

    if "possibleProjectUuid" not in requestData:
        print("I can't select your folder")
        appendMessage(messagesToUser, "I can't select your folder", stage="Start")
        return makeResponse(messagesToUser, 201, True)

    possibleProjectUuid = requestData["possibleProjectUuid"]
    possibleDirectoryPath = f"projects/{possibleProjectUuid}/files"

    if not os.path.exists(possibleDirectoryPath):
        askmessage = {"role": "system",
                      "jsonObject": False,
                      "contentShort": None,
                      "content": "Analyze the following message and determine whether it contains a possible folder name."
                                 '\nMessage: ' + possibleProjectUuid +
                                 '\nIf it does, please provide the folder name. If it does not, respond with "NO"'
                                 '\nPlease answer in the required format, with a one-word response.'}

        projectUuid = callGPTModel([askmessage])

        if projectUuid == "NO":
            print("possibleProjectUuid: No")
            appendMessage(messagesToUser, "Please enter a valid location", stage="Start")
            return makeResponse(messagesToUser)

        directoryPath = f"projects/{projectUuid}/files"
        if not os.path.exists(directoryPath):
            print(f"Project '{directoryPath}' does not exist.")
            appendMessage(messagesToUser, f"Project '{projectUuid}' does not exist.\n Please enter a valid location",
                          stage="Start")
            return makeResponse(messagesToUser)
    else:
        projectUuid = possibleProjectUuid

    print("possibleProjectUuid:" + projectUuid)
    directoryPath = f"projects/{projectUuid}/files"

    all_files = find_files(directoryPath)
    number_interactions = 3
    chat_message = ""
    messagesToChat = []

    try:
        while number_interactions >= 0:
            userContent = (
                    chat_message +
                    'The stage of this iteration is: FindProjectFiles.\n' +
                    'The current task is to classify the provided list of files into two categories:\n' +
                    '1. Executable Files: Files that can be executed to perform a specific task (e.g., .exe, .sh, .py).\n' +
                    '2. Configuration/Installation Files: Files that contain information used to configure the execution of other files or install software components (e.g., .yaml, .json, .cfg, .ini, .deb).\n' +
                    'Some files may not belong to either category and can be left out.\n' +
                    'Please format your response in valid JSON as follows:\n' +
                    '{"ExecutableFiles": [List of executable files], "ConfigurationFiles": [List of configuration and installation files]}\n' +
                    'For example:\n' +
                    '{ "ExecutableFiles": ["file1.exe", "file2.py", "file3.sh", "folder1/file2.py"], ' +
                    '"ConfigurationFiles": ["config.yaml", "setup.ini", "install.deb"] }\n' +
                    'If a file does not fit into either category, do not include it in the response.'
            )

            chatContent = userContent + '\nHere are the name of the files to classify: ' + str(all_files)

            appendMessage(messagesToChat, role="system", content=chatContent)

            # TODO descomentar
            messageText = callGPTModel(messagesToChat)

            messageText = messageText.replace("```", "").replace("json", "")
            print("find_files_project" + messageText)

            try:
                userMessage = json.loads(messageText)
                if len(userMessage["ExecutableFiles"]) > 0:
                    userMessage["ProjectUuid"] = projectUuid
                    appendMessage(messagesToUser, content=userMessage,
                                  contentShort="I've found your project.",
                                  stage="ParametersToUse")
                else:
                    userMessage["ProjectUuid"] = projectUuid
                    appendMessage(messagesToUser, content=userMessage,
                                  contentShort="I've found your project, but I couldn't detect any executable files. Please verify your upload.",
                                  stage="ParametersToUse")
                return makeResponse(messagesToUser)

            except Exception as e:
                number_interactions -= 1
                print("number_interactions" + str(number_interactions))
                print("Error:" + str(e))
                chat_message = ("The previous result is incorrect. Ups! There's an error:" + str(e) +
                                "\nPlease consider the following information.\n")

    except Exception as error:
        appendMessage(messagesToUser, content="Ups! There's an error:" + str(error),
                      contentShort="Ups! There's an error:" + str(error), stage="Start")
        return makeResponse(messagesToUser)

    appendMessage(messagesToUser, content="Ups! some error occurred", contentShort="Ups! some error occurred",
                  stage="Start")
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/parameters-to-use-confirmation', methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/parameters-to-use-confirmation.yml")
def parameters_to_use_confirmation(projectUuid):
    requestData = json.loads(request.data)
    messagesToUser = []

    messagesToChat = return_messages(requestData, messagesToUser)

    length = len(messagesToChat)
    myMessage = messagesToChat[length - 1]["content"]

    try:
        message1confirmation = {
            "role": "system",
            "jsonObject": False,
            "content": (
                    "The stage of this iteration is: ParametersToUse\n"
                    "The user has provided a command to run their experiment. "
                    "The available project files are stored in the `ExecutableFiles` variable from a previous message.\n"
                    "Message: " + myMessage +
                    "\n\nYour task:\n"
                    "1. Extract the run command from the message.\n"
                    "2. Fix minor issues automatically — for example:\n"
                    "   - Convert absolute paths to relative (e.g. '/main.py' → './main.py')\n"
                    "   - Ensure the command uses Unix syntax\n"
                    "   - Correct obvious typos in filenames if the correct file exists in ExecutableFiles\n"
                    "3. Reply with ONLY the corrected command string (no explanation).\n"
                    "4. If the message contains no recognisable command at all (e.g. it is a question or unrelated text), "
                    "reply with exactly 'ParametersToUse'.\n"
            ),
            "contentShort": None
        }

        messagesToChat.append(message1confirmation)

        messageText = callGPTModel(messagesToChat).strip()

        if messageText == "ParametersToUse":
            appendMessage(messagesToUser,
                          "I couldn't find a run command in your message. "
                          "Please provide the command to execute your experiment, "
                          "for example: python ./main.py",
                          stage="ParametersToUse")
        else:
            # New command confirmed — clear stale run artifacts from any previous run.
            # Dockerfile, data, output_spec, and reproducibility_manifest are preserved:
            # the manifest holds Zenodo deposition info that is independent of the command.
            # It is only cleared when the user goes back to the data stage.
            _project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
            for _name in ("output", "run_progress.json"):
                _path = os.path.join(_project_location, _name)
                try:
                    if os.path.isdir(_path):
                        shutil.rmtree(_path, ignore_errors=True)
                    elif os.path.isfile(_path):
                        os.remove(_path)
                except Exception:
                    pass

            appendMessage(messagesToUser, content=messageText,
                          contentShort="I will use this command to execute the experiment.\n Command: " + messageText,
                          stage="SpecifyOutputs")
        return makeResponse(messagesToUser)

    except Exception as error:
        print(str(error))
        appendMessage(messagesToUser, content="Ups! There's an error " + str(error),
                      contentShort="Ups! There's an error " + str(error), stage="Start")
        return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/specify-outputs', methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/specify-outputs.yml")
def specify_outputs(projectUuid):
    """Save the researcher's output file specification for this experiment."""
    requestData = json.loads(request.data)
    messagesToUser = []

    # Skip path: frontend sends skip=true when user clicks "Skip"
    if requestData.get("skip"):
        print(f"[specify-outputs] Skipped for {projectUuid}")
        appendMessage(messagesToUser,
                      content="No output files will be captured.",
                      contentShort="Output capture skipped.",
                      stage="FindConfigurations")
        return makeResponse(messagesToUser)

    messages = requestData.get("messages", [])
    if not messages:
        appendMessage(messagesToUser, "Please specify the output files or directory.", stage="SpecifyOutputs")
        return makeResponse(messagesToUser)

    # Find the last user message and the assistant's suggested paths (if any)
    user_msg = ""
    suggested_paths = ""
    for msg in reversed(messages):
        role = msg.get("role", "")
        raw = msg.get("content") or ""
        content = (raw if isinstance(raw, str) else "").strip()
        if role != "assistant" and not user_msg:
            user_msg = content
        elif role == "assistant" and "detected" in content.lower() and not suggested_paths:
            # Extract the suggested paths from the detection message
            import re as _re
            m = _re.search(r'`([^`]+)`', content)
            if m:
                suggested_paths = m.group(1).strip()

    if not user_msg:
        appendMessage(messagesToUser, "Please specify the output files or directory.", stage="SpecifyOutputs")
        return makeResponse(messagesToUser)

    # Use GPT to extract actual file paths from the user's message
    context = f"The system previously suggested these output paths: {suggested_paths}\n" if suggested_paths else ""
    gpt_prompt = [{
        "role": "user",
        "content": (
            f"{context}"
            f"The user replied: \"{user_msg}\"\n\n"
            "Extract the output file paths or glob patterns the user wants to capture. "
            "Rules:\n"
            "- If the user confirms (e.g. 'yes', 'correct', 'that's right', 'looks good') → return the suggested paths as-is\n"
            "- If the user provides specific paths → return those\n"
            "- If the user confirms AND adds more paths → combine all of them\n"
            "- If the user rejects the suggestion without providing paths "
            "  (e.g. 'no', 'wrong', 'that's not right', 'incorrect') → return 'REJECTED'\n"
            "- Return ONLY the space-separated paths/patterns, or 'REJECTED', or 'UNCLEAR'\n"
            "- If you cannot determine intent at all, return 'UNCLEAR'"
        )
    }]

    extracted = callGPTModel(gpt_prompt).strip()

    if extracted == "REJECTED":
        appendMessage(messagesToUser,
                      "No problem. Please type the correct output paths or glob patterns directly.\n"
                      "For example: `output/results.csv output/plot.png` or `results/`",
                      stage="SpecifyOutputs")
        return makeResponse(messagesToUser)

    if extracted == "UNCLEAR" or not extracted:
        appendMessage(messagesToUser,
                      "I couldn't identify any file paths. Please specify the output files directly.\n"
                      "For example: `output/results.csv output/plot.png` or `results/`",
                      stage="SpecifyOutputs")
        return makeResponse(messagesToUser)

    output_spec = extracted

    # Save to project folder
    spec_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "output_spec.txt")
    with open(spec_path, "w", encoding="utf-8") as f:
        f.write(output_spec)

    print(f"[specify-outputs] Saved output spec for {projectUuid}: {output_spec!r}")

    appendMessage(messagesToUser,
                  content=output_spec,
                  contentShort=f"Output specification saved: {output_spec}",
                  stage="FindConfigurations")
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/infer-output-folder', methods=['GET'])
@cross_origin()
@require_auth
def infer_output_folder(projectUuid):
    """
    Scan code files for file-write patterns and ask GPT which output folder
    the experiment writes to. Returns { folder: str | null }.
    """
    files_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "files")
    if not os.path.isdir(files_location):
        return jsonify({"folder": None})

    patterns = _infer_output_folder_with_gpt(files_location)
    return jsonify({"folder": patterns})


@project_bp.route('/project/<projectUuid>/run-progress', methods=['GET'])
@cross_origin()
@require_auth
def get_run_progress(projectUuid):
    """Return current run_progress.json for the project (polled by the frontend)."""
    progress_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "run_progress.json")
    if os.path.isfile(progress_path):
        with open(progress_path, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    return jsonify({"phase": "waiting", "detail": ""})


@project_bp.route('/project/<projectUuid>/find-configurations', methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/find_configurations.yml")
def find_configurations(projectUuid):
    directoryPath = f"projects/{projectUuid}/files"
    requestData = json.loads(request.data)
    messagesToUser = []
    messagesToChat = []

    if "filenames" not in requestData:
        appendMessage(messagesToUser, 'filenames are missing', stage="Start")
        return makeResponse(messagesToUser)
    filenames = requestData["filenames"]

    commandToRun = return_commands_to_use(requestData, messagesToUser)

    all_files_lines = {}
    try:
        for filename in filenames:
            full_path = os.path.join(directoryPath, filename)
            if os.path.isfile(full_path):
                lines = read_first_50_lines(full_path)
                all_files_lines[filename] = lines
            else:
                print(f"File not found: {filename}")
    except Exception as error:
        appendMessage(messagesToUser, str(error), stage="Start")
        return makeResponse(messagesToUser)

    # Convert the content to JSON format
    filesContent = json.dumps(all_files_lines, indent=4)

    numberInteractions = 3
    chat_message = ""
    try:
        while numberInteractions >= 0:
            message1 = {"role": "system",
                        "jsonObject": False,
                        "contentShort": None,
                        "content": chat_message + "The current stage of this interaction is: FindConfigurations"
                                                  "\nGiven the JSON containing the name of the files, the first 50 lines of each file and the command used to execute this project, determine the following:"
                                                  "\nThe programming language of the files."
                                                  "\nThe version of these languages."
                                                  "\nAny dependencies needed to execute the command."
                                                  "\nI am providing the first 50 lines of each file. Some of the imported dependencies may not be utilized within these lines, but please return all the imported and referenced dependencies present in the files that needs to be installed."
                                                  "\nIt is necessary to verify if all the dependencies is compatible with other dependencies and the programming language."
                                                  '\nProvide your response in the following format:'
                                                  '{ "PL": [all the programming languages used], "PLVersion": [all the programming language version],"Dependencies": [dependencies]}'
                                                  '\nEnsure the dependency names are correct. If the provided name is incorrect, adjust it. For example, in Python, to install the sklearn dependency, the correct command is pip install scikit-learn.'
                                                  '\nBe careful to return a result in which the version of the programming language and the dependencies used are compatible and format the result in JSON.'
                                                  "\nOnly put values that you can infer, don't put generic values"
                                                  '\nCommand To Use: ' + commandToRun +
                                   '\nExample response:'
                                   '\n{ "PL": ["Python"], "PLVersion": "Python 3.8", "Dependencies": ["pandas", "tqdm"]}'
                                   '\nPlease respond in the specified format. The answer should be exactly in json format.'
                        }

            messagesToUser.append(message1)
            myMessage = copy(message1)
            myMessage["content"] = myMessage["content"] + '\nThe first 50 lines of each file: ' + str(filesContent)
            messagesToChat.append(myMessage)

            messageText = callGPTModel(messagesToChat)
            messageText = messageText.replace("```", "").replace("json", "")

            print(messageText)
            try:
                appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True, stage="FindConfigurationsInteraction")
                return makeResponse(messagesToUser)
            except Exception as e:
                numberInteractions -= 1
                print("numberInteractions" + str(numberInteractions))
                print("Error:" + str(e))
                messagesToChat = []

                message1 = {"role": "system",
                            "jsonObject": False,
                            "contentShort": None,
                            "content": chat_message +
                                       "\nExtract from the following message a JSON in the required format."
                                       "\nMessage: " + messageText +
                                       '\nRequired JSON format: '
                                       '{ "PL": [programming language], "PLVersion": [programming language version], "Dependencies": [dependencies]}'
                                       '\nEnsure the response is in the specified JSON format. '
                            }
                messagesToChat.append(message1)

                messageText = callGPTModel(messagesToChat)
                messageText = messageText.replace("```", "").replace("json", "")

                print(messageText)
                try:
                    appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True,
                                  stage="BuildDockerFile")
                    return makeResponse(messagesToUser)
                except Exception as e:
                    print("numberInteractions" + str(numberInteractions))
                    chat_message = "The previous result is incorrect. I encountered this error: " + str(e) + \
                                   "\nPlease consider the following information.\n"

    except Exception as error:
        appendMessage(messagesToUser, content="Ups! There's an error:" + str(error),
                      contentShort="Ups! There's an error:" + str(error), stage="Start")
        return makeResponse(messagesToUser)

    appendMessage(messagesToUser, content="Ups! some error occurred", contentShort="Ups! some error occurred",
                  stage="Start")
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/find-configurations-change', methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/find-configurations-change.yml")
def find_configurations_change(projectUuid):
    requestData = json.loads(request.data)
    messagesToUser = []
    messagesToChat = []

    if "myMessage" not in requestData:
        appendMessage(messagesToUser, "I can't find the messages", stage="Start")
        return makeResponse(messagesToUser)

    myMessage = requestData["myMessage"]

    message1 = {"role": "system",
                "jsonObject": False,
                "contentShort": None,
                "content": "The current stage of this interaction is: FindConfigurationsInteraction "
                           "\nCheck if the content of the user's action in the following message is positive or if the user wants to make changes."
                           "\nMessage: " + myMessage +
                           '\nConsider the following three options: '
                           '\nReply with "BuildDockerFile" if the content is positive.'
                           '\nReply with "WaitChatInteraction" if the content is negative, but no changes are proposed.'
                           '\nIf the user wants to make changes, such as use a specific dependency version, implement the proposed changes and provide your response in the following format: '
                           '{ "PL": [programming language], "PLVersion": [programming language version], "Dependencies": [dependency used, which may include the version]}'
                           '\nIf the user wants to make changes, ensure the response is in the specified JSON format. '
                           'If no changes are requested, the response should be a single word.'
                }

    messagesToUser.append(message1)
    messagesToChat.append(message1)

    # TODO descomentar
    messageText = callGPTModel(messagesToChat)

    if messageText == "BuildDockerFile" or messageText == "WaitChatInteraction":
        appendMessage(messagesToUser, content=messageText, contentShort=None, stage=messageText)
        return makeResponse(messagesToUser)
    else:
        messageText = messageText.replace("```", "").replace("json", "")
        try:
            appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True, stage="WaitChatInteraction")
            return makeResponse(messagesToUser)
        except Exception as e:
            numberInteractions = 3
            chat_message = ""
            while numberInteractions >= 0:
                message1 = {"role": "system",
                            "jsonObject": False,
                            "contentShort": None,
                            "content": chat_message +
                                       "\nI'll give you a message, and based on the settings used and the actions taken by the user, make the necessary changes."
                                       "\nMessage: " + myMessage +
                                       '\nImplement the proposed changes and provide your response in the following format: '
                                       '{ "PL": [programming language], "PLVersion": [programming language version], "Dependencies": [dependencies] }'
                                       '\nEnsure the response is in the specified JSON format. '
                            }
                messagesToChat.append(message1)

                # TODO descomentar
                messageText = callGPTModel(messagesToChat)
                messageText = messageText.replace("```", "").replace("json", "")
                print(messageText)
                # TODO comentar
                # messageText = '{"PL": "Python",  "PLVersion": "Python 3.10", "Dependencies": ["tqdm", "pandas", "shap","numpy", "matplotlib", "scikit-learn"],  "DependenciesVersion": ["shap==0.41.0", "numpy==1.23.4", "pandas==1.5.2", "scipy==1.9.3", "matplotlib==3.6.2", "tqdm==4.64.1"]}'

                try:
                    appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True,
                                  stage="BuildDockerFile")
                    return makeResponse(messagesToUser)
                except Exception as e:
                    numberInteractions -= 1
                    print("numberInteractions" + str(numberInteractions))
                    print("Error:" + str(e))
                    messagesToChat = []

                    message1 = {"role": "system",
                                "jsonObject": False,
                                "contentShort": None,
                                "content": chat_message +
                                           "\nExtract from the following message a JSON in the required format."
                                           "\nMessage: " + messageText +
                                           '\nRequired JSON format: '
                                           '{ "PL": [programming language], "PLVersion": [programming language version], "Dependencies": [dependencies]}'
                                           '\nEnsure the response is in the specified JSON format. '
                                }
                    messagesToChat.append(message1)

                    # TODO descomentar
                    messageText = callGPTModel(messagesToChat)
                    messageText = messageText.replace("```", "").replace("json", "")
                    print(messageText)

                    try:
                        appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True,
                                      stage="WaitChatInteraction")
                        return makeResponse(messagesToUser)
                    except Exception as e:
                        print("numberInteractions" + str(numberInteractions))
                        chat_message = "The previous result is incorrect. I encountered this error: " + str(e) + \
                                       "\nPlease consider the following information.\n"

        appendMessage(messagesToUser, content="Ups! some error occurred", contentShort="Ups! some error occurred",
                      stage="FindConfigurationsInteraction")
        return makeResponse(messagesToUser)


@project_bp.route("/project/<projectUuid>/build-docker-file-chat", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/build-docker-file-chat.yml")
def buildDockerFileChat(projectUuid):
    projectPath = 'projects/' + projectUuid + "/"

    requestData = json.loads(request.data)
    messagesToUser = []

    messagesToChat = return_messages(requestData, messagesToUser)

    # messages= ['projects/newproject\\main.py', 'projects/newproject\\main2.py', 'projects/newproject\\main3.py', 'projects/newproject\\new\\main.py', 'projects/newproject\\new\\main2.py', 'projects/newproject\\new\\main3.py', 'projects/newproject\\new\\newnew\\main2.py', 'projects/newproject\\new\\newnew\\main3.py']

    message1 = {"role": "system",
                "jsonObject": False,
                "contentShort": None,
                "content": "The stage of this interaction is: BuildDockerFile. "
                           "Check if you find this sentence in the conversation history. I tried to build the Docker image, but an error occurred "
                           "If so, you have to take the previous docker file into account so that you don't provide the same dockerfile because the previous one had an error."
                           'Please use the information I have provided, such as the dependencies, programming languages (PL), and programming language versions (PLVersion), to build a Dockerfile. '
                           'All the files I want to use are located in the files folder. Inside the container, I want all the files to remain in the files folder as well. '
                           'Therefore, the following two commands should be used: '
                           '"WORKDIR /files" and "COPY files/ ."'
                           '\nDo not use any additional COPY commands. '
                           'In previous messages, I provided the names of the configuration files (configurationFiles) present in the project. '
                           'You may use them if relevant, but it is not necessary to include COPY or ADD commands for these files, because they are inside the "./files" folder and have already been copied. '
                           'Please do not infer the names of any files not explicitly provided. '
                           'I only need the Dockerfile required to build the Docker image, so do not include the CMD or ENTRYPOINT commands in this Dockerfile. '
                           '\nIMPORTANT base image selection rules:'
                           '\n- For R projects: ALWAYS use rocker/r-ver:<version> instead of r-base:<version>. The rocker images have properly maintained apt package sources.'
                           '\n- For Python projects: use python:<version>-slim or python:<version>.'
                           '\n- For Julia projects: use julia:<version>.'
                           '\nProvide only the created Dockerfile, as I will use your response directly, no additional text or explanation is needed.'}
    messagesToChat.append(message1)

    # messageText = ''

    # TODO descomentar
    messageText = callGPTModel(messagesToChat)
    messageText = messageText.replace("Dockerfile", "").replace("dockerfile", "").replace("```", "")

    #####COnfirmaçao1
    messageVerify = {"role": "system",
                     "jsonObject": False,
                     "contentShort": None,
                     "content": 'Please verify that the content is a valid Dockerfile. '
                                '\nEnsure that all specified filenames exist when executing scripts to install dependencies or configure the system to avoid errors. '
                                'Do not execute commands for non-existent files. '
                                'All project files can be found in the `configurationFiles` field from previous messages. '
                                'Do not include any `COPY` commands other than "COPY files/ .". '
                                'Remove all other commands that copy information.'
                                'Do not add any `WORKDIR` commands other than "WORKDIR /files". '
                                'Remove all other commands that use the `WORKDIR` command. '
                                'Remove all other commands that use "CMD", "AND", or "ENTRYPOINT" commands. '
                                'IMPORTANT: If the base image is "r-base:<version>", replace it with "rocker/r-ver:<version>" to ensure apt sources work correctly. '
                                'Make the necessary changes and provide only the updated Dockerfile. '
                                'Here is the Dockerfile:' + messageText
                     }

    messagesToChat.append(messageVerify)

    # TODO descomentar
    messageText = callGPTModel(messagesToChat)
    messageText = messageText.replace("Dockerfile", "").replace("dockerfile", "").replace("```", "")

    # Ensure rocker images are used for R instead of r-base (which has broken apt sources)
    import re as _re
    messageText = _re.sub(r'FROM\s+r-base:([\w.\-]+)', r'FROM rocker/r-ver:\1', messageText)

    # TODO comentar
    #     messageText = """FROM python:3.10
    #
    # WORKDIR /files
    # COPY files/ .
    #
    # RUN pip install shap==0.41.0 numpy==1.23.4 pandas==1.5.2 scipy==1.9.3 matplotlib==3.6.2 tqdm==4.64.1"""

    write_file(projectPath + "Dockerfile", messageText)
    # For R (rocker) images: replace CRAN repos with Posit Package Manager (PPM),
    # which serves pre-compiled binaries for Ubuntu — no system dev-headers needed.
    import re as _re2
    _rocker_match = _re2.search(r'FROM\s+rocker/r-ver:([\d.]+)', messageText)
    if _rocker_match:
        _r_ver = _rocker_match.group(1)
        _major, _minor = int(_r_ver.split('.')[0]), int(_r_ver.split('.')[1]) if '.' in _r_ver else 0
        if (_major, _minor) >= (4, 3):
            _ubuntu = 'jammy'
        elif (_major, _minor) >= (4, 0):
            _ubuntu = 'focal'
        else:
            _ubuntu = 'focal'
        _ppm_url = f'https://packagemanager.posit.co/cran/__linux__/{_ubuntu}/latest'
        # Replace any common CRAN mirror URL with PPM binary URL
        messageText = _re2.sub(
            r'https://(?:cloud\.r-project\.org|cran\.rstudio\.com|cran\.r-project\.org)[^\'"]*',
            _ppm_url,
            messageText
        )
        # Add warn=2 to catch silent install failures — matches both R -e and Rscript -e
        messageText = _re2.sub(
            r'((?:Rscript|R)\s+-e\s+[\'"])(install\.packages)',
            r'\1options(warn=2); \2',
            messageText,
            flags=_re2.IGNORECASE
        )
        write_file(projectPath + "Dockerfile", messageText)


    # Patch Dockerfile for separately uploaded data:
    #  - embed strategy: COPY data/ /data bakes the data into the image
    #  - any strategy: if code references a different folder name (alias),
    #    add a symlink so /files/<alias> → /data resolves at runtime
    _project_info_path = projectPath + "project_info.json"
    if os.path.isfile(_project_info_path):
        with open(_project_info_path, "r", encoding="utf-8") as _pf:
            _pinfo = json.load(_pf)
        _strategy = _pinfo.get("data_strategy")
        _alias = _pinfo.get("data_folder_alias", "")
        # Reject any alias that isn't a plain folder name (safety check)
        if _alias and not re.match(r'^[\w.\-]+$', _alias):
            _alias = ""

        _df_lines = []
        if _strategy == "embed":
            _data_dir = os.path.join(projectPath, "data")
            if os.path.isdir(_data_dir) and os.listdir(_data_dir):
                _df_lines.append("COPY data/ /data")

        if _alias:
            _df_lines.append(f"RUN ln -s /data /files/{_alias}")

        if _df_lines:
            with open(projectPath + "Dockerfile", "a", encoding="utf-8") as _df:
                _df.write("\n" + "\n".join(_df_lines) + "\n")

    # Pre-create any output directories referenced in output_spec.txt so the
    # experiment never fails with "No such file or directory" when writing output.
    _spec_path = os.path.join(projectPath, "output_spec.txt")
    if os.path.isfile(_spec_path):
        with open(_spec_path, "r", encoding="utf-8") as _sf:
            _spec_paths = [s.strip() for s in _sf.read().split() if s.strip()]
        _output_dirs = sorted({
            os.path.dirname(p).strip("/")
            for p in _spec_paths
            if os.path.dirname(p).strip("/")  # skip files in root (no parent dir)
        })
        # Skip any directory already covered by the data alias symlink —
        # mkdir over a symlink fails with exit code 1.
        _output_dirs = {d for d in _output_dirs if d != _alias}
        if _output_dirs:
            _mkdir_args = " ".join(f"/files/{d}" for d in sorted(_output_dirs))
            with open(projectPath + "Dockerfile", "a", encoding="utf-8") as _df:
                _df.write(f"\nRUN mkdir -p {_mkdir_args}\n")

    try:
        appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True,
                      contentShort="Environment configuration ready.", stage="BuildDockerImage")
    except Exception as e:
        appendMessage(messagesToUser, content=messageText,
                      contentShort="Environment configuration ready.", stage="BuildDockerImage")
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/chat-interation', methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/chat-interation.yml")
def chat_interation(projectUuid):
    requestData = json.loads(request.data)
    messagesToUser = []

    messagesToChat = return_messages(requestData, messagesToUser)

    length = len(messagesToChat)
    myMessage = messagesToChat[length - 1]["content"]
    # messages= ['projects/newproject\\main.py', 'projects/newproject\\main2.py', 'projects/newproject\\main3.py', 'projects/newproject\\new\\main.py', 'projects/newproject\\new\\main2.py', 'projects/newproject\\new\\main3.py', 'projects/newproject\\new\\newnew\\main2.py', 'projects/newproject\\new\\newnew\\main3.py']

    if "nextStep" in requestData:
        nextStep = requestData["nextStep"]
    else:
        appendMessage(messagesToUser, 'NextStep is missing', stage="Start")
        return makeResponse(messagesToUser)

    numberInteractions = 3
    chatMessage = ""

    try:
        while numberInteractions >= 0:
            # TODO descomentar
            message1 = {"role": "system",
                        "jsonObject": False,
                        "contentShort": None,
                        "content": chatMessage + "Please evaluate the following message content: "
                                                 "If the message propose possible changes, reply with 'CHANGE' "
                                                 "If the message is positive or indicates agreement, respond with '" + nextStep +
                                   "'. If the message is negative and indicates disagreement, but does not propose possible changes, respond with 'WaitChatInteraction' "
                                   "\nPlease respond in the specified format. The answer should be exactly one word."
                                   "\nMessage: " + myMessage + ""
                        }
            messagesToUser.append(message1)
            messagesToChat.append(message1)

            messageText = callGPTModel(messagesToChat)

            palavras = messageText.split()
            if len(palavras) == 1:
                if messageText == "CHANGE":
                    message1 = {
                        "role": "system",
                        "jsonObject": False,
                        "contentShort": None,
                        "content": chatMessage + 'Please evaluate the following message:\n'
                                                 'Reply with "FindConfigurationsInteraction" if the message relates to the configuration '
                                                 'of the computing environment (e.g., dependency versions or missing programming languages).\n'
                                                 'Reply with "ProjectLocation" if the message involves changing the location of the project.\n'
                                                 'Reply with "ParametersToUse" if the message concerns the command used to run a computational experiment.\n'
                                                 '\nPlease respond in the specified format. The answer should be exactly one word.\n'
                                                 'Message: "' + myMessage + '"'
                    }

                    messagesToUser.append(message1)
                    messagesToChat.append(message1)

                    messageText = callGPTModel(messagesToChat)

                    words = messageText.split()
                    if len(words) == 1:
                        appendMessage(messagesToUser, content=messageText, contentShort="", stage=messageText)
                        return makeResponse(messagesToUser)
                    else:
                        chatMessage = "The previous result is incorrect. Please consider the following information.\n"
                        numberInteractions -= 1
                        print("numberInteractions" + str(numberInteractions))
                else:
                    appendMessage(messagesToUser, content=messageText, contentShort="", stage=messageText)
                    return makeResponse(messagesToUser)
            else:
                chatMessage = "The previous result is incorrect. The answer should be exactly one word. Please consider the following information.\n"
                numberInteractions -= 1
                print("numberInteractions" + str(numberInteractions))
                # appendMessage(messagesToUser, content=str(e), contentShort=str(e), stage="Start")

    except Exception as error:
        appendMessage(messagesToUser, content=str(error), contentShort=str(error), stage="Start")
        return makeResponse(messagesToUser)

    appendMessage(messagesToUser, content="Ups! some error occurred", contentShort="Ups! some error occurred",
                  stage="Start")
    return makeResponse(messagesToUser)


@project_bp.route("/project/<projectUuid>/build-docker-image-chat", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/build-docker-image-chat.yml")
def buildDockerImageChat(projectUuid):
    projectPath = 'projects/' + projectUuid + "/"
    requestData = json.loads(request.data)
    messagesToUser = []

    try:
        dockerClientResult = startDockerClient()
        dockerClient, port = dockerClientResult["dockerClient"], dockerClientResult["port"]
        number = datetime.now(cfg.timezone).strftime("%Y%m%d%H%M%S")
        # TODO comentar
        # try:
        #     # Build the image and stream the logs in real-time
        #     dockerImageBuilt = dockerClient.images.build(path=projectPath, tag=projectUuid + ":" + number, rm=True,
        #                                                  stream=True)
        #     # Loop through the logs and print them in real-time
        #     for chunk in dockerImageBuilt:
        #         if 'stream' in chunk:
        #             print(chunk['stream'].strip())  # Print the log messages in real-time
        #         else:
        #             print(chunk)  # Catch other potential messages (like errors)
        # except Exception as e:
        #     raise Exception(str(e))

        # For embed strategy: copy data/ into files/data/ so it gets baked
        # into the Docker image via "COPY files/ ." in the Dockerfile.
        _proj_info_path = os.path.join(projectPath, "project_info.json")
        if os.path.isfile(_proj_info_path):
            with open(_proj_info_path) as _pf:
                _pi = json.load(_pf)
            if _pi.get("data_strategy") == "embed":
                _data_src = os.path.join(projectPath, "data")
                _data_dst = os.path.join(projectPath, "files", "data")
                if os.path.isdir(_data_src) and not os.path.exists(_data_dst):
                    shutil.copytree(_data_src, _data_dst)
                    print(f"  [embed] Copied data/ into files/data/ for Docker build")

        dockerImageBuilt = dockerClient.images.build(path=projectPath, tag=projectUuid + ":" + number, rm=True)
        dockerImageBuiltFiltered = [s for s in dockerImageBuilt[0].tags if projectUuid in s]
        dockerTagslength = len(dockerImageBuiltFiltered) - 1

        messageText = dockerImageBuiltFiltered[dockerTagslength]

        # TODO comentar
        # messageText = "e25:20240917151400"
        # raise Exception("gcc: error: -E or -x required when input is from standard input")

        appendMessage(messagesToUser, content=messageText,
                      contentShort="Environment built successfully.", stage="RunContainer")
        return makeResponse(messagesToUser)
    except Exception as e:
        print(str(e))
        dockerfile_content = read_file(projectPath + "Dockerfile")
        message11 = {
            "role": "system",
            "jsonObject": False,
            "contentShort": None,
            "content": 'The stage of this iteration is: BuildDockerImage.\n'
                       'I have this Dockerfile:\n' + dockerfile_content +
                       '\nI tried to build the Docker image, but an error occurred:\n' + str(e) +
                       '\nIs the error due to the Dockerfile? If so, just reply "YES". '
                       'If the problem is not with the Dockerfile, just reply "NO".'
        }
        messagesToChat = []
        messagesToChat.append(message11)

        messageText = callGPTModel(messagesToChat)

        if messageText == "YES":
            appendMessage(messagesToUser,
                          content='\nI have this Dockerfile:' + dockerfile_content +
                                  '\nI tried to build the Docker image, but an error occurred:' + str(e),
                          contentShort='An error occurred during the environment build. I propose to try to build a new environment.',
                          stage="FindConfigurations", goBack=True)
        else:
            errorMessage = (
                    "An error occurred during the environment build. \n"
                    "Error: " + str(e) + "\n"
                                         "What might have caused this unexpected result?")
            examples = ("I want to change the execution parameters.\n"
                        "I want to change the project location.\n"
                        "I want to change the computing environment used (programming languages, dependencies).\n"
                        )
            appendMessage(messagesToUser, errorMessage, stage="WaitChatInteraction", examples=examples)

        return makeResponse(messagesToUser)


@project_bp.route("/project/<projectUuid>/run-container-chat", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/run-container-chat.yml")
def runDockerContainerChat(projectUuid):
    projectPath = cfg.PROJECTS_LOCATION + "/" + projectUuid + "/"
    directoryPath = f"/projects/{projectUuid}/files"  # path inside the container

    requestData = json.loads(request.data)
    messagesToUser = []

    commandToRun = return_commands_to_use(requestData, messagesToUser)

    if "dockerImageId" not in requestData:
        appendMessage(messagesToUser, "The dockerImageId is required", stage="BuildDockerFile")
        return makeResponse(messagesToUser)

    dockerImageId = requestData["dockerImageId"]
    number_of_attempts = 3

    while number_of_attempts >= 0:
        try:
            dockerClientResult = startDockerClient()
            dockerClient, port = dockerClientResult["dockerClient"], dockerClientResult["port"]

            projectImage = dockerClient.images.get(dockerImageId)
            now = datetime.now()
            number = now.strftime("%Y%m%d%H%M%S")

            volumes = {cfg.HOST_VOLUME_PATH: {'bind': directoryPath, 'mode': 'rw'}}

            # ------------------------------------------------------------------
            # Step 5: Data Provisioning (server-side, before container launch)
            # ------------------------------------------------------------------
            _manifest_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "reproducibility_manifest.json")
            _progress_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "run_progress.json")
            _project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
            _host_data_path = None

            # Priority 1: locally uploaded data (provide-data endpoint saves here).
            # Covers all file-upload strategies (embed, externalize, chunk_and_externalize).
            _local_data = os.path.join(_project_location, "data")
            _files_data = os.path.join(_project_location, "files", "data")

            if os.path.isdir(_local_data) and any(True for _ in os.scandir(_local_data)):
                _host_data_path = os.path.normpath(
                    os.path.join(cfg.HOST_VOLUME_PATH, projectUuid, "data"))
            elif os.path.isdir(_files_data) and any(True for _ in os.scandir(_files_data)):
                # Backward compat: data bundled inside the code zip
                _host_data_path = os.path.normpath(
                    os.path.join(cfg.HOST_VOLUME_PATH, projectUuid, "files", "data"))
            elif os.path.isfile(_manifest_path):
                # Priority 2: no local data — use manifest-based provisioning.
                # Handles external_doi strategy where data must be downloaded at runtime.
                with open(_manifest_path, "r", encoding="utf-8") as _mf:
                    _manifest = json.load(_mf)
                try:
                    _host_data_path = _provision_data(
                        _manifest, projectUuid, _project_location, _progress_path)
                except (RuntimeError, ValueError) as prov_err:
                    _write_run_progress(_progress_path, phase="error",
                                        detail=f"Provisioning failed: {prov_err}")
                    appendMessage(messagesToUser,
                                  f"Data provisioning failed: {prov_err}",
                                  stage="Start")
                    return makeResponse(messagesToUser)

            if _host_data_path:
                volumes[_host_data_path] = {'bind': '/data', 'mode': 'ro'}

            # Run the container
            _write_run_progress(_progress_path, phase="running",
                                detail="Running experiment...")
            container = dockerClient.containers.run(
                image=projectImage,
                name=projectUuid + "_" + number,
                volumes=volumes,
                detach=True,
                command="/bin/sh",
                tty=True
            )
            print(f"Container {container.name} started with ID {container.id}")

            # stdin=True: Allows you to pass input to the container via standard input.
            # You often use both together when running fully interactive sessions in containers. For example:
            # container.exec_run('/bin/bash', stdin=True, tty=True)

            # commandToRun= "python ./myfile.py && cd pasta && python ./myfile2.py && cd .. &&  python ./myfile3.py"
            exec_first = container.exec_run('/bin/sh -c "' + commandToRun + '"')

            containerLogs = "Command Output:" + exec_first.output.decode('utf-8') + "\n\n"
            exit_code = f"Exit Code: {exec_first.exit_code}\n"
            containerLogs += exit_code

            print(containerLogs)

            # ------------------------------------------------------------------
            # Output file extraction (if experiment succeeded + spec provided)
            # ------------------------------------------------------------------
            extracted = []
            if exec_first.exit_code == 0:
                _write_run_progress(_progress_path, phase="extracting",
                                    detail="Extracting output files...")
                spec_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "output_spec.txt")
                if os.path.isfile(spec_path):
                    with open(spec_path, "r", encoding="utf-8") as _sf:
                        output_specs = [s.strip() for s in _sf.read().split() if s.strip()]

                    workdir = "/files"
                    output_dir = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "output")
                    os.makedirs(output_dir, exist_ok=True)
                    extracted = []
                    missing = []

                    resolved_paths, missing = _resolve_spec_paths(
                        container, workdir, output_specs
                    )

                    for cpath, display_name in resolved_paths:
                        try:
                            bits, _ = container.get_archive(cpath)
                            with tarfile.open(fileobj=io.BytesIO(b''.join(bits))) as tf:
                                # Preserve relative path under output_dir
                                dest = os.path.join(output_dir, os.path.dirname(display_name))
                                os.makedirs(dest, exist_ok=True)
                                tf.extractall(dest, filter="data")
                            extracted.append(display_name)
                            print(f"  Extracted: {cpath} → {display_name}")
                        except Exception as ex:
                            print(f"  Could not extract {cpath}: {ex}")
                            missing.append(display_name)

                    if extracted:
                        containerLogs += (
                            f"\nOutput files captured ({len(extracted)}):\n"
                            + "\n".join(f"  - {f}" for f in extracted) + "\n"
                        )
                    else:
                        containerLogs += "\nNo output files matched the specified pattern.\n"
                    if missing:
                        containerLogs += (
                            f"\nNot produced ({len(missing)}):\n"
                            + "\n".join(f"  - {f}" for f in missing) + "\n"
                        )

            try:
                container.stop()
                container.remove()
            except Exception as e:
                print(str(e))

            # waitToConclude(container)
            # containerLogs = container.logs().decode("utf-8")
            # print(containerLogs)

            # # Compare snapshots to identify changes
            # added_files = {}
            # removed_files = {}
            # modified_files = {}
            #
            # added_files, removed_files, modified_files = process_container_diff(container.diff())
            # # Create the final JSON object
            # result = {
            #     "result": containerLogs,
            #     # "added_files": added_files,
            #     # "removed_files": removed_files,
            #     # "modified_files": modified_files
            # }
            #
            # # Convert the result to a JSON-formatted string
            # messageText = json.dumps(result)
            # print(messageText)

            # Prepare the message to be sent back to the user

            if exec_first.exit_code != 0:
                diagnosis = _interpret_run_error_with_gpt(containerLogs, commandToRun)
                errorMessage = (
                    "The experiment failed.\n\n"
                    + containerLogs + "\n"
                    + "**What went wrong:** " + diagnosis
                )
                appendMessage(messagesToUser, errorMessage, stage="RunFailed")
            else:
                messagesToUser = [
                    {"role": "assistant",
                     "content": containerLogs,
                     "contentShort": containerLogs,
                     "jsonObject": False,
                     "output_files": extracted if extracted else [],
                     }
                ]

            # changes = container.diff()

            # for change in changes:
            #   print(change['Kind'], change['Path'])

            # created_files = set()

            # for mount in container.attrs['Mounts']:
            #     if mount['Type'] == 'volume':
            #         volume_name = mount['Name']
            #         volume = dockerClient.volumes.get(volume_name)
            #         for file_info in volume.attrs['Mountpoint'].iterdir():
            #             if file_info.is_file():
            #                 created_files.add(str(file_info))

            # for file in created_files:
            #     print(file)

            return makeResponse(messagesToUser)
        except Exception as e:
            print(str(e))
            number_of_attempts -= 1
            dockerfile_content = read_file(projectPath + "Dockerfile")
            message11 = {
                "role": "system",
                "jsonObject": False,
                "contentShort": None,
                "content": 'The stage of this iteration is: RunContainer.\n'
                           'I have this Dockerfile:\n' + dockerfile_content +
                           '\nI built the Docker image, and the build was successful. '
                           'I used the following command to execute this container: ' + commandToRun +
                           '\nHowever, an error occurred during the execution of the container: ' + str(e) +
                           '\n\nConsider the following error and classify it according to its origin:'
                           '\n- If the problem stems from the construction of the computing environment, such as the version of the dependencies used or the absence of a programming language, then only answer "FindConfigurations".'
                           '\n- If the problem stems from negligence in writing the code, such as errors due to missing files or failing to import functions, then only answer "ProjectLocation".'
                           '\n- If the problem stems from the command used to run the computational experiment, then only answer "ParametersToUse".'
                           '\n\nPlease answer in the required format. The answer should be one word in length.'
            }

            messagesToChat = []
            messagesToChat.append(message11)

            messageText = callGPTModel(messagesToChat)

            message2 = {"role": "system",
                        "jsonObject": False,
                        "contentShort": "An error occurred. I propose that we go back and fix it. \n"
                                        "Message: " + str(e),
                        "content": "An error occurred. I propose that we go back and fix it. \n"
                                   "Message: " + str(e),
                        "stage": messageText}

            messagesToUser.append(message2)
            return makeResponse(messagesToUser)


# ---------------------------------------------------------------------------
# Universal context-aware chat
# ---------------------------------------------------------------------------

_UNIVERSAL_CHAT_SYSTEM_PROMPT = """\
You are an intelligent assistant embedded in SciConv, a tool that helps researchers \
package, reproduce, and publish computational experiments as reproducible artifacts.

=== WORKFLOW STAGES (in order) ===

1. FindProjectFiles
   The system scans the uploaded code ZIP to identify executable files and project structure.
   Automatic — no user input needed.

2. WaitForDataInput
   The system detected no data was included in the uploaded ZIP.
   The user must decide about their dataset before anything else can proceed:
     a) Upload a data file using the button already visible in the UI
     b) Type a Zenodo DOI (e.g. 10.5281/zenodo.1234567)
     c) Type "no data" or "skip" if no external data is needed
   Do NOT suggest the run command or any later step until this is resolved.

3. InferDatasetMetadata / ExternalizeData
   Dataset is archived and uploaded to Zenodo if needed. Automatic.

4. ParametersToUse
   User specifies the shell command to run the experiment.
   Example: "python main.py" or "Rscript run.R"
   Code is at /files/ inside Docker, data (if any) at /data/.

5. SpecifyOutputs
   User specifies which files/folders the experiment writes as output.
   Example: "output/results.csv output/plot.png" or "results/" or "output/*.csv"
   User can skip if no output files.

6. FindConfigurations → FindConfigurationsInteraction
   System detects required language, packages, system libraries.
   User confirms or adjusts the detected environment.

7. BuildDockerFile → BuildDockerImage
   System generates a Dockerfile and builds the Docker image. Both automatic.

8. RunContainer
   System runs the experiment inside Docker and extracts output files. Automatic.

9. WaitChatInteraction
   System asks if the result looks correct.
   User confirms → packaging begins. User reports a problem → recovery options.

10. RunFailed
    Experiment failed. User can fix run command, fix dependencies, fix output spec, or retry.

11. ResearchArtifact → Completed
    System packages everything into a ZIP. User can download or publish to Zenodo for a DOI.

=== VALID ACTION TAGS ===
Only append one of these tags at the END of your reply when you are certain the action \
is what the user wants:

  [ACTION:navigate:WaitForDataInput]        — go back to change/add the data file or DOI
  [ACTION:navigate:ParametersToUse]         — change run command
  [ACTION:navigate:SpecifyOutputs]          — change output file spec
  [ACTION:navigate:FindConfigurationsInteraction] — review/change environment
  [ACTION:navigate:BuildDockerFile]         — fix and rebuild Dockerfile
  [ACTION:navigate:RunContainer]            — retry the run immediately
  [ACTION:confirm]                          — user confirmed current prompt
  [ACTION:set_command]                      — user provided a run command
  [ACTION:set_outputs]                      — user provided output file paths
  [ACTION:update_config]                    — user wants to change environment config
  [ACTION:report_issue]                     — user reported a problem after the run
  [ACTION:reset]                            — user wants to start over and upload a new project file

=== CURRENT PROJECT STATE ===
{state_block}

=== CURRENT STAGE ===
The user is at stage: {current_stage}

=== CONVERSATION HISTORY ===
The messages below show what has already been said. Use them to fully understand the
context of the user's latest message. Do not repeat information already given.

=== HOW TO RESPOND ===
Reply in plain natural language, as a helpful assistant would.
Be concise (1–3 sentences). Be specific to the current stage.

Only append an [ACTION:...] tag when you are CERTAIN it is what the user intends.
If the message is ambiguous ("ok", "sure", "thanks") just reply naturally — NO action tag.
If the user is asking a question or needs guidance, reply and explain — NO action tag.
If the user explicitly asks to go somewhere or do something specific, reply and append the tag.

=== STAGE-SPECIFIC ACTION RULES (ALWAYS apply these) ===
At stage SpecifyOutputs:
  - "yes" / "correct" / "looks good" / any affirmative → ALWAYS append [ACTION:confirm]
  - A file path or glob pattern (e.g. "output/results.csv") → ALWAYS append [ACTION:set_outputs]
  Never reply at SpecifyOutputs without one of the above actions (unless the user is asking a question).

At stage ParametersToUse:
  - Any shell command (e.g. "python main.py") → ALWAYS append [ACTION:set_command]
  Never reply at ParametersToUse without [ACTION:set_command] (unless the user is asking a question).

=== SCOPE RESTRICTION ===
You are ONLY permitted to assist with tasks directly related to this experiment, the user's \
project files, or the SciConv packaging workflow described above.
If the user asks about anything outside this scope — general knowledge, unrelated coding \
questions, creative writing, opinions, or any other off-topic request — politely decline and \
redirect them to the current step. Do NOT answer the off-topic question even partially.
Example:
  User: "What is the capital of France?"
  Reply: I'm only able to help with your experiment and the SciConv workflow. \
Is there anything about your current step I can help with?

Examples:
  User: "go back and fix the dockerfile"
  Reply: Sure, taking you back to the Dockerfile. [ACTION:navigate:BuildDockerFile]

  User: "start over" / "upload a new file" / "go back to the beginning"
  Reply: Sure, taking you back to the start so you can upload a new file. [ACTION:reset]

  User: "python main.py"  (at ParametersToUse)
  Reply: Got it, I'll use that command to run your experiment. [ACTION:set_command]

  User: "ok"  (after being shown data options)
  Reply: No problem — whenever you're ready, use the buttons above to upload a file, \
enter a DOI, or click "No data".

  User: "what do I do here?"
  Reply: You're at the data step. Before we can proceed you need to tell me about your \
dataset — upload a file, provide a Zenodo DOI, or say "no data" if none is needed.
"""


def _build_state_block(project_location, project_uuid):
    """Summarise current project state for the universal chat system prompt."""
    lines = [f"Project UUID: {project_uuid}"]
    info_path = os.path.join(project_location, "project_info.json")
    if os.path.isfile(info_path):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            if info.get("mode"):
                lines.append(f"Mode: {info['mode']}")
            if info.get("data_strategy"):
                lines.append(f"Data strategy: {info['data_strategy']}")
        except Exception:
            pass
    spec_path = os.path.join(project_location, "output_spec.txt")
    if os.path.isfile(spec_path):
        try:
            lines.append(f"Output spec: {open(spec_path).read().strip()}")
        except Exception:
            pass
    manifest_path = os.path.join(project_location, "reproducibility_manifest.json")
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            cmd = (m.get("reconstruction") or {}).get("command", "")
            if cmd:
                lines.append(f"Run command: {cmd}")
        except Exception:
            pass
    progress_path = os.path.join(project_location, "run_progress.json")
    if os.path.isfile(progress_path):
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                prog = json.load(f)
            if prog.get("phase"):
                lines.append(f"Last run phase: {prog['phase']}")
            if prog.get("detail"):
                lines.append(f"Last run detail: {prog['detail']}")
        except Exception:
            pass
    return "\n".join(lines)


@project_bp.route("/project/<projectUuid>/universal-chat", methods=['POST'])
@cross_origin()
@require_auth
def universal_chat(projectUuid):
    """
    Context-aware chat that understands any user input and routes it to
    the right action: navigate to a stage, confirm, provide data, or explain.
    """
    data = json.loads(request.data)
    user_message = data.get("message", "").strip()
    current_stage = data.get("stage", "")
    recent_messages = data.get("recent_messages", [])  # [{role, content}, ...]

    if not user_message:
        return jsonify({"intent": "reply", "target_stage": None,
                        "reply": "Please type a message."}), 200

    project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    state_block = _build_state_block(project_location, projectUuid)

    system_prompt = _UNIVERSAL_CHAT_SYSTEM_PROMPT.format(
        current_stage=current_stage,
        state_block=state_block,
    )

    # Build messages: system prompt + conversation history + new user message
    messages = [{"role": "system", "content": system_prompt}]
    for m in recent_messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    try:
        raw = callGPTModel(messages).strip()
    except Exception as e:
        print(f"  [universal-chat] GPT error: {e!r}")
        return jsonify({"reply": "Something went wrong. Please try again.",
                        "action": None, "target_stage": None}), 200

    # Extract optional [ACTION:...] tag from the end of the reply
    action = None
    target_stage = None
    action_match = re.search(r'\[ACTION:([^\]]+)\]\s*$', raw)
    if action_match:
        raw = raw[:action_match.start()].strip()
        tag_parts = action_match.group(1).split(":")
        action = tag_parts[0]                              # e.g. "navigate", "confirm"
        target_stage = tag_parts[1] if len(tag_parts) > 1 else None  # e.g. "BuildDockerFile"

    print(f"  [universal-chat] action={action!r} target={target_stage!r} reply={raw[:80]!r}")
    return jsonify({"reply": raw, "action": action, "target_stage": target_stage}), 200


@project_bp.route("/project/<projectUuid>/research-artifact-chat", methods=['POST'])
@cross_origin()
@require_auth
@swag_from("../swagger/project/research-artifact-chat.yml")
def researchArtifactChat(projectUuid):
    projectPath = cfg.PROJECTS_LOCATION + "/" + projectUuid
    zipFilePath = f"{projectPath}.zip"

    requestData = json.loads(request.data)
    messagesToUser = []

    if "dockerImageId" not in requestData:
        appendMessage(messagesToUser, "The dockerImageId is required", stage="BuildDockerFile")
        return makeResponse(messagesToUser)
    dockerImageID = requestData["dockerImageId"]
    # dockerImageID = "20240820192103"

    # Save conversation history to file for persistence, but do NOT include it
    # in the response — returning the full history causes the frontend to re-push
    # every message and replay the entire workflow.
    all_messages = return_messages(requestData, [])
    write_messagesUser_to_file(all_messages, projectPath)

    commandToRun = return_commands_to_use(requestData, all_messages)
    commandToRun1 = [commandToRun]

    # Persist commandToRun into project_info.json so reproduce-from-doi can recover it
    _proj_info_path = os.path.join(projectPath, "project_info.json")
    if os.path.isfile(_proj_info_path):
        try:
            with open(_proj_info_path, "r", encoding="utf-8") as _pf:
                _proj_info = json.load(_pf)
            _proj_info["command_to_run"] = commandToRun
            with open(_proj_info_path, "w", encoding="utf-8") as _pf:
                json.dump(_proj_info, _pf, indent=2)
        except Exception:
            pass

    # Load manifest so run scripts know which data provisioning strategy to embed.
    # If the manifest is missing but project_info has data strategy info, rebuild it
    # (e.g. user went back to edit the command after Zenodo upload, which used to
    # delete the manifest as part of stale cleanup).
    manifest = None
    manifest_path = os.path.join(projectPath, "reproducibility_manifest.json")
    if not os.path.isfile(manifest_path):
        try:
            _build_manifest(projectPath, projectUuid)
        except Exception:
            pass
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as _mf:
            manifest = json.load(_mf)

    arrayFiles = writeWindowsFIle(projectPath + "/", projectUuid, commandToRun1, dockerImageID, False, None,
                                  manifest=manifest)
    arrayFiles += writeLinuxFile(projectPath + "/", projectUuid, commandToRun1, dockerImageID, False, None,
                                 manifest=manifest)

    # Copy provision_data.py only for strategies that need runtime data provisioning
    _needs_provisioning = (manifest or {}).get("data_strategy", "no_data") not in ("no_data", "embed")
    if _needs_provisioning:
        provision_src = os.path.join(os.path.dirname(__file__), "..", "helpers", "data_provisioning.py")
        provision_dst = os.path.join(projectPath, "provision_data.py")
        if os.path.isfile(provision_src) and not os.path.isfile(provision_dst):
            shutil.copy2(os.path.normpath(provision_src), provision_dst)

    try:
        saveDockerImage(projectPath, projectUuid, dockerImageID)
    except Exception as error:
        appendMessage(messagesToUser, content="Ups! There's an error:" + str(error),
                      contentShort="Ups! There's an error:" + str(error), stage="ResearchArtifact")
        return makeResponse(messagesToUser)

    # zip.write(projectPath + "/" + projectUuid + ".tar.gz", "./" + projectUuid + ".tar.gz")

    # Check if the zip file already exists
    if os.path.isfile(zipFilePath):
        raise FileExistsError(f"The zip file '{zipFilePath}' already exists.")

    def ignore_myfolder(directory, files):
        ignore_list = []
        # Source code — already baked into Docker image tar
        if 'files' in files:
            ignore_list.append('files')
        # Exclude final zip (avoid nesting), scratch dirs, provisioned data; keep output/ for comparison
        for _skip in (projectUuid + ".zip", "data", "data_provisioned",
                      "_upload_work"):
            if _skip in files:
                ignore_list.append(_skip)
        return ignore_list

    # Create a temporary directory
    with tempfile.TemporaryDirectory() as tempdir:
        # Copy everything from projectPath to the temporary directory, excluding 'myfolder'
        shutil.copytree(projectPath, tempdir, ignore=ignore_myfolder, dirs_exist_ok=True)

        # Create the zip archive from the temporary directory
        shutil.make_archive(projectPath, 'zip', tempdir)

    print(f"Archive created at {projectPath}.zip, excluding 'files'.")

    # Move the zip file to the desired location
    finalZipPath = os.path.join(projectPath, f"{projectUuid}.zip")
    shutil.move(zipFilePath, finalZipPath)

    print(f"All files and folders from '{projectPath}' have been zipped into '{finalZipPath}'.")
    appendMessage(messagesToUser, "Your research artifact is ready.",
                  stage="Completed")
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/infer-artifact-metadata', methods=['GET'])
@cross_origin()
@require_auth
def infer_artifact_metadata(projectUuid):
    """Infer a title and description for the artifact using GPT, without uploading."""
    from helpers.article.articleHelper import _sample_code_files

    _project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    _proj_info_path = os.path.join(_project_location, "project_info.json")
    _info = {}
    if os.path.isfile(_proj_info_path):
        try:
            with open(_proj_info_path, "r", encoding="utf-8") as _pf:
                _info = json.load(_pf)
        except Exception:
            pass

    _inferred_title = f"Research artifact: {projectUuid}"
    _inferred_description = (
        f"Reproducible research artifact for experiment {projectUuid}. "
        "Contains the Docker environment, run scripts, and reproducibility manifest."
    )

    try:
        _files_dir = os.path.join(_project_location, "files")
        _code_context = _sample_code_files(_files_dir) if os.path.isdir(_files_dir) else {}

        _output_dir = os.path.join(_project_location, "output")
        _output_files = []
        if os.path.isdir(_output_dir):
            for _root, _dirs, _fnames in os.walk(_output_dir):
                for _fn in _fnames:
                    _output_files.append(
                        os.path.relpath(os.path.join(_root, _fn), _output_dir).replace("\\", "/")
                    )

        _run_command = _info.get("run_command", "")
        _dataset_description = _info.get("dataset_zenodo_description", "")

        # Detect which run scripts are present in the artifact
        _win_scripts = sorted([
            f for f in os.listdir(_project_location)
            if f.startswith("runExperiment") and f.endswith(".bat")
        ])
        _linux_scripts = sorted([
            f for f in os.listdir(_project_location)
            if f.startswith("runExperiment") and f.endswith(".sh")
        ])

        _gpt_payload = {
            "project_uuid": projectUuid,
            "run_command": _run_command,
            "code_context": _code_context,
            "output_files": _output_files[:20],
            "dataset_description": _dataset_description,
        }
        _system = (
            "You are a research data curator writing Zenodo metadata for a reproducibility artifact.\n"
            "Given context about an experiment (code files, run command, outputs, dataset description), "
            "generate:\n"
            "1. A concise, descriptive title (max 15 words) — describe what the experiment does, "
            "not just that it is a 'research artifact'\n"
            "2. A clear description (2-4 sentences) explaining: what the experiment does, "
            "what data it uses, and what outputs it produces. "
            "Do NOT include run instructions — those will be appended separately.\n"
            "Return ONLY valid JSON: {\"title\": \"...\", \"description\": \"...\"}\n"
            "Do not include the project UUID in the title."
        )
        _raw = callGPTModel([
            {"role": "system", "content": _system},
            {"role": "user", "content": json.dumps(_gpt_payload, ensure_ascii=False)},
        ])
        _raw = _raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        _parsed = json.loads(_raw)
        if isinstance(_parsed.get("title"), str) and _parsed["title"].strip():
            _inferred_title = _parsed["title"].strip()
        if isinstance(_parsed.get("description"), str) and _parsed["description"].strip():
            _inferred_description = _parsed["description"].strip()

        # Append run instructions
        _run_section = "\n\n## How to Reproduce\n"
        _run_section += "Requirements: Docker must be installed and running.\n\n"
        if _win_scripts:
            _run_section += "**Windows:**\n"
            for _s in _win_scripts:
                _run_section += f"  {_s}\n"
        if _linux_scripts:
            _run_section += "**Linux / macOS:**\n"
            for _s in _linux_scripts:
                _run_section += f"  bash {_s}\n"
        if not _win_scripts and not _linux_scripts:
            _run_section += "Run the included `runExperiment.bat` (Windows) or `runExperiment.sh` (Linux/macOS).\n"
        _inferred_description += _run_section

    except Exception as _e:
        print(f"[infer_artifact_metadata] GPT inference failed: {_e}")

    return jsonify({
        "title": _inferred_title,
        "description": _inferred_description,
    })


@project_bp.route('/project/<projectUuid>/upload-artifact-to-zenodo', methods=['POST'])
@cross_origin()
@require_auth
def upload_artifact_to_zenodo(projectUuid):
    """Upload the project's artifact zip to a new Zenodo record and return its DOI."""
    requestData = json.loads(request.data) if request.data else {}
    messagesToUser = []

    zip_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, f"{projectUuid}.zip")
    if not os.path.isfile(zip_path):
        appendMessage(messagesToUser,
                      "Artifact zip not found. Please generate the research artifact first.",
                      stage="Completed")
        return makeResponse(messagesToUser)

    # Accept user-reviewed title/description/creator from the frontend editor
    creator_name = requestData.get("creator_name")
    title = requestData.get("title") or f"Research artifact: {projectUuid}"
    description = requestData.get("description") or (
        f"Reproducible research artifact for experiment {projectUuid}. "
        "Contains the Docker environment, run scripts, and reproducibility manifest."
    )

    # Read project_info to find any dataset DOI for linking
    _proj_info_path = os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "project_info.json")
    _dataset_doi = None
    _info = {}
    if os.path.isfile(_proj_info_path):
        try:
            with open(_proj_info_path, "r", encoding="utf-8") as _pf:
                _info = json.load(_pf)
            _dataset_doi = (
                _info.get("zenodo_doi")
                or (_info.get("zenodo_depositions") or [{}])[0].get("doi")
                or (_info.get("dataset") or {}).get("doi")
            )
        except Exception:
            pass

    metadata = {
        "title": title,
        "upload_type": "software",
        "description": description,
        "creators": [{"name": creator_name or "Unknown"}],
    }
    if _dataset_doi:
        metadata["related_identifiers"] = [{
            "identifier": _dataset_doi,
            "relation": "isDerivedFrom",
            "resource_type": "dataset",
        }]

    metadata_json = {"metadata": metadata}

    try:
        dep = _create_zenodo_deposition_from_path(zip_path, f"{projectUuid}.zip", metadata_json)
    except Exception as e:
        appendMessage(messagesToUser, f"Upload failed: {e}", stage="Completed")
        return makeResponse(messagesToUser)

    doi = dep.get("doi") or dep.get("metadata", {}).get("doi", "")
    zenodo_id = dep.get("id", "")
    zenodo_url = dep.get("links", {}).get("html", "")

    # Persist artifact DOI in project_info.json
    if _proj_info_path:
        try:
            _info["artifact_doi"] = doi
            _info["artifact_zenodo_id"] = zenodo_id
            with open(_proj_info_path, "w", encoding="utf-8") as _pf:
                json.dump(_info, _pf, indent=2)
        except Exception:
            pass

    _msg = f"Artifact uploaded to Zenodo. DOI: {doi}"
    if _dataset_doi:
        _msg += f"\nLinked to dataset DOI: {_dataset_doi}"
    appendMessage(messagesToUser,
                  content={"doi": doi, "zenodo_id": zenodo_id, "zenodo_url": zenodo_url},
                  contentShort=_msg,
                  stage="Completed")
    return makeResponse(messagesToUser)


@project_bp.route('/project/reproduce-from-doi/init', methods=['POST'])
@cross_origin()
@require_auth
def reproduce_from_doi_init():
    """
    Step 1 of reproduce-from-doi: resolve the artifact DOI, download the zip,
    extract it to a new project folder, and return the new project UUID.
    The client can then start polling run-progress and call /reproduce-run.
    """
    import requests as _req
    requestData = json.loads(request.data)
    artifact_doi = (requestData.get("artifact_doi") or "").strip()
    if not artifact_doi:
        return jsonify({"error": "artifact_doi is required"}), 400

    token = os.getenv("ZENODO_API_TOKEN")

    # Resolve DOI → Zenodo record
    try:
        parsed = _extract_zenodo_identifier(artifact_doi)
        if parsed.get("record_id"):
            record = _zenodo_fetch_by_id(parsed["record_id"], token)
        else:
            record = _zenodo_fetch_by_doi(parsed["doi"], token)
    except Exception as e:
        return jsonify({"error": f"Could not resolve DOI: {e}"}), 400

    # Find the artifact zip file in the record
    files = record.get("files") or record.get("entries") or []
    zip_entry = next((f for f in files if f.get("key", f.get("filename", "")).endswith(".zip")), None)
    if not zip_entry:
        return jsonify({"error": "No zip file found in the Zenodo record"}), 400

    download_url = (zip_entry.get("links", {}).get("self")
                    or zip_entry.get("links", {}).get("download")
                    or zip_entry.get("download_url", ""))
    zip_filename = zip_entry.get("key") or zip_entry.get("filename", "artifact.zip")

    # Create new project UUID
    new_uuid = "repro_" + datetime.now(cfg.timezone).strftime("%d%m_%H%M%S")
    new_project_location = os.path.join(cfg.PROJECTS_LOCATION, new_uuid)
    os.makedirs(new_project_location, exist_ok=True)

    # Write initial progress
    progress_path = os.path.join(new_project_location, "run_progress.json")
    _write_run_progress(progress_path, phase="downloading", detail="Downloading artifact from Zenodo...")

    # Download zip
    zip_local = os.path.join(new_project_location, zip_filename)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with _req.get(download_url, headers=headers, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(zip_local, "wb") as fout:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    fout.write(chunk)
    except Exception as e:
        shutil.rmtree(new_project_location, ignore_errors=True)
        return jsonify({"error": f"Download failed: {e}"}), 500

    # Extract zip
    _write_run_progress(progress_path, phase="extracting", detail="Extracting artifact...")
    try:
        with zipfile.ZipFile(zip_local, 'r') as zf:
            zf.extractall(new_project_location)
        os.remove(zip_local)
    except Exception as e:
        shutil.rmtree(new_project_location, ignore_errors=True)
        return jsonify({"error": f"Extraction failed: {e}"}), 500

    _write_run_progress(progress_path, phase="ready", detail="Ready to run experiment...")
    return jsonify({"new_project_uuid": new_uuid}), 200


@project_bp.route('/project/<projectUuid>/reproduce-run', methods=['POST'])
@cross_origin()
@require_auth
def reproduce_run(projectUuid):
    """
    Step 2 of reproduce-from-doi: load Docker image, provision data, run experiment.
    Long-running — frontend polls GET /project/<projectUuid>/run-progress in parallel.
    """
    project_location = os.path.join(cfg.PROJECTS_LOCATION, projectUuid)
    progress_path = os.path.join(project_location, "run_progress.json")
    messagesToUser = []

    # Read project_info to get original UUID (needed to find the .tar) and command
    proj_info_path = os.path.join(project_location, "project_info.json")
    if not os.path.isfile(proj_info_path):
        appendMessage(messagesToUser, "project_info.json not found in extracted artifact.",
                      stage="ReproFromDoi_Completed")
        return makeResponse(messagesToUser)

    with open(proj_info_path, "r", encoding="utf-8") as pf:
        proj_info = json.load(pf)

    original_uuid = proj_info.get("projectUuid", projectUuid)
    command_to_run = proj_info.get("command_to_run", "")
    if not command_to_run:
        appendMessage(messagesToUser,
                      "command_to_run not found in artifact. Cannot reproduce.",
                      stage="ReproFromDoi_Completed")
        return makeResponse(messagesToUser)

    # Read manifest
    manifest_path = os.path.join(project_location, "reproducibility_manifest.json")
    manifest = {}
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as mf:
            manifest = json.load(mf)

    # Load Docker image from tar
    tar_path = os.path.join(project_location, f"{original_uuid}.tar")
    if not os.path.isfile(tar_path):
        # Try any .tar file in the project folder
        tars = [f for f in os.listdir(project_location) if f.endswith(".tar")]
        if not tars:
            appendMessage(messagesToUser, "Docker image tar not found in artifact.",
                          stage="ReproFromDoi_Completed")
            return makeResponse(messagesToUser)
        tar_path = os.path.join(project_location, tars[0])

    _write_run_progress(progress_path, phase="loading", detail="Loading Docker image...")
    try:
        dockerClientResult = startDockerClient()
        dockerClient = dockerClientResult["dockerClient"]
        with open(tar_path, "rb") as tf:
            images = dockerClient.images.load(tf.read())
        loaded_image = images[0]
        image_tag = f"{projectUuid}:repro"
        loaded_image.tag(projectUuid, tag="repro")
    except Exception as e:
        appendMessage(messagesToUser, f"Failed to load Docker image: {e}",
                      stage="ReproFromDoi_Completed")
        return makeResponse(messagesToUser)

    # Provision data
    volumes = {cfg.HOST_VOLUME_PATH: {'bind': f"/projects/{projectUuid}/files", 'mode': 'rw'}}
    strategy = manifest.get("data_strategy", "no_data")
    if strategy not in ("no_data", "embed"):
        _write_run_progress(progress_path, phase="provisioning", detail="Provisioning data...")
        try:
            host_data_path = _provision_data(manifest, projectUuid, project_location, progress_path)
            if host_data_path:
                volumes[host_data_path] = {'bind': '/data', 'mode': 'ro'}
        except (RuntimeError, ValueError) as prov_err:
            _write_run_progress(progress_path, phase="error",
                                detail=f"Provisioning failed: {prov_err}")
            appendMessage(messagesToUser, f"Data provisioning failed: {prov_err}",
                          stage="ReproFromDoi_Completed")
            return makeResponse(messagesToUser)

    # For no_data/embed: data is baked into the Docker image at /files/data.
    # Prepend a symlink so experiments reading from /data work transparently.
    run_command = command_to_run
    if strategy in ("no_data", "embed"):
        run_command = f"ln -sfn /files/data /data 2>/dev/null; {command_to_run}"

    # Run experiment
    _write_run_progress(progress_path, phase="running", detail="Running experiment...")
    now = datetime.now()
    container_name = projectUuid + "_" + now.strftime("%Y%m%d%H%M%S")
    container = None
    try:
        container = dockerClient.containers.run(
            image=image_tag,
            name=container_name,
            volumes=volumes,
            detach=True,
            command="/bin/sh",
            tty=True
        )
        exec_result = container.exec_run(f'/bin/sh -c "{run_command}"')
        container_logs = "Command Output:" + exec_result.output.decode("utf-8", errors="replace") + "\n\n"
        container_logs += f"Exit Code: {exec_result.exit_code}\n"
        print(container_logs)

        # Extract output files
        extracted = []
        if exec_result.exit_code == 0:
            _write_run_progress(progress_path, phase="extracting", detail="Extracting output files...")
            spec_path = os.path.join(project_location, "output_spec.txt")
            if os.path.isfile(spec_path):
                with open(spec_path, "r", encoding="utf-8") as sf:
                    output_specs = [s.strip() for s in sf.read().split() if s.strip()]
                output_dir = os.path.join(project_location, "output")
                os.makedirs(output_dir, exist_ok=True)
                resolved_repro, missing_repro = _resolve_spec_paths(
                    container, "/files", output_specs
                )
                for cpath, display_name in resolved_repro:
                    try:
                        bits, _ = container.get_archive(cpath)
                        with tarfile.open(fileobj=io.BytesIO(b"".join(bits))) as tf:
                            dest = os.path.join(output_dir, os.path.dirname(display_name))
                            os.makedirs(dest, exist_ok=True)
                            tf.extractall(dest, filter="data")
                        extracted.append(display_name)
                    except Exception as ex:
                        print(f"  Could not extract {cpath}: {ex}")
                        missing_repro.append(display_name)

                if extracted:
                    container_logs += f"\nOutput files captured ({len(extracted)}):\n"
                    container_logs += "\n".join(f"  - {f}" for f in extracted) + "\n"
                else:
                    container_logs += "\nNo output files matched the specified pattern.\n"
                if missing_repro:
                    container_logs += (
                        f"\nNot produced ({len(missing_repro)}):\n"
                        + "\n".join(f"  - {f}" for f in missing_repro) + "\n"
                    )
    except Exception as e:
        container_logs = f"Container run failed: {e}"
        appendMessage(messagesToUser, container_logs, stage="ReproFromDoi_Completed")
        return makeResponse(messagesToUser)
    finally:
        if container:
            try:
                container.stop()
                container.remove()
            except Exception:
                pass

    appendMessage(messagesToUser,
                  content={
                      "new_project_uuid": projectUuid,
                      "original_uuid": original_uuid,
                      "command_to_run": command_to_run,
                      "data_strategy": strategy,
                      "logs": container_logs,
                      "output_files": extracted,
                  },
                  contentShort=container_logs,
                  stage="ReproFromDoi_Completed",
                  jsonObject=False)
    return makeResponse(messagesToUser)


@project_bp.route('/project/<projectUuid>/download-output/<path:filename>', methods=['GET'])
@cross_origin()
def download_output_file(projectUuid, filename):
    """Serve a single output file from projects/<uuid>/output/ for download."""
    output_dir = os.path.abspath(os.path.join(cfg.PROJECTS_LOCATION, projectUuid, "output"))
    file_path = os.path.abspath(os.path.join(output_dir, filename))

    # Prevent path traversal
    if not file_path.startswith(output_dir):
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(output_dir, filename, as_attachment=True)


@project_bp.route('/project/<projectUuid>/download-artifact', methods=['GET'])
@cross_origin()
def download_artifact(projectUuid):
    """Serve the packaged reproducibility artifact zip for direct download."""
    project_dir = os.path.abspath(os.path.join(cfg.PROJECTS_LOCATION, projectUuid))
    zip_name = f"{projectUuid}.zip"
    zip_path = os.path.abspath(os.path.join(project_dir, zip_name))

    if not zip_path.startswith(project_dir):
        return jsonify({"error": "Invalid path"}), 400

    if not os.path.isfile(zip_path):
        return jsonify({"error": "Artifact not found. Run the experiment first."}), 404

    return send_from_directory(project_dir, zip_name, as_attachment=True)