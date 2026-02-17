"""Tests for chicane.emoji_map — alias generation and emoji mapping."""

import pytest

from chicane.emoji_map import (
    ADJECTIVES,
    NOUNS,
    VERBS,
    all_emojis_for_alias,
    emoji_for_alias,
    emojis_for_alias,
    generate_alias,
)


class TestGenerateAlias:
    """generate_alias produces valid verb-adjective-noun triples."""

    def test_format_is_verb_adj_noun(self):
        alias = generate_alias()
        parts = alias.split("-")
        assert len(parts) == 3
        assert parts[0] in VERBS
        assert parts[1] in ADJECTIVES
        assert parts[2] in NOUNS

    def test_uniqueness_over_many_calls(self):
        """Should produce varied results (not always the same)."""
        aliases = {generate_alias() for _ in range(100)}
        assert len(aliases) >= 80

    def test_all_generated_aliases_have_three_emojis(self):
        """Every alias should resolve to three non-fallback emojis."""
        for _ in range(200):
            alias = generate_alias()
            verb_e, adj_e, noun_e = all_emojis_for_alias(alias)
            assert verb_e != "sparkles", f"{alias} verb fell back"
            assert adj_e != "sparkles", f"{alias} adj fell back"
            assert noun_e != "sparkles", f"{alias} noun fell back"


class TestAllEmojisForAlias:
    """all_emojis_for_alias returns (verb_emoji, adj_emoji, noun_emoji)."""

    def test_verb_adj_noun(self):
        v, a, n = all_emojis_for_alias("dancing-cosmic-eagle")
        assert v == "dancer"
        assert a == "ringed_planet"
        assert n == "eagle"

    def test_crying_frozen_dragon(self):
        v, a, n = all_emojis_for_alias("crying-frozen-dragon")
        assert v == "cry"
        assert a == "ice_cube"
        assert n == "dragon"

    def test_sleeping_wild_phoenix(self):
        v, a, n = all_emojis_for_alias("sleeping-wild-phoenix")
        assert v == "sleeping"
        assert a == "boom"
        assert n == "phoenix"

    def test_backward_compat_adj_adj_noun(self):
        """Old adjective-adjective-noun aliases still resolve."""
        v, a, n = all_emojis_for_alias("blazing-cosmic-eagle")
        assert v == "fire"  # blazing found in ADJECTIVES via fallback
        assert a == "ringed_planet"
        assert n == "eagle"

    def test_backward_compat_two_part(self):
        """Old adjective-noun aliases still work."""
        first, second, noun = all_emojis_for_alias("blazing-eagle")
        assert first == "fire"
        assert second == "sparkles"  # no second part
        assert noun == "eagle"

    def test_single_word(self):
        first, second, noun = all_emojis_for_alias("dragon")
        assert first == "sparkles"
        assert second == "sparkles"
        assert noun == "dragon"

    def test_empty_string(self):
        first, second, noun = all_emojis_for_alias("")
        assert first == "sparkles"
        assert second == "sparkles"
        assert noun == "sparkles"


class TestEmojisForAlias:
    """emojis_for_alias picks 2 random emojis from the 3 available."""

    def test_returns_two_emojis(self):
        result = emojis_for_alias("dancing-cosmic-eagle")
        assert len(result) == 2

    def test_picked_from_valid_set(self):
        """Both returned emojis must be from the alias's 3 candidates."""
        all_three = set(all_emojis_for_alias("dancing-cosmic-eagle"))
        for _ in range(50):
            e1, e2 = emojis_for_alias("dancing-cosmic-eagle")
            assert e1 in all_three
            assert e2 in all_three

    def test_not_always_same_pair(self):
        """Over many calls, different pairs should appear."""
        pairs = set()
        for _ in range(100):
            pairs.add(emojis_for_alias("dancing-cosmic-eagle"))
        # 3 candidates → 3 possible ordered pairs of 2
        assert len(pairs) >= 2


class TestEmojiForAlias:
    """Legacy single-emoji API returns the noun emoji."""

    def test_returns_noun_emoji(self):
        assert emoji_for_alias("dancing-cosmic-eagle") == "eagle"

    def test_backward_compat_two_part(self):
        assert emoji_for_alias("blazing-eagle") == "eagle"

    def test_fallback(self):
        assert emoji_for_alias("dancing-cosmic-xyzzy") == "sparkles"


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

    def test_all_lowercase(self):
        for adj in ADJECTIVES:
            assert adj == adj.lower()

    def test_all_alpha(self):
        for adj in ADJECTIVES:
            assert adj.isalpha(), f"Adjective '{adj}' not purely alphabetic"


class TestVerbCompleteness:
    """Verify every verb maps to a valid Slack shortcode."""

    def test_no_empty_values(self):
        for verb, emoji in VERBS.items():
            assert emoji, f"Verb '{verb}' has empty emoji"

    def test_no_colons_or_spaces(self):
        for verb, emoji in VERBS.items():
            assert ":" not in emoji, f"'{verb}' emoji '{emoji}' has colons"
            assert " " not in emoji, f"'{verb}' emoji '{emoji}' has spaces"

    def test_sufficient_count(self):
        assert len(VERBS) >= 50

    def test_all_lowercase(self):
        for verb in VERBS:
            assert verb == verb.lower()

    def test_all_alpha(self):
        for verb in VERBS:
            assert verb.isalpha(), f"Verb '{verb}' not purely alphabetic"

    def test_all_end_in_ing(self):
        for verb in VERBS:
            assert verb.endswith("ing"), f"Verb '{verb}' doesn't end in -ing"

    def test_no_overlap_with_adjectives(self):
        """Verbs and adjectives should be distinct word sets."""
        overlap = set(VERBS.keys()) & set(ADJECTIVES.keys())
        assert not overlap, f"Overlap between verbs and adjectives: {overlap}"


class TestIntegrationWithConfig:
    """Verify config.generate_session_alias uses the new generator."""

    def test_config_alias_maps_to_three_emojis(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        verb_e, adj_e, noun_e = all_emojis_for_alias(alias)
        assert verb_e != "sparkles"
        assert adj_e != "sparkles"
        assert noun_e != "sparkles"

    def test_config_alias_format(self):
        from chicane.config import generate_session_alias

        alias = generate_session_alias()
        parts = alias.split("-")
        assert len(parts) == 3
        assert parts[0] in VERBS
        assert parts[1] in ADJECTIVES
        assert parts[2] in NOUNS
