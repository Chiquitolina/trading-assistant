SECTOR_DEFINITIONS = {
    "AI": {
        "label": "Artificial Intelligence",
        "coingecko_category_ids": (
            "artificial-intelligence",
        ),
    },

    "DeFi": {
        "label": "Decentralized Finance",
        "coingecko_category_ids": (
            "decentralized-finance-defi",
        ),
    },

    "Gaming": {
        "label": "Gaming / GameFi",
        "coingecko_category_ids": (
            "gaming",
        ),
    },

    "Layer 1": {
        "label": "Layer 1",
        "coingecko_category_ids": (
            "layer-1",
        ),
    },

    "Layer 2": {
        "label": "Layer 2",
        "coingecko_category_ids": (
            "layer-2",
        ),
    },

    "Meme": {
        "label": "Meme",
        "coingecko_category_ids": (
            "meme-token",
        ),
    },

    "Payments": {
        "label": "Payments",
        "coingecko_category_ids": (
            "payment-solutions",
        ),
    },

    "Privacy": {
        "label": "Privacy",
        "coingecko_category_ids": (
            "privacy-coins",
        ),
    },

    "RWA": {
        "label": "Real World Assets",
        "coingecko_category_ids": (
            "real-world-assets-rwa",
        ),
    },

    "SocialFi": {
        "label": "SocialFi",
        "coingecko_category_ids": (
            "socialfi",
        ),
    },

    "Exchange": {
        "label": "Exchange Tokens",
        "coingecko_category_ids": (
            "centralized-exchange-token-cex",
            "decentralized-exchange",
        ),
    },

    "Liquid Staking": {
        "label": "Liquid Staking",
        "coingecko_category_ids": (
            "liquid-staking-tokens",
        ),
    },
}


# Un activo puede pertenecer a varios sectores.
# Esta prioridad se usa únicamente para elegir
# un primary_sector estable.
PRIMARY_SECTOR_PRIORITY = (
    "Meme",
    "AI",
    "Gaming",
    "RWA",
    "Privacy",
    "SocialFi",
    "Payments",
    "Liquid Staking",
    "Exchange",
    "DeFi",
    "Layer 2",
    "Layer 1",
)


DEFAULT_SECTOR = "Other"


# No publicaremos estadísticas sectoriales
# de grupos demasiado pequeños.
MIN_SECTOR_SYMBOLS = 3


def get_all_coingecko_category_ids():
    category_ids = []

    for definition in (
        SECTOR_DEFINITIONS.values()
    ):
        category_ids.extend(
            definition[
                "coingecko_category_ids"
            ]
        )

    return tuple(
        dict.fromkeys(category_ids)
    )


def get_sector_for_category(
    category_id,
):
    for sector, definition in (
        SECTOR_DEFINITIONS.items()
    ):
        if (
            category_id
            in definition[
                "coingecko_category_ids"
            ]
        ):
            return sector

    return None


def select_primary_sector(
    sectors,
):
    sectors = set(
        sectors or []
    )

    for sector in PRIMARY_SECTOR_PRIORITY:
        if sector in sectors:
            return sector

    return DEFAULT_SECTOR

BINANCE_BASE_SYMBOL_ALIASES = {
    "1000BONK": "BONK",
    "1000FLOKI": "FLOKI",
    "1000PEPE": "PEPE",
    "1000XEC": "XEC",
}


# Se completará después de revisar
# coincidencias ambiguas.
COINGECKO_ID_OVERRIDES = {
}