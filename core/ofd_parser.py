"""OFD 解析器 — 使用 zipfile 解包 OFD, 用 lxml 解析全链路 XML 结构。

解析流程:
  1. zipfile 打开 .ofd → 读取 OFD.xml (根清单)
  2. OFD.xml → DocBody → DocRoot → Doc_N/Document.xml
  3. Document.xml → CommonData (PublicRes / DocumentRes / TemplatePage) + Pages
  4. PublicRes.xml → Fonts / ColorSpaces / DrawParams
  5. DocumentRes.xml → MultiMedias (图片资源)
  6. Pages/Page_N/Content.xml → 页面尺寸 + Template 引用 + Layer → TextObject/PathObject/ImageObject
  7. Tpls/Tpl_N/Content.xml → 模板页内容 (结构与页面 Content.xml 相同)
  8. Annotations.xml → 注释 (水印等)

设计原则: 解析器只负责读取 XML 并构建内存数据结构 (lxml Element 树),
          不做任何渲染逻辑。渲染由 pdf_renderer.py 负责。
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Optional
from lxml import etree

from .resource_model import (
    ColorSpace, ColorValue, DrawParam, Font, MultiMedia,
    PageTemplate, ResourceModel, ofd_tag,
)

logger = logging.getLogger(__name__)


# ── 数据结构 ───────────────────────────────────────────

@dataclass
class OFDAnnotation:
    """OFD 注释 (水印/监制章等)。"""
    annot_id: str
    annot_type: str = ""            # Watermark / Stamp 等
    appearance_root: Optional[etree._Element] = None  # Appearance 的 XML 根节点


@dataclass
class OFDPageInfo:
    """单页 OFD 的解析信息。"""
    page_id: str
    base_loc: str                    # Content.xml 在 ZIP 内路径
    physical_box: tuple = (0, 0, 210, 140)  # (x, y, w, h) in mm
    template: Optional[PageTemplate] = None
    content_root: Optional[etree._Element] = None  # Content.xml 的 ofd:Page 根节点
    doc_prefix: str = ""             # 文档前缀路径 (如 'Doc_0')
    annotations: list[OFDAnnotation] = field(default_factory=list)  # 页面注释列表


@dataclass
class OFDDocument:
    """完整的 OFD 文档解析结果。"""
    ofd_path: str
    zip_file: zipfile.ZipFile
    pages: list[OFDPageInfo] = field(default_factory=list)
    template_pages: dict[str, OFDPageInfo] = field(default_factory=dict)  # ID → 模板页
    resources: ResourceModel = None
    # 原始 XML 根节点引用 (供渲染器遍历)
    doc_root: Optional[etree._Element] = None
    doc_prefix: str = "Doc_0"

    def close(self):
        if self.zip_file:
            self.zip_file.close()


class OFDParser:
    """OFD 文件解析器。"""

    def __init__(self, ofd_path: str):
        self.ofd_path = ofd_path
        self._zip: Optional[zipfile.ZipFile] = None
        self._doc_prefix: str = "Doc_0"

    # ── 公共方法 ──────────────────────────────────────

    def parse(self) -> OFDDocument:
        """解析 OFD 文件, 返回 OFDDocument。"""
        self._zip = zipfile.ZipFile(self.ofd_path, 'r')
        doc = OFDDocument(ofd_path=self.ofd_path, zip_file=self._zip)

        # 1. 解析 OFD.xml → 定位 DocRoot
        doc_root_path = self._parse_ofd_xml()
        if not doc_root_path:
            raise ValueError(f"无法在 OFD.xml 中找到 DocRoot 路径: {self.ofd_path}")

        # 提取文档前缀 (如 'Doc_0')
        self._doc_prefix = doc_root_path.split('/')[0] if '/' in doc_root_path else ""
        doc.doc_prefix = self._doc_prefix

        # 2. 解析 Document.xml
        doc_xml = self._read_xml(doc_root_path)
        doc.doc_root = doc_xml

        # 3. 解析资源 (PublicRes + DocumentRes)
        resources = ResourceModel()
        self._parse_resources(doc_xml, resources)
        doc.resources = resources

        # 4. 解析模板页
        self._parse_template_pages(doc_xml, doc)

        # 5. 解析页面
        self._parse_pages(doc_xml, doc)

        # 6. 解析注释 (Annotations - 监制章/水印等)
        self._parse_annotations(doc_xml, doc)

        logger.info(f"OFD 解析完成: {len(doc.pages)} 页, {len(doc.template_pages)} 模板, "
                     f"{len(resources.fonts)} 字体, {len(resources.multi_medias)} 图片")
        return doc

    # ── XML 读取 ──────────────────────────────────────

    def _read_xml(self, path_in_zip: str) -> etree._Element:
        """从 ZIP 中读取 XML 文件并返回 lxml Element 根节点。"""
        raw = self._zip.read(path_in_zip)
        # 去除 BOM
        if raw.startswith(b'\xef\xbb\xbf'):
            raw = raw[3:]
        return etree.fromstring(raw)

    def _read_bytes(self, path_in_zip: str) -> bytes:
        """从 ZIP 中读取二进制文件。"""
        return self._zip.read(path_in_zip)

    def _safe_path(self, *parts) -> str:
        """拼接 ZIP 内路径, 处理前缀。"""
        parts = [p for p in parts if p]
        return "/".join(parts)

    def _resolve_path(self, base: str, ref: str) -> str:
        """解析相对路径引用 (如 PublicRes.xml 相对于 Doc_0/)。"""
        if not ref:
            return ""
        # 如果 ref 已含文档前缀, 直接返回
        if self._doc_prefix and ref.startswith(self._doc_prefix + "/"):
            return ref
        return self._safe_path(self._doc_prefix, base, ref) if base else self._safe_path(self._doc_prefix, ref)

    # ── OFD.xml 解析 ──────────────────────────────────

    def _parse_ofd_xml(self) -> str:
        """解析 OFD.xml, 返回 DocRoot 路径 (如 'Doc_0/Document.xml')。"""
        try:
            root = self._read_xml("OFD.xml")
        except KeyError:
            # 兼容大小写
            root = self._read_xml("ofd.xml")

        # 查找 ofd:DocBody > ofd:DocRoot
        doc_body = root.find(ofd_tag("DocBody"))
        if doc_body is None:
            raise ValueError("OFD.xml 中未找到 DocBody 元素")

        doc_root = doc_body.find(ofd_tag("DocRoot"))
        if doc_root is not None and doc_root.text:
            return doc_root.text.strip()

        raise ValueError("OFD.xml 中未找到 DocRoot 元素")

    # ── 资源解析 ──────────────────────────────────────

    def _parse_resources(self, doc_xml: etree._Element, resources: ResourceModel):
        """解析 PublicRes.xml 和 DocumentRes.xml。"""
        common_data = doc_xml.find(ofd_tag("CommonData"))
        if common_data is None:
            logger.warning("Document.xml 中未找到 CommonData")
            return

        # PublicRes
        public_res_node = common_data.find(ofd_tag("PublicRes"))
        if public_res_node is not None and public_res_node.text:
            path = self._resolve_path("", public_res_node.text.strip())
            try:
                self._parse_res_file(path, resources)
            except (KeyError, OSError) as e:
                logger.warning(f"解析 PublicRes 失败: {path} - {e}")

        # DocumentRes
        doc_res_node = common_data.find(ofd_tag("DocumentRes"))
        if doc_res_node is not None and doc_res_node.text:
            path = self._resolve_path("", doc_res_node.text.strip())
            try:
                self._parse_res_file(path, resources)
            except (KeyError, OSError) as e:
                logger.warning(f"解析 DocumentRes 失败: {path} - {e}")

    def _parse_res_file(self, path: str, resources: ResourceModel):
        """解析 Res.xml (PublicRes 或 DocumentRes), 加载字体/颜色空间/绘制参数/多媒体。"""
        root = self._read_xml(path)
        base_loc = root.get("BaseLoc", "")

        # 颜色空间
        for cs in root.findall(ofd_tag("ColorSpace")):
            cs_id = cs.get("ID", "")
            cs_type = cs.get("Type", "RGB")
            resources.add_color_space(ColorSpace(id=cs_id, type=cs_type))
        # 也可能在 ColorSpaces 容器下
        cs_container = root.find(ofd_tag("ColorSpaces"))
        if cs_container is not None:
            for cs in cs_container.findall(ofd_tag("ColorSpace")):
                cs_id = cs.get("ID", "")
                cs_type = cs.get("Type", "RGB")
                resources.add_color_space(ColorSpace(id=cs_id, type=cs_type))

        # 字体
        fonts_container = root.find(ofd_tag("Fonts"))
        font_parent = fonts_container if fonts_container is not None else root
        for font_el in font_parent.findall(ofd_tag("Font")):
            font_id = font_el.get("ID", "")
            font_name = font_el.get("FontName", "")
            family_name = font_el.get("FamilyName", "")
            font_file_el = font_el.find(ofd_tag("FontFile"))
            font_file = font_file_el.text.strip() if font_file_el is not None and font_file_el.text else ""
            is_embedded = bool(font_file)
            if is_embedded and base_loc:
                font_file = self._safe_path(self._doc_prefix, base_loc, font_file)
            elif is_embedded:
                font_file = self._safe_path(self._doc_prefix, font_file)
            resources.add_font(Font(
                id=font_id, font_name=font_name, family_name=family_name,
                font_file=font_file, is_embedded=is_embedded
            ))

        # 绘制参数
        dp_container = root.find(ofd_tag("DrawParams"))
        dp_parent = dp_container if dp_container is not None else root
        for dp_el in dp_parent.findall(ofd_tag("DrawParam")):
            dp_id = dp_el.get("ID", "")
            line_width = dp_el.get("LineWidth")
            relative = dp_el.get("Relative")
            join = dp_el.get("Join", "Miter")
            cap = dp_el.get("Cap", "Butt")
            dash = dp_el.get("DashPattern", "")

            stroke_color = None
            fill_color = None
            stroke_node = dp_el.find(ofd_tag("StrokeColor"))
            if stroke_node is not None:
                stroke_color = ColorValue(
                    values=[float(v) for v in stroke_node.get("Value", "").split()],
                    color_space_id=stroke_node.get("ColorSpace", "")
                )
            fill_node = dp_el.find(ofd_tag("FillColor"))
            if fill_node is not None:
                fill_color = ColorValue(
                    values=[float(v) for v in fill_node.get("Value", "").split()],
                    color_space_id=fill_node.get("ColorSpace", "")
                )

            resources.add_draw_param(DrawParam(
                id=dp_id,
                line_width=float(line_width) if line_width else None,
                relative=relative,
                stroke_color=stroke_color,
                fill_color=fill_color,
                join=join, cap=cap, dash_pattern=dash
            ))

        # 多媒体
        mm_container = root.find(ofd_tag("MultiMedias"))
        if mm_container is not None:
            if base_loc:
                resources.base_loc = self._safe_path(self._doc_prefix, base_loc)
            else:
                resources.base_loc = self._doc_prefix
            for mm_el in mm_container.findall(ofd_tag("MultiMedia")):
                mm_id = mm_el.get("ID", "")
                mm_type = mm_el.get("Type", "Image")
                mm_format = mm_el.get("Format", "")
                media_file_el = mm_el.find(ofd_tag("MediaFile"))
                media_file = media_file_el.text.strip() if media_file_el is not None and media_file_el.text else ""
                resources.add_multi_media(MultiMedia(
                    id=mm_id, type=mm_type, format=mm_format, media_file=media_file
                ))

    # ── 模板页解析 ────────────────────────────────────

    def _parse_template_pages(self, doc_xml: etree._Element, doc: OFDDocument):
        """解析 Document.xml 中的 TemplatePage 定义。"""
        common_data = doc_xml.find(ofd_tag("CommonData"))
        if common_data is None:
            return

        for tpl in common_data.findall(ofd_tag("TemplatePage")):
            tpl_id = tpl.get("ID", "")
            base_loc = tpl.get("BaseLoc", "")
            if not tpl_id or not base_loc:
                continue

            content_path = self._resolve_path("", base_loc.strip())
            try:
                content_root = self._read_xml(content_path)
            except (KeyError, OSError) as e:
                logger.warning(f"解析模板页失败: {content_path} - {e}")
                continue

            page_info = OFDPageInfo(
                page_id=tpl_id,
                base_loc=content_path,
                content_root=content_root,
                doc_prefix=self._doc_prefix,
            )
            # 模板页也可能有 PhysicalBox
            self._extract_physical_box(content_root, page_info)
            doc.template_pages[tpl_id] = page_info

    # ── 页面解析 ──────────────────────────────────────

    def _parse_pages(self, doc_xml: etree._Element, doc: OFDDocument):
        """解析 Document.xml 中的 Pages 列表。"""
        pages_node = doc_xml.find(ofd_tag("Pages"))
        if pages_node is None:
            logger.warning("Document.xml 中未找到 Pages 元素")
            return

        for page_el in pages_node.findall(ofd_tag("Page")):
            page_id = page_el.get("ID", "")
            base_loc = page_el.get("BaseLoc", "")
            if not base_loc:
                continue

            content_path = self._resolve_path("", base_loc.strip())
            try:
                content_root = self._read_xml(content_path)
            except (KeyError, OSError) as e:
                logger.error(f"解析页面 Content.xml 失败: {content_path} - {e}")
                continue

            page_info = OFDPageInfo(
                page_id=page_id,
                base_loc=content_path,
                content_root=content_root,
                doc_prefix=self._doc_prefix,
            )
            self._extract_physical_box(content_root, page_info)
            self._extract_template_ref(content_root, page_info)
            doc.pages.append(page_info)

    def _extract_physical_box(self, page_root: etree._Element, page_info: OFDPageInfo):
        """从 Content.xml 提取 PhysicalBox 页面尺寸。"""
        area = page_root.find(ofd_tag("Area"))
        if area is None:
            return
        phys_box = area.find(ofd_tag("PhysicalBox"))
        if phys_box is not None and phys_box.text:
            vals = [float(v) for v in phys_box.text.split()]
            if len(vals) >= 4:
                page_info.physical_box = (vals[0], vals[1], vals[2], vals[3])
        # 也检查 ApplicationBox 作为回退
        if page_info.physical_box == (0, 0, 210, 140):
            app_box = area.find(ofd_tag("ApplicationBox"))
            if app_box is not None and app_box.text:
                vals = [float(v) for v in app_box.text.split()]
                if len(vals) >= 4:
                    page_info.physical_box = (0, 0, vals[2], vals[3])

    def _extract_template_ref(self, page_root: etree._Element, page_info: OFDPageInfo):
        """从页面 Content.xml 提取 Template 引用。"""
        tpl = page_root.find(ofd_tag("Template"))
        if tpl is not None:
            page_info.template = PageTemplate(
                template_id=tpl.get("TemplateID", ""),
                z_order=tpl.get("ZOrder", "Background")
            )

    # ── 注释解析 ──────────────────────────────────────

    def _parse_annotations(self, doc_xml: etree._Element, doc: OFDDocument):
        """解析 Annotations.xml → 各页面 Annotation.xml。

        注释包含: 水印 (Watermark)、监制章 (Stamp/图片)、下载次数文字等。
        Annotations 节点可能在 Document.xml 根节点下, 也可能在 CommonData 下。
        """
        # 尝试在根节点下查找 Annotations
        annots_node = doc_xml.find(ofd_tag("Annotations"))
        if annots_node is None:
            # 回退: 在 CommonData 下查找
            common_data = doc_xml.find(ofd_tag("CommonData"))
            if common_data is not None:
                annots_node = common_data.find(ofd_tag("Annotations"))
        if annots_node is None:
            return

        # Annotations 节点的 FileLoc 指向 Annotations.xml
        file_loc = annots_node.find(ofd_tag("FileLoc"))
        if file_loc is not None and file_loc.text:
            annots_path = self._resolve_path("", file_loc.text.strip())
        elif annots_node.text and annots_node.text.strip():
            annots_path = self._resolve_path("", annots_node.text.strip())
        else:
            return

        try:
            annots_root = self._read_xml(annots_path)
        except (KeyError, OSError) as e:
            logger.warning(f"解析 Annotations.xml 失败: {annots_path} - {e}")
            return

        # 遍历 Annotations.xml 中的 Page 节点
        for page_node in annots_root.findall(ofd_tag("Page")):
            page_id = page_node.get("PageID", "")
            file_loc = page_node.find(ofd_tag("FileLoc"))
            if file_loc is None or not file_loc.text:
                continue

            # 解析 Annotation.xml 路径 (相对于 Annotations.xml 所在目录)
            annot_dir = "/".join(annots_path.split("/")[:-1])
            annot_file = file_loc.text.strip()
            if annot_file.startswith("Page_"):
                annot_xml_path = self._safe_path(annot_dir, annot_file)
            else:
                annot_xml_path = self._resolve_path("", annot_file)

            try:
                annot_root = self._read_xml(annot_xml_path)
            except (KeyError, OSError) as e:
                logger.warning(f"解析 Annotation.xml 失败: {annot_xml_path} - {e}")
                continue

            # 解析每个 Annot 节点
            annots = []
            for annot_el in annot_root.findall(ofd_tag("Annot")):
                annot_id = annot_el.get("ID", "")
                annot_type = annot_el.get("Type", "")
                appearance = annot_el.find(ofd_tag("Appearance"))
                if appearance is not None:
                    annots.append(OFDAnnotation(
                        annot_id=annot_id,
                        annot_type=annot_type,
                        appearance_root=appearance,
                    ))

            # 将注释关联到对应页面
            target_page = None
            for p in doc.pages:
                if p.page_id == page_id:
                    target_page = p
                    break
            if target_page is None and doc.pages:
                target_page = doc.pages[0]
            if target_page:
                target_page.annotations.extend(annots)

            logger.info(f"解析注释: 页面 {page_id}, {len(annots)} 个注释")
