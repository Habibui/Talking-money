#!/usr/bin/env python3
"""
26.09.2026, вечер — продакшен-точка входа v2 для почасового режима
(минимальный запуск, решение менеджера, п.4 «отдельный workflow v2»).
В отличие от scripts/dry_run_v2.py (весь пайплайн одним ручным запуском,
ничего не публикует, для локальной проверки человеком) — этот скрипт
запускает ТОЛЬКО Сборщик (src.collector.run()) и предназначен для
регулярного автоматического запуска (см. .github/workflows/publish_v2.yml,
mode=collect): раз в час дёргается внешним Cloudflare Worker так же, как
main.py у v1 (см. README.md), плюс редкий резервный cron в самом workflow.

Ничего не публикует в Telegram — Сборщик только копит архив заметок
(archive/cards/, state/v2_seen.json). Публикация/отправка автору — отдельный
шаг, scripts/issue_v2.py, два раза в день.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collector, config

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("collect_v2")


def main() -> int:
    if not config.ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY не задан — Экстрактор не сможет вызвать Claude API")
        return 1

    stats = collector.run()
    logger.info("Сборщик (продакшен, почасовой режим): %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
