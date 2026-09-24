from typing import Annotated

import pydantic
import pydantic_settings


class Settings(pydantic_settings.BaseSettings):
    model_config = pydantic_settings.SettingsConfigDict(
        frozen=True,
        str_strip_whitespace=True,
        hide_input_in_errors=True,
    )

    discord_bot_token: Annotated[pydantic.SecretStr, pydantic.Field(min_length=1)]
    discord_owner_id: Annotated[pydantic.PositiveInt, pydantic.Field(lt=2**64)]
    codex_model: Annotated[str, pydantic.StringConstraints(min_length=1)] = (
        "gpt-5.6-luna"
    )
