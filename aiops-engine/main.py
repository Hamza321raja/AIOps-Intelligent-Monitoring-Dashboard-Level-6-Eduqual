from engine.aiops import run
import logging
import sys

logger = logging.getLogger("main")


def main():
    print("🚀 AIOps Engine Started")

    try:
        run()

    except KeyboardInterrupt:
        print("\n🛑 AIOps Engine stopped by user")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Critical Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()