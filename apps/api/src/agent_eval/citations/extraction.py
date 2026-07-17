import trafilatura


def extract_citation_text(content: bytes, *, max_bytes: int = 204_800) -> str | None:
    if not content or max_bytes <= 0:
        return None
    probe = content.lstrip()
    if probe.startswith(b"\xef\xbb\xbf"):
        probe = probe[3:].lstrip()
    if probe.startswith(b"<"):
        extracted = trafilatura.extract(
            content,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if extracted is None:
            return None
        text = extracted
    else:
        text = content.decode("utf-8", errors="replace")
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return None
    encoded = normalized.encode("utf-8")
    if len(encoded) <= max_bytes:
        return normalized
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip() or None
