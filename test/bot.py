from javascript import require, On

if __package__:
    from .controller import SumoController
else:
    from controller import SumoController


def create_training_bot(*, observation_delay_ms=250, arena_center=None, manual_chat=False,
                        **connection_options):
    """Create the bot and attach controls before it spawns."""
    mineflayer = require("mineflayer")
    bot = mineflayer.createBot({
        "host": "127.0.0.1",
        "port": 25565,
        "username": "TrainingBot",
        "auth": "offline",
        "version": "1.8.8",
        **connection_options,
    })
    controller = SumoController(bot, observation_delay_ms=observation_delay_ms,
                                arena_center=arena_center)

    @On(bot, "spawn")
    def on_spawn(*args):
        print("Bot spawned! Controls ready; observations are delayed.")
        bot.chat("Ready")

    @On(bot, "chat")
    def on_chat(this, username, message, *args):
        if not manual_chat or username == bot.username:
            return
        commands = {
            "forward": {"forward": 1},
            "back": {"forward": -1},
            "left": {"strafe": -1},
            "right": {"strafe": 1},
            "jump": {"jump": True},
            "sprint": {"forward": 1, "sprint": True},
            "attack": {"attack": True},
            "turn": {"delta_yaw": 0.3},
            "stop": {},
        }
        if message in commands:
            controller.apply_action(**commands[message])
        elif message == "state":
            print(controller.get_state())

    @On(bot, "kicked")
    def on_kicked(this, reason, *args):
        print("Kicked:", reason)

    @On(bot, "error")
    def on_error(this, error, *args):
        print("Error:", error)

    return bot, controller


def main():
    bot, controller = create_training_bot(manual_chat=True)
    try:
        input("Press Enter to quit.\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        controller.close()
        bot.quit()


if __name__ == "__main__":
    main()
