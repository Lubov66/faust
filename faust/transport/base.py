"""Base message transport implementation."""
import asyncio
import weakref
from itertools import count
from typing import Awaitable, Callable, Optional, List, Tuple, Type, cast
from ..codecs import loads
from ..exceptions import KeyDecodeError, ValueDecodeError
from ..types import (
    AppT, ConsumerCallback, ConsumerT, Event, EventRefT,
    K, KeyDecodeErrorCallback, ValueDecodeErrorCallback,
    Message, ProducerT, Topic, TransportT,
)
from ..utils.services import Service

__all__ = ['EventRef', 'Consumer', 'Producer', 'Transport']

# The Transport is responsible for:
#
#  - Holds reference to the app that created it.
#  - Creates new consumers/producers.
#
# The Consumer is responsible for:
#
#   - Holds reference to the transport that created it
#   - ... and the app via ``self.transport.app``.
#   - Has a callback that usually points back to ``Stream.on_message``.
#   - Receives messages and calls the callback for every message received.
#   - The messages are deserialized first, so the Consumer also handles that.
#   - Keep track of the message and it's acked/unacked status.
#   - If automatic acks are enabled the message is acked when the Event goes
#     out of scope (like any variable using reference counting).
#   - Commits the offset at an interval
#      - The current offset is based on range of the messages acked.
#
# The Producer is responsible for:
#
#   - Holds reference to the transport that created it
#   - ... and the app via ``self.transport.app``.
#   - Sending messages.
#
# To see a reference transport implementation go to:
#     faust/transport/aiokafka.py


class EventRef(weakref.ref, EventRefT):
    """Weak-reference to :class:`ModelT`.

    Remembers the offset of the event, even after event out of scope.
    """

    # Used for tracking when events go out of scope.

    def __init__(self, event: Event,
                 callback: Callable = None,
                 offset: int = None) -> None:
        super().__init__(event, callback)
        self.offset = offset


class Consumer(ConsumerT, Service):
    """Base Consumer."""

    #: This counter generates new consumer ids.
    _consumer_ids = count(0)

    _app: AppT
    _dirty_events: List[EventRefT] = None
    _acked: List[int] = None
    _current_offset: int = None

    def __init__(self, transport: TransportT,
                 *,
                 topic: Topic = None,
                 callback: ConsumerCallback = None,
                 on_key_decode_error: KeyDecodeErrorCallback = None,
                 on_value_decode_error: ValueDecodeErrorCallback = None,
                 commit_interval: float = None) -> None:
        assert callback is not None
        self.id = next(self._consumer_ids)
        self.transport = transport
        self._app = self.transport.app
        self.callback = callback
        self.topic = topic
        self.type = self.topic.type
        self.on_key_decode_error = on_key_decode_error
        self.on_value_decode_error = on_value_decode_error
        self._key_serializer = (
            self.topic.key_serializer or self._app.key_serializer)
        self._value_serializer = self._app.value_serializer
        self.commit_interval = (
            commit_interval or self._app.commit_interval)
        if self.topic.topics and self.topic.pattern:
            raise TypeError('Topic can specify either topics or pattern')
        self._dirty_events = []
        self._acked = []
        super().__init__(loop=self.transport.loop)

    async def register_timers(self) -> None:
        asyncio.ensure_future(self._commit_handler(), loop=self.loop)

    async def on_message(self, message: Message) -> None:
        try:
            k, v = self.to_KV(message)
        except KeyDecodeError as exc:
            if not self.on_key_decode_error:
                raise
            await self.on_key_decode_error(exc, message)
        except ValueDecodeError as exc:
            if not self.on_value_decode_error:
                raise
            await self.on_value_decode_error(exc, message)
        self.track_event(v, message.offset)
        await self.callback(self.topic, k, v)

    def to_KV(self, message: Message) -> Tuple[K, Event]:
        key = message.key
        if self._key_serializer:
            try:
                key = loads(self._key_serializer, message.key)
            except Exception as exc:
                raise KeyDecodeError(exc)
        k = cast(K, key)
        try:
            v = self.type.from_message(  # type: ignore
                k, message, self._app,
                default_serializer=self._value_serializer,
            )
        except Exception as exc:
            raise ValueDecodeError(exc)
        return k, v

    def track_event(self, event: Event, offset: int) -> None:
        self._dirty_events.append(
            EventRef(event, self.on_event_ready, offset=offset))

    def on_event_ready(self, ref: EventRefT) -> None:
        print('ACKED MESSAGE %r' % (ref.offset,))
        self._acked.append(ref.offset)
        self._acked.sort()

    async def _commit_handler(self) -> None:
        asyncio.sleep(self.commit_interval)
        while 1:
            try:
                offset = self._new_offset()
            except IndexError:
                pass
            else:
                if self._should_commit(offset):
                    self._current_offset = offset
                    await self._commit(offset)
            await asyncio.sleep(self.commit_interval)

    def _should_commit(self, offset) -> bool:
        return (
            self._current_offset is None or
            (offset and offset > self._current_offset)
        )

    def _new_offset(self) -> int:
        acked = self._acked
        for i, offset in enumerate(acked):
            if offset != acked[i - 1]:
                break
        else:
            raise IndexError()
        return offset


class Producer(ProducerT, Service):
    """Base Producer."""

    def __init__(self, transport: TransportT) -> None:
        self.transport = transport
        super().__init__(loop=self.transport.loop)

    async def send(
            self,
            topic: str,
            key: Optional[bytes],
            value: bytes) -> Awaitable:
        raise NotImplementedError()

    async def send_and_wait(
            self,
            topic: str,
            key: Optional[bytes],
            value: bytes) -> Awaitable:
        raise NotImplementedError()


class Transport(TransportT):
    """Message transport implementation."""

    #: Consumer subclass used for this transport.
    Consumer: Type

    #: Producer subclass used for this transport.
    Producer: Type

    def __init__(self, url: str, app: AppT,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.url = url
        self.app = app
        self.loop = loop

    def create_consumer(self, topic: Topic, callback: ConsumerCallback,
                        **kwargs) -> ConsumerT:
        return cast(ConsumerT, self.Consumer(
            self, topic=topic, callback=callback, **kwargs))

    def create_producer(self, **kwargs) -> ProducerT:
        return cast(ProducerT, self.Producer(self, **kwargs))
