"""OFD 资源模型 — 定义字体、颜色空间、绘制参数、多媒体资源的数据结构与解析逻辑。

OFD (GB/T 33190-2016) 资源体系:
  - Font: 字体定义, 可嵌入 TTF 子集 (FontFile) 或引用系统字体 (FontName/FamilyName)
  - ColorSpace: 颜色空间 (RGB / CMYK / GRAY)
  - DrawParam: 绘制参数, 支持 Relative 属性继承父级 (LineWidth / StrokeColor / FillColor)
  - MultiMedia: 多媒体资源 (图片 PNG/JPEG 等)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── 常量 ──────────────────────────────────────────────
MM_TO_PT = 2.834645669  # 1 mm = 2.834645669 pt (72/25.4)
OFD_NS = "http://www.ofdspec.org/2016"
NS_MAP = {"ofd": OFD_NS}


def ofd_tag(name: str) -> str:
    """生成带命名空间的完整标签名, 如 ofd_tag('Font') -> '{http://www.ofdspec.org/2016}Font'"""
    return f"{{{OFD_NS}}}{name}"


# ── 数据类 ─────────────────────────────────────────────

@dataclass
class Font:
    """OFD 字体资源。"""
    id: str
    font_name: str = ""
    family_name: str = ""
    font_file: str = ""          # ZIP 内 TTF 路径 (如 'Doc_0/font_6955.ttf'), 空表示系统字体
    is_embedded: bool = False
    # 运行时: 注册到 reportlab 的字体名
    registered_name: str = ""

    def resolve_font_name(self) -> str:
        """返回最适合的字体显示名, 用于系统字体回退匹配。"""
        if self.font_name:
            return self.font_name
        if self.family_name:
            return self.family_name
        return "SimSun"


@dataclass
class ColorSpace:
    """OFD 颜色空间。"""
    id: str
    type: str = "RGB"  # RGB / CMYK / GRAY


@dataclass
class ColorValue:
    """解析后的颜色值。"""
    values: list = field(default_factory=list)  # [r, g, b] 或 [c, m, y, k] 或 [gray]
    color_space_id: str = ""

    def to_rgb(self, color_spaces: dict) -> tuple:
        """转换为 (r, g, b) 三元组, 值域 0~1。"""
        if not self.values:
            return (0, 0, 0)
        cs = color_spaces.get(self.color_space_id)
        cs_type = cs.type if cs else "RGB"
        vals = [v / 255.0 for v in self.values]
        if cs_type == "RGB" and len(vals) >= 3:
            return (vals[0], vals[1], vals[2])
        elif cs_type == "GRAY" and len(vals) >= 1:
            return (vals[0], vals[0], vals[0])
        elif cs_type == "CMYK" and len(vals) >= 4:
            # CMYK -> RGB 简易转换
            r = 1.0 - min(1.0, vals[0] + vals[3])
            g = 1.0 - min(1.0, vals[1] + vals[3])
            b = 1.0 - min(1.0, vals[2] + vals[3])
            return (r, g, b)
        # 默认按 RGB 处理
        if len(vals) >= 3:
            return (vals[0], vals[1], vals[2])
        return (0, 0, 0)


@dataclass
class DrawParam:
    """OFD 绘制参数, 支持 Relative 继承链。"""
    id: str
    line_width: Optional[float] = None
    relative: Optional[str] = None       # 父级 DrawParam ID
    stroke_color: Optional[ColorValue] = None
    fill_color: Optional[ColorValue] = None
    join: str = "Miter"                  # Miter / Round / Bevel
    cap: str = "Butt"                    # Butt / Round / Square
    dash_pattern: str = ""               # 虚线模式

    def resolve(self, draw_params: dict) -> dict:
        """递归解析继承链, 返回合并后的最终绘制参数。"""
        result = {}
        # 先解析父级
        if self.relative and self.relative in draw_params:
            parent = draw_params[self.relative]
            result.update(parent.resolve(draw_params))
        # 再用自身覆盖
        if self.line_width is not None:
            result['line_width'] = self.line_width
        if self.stroke_color is not None:
            result['stroke_color'] = self.stroke_color
        if self.fill_color is not None:
            result['fill_color'] = self.fill_color
        if self.join != "Miter":
            result['join'] = self.join
        if self.cap != "Butt":
            result['cap'] = self.cap
        if self.dash_pattern:
            result['dash_pattern'] = self.dash_pattern
        return result


@dataclass
class MultiMedia:
    """OFD 多媒体资源 (图片等)。"""
    id: str
    type: str = "Image"        # Image / Video / Audio
    format: str = ""           # PNG / JPEG 等
    media_file: str = ""       # ZIP 内路径


@dataclass
class PageTemplate:
    """OFD 模板页引用。"""
    template_id: str            # Document.xml 中 TemplatePage 的 ID
    z_order: str = "Background"  # Background / Foreground


# ── 资源模型容器 ───────────────────────────────────────

class ResourceModel:
    """管理 OFD 文档所有资源的容器, 提供 ID → 资源查询。"""

    def __init__(self):
        self.fonts: dict[str, Font] = {}
        self.color_spaces: dict[str, ColorSpace] = {}
        self.draw_params: dict[str, DrawParam] = {}
        self.multi_medias: dict[str, MultiMedia] = {}
        self.base_loc: str = ""  # DocumentRes 的 BaseLoc (如 'Res')

    def add_font(self, font: Font):
        self.fonts[font.id] = font

    def add_color_space(self, cs: ColorSpace):
        self.color_spaces[cs.id] = cs

    def add_draw_param(self, dp: DrawParam):
        self.draw_params[dp.id] = dp

    def add_multi_media(self, mm: MultiMedia):
        self.multi_medias[mm.id] = mm

    def get_font(self, font_id: str) -> Optional[Font]:
        return self.fonts.get(font_id)

    def get_color_space(self, cs_id: str) -> Optional[ColorSpace]:
        return self.color_spaces.get(cs_id)

    def get_draw_param(self, dp_id: str) -> Optional[DrawParam]:
        return self.draw_params.get(dp_id)

    def resolve_draw_param(self, dp_id: str) -> dict:
        """解析 DrawParam 继承链, 返回最终参数字典。"""
        dp = self.draw_params.get(dp_id)
        if dp:
            return dp.resolve(self.draw_params)
        return {}

    def get_multi_media(self, mm_id: str) -> Optional[MultiMedia]:
        return self.multi_medias.get(mm_id)

    def get_media_path(self, resource_id: str) -> str:
        """根据 ResourceID 获取多媒体文件在 ZIP 内的完整路径。"""
        mm = self.multi_medias.get(resource_id)
        if not mm:
            return ""
        if self.base_loc and mm.media_file:
            return f"{self.base_loc}/{mm.media_file}"
        return mm.media_file
