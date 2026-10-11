from __future__ import annotations

import hashlib
import os
import shutil
import struct
import zlib
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           IndirectObject, NameObject, NumberObject, StreamObject)


@dataclass(frozen=True)
class Preset:
    label: str
    max_edge: int | None
    jpeg_quality: int | None


PRESETS = {
    "high": Preset("画质优先", None, None),
    "mid": Preset("均衡压缩", 2400, 85),
    "low": Preset("体积优先", 1600, 72),
}


class CompressionError(Exception):
    """An input cannot be safely processed."""


@dataclass(frozen=True)
class Result:
    source: Path
    output: Path
    preset: str
    before_bytes: int
    after_bytes: int
    pages: int
    images_reencoded: int
    skipped_images: int

    def as_dict(self) -> dict:
        return {**self.__dict__, "source": str(self.source), "output": str(self.output)}


def _remove_unreachable(writer: PdfWriter) -> None:
    """Trace the live graph once, including cycles, without decoding streams."""
    stack = [writer.root_object.indirect_reference, writer._info_obj, writer._ID]
    live: set[int] = set()
    direct_seen: set[int] = set()
    while stack:
        obj = stack.pop()
        if isinstance(obj, IndirectObject):
            if obj.pdf is not writer or obj.idnum in live:
                continue
            live.add(obj.idnum)
            stack.append(obj.get_object())
        elif isinstance(obj, (DictionaryObject, ArrayObject)):
            if id(obj) in direct_seen:
                continue
            direct_seen.add(id(obj))
            stack.extend(obj.values() if isinstance(obj, DictionaryObject) else obj)
    for index in range(len(writer._objects)):
        if index + 1 not in live:
            writer._objects[index] = None


def _merge_identical_encoded_objects(writer: PdfWriter) -> None:
    # Compare encoded streams plus their full dictionaries. This is conservative
    # (different encodings stay separate), but never decodes pixels just to hash.
    hashes = {}
    replacements = {}
    protected = {writer.root_object.indirect_reference.idnum}
    if isinstance(writer._info_obj, IndirectObject):
        protected.add(writer._info_obj.idnum)
    for index, obj in enumerate(writer._objects):
        if obj is None or index + 1 in protected:
            continue
        digest = hashlib.sha256(type(obj).__name__.encode())
        digest.update(DictionaryObject.hash_value_data(obj) if isinstance(obj, DictionaryObject)
                      else obj.hash_value_data())
        if isinstance(obj, StreamObject):
            digest.update(obj._data)
        key = digest.digest()
        if key in hashes:
            replacements[index + 1] = hashes[key]
            writer._objects[index] = None
        else:
            hashes[key] = obj.indirect_reference
    stack = [obj for obj in writer._objects if isinstance(obj, (DictionaryObject, ArrayObject))]
    visited = set()
    while stack:
        obj = stack.pop()
        if id(obj) in visited:
            continue
        visited.add(id(obj))
        for key, value in (list(obj.items()) if isinstance(obj, DictionaryObject) else enumerate(obj)):
            if isinstance(value, IndirectObject) and value.pdf is writer:
                if value.idnum in replacements:
                    obj[key] = replacements[value.idnum]
            elif isinstance(value, (DictionaryObject, ArrayObject)):
                stack.append(value)


def _clean_private_data(writer: PdfWriter) -> None:
    # Illustrator editing payloads are not needed to render the PDF. Preserve
    # attachments, annotations, document metadata and every visible resource.
    for obj in writer._objects:
        if isinstance(obj, DictionaryObject):
            obj.pop(NameObject("/PieceInfo"), None)
            for key in list(obj):
                if str(key).startswith("/AIPDFPrivateData"):
                    del obj[key]
    # Drop unreachable editing payloads before deduplication hashes streams:
    # hashing decodes them, which can be expensive for PNG prediction filters.
    _remove_unreachable(writer)
    while True:
        before = sum(obj is not None for obj in writer._objects)
        _merge_identical_encoded_objects(writer)
        _remove_unreachable(writer)
        if sum(obj is not None for obj in writer._objects) == before:
            break
    for index, obj in enumerate(writer._objects):
        if hasattr(obj, "_data") and not obj.get("/Filter"):
            compressed = obj.flate_encode(level=9)
            if len(compressed._data) < len(obj._data):
                compressed.indirect_reference = obj.indirect_reference
                writer._objects[index] = compressed


