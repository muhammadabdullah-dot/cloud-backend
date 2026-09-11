from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "aerich" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSON NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "eJzdlF1r2zAUhv+K0FUL2Ui8ZC2+S8PGNrYE2jAGowhZVhxRWXKl460l838fR04ix2nKej"
    "PYLv2e93w9+GhDS5tL7V9PpVNiTVOyoYaXkqakFxkQyqsq6igAz3Sw8ujJPDgugKZkxbWX"
    "A0Jz6YVTFShraEpMrTWKVnhwyhRRqo26ryUDW0hYS0dT8v12QKgyuXyQfvdZ3bGVkjo/GF"
    "Xl2DvoDB6roH008D4YsVvGhNV1aaK5eoS1NXu3MoBqIY10HCSWB1fj+Djdds/dRu2k0dKO"
    "2MnJ5YrXGjrrZixqlLH5Yslu3i0Zoy8AJKxBuMoA0tjQAkd4lYzGF+PLN2/HlwNCw5h75a"
    "JpW0cwbWLAM1/SJsQ58NYRGEeoP6TzONIR2dmau6fRdlJ6fD24Pt8dzecA74RIOP5VfwNx"
    "yR+YlqYAPI1kMnkG6Nfp9ezD9PosmUzOsaV1XLTXMd+GkjaG1CNlPKoXEN7a/0O6o+HwD+"
    "iOhsOTdEPskK6wBmR72oeEP90s5k8T7qT0KOdKAPlFtPLwD9JuTsNFGFi59P5ed5mefZl+"
    "6+OefV5cBTjWQ+FClVDgijYNPtCr7QO9f7EzLu5+cpezo4hN7CnvcahMyr7CDS8CSNy4aX"
    "4DG78okA=="
)
