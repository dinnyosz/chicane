"""Session alias generation with 1:1 Slack emoji mapping.

Replaces ``coolname`` with a custom generator.  Every noun, adjective,
and verb has an exact standard Slack emoji shortcode — no approximations,
no custom emoji needed.  Aliases are ``verb-adjective-noun`` triples
(e.g. ``dancing-cosmic-falcon``) and produce *two* randomly-chosen emoji
reactions (out of the three available) on the thread root.

~65 verbs × ~134 adjectives × ~221 nouns ≈ 1,900,000 unique combinations.
"""

from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# Nouns — every entry maps to an *exact* standard Slack emoji shortcode.
# Sourced from the iamcal/emoji-data set used by Slack.
# ---------------------------------------------------------------------------

NOUNS: dict[str, str] = {
    # -- Animals --
    "ant": "ant",
    "badger": "badger",
    "bat": "bat",
    "bear": "bear",
    "beaver": "beaver",
    "bee": "honeybee",
    "beetle": "beetle",
    "bird": "bird",
    "bison": "bison",
    "boar": "boar",
    "bug": "bug",
    "butterfly": "butterfly",
    "camel": "camel",
    "cat": "cat",
    "chicken": "chicken",
    "chipmunk": "chipmunk",
    "cow": "cow",
    "crab": "crab",
    "cricket": "cricket",
    "crocodile": "crocodile",
    "deer": "deer",
    "dodo": "dodo",
    "dog": "dog",
    "dolphin": "dolphin",
    "donkey": "donkey",
    "dove": "dove_of_peace",
    "dragon": "dragon",
    "duck": "duck",
    "eagle": "eagle",
    "elephant": "elephant",
    "flamingo": "flamingo",
    "fox": "fox_face",
    "frog": "frog",
    "giraffe": "giraffe_face",
    "goat": "goat",
    "goose": "goose",
    "gorilla": "gorilla",
    "hedgehog": "hedgehog",
    "hippo": "hippopotamus",
    "horse": "horse",
    "kangaroo": "kangaroo",
    "koala": "koala",
    "leopard": "leopard",
    "lion": "lion_face",
    "lizard": "lizard",
    "llama": "llama",
    "lobster": "lobster",
    "mammoth": "mammoth",
    "monkey": "monkey",
    "moose": "moose",
    "mosquito": "mosquito",
    "mouse": "mouse",
    "octopus": "octopus",
    "orangutan": "orangutan",
    "otter": "otter",
    "owl": "owl",
    "ox": "ox",
    "panda": "panda_face",
    "parrot": "parrot",
    "peacock": "peacock",
    "penguin": "penguin",
    "pig": "pig",
    "poodle": "poodle",
    "rabbit": "rabbit",
    "raccoon": "raccoon",
    "ram": "ram",
    "rat": "rat",
    "rhino": "rhinoceros",
    "rooster": "rooster",
    "scorpion": "scorpion",
    "seal": "seal",
    "shark": "shark",
    "shrimp": "shrimp",
    "skunk": "skunk",
    "sloth": "sloth",
    "snail": "snail",
    "snake": "snake",
    "spider": "spider",
    "squid": "squid",
    "swan": "swan",
    "tiger": "tiger",
    "turkey": "turkey",
    "turtle": "turtle",
    "unicorn": "unicorn_face",
    "whale": "whale",
    "wolf": "wolf",
    "worm": "worm",
    "zebra": "zebra_face",
    # -- More animals --
    "chick": "hatching_chick",
    "blowfish": "blowfish",
    "cockroach": "cockroach",
    "fish": "fish",
    "fly": "fly",
    "hamster": "hamster",
    "jellyfish": "jellyfish",
    "microbe": "microbe",
    "phoenix": "phoenix",
    # -- Food & drink --
    "avocado": "avocado",
    "banana": "banana",
    "broccoli": "broccoli",
    "burrito": "burrito",
    "cake": "cake",
    "candy": "candy",
    "cherry": "cherries",
    "chestnut": "chestnut",
    "coconut": "coconut",
    "cookie": "cookie",
    "corn": "corn",
    "cupcake": "cupcake",
    "doughnut": "doughnut",
    "grapes": "grapes",
    "hamburger": "hamburger",
    "lemon": "lemon",
    "mango": "mango",
    "melon": "melon",
    "mushroom": "mushroom",
    "peach": "peach",
    "peanut": "peanuts",
    "pear": "pear",
    "pepper": "hot_pepper",
    "pie": "pie",
    "pineapple": "pineapple",
    "pizza": "pizza",
    "potato": "potato",
    "pretzel": "pretzel",
    "strawberry": "strawberry",
    "taco": "taco",
    "tomato": "tomato",
    "waffle": "waffle",
    "watermelon": "watermelon",
    "apple": "apple",
    "bacon": "bacon",
    "bagel": "bagel",
    "blueberry": "blueberries",
    "chocolate": "chocolate_bar",
    "coffee": "coffee",
    "croissant": "croissant",
    "egg": "egg",
    "garlic": "garlic",
    "onion": "onion",
    "pancake": "pancakes",
    "popcorn": "popcorn",
    "sushi": "sushi",
    # -- Nature & weather --
    "cactus": "cactus",
    "comet": "comet",
    "crystal": "crystal_ball",
    "cyclone": "cyclone",
    "flame": "fire",
    "globe": "earth_americas",
    "leaf": "fallen_leaf",
    "moon": "full_moon",
    "mountain": "mountain",
    "ocean": "ocean",
    "rainbow": "rainbow",
    "snowflake": "snowflake",
    "star": "star",
    "sunflower": "sunflower",
    "tornado": "tornado",
    "tulip": "tulip",
    "volcano": "volcano",
    "wave": "ocean",
    "lotus": "lotus",
    "rose": "rose",
    "seedling": "seedling",
    "palm": "palm_tree",
    "shamrock": "shamrock",
    # -- Objects & things --
    "anchor": "anchor",
    "balloon": "balloon",
    "bell": "bell",
    "bomb": "bomb",
    "bone": "bone",
    "book": "book",
    "boot": "boot",
    "boomerang": "boomerang",
    "brick": "bricks",
    "broom": "broom",
    "bucket": "bucket",
    "candle": "candle",
    "compass": "compass",
    "crown": "crown",
    "diamond": "gem",
    "drum": "drum_with_drumsticks",
    "feather": "feather",
    "flashlight": "flashlight",
    "gear": "gear",
    "guitar": "guitar",
    "hammer": "hammer",
    "hook": "hook",
    "key": "key",
    "kite": "kite",
    "knot": "knot",
    "ladder": "ladder",
    "lantern": "izakaya_lantern",
    "magnet": "magnet",
    "mirror": "mirror",
    "parachute": "parachute",
    "puzzle": "jigsaw",
    "ribbon": "ribbon",
    "rocket": "rocket",
    "satellite": "satellite",
    "scroll": "scroll",
    "shield": "shield",
    "sponge": "sponge",
    "telescope": "telescope",
    "tent": "tent",
    "thread": "thread",
    "trophy": "trophy",
    "wheel": "wheel",
    "wrench": "wrench",
    "yarn": "yarn",
    # -- Vehicles & transport --
    "canoe": "canoe",
    "helicopter": "helicopter",
    "sailboat": "sailboat",
    "sled": "sled",
    # -- More objects --
    "axe": "axe",
    "dagger": "dagger_knife",
    "scissors": "scissors",
    "trident": "trident",
    "bow": "bow_and_arrow",
    "hourglass": "hourglass",
    "stopwatch": "stopwatch",
}

