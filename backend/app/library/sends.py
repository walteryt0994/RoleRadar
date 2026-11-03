"""What a task or run handed to an analysis service, by kind and by how sure that is (U2-C4 prerequisites).

Behind a real service's gate a request is a count of its input, made first,
and the generation after it (app.live_gate); the simulation and the other
providers of this process only generate. Each request comes back sent, when
the service answered (an error answer too), or maybe, when it was handed to
the SDK and no answer came: a timeout, a lost connection, or an end the
process never saw, so it may have reached the service. A request its
provider stopped before handing it to the SDK, with nothing reported for it,
is not counted at all; one still out counts as maybe until it comes back.
The pages say each kind and the total, and never count a maybe as sent.
"""
from dataclasses import dataclass, fields, replace
from enum import StrEnum


class Call(StrEnum):
    COUNT = "count"
    GENERATION = "generation"


class Delivery(StrEnum):
    NOT_SENT = "not_sent"
    MAYBE = "maybe"
    SENT = "sent"


@dataclass(frozen=True)
class Sends:
    count_sent: int = 0
    count_maybe: int = 0
    generation_sent: int = 0
    generation_maybe: int = 0

    def plus(self, call: Call, delivered: Delivery) -> "Sends":
        if delivered is Delivery.NOT_SENT:
            return self

        name = f"{call.value}_{'sent' if delivered is Delivery.SENT else 'maybe'}"

        return replace(self, **{name: getattr(self, name) + 1})

    def __add__(self, other: "Sends") -> "Sends":
        return Sends(*(getattr(self, field.name) + getattr(other, field.name) for field in fields(self)))

    def __sub__(self, other: "Sends") -> "Sends":
        return Sends(*(getattr(self, field.name) - getattr(other, field.name) for field in fields(self)))

    @property
    def sent(self) -> int:
        return self.count_sent + self.generation_sent

    @property
    def maybe(self) -> int:
        return self.count_maybe + self.generation_maybe

    @property
    def total(self) -> int:
        return self.sent + self.maybe


def sends_text(sends: Sends) -> str:
    """What the pages say after 已交给服务的请求：each kind and the total, what is only maybe apart."""
    text = (
        f"计数 {_times(sends.count_sent, sends.count_maybe)}、"
        f"生成 {_times(sends.generation_sent, sends.generation_maybe)}，合计 {sends.total} 次"
    )

    if sends.maybe:
        text += f"，其中 {sends.maybe} 次只是可能已发出"

    return text


def _times(sent: int, maybe: int) -> str:
    if not maybe:
        return f"{sent} 次"

    if not sent:
        return f"{maybe} 次（可能已发出）"

    return f"{sent + maybe} 次（其中 {maybe} 次可能已发出）"
