"""Tests for chicane.emoji_map — alias generation and emoji mapping."""

import pytest

from chicane.emoji_map import (
    ADJECTIVES,
    NOUNS,
    emoji_for_alias,
    emojis_for_alias,
    generate_alias,
)


class TestGenerateAlias:
    """generate_alias produces valid adjective-noun pairs."""

    def test_format_is_adjective_dash_noun(self):
        alias = generate_alias()
        parts = alias.split("-")
        assert len(parts) == 2
        assert parts[0] in ADJECTIVES
        assert parts[1] in NOUNS

    def test_uniqueness_over_many_calls(self):
        """Should produce varied results (not always the same)."""
        aliases = {generate_alias() for _ in range(100)}
        assert len(aliases) >= 80

    def test_all_generated_aliases_have_both_emojis(self):
        """Every alias should resolve to two non-fallback emojis."""
        for _ in range(200):
            alias = generate_alias()
            adj_emoji, noun_emoji = emojis_for_alias(alias)
            assert adj_emoji != "sparkles", f"{alias} adjective fell back"
            assert noun_emoji != "sparkles", f"{alias} noun fell back"


class TestEmojisForAlias:
    """emojis_for_alias returns (adjective_emoji, noun_emoji)."""

    def test_both_emojis_returned(self):
        adj, noun = emojis_for_alias("blazing-eagle")
        assert adj == "fire"
        assert noun == "eagle"

    def test_stormy_shrimp(self):
        adj, noun = emojis_for_alias("stormy-shrimp")
        assert adj == "thunder_cloud_and_rain"
        assert noun == "shrimp"

    def test_golden_dragon(self):
        adj, noun = emojis_for_alias("golden-dragon")
        assert adj == "star2"
        assert noun == "dragon"

    def test_frozen_rocket(self):
        adj, noun = emojis_for_alias("frozen-rocket")
        assert adj == "ice_cube"
        assert noun == "rocket"

    def test_mystic_owl(self):
        adj, noun = emojis_for_alias("mystic-owl")
        assert adj == "crystal_ball"
        assert noun == "owl"

    def test_unknown_adjective_falls_back(self):
        adj, noun = emojis_for_alias("xyzzy-eagle")
        assert adj == "sparkles"
        assert noun == "eagle"

    def test_unknown_noun_falls_back(self):
        adj, noun = emojis_for_alias("blazing-xyzzy")
        assert adj == "fire"
        assert noun == "sparkles"

    def test_single_word(self):
        adj, noun = emojis_for_alias("dragon")
        assert adj == "sparkles"
        assert noun == "dragon"

    def test_empty_string(self):
        adj, noun = emojis_for_alias("")
        assert adj == "sparkles"
        assert noun == "sparkles"


class TestEmojiForAlias:
    """Legacy single-emoji API returns the noun emoji."""

    def test_returns_noun_emoji(self):
        assert emoji_for_alias("blazing-eagle") == "eagle"

    def test_fallback(self):
        assert emoji_for_alias("blazing-xyzzy") == "sparkles"


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


class TestIntegrationWithConfig:
    """Verify config.generate_session_alias uses the new generator."""

    def test_config_alias_maps_to_both_emojis(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        adj_emoji, noun_emoji = emojis_for_alias(alias)
        assert adj_emoji != "sparkles"
        assert noun_emoji != "sparkles"

    def test_config_alias_format(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        parts = alias.split("-")
        assert len(parts) == 2
        assert parts[0] in ADJECTIVES
        assert parts[1] in NOUNS
