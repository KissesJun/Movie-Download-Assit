"""跨站磁力链接提取：先解码候选，再校验哈希，最后按种子身份去重。"""

import base64
import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

MAGNET_RE = re.compile(r"magnet:\?[^\s<>\"'`\\\[\]{}，。；、）]+", re.I)
BTIH_RE = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})\Z", re.I)
BTMH_RE = re.compile(r"urn:btmh:(1220[0-9a-f]{64})\Z", re.I)
# 裸哈希只在明确标记或 .torrent 文件名中识别，避免误收普通摘要。
HASH_CONTEXT_RE = re.compile(
    r"(?:info[_ -]?hash|btih|磁力哈希|种子哈希|特征码)\s*[\"']?\s*[:：=]\s*[\"']?"
    r"([0-9a-f]{40}|[a-z2-7]{32})(?![a-z0-9])"
    r"|(?<![a-z0-9])([0-9a-f]{40})(?=\.torrent(?:\b|\?))", re.I
)


@dataclass(frozen=True)
class MagnetLink:
    magnet: str
    info_hash: str
    title: str = ""


def normalize_btih(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[a-zA-Z2-7]{32}", value):
        return base64.b32decode(value.upper()).hex()
    raise ValueError("BTIH 必须是 40 位十六进制或 32 位 Base32")


def normalize_magnet(value: str) -> MagnetLink:
    value = html.unescape(value.strip())
    parts = urlsplit(value)
    if parts.scheme.lower() != "magnet" or parts.path or parts.netloc:
        raise ValueError("无效磁力链接")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    normalized = []
    v1 = v2 = title = ""
    for key, val in pairs:
        if key.lower() == "xt":
            if match := BTIH_RE.fullmatch(val):
                identity = normalize_btih(match[1])
                if v1 and v1 != identity:
                    raise ValueError("磁力链接包含冲突的 BTIH")
                v1 = identity
                val = "urn:btih:" + v1
            elif match := BTMH_RE.fullmatch(val):
                identity = match[1].lower()
                if v2 and v2 != identity:
                    raise ValueError("磁力链接包含冲突的 BTMH")
                v2 = identity
                val = "urn:btmh:" + v2
            else:
                raise ValueError("不支持或无效的 xt 哈希")
            key = "xt"
        if key.lower() == "dn" and not title:
            title = val
        normalized.append((key, val))
    if not (v1 or v2):
        raise ValueError("磁力链接缺少有效的 BTIH/BTMH")
    return MagnetLink("magnet:?" + urlencode(normalized), v1 or "btmh:" + v2, title)


def _decode_js(text: str) -> str:
    # 只解码相关 JS 字符转义，不对中文文本使用 unicode_escape。
    text = re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})",
                  lambda m: chr(int(m[1] or m[2], 16)), text)
    return text.replace(r"\/", "/")


def extract_magnets(text: str, *, allow_hashes: bool = False) -> list[MagnetLink]:
    found: dict[str, MagnetLink] = {}
    current = text
    # 有界解码，覆盖复制文本、HTML、JSON、跳转 URL 和双重 URL 编码。
    for _ in range(4):
        current = html.unescape(_decode_js(current))
        for match in MAGNET_RE.finditer(current):
            candidate = match[0]
            # href="/redirect?url=magnet%3A...&other=..." 的嵌套参数单独解析。
            try:
                link = normalize_magnet(candidate)
            except ValueError:
                continue
            found.setdefault(link.info_hash, link)
        if allow_hashes:
            for match in HASH_CONTEXT_RE.finditer(current):
                info_hash = normalize_btih(match[1] or match[2])
                found.setdefault(info_hash, MagnetLink("magnet:?xt=urn:btih:" + info_hash, info_hash))
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return list(found.values())
