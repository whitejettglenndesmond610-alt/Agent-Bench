from agent_eval.db.enums import AuthType


def json_request_headers(
    *,
    auth_type: AuthType,
    token: str | None,
    extra_headers: dict[str, str] | None = None,
    api_key_header: str | None = None,
) -> dict[str, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if auth_type == AuthType.BEARER and token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if api_key_header and token is not None:
        headers[api_key_header] = token
    return headers
