"""Fixed self_learning delivery contract, with ledger reconciliation on reload."""
import asyncio


def required_companion(context):
    for star in context.get_all_stars() or []:
        name = str(getattr(star, "name", "")) + " " + str(getattr(star, "module_path", ""))
        if "self_learning" not in name.lower():
            continue
        instance = getattr(star, "star_cls", None)
        handler = getattr(instance, "on_memory_source_event", None)
        if callable(handler):
            return instance, handler
    # Absence is not acknowledgement. This deployment requires this consumer.
    raise RuntimeError("self_learning source consumer is not ready")


async def notify_source_companions(context, event):
    _, handler = required_companion(context)
    await handler(event)


class SourceCompanionDelivery:
    """Delivery succeeds only after this consumer has reconciled durable state."""
    def __init__(self, context, store, *, consumer_provider=None):
        self.context, self.store = context, store
        self._consumer_provider = consumer_provider
        self._reconciled_instance = None
        self._lock = asyncio.Lock()

    async def reconcile(self):
        async with self._lock:
            instance, handler = self._consumer()
            if instance is self._reconciled_instance:
                return
            after = 0
            while True:
                cursor = await self.store.connection.execute(
                    "SELECT rowid AS sequence,session_id,source_uid,revision,state FROM source_ledger "
                    "WHERE rowid>? AND (state!='active' OR revision>0) ORDER BY rowid LIMIT 200", (after,))
                rows = await cursor.fetchall()
                if not rows:
                    break
                for row in rows:
                    event = dict(row)
                    if event["state"] == "active":
                        event["state"] = "revised"
                    await handler(event)
                after = rows[-1]["sequence"]
                await asyncio.sleep(0)
            # A failure leaves this unset; restart/reload always repeats safely.
            self._reconciled_instance = instance

    async def __call__(self, event):
        await self.reconcile()
        _, handler = self._consumer()
        await handler(event)

    def _consumer(self):
        if self._consumer_provider is not None:
            return self._consumer_provider()
        return required_companion(self.context)
