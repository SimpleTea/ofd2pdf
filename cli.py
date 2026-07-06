"""Child process entry point for OFD to PDF conversion."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path


def _result(
    *,
    success: bool,
    code: str,
    message: str,
    source_path: str,
    output_path: str | None = None,
    pages: int | None = None,
    elapsed: float | None = None,
    logs: list[str] | None = None,
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
        "logs": logs or [],
    }


def _emit(result: dict) -> int:
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0 if result.get("success") else 1


def convert_in_process(source_file_path: str) -> dict:
    source_path = str(source_file_path)
    path = Path(source_path)
    output_path = str(path.with_suffix(".pdf")) if source_path else None

    if not source_path.strip():
        return _result(
            success=False,
            code="INVALID_SOURCE",
            message="Source file path is required.",
            source_path=source_path,
            output_path=output_path,
        )

    if not path.exists():
        return _result(
            success=False,
            code="FILE_NOT_FOUND",
            message=f"Source file does not exist: {source_path}",
            source_path=source_path,
            output_path=output_path,
        )

    if not path.is_file() or path.suffix.lower() != ".ofd":
        return _result(
            success=False,
            code="INVALID_SOURCE",
            message=f"Source path is not a valid OFD file: {source_path}",
            source_path=source_path,
            output_path=output_path,
        )

    try:
        from .core.converter import OFDConverter
    except Exception as exc:
        return _result(
            success=False,
            code="PROCESS_FAILED",
            message=f"Could not import core converter: {exc}",
            source_path=source_path,
            output_path=output_path,
        )

    logs: list[str] = []
    captured_stdout = io.StringIO()

    try:
        with contextlib.redirect_stdout(captured_stdout):
            conversion = OFDConverter(scan_watermark=True).convert(
                source_path,
                output_path,
                log_func=logs.append,
            )
    except Exception as exc:
        return _result(
            success=False,
            code="UNEXPECTED_ERROR",
            message=f"Unexpected conversion error: {exc}",
            source_path=source_path,
            output_path=output_path,
            logs=logs,
        )

    extra_output = captured_stdout.getvalue().strip()
    if extra_output:
        logs.append(extra_output)

    success = bool(conversion.success)
    if success and (not os.path.exists(conversion.output_path) or os.path.getsize(conversion.output_path) <= 0):
        return _result(
            success=False,
            code="PROCESS_FAILED",
            message="Conversion reported success but output PDF was not created or is empty.",
            source_path=conversion.input_path,
            output_path=conversion.output_path,
            pages=conversion.pages,
            elapsed=conversion.elapsed,
            logs=logs,
        )

    return _result(
        success=success,
        code="OK" if success else "PROCESS_FAILED",
        message="Converted successfully." if success else (conversion.error or "Conversion failed."),
        source_path=conversion.input_path,
        output_path=conversion.output_path,
        pages=conversion.pages,
        elapsed=conversion.elapsed,
        logs=logs,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        return _emit(
            _result(
                success=False,
                code="INVALID_SOURCE",
                message="Usage: python -m ofd2pdf.cli <input.ofd>",
                source_path=args[0] if args else "",
            )
        )

    return _emit(convert_in_process(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())

