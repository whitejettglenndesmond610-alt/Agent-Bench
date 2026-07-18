from cryptography.exceptions import InvalidTag

from agent_eval.agents.inference import InferenceConfig
from agent_eval.auth.encryption import TokenCipher
from agent_eval.db.enums import AuthType
from agent_eval.db.models import AgentVersion


class AgentConfigurationUnavailable(RuntimeError):
    pass


def inference_config_for_version(
    version: AgentVersion, token_cipher: TokenCipher | None
) -> InferenceConfig:
    token: str | None = None
    if version.auth_type == AuthType.BEARER:
        if version.encrypted_token is None or token_cipher is None:
            raise AgentConfigurationUnavailable("Agent token is unavailable")
        try:
            token = token_cipher.decrypt(version.encrypted_token)
        except (InvalidTag, UnicodeError, ValueError) as error:
            raise AgentConfigurationUnavailable("Agent token is unavailable") from error
    return InferenceConfig(
        endpoint_url=version.endpoint_url,
        auth_type=version.auth_type,
        token=token,
        request_template=version.request_template,
        answer_jsonpath=version.answer_jsonpath,
        citations_jsonpath=version.citations_jsonpath,
        timeout_seconds=version.timeout_seconds,
    )
