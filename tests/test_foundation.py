import asyncio
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID

import pytest

from active_agent_platform.foundation import (
    CapturingLogger,
    FakeClock,
    FakeUuidGenerator,
    RuntimeDependencies,
    Settings,
    StdlibLogger,
    SystemClock,
    Uuid7Generator,
)
from brain_kernel.ports import LogLevel

START = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_system_clock_provides_utc_monotonic_and_nonblocking_sleep() -> None:
    clock = SystemClock()
    before = clock.monotonic()
    await clock.sleep(0)
    after = clock.monotonic()

    assert clock.now().tzinfo is UTC
    assert after >= before
    with pytest.raises(ValueError, match="non-negative"):
        await clock.sleep(-1)


@pytest.mark.asyncio
async def test_fake_clock_wakes_only_at_virtual_deadline() -> None:
    clock = FakeClock(START)
    sleeper = asyncio.create_task(clock.sleep(10))
    await asyncio.sleep(0)

    clock.advance(9)
    await asyncio.sleep(0)
    assert not sleeper.done()
    assert clock.now() == datetime(2026, 8, 17, 8, 0, 9, tzinfo=UTC)

    clock.advance(1)
    await sleeper
    assert clock.monotonic() == 10


@pytest.mark.asyncio
async def test_fake_clock_handles_cancelled_sleeper() -> None:
    clock = FakeClock(START)
    sleeper = asyncio.create_task(clock.sleep(10))
    await asyncio.sleep(0)
    sleeper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sleeper
    clock.advance(10)


@pytest.mark.asyncio
async def test_fake_clock_zero_sleep_yields_and_negative_sleep_fails() -> None:
    clock = FakeClock(START)
    await clock.sleep(0)
    with pytest.raises(ValueError, match="non-negative"):
        await clock.sleep(-1)


def test_clock_rejects_naive_start_and_negative_changes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(datetime.fromisoformat("2026-08-17T00:00:00"))
    clock = FakeClock(START)
    with pytest.raises(ValueError, match="non-negative"):
        clock.advance(-1)


def test_uuid7_is_valid_and_ordered_with_same_timestamp() -> None:
    clock = FakeClock(START)
    generator = Uuid7Generator(clock, random_bits=lambda _: 1)

    first = generator.new()
    second = generator.new()

    assert first.version == 7
    assert first.variant == "specified in RFC 4122"
    assert first.int < second.int


def test_uuid7_remains_ordered_when_wall_clock_moves_back() -> None:
    clock = FakeClock(START)
    generator = Uuid7Generator(clock, random_bits=lambda _: 10)
    first = generator.new()
    clock._now = datetime(2026, 8, 16, tzinfo=UTC)  # Simulate an operating-system correction.
    second = generator.new()
    assert first.int < second.int


def test_uuid7_entropy_wrap_advances_logical_millisecond() -> None:
    clock = FakeClock(START)
    generator = Uuid7Generator(clock, random_bits=lambda _: (1 << 74) - 1)
    first = generator.new()
    second = generator.new()

    first_millis = first.int >> 80
    second_millis = second.int >> 80
    assert second_millis == first_millis + 1


def test_fake_uuid_sequence_is_deterministic_and_fails_when_exhausted() -> None:
    expected = UUID("00000000-0000-0000-0000-000000000001")
    generator = FakeUuidGenerator([expected])
    assert generator.new() == expected
    with pytest.raises(RuntimeError, match="exhausted"):
        generator.new()


def test_settings_load_and_validate_environment() -> None:
    settings = Settings.from_env(
        {
            "BIA_SERVICE_NAME": "worker",
            "BIA_ENVIRONMENT": "test",
            "BIA_LOG_LEVEL": "warning",
            "BIA_SHUTDOWN_TIMEOUT_SECONDS": "2.5",
        }
    )
    assert settings.service_name == "worker"
    assert settings.log_level is LogLevel.WARNING
    assert settings.shutdown_timeout_seconds == 2.5

    with pytest.raises(ValueError, match="invalid BIA_LOG_LEVEL"):
        Settings.from_env({"BIA_LOG_LEVEL": "verbose"})
    with pytest.raises(ValueError, match="must be positive"):
        Settings(shutdown_timeout_seconds=0)


def test_settings_reject_invalid_names_timeout_and_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="service_name"):
        Settings(service_name="  ")
    with pytest.raises(ValueError, match="environment"):
        Settings(environment="")
    with pytest.raises(ValueError, match="must be numeric"):
        Settings.from_env({"BIA_SHUTDOWN_TIMEOUT_SECONDS": "later"})

    monkeypatch.setenv("BIA_SERVICE_NAME", "from-process")
    settings = Settings.from_env()
    assert settings.service_name == "from-process"
    with pytest.raises(FrozenInstanceError):
        settings.service_name = "changed"  # type: ignore[misc]


def test_loggers_emit_structured_fields(caplog: pytest.LogCaptureFixture) -> None:
    capturing = CapturingLogger()
    capturing.emit(LogLevel.INFO, "runtime.started", component="loop", attempt=1)
    assert capturing.records[0].event == "runtime.started"
    assert capturing.records[0].fields == {"component": "loop", "attempt": 1}

    logger = logging.getLogger("bia.tests")
    with caplog.at_level(logging.INFO, logger="bia.tests"):
        StdlibLogger(logger).emit(LogLevel.INFO, "runtime.started", component="loop")
    assert caplog.records[-1].structured_fields == {"component": "loop"}


def test_runtime_dependencies_accept_test_doubles() -> None:
    clock = FakeClock(START)
    logger = CapturingLogger()
    identifiers = FakeUuidGenerator([UUID(int=1)])
    dependencies = RuntimeDependencies(Settings(), clock, identifiers, logger)

    assert dependencies.clock is clock
    assert dependencies.uuid.new() == UUID(int=1)
