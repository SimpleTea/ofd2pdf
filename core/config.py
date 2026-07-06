"""配置持久化模块 — 保存和读取用户设置。

功能:
  - 输出目录持久化: 自动保存用户选择的输出目录, 下次启动时自动恢复
  - 默认输出目录: 若未设置过, 则使用程序所在目录 (exe 或脚本同级目录)
  - 配置文件位置: ~/.ofd2pdf/config.json (跨会话持久化)
"""

from __future__ import annotations

import json
import os
import sys
import logging

logger = logging.getLogger(__name__)

VERSION = "1.6"


def get_app_dir() -> str:
    """获取程序所在目录 (exe 或脚本同级目录)。

    PyInstaller 打包后, sys.frozen 为 True, sys.executable 指向 exe 路径。
    脚本模式下, 使用 main.py 所在目录。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(sys.argv[0]))


def get_config_dir() -> str:
    """获取配置文件目录。"""
    return os.path.join(os.path.expanduser("~"), ".ofd2pdf")


def get_config_path() -> str:
    """获取配置文件完整路径。"""
    return os.path.join(get_config_dir(), "config.json")


def load_config() -> dict:
    """从文件加载配置。文件不存在或格式错误时返回空字典。"""
    path = get_config_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取配置失败: {e}")
        return {}


def save_config(config: dict):
    """保存配置到文件。"""
    path = get_config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"保存配置失败: {e}")


def get_output_dir() -> str:
    """获取输出目录: 优先已保存的值, 否则默认程序所在目录。"""
    config = load_config()
    output_dir = config.get('output_dir', '')
    if output_dir and os.path.isdir(output_dir):
        return output_dir
    # 默认: 程序所在目录
    return get_app_dir()


def set_output_dir(path: str):
    """保存输出目录。"""
    config = load_config()
    config['output_dir'] = path
    save_config(config)
    logger.info(f"输出目录已保存: {path}")
