import io
import os
import re
import shutil
import secrets
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import zipfile
import tarfile

# 你自己的异常（这里假设已经在你项目里定义好了）
# from your_project.errors import ValidationAbort


def _gen_file_net_id(length: int = 25) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _now_ts_ms_str() -> str:
    # 纯数字，避免路径脏字符
    return str(int(time.time() * 1000))


def _is_supported_archive(filename: str) -> str:
    """
    返回: 'zip' 或 'tar'
    支持:
      - .zip
      - .tar
      - .tar.gz / .tgz
    """
    name = (filename or "").lower()
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".tar") or name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar"
    return ""


def _extract_in_memory(file_bytes: bytes, kind: str) -> List[Tuple[str, bytes]]:
    """
    解压到内存，返回 [(basename, content_bytes), ...]
    - basename: 仅保留文件名（不含目录）
    - 自动跳过目录项
    """
    out: List[Tuple[str, bytes]] = []
    bio = io.BytesIO(file_bytes)

    if kind == "zip":
        with zipfile.ZipFile(bio, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # 只取最后的文件名
                base = Path(info.filename).name
                if not base:
                    continue
                with zf.open(info, "r") as f:
                    out.append((base, f.read()))
        return out

    if kind == "tar":
        # tarfile 可以自动识别 tar / tar.gz / tgz
        bio.seek(0)
        with tarfile.open(fileobj=bio, mode="r:*") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                base = Path(m.name).name
                if not base:
                    continue
                f = tf.extractfile(m)
                if f is None:
                    continue
                out.append((base, f.read()))
        return out

    return out


def _letters_prefix(filename: str) -> str:
    """
    从“英文字母+数字”的文件名（不含扩展名）里提取英文字母部分。
    例如:
      INV.pdf  -> INV
      BOL1.pdf -> BOL
      bol2.PDF -> BOL
    """
    stem = Path(filename).stem  # 去扩展名
    m = re.match(r"^([A-Za-z]+)", stem)
    if not m:
        return ""
    return m.group(1).upper()


def process_archive_upload(
    upload_file: Any,  # FastAPI UploadFile (starlette.datastructures.UploadFile)
    temp_root: Path,   # 预设好的 temp 文件夹路径（例如 Path("/tmp/myapp")）
) -> List[Dict[str, Any]]:
    """
    需求实现：
    1) 检测 zip / tar，否则 raise ValidationAbort(violations=[...])
    2) 内存解压，检查 lc 文件（大小写不敏感）：
       - 没找到 -> raise
       - 找到但不是 .txt -> raise
       - 除了 lc 没有其它文件 -> raise
    3) temp_root 下创建以毫秒时间戳命名的目录（存在则覆盖）
    4) 按文件名前缀英文字母建子目录并落盘（BOL1/BOL2 -> BOL）
    5) 生成 list[dict] 返回（LC 的 messageType=MT700，其它为空字符串）
    """
    filename = getattr(upload_file, "filename", None) or ""
    kind = _is_supported_archive(filename)
    if not kind:
        raise ValidationAbort(violations=["错误：不是支持的压缩文件格式。"])

    # 读入内存字节（UploadFile.read() 是 async；这里兼容 sync/async）
    read_attr = getattr(upload_file, "read", None)
    if read_attr is None:
        raise ValidationAbort(violations=["错误：未收到有效的文件对象。"])

    if callable(read_attr):
        data = read_attr()
        # 如果是协程（UploadFile.read 是 async），需要 await：你可在上层 async 函数里 await 再传 bytes
        if hasattr(data, "__await__"):
            raise RuntimeError(
                "upload_file.read() 是 async 协程；请在你的 FastAPI async endpoint 里先 `data = await file.read()`，"
                "然后把 bytes 传给一个接收 bytes 的版本，或把本函数改为 async 并在这里 await。"
            )
        file_bytes: bytes = data
    else:
        raise ValidationAbort(violations=["错误：未收到有效的文件对象。"])

    extracted = _extract_in_memory(file_bytes, kind)

    # 基础过滤：只保留“文件”，并丢弃空名
    extracted = [(name, b) for (name, b) in extracted if name and not name.endswith("/")]

    # 2) 检查 lc 文件（大小写不敏感，按“文件名（不含扩展名）== lc”）
    lc_candidates = []
    for name, b in extracted:
        p = Path(name)
        if p.stem.lower() == "lc":
            lc_candidates.append((name, b))

    if not lc_candidates:
        raise ValidationAbort(
            violations=["没有找到lc文件，请加入lc文件或者将lc文件命名为lc。txt"]
        )

    # 若存在多个 lc 文件，按需求这里视为不合规也可以；你没要求，我默认取第一个并继续校验
    lc_name, lc_bytes = lc_candidates[0]
    if Path(lc_name).suffix.lower() != ".txt":
        raise ValidationAbort(violations=["不支持的lc格式，lc仅支持txt"])

    # 除了 lc 没有其他文件
    non_lc_files = [(n, b) for (n, b) in extracted if Path(n).stem.lower() != "lc"]
    if len(non_lc_files) == 0:
        raise ValidationAbort(violations=["只检测到lc 没有其他presentation document"])

    # 3) 创建时间戳目录（存在则覆盖）
    ts_dir = temp_root / _now_ts_ms_str()
    if ts_dir.exists():
        shutil.rmtree(ts_dir, ignore_errors=True)
    ts_dir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []

    # 将 LC 也包含在落盘与返回中（通常需要）
    # 统一按 extracted 保存：包含 lc + 其它文件
    for name, content in extracted:
        # 4) 解析文件名（字母前缀）建子目录
        doc_code = _letters_prefix(name)
        # 如果文件名不符合“字母开头”，你可以选择跳过或报错；需求没说，我选择报错更安全
        if not doc_code:
            raise ValidationAbort(violations=[f"不支持的文件命名格式：{name}（需以英文字母开头）"])

        subdir = ts_dir / doc_code
        subdir.mkdir(parents=True, exist_ok=True)

        file_path = (subdir / name)
        # 覆盖写入
        file_path.write_bytes(content)

        ext = Path(name).suffix.lower().lstrip(".")  # 识别到的后缀（不带点）
        message_type = "MT700" if Path(name).stem.lower() == "lc" else ""

        results.append(
            {
                "documentCode": doc_code,                 # 必须大写
                "fileName": name,                         # 全名
                "fileNetID": _gen_file_net_id(25),        # 25位唯一字符串
                "messageType": message_type,              # LC=MT700 其它=""
                "filePath": file_path.resolve(),          # pathlib.Path 绝对路径
                "fileIdentifiedExt": ext,                 # 后缀名（如 pdf/txt）
            }
        )

    return results