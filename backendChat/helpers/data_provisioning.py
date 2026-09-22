"""
Step 5 — Data Provisioning Agent

Reads reproducibility_manifest.json and provisions data before container execution.
Critical security feature: halts execution if MD5 verification fails.

Strategies handled:
  no_data / embed          → no-op (data absent or already in Docker image)
  externalize_files        → rclone mount Zenodo record (zero-copy); falls back to download
  externalize              → download dataset.tar, verify MD5, extract to /data
  chunk_and_externalize    → download chunks, verify each, concatenate, verify full tar, extract
  external_doi             → download individual files from researcher-supplied DOI, verify MD5s

Usage as module (server-side):
    from helpers.data_provisioning import provision_data
    provision_data("/path/to/manifest.json", "/data", zenodo_token="...")

Usage as standalone script (packaged inside research artifact ZIP):
    python3 provision_data.py --manifest reproducibility_manifest.json --output ./data
    python3 provision_data.py --manifest reproducibility_manifest.json --output ./data --token <token>
"""

import os
import json
import hashlib
import shutil
import tarfile
import argparse
import subprocess
import tempfile

import requests


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n):
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB"


def _write_progress(progress_path, **kwargs):
    """Write progress JSON atomically. Never raises."""
    if not progress_path:
        return
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(kwargs, f)
    except Exception:
        pass


def _md5(file_path):
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_md5(file_path, expected_md5, label="file"):
    """
    Compute and compare MD5. Raises RuntimeError on mismatch.
    This is the critical security gate — callers must not launch the container
    if this raises.
    """
    print(f"  Verifying MD5 for {label} ...")
    actual = _md5(file_path)
    if actual != expected_md5:
        raise RuntimeError(
            f"MD5 mismatch for {label}:\n"
            f"  expected : {expected_md5}\n"
            f"  actual   : {actual}\n"
            "EXECUTION HALTED: data integrity check failed — "
            "the downloaded file does not match the manifest fingerprint."
        )
    print(f"  MD5 OK: {label}")


def _download(url, dest_path, token=None, label=None,
              progress_path=None, file_index=0, file_count=1):
    """Stream-download a file from a URL. Supports Zenodo Bearer token auth."""
    label = label or os.path.basename(dest_path)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    print(f"  Downloading {label} ...")
    _write_progress(progress_path,
                    phase="provisioning",
                    detail=f"Downloading {label}...",
                    file_index=file_index,
                    file_count=file_count,
                    bytes_done=0,
                    bytes_total=0)
    with requests.get(url, headers=headers, stream=True, timeout=None) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        last_reported = 0
        report_interval = 50 * 1024 * 1024  # every 50 MB
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress_path and done - last_reported >= report_interval:
                    last_reported = done
                    detail = (
                        f"Downloading {label} ({_fmt_bytes(done)} / {_fmt_bytes(total)})"
                        if total else
                        f"Downloading {label} ({_fmt_bytes(done)})"
                    )
                    _write_progress(progress_path,
                                    phase="provisioning",
                                    detail=detail,
                                    file_index=file_index,
                                    file_count=file_count,
                                    bytes_done=done,
                                    bytes_total=total)
    print(f"  Saved: {dest_path}")


def _zenodo_file_url(record_id, filename):
    """Construct the download URL for a file in a published Zenodo record."""
    return f"https://zenodo.org/api/records/{record_id}/files/{filename}/content"


def _extract_tar(tar_path, dest_dir):
    print(f"  Extracting {os.path.basename(tar_path)} → {dest_dir} ...")
    with tarfile.open(tar_path, "r") as tf:
        tf.extractall(dest_dir, filter="data")
    print("  Extraction complete.")


# ---------------------------------------------------------------------------
# rclone helpers (mount and copy)
# ---------------------------------------------------------------------------