# ---------------------------------------------------------------------------
# Adjectives — each maps to a standard Slack emoji shortcode.
# ---------------------------------------------------------------------------

ADJECTIVES: dict[str, str] = {
    # -- Temperature & weather --
    "arctic": "snowflake",
    "blazing": "fire",
    "breezy": "wind_blowing_face",
    "foggy": "fog",
    "frozen": "ice_cube",
    "frosty": "snowman",
    "gusty": "dash",
    "icy": "cold_face",
    "misty": "fog",
    "molten": "volcano",
    "snowy": "cloud",
    "solar": "sunny",
    "stormy": "thunder_cloud_and_rain",
    "sunny": "sun_with_face",
    "tidal": "ocean",
    "warm": "hotsprings",
    "windy": "leaves",
    "wintry": "snowflake",
    # -- Light & color --
    "amber": "large_orange_circle",
    "bright": "high_brightness",
    "cobalt": "large_blue_circle",
    "coral": "coral",
    "crimson": "red_circle",
    "cyan": "large_blue_circle",
    "dark": "new_moon",
    "emerald": "large_green_circle",
    "golden": "star2",
    "jade": "large_green_circle",
    "lavender": "large_purple_circle",
    "neon": "zap",
    "onyx": "black_circle",
    "pale": "white_circle",
    "rosy": "cherry_blossom",
    "ruby": "red_circle",
    "scarlet": "red_circle",
    "silver": "white_circle",
    "topaz": "large_yellow_circle",
    "vivid": "rainbow",
    # -- Material & texture --
    "bronze": "coin",
    "carbon": "black_large_square",
    "chrome": "mirror",
    "copper": "coin",
    "flint": "rock",
    "gilded": "crown",
    "granite": "rock",
    "iron": "chains",
    "ivory": "bone",
    "marble": "moyai",
    "mossy": "herb",
    "opal": "gem",
    "paper": "scroll",
    "pearly": "shell",
    "plaid": "shirt",
    "rustic": "wood",
    "sandy": "beach_with_umbrella",
    "steel": "shield",
    "stony": "rock",
    "timber": "wood",
    "velvet": "ribbon",
    # -- Sound & motion --
    "acoustic": "guitar",
    "echo": "loud_sound",
    "hushed": "shushing_face",
    "muted": "mute",
    "quiet": "shushing_face",
    "rapid": "fast_forward",
    "silent": "zipper_mouth_face",
    "sonic": "boom",
    "swift": "dash",
    "turbo": "rocket",
    "zippy": "racing_car",
    # -- Space & cosmic --
    "astral": "milky_way",
    "cosmic": "ringed_planet",
    "galactic": "stars",
    "lunar": "crescent_moon",
    "nebula": "milky_way",
    "orbit": "ringed_planet",
    "stellar": "star2",
    # -- Mood & character --
    "bold": "muscle",
    "brave": "crossed_swords",
    "calm": "dove_of_peace",
    "clever": "brain",
    "cunning": "fox_face",
    "daring": "dart",
    "deft": "pinching_hand",
    "eager": "eyes",
    "epic": "trophy",
    "feral": "wolf",
    "fierce": "fire",
    "gentle": "dove_of_peace",
    "humble": "pray",
    "keen": "mag",
    "lavish": "money_with_wings",
    "lofty": "mountain",
    "lucky": "four_leaf_clover",
    "mighty": "muscle",
    "mystic": "crystal_ball",
    "nimble": "wind_blowing_face",
    "noble": "crown",
    "proud": "lion_face",
    "regal": "crown",
    "rugged": "mountain",
    "sage": "herb",
    "sharp": "hocho",
    "spicy": "hot_pepper",
    "steady": "anchor",
    "subtle": "mag",
    "wild": "boom",
    "zen": "pray",
    # -- Other evocative --
    "alpine": "mountain",
    "ancient": "moyai",
    "atomic": "atom_symbol",
    "dusty": "wind_blowing_face",
    "electric": "zap",
    "ember": "fire",
    "fossil": "bone",
    "glacial": "snowflake",
    "hidden": "sleuth_or_spy",
    "hollow": "hole",
    "idle": "zzz",
    "kindled": "candle",
    "laser": "flashlight",
    "lean": "dart",
    "lone": "cactus",
    "nomad": "compass",
    "obsidian": "black_large_square",
    "pastel": "art",
    "polar": "polar_bear",
    "prism": "rainbow",
    "shadow": "new_moon",
    "smoky": "fog",
    "twilight": "city_sunset",
    "vast": "earth_americas",
    "wired": "electric_plug",
    "wispy": "cloud",
}

