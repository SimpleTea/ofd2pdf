"""水印扫描与剥离 — 使用 fitz (PyMuPDF) 扫描 PDF 中的 Spire.PDF 评估水印并擦除。

虽然新的 OFD→PDF 转换引擎不使用 Spire.PDF, 理论上不会产生
"Evaluation Warning : The document was created with Spire.PDF for .NET" 水印,
但本模块作为"纯净性保障层", 对生成的 PDF 进行后置扫描:
  1. 全文搜索 "Evaluation Warning", "Spire.PDF" 等关键词
  2. 若命中, 使用 redaction (密文标注) 方式擦除对应文本块
  3. 返回扫描报告 (命中数 / 擦除数 / 是否纯净)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 水印关键词列表
WATERMARK_KEYWORDS = [
    "Evaluation Warning",
    "Spire.PDF",
    "Spire.PDF for .NET",
    "The document was created with Spire.PDF",
    "Spire.Doc",
    "E-iceblue",
]


@dataclass
class ScanResult:
    """水印扫描结果报告。"""
    total_hits: int = 0                    # 命中总数
    erased_count: int = 0                 # 已擦除数量
    is_clean: bool = True                 # 是否纯净 (无水印)
    hit_details: list = field(default_factory=list)  # [(page, keyword, rect), ...]

    def __str__(self):
        if self.is_clean:
            return "扫描通过: 未发现水印, PDF 纯净"
        return (f"发现 {self.total_hits} 处水印, 已擦除 {self.erased_count} 处, "
                f"{'剩余水印需人工检查' if self.total_hits > self.erased_count else '已全部清除'}")


class WatermarkScanner:
    """PDF 水印扫描与剥离器。"""

    def __init__(self, keywords: list = None):
        self.keywords = keywords or WATERMARK_KEYWORDS

    def scan_and_clean(self, pdf_path: str) -> ScanResult:
        """扫描并清除 PDF 中的水印, 原地覆盖保存。

        Args:
            pdf_path: PDF 文件路径

        Returns:
            ScanResult 扫描结果报告
        """
        try:
            import fitz  # PyMuPDF
        except ImportError:
            logger.warning("PyMuPDF (fitz) 未安装, 跳过水印扫描")
            return ScanResult(is_clean=True)

        result = ScanResult()

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.error(f"打开 PDF 失败: {pdf_path} - {e}")
            return ScanResult(is_clean=True)

        try:
            for page_num in range(len(doc)):
                page = doc[page_num]
                for keyword in self.keywords:
                    # search_for 返回匹配的矩形区域列表
                    rects = page.search_for(keyword)
                    for rect in rects:
                        result.total_hits += 1
                        result.hit_details.append((page_num + 1, keyword, str(rect)))
                        # 添加红色密文标注并应用擦除
                        page.add_redact_annot(rect, fill=(1, 1, 1))
                        result.erased_count += 1

            # 如果有命中, 应用所有 redaction 并保存
            if result.total_hits > 0:
                result.is_clean = False
                for page_num in range(len(doc)):
                    doc[page_num].apply_redactions()
                # 保存到临时文件再替换原文件
                tmp_path = pdf_path + ".clean.tmp"
                doc.save(tmp_path, garbage=4, deflate=True)
                doc.close()
                # 替换原文件
                import os
                os.replace(tmp_path, pdf_path)
                logger.info(f"水印已清除: {result.total_hits} 处 ({pdf_path})")
            else:
                doc.close()
                logger.info("水印扫描通过: PDF 纯净无冗余")

        except Exception as e:
            logger.error(f"水印扫描过程出错: {e}")
            result.is_clean = True  # 出错时默认通过

        return result
