"""EventBus: pub/sub, async dispatch, thread-safe publish_sync."""

import asyncio
import threading
import time

from agent.services.event_bus import Event, EventBus


class TestEvent:
    def test_accessors(self):
        ev = Event(topic="t", data={"a": 1}, source="x")
        assert ev["a"] == 1
        assert ev.get("a") == 1
        assert ev.get("missing", 9) == 9
        assert ev.topic == "t"
        assert ev.source == "x"
        assert ev.ts > 0

    def test_to_dict(self):
        d = Event(topic="t", data={"a": 1}).to_dict()
        assert d["topic"] == "t"
        assert d["data"] == {"a": 1}
        assert "ts" in d and "source" in d

    def test_emit_factory(self):
        ev = EventBus.emit("t", {"a": 1}, source="svc")
        assert ev.topic == "t"
        assert ev.data == {"a": 1}
        assert ev.source == "svc"


class TestPublish:
    def test_sync_handler_called(self):
        bus = EventBus()
        seen = []
        bus.subscribe("t", lambda e: seen.append(e.data))
        asyncio.run(bus.publish(Event("t", {"v": 1})))
        assert seen == [{"v": 1}]

    def test_async_handler_awaited(self):
        bus = EventBus()
        order = []

        async def handler(e):
            order.append("start")
            await asyncio.sleep(0.01)
            order.append("end")

        bus.subscribe("t", handler)
        asyncio.run(bus.publish(Event("t", {})))
        assert order == ["start", "end"]

    def test_wildcard_catch_all(self):
        bus = EventBus()
        got = []
        bus.subscribe("*", lambda e: got.append(("all", e.topic)))
        bus.subscribe("t", lambda e: got.append(("exact", e.topic)))
        asyncio.run(bus.publish(Event("t", {})))
        assert got == [("exact", "t"), ("all", "t")]

    def test_handler_error_isolated(self):
        bus = EventBus()
        calls = []

        def bad(e):
            raise ValueError("boom")

        def good(e):
            calls.append(1)

        bus.subscribe("t", bad)
        bus.subscribe("t", good)
        asyncio.run(bus.publish(Event("t", {})))
        assert calls == [1]

    def test_unsubscribe(self):
        bus = EventBus()
        got = []

        def h(e):
            got.append(1)

        bus.subscribe("t", h)
        bus.unsubscribe("t", h)
        asyncio.run(bus.publish(Event("t", {})))
        assert got == []

    def test_unsubscribe_wildcard(self):
        bus = EventBus()
        got = []

        def h(e):
            got.append(1)

        bus.subscribe("*", h)
        bus.unsubscribe("*", h)
        asyncio.run(bus.publish(Event("t", {})))
        assert got == []

    def test_has_handlers(self):
        bus = EventBus()
        assert not bus.has_handlers("t")
        bus.subscribe("t", lambda e: None)
        assert bus.has_handlers("t")


class TestPublishSync:
    def test_schedules_onto_running_loop(self):
        bus = EventBus()
        seen = []
        bus.subscribe("t", lambda e: seen.append(e.data))
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        try:
            bus.bind(loop)
            bus.publish_sync(Event("t", {"v": 1}))
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.01)
            assert seen == [{"v": 1}]
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join()

    def test_sync_dispatch_without_loop(self):
        bus = EventBus()
        seen = []
        bus.subscribe("t", lambda e: seen.append(e.data))
        bus.publish_sync(Event("t", {"v": 2}))
        assert seen == [{"v": 2}]

    def test_sync_dispatch_rejects_async_handler(self):
        bus = EventBus()

        def handler(e):
            c = asyncio.sleep(0)
            c.close()
            return c

        bus.subscribe("t", handler)

        async def target():
            bus.publish_sync(Event("t", {}))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(target())
        except TypeError:
            pass
        else:
            raise AssertionError("async handler without loop must raise TypeError")
        finally:
            loop.close()
