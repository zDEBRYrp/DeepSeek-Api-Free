def run_inline(awaitable):
    """Выполнить корутину, которая использует только немедленные mock-awaits.

    Нужен unit-тестам без запуска Windows event loop. Если код реально ждёт
    незавершённый Future, тест намеренно падает вместо скрытого зависания.
    """
    iterator = awaitable.__await__()
    value = None
    while True:
        try:
            yielded = iterator.send(value)
        except StopIteration as done:
            return done.value
        value = None
        if yielded is None:
            continue
        if hasattr(yielded, "done") and yielded.done():
            value = yielded.result()
            continue
        raise RuntimeError(f"Корутинa потребовала реального event loop: {yielded!r}")


def collect_async_iter(iterator):
    items = []
    while True:
        try:
            items.append(run_inline(iterator.__anext__()))
        except StopAsyncIteration:
            return items
