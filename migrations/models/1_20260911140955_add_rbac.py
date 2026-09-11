from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "roles" (
    "id" VARCHAR(40) NOT NULL PRIMARY KEY,
    "name" VARCHAR(80) NOT NULL,
    "landing" VARCHAR(120) NOT NULL
);
        CREATE TABLE IF NOT EXISTS "role_default_permissions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "resource" VARCHAR(120) NOT NULL,
    "can_read" INT NOT NULL,
    "can_write" INT NOT NULL,
    "can_execute" INT NOT NULL,
    "role_id" VARCHAR(40) NOT NULL REFERENCES "roles" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_role_defaul_role_id_b36628" UNIQUE ("role_id", "resource")
) /* Seed-time template only — copied onto a user at creation, never read at request time. */;
        CREATE TABLE IF NOT EXISTS "users" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "name" VARCHAR(120) NOT NULL,
    "email" VARCHAR(180) NOT NULL UNIQUE,
    "password_hash" VARCHAR(255) NOT NULL,
    "active" INT NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "role_id" VARCHAR(40) NOT NULL REFERENCES "roles" ("id") ON DELETE CASCADE
);
        CREATE TABLE IF NOT EXISTS "user_permissions" (
    "id" CHAR(36) NOT NULL PRIMARY KEY,
    "resource" VARCHAR(120) NOT NULL,
    "can_read" INT NOT NULL,
    "can_write" INT NOT NULL,
    "can_execute" INT NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    "granted_by_id" CHAR(36) REFERENCES "users" ("id") ON DELETE CASCADE,
    "user_id" CHAR(36) NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_user_permis_user_id_3426da" UNIQUE ("user_id", "resource")
) /* The only table any authorization check reads (contracts.md §2.3) — per-user, not per-role. */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "user_permissions";
        DROP TABLE IF EXISTS "roles";
        DROP TABLE IF EXISTS "users";
        DROP TABLE IF EXISTS "role_default_permissions";"""


MODELS_STATE = (
    "eJztm1tP4zgUgP+KlSdGKlUbyoCq1UoFiqY7Ax1B2V3NRZGbHNqIxA62A3TY/veV3dyblA"
    "Zatu3mBeixj+18PrHPpTxrLrXA4fUr6oDWRs8awa78IyWvIQ17XiyVAoGHSkNj1AElwUMu"
    "GDaF1ka32OFQQ5oF3GS2J2xKtDYivuNIITW5YDYZxSKf2Pc+GIKOQIyBaW30/WcNaTax4A"
    "l4+NG7M25tcKzUOm1Lzq3khph4SnY6xuxc9ZTTDQ2TOr5L4t7eRIwpibpzwaR0BAQYFmAl"
    "HkCuL3jQUDRbq9ZGgvkQLdKKBRbcYt8RiQceGrFMM4zL/sC47g4MQyuByKRE4rWJkDyeNR"
    "c/GQ6QkRhrbdRqTGfzxBxmveSEf3auTj91rvZajQ9yQsqwOdu5y6BFV01TNQQWeDaIoh5j"
    "Vr9LgA77rwZ1KIhZxxb27rCPl4F9XAz7OIAdw3UwseT8JfgmVHYQcVNfhnFTL4as2qZTeW"
    "zcBsdGdI4MsXn3iJllpFri7QgezvCAuTbnNiV8fmtOgkHOP1+Bg9Xjzu9F4hA9mw36NRpz"
    "Czcp2JNYGhx6KWP2ObA38rrhwHYJjzQ6qtMiM5xvcnU3K8EEj9QjybnlTAttq+AmzzXCxV"
    "e7UfA2vHjba9cA1r6wXUACXM/BAhAlzgT98PVGs4VM6tlgIUoERRhJq0FYIJOBMo4aIvAA"
    "DDHAlpQzuPeBCyTHq2uZ7VjzVDneyXfFRjYy4NRnJmg/3+Kx3Nz0zkp4LL5vW3Wpsx6/Rf"
    "vt1iemZIPUTPJH63dtva9N3kmvjvmDj+qU9ygXI6YaFa4XHJZoX0pcqkmd6lYtvlWTx72J"
    "iSHfnJwTn1IHMMlHnVTLoB5S6qyLdSR5Xzs+6fe/yJFdzu8dJegNMoxvLk66V3tNhZ7fO7"
    "ZQ4t7lIIf3I5PN5YFHehXxksThCUz/VcwTmhX1EtSV71EuuE+o7ODpvZoYfy4gShOfx31O"
    "Gdgj8hkmCnqPcIFJ7hWZyRjtiPMuXTz8GLlxSSOjxLDAgZkFn3auTztnXW1aHGGuMwxQIV"
    "OO1x+GUsVOfhSvbU7+rvKGV+0NV+m79XrB4GLbKQM4Uti5XHRzqfxoc0GCVLWl+XqY80fK"
    "LGOM+bgM5znFHbRo/fBwCeL64WEhcdWWJo5NYT+UdXhjpXf0dUNT31pXVyWiwDKwmMd9hg"
    "XIRFRBgJHSzDC3AtV6+Mc2GrrMEfSJM4kzzUUbMuhddK8HnYuvqV056wy6skVX0klGuje7"
    "TOM9iwZBf/UGn5D8iL71L7vZKzfqN/imyTVhX1CD0EcDW4nTN5SG1KoApwpwdjnAiVGPGC"
    "byYFpZCU3GMaVqZ8GjbTr9tJfzn9HaFmNdayktQ60gml62eCbj6vJFs8E4KF2pcRAmE4R9"
    "MabM/qX2HJljMO9UrYqjPZMSNR6vuxb64Tca+EivH3wI614esH25jBoiVKhP8uWeL5+916"
    "S5hTTZtyqkbW7qoCqkVYW0XSwwVIW0qpD2f6Due9YrswtpzSq7sCnZhZBRIr0wF0qEAdhw"
    "YpRzk+YU3+AxbWYM9ir/KP39xpJMEyqr9D+3FueCHE0YDbwxR7Nj3yDN5mgSBpWfo8k/Ct"
    "4P7Ga++S9ynTv8NqnE3wFmm+O8tETQsjAdgeM+G1Pn7xFRIlaX25axvGAPN7cyOpJL2Neb"
    "raPW8cHH1nENaWqZkeRowSka+nLFsfkDsDARtWxonlDZwch8PaVQzytDOOi+g3SbjaXyHo"
    "0FeQ/ZlokKKREwe7XThP+47l8WhIOxSjYqsU2B/kGOzcUW0p4Ww5UwUqFHyHTvovN3Fvfp"
    "l/5J1vWSA5yU+4+o1V9m038BB8HZpg=="
)