# ---------------------------------------------------------------------------
# Verbs — present-participle form, each maps to a standard Slack emoji.
# ---------------------------------------------------------------------------

VERBS: dict[str, str] = {
    # -- Emotions & expressions --
    "crying": "cry",
    "sobbing": "sob",
    "laughing": "joy",
    "blushing": "blush",
    "winking": "wink",
    "yawning": "yawning_face",
    "sneezing": "sneezing_face",
    "raging": "rage",
    "melting": "melting_face",
    "cursing": "face_with_symbols_on_mouth",
    "thinking": "thinking_face",
    "partying": "partying_face",
    "saluting": "saluting_face",
    "freezing": "cold_face",
    "dreaming": "thought_balloon",
    # -- Movement & action --
    "dancing": "dancer",
    "running": "runner",
    "surfing": "surfer",
    "rowing": "rowboat",
    "diving": "diving_mask",
    "skating": "ice_skate",
    "skiing": "ski",
    "racing": "racing_car",
    "riding": "horse_racing",
    "flying": "airplane",
    "floating": "balloon",
    "spinning": "cyclone",
    "rolling": "wheel",
    "bouncing": "basketball",
    "sneaking": "ninja",
    "juggling": "juggling",
    # -- Sound & signal --
    "singing": "microphone",
    "ringing": "bell",
    "roaring": "lion_face",
    "howling": "wolf",
    "ticking": "stopwatch",
    # -- Nature & transformation --
    "blooming": "blossom",
    "wilting": "wilted_flower",
    "rising": "sunrise",
    "falling": "fallen_leaf",
    "erupting": "volcano",
    "burning": "fire",
    "sparkling": "star2",
    "glowing": "bulb",
    "sizzling": "hot_pepper",
    "bubbling": "bubble_tea",
    "drifting": "cloud",
    "fading": "ghost",
    "waving": "wave",
    # -- Activities --
    "cooking": "cook",
    "fishing": "fishing_pole_and_fish",
    "camping": "tent",
    "painting": "art",
    "knitting": "yarn",
    "gardening": "seedling",
    "hammering": "hammer",
    # -- Power & force --
    "charging": "zap",
    "exploding": "boom",
    "flexing": "muscle",
    "prowling": "leopard",
    "soaring": "eagle",
    "zapping": "zap",
    # -- Rest --
    "sleeping": "sleeping",
    "napping": "zzz",
    "peeking": "eyes",
    "praying": "pray",
}


