from .base import CalendarBackend, CalendarError, Event
from .memory import InMemoryCalendar

__all__ = ["CalendarBackend", "CalendarError", "Event", "InMemoryCalendar", "build_calendar"]


def build_calendar(config, tz):
    """Pick the backend from config. Imports caldav lazily so tests stay light."""
    if config.calendar_backend == "icloud":
        from .icloud import ICloudCalendar

        return ICloudCalendar(config.icloud_username, config.icloud_password, tz, config.icloud_calendar)
    return InMemoryCalendar()
