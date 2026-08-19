"""Run the portable composition smoke test."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID

from active_agent_platform.foundation import (
    CapturingLogger,
    FakeClock,
    FakeUuidGenerator,
    RuntimeDependencies,
    Settings,
)
from apps.hello_research import HelloResearchPlugin
from domain_sdk import CompositionRoot


async def main() -> None:
    dependencies = RuntimeDependencies(
        settings=Settings(environment="example"),
        clock=FakeClock(datetime(2026, 8, 17, tzinfo=UTC)),
        uuid=FakeUuidGenerator([UUID("00000000-0000-0000-0000-000000000001")]),
        logger=CapturingLogger(),
    )
    application = CompositionRoot(
        dependencies,
        ":memory:",
        [HelloResearchPlugin()],
    ).build()
    application.request_shutdown()
    await application.run()
    print(
        json.dumps(
            {
                "status": application.health().system.value,
                "capabilities": len(application.catalog.capabilities),
                "skills": len(application.catalog.skills),
                "workflows": len(application.catalog.workflows),
                "loop_profiles": len(application.catalog.loop_profiles),
                "evaluators": len(application.catalog.evaluators),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
