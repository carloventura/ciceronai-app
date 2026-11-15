ADJECTIVE_BASE = [
    "moon", "frost", "ember", "ash", "silver", "stone", "gale", "thorn", "wild", "cinder",
    "mist", "dawn", "echo", "bloom", "raven", "iron", "hollow", "amber", "glow", "storm",
    "whisper", "crystal", "bramble", "sun", "river", "dusk", "cedar", "pine", "falcon", "fox",
    "lunar", "sage", "myrtle", "moss", "willow", "drift", "rune", "shadow", "winter", "autumn",
    "spring", "summer", "star", "night", "blaze", "specter", "aurora", "morrow", "copper", "jade",
    "violet", "saffron", "marble", "ebony", "golden", "honey", "heather", "seabloom", "oaken",
    "cloud", "drake", "wyvern", "flame", "mystic", "ancient", "crimson", "ivory", "sable", "silken",
    "fern", "dewdrop", "tide", "brine", "hearth", "sol", "petal", "linden", "ivy", "glimmer",
    "lark", "halo", "feather", "sapphire", "pearl", "quartz", "sylvan", "foxglove", "meadow",
    "wren", "swift", "harrow", "gilded", "obsidian", "briar", "brass", "ink", "starlight", "beacon",
    "riverstone", "mire", "rose", "velvet", "misty", "summit", "grove", "lavender", "prairie",
    "cobalt", "fable", "dragon", "wolf", "wynd", "clouded", "molten", "thistle", "dandelion",
    "bluebell", "marigold", "harbor", "glade", "hush", "glass", "breeze", "zephyr", "water",
    "fire", "ice", "earth", "air", "lantern", "ocean", "fjord", "canyon", "horizon", "lotus",
    "cliff", "praxis", "fable", "dew", "sunlit", "snow", "glacier", "stormglass", "relic", "heaven"
]

NOUN_BASE = [
    "vale", "rowan", "hollow", "fen", "grove", "keep", "ridge", "moor", "oak", "thorn", "cairn",
    "wren", "moss", "watch", "ash", "wild", "spire", "meadow", "raven", "stone", "crown", "glade",
    "forge", "fane", "willow", "mill", "stream", "pines", "glen", "cliff", "firth", "hearth", "root",
    "briar", "tide", "marsh", "drift", "thistle", "summit", "echo", "wisp", "cove", "dune", "grass",
    "heather", "canyon", "harbor", "peak", "shelter", "garden", "gate", "hall", "haunt", "linden",
    "ivy", "drake", "nest", "eagle", "brook", "wood", "glimmer", "bridge", "chapel", "quarry",
    "orchard", "ravine", "lake", "lantern", "watcher", "tavern", "bower", "trail", "landfall", "mire",
    "lotus", "morrow", "stormgate", "snowfall", "pinecrest", "falcon", "riverbank", "forrest", "embershade",
    "sunfall", "moonfall", "forgefire", "dawnmere", "frostmere", "stonefall", "cinderkeep", "thornmere",
    "wildmere", "blossom", "bluebell", "relic", "altar", "sanctum", "wander", "pinecone", "rootmere",
    "crownfall", "ambervale", "hollowwood", "riverfall", "mistwatch", "fogbank", "silvermere", "ashfall",
    "emberglow", "sunstone", "moonwood", "briarwood", "mossvale", "daybreak", "covegate", "gildedhall",
    "glassmere", "dawnwatch", "brookmere", "woodmere", "redpine", "mistgrove", "northern", "sunder", "hallow"
]


def build_word_bank(base_words, suffixes, target_count):
    bank = set()
    for word in base_words:
        bank.add(word)
    while len(bank) < target_count:
        for prefix in base_words:
            for suffix in suffixes:
                bank.add(f"{prefix}-{suffix}")
                if len(bank) >= target_count:
                    return sorted(bank)
    return sorted(bank)


ADJECTIVES = build_word_bank(ADJECTIVE_BASE, [
    "ael", "ere", "eth", "ion", "or", "en", "ar", "an", "lyn", "dra", "mor", "th", "vane",
    "shade", "mist", "bloom", "fall", "peak", "vale", "mere", "gale", "rune", "thorn", "harrow",
    "vale", "crest", "weaver", "stone", "thorn", "sky", "tide", "lore", "oak", "glass", "bloom",
    "drift", "frost", "ember", "fire", "sleet", "wind", "ash", "star", "glow", "mist", "gale"
], 1000)

NOUNS = build_word_bank(NOUN_BASE, [
    "vale", "mere", "grove", "watch", "keep", "thorn", "ridge", "hall", "lore", "oak", "marsh",
    "spire", "fen", "cairn", "moss", "harbor", "stone", "bloom", "glade", "weir", "bank", "fall",
    "reach", "dune", "cliff", "forge", "root", "hollow", "mire", "hush", "glen", "brook", "peak",
    "garden", "meadow", "fen", "sky", "hollow", "wood", "tide", "glow", "gale", "trail", "thorn"
], 1000)
