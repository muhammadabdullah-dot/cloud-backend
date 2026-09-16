from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "D.Marina Cloud Server"
    port: int = 4175
    db_url: str = "sqlite://./cloud.db"
    jwt_secret: str = "change-me-cloud"
    # "*" is fine pre-launch (no cookies are used — auth is a Bearer header, so wildcard + no
    # credentials is safe per the CORS spec). Revisit before any real deployment: set this to
    # the actual frontend origin(s) via .env instead of leaving it wide open.
    cors_origins: str = "*"
    # Item pictures and attachments. Served only through authenticated endpoints, never as a static folder.
    media_dir: str = "./media"

    # Where the server's log goes (server.log, rotated). Blank = a "logs" folder beside the database.
    log_dir: str = ""
    # The built app this server hands out on its own port. Blank = the app's dist/ folder in this
    # repo (cloud-app/dist); a path = that folder; "off" = API only.
    frontend_dir: str = ""


settings = Settings()

TORTOISE_ORM = {
    "connections": {"default": settings.db_url},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}
