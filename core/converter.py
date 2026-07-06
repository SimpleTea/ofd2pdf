"""OFD 转 PDF 转换器 — 编排解析、渲染、水印扫描三阶段。

使用方式:
  converter = OFDConverter()
  result = converter.convert("input.ofd", "output.pdf")
  print(result)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .ofd_parser import OFDParser, OFDDocument
from .pdf_renderer import PDFRenderer
from .watermark_scanner import WatermarkScanner, ScanResult

logger = logging.getLogger(__name__)


def _try_remove_file(path: str) -> bool:
    """尝试删除已存在的文件, 处理文件被占用的情况。

    返回 True 表示成功删除或文件不存在, False 表示文件被占用无法删除。
    """
    if not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except PermissionError:
        return False


@dataclass
class ConversionResult:
    """单个文件转换结果。"""
    input_path: str
    output_path: str
    success: bool
    elapsed: float = 0.0
    pages: int = 0
    watermark: Optional[ScanResult] = None
    error: str = ""

    def __str__(self):
        if self.success:
            wm = str(self.watermark) if self.watermark else "未扫描"
            return (f"✓ {os.path.basename(self.input_path)} → {os.path.basename(self.output_path)} "
                    f"({self.pages} 页, {self.elapsed:.1f}s, {wm})")
        return f"✗ {os.path.basename(self.input_path)} 失败: {self.error}"


class OFDConverter:
    """OFD 转 PDF 转换器。"""

    def __init__(self, scan_watermark: bool = True):
        self.parser = OFDParser.__new__(OFDParser)  # 每次转换创建新实例
        self.scanner = WatermarkScanner() if scan_watermark else None
        self.scan_watermark = scan_watermark

    def convert(self, ofd_path: str, output_path: str,
                log_func: Optional[Callable[[str], None]] = None) -> ConversionResult:
        """转换单个 OFD 文件为 PDF。

        Args:
            ofd_path: 输入 OFD 文件路径
            output_path: 输出 PDF 文件路径
            log_func: 日志回调函数

        Returns:
            ConversionResult 转换结果
        """
        def log(msg):
            logger.info(msg)
            if log_func:
                log_func(msg)

        start = time.time()
        result = ConversionResult(input_path=ofd_path, output_path=output_path, success=False)

        try:
            # 阶段 1: 解析 OFD
            log(f"[1/3] 解析 OFD: {ofd_path}")
            parser = OFDParser(ofd_path)
            doc = parser.parse()
            result.pages = len(doc.pages)
            log(f"  解析完成: {result.pages} 页, {len(doc.template_pages)} 模板, "
                f"{len(doc.resources.fonts) if doc.resources else 0} 字体")

            # 预处理: 如果输出文件已存在且被占用, 尝试删除或使用临时文件名
            if os.path.exists(output_path):
                if not _try_remove_file(output_path):
                    # 文件被占用, 使用临时文件名
                    base, ext = os.path.splitext(output_path)
                    temp_path = f"{base}_tmp{ext}"
                    log(f"  ⚠ 输出文件被占用, 使用临时文件: {os.path.basename(temp_path)}")
                    output_path = temp_path
                    result.output_path = output_path

            # 阶段 2: 渲染 PDF
            log(f"[2/3] 渲染 PDF: {output_path}")
            renderer = PDFRenderer(doc, output_path)
            renderer.render()
            doc.close()
            log(f"  渲染完成")

            # 阶段 3: 水印扫描
            if self.scan_watermark:
                log(f"[3/3] 扫描水印...")
                wm_result = self.scanner.scan_and_clean(output_path)
                result.watermark = wm_result
                log(f"  {wm_result}")
            else:
                log(f"[3/3] 跳过水印扫描")

            result.success = True
            result.elapsed = time.time() - start

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            logger.error(f"转换失败: {ofd_path}\n{err}")
            result.error = str(e)
            result.elapsed = time.time() - start
            log(f"  ✗ 转换失败: {e}")

        return result

    def convert_batch(self, ofd_files: list, output_dir: str,
                      log_func: Optional[Callable[[str], None]] = None) -> list:
        """批量转换多个 OFD 文件。

        Args:
            ofd_files: OFD 文件路径列表
            output_dir: 输出目录
            log_func: 日志回调函数

        Returns:
            list[ConversionResult] 每个文件的转换结果
        """
        results = []
        total = len(ofd_files)

        for i, ofd_path in enumerate(ofd_files):
            if log_func:
                log_func(f"\n{'='*50}")
                log_func(f"转换 [{i+1}/{total}]: {os.path.basename(ofd_path)}")
                log_func(f"{'='*50}")

            # 生成输出文件名
            base_name = os.path.splitext(os.path.basename(ofd_path))[0]
            output_path = os.path.join(output_dir, f"{base_name}.pdf")

            result = self.convert(ofd_path, output_path, log_func)
            results.append(result)

        return results
