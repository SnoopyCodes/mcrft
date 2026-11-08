"""Exercise real Spigot rounds with two scripted bots: python -m test.run_arena."""

import argparse

from .arena_client import ArenaClient
from .bot import create_training_bot
from .environment import SumoEnvironment, chase_policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Minecraft server host")
    parser.add_argument("--port", type=int, default=25565)
    parser.add_argument("--bridge-host", default="127.0.0.1")
    parser.add_argument("--bridge-port", type=int, default=8765)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--delay-ms", type=float, default=250)
    args = parser.parse_args()
    if args.rounds <= 0:
        parser.error("--rounds must be positive")
    bots = []
    controllers = {}
    arena = env = None
    try:
        arena = ArenaClient(args.bridge_host, args.bridge_port)
        for name in ("TrainingBot", "OpponentBot"):
            bot, controller = create_training_bot(
                username=name, host=args.host, port=args.port,
                observation_delay_ms=args.delay_ms,
            )
            bots.append(bot)
            controllers[name] = controller
        env = SumoEnvironment(arena, controllers, opponent_policy=chase_policy)
        for _ in range(args.rounds):
            observation, info = env.reset()
            while True:
                observation, reward, terminated, truncated, info = env.step(chase_policy(observation))
                if terminated or truncated:
                    print(f"Episode {info['episode_id']}: reward={reward}, result={info}")
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if env is not None:
            env.close()
        for controller in controllers.values():
            controller.close()
        for bot in bots:
            bot.quit()
        if arena is not None:
            arena.close()


if __name__ == "__main__":
    main()
