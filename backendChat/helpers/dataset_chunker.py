# helpers/dataset_chunker.py
"""
Data Extension Layer — Step 3 utilities.

Provides deterministic tarring and chunking of dataset directories
so they can be uploaded to Zenodo within its per-record limits
(50 GB total, 100 files max).

Workflow:
  1. tar_directory()   — tar the entire data/ folder into a single archive
  2. split_file()      — split the tar into fixed-size chunks (if > 50 GB)
  3. md5_file()        — compute MD5 checksum of a file (for manifest)
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from typing import List, Dict, Any

# Default chunk size: 5 GB (test — production: 40 GB)
DEFAULT_CHUNK_SIZE_BYTES = int(1 * 1024 * 1024)  # 1 MB (temp test)

# Read buffer for hashing and splitting (8 MB)
_BUF_SIZE = 8 * 1024 * 1024


def md5_file(path: str) -> str:
    """Compute the MD5 hex digest of a file (streaming, constant memory)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            buf = f.read(_BUF_SIZE)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def tar_directory(data_dir: str, output_path: str) -> str:
    """
    Create a deterministic tar archive of *data_dir*.

    The archive is NOT compressed (plain .tar) because:
      - Compression adds non-determinism across platforms/versions
      - Large scientific data (images, HDF5, NetCDF) barely compresses
      - Chunking a compressed archive breaks streaming decompression

    Returns the absolute path to the created tar file.
    """
    data_dir = os.path.abspath(data_dir)
    output_path = os.path.abspath(output_path)

    with tarfile.open(output_path, "w") as tar:
        # Walk in sorted order for determinism
        for root, dirs, files in os.walk(data_dir):
            dirs.sort()
            for fname in sorted(files):
                full_path = os.path.join(root, fname)
                arcname = os.path.relpath(full_path, data_dir)
                tar.add(full_path, arcname=arcname)

    return output_path


def split_file(file_path: str, output_dir: str,
               chunk_size: int = DEFAULT_CHUNK_SIZE_BYTES,
               prefix: str = "dataset_chunk_") -> List[Dict[str, Any]]:
    """
    Split *file_path* into fixed-size chunks written to *output_dir*.

    Returns a list of chunk metadata dicts:
      [
        {"chunk_id": 1, "filename": "dataset_chunk_001.bin",
         "path": "/abs/path", "size": ..., "md5": "..."},
        ...
      ]
    """
    os.makedirs(output_dir, exist_ok=True)
    chunks: List[Dict[str, Any]] = []
    chunk_id = 0

    with open(file_path, "rb") as src:
        while True:
            chunk_id += 1
            chunk_name = f"{prefix}{chunk_id:03d}.bin"
            chunk_path = os.path.join(output_dir, chunk_name)

            hasher = hashlib.md5()
            bytes_written = 0

            with open(chunk_path, "wb") as dst:
                while bytes_written < chunk_size:
                    to_read = min(_BUF_SIZE, chunk_size - bytes_written)
                    buf = src.read(to_read)
                    if not buf:
                        break
                    dst.write(buf)
                    hasher.update(buf)
                    bytes_written += len(buf)

            if bytes_written == 0:
                # Source was exactly a multiple of chunk_size; remove empty file
                os.remove(chunk_path)
                break

            chunks.append({
                "chunk_id": chunk_id,
                "filename": chunk_name,
                "path": chunk_path,
                "size": bytes_written,
                "md5": hasher.hexdigest(),
            })

            if bytes_written < chunk_size:
                # Last chunk (smaller than chunk_size) — we're done
                break

    return chunks


def prepare_dataset_for_upload(data_dir: str, work_dir: str,
                               zenodo_limit: int = 50 * 1024 * 1024 * 1024  # 50 GB
                               ) -> Dict[str, Any]:
    """
    High-level helper: tar the data directory and, if the tar exceeds
    *zenodo_limit*, split it into chunks.

    *work_dir* is a scratch directory where the tar and chunks are written.

    Returns:
      {
        "tar_path":   "/abs/path/dataset.tar",
        "tar_size":   <bytes>,
        "tar_md5":    "abc123...",
        "chunked":    True | False,
        "chunks":     [ {chunk_id, filename, path, size, md5}, ... ],
        "files_to_upload": [ {"path": ..., "filename": ...}, ... ]
      }

    files_to_upload contains either:
      - The single tar (if not chunked)
      - The list of chunk files (if chunked)
    """
    os.makedirs(work_dir, exist_ok=True)

    # 1) Tar
    tar_path = os.path.join(work_dir, "dataset.tar")
    tar_directory(data_dir, tar_path)
    tar_size = os.path.getsize(tar_path)
    tar_md5 = md5_file(tar_path)

    result: Dict[str, Any] = {
        "tar_path": tar_path,
        "tar_size": tar_size,
        "tar_md5": tar_md5,
        "chunked": False,
        "chunks": [],
        "files_to_upload": [],
    }

    # 2) Chunk if needed
    if tar_size > zenodo_limit:
        chunks_dir = os.path.join(work_dir, "chunks")
        chunks = split_file(tar_path, chunks_dir)
        result["chunked"] = True
        result["chunks"] = chunks
        # Each chunk goes to a separate Zenodo deposition
        result["files_to_upload"] = [
            {"path": c["path"], "filename": c["filename"]}
            for c in chunks
        ]
    else:
        result["files_to_upload"] = [
            {"path": tar_path, "filename": "dataset.tar"}
        ]

    return result
