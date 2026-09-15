"""Extensible presentation groups; not a fixed 2K16/game-memory schema."""
TIER_COLORS = {"Pink Diamond": "#ef9bd2", "Diamond": "#8adaf1", "Amethyst": "#b38ae8",
    "Ruby": "#ec657a", "Sapphire": "#68a4ed", "Emerald": "#60ce99", "Gold": "#eacb73",
    "Silver": "#becbd8", "Bronze": "#c49b79", "Onyx": "#a4aaba"}
BADGE_COLORS = {"HALL_OF_FAME": "#bd8bfa", "GOLD": "#efcd72", "SILVER": "#c3cfde", "BRONZE": "#c9976c", "PERSONALITY": "#78bcec"}
BADGE_LABELS = {"HALL_OF_FAME": "Hall of Fame", "GOLD": "Gold", "SILVER": "Silver", "BRONZE": "Bronze", "PERSONALITY": "Personality"}
ATTRIBUTE_GROUPS = {
    "Outside scoring": "open_shot_mid_range open_shot_three contested_shot_mid_range contested_shot_three off_dribble_shot_mid_range off_dribble_shot_three shot_iq free_throw offensive_consistency",
    "Inside scoring": "shot_close standing_layup driving_layup standing_dunk driving_dunk contact_dunk draw_foul post_control post_hook post_fadeaway hands",
    "Playmaking": "ball_control passing_accuracy passing_vision passing_iq",
    "Athleticism": "speed speed_with_ball acceleration vertical strength stamina hustle reaction_time overall_durability",
    "Defending": "on_ball_defense_iq low_post_defense_iq pick_and_roll_defense_iq help_defense_iq lateral_quickness pass_perception steal block shot_contest defensive_consistency",
    "Rebounding": "offensive_rebound defensive_rebound boxout",
    "Additional attributes": "intangibles potential",
}


def label(key: str) -> str:
    return key.replace("_", " ").title().replace(" Iq", " IQ").replace("Three", "3PT")


def section_values(card, section):
    return {k: v for k, v in card.get(section, {}).get("values", {}).items() if not k.startswith("source_")}


def attribute_groups(card):
    values = section_values(card, "attributes")
    grouped, seen = [], set()
    for name, fields in ATTRIBUTE_GROUPS.items():
        keys = [field for field in fields.split() if field in values]
        if keys:
            grouped.append((name, [(key, values[key]) for key in keys]))
            seen.update(keys)
    extra = [(key, value) for key, value in values.items() if key not in seen]
    if extra:
        grouped.append(("Other attributes", extra))
    return grouped


def rating_color(value):
    if not isinstance(value, (int, float)):
        return "#7e91a7"
    return "#6ce3b0" if value >= 90 else "#b1d989" if value >= 80 else "#e4c772" if value >= 70 else "#e9a075" if value >= 60 else "#dd7b89"
