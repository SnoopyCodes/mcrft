"""A fixed numeric observation layout, with no Gymnasium/NumPy dependency.

The stateful calculator runs in observations.js on every physics tick. This
module encodes its delayed result for a Box observation space in your own env.
"""

OBSERVATION_FIELDS = (
    "self_position.radius", "self_position.yaw", "self_position.height",
    "self_velocity.radial", "self_velocity.tangential", "self_velocity.vertical",
    "self_aim_to_center.yaw", "self_aim_to_center.pitch",
    "opponent_position.radius", "opponent_position.yaw", "opponent_position.height",
    "opponent_velocity.radial", "opponent_velocity.tangential", "opponent_velocity.vertical",
    "opponent_aim_to_self.yaw", "opponent_aim_to_self.pitch",
    "on_ground", "ticks_since_gotten_hit", "ticks_since_hit_on_opponent",
    "self_velocity_valid", "opponent_visible", "opponent_velocity_valid", "on_ground_valid",
)


def observation_vector(observation):
    """Return 23 floats in OBSERVATION_FIELDS order.

    Missing geometry/velocity/ground values are 0 with explicit validity bits.
    A hit timer of -1 means no matching hurt event has been observed yet.
    Values retain their physical units; normalization belongs to your env.
    """
    if observation is None:
        return None
    validity = {
        "self_velocity_valid": observation["self_velocity"] is not None,
        "opponent_visible": observation["opponent_position"] is not None,
        "opponent_velocity_valid": observation["opponent_velocity"] is not None,
        "on_ground_valid": observation["on_ground"] is not None,
    }
    result = []
    for field in OBSERVATION_FIELDS:
        if field in validity:
            value = validity[field]
        elif "." in field:
            group, name = field.split(".")
            value = (observation[group] or {}).get(name)
        else:
            value = observation[field]
        if value is None:
            value = -1 if field.startswith("ticks_since_") else 0
        result.append(float(value))
    return result
