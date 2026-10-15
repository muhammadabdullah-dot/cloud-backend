from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "D.Marina Cloud Server"
    port: int = 4175
    # NO DEFAULT, AND DELIBERATELY NOT SQLITE.
    #
    # Head office runs on SQL Server. This said `sqlite://./cloud.db`, and `.env` said the same, so the ONLY reason
    # the server ran on SQL Server was that every start script overrode DB_URL at launch. Anything run without that
    # override -- a script, a check, a one-off at a prompt -- silently opened the retired `cloud.db`, which still
    # exists and still answers every query with figures that stopped moving when the migration finished.
    #
    # That is not hypothetical: on 13 October 2026 the first query of a profit-and-loss reconciliation went to it,
    # and was caught only because the SQL happened to use a placeholder SQLite does not take. Had it been portable,
    # a month of figures would have been reported off a stale database.
    #
    # A SQLite URL is still perfectly good and still accepted -- `ops/dev/start-headoffice.cmd` uses one, and so
    # does a check run against a copy. What is gone is the silent default. `scripts/print_db_url.py` builds the real
    # one out of the single file the password lives in, which is why no start script, document or log carries it.
    db_url: str = ""
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
    # The demo branch (Fort Colony) and demo godown (Items, suppliers, bins, stock, requisitions, transfers) a new
    # database gets on its first start. DEMO_DATA=off starts it with only the roles, the seeded sign-ins and what the
    # software needs to run (a fresh test, a real new install). Once a database has started without demo data it never
    # gets any, whatever this says later (seed_service.demo_data_wanted).
    demo_data: bool = True


settings = Settings()

if not settings.db_url:
    raise RuntimeError(
        "DB_URL is not set, and there is no default any more: there used to be one, and it pointed at the retired "
        "SQLite copy of head office.\n"
        "  To start the server:  fresh-test\\start-headoffice.cmd\n"
        "  For a script:         set DB_URL=<the output of scripts\\print_db_url.py>\n"
        "A sqlite:// URL is still accepted when you mean it, for dev or for a check against a copy."
    )

TORTOISE_ORM = {
    "connections": {"default": settings.db_url},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}
