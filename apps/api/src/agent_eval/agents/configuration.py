from uuid import UUID

from cryptography.exceptions import InvalidTag

from agent_eval.agents.inference import InferenceConfig
from agent_eval.auth.encryption import TokenCipher
from agent_eval.db.enums import AuthType, PrivateAgentHealth
from agent_eval.db.models import AgentVersion


class AgentConfigurationUnavailable(RuntimeError):
    pass


def inference_config_for_version(
    version: AgentVersion,
    token_cipher: TokenCipher | None,
    owner_user_id: UUID | None,
) -> InferenceConfig:
    token: str | None = None
    if version.auth_type == AuthType.BEARER:
        if version.encrypted_token is None or token_cipher is None:
            raise AgentConfigurationUnavailable("Agent token is unavailable")
        try:
            if version.agent.owner_user_id is None:
                token = token_cipher.decrypt(version.encrypted_token)
            elif (
                owner_user_id == version.agent.owner_user_id
                and version.private_health_status == PrivateAgentHealth.HEALTHY
            ):
                token = token_cipher.decrypt_private(
                    version.encrypted_token,
                    version.agent.owner_user_id,
                    version.id,
                )
            else:
                raise AgentConfigurationUnavailable("Agent token is unavailable")
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
