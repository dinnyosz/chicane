"""Tests for chicane.emoji_map — alias generation and emoji mapping."""

import pytest

from chicane.emoji_map import (
    ADJECTIVES,
    NOUNS,
    all_emojis_for_alias,
    emoji_for_alias,
    emojis_for_alias,
    generate_alias,
)


class TestGenerateAlias:
    """generate_alias produces valid adjective-adjective-noun triples."""

    def test_format_is_adj_adj_noun(self):
        alias = generate_alias()
        parts = alias.split("-")
        assert len(parts) == 3
        assert parts[0] in ADJECTIVES
        assert parts[1] in ADJECTIVES
        assert parts[2] in NOUNS

    def test_two_adjectives_are_different(self):
        """The two adjectives should never be the same word."""
        for _ in range(100):
            alias = generate_alias()
            parts = alias.split("-")
            assert parts[0] != parts[1], f"Duplicate adjectives in {alias}"

    def test_uniqueness_over_many_calls(self):
        """Should produce varied results (not always the same)."""
        aliases = {generate_alias() for _ in range(100)}
        assert len(aliases) >= 80

    def test_all_generated_aliases_have_three_emojis(self):
        """Every alias should resolve to three non-fallback emojis."""
        for _ in range(200):
            alias = generate_alias()
            adj1, adj2, noun = all_emojis_for_alias(alias)
            assert adj1 != "sparkles", f"{alias} adj1 fell back"
            assert adj2 != "sparkles", f"{alias} adj2 fell back"
            assert noun != "sparkles", f"{alias} noun fell back"


class TestAllEmojisForAlias:
    """all_emojis_for_alias returns (adj1_emoji, adj2_emoji, noun_emoji)."""

    def test_three_part_alias(self):
        adj1, adj2, noun = all_emojis_for_alias("blazing-cosmic-eagle")
        assert adj1 == "fire"
        assert adj2 == "ringed_planet"
        assert noun == "eagle"

    def test_stormy_golden_shrimp(self):
        adj1, adj2, noun = all_emojis_for_alias("stormy-golden-shrimp")
        assert adj1 == "thunder_cloud_and_rain"
        assert adj2 == "star2"
        assert noun == "shrimp"

    def test_backward_compat_two_part(self):
        """Old adjective-noun aliases still work."""
        adj1, adj2, noun = all_emojis_for_alias("blazing-eagle")
        assert adj1 == "fire"
        assert adj2 == "sparkles"  # no second adjective
        assert noun == "eagle"

    def test_single_word(self):
        adj1, adj2, noun = all_emojis_for_alias("dragon")
        assert adj1 == "sparkles"
        assert adj2 == "sparkles"
        assert noun == "dragon"

    def test_empty_string(self):
        adj1, adj2, noun = all_emojis_for_alias("")
        assert adj1 == "sparkles"
        assert adj2 == "sparkles"
        assert noun == "sparkles"


class TestEmojisForAlias:
    """emojis_for_alias picks 2 random emojis from the 3 available."""

    def test_returns_two_emojis(self):
        result = emojis_for_alias("blazing-cosmic-eagle")
        assert len(result) == 2

    def test_picked_from_valid_set(self):
        """Both returned emojis must be from the alias's 3 candidates."""
        all_three = set(all_emojis_for_alias("blazing-cosmic-eagle"))
        for _ in range(50):
            e1, e2 = emojis_for_alias("blazing-cosmic-eagle")
            assert e1 in all_three
            assert e2 in all_three

    def test_not_always_same_pair(self):
        """Over many calls, different pairs should appear."""
        pairs = set()
        for _ in range(100):
            pairs.add(emojis_for_alias("blazing-cosmic-eagle"))
        # 3 candidates → 3 possible ordered pairs of 2
        assert len(pairs) >= 2


class TestEmojiForAlias:
    """Legacy single-emoji API returns the noun emoji."""

    def test_returns_noun_emoji(self):
        assert emoji_for_alias("blazing-cosmic-eagle") == "eagle"

    def test_backward_compat_two_part(self):
        assert emoji_for_alias("blazing-eagle") == "eagle"

    def test_fallback(self):
        assert emoji_for_alias("blazing-cosmic-xyzzy") == "sparkles"


class TestNounCompleteness:
    """Verify every noun maps to a valid Slack shortcode."""

    def test_no_empty_values(self):
        for noun, emoji in NOUNS.items():
            assert emoji, f"Noun '{noun}' has empty emoji"

    def test_no_colons_or_spaces(self):
        for noun, emoji in NOUNS.items():
            assert ":" not in emoji, f"'{noun}' emoji '{emoji}' has colons"
            assert " " not in emoji, f"'{noun}' emoji '{emoji}' has spaces"

    def test_sufficient_count(self):
        assert len(NOUNS) >= 100

    def test_all_lowercase(self):
        for noun in NOUNS:
            assert noun == noun.lower()

    def test_no_spaces_in_nouns(self):
        for noun in NOUNS:
            assert " " not in noun


class TestAdjectiveCompleteness:
    """Verify every adjective maps to a valid Slack shortcode."""

    def test_no_empty_values(self):
        for adj, emoji in ADJECTIVES.items():
            assert emoji, f"Adjective '{adj}' has empty emoji"

    def test_no_colons_or_spaces(self):
        for adj, emoji in ADJECTIVES.items():
            assert ":" not in emoji, f"'{adj}' emoji '{emoji}' has colons"
            assert " " not in emoji, f"'{adj}' emoji '{emoji}' has spaces"

    def test_sufficient_count(self):
        assert len(ADJECTIVES) >= 100

    def test_no_duplicates_in_keys(self):
        # dict keys are unique by definition, but verify no weird shadowing
        keys = list(ADJECTIVES.keys())
        assert len(keys) == len(set(keys))

    def test_all_lowercase(self):
        for adj in ADJECTIVES:
            assert adj == adj.lower()

    def test_all_alpha(self):
        for adj in ADJECTIVES:
            assert adj.isalpha(), f"Adjective '{adj}' not purely alphabetic"

    def test_enough_for_sample_of_two(self):
        """Need at least 2 adjectives for random.sample(adj_keys, 2)."""
        assert len(ADJECTIVES) >= 2


class TestIntegrationWithConfig:
    """Verify config.generate_session_alias uses the new generator."""

    def test_config_alias_maps_to_three_emojis(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        adj1, adj2, noun = all_emojis_for_alias(alias)
        assert adj1 != "sparkles"
        assert adj2 != "sparkles"
        assert noun != "sparkles"

    def test_config_alias_format(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        parts = alias.split("-")
        assert len(parts) == 3
        assert parts[0] in ADJECTIVES
        assert parts[1] in ADJECTIVES
        assert parts[2] in NOUNS
