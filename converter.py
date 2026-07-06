"""Subprocess-based public API for OFD to PDF conversion."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 300


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _result(
    *,
    success: bool,
    code: str,
    message: str,
    source_path: str,
    output_path: str | None = None,
    pages: int | None = None,
    elapsed: float | None = None,
    returncode: int | None = None,
    stdout: str = "",
    stderr: str = "",
) -> dict:
    return {
        "success": success,
        "status": "success" if success else "error",
        "code": code,
        "message": message,
        "source_path": source_path,
        "output_path": output_path,
        "pages": pages,
        "elapsed": elapsed,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _invalid_child_output(
    source_path: str,
    output_path: str,
    completed: subprocess.CompletedProcess,
) -> dict:
    return _result(
        success=False,
        code="INVALID_CHILD_OUTPUT",
        message="Conversion process did not return valid JSON.",
        source_path=source_path,
        output_path=output_path,
        returncode=completed.returncode,
        stdout=_text(completed.stdout),
        stderr=_text(completed.stderr),
    )


def _normalize_child_result(
    child_result: dict,
    *,
    source_path: str,
    default_output_path: str,
    completed: subprocess.CompletedProcess,
) -> dict:
    output_path = child_result.get("output_path") or default_output_path
    success = bool(child_result.get("success"))

    if success and (not os.path.exists(output_path) or os.path.getsize(output_path) <= 0):
        return _result(
            success=False,
            code="PROCESS_FAILED",
            message="Conversion reported success but output PDF was not created or is empty.",
            source_path=child_result.get("source_path") or source_path,
            output_path=output_path,
            pages=child_result.get("pages"),
            elapsed=child_result.get("elapsed"),
            returncode=completed.returncode,
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
        )

    return _result(
        success=success,
        code=child_result.get("code") or ("OK" if success else "PROCESS_FAILED"),
        message=child_result.get("message") or ("Converted successfully." if success else "Conversion failed."),
        source_path=child_result.get("source_path") or source_path,
        output_path=output_path,
        pages=child_result.get("pages"),
        elapsed=child_result.get("elapsed"),
        returncode=completed.returncode,
        stdout=_text(completed.stdout),
        stderr=_text(completed.stderr),
    )


def convert_ofd_to_pdf(source_file_path: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Convert one OFD file to PDF through an isolated Python subprocess.

    Args:
        source_file_path: Source OFD file path.
        timeout: Child process timeout in seconds.

    Returns:
        A unified status dictionary containing conversion status and details.
    """
    source_path = _text(source_file_path)

    if not source_path.strip():
        return _result(
            success=False,
            code="INVALID_SOURCE",
            message="Source file path is required.",
            source_path=source_path,
        )

    path = Path(source_path)
    if not path.exists():
        return _result(
            success=False,
            code="FILE_NOT_FOUND",
            message=f"Source file does not exist: {source_path}",
            source_path=source_path,
        )

    if not path.is_file():
        return _result(
            success=False,
            code="INVALID_SOURCE",
            message=f"Source path is not a file: {source_path}",
            source_path=source_path,
        )

    if path.suffix.lower() != ".ofd":
        return _result(
            success=False,
            code="INVALID_SOURCE",
            message=f"Source file must use the .ofd extension: {source_path}",
            source_path=source_path,
        )

    output_path = str(path.with_suffix(".pdf"))
    command = [sys.executable, "-m", "ofd2pdf.cli", source_path]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        return _result(
            success=False,
            code="PROCESS_FAILED",
            message="Child conversion process failed.",
            source_path=source_path,
            output_path=output_path,
            returncode=exc.returncode,
            stdout=_text(exc.output),
            stderr=_text(exc.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        return _result(
            success=False,
            code="TIMEOUT",
            message=f"Conversion timed out after {exc.timeout} seconds.",
            source_path=source_path,
            output_path=output_path,
            stdout=_text(exc.output),
            stderr=_text(exc.stderr),
        )
    except OSError as exc:
        return _result(
            success=False,
            code="PROCESS_FAILED",
            message=f"Could not start conversion process: {exc}",
            source_path=source_path,
            output_path=output_path,
        )
    except Exception as exc:
        return _result(
            success=False,
            code="UNEXPECTED_ERROR",
            message=f"Unexpected conversion error: {exc}",
            source_path=source_path,
            output_path=output_path,
        )

    try:
        child_result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _invalid_child_output(source_path, output_path, completed)

    if not isinstance(child_result, dict):
        return _invalid_child_output(source_path, output_path, completed)

    return _normalize_child_result(
        child_result,
        source_path=source_path,
        default_output_path=output_path,
        completed=completed,
    )
