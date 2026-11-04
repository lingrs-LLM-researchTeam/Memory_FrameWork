"""Clock boundaries for automatic incremental summaries (China Standard Time)."""
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))
SLOT_MINUTES = (0, 480, 780, 1110, 1440)


def uses_fixed_slots(config):
    return config.get("reflection_engine.summary_trigger_mode", "rounds") == "daily_slots"


def slot_bounds(timestamp):
    """Return [start, end) for this message, independent of host timezone."""
    now = datetime.fromtimestamp(timestamp, SHANGHAI)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    minute = now.hour * 60 + now.minute
    for start, end in zip(SLOT_MINUTES, SLOT_MINUTES[1:]):
        if minute < end:
            return (int(midnight.timestamp()) + start * 60,
                    int(midnight.timestamp()) + end * 60)
    raise AssertionError("invalid minute of day")


def due_slot_messages(messages, cutoff, minimum):
    """Batch closed slots from one Shanghai day, preserving the cursor prefix."""
    first = messages[0].timestamp
    if first >= cutoff:
        return []
    midnight = datetime.fromtimestamp(first, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = (midnight + timedelta(days=1)).timestamp()
    day_start, through = midnight.timestamp(), min(cutoff, day_end)
    begin, end = slot_bounds(first)
    size = 0
    for message in messages:
        stamp = message.timestamp
        if not day_start <= stamp < through:
            break
        if not begin <= stamp < end:
            # Keep full batches independent even when resuming several missed slots.
            if size >= minimum:
                break
            begin, end = slot_bounds(stamp)
        size += 1
    if size < minimum and cutoff < day_end:
        return []
    return messages[:size]