def _native_png_image(obj: DictionaryObject):
    """Decode compatible PNG-predicted streams in Pillow's native PNG codec."""
    if obj.get("/Filter") != "/FlateDecode" or "/Decode" in obj or "/Mask" in obj:
        return None
    params = obj.get("/DecodeParms")
    if isinstance(params, IndirectObject):
        params = params.get_object()
    if not isinstance(params, DictionaryObject) or not 10 <= params.get("/Predictor", 1) <= 15:
        return None
    space = obj.get("/ColorSpace")
    channels = {"/DeviceRGB": 3, "/DeviceGray": 1}.get(space)
    width, height = int(obj["/Width"]), int(obj["/Height"])
    if (channels is None or obj.get("/BitsPerComponent") != 8
            or params.get("/BitsPerComponent", 8) != 8
            or params.get("/Columns", 1) != width or params.get("/Colors", 1) != channels):
        return None
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2 if channels == 3 else 0, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", obj._data) + chunk(b"IEND", b"")
    image = Image.open(BytesIO(png))
    image.load()
    if "/SMask" in obj:
        mask = _native_image(obj["/SMask"])
        if mask is None or mask.mode != "L" or mask.size != image.size:
            return None
        image = image.convert("RGBA")
        image.putalpha(mask)
    return image


def _native_image(obj: DictionaryObject):
    """Avoid intermediate PNG/JPEG encoding for compatible image streams."""
    if "/Decode" in obj or "/Mask" in obj:
        return None
    space = obj.get("/ColorSpace")
    if space not in ("/DeviceRGB", "/DeviceGray") or obj.get("/BitsPerComponent") != 8:
        return None
    size = (int(obj["/Width"]), int(obj["/Height"]))
    mode = "RGB" if space == "/DeviceRGB" else "L"
    image = None
    if obj.get("/Filter") == "/FlateDecode":
        params = obj.get("/DecodeParms", {})
        if isinstance(params, IndirectObject):
            params = params.get_object()
        if isinstance(params, (dict, DictionaryObject)) and params.get("/Predictor", 1) == 1:
            data = zlib.decompress(obj._data)
            if len(data) != size[0] * size[1] * (3 if mode == "RGB" else 1):
                return None
            image = Image.frombytes(mode, size, data)
    elif obj.get("/Filter") == "/DCTDecode" and not obj.get("/DecodeParms"):
        image = Image.open(BytesIO(obj._data))
        image.load()
        if image.mode != mode or image.size != size:
            return None
    if image is None:
        return _native_png_image(obj)
    if "/SMask" in obj:
        mask = _native_image(obj["/SMask"])
        if mask is None or mask.mode != "L" or mask.size != size:
            return None
        image = image.convert("RGBA")
        image.putalpha(mask)
    return image