def _rclone_doi_mount(doi, mount_dir):
    """
    Mount a Zenodo record using rclone's doi backend (type = doi).

    The doi backend resolves the DOI via doi.org, calls the Zenodo REST API
    to list files, and streams them over HTTPS — no WebDAV or S3 required.
    This is the same approach used by RenkuLab (SwissDataScienceCenter/rclone).

    Requires rclone >= v1.67 (doi backend was merged in rclone PR #8510).

    Returns True if mount was established, False otherwise.
    """
    if not shutil.which("rclone"):
        print("  rclone not found in PATH.")
        return False

    os.makedirs(mount_dir, exist_ok=True)

    # Write a temporary rclone config using the doi backend
    config_content = f"[dataset]\ntype = doi\ndoi = {doi}\n"
    config_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".conf", delete=False
        ) as cf:
            cf.write(config_content)
            config_file = cf.name

        print(f"  Mounting Zenodo record {doi} via rclone doi backend ...")
        result = subprocess.run(
            [
                "rclone", "mount",
                "dataset:", mount_dir,
                "--config", config_file,
                "--read-only",
                "--daemon",
                "--allow-non-empty",
                "--no-modtime",
            ],
            timeout=20,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            print(f"  rclone doi mount active at {mount_dir}")
            return True

        print(f"  rclone doi mount failed: {result.stderr.strip()}")
        return False

    except Exception as e:
        print(f"  rclone doi mount error: {e}")
        return False
    finally:
        if config_file and os.path.exists(config_file):
            os.unlink(config_file)


def _rclone_copy(doi, dest_dir):
    """
    Download all files from a Zenodo record using rclone copy.
    More robust than HTTP download: automatic retries, parallel transfers.
    Returns True if successful, False otherwise.
    """
    if not shutil.which("rclone"):
        print("  rclone not found in PATH.")
        return False

    config_content = f"[dataset]\ntype = doi\ndoi = {doi}\n"
    config_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as cf:
            cf.write(config_content)
            config_file = cf.name

        print(f"  Copying Zenodo record {doi} via rclone copy ...")
        result = subprocess.run(
            ["rclone", "copy", "dataset:", dest_dir,
             "--config", config_file, "--progress"],
            timeout=None,
        )
        if result.returncode == 0:
            print(f"  rclone copy complete → {dest_dir}")
            return True
        print(f"  rclone copy failed (exit {result.returncode})")
        return False
    except Exception as e:
        print(f"  rclone copy error: {e}")
        return False
    finally:
        if config_file and os.path.exists(config_file):
            os.unlink(config_file)


# ---------------------------------------------------------------------------
# Strategy handlers
# ---------------------------------------------------------------------------

def _provision_externalize(dataset, data_output_dir, token, progress_path=None):
    """
    externalize strategy: download single dataset.tar, verify MD5, extract.
    """
    depositions = dataset.get("depositions", [])
    if not depositions:
        raise RuntimeError("Manifest has no depositions for 'externalize' strategy.")

    dep = depositions[0]
    record_id = dep["record_id"]
    filename = dep["filename"]
    expected_md5 = dataset.get("tar_md5")

    tar_path = os.path.join(data_output_dir, "_dataset.tar")
    url = _zenodo_file_url(record_id, filename)

    _download(url, tar_path, token=token, label=filename,
              progress_path=progress_path, file_index=1, file_count=1)

    if expected_md5:
        _write_progress(progress_path, phase="provisioning", detail=f"Verifying {filename}...")
        _verify_md5(tar_path, expected_md5, label=filename)

    _write_progress(progress_path, phase="provisioning", detail="Extracting dataset...")
    _extract_tar(tar_path, data_output_dir)
    os.remove(tar_path)
    print(f"  Data provisioned at: {data_output_dir}")


def _provision_chunk_and_externalize(dataset, data_output_dir, token, progress_path=None):
    """
    chunk_and_externalize strategy:
      1. Download each chunk in chunk_id order
      2. Verify each chunk MD5
      3. Concatenate into dataset.tar
      4. Verify full tar MD5
      5. Extract
    """
    depositions = dataset.get("depositions", [])
    if not depositions:
        raise RuntimeError("Manifest has no depositions for 'chunk_and_externalize' strategy.")

    # Sort by chunk_id — order is critical for correct reassembly
    depositions = sorted(depositions, key=lambda d: d.get("chunk_id", 0))
    total_chunks = len(depositions)

    chunk_paths = []
    for dep in depositions:
        record_id = dep["record_id"]
        filename = dep["filename"]
        chunk_id = dep.get("chunk_id", len(chunk_paths))
        expected_chunk_md5 = dep.get("md5")

        chunk_path = os.path.join(data_output_dir, f"_chunk_{chunk_id:04d}")
        url = _zenodo_file_url(record_id, filename)
        _download(url, chunk_path, token=token,
                  label=f"chunk {chunk_id + 1}/{total_chunks} ({filename})",
                  progress_path=progress_path,
                  file_index=chunk_id + 1,
                  file_count=total_chunks)

        if expected_chunk_md5:
            _write_progress(progress_path, phase="provisioning",
                            detail=f"Verifying chunk {chunk_id + 1}/{total_chunks}...")
            _verify_md5(chunk_path, expected_chunk_md5, label=f"chunk {chunk_id}")

        chunk_paths.append(chunk_path)

    # Concatenate chunks into a single tar
    tar_path = os.path.join(data_output_dir, "_dataset.tar")
    _write_progress(progress_path, phase="provisioning",
                    detail=f"Assembling {total_chunks} chunks...")
    print(f"  Concatenating {total_chunks} chunks → _dataset.tar ...")
    with open(tar_path, "wb") as out:
        for cp in chunk_paths:
            with open(cp, "rb") as inp:
                shutil.copyfileobj(inp, out)
            os.remove(cp)

    # Verify reassembled tar before extraction
    expected_tar_md5 = dataset.get("tar_md5")
    if expected_tar_md5:
        _write_progress(progress_path, phase="provisioning",
                        detail="Verifying reassembled dataset...")
        _verify_md5(tar_path, expected_tar_md5, label="reassembled dataset.tar")

    _write_progress(progress_path, phase="provisioning", detail="Extracting dataset...")
    _extract_tar(tar_path, data_output_dir)
    os.remove(tar_path)
    print(f"  Data provisioned at: {data_output_dir}")


def _provision_externalize_files(dataset, data_output_dir, token, progress_path=None):
    """
    externalize_files strategy: data was uploaded as individual files (no tar).

    Attempts rclone doi mount first (zero-copy, zero local disk usage).
    Falls back to individual file download if rclone is unavailable or fails.
    """
    doi = dataset.get("doi", "")
    record_id = dataset.get("record_id", "")
    files = dataset.get("files", [])

    # --- Attempt rclone doi mount ---
    if doi and _rclone_doi_mount(doi, data_output_dir):
        return  # Mount succeeded — no download needed

    # --- Attempt rclone copy ---
    if doi and _rclone_copy(doi, data_output_dir):
        return  # Copy succeeded

    # --- Fallback: download individual files via Python requests ---
    print("  Falling back to individual file download ...")
    total_files = len(files)
    for idx, file_info in enumerate(files):
        filename = file_info.get("filename", "")
        expected_md5 = file_info.get("md5")
        download_url = (
            file_info.get("download_url")
            or _zenodo_file_url(record_id, filename)
        )
        dest_path = os.path.join(data_output_dir, filename)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        _download(download_url, dest_path, token=token, label=filename,
                  progress_path=progress_path,
                  file_index=idx + 1, file_count=total_files)

        if expected_md5:
            _write_progress(progress_path, phase="provisioning",
                            detail=f"Verifying {filename}...")
            _verify_md5(dest_path, expected_md5, label=filename)

    print(f"  Data provisioned at: {data_output_dir}")


def _provision_external_doi(dataset, data_output_dir, token, progress_path=None):
    """
    external_doi strategy (Mode C): researcher supplied a pre-existing Zenodo DOI.

    Two sub-paths based on doi_files_are_archives flag in manifest:
      - Archives (.zip/.tar): download + extract (cannot rclone mount an archive)
      - Individual files:     rclone mount → rclone copy → Python requests fallback
    """
    import zipfile as _zipfile

    doi = dataset.get("doi", "")
    files = dataset.get("files", [])
    record_id = dataset.get("record_id", "")
    doi_files_are_archives = dataset.get("doi_files_are_archives", False)

    if not files and not doi:
        raise RuntimeError("Manifest has no DOI or files listed for 'external_doi' strategy.")

    if doi_files_are_archives:
        # --- Archive path: download each file and extract ---
        print("  external_doi: files are archives — downloading and extracting ...")
        total_files = len(files)
        for idx, file_info in enumerate(files):
            filename = file_info.get("filename", "")
            expected_md5 = file_info.get("md5")
            download_url = (
                file_info.get("download_url")
                or _zenodo_file_url(record_id, filename)
            )
            dest_path = os.path.join(data_output_dir, filename)
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
            _download(download_url, dest_path, token=token, label=filename,
                      progress_path=progress_path,
                      file_index=idx + 1, file_count=total_files)

            if expected_md5:
                _write_progress(progress_path, phase="provisioning",
                                detail=f"Verifying {filename}...")
                _verify_md5(dest_path, expected_md5, label=filename)

            name_lower = filename.lower()
            if name_lower.endswith(".tar") or name_lower.endswith(".tar.gz") or name_lower.endswith(".tgz"):
                _write_progress(progress_path, phase="provisioning",
                                detail=f"Extracting {filename}...")
                _extract_tar(dest_path, data_output_dir)
                os.remove(dest_path)
            elif name_lower.endswith(".zip"):
                _write_progress(progress_path, phase="provisioning",
                                detail=f"Extracting {filename}...")
                print(f"  Extracting {filename} → {data_output_dir} ...")
                with _zipfile.ZipFile(dest_path, 'r') as zf:
                    zf.extractall(data_output_dir)
                os.remove(dest_path)

        print(f"  Data provisioned at: {data_output_dir}")
        return

    # --- Individual files path: rclone mount → rclone copy → Python requests ---
    if doi and _rclone_doi_mount(doi, data_output_dir):
        return  # Mount succeeded — no download needed

    if doi and _rclone_copy(doi, data_output_dir):
        return  # Copy succeeded

    # --- Fallback: Python requests ---
    if not files:
        raise RuntimeError("rclone failed and manifest has no file list to fall back to.")

    print("  Falling back to individual file download ...")
    total_files = len(files)
    for idx, file_info in enumerate(files):
        filename = file_info.get("filename", "")
        expected_md5 = file_info.get("md5")
        download_url = (
            file_info.get("download_url")
            or _zenodo_file_url(record_id, filename)
        )
        dest_path = os.path.join(data_output_dir, filename)
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        _download(download_url, dest_path, token=token, label=filename,
                  progress_path=progress_path,
                  file_index=idx + 1, file_count=total_files)

        if expected_md5:
            _write_progress(progress_path, phase="provisioning",
                            detail=f"Verifying {filename}...")
            _verify_md5(dest_path, expected_md5, label=filename)

    print(f"  Data provisioned at: {data_output_dir}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def provision_data(manifest_path, data_output_dir, zenodo_token=None, progress_path=None):
    """
    Read reproducibility_manifest.json and provision data to data_output_dir.

    Raises RuntimeError on MD5 mismatch — the caller MUST NOT launch the
    Docker container if this function raises. This is the integrity gate for
    Step 5 of the Data Extension Layer.

    Args:
        manifest_path:   Path to reproducibility_manifest.json
        data_output_dir: Directory where data will be provisioned
        zenodo_token:    Zenodo API token (required for private records only)
        progress_path:   Optional path to write run_progress.json during download
    """
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    strategy = manifest.get("data_strategy", "no_data")
    dataset = manifest.get("dataset") or {}

    print(f"[provision_data] strategy={strategy}")

    if strategy in ("no_data", "embed"):
        print(f"  Strategy '{strategy}' — no external data required.")
        return

    os.makedirs(data_output_dir, exist_ok=True)

    if strategy == "externalize":
        _provision_externalize(dataset, data_output_dir, zenodo_token,
                               progress_path=progress_path)
    elif strategy == "chunk_and_externalize":
        _provision_chunk_and_externalize(dataset, data_output_dir, zenodo_token,
                                         progress_path=progress_path)
    elif strategy == "externalize_files":
        _provision_externalize_files(dataset, data_output_dir, zenodo_token,
                                     progress_path=progress_path)
    elif strategy == "external_doi":
        _provision_external_doi(dataset, data_output_dir, zenodo_token,
                                progress_path=progress_path)
    else:
        raise ValueError(f"Unknown data strategy: {strategy}")

    print(f"[provision_data] Complete.")


# ---------------------------------------------------------------------------
# Standalone entry point (packaged inside research artifact)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SciConv Data Provisioning Agent (Step 5)"
    )
    parser.add_argument(
        "--manifest", required=True,
        help="Path to reproducibility_manifest.json"
    )
    parser.add_argument(
        "--output", required=True,
        help="Directory to provision data into (e.g. ./data)"
    )
    parser.add_argument(
        "--token", default=None,
        help="Zenodo API token (required for private records only)"
    )
    args = parser.parse_args()

    try:
        provision_data(args.manifest, args.output, zenodo_token=args.token)
    except (RuntimeError, ValueError) as e:
        print(f"\nERROR: {e}")
        print("Execution HALTED — container will not be launched with unverified data.")
        raise SystemExit(1)
