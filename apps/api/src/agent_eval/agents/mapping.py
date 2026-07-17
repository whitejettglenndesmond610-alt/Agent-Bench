import ipaddress
import json
import re
from typing import Any, cast
from urllib.parse import urlsplit

from jsonpath_ng.ext import parse
from jsonpath_ng.jsonpath import JSONPath

PROMPT_VARIABLE = "{{prompt}}"
VARIABLE_PATTERN = re.compile(r"{{[^{}]+}}")


class TemplateValidationError(ValueError):
    pass


class ResponseMappingError(ValueError):
    pass


def parse_request_template(template: str) -> dict[str, Any]:
    variables = VARIABLE_PATTERN.findall(template)
    if PROMPT_VARIABLE not in variables:
        raise TemplateValidationError("request template must contain {{prompt}}")
    if any(variable != PROMPT_VARIABLE for variable in variables):
        raise TemplateValidationError("request template contains an unsupported variable")
    if "{{" in template.replace(PROMPT_VARIABLE, "") or "}}" in template.replace(
        PROMPT_VARIABLE, ""
    ):
        raise TemplateValidationError("request template contains malformed variable syntax")

    try:
        value = json.loads(template)
    except json.JSONDecodeError as error:
        raise TemplateValidationError("request template must be valid JSON") from error
    if not isinstance(value, dict):
        raise TemplateValidationError("request template root must be a JSON object")
    _reject_variable_keys(value)
    return cast(dict[str, Any], value)


def _reject_variable_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if PROMPT_VARIABLE in str(key):
                raise TemplateValidationError("{{prompt}} cannot be used in a JSON key")
            _reject_variable_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_variable_keys(child)


def render_request_template(template: str, prompt: str) -> dict[str, Any]:
    return cast(dict[str, Any], _replace_prompt(parse_request_template(template), prompt))


def _replace_prompt(value: Any, prompt: str) -> Any:
    if isinstance(value, str):
        return value.replace(PROMPT_VARIABLE, prompt)
    if isinstance(value, list):
        return [_replace_prompt(item, prompt) for item in value]
    if isinstance(value, dict):
        return {key: _replace_prompt(item, prompt) for key, item in value.items()}
    return value


def compile_jsonpath(expression: str) -> JSONPath:
    try:
        return parse(expression)
    except Exception as error:
        raise ValueError("invalid JSONPath expression") from error


def extract_answer(payload: Any, expression: str) -> str:
    matches = compile_jsonpath(expression).find(payload)
    if len(matches) != 1:
        raise ResponseMappingError("answer JSONPath must match exactly one value")
    answer = matches[0].value
    if isinstance(answer, bool):
        return "true" if answer else "false"
    if not isinstance(answer, (str, int, float)):
        raise ResponseMappingError("answer JSONPath must resolve to a scalar value")
    result = str(answer).strip()
    if not result:
        raise ResponseMappingError("answer is empty")
    return result


def extract_citations(
    payload: Any, expression: str | None, *, answer: str | None = None
) -> list[str]:
    matches = compile_jsonpath(expression).find(payload) if expression is not None else []
    values: list[Any]
    if len(matches) == 1 and isinstance(matches[0].value, list):
        values = matches[0].value
    else:
        values = [match.value for match in matches]

    citations: list[str] = []
    for value in values:
        if not isinstance(value, str) or not is_http_url(value):
            continue
        if value not in citations:
            citations.append(value)
        if len(citations) == 3:
            return citations
    return citations or extract_inline_citations(answer or "")


def extract_inline_citations(answer: str) -> list[str]:
    citations: list[str] = []
    for match in re.finditer(r"https?://[^\s<>\"']+", answer):
        value = _trim_url_punctuation(match.group(0))
        if is_http_url(value) and value not in citations:
            citations.append(value)
        if len(citations) == 3:
            break
    return citations


def _trim_url_punctuation(value: str) -> str:
    value = value.rstrip(".,;:!?")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while value.endswith(closing) and value.count(closing) > value.count(opening):
            value = value[:-1]
    return value


def is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    hostname = parts.hostname
    if (
        parts.scheme not in {"http", "https"}
        or not hostname
        or any(character.isspace() for character in value)
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_hostname.rstrip(".").split(".")
    return bool(labels) and all(
        label
        and len(label) <= 63
        and not label.startswith("-")
        and not label.endswith("-")
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )
