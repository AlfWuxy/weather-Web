"""有界解码官方预警响应；可作为独立标准库源码在隔离进程中执行。"""

import zlib


_BODY_ERROR = "官方预警响应编码无效或内容超过限制"


def decode_alert_body(wire_bytes, content_encoding, *, max_bytes=1_000_000) -> str:
    """接受 identity 或单个完整 gzip 成员，返回严格 UTF-8 文本。

    压缩前后均使用同一字节上限；拒绝拼接成员和尾随数据，避免不同读取方
    对响应边界作不同解释。调用方缺少 Content-Encoding 时应传 identity。
    返回文本的哈希代表解压后原始 UTF-8 内容，不是压缩网络字节的哈希。
    """
    if (type(max_bytes) is not int or max_bytes <= 0
            or not isinstance(wire_bytes, bytes) or len(wire_bytes) > max_bytes
            or not isinstance(content_encoding, str)
            or any(ord(char) < 32 and char != "\t" or ord(char) >= 127
                   for char in content_encoding)):
        raise ValueError(_BODY_ERROR)
    encoding = content_encoding.strip(" \t").lower()
    if encoding not in {"identity", "gzip"}:
        raise ValueError(_BODY_ERROR)
    try:
        decoded = wire_bytes
        if encoding == "gzip":
            # 最多产生上限加一字节；不调用无界 decompress 或 flush。
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(wire_bytes, max_bytes + 1)
            # eof 只有在完整 gzip 尾部通过 CRC 和长度核验后才成立。
            if (len(decoded) > max_bytes or not decoder.eof
                    or decoder.unused_data or decoder.unconsumed_tail):
                raise ValueError(_BODY_ERROR)
        return decoded.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError, zlib.error, OverflowError):
        # 不将供应商内容或底层异常原文带入调用方提示。
        raise ValueError(_BODY_ERROR) from None
