"""Configurações da aplicação, lidas de variáveis de ambiente ou do arquivo .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    modelos: str = "nvidia/nemotron-3.5-lightning:free,z-ai/glm-5.2:free,openrouter/free"
    caminho_banco: str = "cinerocket.db"

    max_requisicoes_por_pergunta: int = 4
    max_linhas: int = 50
    timeout_sql_segundos: float = 5.0

    @property
    def lista_modelos(self) -> list[str]:
        return [m.strip() for m in self.modelos.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
