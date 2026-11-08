"""Python observation interface checks; no server or JavaScript bridge needed."""

import unittest

from test.controller import SumoController
from test.observations import OBSERVATION_FIELDS, observation_vector


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.observation = {
            "self_position": {"radius": 3, "yaw": 0.2, "height": 1},
            "self_velocity": None,
            "self_aim_to_center": {"yaw": -0.1, "pitch": 0.5},
            "opponent_position": None,
            "opponent_velocity": None,
            "opponent_aim_to_self": None,
            "on_ground": False,
            "ticks_since_gotten_hit": None,
            "ticks_since_hit_on_opponent": None,
        }

    def test_fixed_layout_masks_and_unknown_hit_sentinels(self):
        vector = observation_vector(self.observation)
        self.assertEqual(len(vector), 23)
        values = dict(zip(OBSERVATION_FIELDS, vector))
        self.assertEqual(values["self_position.radius"], 3.0)
        self.assertEqual(values["self_aim_to_center.pitch"], 0.5)
        self.assertEqual(values["self_velocity.radial"], 0.0)
        self.assertEqual(values["self_velocity_valid"], 0.0)
        self.assertEqual(values["opponent_visible"], 0.0)
        self.assertEqual(values["on_ground"], 0.0)
        self.assertEqual(values["on_ground_valid"], 1.0)
        self.assertEqual(values["ticks_since_gotten_hit"], -1.0)
        self.assertTrue(all(isinstance(value, float) for value in vector))

    def test_zero_velocity_and_zero_tick_hit_are_valid(self):
        self.observation.update(
            self_velocity={"radial": 0, "tangential": -0.5, "vertical": 0.1},
            opponent_position={"radius": 2, "yaw": 1, "height": 0},
            opponent_velocity={"radial": 0, "tangential": 0, "vertical": 0},
            opponent_aim_to_self={"yaw": 0, "pitch": 0},
            on_ground=None, ticks_since_gotten_hit=0, ticks_since_hit_on_opponent=8,
        )
        values = dict(zip(OBSERVATION_FIELDS, observation_vector(self.observation)))
        self.assertEqual(values["self_velocity_valid"], 1)
        self.assertEqual(values["self_velocity.tangential"], -0.5)
        self.assertEqual(values["opponent_visible"], 1)
        self.assertEqual(values["opponent_velocity_valid"], 1)
        self.assertEqual(values["on_ground_valid"], 0)
        self.assertEqual(values["ticks_since_gotten_hit"], 0)
        self.assertEqual(values["ticks_since_hit_on_opponent"], 8)

    def test_controller_interface_warmup_and_encoding(self):
        controller = SumoController.__new__(SumoController)
        controller._has_center = False
        with self.assertRaisesRegex(ValueError, "arena_center"):
            controller.get_observation()
        controller._has_center = True
        controller.get_state = lambda: None
        self.assertIsNone(controller.get_observation())
        self.assertIsNone(controller.get_observation(vector=True))
        controller.get_state = lambda: {"observation": self.observation}
        self.assertEqual(controller.get_observation(), self.observation)
        self.assertEqual(controller.get_observation(vector=True), observation_vector(self.observation))


if __name__ == "__main__":
    unittest.main()
