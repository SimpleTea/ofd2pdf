# ofd2pdf
OFD转PDF的python代码和exe，可以通过代码调用进行单独或批量转换，也支持图形化直接批量转换。
本文档面向需要把 OFD 转 PDF 能力集成到其他 Python 项目的开发者。当前目录包含：已经包含公开 API、子进程入口，以及从本项目 `core` 复制来的核心转换代码。

## 需要打包的代码

只需要将以下目录拷贝或打包到目标项目中：

```text
pkg/
```

`pkg/` 内部结构：

```text
pkg/
├─ __init__.py          # 对外导出 convert_ofd_to_pdf
├─ __main__.py          # 支持 python -m pkg
├─ cli.py               # 子进程转换入口
├─ converter.py         # 公开 API，内部 subprocess.run 调用 cli
├─ dependencies.py      # pip 依赖声明
└─ core/                # OFD 解析、PDF 渲染、水印扫描等核心代码副本
```

推荐目标项目结构：

```text
your_project/
├─ pkg/
└─ your_app.py
```

## pip 依赖

目标项目需要自行安装以下 pip 包：

```bash
pip install lxml reportlab PyMuPDF Pillow
```

也可以从代码中读取依赖清单：

```python
from pkg import REQUIRED_PIP_PACKAGES

print(REQUIRED_PIP_PACKAGES)
# ["lxml", "reportlab", "PyMuPDF", "Pillow"]
```

## 单文件调用

```python
from pkg import convert_ofd_to_pdf

ofd_file_path = r"D:\files\invoice.ofd"

result = convert_ofd_to_pdf(ofd_file_path)

print(result)
```

默认输出 PDF 路径为源 OFD 文件同目录、同文件名、扩展名 `.pdf`：

```text
D:\files\invoice.ofd -> D:\files\invoice.pdf
```

## 批量转换示例

```python
from pathlib import Path
from pkg import convert_ofd_to_pdf

ofd_dir = Path(r"D:\files")

for ofd_file in ofd_dir.glob("*.ofd"):
    result = convert_ofd_to_pdf(str(ofd_file))
    print(result)
```

## 返回结构

`convert_ofd_to_pdf()` 始终返回统一的 `dict`：

```python
{
    "success": bool,
    "status": "success" | "error",
    "code": "OK" | "FILE_NOT_FOUND" | "INVALID_SOURCE" | "PROCESS_FAILED" | "TIMEOUT" | "INVALID_CHILD_OUTPUT" | "UNEXPECTED_ERROR",
    "message": str,
    "source_path": str,
    "output_path": str | None,
    "pages": int | None,
    "elapsed": float | None,
    "returncode": int | None,
    "stdout": str,
    "stderr": str,
}
```

常见错误码：

| code                   | 含义                                 |
| ---------------------- | ------------------------------------ |
| `OK`                   | 转换成功                             |
| `FILE_NOT_FOUND`       | 源 OFD 文件不存在                    |
| `INVALID_SOURCE`       | 路径为空、不是文件或不是 `.ofd` 文件 |
| `PROCESS_FAILED`       | 子进程启动或转换失败                 |
| `TIMEOUT`              | 子进程转换超时                       |
| `INVALID_CHILD_OUTPUT` | 子进程没有返回合法 JSON              |
| `UNEXPECTED_ERROR`     | 未预期异常                           |

## 子进程调用说明

`pkg.converter.convert_ofd_to_pdf()` 内部调用：

```python
subprocess.run(
    [sys.executable, "-m", "pkg.cli", ofd_file_path],
    capture_output=True,
    text=True,
    check=True,
    timeout=300,
)
```

说明：

- 使用当前 Python 解释器 `sys.executable`。
- 调用的是 Python 模块 `pkg.cli`，不是外部 exe。
- 子进程内部使用 `pkg.core.converter.OFDConverter`。
- stdout 只输出 JSON，便于父进程解析。

## 命令行调用

在目标项目根目录下运行：

```bash
python -m pkg path\to\input.ofd
```

或：

```bash
python -m pkg.cli path\to\input.ofd
```

命令执行后会在 stdout 输出 JSON 结果。成功时进程退出码为 `0`，失败时为非 `0`。

## 注意事项

- 目标项目运行时必须能 import 到 `pkg`。
- `core/` 是核心代码。
- 输出 PDF 如果已经存在，底层转换逻辑会尝试覆盖；如果目标文件被占用，可能生成临时输出路径。
- Windows 环境建议安装中文字体，否则部分 OFD 字体可能使用 fall