def generate_alias() -> str:
    """Generate a session alias like ``dancing-cosmic-falcon``.

    Returns a ``verb-adjective-noun`` string.  All three parts
    map 1:1 to standard Slack emoji via :data:`VERBS`, :data:`ADJECTIVES`,
    and :data:`NOUNS`.

    ~65 × ~134 × ~221 ≈ 1,900,000 unique combinations.
    """
    verb = random.choice(tuple(VERBS.keys()))
    adj = random.choice(tuple(ADJECTIVES.keys()))
    noun = random.choice(tuple(NOUNS.keys()))
    return f"{verb}-{adj}-{noun}"


def all_emojis_for_alias(alias: str) -> tuple[str, str, str]:
    """Return (verb_emoji, adj_emoji, noun_emoji) for a session alias.

    All are standard Slack emoji shortcodes (without colons).
    Falls back to ``sparkles`` for unrecognised parts.
    Checks VERBS first, then ADJECTIVES for the first two parts.
    """
    parts = alias.split("-")
    if len(parts) >= 3:
        first, second, noun = parts[0], parts[1], parts[-1]
    elif len(parts) == 2:
        # Backward compat with old adjective-noun aliases
        first, second, noun = parts[0], "", parts[1]
    else:
        first, second, noun = "", "", parts[0] if parts else ""

    # Resolve first part: try VERBS, then ADJECTIVES (backward compat)
    first_emoji = VERBS.get(first, ADJECTIVES.get(first, "sparkles"))
    # Resolve second part: try ADJECTIVES, then VERBS (backward compat)
    second_emoji = ADJECTIVES.get(second, VERBS.get(second, "sparkles"))

    return (
        first_emoji,
        second_emoji,
        NOUNS.get(noun, "sparkles"),
    )


def emojis_for_alias(alias: str) -> tuple[str, str]:
    """Return two randomly-chosen emojis (out of 3) for a session alias.

    Returns a pair of standard Slack emoji shortcodes (without colons).
    The three candidates are (adj1, adj2, noun); two are picked at random.
    """
    all_three = all_emojis_for_alias(alias)
    picked = random.sample(all_three, 2)
    return (picked[0], picked[1])


# Keep a single-emoji convenience function for backward compatibility.
def emoji_for_alias(alias: str) -> str:
    """Return the noun emoji for a session alias (legacy single-emoji API)."""
    *_, noun_emoji = all_emojis_for_alias(alias)
    return noun_emoji