def _compress_images(writer: PdfWriter, preset: Preset) -> tuple[int, int]:
    if preset.max_edge is None:
        return 0, 0
    seen: set[int] = set()
    count = skipped = 0
    for page in writer.pages:
        # A damaged/unsupported image may fail when pypdf enumerates it. Leave
        # that resource untouched; do not risk changing the document to fix it.
        try:
            keys = list(page.images.keys())
        except Exception:
            skipped += 1
            continue
        for key in keys:
            try:
                path = [key] if isinstance(key, str) else list(key)
                if path[-1].startswith("~"):
                    continue  # Inline images have no replaceable indirect object.
                resource = page
                for name in path:
                    resource = resource["/Resources"]["/XObject"][name]
                obj = resource
                ref = obj.indirect_reference
                if ref is None or ref.idnum in seen:
                    continue
                seen.add(ref.idnum)
                if int(obj.get("/Width", 0)) * int(obj.get("/Height", 0)) < 100_000:
                    continue
                if "/Mask" in obj:
                    skipped += 1
                    continue
                try:
                    original = _native_image(obj)
                except Exception:
                    original = None
                if original is None:
                    original = page.images[key].image
                rgb = original.convert("RGB")
                rgb.thumbnail((preset.max_edge, preset.max_edge), Image.Resampling.LANCZOS)
                encoded = BytesIO()
                rgb.save(encoded, format="JPEG", quality=preset.jpeg_quality,
                         subsampling=0, optimize=True)
                data = encoded.getvalue()
                if len(data) >= len(obj._data):
                    continue
                mask_ref = None
                if "/SMask" in obj:
                    if original.mode != "RGBA":
                        skipped += 1
                        continue
                    alpha = original.getchannel("A").resize(rgb.size, Image.Resampling.LANCZOS)
                    mask = DecodedStreamObject()
                    mask.set_data(alpha.tobytes())
                    mask.update({NameObject("/Type"): NameObject("/XObject"),
                                 NameObject("/Subtype"): NameObject("/Image"),
                                 NameObject("/Width"): NumberObject(rgb.width),
                                 NameObject("/Height"): NumberObject(rgb.height),
                                 NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                                 NameObject("/BitsPerComponent"): NumberObject(8)})
                    # New mask object: a shared original mask must never be
                    # resized in place, since other images may still use it.
                    mask_ref = writer._add_object(mask.flate_encode(level=9))
                obj._data = data
                obj.decoded_self = None
                obj.update({NameObject("/Filter"): NameObject("/DCTDecode"),
                            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                            NameObject("/BitsPerComponent"): NumberObject(8),
                            NameObject("/Width"): NumberObject(rgb.width),
                            NameObject("/Height"): NumberObject(rgb.height)})
                for field in ("/DecodeParms", "/Decode"):
                    obj.pop(NameObject(field), None)
                if mask_ref is not None:
                    obj[NameObject("/SMask")] = mask_ref
                count += 1
            except Exception:
                skipped += 1
    return count, skipped


def _publish(temp: Path, source: Path, preset: str) -> Path:
    # Reserve the final name with O_EXCL so concurrent invocations cannot
    # overwrite each other, existing files or symlinks.
    index = 1
    while True:
        suffix = "" if index == 1 else f"_{index}"
        output = source.with_name(f"{source.stem}_{preset}{suffix}.pdf")
        try:
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            index += 1
            continue
        os.close(fd)
        try:
            os.replace(temp, output)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        return output


def compress(source: str | Path, preset: str = "high") -> Result:
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset: {preset}")
    source = Path(source).expanduser().absolute()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise CompressionError("请选择现有的 PDF 文件。")
    temporary: Path | None = None
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise CompressionError("暂不处理加密 PDF，请先保存未加密副本。")
        fields = reader.get_fields() or {}
        if any(field.get("/FT") == "/Sig" and field.get("/V") for field in fields.values()):
            raise CompressionError("此 PDF 带有数字签名，重新保存会使签名失效，已跳过。")
        if not len(reader.pages):
            raise CompressionError("PDF 没有页面。")
        before = source.stat().st_size
        writer = PdfWriter(clone_from=reader)
        changed, skipped = _compress_images(writer, PRESETS[preset])
        _clean_private_data(writer)
        fd, path = tempfile.mkstemp(prefix=".pdfcompress-", suffix=".tmp", dir=source.parent)
        os.close(fd)
        temporary = Path(path)
        writer.write(temporary)
        check = PdfReader(temporary)
        if len(check.pages) != len(reader.pages):
            raise CompressionError("输出页面校验失败，未保存副本。")
        # Already optimized inputs may grow. Save an unchanged copy instead.
        if temporary.stat().st_size >= before:
            shutil.copyfile(source, temporary)
            changed = 0
        after = temporary.stat().st_size
        output = _publish(temporary, source, preset)
        temporary = None
        return Result(source, output, preset, before, after, len(reader.pages), changed, skipped)
    except CompressionError:
        raise
    except Exception as exc:
        raise CompressionError(f"无法压缩此 PDF：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
