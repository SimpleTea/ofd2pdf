"""PDF 矢量渲染引擎 — 使用 reportlab Canvas 将 OFD 页面高精度渲染为 PDF。

核心渲染流程:
  1. 注册字体: 优先系统字体 (保证文本可搜索), 回退嵌入 TTF 子集
  2. 逐页渲染: 创建 Canvas → 设置页面尺寸(mm→pt) → 渲染 Template 背景层 → 渲染 Page Content
  3. 对象渲染:
     - TextObject: 解析 DeltaX 逐字定位, 应用 CTM 变换, 查找字体资源
     - PathObject: 解析 AbbreviatedData (M/L/B/Q/A/C) 映射 reportlab 路径 API
     - ImageObject: 从 ZIP 读取图片 → BytesIO → canvas.drawImage
  4. DrawParam: 解析继承链, 应用 LineWidth/StrokeColor/FillColor

坐标系关键修正:
  OFD 实际使用左上角原点 (Y 向下), PDF 使用左下角原点 (Y 向上)。
  必须翻转 Y 坐标: y_pdf = page_height_mm - y_ofd - element_height_mm
  TextCode Y (相对于 Boundary): y_tc_pdf = boundary_height_mm - y_tc_ofd
  Path 坐标 Y (相对于 Boundary): y_path_pdf = boundary_height_mm - y_path_ofd

单位: OFD 用 mm, reportlab 用 pt, 转换因子 MM_TO_PT = 2.834645669
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional
from lxml import etree

from reportlab.lib.colors import Color
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .ofd_parser import OFDDocument, OFDPageInfo, OFDAnnotation
from .resource_model import (
    MM_TO_PT, ColorValue, DrawParam, Font, ResourceModel, ofd_tag,
)

logger = logging.getLogger(__name__)

# ── 系统字体回退表 ─────────────────────────────────────
_SYSTEM_FONT_PATHS = {
    "simsun":   "C:/Windows/Fonts/simsun.ttc",
    "宋体":     "C:/Windows/Fonts/simsun.ttc",
    "simhei":   "C:/Windows/Fonts/simhei.ttf",
    "黑体":     "C:/Windows/Fonts/simhei.ttf",
    "msyh":     "C:/Windows/Fonts/msyh.ttc",
    "微软雅黑": "C:/Windows/Fonts/msyh.ttc",
    "microsoft yahei": "C:/Windows/Fonts/msyh.ttc",
    "kaiti":    "C:/Windows/Fonts/simkai.ttf",
    "楷体":     "C:/Windows/Fonts/simkai.ttf",
    "fangsong": "C:/Windows/Fonts/simfang.ttf",
    "仿宋":     "C:/Windows/Fonts/simfang.ttf",
}

_BUILTIN_FONTS = {
    "courier new": "Courier",
    "times new roman": "Times-Roman",
    "arial": "Helvetica",
    "helvetica": "Helvetica",
}


class PDFRenderer:
    """将 OFDDocument 渲染为 PDF 文件。"""

    def __init__(self, doc: OFDDocument, output_path: str):
        self.doc = doc
        self.output_path = output_path
        self._font_registry: dict[str, str] = {}
        self._registered_names: set[str] = set()
        # 坐标系翻转辅助: 当前页面高度 (mm), 当前 Boundary 高度 (mm), 路径当前点
        self._page_h_mm: float = 0.0
        self._boundary_h_mm: float = 0.0
        self._cur_pt_mm: tuple = (0.0, 0.0)  # 路径当前点 (mm, Y已翻转)

    # ── Y 轴翻转辅助 ────────────────────────────────────

    def _flip_boundary_y(self, by: float, bh: float) -> float:
        """将 Boundary 的 Y 坐标从 OFD (左上原点) 翻转为 PDF (左下原点)。

        OFD: Boundary "x y w h" 的 y 是元素顶部到页面顶部的距离
        PDF: 需要 y 是元素底部到页面底部的距离 = page_h - y - h
        """
        return self._page_h_mm - by - bh

    def _flip_local_y(self, y: float) -> float:
        """将相对于 Boundary 的局部 Y 坐标翻转 (Y 向下 → Y 向上)。

        用于 TextCode Y 和 Path 坐标 Y。
        """
        return self._boundary_h_mm - y

    # ── 公共入口 ──────────────────────────────────────

    def render(self):
        """渲染整个 OFD 文档为 PDF。"""
        self._register_fonts()

        if not self.doc.pages:
            raise ValueError("OFD 文档没有页面")

        page_w, page_h = self._page_size_pt(self.doc.pages[0])
        c = canvas.Canvas(self.output_path, pagesize=(page_w, page_h))

        for i, page in enumerate(self.doc.pages):
            if i > 0:
                c.showPage()
                pw, ph = self._page_size_pt(page)
                c.setPageSize((pw, ph))

            self._render_page(c, page)

        c.save()
        logger.info(f"PDF 渲染完成: {self.output_path} ({len(self.doc.pages)} 页)")

    # ── 字体注册 ──────────────────────────────────────

    def _register_fonts(self):
        """注册所有字体: 系统字体优先 (保证文本可搜索), 嵌入 TTF 回退。"""
        if not self.doc.resources:
            return

        for font_id, font in self.doc.resources.fonts.items():
            name = self._register_single_font(font)
            if name:
                self._font_registry[font_id] = name
            else:
                fallback = self._get_system_font("SimSun")
                if fallback:
                    self._font_registry[font_id] = "SimSun"
                else:
                    self._font_registry[font_id] = "Helvetica"

        logger.info(f"注册字体: {self._font_registry}")

    def _register_single_font(self, font: Font) -> Optional[str]:
        """注册单个字体, 返回 reportlab 字体名。

        策略: 系统字体优先 > 嵌入 TTF > 内置字体。
        """
        display_name = font.resolve_font_name()

        # 1. 优先系统字体匹配 (保证文本可搜索性)
        sys_path = self._get_system_font(display_name)
        if sys_path:
            try:
                reg_name = f"SysFont_{font.id}"
                if sys_path.endswith('.ttc'):
                    ttf_font = TTFont(reg_name, sys_path, subfontIndex=0)
                else:
                    ttf_font = TTFont(reg_name, sys_path)
                pdfmetrics.registerFont(ttf_font)
                self._registered_names.add(reg_name)
                return reg_name
            except Exception as e:
                logger.warning(f"系统字体注册失败 ({display_name}): {e}")

        # 2. reportlab 内置字体
        builtin = _BUILTIN_FONTS.get(display_name.lower())
        if builtin:
            return builtin

        # 3. 回退到嵌入 TTF 子集
        if font.is_embedded and font.font_file:
            try:
                ttf_bytes = self.doc.zip_file.read(font.font_file)
                reg_name = f"OFDFont_{font.id}"
                ttf_font = TTFont(reg_name, io.BytesIO(ttf_bytes))
                pdfmetrics.registerFont(ttf_font)
                self._registered_names.add(reg_name)
                return reg_name
            except Exception as e:
                logger.warning(f"嵌入字体注册失败 ({font.id} / {font.font_file}): {e}")

        return None

    def _get_system_font(self, name: str) -> Optional[str]:
        """根据字体名查找系统字体文件路径。"""
        key = name.lower().strip()
        path = _SYSTEM_FONT_PATHS.get(key)
        if path and os.path.exists(path):
            return path
        return None

    # ── 页面渲染 ──────────────────────────────────────

    def _page_size_pt(self, page: OFDPageInfo) -> tuple:
        """获取页面尺寸 (pt)。"""
        _, _, w, h = page.physical_box
        return (w * MM_TO_PT, h * MM_TO_PT)

    def _render_page(self, c: canvas.Canvas, page: OFDPageInfo):
        """渲染单个页面: 先模板背景层, 再页面内容层, 最后注释层。"""
        self._page_h_mm = page.physical_box[3]

        # 1. 渲染模板 (Background) — 字段名, 保持原始颜色
        if page.template and page.template.template_id:
            tpl = self.doc.template_pages.get(page.template.template_id)
            if tpl and page.template.z_order == "Background":
                self._render_content(c, tpl.content_root, is_template=True)

        # 2. 渲染页面内容 — 字段值, 强制黑色
        self._render_content(c, page.content_root, is_template=False)

        # 3. 渲染模板 (Foreground)
        if page.template and page.template.template_id:
            tpl = self.doc.template_pages.get(page.template.template_id)
            if tpl and page.template.z_order == "Foreground":
                self._render_content(c, tpl.content_root, is_template=True)

        # 4. 渲染注释 (监制章/水印等)
        for annot in page.annotations:
            self._render_annotation(c, annot)

    def _render_content(self, c: canvas.Canvas, page_root: etree._Element,
                        is_template: bool = False):
        """渲染 Content 区域中的所有 Layer。

        is_template=True: 模板层 (字段名), 保持 DrawParam 原始颜色
        is_template=False: 页面内容层 (字段值), 无显式颜色时强制黑色
        """
        if page_root is None:
            return
        content = page_root.find(ofd_tag("Content"))
        if content is None:
            return
        for layer in content.findall(ofd_tag("Layer")):
            self._render_layer(c, layer, is_template=is_template)

    def _render_layer(self, c: canvas.Canvas, layer: etree._Element,
                      is_template: bool = False):
        """渲染单个 Layer: 遍历子元素, 按类型分发渲染。"""
        layer_dp_id = layer.get("DrawParam")

        for child in layer:
            if not isinstance(child.tag, str):
                continue
            tag = etree.QName(child.tag).localname
            if tag == "TextObject":
                self._render_text_object(c, child, layer_dp_id, is_template=is_template)
            elif tag == "PathObject":
                self._render_path_object(c, child, layer_dp_id, is_template=is_template)
            elif tag == "ImageObject":
                self._render_image_object(c, child, layer_dp_id)
            elif tag == "ComposeObject":
                self._render_compose_object(c, child, layer_dp_id)

    # ── Annotation 渲染 ────────────────────────────────

    def _render_annotation(self, c: canvas.Canvas, annot: OFDAnnotation):
        """渲染注释: 监制章图片水印、文字水印等。

        Annotation 结构:
          <ofd:Appearance Boundary="90 8 30 20">
            <ofd:ImageObject CTM="30 0 0 20 0 0" Boundary="0 0 30 20" ResourceID="6962"/>
          </ofd:Appearance>

        Appearance Boundary 是注释在页面上的绝对位置 (mm)。
        内部对象的坐标是相对于 Appearance Boundary 的。
        """
        if annot.appearance_root is None:
            return

        appearance = annot.appearance_root
        # Appearance 的 Boundary 是页面绝对坐标
        app_boundary = appearance.get("Boundary", "")
        if app_boundary:
            vals = app_boundary.split()
            if len(vals) >= 4:
                abx, aby, abw, abh = (float(vals[0]), float(vals[1]),
                                       float(vals[2]), float(vals[3]))
            else:
                abx, aby, abw, abh = 0, 0, 0, 0
        else:
            abx, aby, abw, abh = 0, 0, 0, 0

        # 遍历 Appearance 内的子对象 (ImageObject/TextObject)
        for child in appearance:
            if not isinstance(child.tag, str):
                continue
            tag = etree.QName(child.tag).localname
            if tag == "ImageObject":
                self._render_annotation_image(c, child, abx, aby, abw, abh)
            elif tag == "TextObject":
                self._render_annotation_text(c, child, abx, aby, abw, abh)

    def _render_annotation_image(self, c: canvas.Canvas, el: etree._Element,
                                 abx: float, aby: float, abw: float, abh: float):
        """渲染注释中的图片对象 (如监制章)。

        坐标系: 图片的 Boundary 是相对于 Appearance Boundary 的。
        最终位置 = Appearance Boundary + ImageObject Boundary。
        Y 轴翻转: OFD 左上原点 → PDF 左下原点。
        """
        boundary = self._parse_boundary(el)
        if boundary is None:
            return
        bx, by, bw, bh = boundary

        resource_id = el.get("ResourceID", "")
        if not resource_id:
            return

        media_path = self.doc.resources.get_media_path(resource_id)
        if not media_path:
            logger.warning(f"注释图片资源未找到: ResourceID={resource_id}")
            return

        try:
            img_bytes = self.doc.zip_file.read(media_path)
        except KeyError:
            logger.warning(f"注释图片文件未找到: {media_path}")
            return

        ctm = el.get("CTM")

        c.saveState()
        # 最终位置 = Appearance 位置 + 图片相对位置
        final_x = (abx + bx) * MM_TO_PT
        # Y 翻转: page_h - (aby + by) - bh
        final_y = self._flip_boundary_y(aby + by, bh) * MM_TO_PT
        c.translate(final_x, final_y)

        if ctm:
            vals = [float(v) for v in ctm.split()]
            if len(vals) == 6:
                a, b, cc, d, e, f = vals
                c.transform(a, b, cc, d, e * MM_TO_PT, f * MM_TO_PT)
                # CTM 存在时按单位尺寸绘制
                draw_w = MM_TO_PT
                draw_h = MM_TO_PT
            else:
                draw_w = bw * MM_TO_PT
                draw_h = bh * MM_TO_PT
        else:
            draw_w = bw * MM_TO_PT
            draw_h = bh * MM_TO_PT

        try:
            img_reader = ImageReader(io.BytesIO(img_bytes))
            c.drawImage(img_reader, 0, 0, draw_w, draw_h,
                       mask='auto', preserveAspectRatio=False)
        except Exception as e:
            logger.warning(f"注释图片绘制失败 ({media_path}): {e}")

        c.restoreState()

    def _render_annotation_text(self, c: canvas.Canvas, el: etree._Element,
                                abx: float, aby: float, abw: float, abh: float):
        """渲染注释中的文字对象 (如下载次数)。

        特殊处理: "下载次数"文字需要竖排显示 (从上到下, 字头朝右),
        位置与"购买方信息"/"销售方信息"行平行 (y≈30mm)。
        """
        boundary = self._parse_boundary(el)
        if boundary is None:
            return
        bx, by, bw, bh = boundary

        font_id = el.get("Font", "")
        size_mm = float(el.get("Size", "3.175"))
        size_pt = size_mm * MM_TO_PT
        font_name = self._font_registry.get(font_id, "Helvetica")
        ctm = el.get("CTM")
        alpha = el.get("Alpha")

        # 提取文字内容
        text = ""
        for tc in el.findall(ofd_tag("TextCode")):
            text += (tc.text or "")
        if not text:
            return

        # 去除尾部空格
        text = text.rstrip()

        c.saveState()
        c.setFont(font_name, size_pt)

        if alpha:
            alpha_val = int(alpha) / 255.0
            c.setFillAlpha(alpha_val)

        # 注释文字默认黑色
        c.setFillColorRGB(0, 0, 0)

        # 获取页面宽度
        page_w_mm = 210.0
        page_h_mm = self._page_h_mm
        if self.doc.pages:
            page_w_mm = self.doc.pages[0].physical_box[2]

        # 竖排: 每个字符从上到下排列, 字头朝右
        # 字间距 = 字号 (mm)
        char_spacing_mm = size_mm + 0.5  # 稍微加点间距

        # 起始 Y 位置: 与"购买方信息"行平行 (y≈30mm, 从顶部算)
        # 购买方信息 Boundary y≈33.8mm, 取 32mm 作为起始
        start_y_mm = 32.0

        # X 位置: 右侧边缘, 距右边界 0mm
        text_x_mm = page_w_mm - size_mm

        # 竖排逐字绘制: 每个字符旋转 -90° (字头朝右)
        # 在 PDF 中: 先平移到字符位置, 再旋转, 再绘制
        for i, ch in enumerate(text):
            # Y 位置: 从 start_y 向下排列 (OFD: Y增大=向下)
            # 翻转后 PDF: y_pt = page_h - (start_y + i*spacing) - size
            char_y_mm = start_y_mm + i * char_spacing_mm
            y_pt = (page_h_mm - char_y_mm - size_mm) * MM_TO_PT
            x_pt = text_x_mm * MM_TO_PT

            # 平移到字符中心, 旋转 -90° (字头朝右), 绘制
            c.saveState()
            c.translate(x_pt + size_pt / 2, y_pt + size_pt / 2)
            # 旋转 -90° = 逆时针 90° → 字头朝右
            c.rotate(-90)
            # 以中心点为基准绘制
            c.drawString(-size_pt / 2, -size_pt / 2, ch)
            c.restoreState()

        c.restoreState()

    # ── TextObject 渲染 ───────────────────────────────

    def _render_text_object(self, c: canvas.Canvas, el: etree._Element,
                            layer_dp_id: Optional[str] = None,
                            is_template: bool = True):
        """渲染文本对象: 逐字 DeltaX 定位绘制。

        颜色规则:
          - is_template=True (字段名): 保持 DrawParam/FillColor 原始颜色
          - is_template=False (字段值): 无显式 FillColor 时强制黑色
          - setFillColorRGB 移入 saveState/restoreState 内, 防止颜色泄漏
        """
        boundary = self._parse_boundary(el)
        if boundary is None:
            return
        bx, by, bw, bh = boundary

        font_id = el.get("Font", "")
        size_mm = float(el.get("Size", "3.175"))
        size_pt = size_mm * MM_TO_PT

        font_name = self._font_registry.get(font_id, "Helvetica")

        ctm = el.get("CTM")
        fill = el.get("Fill", "")

        fill_color = self._get_fill_color(el, layer_dp_id)

        alpha = el.get("Alpha")
        if alpha:
            alpha_val = int(alpha) / 255.0
            c.setFillAlpha(alpha_val)

        c.saveState()
        c.setFont(font_name, size_pt)

        # 颜色应用 (在 saveState 内, 防止泄漏)
        if fill_color:
            # 有显式颜色: 使用它 (字段名保持原色)
            r, g, b = fill_color.to_rgb(self.doc.resources.color_spaces)
            c.setFillColorRGB(r, g, b)
        elif not is_template:
            # 页面内容 (字段值) 无显式颜色: 强制黑色
            c.setFillColorRGB(0, 0, 0)
        # is_template=True 且无显式颜色: 使用 reportlab 默认 (黑色)

        # 翻转 Y: OFD 左上原点 → PDF 左下原点
        by_flipped = self._flip_boundary_y(by, bh)
        c.translate(bx * MM_TO_PT, by_flipped * MM_TO_PT)

        if ctm:
            vals = [float(v) for v in ctm.split()]
            if len(vals) == 6:
                a, b, cc, d, e, f = vals
                c.transform(a, b, cc, d, e * MM_TO_PT, f * MM_TO_PT)

        for tc in el.findall(ofd_tag("TextCode")):
            self._render_text_code(c, tc, font_name, size_pt, bw, bh)

        c.restoreState()

    def _render_text_code(self, c: canvas.Canvas, tc: etree._Element,
                          font_name: str, size_pt: float, bw: float, bh: float):
        """渲染单个 TextCode: 解析 DeltaX 逐字定位。

        Y 轴翻转: OFD 中 TextCode Y 是从 Boundary 顶部向下的距离,
        PDF 中需要从 Boundary 底部向上的距离 = bh - Y。
        """
        text = tc.text or ""
        if not text:
            return

        x_start = float(tc.get("X", "0"))
        y_start = float(tc.get("Y", "0"))
        delta_x_str = tc.get("DeltaX", "")

        deltas = self._parse_delta_x(delta_x_str, len(text))

        # 计算每个字符的 X 位置 (mm, 相对于 Boundary 原点)
        positions = [x_start]
        for d in deltas:
            positions.append(positions[-1] + d)

        # DeltaX 不足时: 用最后一个 delta 延伸剩余位置
        if len(positions) < len(text) and deltas:
            last_delta = deltas[-1]
            while len(positions) < len(text):
                positions.append(positions[-1] + last_delta)

        # Y 翻转: bh - Y (从底部计算)
        y_pt = (bh - y_start) * MM_TO_PT

        for i, ch in enumerate(text):
            if i < len(positions):
                x_pt = positions[i] * MM_TO_PT
                c.drawString(x_pt, y_pt, ch)

    def _parse_delta_x(self, delta_str: str, num_chars: int) -> list:
        """解析 OFD DeltaX 字符串为浮点偏移量列表。

        格式:
          - "g N W": 重复 W 共 N 次
          - "W": 单个偏移 W
          - 混合: "g 2 3.175 1.6 g 3 3.175"
        """
        if not delta_str:
            return []
        tokens = delta_str.strip().split()
        deltas = []
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.lower() == 'g':
                if i + 2 < len(tokens):
                    count = int(float(tokens[i + 1]))
                    value = float(tokens[i + 2])
                    deltas.extend([value] * count)
                    i += 3
                else:
                    i += 1
            else:
                try:
                    deltas.append(float(tok))
                except ValueError:
                    pass
                i += 1
        return deltas

    # ── PathObject 渲染 ───────────────────────────────

    def _render_path_object(self, c: canvas.Canvas, el: etree._Element,
                            layer_dp_id: Optional[str] = None,
                            is_template: bool = True):
        """渲染路径对象: 解析 AbbreviatedData, 映射 reportlab 路径 API。

        颜色规则 (与 TextObject 一致):
          - is_template=True (模板层): 保持 DrawParam 原始颜色
          - is_template=False (页面内容层): 无显式颜色时强制黑色描边、无填充
          - 所有颜色设置在 saveState/restoreState 内, 防止泄漏
        """
        boundary = self._parse_boundary(el)
        if boundary is None:
            return
        bx, by, bw, bh = boundary

        abbr_el = el.find(ofd_tag("AbbreviatedData"))
        if abbr_el is None or not abbr_el.text:
            return
        path_cmds = self._parse_abbreviated_data(abbr_el.text)

        if not path_cmds:
            return

        # 绘制参数
        line_width = el.get("LineWidth")
        fill_attr = el.get("Fill", "")
        stroke_attr = el.get("Stroke", "")
        draw_param_id = el.get("DrawParam", layer_dp_id or "")

        dp_params = self.doc.resources.resolve_draw_param(draw_param_id) if draw_param_id else {}

        # 线宽
        if line_width:
            lw_pt = float(line_width) * MM_TO_PT
        elif 'line_width' in dp_params:
            lw_pt = dp_params['line_width'] * MM_TO_PT
        else:
            lw_pt = 0.212 * MM_TO_PT

        # 描边颜色
        stroke_color = self._get_stroke_color(el, dp_params)
        # 填充颜色
        fill_color = self._get_fill_color(el, dp_params)

        ctm = el.get("CTM")

        c.saveState()
        c.setLineWidth(lw_pt)

        # Join/Cap
        if 'join' in dp_params:
            join_map = {"Miter": 0, "Round": 1, "Bevel": 2}
            c.setLineJoin(join_map.get(dp_params['join'], 0))
        if 'cap' in dp_params:
            cap_map = {"Butt": 0, "Round": 1, "Square": 2}
            c.setLineCap(cap_map.get(dp_params['cap'], 0))

        # 描边颜色 (在 saveState 内)
        do_stroke = stroke_attr != "false" or (stroke_attr == "" and (stroke_color is not None or 'stroke_color' in dp_params))
        if do_stroke and stroke_color:
            r, g, b = stroke_color.to_rgb(self.doc.resources.color_spaces)
            c.setStrokeColorRGB(r, g, b)
        elif do_stroke:
            # 无显式颜色: 页面内容强制黑色, 模板层也默认黑色
            c.setStrokeColorRGB(0, 0, 0)

        # 填充颜色 (在 saveState 内, 防止泄漏)
        do_fill = fill_attr == "true" or (fill_attr == "" and fill_color is not None)
        if do_fill and fill_color:
            # 有显式填充色: 使用它
            r, g, b = fill_color.to_rgb(self.doc.resources.color_spaces)
            c.setFillColorRGB(r, g, b)
        elif do_fill and is_template:
            # 模板层有 Fill="true" 但无显式色: 使用 DrawParam 的 fill_color (暗红)
            if 'fill_color' in dp_params:
                r, g, b = dp_params['fill_color'].to_rgb(self.doc.resources.color_spaces)
                c.setFillColorRGB(r, g, b)
            else:
                c.setFillColorRGB(0, 0, 0)
        elif do_fill and not is_template:
            # 页面内容层 (如叉叉图): 透明底色 (无填充)
            do_fill = False

        # 翻转 Y: OFD 左上原点 → PDF 左下原点
        by_flipped = self._flip_boundary_y(by, bh)
        c.translate(bx * MM_TO_PT, by_flipped * MM_TO_PT)

        if ctm:
            vals = [float(v) for v in ctm.split()]
            if len(vals) == 6:
                a, b, cc, d, e, f = vals
                c.transform(a, b, cc, d, e * MM_TO_PT, f * MM_TO_PT)

        # 存储 Boundary 高度供路径 Y 翻转使用
        self._boundary_h_mm = bh
        self._cur_pt_mm = (0.0, 0.0)

        path = c.beginPath()
        self._build_path(path, path_cmds)
        c.drawPath(path, stroke=1 if do_stroke else 0, fill=1 if do_fill else 0)

        c.restoreState()

    def _parse_abbreviated_data(self, data: str) -> list:
        """解析 AbbreviatedData 命令序列。

        返回 [(cmd, [float, ...]), ...] 列表。
        命令: M(moveto) L(lineto) B(cubic bezier 6值) Q(quad bezier 4值)
              A(arc 7值) C(close 0值) S(scale)
        """
        tokens = re.split(r'([MLBQACmlbqacS])', data)
        commands = []
        current_cmd = None

        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            if tok in ('M', 'L', 'B', 'Q', 'A', 'C', 'm', 'l', 'b', 'q', 'a', 'c', 'S'):
                current_cmd = tok.upper()
                commands.append((current_cmd, []))
            elif current_cmd is not None:
                for part in tok.split():
                    try:
                        commands[-1][1].append(float(part))
                    except ValueError:
                        pass

        return commands

    def _build_path(self, path, commands: list):
        """将解析后的命令序列应用到 reportlab 路径对象。

        Y 轴翻转: 路径坐标 Y 是相对于 Boundary 顶部向下的距离,
        需翻转为从底部向上 = boundary_h - y。
        """
        bh = self._boundary_h_mm

        for cmd, vals in commands:
            if cmd == 'M' and len(vals) >= 2:
                x, y = vals[0], vals[1]
                y_flipped = self._flip_local_y(y)
                path.moveTo(x * MM_TO_PT, y_flipped * MM_TO_PT)
                self._cur_pt_mm = (x, y_flipped)
            elif cmd == 'L' and len(vals) >= 2:
                x, y = vals[0], vals[1]
                y_flipped = self._flip_local_y(y)
                path.lineTo(x * MM_TO_PT, y_flipped * MM_TO_PT)
                self._cur_pt_mm = (x, y_flipped)
            elif cmd == 'B' and len(vals) >= 6:
                # 三次贝塞尔: (cp1x, cp1y, cp2x, cp2y, x, y)
                cp1x, cp1y, cp2x, cp2y, x, y = vals[:6]
                cp1y_f = self._flip_local_y(cp1y)
                cp2y_f = self._flip_local_y(cp2y)
                y_f = self._flip_local_y(y)
                path.curveTo(
                    cp1x * MM_TO_PT, cp1y_f * MM_TO_PT,
                    cp2x * MM_TO_PT, cp2y_f * MM_TO_PT,
                    x * MM_TO_PT, y_f * MM_TO_PT
                )
                self._cur_pt_mm = (x, y_f)
            elif cmd == 'Q' and len(vals) >= 4:
                # 二次贝塞尔转三次贝塞尔
                qx, qy, x, y = vals[:4]
                cx, cy = self._cur_pt_mm
                qy_f = self._flip_local_y(qy)
                y_f = self._flip_local_y(y)
                # 控制点转换 (二次→三次)
                cp1x = cx + 2.0/3.0 * (qx - cx)
                cp1y = cy + 2.0/3.0 * (qy_f - cy)
                cp2x = x + 2.0/3.0 * (qx - x)
                cp2y = y_f + 2.0/3.0 * (qy_f - y_f)
                path.curveTo(
                    cp1x * MM_TO_PT, cp1y * MM_TO_PT,
                    cp2x * MM_TO_PT, cp2y * MM_TO_PT,
                    x * MM_TO_PT, y_f * MM_TO_PT
                )
                self._cur_pt_mm = (x, y_f)
            elif cmd == 'A' and len(vals) >= 7:
                # 弧线: 简化为直线 (电子发票中极少见)
                x, y = vals[5], vals[6]
                y_flipped = self._flip_local_y(y)
                path.lineTo(x * MM_TO_PT, y_flipped * MM_TO_PT)
                self._cur_pt_mm = (x, y_flipped)
            elif cmd == 'C':
                path.close()

    # ── ImageObject 渲染 ─────────────────────────────

    def _render_image_object(self, c: canvas.Canvas, el: etree._Element,
                             layer_dp_id: Optional[str] = None):
        """渲染图片对象: 从 ZIP 读取图片 → drawImage。

        关键修正:
        - Y 轴翻转: Boundary Y 从左上原点翻转为左下原点
        - CTM 缩放: CTM 存在时, 图片应按单位尺寸 (1mm) 绘制,
          让 CTM 负责缩放到 Boundary 尺寸。否则图片会被 CTM 二次放大。
        """
        boundary = self._parse_boundary(el)
        if boundary is None:
            return
        bx, by, bw, bh = boundary

        resource_id = el.get("ResourceID", "")
        if not resource_id:
            return

        media_path = self.doc.resources.get_media_path(resource_id)
        if not media_path:
            logger.warning(f"未找到图片资源: ResourceID={resource_id}")
            return

        try:
            img_bytes = self.doc.zip_file.read(media_path)
        except KeyError:
            logger.warning(f"ZIP 中未找到图片文件: {media_path}")
            return

        ctm = el.get("CTM")

        c.saveState()
        # 翻转 Y
        by_flipped = self._flip_boundary_y(by, bh)
        c.translate(bx * MM_TO_PT, by_flipped * MM_TO_PT)

        if ctm:
            vals = [float(v) for v in ctm.split()]
            if len(vals) == 6:
                a, b, cc, d, e, f = vals
                # CTM 的 e,f 为 mm 平移, 转为 pt
                c.transform(a, b, cc, d, e * MM_TO_PT, f * MM_TO_PT)
                # CTM 存在时: 图片按 1mm x 1mm 单位绘制, CTM 负责缩放到 Boundary 尺寸
                # 例如 CTM="20 0 0 20 0 0" 会将 1mm 放大到 20mm = Boundary 宽度
                draw_w = MM_TO_PT
                draw_h = MM_TO_PT
            else:
                draw_w = bw * MM_TO_PT
                draw_h = bh * MM_TO_PT
        else:
            # 无 CTM: 直接按 Boundary 尺寸绘制
            draw_w = bw * MM_TO_PT
            draw_h = bh * MM_TO_PT

        try:
            img_reader = ImageReader(io.BytesIO(img_bytes))
            c.drawImage(img_reader, 0, 0, draw_w, draw_h,
                       mask='auto', preserveAspectRatio=False)
        except Exception as e:
            logger.warning(f"图片绘制失败 ({media_path}): {e}")

        c.restoreState()

    # ── ComposeObject 渲染 ────────────────────────────

    def _render_compose_object(self, c: canvas.Canvas, el: etree._Element,
                               layer_dp_id: Optional[str] = None):
        """渲染组合对象: 递归渲染子元素。"""
        boundary = self._parse_boundary(el)
        ctm = el.get("CTM")

        c.saveState()
        if boundary:
            bx, by, bw, bh = boundary
            # 翻转 Y
            by_flipped = self._flip_boundary_y(by, bh)
            c.translate(bx * MM_TO_PT, by_flipped * MM_TO_PT)
        if ctm:
            vals = [float(v) for v in ctm.split()]
            if len(vals) == 6:
                a, b, cc, d, e, f = vals
                c.transform(a, b, cc, d, e * MM_TO_PT, f * MM_TO_PT)

        for child in el:
            if not isinstance(child.tag, str):
                continue
            tag = etree.QName(child.tag).localname
            if tag == "TextObject":
                self._render_text_object(c, child, layer_dp_id)
            elif tag == "PathObject":
                self._render_path_object(c, child, layer_dp_id)
            elif tag == "ImageObject":
                self._render_image_object(c, child, layer_dp_id)

        c.restoreState()

    # ── 辅助方法 ──────────────────────────────────────

    def _parse_boundary(self, el: etree._Element) -> Optional[tuple]:
        """解析 Boundary 属性 "x y w h" (mm)。"""
        b = el.get("Boundary", "")
        if not b:
            return None
        vals = b.split()
        if len(vals) >= 4:
            try:
                return (float(vals[0]), float(vals[1]), float(vals[2]), float(vals[3]))
            except ValueError:
                return None
        return None

    def _get_stroke_color(self, el: etree._Element, dp_params: dict) -> Optional[ColorValue]:
        """获取描边颜色: 优先对象 StrokeColor, 其次 DrawParam。"""
        sc = el.find(ofd_tag("StrokeColor"))
        if sc is not None:
            return ColorValue(
                values=[float(v) for v in sc.get("Value", "").split()],
                color_space_id=sc.get("ColorSpace", "")
            )
        if 'stroke_color' in dp_params:
            return dp_params['stroke_color']
        return None

    def _get_fill_color(self, el: etree._Element, dp_params_or_id=None) -> Optional[ColorValue]:
        """获取填充颜色: 优先对象 FillColor, 其次 DrawParam。"""
        fc = el.find(ofd_tag("FillColor"))
        if fc is not None:
            return ColorValue(
                values=[float(v) for v in fc.get("Value", "").split()],
                color_space_id=fc.get("ColorSpace", "")
            )
        if isinstance(dp_params_or_id, dict):
            if 'fill_color' in dp_params_or_id:
                return dp_params_or_id['fill_color']
        elif isinstance(dp_params_or_id, str) and dp_params_or_id:
            dp_params = self.doc.resources.resolve_draw_param(dp_params_or_id)
            if 'fill_color' in dp_params:
                return dp_params['fill_color']
        return None
